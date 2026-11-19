import json
from subtitlehandoff import privacy_scan, privacy_fixture


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_synthetic_privacy_fixture(tmp_path):
    db = tmp_path / 'ledger.db'
    source = {'version': 'v1', 'rows': [{'id': 'C1', 'text': 'Call me at a@example.com'}]}
    assert privacy_scan.scan(db, write(tmp_path, 'source.json', source))[0]['status'] == '待遮盖'
    fixture = {'version': 'v1', 'rows': [{'id': 'F1', 'source': 'C1'}]}
    assert privacy_fixture.build(db, write(tmp_path, 'fixture.json', fixture))[0]['status'] == '已生成'
