"""Plan a simple offline desktop subtitle export queue."""
from datetime import datetime, timedelta
from .ingest import capture
from .store import connect, save, decide


def plan(path, file):
    source, payload = capture(path, 'desktop_export_queue', file)
    db = connect(path)
    results = []
    try:
        with db:
            queue_id = str(payload['queue_id'])
            cursor = datetime.fromisoformat(payload['start'])
            planned = []
            for row in payload.get('rows', []):
                job_id = str(row['id'])
                begin = cursor
                cursor = begin + timedelta(minutes=float(row['minutes']))
                status = '可按时完成' if cursor <= datetime.fromisoformat(row['deadline']) else '预计逾期'
                job = dict(row, queue_id=queue_id, planned_start=begin.isoformat(), planned_end=cursor.isoformat())
                save(db, 'export_job', job_id, source, payload['version'], job, status, 'fifo_single_worker')
                planned.append(job)
                results.append(decide(db, '桌面导出排程', job_id, status, 'fifo_single_worker', source))
            save(db, 'export_queue', queue_id, source, payload['version'],
                 {'start': payload['start'], 'jobs': planned}, '已规划', 'fifo_single_worker')
        return results
    finally:
        db.close()
