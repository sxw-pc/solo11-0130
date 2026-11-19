"""Review a basic access trace finding."""
from .ingest import capture
from .store import connect, save, decide


def review(path, file):
    source, payload = capture(path, 'desktop_incident_review', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                trace = db.execute('SELECT * FROM access_trace WHERE id=?',
                                   (str(row['trace']),)).fetchone()
                if not trace:
                    status, reason = '待核', 'trace_missing'
                elif trace['status'] == '待调查' and row.get('reviewer'):
                    status, reason = '已立案', 'reviewer_confirmed'
                else:
                    status, reason = '待核', 'review_incomplete'
                save(db, 'incident_case', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '访问异常复核', key, status, reason, source))
        return results
    finally:
        db.close()
