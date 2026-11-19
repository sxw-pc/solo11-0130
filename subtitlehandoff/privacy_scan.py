"""Capture synthetic subtitle text and simple private token findings."""
import re
from .ingest import capture
from .store import connect, save, decide


EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')


def scan(path, file):
    source, payload = capture(path, 'desktop_privacy_source', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                findings = [m.group() for m in EMAIL.finditer(row['text'])]
                status = '待遮盖' if findings else '无发现'
                save(db, 'privacy_source', key, source, payload['version'],
                     dict(row, findings=findings), status, 'email_regex')
                results.append(decide(db, '隐私扫描', key, status, 'email_regex', source))
        return results
    finally:
        db.close()
