"""Capture offline subtitle editor access actions."""
from .ingest import capture
from .store import connect, save, decide


def import_actions(path, file):
    source, payload = capture(path, 'desktop_access_trace', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                actions = row.get('actions', [])
                logged_in = any(x.get('kind') == 'login' for x in actions)
                exported = any(x.get('kind') == 'export' for x in actions)
                if exported and not logged_in:
                    status, reason = '待调查', 'export_without_login'
                else:
                    status, reason = '已记录', 'basic_trace_ok'
                save(db, 'access_trace', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '桌面访问采录', key, status, reason, source))
        return results
    finally:
        db.close()
