import json
from subtitlehandoff import archive, archive_restore


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_offline_backup_and_restore(tmp_path):
    db = tmp_path / 'ledger.db'
    original = tmp_path / 'project.bin'
    original.write_bytes(b'synthetic project export')
    request = {'version': 'v1', 'rows': [{'id': 'A1', 'file': str(original)}]}
    assert archive.backup(db, write(tmp_path, 'backup.json', request))[0]['status'] == '已归档'
    out = tmp_path / 'restored.bin'
    restore = {'version': 'v1', 'rows': [{'id': 'R1', 'archive': 'A1', 'out': str(out)}]}
    assert archive_restore.restore(db, write(tmp_path, 'restore.json', restore))[0]['status'] == '已恢复'
    assert out.read_bytes() == original.read_bytes()
