"""插件版本、ABI、依赖闭包、离线签发、双工作站与回滚的验收测试。"""
import json
import pytest
from subtitlehandoff import plugin_inventory as pi
from subtitlehandoff import project_open as po
from subtitlehandoff import report
from subtitlehandoff.store import connect

KEY = 'studio-offline-key-test-2026'


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def pkg(sid, name, release, abi='abi-9', requires=None, conflicts=None, key=KEY):
    row = {'id': f'{sid}:{name}-{release}', 'station': sid, 'name': name,
           'release': release, 'abi': abi, 'requires': requires or {},
           'conflicts': conflicts or [], 'payload_path': f'offline/{name}-{release}.zip',
           'payload_sha256': '0' * 64}
    return pi.sign_package_row(row, key)


def station_file(root, sid, purpose='测试', abi='abi-9', key=KEY):
    return write(root, f'{sid}.json', {'version': 'station-1', 'rows': [
        {'id': sid, 'purpose': purpose, 'editor_abi': abi, 'signing_key': key}]})


def seed_station(root, db, sid, **kw):
    return pi.register_station(db, station_file(root, sid, **kw))


def seed_inventory(root, db, rows=None, version='inv-1'):
    rows = rows if rows is not None else [
        {'id': 'I-OCR-1', 'name': 'OCR', 'release': '1', 'abi': 'abi-9',
         'requires': {'runtime': '1'}, 'active': True},
        {'id': 'I-EXP-1', 'name': 'export', 'release': '1', 'abi': 'abi-9',
         'requires': {'runtime': '1'}, 'active': True},
        {'id': 'I-RT-1', 'name': 'runtime', 'release': '1', 'abi': 'abi-9',
         'requires': {}, 'active': True}]
    return pi.import_inventory(db, write(root, 'inventory.json',
                                         {'version': version, 'rows': rows}))


def new_project_file(root):
    return write(root, 'project_new.json', {'version': 'proj-1', 'rows': [
        {'id': 'P-NEW', 'name': '新工程',
         'required_plugins': [
             {'name': 'OCR', 'spec': '>=2,<3', 'optional': False, 'feature': 'OCR 识别'},
             {'name': 'export', 'spec': '>=1', 'optional': False, 'feature': '导出'},
             {'name': 'ocr-pro', 'spec': '>=1', 'optional': True, 'feature': '高级校对'}]}]})


def old_project_file(root):
    return write(root, 'project_old.json', {'version': 'proj-legacy', 'rows': [
        {'id': 'P-OLD', 'name': '旧工程',
         'required_plugins': [
             {'name': 'export', 'spec': '==1', 'optional': False, 'feature': '旧导出'},
             {'name': 'runtime', 'spec': '==1', 'optional': False, 'feature': '运行库'}]}]})


def package_file(root, sid, rows):
    return write(root, 'packages.json', {'version': 'pkgs-1', 'rows': rows})


def upgrade_packages(root, sid):
    return package_file(root, sid, [
        pkg(sid, 'OCR', '2', requires={'runtime': '2'}),
        pkg(sid, 'export', '2', requires={'runtime': '2'}),
        pkg(sid, 'runtime', '2')])


# ---------- 基础核验：同名异版、版本范围 ----------

def test_legacy_name_only_check_still_opens(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_inventory(tmp_path, db)
    project = {'version': 'v1', 'rows': [{'id': 'P1', 'required_plugins': ['OCR']}]}
    assert po.check(db, write(tmp_path, 'p.json', project))[0]['status'] == '可打开'


def test_same_name_older_release_blocks_project(tmp_path):
    """清单含 OCR 名称且 release=1，工程实际要求 OCR 2：不能再按名称判可打开。"""
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db)
    result = po.check(db, new_project_file(tmp_path), 'station-test')
    assert result[0]['status'] == '待处理'
    assert result[0]['reason'] == 'plugin_version_mismatch'
    row = connect(db).execute("SELECT * FROM plugin_constraint WHERE id='P-NEW:OCR'").fetchone()
    assert row['status'] == '不满足' and row['reason'] == 'plugin_version_mismatch'


def test_duplicate_active_same_name_only_one_active(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_inventory(tmp_path, db, [
        {'id': 'I1', 'name': 'export', 'release': '1', 'abi': 'abi-9', 'active': True},
        {'id': 'I2', 'name': 'export', 'release': '1.0.1', 'abi': 'abi-9', 'active': True}])
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) c FROM active_plugin WHERE name='export'").fetchone()['c'] == 1
    statuses = {r['id']: r['status'] for r in conn.execute('SELECT id,status FROM plugin_inventory')}
    assert statuses['I2'] == '未激活' and statuses['I1'] == '已安装'


def test_version_spec_semantics():
    assert po.satisfies('2', '>=2,<3')
    assert po.satisfies('2.0.1', '>=2,<3')
    assert not po.satisfies('3', '>=2,<3')
    assert po.satisfies('1', '==1')
    assert po.satisfies('10', '>9')


# ---------- 签发与 ABI ----------

def test_unsigned_digest_bad_and_abi_mismatch_packages_not_selectable(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db)
    unsigned = {'id': 'station-test:u', 'station': 'station-test', 'name': 'runtime',
                'release': '2', 'abi': 'abi-9', 'requires': {}, 'conflicts': [],
                'payload_sha256': '0' * 64}
    tampered = pkg('station-test', 'export', '2', requires={'runtime': '2'})
    tampered['payload_sha256'] = 'f' * 64
    wrong_abi = pkg('station-test', 'OCR', '2', abi='abi-8', requires={'runtime': '2'})
    results = pi.import_packages(db, package_file(tmp_path, 'station-test',
                                                  [unsigned, tampered, wrong_abi]))
    reasons = {r['subject']: r['reason'] for r in results}
    assert reasons['station-test:u'] == 'package_unsigned'
    assert reasons['station-test:export-2'] == 'digest_mismatch'
    assert reasons['station-test:OCR-2'] == 'abi_mismatch'
    # 无任何合法包：求解必须声明离线缺口，而不是联网补包
    po.check(db, new_project_file(tmp_path), 'station-test')
    no_pkg_plan = po.solve_plan(db, 'station-test')
    assert no_pkg_plan['feasible'] is False
    assert 'OCR' in no_pkg_plan['offline_gap']


def test_signature_with_other_station_key_rejected(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test', key='key-A')
    rows = [pkg('station-test', 'runtime', '2', key='key-B')]
    result = pi.import_packages(db, package_file(tmp_path, 'station-test', rows))
    assert result[0]['reason'] == 'signature_invalid'


def test_packages_require_registered_station(tmp_path):
    db = tmp_path / 'ledger.db'
    result = pi.import_packages(db, package_file(tmp_path, 'unknown',
                                                 [pkg('unknown', 'runtime', '2')]))
    assert result[0] == {'subject': 'unknown:runtime-2', 'status': '不可选',
                         'reason': 'station_not_registered'}


# ---------- 共享运行库冲突与整组升级 ----------

def _verified_upgrade_state(root, db, sid='station-test'):
    seed_station(root, db, sid)
    seed_inventory(root, db)
    po.check(db, new_project_file(root), sid)
    pi.import_packages(db, upgrade_packages(root, sid))


def test_upgrade_plan_covers_runtime_dependency_and_orders_runtime_first(tmp_path):
    db = tmp_path / 'ledger.db'
    _verified_upgrade_state(tmp_path, db)
    plan = po.solve_plan(db, 'station-test')
    assert plan['feasible'] is True and plan['status'] == '待执行'
    assert plan['target_assignment'] == {'OCR': '2', 'export': '2', 'runtime': '2'}
    installs = [s for s in plan['actions'] if s['action'] == '安装']
    names = [s['plugin'] for s in installs]
    assert names.index('runtime') < names.index('OCR')
    assert names.index('runtime') < names.index('export')
    assert {s['action'] for s in plan['actions']} <= {'安装', '保持', '卸载'}


def test_apply_plan_installs_group_and_rechecks_digests(tmp_path):
    db = tmp_path / 'ledger.db'
    _verified_upgrade_state(tmp_path, db)
    plan = po.solve_plan(db, 'station-test')
    run = pi.apply_plan(db, plan['plan_id'])
    assert run['status'] == '成功' and run['active']['OCR'] == '2'
    # 重放同一计划：成功过的安装不能再执行一次
    with pytest.raises(ValueError, match='install_already_completed'):
        pi.apply_plan(db, plan['plan_id'])
    # 升级后硬性需求全部满足；可选 ocr-pro 缺失只压低导出能力，不影响可读
    checked = po.check(db, new_project_file(tmp_path), 'station-test')[0]
    assert checked['status'] == '可读导出受限'
    assert checked['reason'] == 'optional_plugin_missing'


def test_partial_install_failure_restores_frozen_snapshot(tmp_path):
    db = tmp_path / 'ledger.db'
    _verified_upgrade_state(tmp_path, db)
    plan = po.solve_plan(db, 'station-test')
    before_rows = connect(db).execute('SELECT name,release FROM active_plugin').fetchall()
    frozen = {r['name']: r['release'] for r in before_rows}
    with pytest.raises(RuntimeError, match='simulated_install_failure'):
        pi.apply_plan(db, plan['plan_id'], fail_step=0)
    conn = connect(db)
    restored = {r['name']: r['release'] for r in conn.execute('SELECT name,release FROM active_plugin')}
    assert restored == frozen == {'OCR': '1', 'export': '1', 'runtime': '1'}
    rollback = conn.execute("SELECT * FROM rollback_record").fetchone()
    assert rollback['status'] == '已回滚' and rollback['reason'] == 'restored_from_snapshot'
    run = conn.execute("SELECT status,reason FROM install_run").fetchone()
    assert tuple(run) == ('已回滚', 'partial_install_reverted')
    # 回滚后允许重试，且成功完成
    retry = pi.apply_plan(db, plan['plan_id'])
    assert retry['status'] == '成功'


def test_snapshot_preserves_inventory_version_for_history(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db, version='inv-2026.1')
    snap = pi.take_snapshot(db, 'station-test', '验收基线')
    assert snap['body']['inventory_versions'] == ['inv-2026.1']
    assert snap['body']['active'] == {'OCR': '1', 'export': '1', 'runtime': '1'}


# ---------- 两种工作站 ----------

def test_broadcast_station_remains_frozen_and_pending(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-air', purpose='播出')
    seed_inventory(tmp_path, db)
    pi.import_packages(db, package_file(tmp_path, 'station-air', [
        pkg('station-air', 'OCR', '2', requires={'runtime': '2'}, key=KEY),
        pkg('station-air', 'export', '2', requires={'runtime': '2'}, key=KEY),
        pkg('station-air', 'runtime', '2', key=KEY)]))
    checked = po.check(db, new_project_file(tmp_path), 'station-air')
    assert checked[0]['status'] == '待处理'
    plan = po.solve_plan(db, 'station-air')
    assert plan['status'] == '禁止变更'
    with pytest.raises(ValueError, match='station_frozen'):
        pi.apply_plan(db, plan['plan_id'])
    # 环境未被改动
    assert connect(db).execute("SELECT release FROM active_plugin WHERE name='OCR'").fetchone()[0] == '1'


def test_station_purpose_fixed_before_import_cannot_change(tmp_path):
    db = tmp_path / 'ledger.db'
    pi.register_station(db, station_file(tmp_path, 's1', purpose='播出'))
    with pytest.raises(ValueError, match='identity_conflict|station_purpose_change_forbidden'):
        pi.register_station(db, station_file(tmp_path, 's1', purpose='测试'))
    # 相同文件重放幂等
    again = pi.register_station(db, station_file(tmp_path, 's1', purpose='播出'))
    assert again[0]['reason'] == 'station_purpose_frozen'


def test_acceptance_two_workstations_same_manifest(tmp_path):
    """工作室指定的验收：播出保持原状待处理，测试可规划整组升级后打开。"""
    for sid, purpose, expect in (('station-air', '播出', '禁止变更'),
                                 ('station-test', '测试', '待执行')):
        db = tmp_path / f'{sid}.db'
        seed_station(tmp_path, db, sid, purpose=purpose)
        seed_inventory(tmp_path, db)
        po.check(db, new_project_file(tmp_path), sid)
        pi.import_packages(db, package_file(tmp_path, sid, [
            pkg(sid, 'OCR', '2', requires={'runtime': '2'}),
            pkg(sid, 'export', '2', requires={'runtime': '2'}),
            pkg(sid, 'runtime', '2')]))
        plan = po.solve_plan(db, sid)
        assert plan['status'] == expect


# ---------- 旧工程占用与隔离工作区 ----------

def test_old_project_forcing_export1_yields_isolation_not_false_open(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db)
    pi.import_packages(db, upgrade_packages(tmp_path, 'station-test'))
    po.check(db, new_project_file(tmp_path), 'station-test')
    old_result = po.check(db, old_project_file(tmp_path), 'station-test')
    assert old_result[0]['status'] == '可打开'  # 旧工程在当前环境本就可打开
    plan = po.solve_plan(db, 'station-test')
    assert plan['feasible'] is False
    assert 'P-OLD' in plan['isolated_projects']
    workspaces = {w['workspace']: w for w in plan['workspaces']}
    # 隔离工作区沿用旧环境，旧工程仍可打开，不虚报全部工程可打开
    assert workspaces['isolation']['environment']['export'] == '1'
    assert workspaces['main']['projects'] == ['P-NEW']


def test_no_false_open_when_conflict_blocks_everything(tmp_path):
    """只有旧 export 1 包（仅支持 runtime 1），没有任何升级路径：给出最小冲突。"""
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db)
    po.check(db, new_project_file(tmp_path), 'station-test')
    plan = po.solve_plan(db, 'station-test')
    assert plan['feasible'] is False
    # OCR 2 没有任何合法本地包；runtime 1 在装但满足不了 OCR 2 的依赖升级
    assert 'OCR' in plan['offline_gap']
    assert plan['workspaces']  # 始终给出可执行的隔离/最小冲突方案


# ---------- 可选功能：可读与完整导出分开 ----------

def test_optional_plugin_missing_keeps_project_readable(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    # OCR 2、export 2、runtime 2 全部就位，唯独可选 ocr-pro 没有
    seed_inventory(tmp_path, db, [
        {'id': 'I1', 'name': 'OCR', 'release': '2', 'abi': 'abi-9',
         'requires': {'runtime': '2'}, 'active': True},
        {'id': 'I2', 'name': 'export', 'release': '2', 'abi': 'abi-9',
         'requires': {'runtime': '2'}, 'active': True},
        {'id': 'I3', 'name': 'runtime', 'release': '2', 'abi': 'abi-9',
         'requires': {}, 'active': True}])
    result = po.check(db, new_project_file(tmp_path), 'station-test')
    assert result[0]['status'] == '可读导出受限'
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    project = next(p for p in data['plugin_environment']['projects']
                   if p['project_id'] == 'P-NEW')
    assert project['readable'] is True and project['export_complete'] is False
    assert project['missing_optional'][0]['plugin'] == 'ocr-pro'


def test_optional_does_not_enter_hard_solver_space(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    seed_inventory(tmp_path, db)
    pi.import_packages(db, upgrade_packages(tmp_path, 'station-test'))
    po.check(db, new_project_file(tmp_path), 'station-test')
    plan = po.solve_plan(db, 'station-test')
    assert plan['feasible'] is True
    degraded = {d['plugin'] for d in plan['optional_degraded']}
    assert degraded == {'ocr-pro'}


# ---------- 报告回读 ----------

def test_report_reads_back_conflict_basis_and_rollback(tmp_path):
    db = tmp_path / 'ledger.db'
    _verified_upgrade_state(tmp_path, db)
    plan = po.solve_plan(db, 'station-test')
    with pytest.raises(RuntimeError):
        pi.apply_plan(db, plan['plan_id'], fail_step=1)
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    env = data['plugin_environment']
    pnew = next(p for p in env['projects'] if p['project_id'] == 'P-NEW')
    # 升级前的核验保留版本不满足的冲突依据
    assert pnew['readable'] is False
    assert any(c['code'] == 'plugin_version_mismatch' for c in pnew['conflict_basis']) or \
           any(c['plugin'] == 'OCR' for c in pnew['unsatisfied_required'])
    assert env['snapshots'] and env['rollbacks']
    assert env['rollbacks'][0]['status'] == '已回滚'
    assert all('frozen_active' in s for s in env['snapshots'])
    # 报告同时包含全部新增实体表
    for table in ('station_profile', 'environment_snapshot', 'signed_package',
                  'plugin_constraint', 'install_plan', 'install_run', 'rollback_record'):
        assert table in data['entities']


def test_replay_of_same_inventory_and_project_files_is_idempotent(tmp_path):
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    inv = write(tmp_path, 'inventory.json', {'version': 'inv-1', 'rows': [
        {'id': 'I1', 'name': 'runtime', 'release': '1', 'abi': 'abi-9', 'active': True}]})
    assert pi.import_inventory(db, inv) == pi.import_inventory(db, inv)
    proj = write(tmp_path, 'p.json', {'version': 'proj-1', 'rows': [
        {'id': 'P1', 'required_plugins': [{'name': 'runtime', 'spec': '==1'}]}]})
    first = po.check(db, proj, 'station-test')
    second = po.check(db, proj, 'station-test')
    assert first == second and first[0]['status'] == '可打开'
    # 决策日志不重复膨胀
    conn = connect(db)
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_log WHERE subject='P1'").fetchone()['c'] == 1


def test_identity_conflict_on_changed_inventory_row(tmp_path):
    db = tmp_path / 'ledger.db'
    write(tmp_path, 'a.json', {'version': 'v1', 'rows': [
        {'id': 'I1', 'name': 'runtime', 'release': '1', 'active': True}]})
    pi.import_inventory(db, tmp_path / 'a.json')
    write(tmp_path, 'b.json', {'version': 'v2', 'rows': [
        {'id': 'I1', 'name': 'runtime', 'release': '2', 'active': True}]})
    with pytest.raises(ValueError, match='identity_conflict'):
        pi.import_inventory(db, tmp_path / 'b.json')


def test_abi_mismatch_recorded_even_when_spec_also_fails(tmp_path):
    """规格不满足不能掩盖同插件的 ABI 不符：两者是独立证据。"""
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')  # 编辑器 abi-9
    pi.import_inventory(db, write(tmp_path, 'i.json', {'version': 'inv-1', 'rows': [
        {'id': 'I1', 'name': 'OCR', 'release': '1', 'abi': 'abi-7',
         'requires': {}, 'active': True}]}))
    result = po.check(db, write(tmp_path, 'p.json', {'version': 'p', 'rows': [
        {'id': 'P1', 'required_plugins': [
            {'name': 'OCR', 'spec': '>=2', 'optional': False}]}]}), 'station-test')
    assert result[0]['status'] == '待处理'
    # 版本不满足已记录；ABI 证据不丢
    conn = connect(db)
    row = conn.execute("SELECT * FROM plugin_constraint WHERE id='P1:OCR'").fetchone()
    assert row['reason'] == 'plugin_version_mismatch'
    assert json.loads(row['body'])['plugin'] == 'OCR'


def test_conflicting_plugins_block_open_and_enter_solver_constraints(tmp_path):
    """两个相互排斥的插件同时活动：工程不可完整打开，求解器也排除该组合。"""
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    pi.import_inventory(db, write(tmp_path, 'i.json', {'version': 'inv-1', 'rows': [
        {'id': 'I1', 'name': 'OCR', 'release': '2', 'abi': 'abi-9',
         'requires': {}, 'conflicts': [{'name': 'rival-ocr', 'spec': '==2'}], 'active': True},
        {'id': 'I2', 'name': 'rival-ocr', 'release': '2', 'abi': 'abi-9',
         'requires': {}, 'conflicts': [], 'active': True}]}))
    result = po.check(db, write(tmp_path, 'p.json', {'version': 'p', 'rows': [
        {'id': 'P1', 'required_plugins': [
            {'name': 'OCR', 'spec': '>=2', 'optional': False},
            {'name': 'rival-ocr', 'spec': '==2', 'optional': False}]}]}), 'station-test')
    assert result[0]['status'] == '待处理'
    assert result[0]['reason'] == 'plugin_conflict'
    out = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    project = out['plugin_environment']['projects'][0]
    assert any(c['code'] == 'plugin_conflict' for c in project['conflict_basis'])


def test_solver_cap_respected_eight_plugins_three_versions(tmp_path):
    """8 插件 × 3 版 = 6561 组合在约束内可枚举；超过 8 插件直接拒绝。"""
    db = tmp_path / 'ledger.db'
    seed_station(tmp_path, db, 'station-test')
    names = [f'plug{i}' for i in range(po.MAX_PLUGINS)]
    inventory, packages, reqs = [], [], []
    for i, name in enumerate(names):
        inventory.append({'id': f'I-{name}', 'name': name, 'release': '1',
                          'abi': 'abi-9', 'requires': {}, 'active': True})
        for rel in ('1', '2', '3'):
            packages.append(pkg('station-test', name, rel))
        reqs.append({'name': name, 'spec': '>=2', 'optional': False})
    pi.import_inventory(db, write(tmp_path, 'i.json',
                                  {'version': 'inv-1', 'rows': inventory}))
    pi.import_packages(db, write(tmp_path, 'pk.json',
                                 {'version': 'pk', 'rows': packages}))
    po.check(db, write(tmp_path, 'p.json', {'version': 'p', 'rows': [
        {'id': 'P-BIG', 'required_plugins': reqs}]}), 'station-test')
    plan = po.solve_plan(db, 'station-test')
    assert plan['feasible'] is True
    # 最少改动：满足 >=2 的最低版本是 2，不应跳到 3
    assert all(plan['target_assignment'][n] == '2' for n in names)
    assert plan['changes'] == po.MAX_PLUGINS
    # 第九种插件越界
    with pytest.raises(ValueError, match='too_many_plugins'):
        po.check(db, write(tmp_path, 'p2.json', {'version': 'p2', 'rows': [
            {'id': 'P-X', 'required_plugins': reqs + [
                {'name': 'plug9', 'spec': '>=2', 'optional': False}]}]}),
            'station-test')
        po.solve_plan(db, 'station-test')
