"""Capture a local subtitle editor plugin inventory."""
from .ingest import capture
from .store import connect, save, decide


def import_inventory(path, file):
    source, payload = capture(path, 'desktop_plugin_inventory', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                save(db, 'plugin_inventory', key, source, payload['version'],
                     row, '已安装', 'inventory_captured')
                results.append(decide(db, '编辑器插件清单', key, '已安装', 'inventory_captured', source))
        return results
    finally:
        db.close()
