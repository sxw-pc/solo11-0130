"""Three-disk sharded offline project archive with committed generations.

每个工程版本分成两份数据片和一份异或校验片，按任务建立时锁定的盘名册
分别写入三块轮换移动盘。备份区分三种状态：

- 已写完片：片字节和回执已落盘，尚未在后续挂载中重核；
- 已校验片：重启后的核对阶段重读盘上字节与回执，摘要验证通过；
- 已提交世代：至少两块盘上留有已验证的提交回执（提交仲裁数 2/3）。

用途（disaster_recovery 灾难恢复 / preservation 保全）在任务建立时锁定，
之后任何请求不得改名。局部重试重放同一请求只会续写同一世代，不会产生
新世代；同名不同正文按 identity_conflict 拒绝，原始记录不被覆盖。
"""
import hashlib
import json
from pathlib import Path
from .ingest import capture
from .store import connect, save, decide

SHARD_COUNT = 3        # 两份数据片 + 一份异或校验片
COMMIT_QUORUM = 2      # 已验证提交回执达到该数量，世代才算已提交
PURPOSES = ('disaster_recovery', 'preservation')
WRITTEN, VERIFIED, CORRUPT = '已写完', '已校验', '已损坏'
STATUS_TO_STATE = {WRITTEN: 'written', VERIFIED: 'verified', CORRUPT: 'corrupt'}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _split(raw):
    """Split bytes into two data shards and one XOR parity shard.

    奇数长度时第二份数据片较短（最后不满片），校验片按补零后的长度生成，
    每片的真实长度记入清单，重建时按记录长度截断。空文件三片皆为空。
    """
    chunk = (len(raw) + 1) // 2
    d0 = raw[:chunk]
    d1 = raw[chunk:]
    parity = bytes(a ^ b for a, b in zip(d0, d1.ljust(chunk, b'\x00')))
    return [d0, d1, parity], chunk


def _get(db, table, key):
    return db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()


def _issue(db, source, code, detail):
    db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)', (source, code, detail))


def _generation_dir(disk_path, project, pver):
    return Path(disk_path) / project / pver


def _read_json(file):
    try:
        return json.loads(Path(file).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_json(file, body):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')


def _receipt_matches(receipt, body, state=None):
    """盘上回执须覆盖账本回执全部字段；给定 state 时还需状态一致。"""
    if not isinstance(receipt, dict):
        return False
    if any(receipt.get(key) != value for key, value in body.items()):
        return False
    return state is None or receipt.get('state') == state


def _manifest(project, pver, raw, roster, seq):
    pieces, chunk = _split(raw)
    by_index = {index: disk for disk, index in roster.items()}
    shards = [{'index': index, 'disk': by_index[index], 'length': len(piece), 'sha256': _sha256(piece)}
              for index, piece in enumerate(pieces)]
    body = {'project': project, 'project_version': pver, 'seq': seq,
            'file_length': len(raw), 'file_sha256': _sha256(raw), 'chunk': chunk, 'shards': shards}
    return body, pieces


def _shard_receipt(gbody, shard):
    """片回执（账本与盘上共享的不可变字段）；状态列单独跟踪。"""
    return {'kind': 'shard', 'project': gbody['project'], 'project_version': gbody['project_version'],
            'shard_index': shard['index'], 'disk': shard['disk'], 'length': shard['length'],
            'sha256': shard['sha256'], 'file_sha256': gbody['file_sha256']}


def _commit_receipt(gbody, disk):
    """提交回执：某盘对整份清单的签收，内容不随挂载点变化。"""
    return {'kind': 'commit', 'project': gbody['project'], 'project_version': gbody['project_version'],
            'disk': disk, 'file_length': gbody['file_length'], 'file_sha256': gbody['file_sha256'],
            'shard_sha256': [shard['sha256'] for shard in gbody['shards']]}


def _lock_task(db, source, row, project, purpose, disks):
    """建立或核对归档任务：用途与盘名册在首次备份时锁定，之后不得改名。"""
    task = _get(db, 'archive_task', project)
    ids = [str(disk.get('id')) for disk in disks]
    if task:
        tbody = json.loads(task['body'])
        if tbody['purpose'] != purpose:
            _issue(db, source, 'purpose_conflict', f'archive_task:{project}')
            raise ValueError('purpose_conflict')
        unknown = [disk for disk in ids if disk not in tbody['roster']]
        if unknown:
            _issue(db, source, 'unknown_disk', f'archive_task:{project}:{",".join(sorted(unknown))}')
            raise ValueError('unknown_disk')
        return tbody['roster']
    if purpose not in PURPOSES:
        raise ValueError('purpose_unknown')
    if len(disks) != SHARD_COUNT or len(set(ids)) != SHARD_COUNT or any(not disk for disk in ids):
        raise ValueError('roster_required')
    roster = {disk: index for index, disk in enumerate(ids)}
    save(db, 'archive_task', project, source, 'task',
         {'project': project, 'purpose': purpose, 'roster': roster}, '已建立', 'task_created')
    return roster


def _lock_generation(db, source, project, pver, raw, roster):
    """建立或续用世代清单：同名同正文重放幂等，同名不同正文拒绝。"""
    gen_id = f'{project}:{pver}'
    gen = _get(db, 'archive_generation', gen_id)
    if gen:
        gbody = json.loads(gen['body'])
        fresh, pieces = _manifest(project, pver, raw, roster, gbody['seq'])
        if fresh != gbody:
            _issue(db, source, 'identity_conflict', f'archive_generation:{gen_id}')
            raise ValueError('identity_conflict')
        return gen_id, gbody, pieces
    seq = 1 + sum(1 for row in db.execute('SELECT body FROM archive_generation')
                  if json.loads(row['body'])['project'] == project)
    gbody, pieces = _manifest(project, pver, raw, roster, seq)
    save(db, 'archive_generation', gen_id, source, pver, gbody, '写入中', 'shards_incomplete')
    return gen_id, gbody, pieces


def _resume_verify(db, source, pver, gen_id, gbody, roster, attached):
    """重启核对：先重读各盘原始回执与片摘要，再决定补片或补提交记录。

    已写完的片与提交回执只有在这一阶段重读通过后才升为已校验；
    摘要不符或回执被篡改的片记为已损坏，等待写盘阶段重写。
    """
    for disk, dpath in attached.items():
        index = roster[disk]
        shard = gbody['shards'][index]
        shard_id = f'{gen_id}:{index}'
        ledger = _get(db, 'archive_shard', shard_id)
        gdir = _generation_dir(dpath, gbody['project'], gbody['project_version'])
        shard_file = gdir / f'shard-{index}.bin'
        if ledger and shard_file.is_file():
            data = shard_file.read_bytes()
            receipt = _read_json(gdir / f'receipt-{index}.json')
            body = json.loads(ledger['body'])
            ok = (_sha256(data) == shard['sha256']
                  and (receipt is None or _receipt_matches(receipt, body)))
            status, reason = (VERIFIED, 'digest_verified') if ok else (CORRUPT, 'digest_mismatch')
            save(db, 'archive_shard', shard_id, source, pver, body, status, reason)
            _write_json(gdir / f'receipt-{index}.json', {**body, 'state': STATUS_TO_STATE[status]})
        commit_id = f'{gen_id}:commit:{disk}'
        cledger = _get(db, 'archive_commit', commit_id)
        if cledger:
            cbody = json.loads(cledger['body'])
            creceipt = _read_json(gdir / 'commit.json')
            if creceipt is not None:
                ok = _receipt_matches(creceipt, cbody)
                status, reason = (VERIFIED, 'commit_verified') if ok else (CORRUPT, 'commit_mismatch')
                save(db, 'archive_commit', commit_id, source, pver, cbody, status, reason)
                _write_json(gdir / 'commit.json', {**cbody, 'state': STATUS_TO_STATE[status]})


def _write_shards(db, source, pver, gen_id, gbody, pieces, roster, attached):
    """补片：只写缺失或损坏的片；已写完的片留待下次挂载核对，不重复写。"""
    for disk, dpath in attached.items():
        index = roster[disk]
        shard = gbody['shards'][index]
        shard_id = f'{gen_id}:{index}'
        ledger = _get(db, 'archive_shard', shard_id)
        gdir = _generation_dir(dpath, gbody['project'], gbody['project_version'])
        shard_file = gdir / f'shard-{index}.bin'
        if ledger and ledger['status'] in (WRITTEN, VERIFIED) and shard_file.is_file() \
                and _sha256(shard_file.read_bytes()) == shard['sha256']:
            continue
        gdir.mkdir(parents=True, exist_ok=True)
        shard_file.write_bytes(pieces[index])
        body = _shard_receipt(gbody, shard)
        save(db, 'archive_shard', shard_id, source, pver, body, WRITTEN, 'shard_written')
        _write_json(gdir / f'receipt-{index}.json', {**body, 'state': 'written'})


def _commit(db, source, pver, gen_id, gbody, roster, attached):
    """补提交记录：三片全部已校验后，向当前挂载的盘补写提交回执。"""
    shards = [_get(db, 'archive_shard', f'{gen_id}:{index}') for index in range(SHARD_COUNT)]
    if not all(shard and shard['status'] == VERIFIED for shard in shards):
        return
    gen = _get(db, 'archive_generation', gen_id)
    if gen['status'] == '写入中':
        save(db, 'archive_generation', gen_id, source, pver, gbody, '待提交', 'shards_verified')
    for disk, dpath in attached.items():
        commit_id = f'{gen_id}:commit:{disk}'
        ledger = _get(db, 'archive_commit', commit_id)
        gdir = _generation_dir(dpath, gbody['project'], gbody['project_version'])
        if ledger and ledger['status'] in (WRITTEN, VERIFIED) and (gdir / 'commit.json').is_file():
            continue
        body = _commit_receipt(gbody, disk)
        save(db, 'archive_commit', commit_id, source, pver, body, WRITTEN, 'commit_written')
        _write_json(gdir / 'commit.json', {**body, 'state': 'written'})


def _refresh_commit(db, source, pver, gen_id, gbody):
    """已验证提交回执达到仲裁数（2/3 盘）时，世代升为已提交。"""
    prefix = f'{gen_id}:commit:'
    verified = sum(1 for row in db.execute('SELECT id,status FROM archive_commit')
                   if row['id'].startswith(prefix) and row['status'] == VERIFIED)
    gen = _get(db, 'archive_generation', gen_id)
    if verified >= COMMIT_QUORUM and gen['status'] != '已提交':
        save(db, 'archive_generation', gen_id, source, pver, gbody, '已提交', 'commit_quorum')


def _backup_row(db, source, row):
    key = str(row['id'])
    project = str(row['project'])
    pver = str(row['project_version'])
    purpose = str(row.get('purpose'))
    raw = Path(row['file']).read_bytes()
    disks = row.get('disks') or []
    roster = _lock_task(db, source, row, project, purpose, disks)
    attached = {str(disk['id']): str(disk['path']) for disk in disks
                if disk.get('path') and str(disk['id']) in roster}
    gen_id, gbody, pieces = _lock_generation(db, source, project, pver, raw, roster)
    _resume_verify(db, source, pver, gen_id, gbody, roster, attached)
    _write_shards(db, source, pver, gen_id, gbody, pieces, roster, attached)
    _commit(db, source, pver, gen_id, gbody, roster, attached)
    _refresh_commit(db, source, pver, gen_id, gbody)
    gen = _get(db, 'archive_generation', gen_id)
    return decide(db, '离线工程归档', key, gen['status'], gen['reason'], source)


def backup(path, file):
    """执行一轮三盘分片备份；可重入，断电后续挂载重放同一请求即可续传。"""
    source, payload = capture(path, 'desktop_archive_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                results.append(_backup_row(db, source, row))
        return results
    finally:
        db.close()
