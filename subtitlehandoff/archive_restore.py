"""Restore a simple offline project export archive copy."""
import base64
import hashlib
import json
from pathlib import Path
from .ingest import capture
from .store import connect, save, decide


def restore(path, file):
    source, payload = capture(path, 'desktop_restore_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                item = db.execute('SELECT * FROM archive_copy WHERE id=?',
                                  (str(row['archive']),)).fetchone()
                if not item:
                    status, reason = '待核', 'archive_missing'
                else:
                    body = json.loads(item['body'])
                    raw = base64.b64decode(body['body'])
                    if hashlib.sha256(raw).hexdigest() != body['sha256']:
                        status, reason = '待核', 'hash_mismatch'
                    else:
                        target = Path(row['out'])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(raw)
                        status, reason = '已恢复', 'bytes_restored'
                save(db, 'restore_record', key, source, payload['version'],
                     row, status, reason)
                results.append(decide(db, '离线工程恢复', key, status, reason, source))
        return results
    finally:
        db.close()
