"""Capture a small offline desktop automation regression suite."""
from .ingest import capture
from .store import connect, save, decide


def plan(path, file):
    source, payload = capture(path, 'desktop_ui_cases', file)
    db = connect(path)
    try:
        with db:
            cases = payload.get('rows', [])
            for row in cases:
                save(db, 'ui_case', str(row['id']), source, payload['version'],
                     row, '已采录', 'case_captured')
            selected = [str(row['id']) for row in cases[:3]]
            key = str(payload['suite_id'])
            save(db, 'qa_suite', key, source, payload['version'],
                 {'cases': selected}, '已规划', 'first_three')
            return decide(db, '桌面回归用例', key, '已规划', 'first_three', source)
    finally:
        db.close()
