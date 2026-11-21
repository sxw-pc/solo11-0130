"""三盘分片归档的场景测试：一盘遗失、单片损坏、混世代、断电半提交、
最后不满片、空文件、同名不同正文、盘符改变、两种用途与重放。"""
import hashlib
import json
import shutil

import pytest

from subtitlehandoff import archive, archive_restore, report
from subtitlehandoff.store import connect

DISKS = ('DISK-A', 'DISK-B', 'DISK-C')
DR = 'disaster_recovery'
PRES = 'preservation'


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def corrupt(file):
    data = bytearray(file.read_bytes())
    data[0] ^= 0xFF
    file.write_bytes(bytes(data))


class Rig:
    """合成三盘轮换环境：盘是本地目录，备份/恢复请求是 JSON 文件。"""

    def __init__(self, root):
        self.root = root
        self.db = root / 'ledger.db'
        self.paths = {disk: root / f'disk-{disk}' for disk in DISKS}
        self.n = 0
        self.last_id = None

    def roster(self, *attached):
        return [{'id': d, 'path': str(self.paths[d]) if d in attached else None} for d in DISKS]

    def backup(self, project, pver, purpose, content, attached, name=None):
        self.n += 1
        src = self.root / f'src-{self.n}.bin'
        src.write_bytes(content)
        self.last_id = f'JOB{self.n}'
        row = {'id': self.last_id, 'project': project, 'project_version': pver,
               'purpose': purpose, 'file': str(src), 'disks': self.roster(*attached)}
        req = write(self.root, name or f'backup-{self.n}.json', {'version': 'req', 'rows': [row]})
        return archive.backup(self.db, req)

    def commit(self, project, pver, purpose, content):
        result = None
        for _ in range(3):
            result = self.backup(project, pver, purpose, content, DISKS)
        assert result[0]['status'] == '已提交'
        return result

    def restore(self, project, out, attached, purpose=None, name=None):
        self.n += 1
        self.last_id = f'RES{self.n}'
        row = {'id': self.last_id, 'project': project, 'out': str(out),
               'disks': [{'id': d, 'path': str(self.paths[d])} for d in attached]}
        if purpose:
            row['purpose'] = purpose
        req = write(self.root, name or f'restore-{self.n}.json', {'version': 'req', 'rows': [row]})
        return archive_restore.restore(self.db, req)

    def row(self, table, key):
        db = connect(self.db)
        try:
            found = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
            return dict(found) if found else None
        finally:
            db.close()

    def rows(self, table):
        db = connect(self.db)
        try:
            return [dict(r) for r in db.execute(f'SELECT * FROM {table}')]
        finally:
            db.close()

    def proof(self):
        return json.loads(self.row('restore_record', self.last_id)['body'])

    def shard_file(self, disk, project, pver, index):
        return self.paths[disk] / project / pver / f'shard-{index}.bin'


def test_backup_progresses_through_write_verify_commit_states(tmp_path):
    rig = Rig(tmp_path)
    first = rig.backup('P1', 'v1', DR, b'alpha', DISKS)
    assert first[0]['status'] == '写入中'
    assert [r['status'] for r in rig.rows('archive_shard')] == ['已写完'] * 3
    second = rig.backup('P1', 'v1', DR, b'alpha', DISKS)
    assert second[0]['status'] == '待提交'
    assert [r['status'] for r in rig.rows('archive_shard')] == ['已校验'] * 3
    assert [r['status'] for r in rig.rows('archive_commit')] == ['已写完'] * 3
    third = rig.backup('P1', 'v1', DR, b'alpha', DISKS)
    assert third[0]['status'] == '已提交'
    assert [r['status'] for r in rig.rows('archive_commit')] == ['已校验'] * 3


def test_committed_generation_restores_after_disk_loss(tmp_path):
    rig = Rig(tmp_path)
    v1 = b'project export v1\n' * 11
    v2 = b'project export v2 changed\n' * 13
    rig.commit('P1', 'v1', DR, v1)
    rig.commit('P1', 'v2', DR, v2)
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A', 'DISK-B'))  # C 盘遗失
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == v2
    proof = rig.proof()
    assert proof['selected_generation'] == 'v2'
    assert proof['disks_used'] == ['DISK-A', 'DISK-B']
    assert proof['shards_rebuilt'] == []
    # 原始 v1 与 v2 记录都保留，恢复不覆盖任何世代
    assert rig.row('archive_generation', 'P1:v1')['status'] == '已提交'
    assert rig.row('archive_generation', 'P1:v2')['status'] == '已提交'


def test_parity_rebuilds_missing_data_shard(tmp_path):
    rig = Rig(tmp_path)
    content = bytes(range(256)) * 3 + b'odd-tail'  # 长度不整除，末片不满
    rig.commit('P1', 'v1', DR, content)
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-B', 'DISK-C'))  # A 盘（数据片 0）遗失
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == content
    proof = rig.proof()
    assert proof['shards_rebuilt'] == [0]
    assert proof['disks_used'] == ['DISK-B', 'DISK-C']
    out2 = tmp_path / 'restored2.bin'
    result2 = rig.restore('P1', out2, ('DISK-A', 'DISK-C'))  # B 盘（数据片 1）遗失
    assert result2[0]['status'] == '已恢复'
    assert out2.read_bytes() == content
    assert rig.proof()['shards_rebuilt'] == [1]


def test_single_byte_corruption_rebuilt_from_parity(tmp_path):
    rig = Rig(tmp_path)
    content = b'subtitle project body' * 40
    rig.commit('P1', 'v1', DR, content)
    corrupt(rig.shard_file('DISK-B', 'P1', 'v1', 1))  # 单字节损坏
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, DISKS)
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == content
    proof = rig.proof()
    assert proof['corrupt'] == [{'generation': 'v1', 'shard_index': 1, 'disk': 'DISK-B'}]
    assert proof['shards_rebuilt'] == [1]


def test_unverified_commit_receipts_fall_back_to_previous_generation(tmp_path):
    """断电半提交：v2 提交回执只写完未验证，恢复必须退回已提交的 v1。"""
    rig = Rig(tmp_path)
    v1, v2 = b'v1-bytes' * 9, b'v2-bytes' * 9
    rig.commit('P1', 'v1', DR, v1)
    rig.backup('P1', 'v2', DR, v2, DISKS)   # 片写完
    rig.backup('P1', 'v2', DR, v2, DISKS)   # 片校验 + 提交回执写完，随后断电
    assert rig.row('archive_generation', 'P1:v2')['status'] == '待提交'
    assert [r['status'] for r in rig.rows('archive_commit') if 'v2' in r['id']] == ['已写完'] * 3
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, DISKS)
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == v1
    proof = rig.proof()
    assert proof['selected_generation'] == 'v1'
    assert proof['rejected'][0]['generation'] == 'v2'
    assert proof['rejected'][0]['reason'] == 'not_committed'


def test_commit_record_on_one_disk_is_not_committed(tmp_path):
    """两块盘各有 v2 数据片，但提交记录只验证到一块盘，不能把 v2 当已提交。"""
    rig = Rig(tmp_path)
    v1, v2 = b'v1-content' * 7, b'v2-content' * 7
    rig.commit('P1', 'v1', DR, v1)
    rig.backup('P1', 'v2', DR, v2, DISKS)          # 三片写完
    rig.backup('P1', 'v2', DR, v2, DISKS)          # 三片校验，提交回执写到 A/B/C
    rig.backup('P1', 'v2', DR, v2, ('DISK-A',))    # 只有 A 的提交回执被验证
    assert rig.row('archive_generation', 'P1:v2')['status'] == '待提交'
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A', 'DISK-B'))
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == v1
    rejected = rig.proof()['rejected'][0]
    assert rejected['generation'] == 'v2'
    assert rejected['verified_commit_receipts'] == ['DISK-A']


def test_mixed_generation_shards_are_pending(tmp_path):
    """剩余两片分属不同世代，不能拼出混合版本，只能待核。"""
    rig = Rig(tmp_path)
    v1, v2 = b'v1-mixed' * 8, b'v2-mixed' * 8
    rig.commit('P1', 'v1', DR, v1)
    rig.backup('P1', 'v2', DR, v2, ('DISK-A',))   # v2 只有 A 的片 0 写完
    rig.backup('P1', 'v2', DR, v2, ('DISK-A',))   # v2 片 0 校验，片 1/2 从未写
    corrupt(rig.shard_file('DISK-A', 'P1', 'v1', 0))  # v1 在 A 上的片损坏
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A', 'DISK-B'))  # C 遗失
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'no_recoverable_generation'
    assert not out.exists()
    proof = rig.proof()
    reasons = {(r['generation'], r['reason']) for r in proof['rejected']}
    assert ('v2', 'not_committed') in reasons
    assert ('v1', 'insufficient_shards') in reasons


def test_two_missing_shards_are_pending(tmp_path):
    rig = Rig(tmp_path)
    rig.commit('P1', 'v1', DR, b'content' * 20)
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A',))  # 两盘遗失，只剩一片
    assert result[0]['status'] == '待核'
    assert not out.exists()


def test_unverified_shard_blocks_newer_generation(tmp_path):
    """A 的 v2 片只写完未验证，灾难恢复也不能用 v2，须退回已提交的 v1。"""
    rig = Rig(tmp_path)
    v1, v2 = b'v1-shard' * 6, b'v2-shard' * 6
    rig.commit('P1', 'v1', DR, v1)
    rig.backup('P1', 'v2', DR, v2, DISKS)               # 三片写完
    rig.backup('P1', 'v2', DR, v2, ('DISK-B', 'DISK-C'))  # 只有 B、C 的片被校验
    assert rig.row('archive_shard', 'P1:v2:0')['status'] == '已写完'
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, DISKS)
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == v1
    assert rig.proof()['selected_generation'] == 'v1'


def test_empty_file_roundtrip_with_parity_rebuild(tmp_path):
    rig = Rig(tmp_path)
    rig.commit('P1', 'v1', DR, b'')
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-B', 'DISK-C'))  # 缺片 0，空文件也走重建
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == b''
    assert rig.proof()['shards_rebuilt'] == [0]


def test_odd_length_partial_last_shard(tmp_path):
    rig = Rig(tmp_path)
    content = b'x' * 7  # chunk=4：片 0 四字节，片 1 三字节（末片不满），校验片四字节
    rig.commit('P1', 'v1', DR, content)
    assert json.loads(rig.row('archive_shard', 'P1:v1:1')['body'])['length'] == 3
    assert json.loads(rig.row('archive_shard', 'P1:v1:2')['body'])['length'] == 4
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A', 'DISK-C'))  # 重建较短的片 1
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == content


def test_preservation_requires_all_three_signed_receipts(tmp_path):
    """同一组分片证据：灾难恢复可重建 v2，保全用途缺 C 签收只能待补。"""
    rig = Rig(tmp_path)
    v2 = b'evidence-grade v2' * 10
    rig.backup('PP', 'v2', PRES, v2, DISKS)
    rig.backup('PP', 'v2', PRES, v2, DISKS)
    rig.backup('PP', 'v2', PRES, v2, ('DISK-A', 'DISK-B'))  # 只有 A、B 提交回执被验证
    assert rig.row('archive_generation', 'PP:v2')['status'] == '已提交'
    assert rig.row('archive_commit', 'PP:v2:commit:DISK-C')['status'] == '已写完'
    dr_out = tmp_path / 'dr.bin'
    result = rig.restore('PP', dr_out, ('DISK-A', 'DISK-B'), purpose=DR)  # C 遗失
    assert result[0]['status'] == '已恢复'
    assert dr_out.read_bytes() == v2
    pres_out = tmp_path / 'pres.bin'
    result = rig.restore('PP', pres_out, ('DISK-A', 'DISK-B'), purpose=PRES)
    assert result[0]['status'] == '待补签收'
    assert result[0]['reason'] == 'preservation_receipt_missing'
    assert not pres_out.exists()
    assert rig.proof()['preservation_gap'] == ['DISK-C']
    # 补签收：C 重新挂载后重放备份，提交回执补齐验证
    rig.backup('PP', 'v2', PRES, v2, DISKS)
    assert rig.row('archive_commit', 'PP:v2:commit:DISK-C')['status'] == '已校验'
    result = rig.restore('PP', pres_out, ('DISK-A', 'DISK-B'), purpose=PRES)
    assert result[0]['status'] == '已恢复'
    assert result[0]['reason'] == 'preservation_complete'
    assert pres_out.read_bytes() == v2
    assert rig.proof()['certificate']['receipts'] == ['DISK-A', 'DISK-B', 'DISK-C']


def test_preservation_cannot_be_claimed_on_disaster_recovery_task(tmp_path):
    """用途在任务建立前锁定，不能断盘后改名规避签收要求。"""
    rig = Rig(tmp_path)
    rig.commit('PD', 'v1', DR, b'dr-only' * 5)
    out = tmp_path / 'out.bin'
    result = rig.restore('PD', out, DISKS, purpose=PRES)
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'preservation_not_locked'
    assert not out.exists()
    result = rig.restore('PD', out, DISKS)  # 默认按任务锁定的用途
    assert result[0]['status'] == '已恢复'
    with pytest.raises(ValueError, match='purpose_conflict'):
        rig.backup('PD', 'v2', PRES, b'other' * 5, DISKS)


def test_same_version_different_content_rejected(tmp_path):
    rig = Rig(tmp_path)
    rig.backup('P1', 'v1', DR, b'original', DISKS)
    with pytest.raises(ValueError, match='identity_conflict'):
        rig.backup('P1', 'v1', DR, b'tampered', DISKS)
    body = json.loads(rig.row('archive_generation', 'P1:v1')['body'])
    assert body['file_sha256'] == sha256(b'original')  # 原始记录不被覆盖


def test_repeated_import_is_idempotent_replay(tmp_path):
    """一次任务重复导入：重放只沿状态机续走到不动点，不产生新世代或重复片。"""
    rig = Rig(tmp_path)
    src = tmp_path / 'project.bin'
    src.write_bytes(b'replay-content' * 3)
    row = {'id': 'JOB1', 'project': 'P1', 'project_version': 'v1', 'purpose': DR,
           'file': str(src), 'disks': rig.roster(*DISKS)}
    req = write(tmp_path, 'backup.json', {'version': 'req', 'rows': [row]})
    stages = [archive.backup(rig.db, req)[0]['status'] for _ in range(5)]
    assert stages == ['写入中', '待提交', '已提交', '已提交', '已提交']  # 收敛后重放结果一致
    assert len(rig.rows('archive_generation')) == 1
    assert len(rig.rows('archive_shard')) == 3
    assert len(rig.rows('archive_commit')) == 3
    assert len(rig.rows('export_file')) == 1  # 同一请求文件去重采录
    out = tmp_path / 'restored.bin'
    req = write(tmp_path, 'restore.json', {'version': 'req', 'rows': [
        {'id': 'RES1', 'project': 'P1', 'out': str(out),
         'disks': [{'id': d, 'path': str(rig.paths[d])} for d in DISKS]}]})
    first = archive_restore.restore(rig.db, req)
    snapshot = [json.loads(r['body']) for r in rig.rows('archive_generation')]
    second = archive_restore.restore(rig.db, req)  # 恢复重放结果一致
    assert first == second == [{'subject': 'RES1', 'status': '已恢复', 'reason': 'bytes_restored'}]
    assert len(rig.rows('restore_record')) == 1
    assert [json.loads(r['body']) for r in rig.rows('archive_generation')] == snapshot


def test_drive_remount_uses_disk_identity_not_letter(tmp_path):
    rig = Rig(tmp_path)
    content = b'remounted' * 12
    rig.commit('P1', 'v1', DR, content)
    moved = tmp_path / 'mnt' / 'volume-B'  # 盘符改变：同一 DISK-B 挂到新位置
    moved.parent.mkdir(parents=True)
    shutil.move(str(rig.paths['DISK-B']), str(moved))
    rig.paths['DISK-B'] = moved
    out = tmp_path / 'restored.bin'
    result = rig.restore('P1', out, ('DISK-A', 'DISK-B'))
    assert result[0]['status'] == '已恢复'
    assert out.read_bytes() == content
    v2 = b'remounted-v2' * 12
    rig.commit('P1', 'v2', DR, v2)  # 换路径后续写与提交不受影响
    out2 = tmp_path / 'restored2.bin'
    assert rig.restore('P1', out2, ('DISK-A', 'DISK-B'))[0]['status'] == '已恢复'
    assert out2.read_bytes() == v2


def test_restore_does_not_mutate_archive_evidence(tmp_path):
    rig = Rig(tmp_path)
    v1, v2 = b'keep-v1' * 4, b'keep-v2' * 4
    rig.commit('P1', 'v1', DR, v1)
    rig.commit('P1', 'v2', DR, v2)
    before = {t: rig.rows(t) for t in ('archive_task', 'archive_generation', 'archive_shard', 'archive_commit')}
    rig.restore('P1', tmp_path / 'out.bin', ('DISK-A', 'DISK-B'))
    after = {t: rig.rows(t) for t in ('archive_task', 'archive_generation', 'archive_shard', 'archive_commit')}
    assert before == after


def test_report_lists_recoverability_and_preservation_gap(tmp_path):
    rig = Rig(tmp_path)
    rig.commit('P1', 'v1', PRES, b'report-v1' * 5)
    v2 = b'report-v2' * 5
    rig.backup('P1', 'v2', PRES, v2, DISKS)
    rig.backup('P1', 'v2', PRES, v2, DISKS)
    rig.backup('P1', 'v2', PRES, v2, ('DISK-A', 'DISK-B'))  # v2 提交回执只有 A、B 验证
    out = tmp_path / 'report.json'
    data = json.loads(report.build(rig.db, out).read_text(encoding='utf-8'))
    recovery = {entry['generation']: entry for entry in data['archive_recovery']}
    assert recovery['P1:v1']['committed'] is True
    assert recovery['P1:v1']['preservation_complete'] is True
    assert recovery['P1:v2']['committed'] is True
    assert recovery['P1:v2']['recoverable_with_any_two_disks'] is True
    assert recovery['P1:v2']['preservation_complete'] is False
    assert recovery['P1:v2']['preservation_gap'] == ['DISK-C']
    assert recovery['P1:v2']['commit_receipts'] == {'DISK-A': '已校验', 'DISK-B': '已校验', 'DISK-C': '已写完'}
    assert len(data['entities']['archive_shard']) == 6
    assert len(data['entities']['archive_commit']) == 6
