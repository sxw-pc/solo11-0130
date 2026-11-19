"""Reconcile simulated desktop run completions with a planned export queue."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide


def reconcile(path, file):
    source, payload = capture(path, 'desktop_export_dispatch', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                job = db.execute('SELECT * FROM export_job WHERE id=?', (str(row['job']),)).fetchone()
                if not job:
                    status, reason = '待核', 'job_missing'
                elif row.get('state') != 'completed':
                    status, reason = '待续', 'export_not_completed'
                elif datetime.fromisoformat(row['at']) > datetime.fromisoformat(json.loads(job['body'])['deadline']):
                    status, reason = '逾期', 'deadline_missed'
                else:
                    status, reason = '已完成', 'export_on_time'
                save(db, 'dispatch_result', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '桌面导出执行', key, status, reason, source))
        return results
    finally:
        db.close()
