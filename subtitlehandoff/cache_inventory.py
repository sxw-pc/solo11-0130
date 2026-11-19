"""Capture a local editor preview cache inventory."""
from .ingest import capture
from .store import connect, save, decide


def import_cache(path, file):
    source, payload = capture(path, 'desktop_preview_cache', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                save(db, 'preview_cache', key, source, payload['version'],
                     row, '已缓存', 'inventory_captured')
                results.append(decide(db, '预览缓存', key, '已缓存', 'inventory_captured', source))
        return results
    finally:
        db.close()
