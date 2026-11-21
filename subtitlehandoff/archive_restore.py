"""Restore project bytes from the three-disk sharded archive.

恢复只接受同一完整已提交世代的片：提交回执须在至少两块剩余盘上
有账本已验证记录且盘上回执内容一致；使用的每一片须账本已校验、
盘上回执一致且摘要重验通过。缺一片时用异或校验片重建并回验整文件
摘要；两片缺失或剩余片分属不同世代只能待核。恢复决定由写入状态驱动，
不再读取 SQLite 中的 base64 正文。

灾难恢复用途凭同一已提交世代任意两片重建；保全用途还要求三块盘的
签收回执都在案，缺一份即待补签收，即使技术上能重建文件。
"""
import hashlib
import json
from pathlib import Path
from .ingest import capture
from .store import connect, decide, save
from .archive import (COMMIT_QUORUM, VERIFIED, _get, _generation_dir, _read_json,
                      _receipt_matches, _sha256)


def _commit_receipt_verified(db, gen_id, disk):
    row = _get(db, 'archive_commit', f'{gen_id}:commit:{disk}')
    return bool(row) and row['status'] == VERIFIED


def _usable_shards(db, gbody, attached, proof):
    """逐片核对账本状态、盘上回执与实时摘要，返回 {片号: 字节}。"""
    project, pver = gbody['project'], gbody['project_version']
    gen_id = f'{project}:{pver}'
    usable = {}
    for shard in gbody['shards']:
        index, disk = shard['index'], shard['disk']
        ledger = _get(db, 'archive_shard', f'{gen_id}:{index}')
        if not ledger or ledger['status'] != VERIFIED:
            proof['notes'].append(f'shard_not_verified:{pver}:{index}')
            continue
        if disk not in attached:
            continue
        gdir = _generation_dir(attached[disk], project, pver)
        shard_file = gdir / f'shard-{index}.bin'
        receipt = _read_json(gdir / f'receipt-{index}.json')
        data = shard_file.read_bytes() if shard_file.is_file() else None
        if (data is not None and _sha256(data) == shard['sha256']
                and _receipt_matches(receipt, json.loads(ledger['body']), state='verified')):
            usable[index] = data
        else:
            proof['corrupt'].append({'generation': pver, 'shard_index': index, 'disk': disk})
    return usable


def _committed(db, gbody, attached, proof):
    """已提交世代：至少两块当前挂载的盘持有已验证且内容一致的提交回执。"""
    project, pver = gbody['project'], gbody['project_version']
    gen_id = f'{project}:{pver}'
    verified = set()
    for row in db.execute('SELECT * FROM archive_commit'):
        if not row['id'].startswith(f'{gen_id}:commit:') or row['status'] != VERIFIED:
            continue
        cbody = json.loads(row['body'])
        disk = cbody['disk']
        if disk not in attached:
            continue
        receipt = _read_json(_generation_dir(attached[disk], project, pver) / 'commit.json')
        if _receipt_matches(receipt, cbody, state='verified'):
            verified.add(disk)
        else:
            proof['notes'].append(f'commit_receipt_unreadable:{pver}:{disk}')
    if len(verified) < COMMIT_QUORUM:
        proof['rejected'].append({'generation': pver, 'reason': 'not_committed',
                                  'verified_commit_receipts': sorted(verified)})
        return False
    return True


def _reconstruct(gbody, usable, proof):
    """用同一世代的任意两片还原原字节；缺数据片时以校验片异或重建并回验。"""
    pver = gbody['project_version']
    meta = {shard['index']: shard for shard in gbody['shards']}
    chunk = gbody['chunk']
    rebuilt = []
    if 0 in usable and 1 in usable:
        d0, d1 = usable[0], usable[1]
    elif 0 in usable and 2 in usable:
        d0 = usable[0]
        d1 = bytes(a ^ b for a, b in zip(usable[2], usable[0]))[:meta[1]['length']]
        rebuilt.append(1)
    else:
        d1 = usable[1]
        d0 = bytes(a ^ b for a, b in zip(usable[2], usable[1].ljust(chunk, b'\x00')))[:meta[0]['length']]
        rebuilt.append(0)
    for index in rebuilt:
        piece = d0 if index == 0 else d1
        if _sha256(piece) != meta[index]['sha256']:
            proof['rejected'].append({'generation': pver, 'reason': 'hash_mismatch',
                                      'rebuilt_shard': index})
            return None
    raw = (d0 + d1)[:gbody['file_length']]
    if _sha256(raw) != gbody['file_sha256']:
        proof['rejected'].append({'generation': pver, 'reason': 'hash_mismatch'})
        return None
    shards_used = [{'index': index, 'disk': meta[index]['disk'],
                    'source': 'rebuilt' if index in rebuilt else 'disk'} for index in (0, 1)]
    if rebuilt:
        shards_used.append({'index': 2, 'disk': meta[2]['disk'], 'source': 'disk'})
    disks_used = sorted({entry['disk'] for entry in shards_used if entry['source'] == 'disk'})
    return {'generation': pver, 'raw': raw, 'file_sha256': gbody['file_sha256'],
            'disks_used': disks_used, 'shards_used': shards_used, 'shards_rebuilt': rebuilt}


def _assess(db, gbody, attached, proof):
    """评估一个世代：已提交且同世代可用片不少于两片才可重建。"""
    if not _committed(db, gbody, attached, proof):
        return None
    usable = _usable_shards(db, gbody, attached, proof)
    if len(usable) < 2:
        proof['rejected'].append({'generation': gbody['project_version'],
                                  'reason': 'insufficient_shards',
                                  'usable_shards': sorted(usable)})
        return None
    return _reconstruct(gbody, usable, proof)


def _restore_row(db, source, row):
    key = str(row['id'])
    project = str(row['project'])
    attached = {str(disk['id']): str(disk['path']) for disk in row.get('disks') or [] if disk.get('path')}
    proof = {'project': project, 'out': row.get('out'), 'disks_attached': sorted(attached),
             'disks_used': [], 'shards_used': [], 'shards_rebuilt': [], 'corrupt': [],
             'rejected': [], 'notes': []}
    task = _get(db, 'archive_task', project)
    if not task:
        status, reason = '待核', 'task_missing'
        return _record(db, source, key, proof, status, reason)
    tbody = json.loads(task['body'])
    roster = tbody['roster']
    requested = str(row.get('purpose') or tbody['purpose'])
    proof['purpose'] = requested
    proof['task_purpose'] = tbody['purpose']
    for disk in sorted(set(attached) - set(roster)):
        proof['notes'].append(f'unknown_disk:{disk}')
    attached = {disk: path for disk, path in attached.items() if disk in roster}
    if requested not in ('disaster_recovery', 'preservation'):
        return _record(db, source, key, proof, '待核', 'purpose_unknown')
    if requested == 'preservation' and tbody['purpose'] != 'preservation':
        # 用途在任务建立前锁定，不能断盘后改名规避签收要求
        return _record(db, source, key, proof, '待核', 'preservation_not_locked')
    generations = sorted((json.loads(r['body']) for r in db.execute('SELECT body FROM archive_generation')),
                         key=lambda g: g['seq'], reverse=True)
    plan = None
    for gbody in [g for g in generations if g['project'] == project]:
        plan = _assess(db, gbody, attached, proof)
        if plan:
            break
    if not plan:
        return _record(db, source, key, proof, '待核', 'no_recoverable_generation')
    proof.update({'selected_generation': plan['generation'], 'file_sha256': plan['file_sha256'],
                  'disks_used': plan['disks_used'], 'shards_used': plan['shards_used'],
                  'shards_rebuilt': plan['shards_rebuilt']})
    if requested == 'preservation':
        gen_id = f"{project}:{plan['generation']}"
        gap = [disk for disk in roster if not _commit_receipt_verified(db, gen_id, disk)]
        if gap:
            proof['preservation_gap'] = sorted(gap)
            return _record(db, source, key, proof, '待补签收', 'preservation_receipt_missing')
        proof['certificate'] = {'complete': True, 'receipts': sorted(roster),
                                'file_sha256': plan['file_sha256']}
        _write_out(row, plan['raw'])
        return _record(db, source, key, proof, '已恢复', 'preservation_complete')
    _write_out(row, plan['raw'])
    return _record(db, source, key, proof, '已恢复', 'bytes_restored')


def _write_out(row, raw):
    target = Path(row['out'])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)


def _record(db, source, key, proof, status, reason):
    save(db, 'restore_record', key, source, 'restore', proof, status, reason)
    return decide(db, '离线工程恢复', key, status, reason, source)


def restore(path, file):
    """按当前挂载的盘恢复工程字节；同一请求重放结果一致。"""
    source, payload = capture(path, 'desktop_restore_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                results.append(_restore_row(db, source, row))
        return results
    finally:
        db.close()
