"""Make a basic redacted fixture from a captured source row."""
import json
from .ingest import capture
from .store import connect, save, decide


def build(path, file):
    source, payload = capture(path, 'desktop_privacy_fixture', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                original = db.execute('SELECT * FROM privacy_source WHERE id=?',
                                      (str(row['source']),)).fetchone()
                if not original:
                    status, reason, masked = '待核', 'source_missing', ''
                else:
                    item = json.loads(original['body'])
                    masked = item['text']
                    for finding in item['findings']:
                        masked = masked.replace(finding, '[REDACTED]')
                    status, reason = '已生成', 'email_masked'
                save(db, 'privacy_fixture', key, source, payload['version'],
                     dict(row, masked_text=masked), status, reason)
                results.append(decide(db, '脱敏样例', key, status, reason, source))
        return results
    finally:
        db.close()
