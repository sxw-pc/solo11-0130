"""离线编辑器插件环境：清单采录、签发包、环境快照、安装与回滚。

全部材料来自本地文件，不访问网络。签发包使用工作室预置离线密钥做
HMAC-SHA256 签发，未签发、摘要不符或编辑器 ABI 不符的包一律不可选。
"""
import hashlib
import hmac
import json
from .ingest import capture
from .store import (connect, save, decide, active_plugins, set_active_plugin,
                    deactivate_plugin, freeze_environment, restore_environment,
                    next_run_seq)

PURPOSES = ('播出', '测试')
PACKAGE_TABLE = 'signed_package'


def canonical(payload):
    """签发与重放共用的规范化编码：键序、分隔符固定，字节可复现。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def package_digest(row):
    basis = {k: v for k, v in row.items() if k not in ('digest', 'signature')}
    return hashlib.sha256(canonical(basis)).hexdigest()


def sign_package_row(row, signing_key):
    """离线签发工具：返回带 digest 与 signature 的新行（样例制备用）。"""
    signed = dict(row)
    signed['digest'] = package_digest(signed)
    signed['signature'] = hmac.new(str(signing_key).encode('utf-8'),
                                   signed['digest'].encode('utf-8'),
                                   hashlib.sha256).hexdigest()
    return signed


def verify_package_row(row, signing_key):
    """返回 (是否通过, 原因)。先核摘要再核 HMAC，任一不过即不可选。"""
    if not row.get('digest') or not row.get('signature'):
        return False, 'package_unsigned'
    if not hmac.compare_digest(package_digest(row), str(row['digest'])):
        return False, 'digest_mismatch'
    expected = hmac.new(str(signing_key).encode('utf-8'),
                        str(row['digest']).encode('utf-8'),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(row['signature'])):
        return False, 'signature_invalid'
    return True, 'signature_verified'


def _station(db, station_id):
    row = db.execute('SELECT * FROM station_profile WHERE id=?', (str(station_id),)).fetchone()
    return (json.loads(row['body']), row) if row else (None, None)


def register_station(path, file):
    """导入前固定工作站用途；用途一经固定不允许在升级失败后更改。"""
    source, payload = capture(path, 'desktop_station_profile', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                old = db.execute('SELECT body FROM station_profile WHERE id=?', (key,)).fetchone()
                if old:  # 重放或改用途：同体幂等，异体由 save 的 identity_conflict 拒绝
                    prior = json.loads(old['body'])
                    if prior.get('purpose') != row.get('purpose'):
                        raise ValueError('station_purpose_change_forbidden')
                    results.append(decide(db, '工作站用途', key, '用途冻结', 'station_purpose_frozen', source))
                    continue
                if row.get('purpose') not in PURPOSES:
                    status, reason = '拒绝', 'unknown_station_purpose'
                elif not row.get('editor_abi') or not row.get('signing_key'):
                    status, reason = '拒绝', 'station_profile_incomplete'
                else:
                    status, reason = '用途冻结', 'station_purpose_frozen'
                save(db, 'station_profile', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '工作站用途', key, status, reason, source))
        return results
    finally:
        db.close()


def import_inventory(path, file):
    """采录插件清单并重建活动环境；同名插件只允许一个活动版本。"""
    source, payload = capture(path, 'desktop_plugin_inventory', file)
    db = connect(path)
    results = []
    try:
        with db:
            seen_active = set()
            for row in payload.get('rows', []):
                key = str(row['id'])
                name = str(row['name'])
                wants_active = bool(row.get('active', True))
                if wants_active and name in seen_active:
                    status, reason = '未激活', 'duplicate_active_version'
                    wants_active = False
                else:
                    status, reason = '已安装', 'inventory_captured'
                save(db, 'plugin_inventory', key, source, payload['version'],
                     dict(row, active=wants_active), status, reason)
                if wants_active:
                    seen_active.add(name)
                    set_active_plugin(db, name, str(row.get('release', '')), source)
                results.append(decide(db, '编辑器插件清单', key, status, reason, source))
        return results
    finally:
        db.close()


def import_packages(path, file):
    """导入离线签发包：校验工作站、摘要、HMAC 签发与编辑器 ABI。"""
    source, payload = capture(path, 'desktop_signed_packages', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                station_id = str(row.get('station', payload.get('station', '')))
                profile, _ = _station(db, station_id)
                if not profile:
                    status, reason = '不可选', 'station_not_registered'
                else:
                    ok, reason = verify_package_row(row, profile['signing_key'])
                    if ok and str(row.get('abi')) != str(profile['editor_abi']):
                        status, reason = '不可选', 'abi_mismatch'
                    elif ok:
                        status = '已签发'
                    else:
                        status = '不可选'
                save(db, PACKAGE_TABLE, key, source, payload['version'], row, status, reason)
                results.append(decide(db, '离线签发包', key, status, reason, source))
        return results
    finally:
        db.close()


def take_snapshot(path, station_id, cause):
    """显式冻结当前环境（安装前基线/历史环境报告用）。"""
    db = connect(path)
    try:
        with db:
            seq = 1 + db.execute("SELECT COUNT(*) c FROM environment_snapshot").fetchone()['c']
            snapshot_id = f'snapshot:{station_id}:{seq}'
            body = freeze_environment(db, snapshot_id, None, 'manual', cause)
            decide(db, '环境快照', snapshot_id, '已冻结', 'snapshot_taken', None)
        return {'snapshot_id': snapshot_id, 'body': body}
    finally:
        db.close()


def _verified_package(db, package_id, signing_key):
    row = db.execute('SELECT * FROM signed_package WHERE id=?', (str(package_id),)).fetchone()
    if not row:
        return None, 'package_not_found'
    body = json.loads(row['body'])
    ok, reason = verify_package_row(body, signing_key)
    if not ok:
        return body, reason
    return body, 'signature_verified'


def apply_plan(path, plan_id, fail_step=None):
    """按安装计划执行整组变更；中途失败必须从冻结快照整体回滚。

    fail_step 仅供离线演练：在第 fail_step 个执行步（0 起）注入失败，
    验证回滚路径，生产调用不传该参数。相同计划成功重放不会二次安装。
    """
    db = connect(path)
    try:
        plan_row = db.execute('SELECT * FROM install_plan WHERE id=?', (str(plan_id),)).fetchone()
        if not plan_row:
            raise ValueError('plan_not_found')
        plan = json.loads(plan_row['body'])
        station_id = str(plan['station'])
        profile, _ = _station(db, station_id)
        if not profile:
            raise ValueError('station_not_registered')
        if profile['purpose'] == '播出':
            save(db, 'install_plan', plan_id, plan_row['source_id'], plan_row['version'],
                 plan, '禁止变更', 'station_frozen')
            decide(db, '安装计划', plan_id, '禁止变更', 'station_frozen', plan_row['source_id'])
            raise ValueError('station_frozen')
        if not plan.get('feasible'):
            raise ValueError('plan_not_feasible')
        prior = next((r for r in db.execute('SELECT body FROM install_run WHERE status=?', ('成功',))
                      if json.loads(r['body']).get('plan_id') == plan_id), None)
        if prior:
            raise ValueError('install_already_completed')
        seq = next_run_seq(db, plan_id)
        run_id = f'run:{plan_id}:{seq}'
        snapshot_id = f'snapshot:{plan_id}:run{seq}'
        failure = None
        result = None
        with db:
            frozen = freeze_environment(db, snapshot_id, plan_row['source_id'],
                                        plan_row['version'], f'before_install:{run_id}')
            # 执行前再次验证整组本地包：签发、摘要、ABI 任一失效即不开始
            steps = [s for s in plan['actions'] if s['action'] in ('安装', '卸载')]
            for step in steps:
                if step['action'] == '安装':
                    if not step.get('package_id'):
                        raise ValueError('offline_package_missing')
                    pkg, why = _verified_package(db, step['package_id'], profile['signing_key'])
                    if not pkg:
                        raise ValueError(why)
                    if str(pkg.get('abi')) != str(profile['editor_abi']):
                        raise ValueError('abi_mismatch')
            try:
                for index, step in enumerate(steps):
                    if fail_step == index:
                        raise RuntimeError('simulated_install_failure')
                    if step['action'] == '卸载':
                        deactivate_plugin(db, step['plugin'])
                    else:
                        set_active_plugin(db, step['plugin'], str(step['release']),
                                          plan_row['source_id'])
            except Exception as exc:
                # 注意：必须在 with 块内完成还原与落账并正常退出，否则上下文会
                # 把回滚记录一并回滚；异常在事务提交后再向上抛
                restore_environment(db, frozen)
                rollback_id = f'rollback:{run_id}'
                save(db, 'rollback_record', rollback_id, plan_row['source_id'], plan_row['version'],
                     {'plan_id': plan_id, 'run_id': run_id, 'snapshot_id': snapshot_id,
                      'failed_step': fail_step, 'error': str(exc),
                      'restored_active': frozen['active']},
                     '已回滚', 'restored_from_snapshot')
                save(db, 'install_run', run_id, plan_row['source_id'], plan_row['version'],
                     {'plan_id': plan_id, 'run_seq': seq, 'snapshot_id': snapshot_id,
                      'fail_step': fail_step}, '已回滚', 'partial_install_reverted')
                decide(db, '安装执行', run_id, '已回滚', 'partial_install_reverted',
                       plan_row['source_id'])
                failure = exc
            else:
                save(db, 'install_run', run_id, plan_row['source_id'], plan_row['version'],
                     {'plan_id': plan_id, 'run_seq': seq, 'snapshot_id': snapshot_id,
                      'completed_steps': len(steps)}, '成功', 'install_completed')
                save(db, 'install_plan', plan_id, plan_row['source_id'], plan_row['version'],
                     plan, '已安装', 'install_completed')
                decide(db, '安装执行', run_id, '成功', 'install_completed', plan_row['source_id'])
                result = {'run_id': run_id, 'snapshot_id': snapshot_id,
                          'active': active_plugins(db), 'status': '成功'}
        if failure is not None:
            raise failure
        return result
    finally:
        db.close()
