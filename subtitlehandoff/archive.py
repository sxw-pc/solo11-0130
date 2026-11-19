"""Capture a simple offline project export archive copy."""
import base64
import hashlib
from pathlib import Path
from .ingest import capture
from .store import connect, save, decide


def backup(path, file):
    source, payload = capture(path, 'desktop_archive_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                raw = Path(row['file']).read_bytes()
                body = {'file': row['file'], 'sha256': hashlib.sha256(raw).hexdigest(),
                        'body': base64.b64encode(raw).decode('ascii')}
                save(db, 'archive_copy', key, source, payload['version'],
                     body, '已归档', 'bytes_captured')
                results.append(decide(db, '离线工程归档', key, '已归档', 'bytes_captured', source))
        return results
    finally:
        db.close()
