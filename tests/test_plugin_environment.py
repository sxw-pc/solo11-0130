import json
from subtitlehandoff import plugin_inventory, project_open


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_local_plugin_inventory_opens_project(tmp_path):
    db = tmp_path / 'ledger.db'
    inventory = {'version': 'v1', 'rows': [{'id': 'I1', 'name': 'srt', 'release': '1.0'}]}
    assert plugin_inventory.import_inventory(db, write(tmp_path, 'inventory.json', inventory))[0]['status'] == '已安装'
    project = {'version': 'v1', 'rows': [{'id': 'P1', 'required_plugins': ['srt']}]}
    assert project_open.check(db, write(tmp_path, 'project.json', project))[0]['status'] == '可打开'
