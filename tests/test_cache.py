import json
from subtitlehandoff import cache_inventory, cache_prune


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_oldest_cache_prune_entry(tmp_path):
    db = tmp_path / 'ledger.db'
    cache = {'version': 'v1', 'rows': [
        {'id': 'C1', 'size_mb': 10, 'last_used': '2026-01-01T09:00:00'},
        {'id': 'C2', 'size_mb': 10, 'last_used': '2026-01-01T10:00:00'}]}
    assert cache_inventory.import_cache(db, write(tmp_path, 'cache.json', cache))[0]['status'] == '已缓存'
    request = {'version': 'v1', 'request_id': 'R1', 'quota_mb': 10}
    assert cache_prune.prune(db, write(tmp_path, 'prune.json', request))['status'] == '已规划'
