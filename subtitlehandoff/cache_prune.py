"""Choose oldest preview entries until the reported quota is met."""
import json
from .ingest import capture
from .store import connect, save, decide


def prune(path, file):
    source, payload = capture(path, 'desktop_cache_prune', file)
    db = connect(path)
    try:
        with db:
            entries = [(x, json.loads(x['body'])) for x in db.execute("SELECT * FROM preview_cache WHERE status='已缓存'")]
            total = sum(float(body['size_mb']) for _, body in entries)
            removed = []
            for item, body in sorted(entries, key=lambda pair: pair[1]['last_used']):
                if total <= float(payload['quota_mb']):
                    break
                total -= float(body['size_mb'])
                removed.append(item['id'])
                save(db, 'preview_cache', item['id'], item['source_id'], item['version'],
                     body, '已淘汰', 'oldest_first')
            key = str(payload['request_id'])
            save(db, 'cache_prune', key, source, payload['version'],
                 {'removed': removed, 'remaining_mb': total}, '已规划', 'oldest_first')
            return decide(db, '缓存清理', key, '已规划', 'oldest_first', source)
    finally:
        db.close()
