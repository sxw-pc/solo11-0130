"""Summarize simulated execution results for a captured QA suite."""
import json
from .ingest import capture
from .store import connect, save, decide


def summarize(path, file):
    source, payload = capture(path, 'desktop_qa_results', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                suite = db.execute('SELECT * FROM qa_suite WHERE id=?',
                                   (str(row['suite']),)).fetchone()
                planned = set(json.loads(suite['body'])['cases']) if suite else set()
                passed = {str(x['case']) for x in row.get('results', []) if x.get('passed')}
                if not planned:
                    status, reason = '待核', 'suite_missing'
                elif planned.issubset(passed):
                    status, reason = '通过', 'all_selected_cases_passed'
                else:
                    status, reason = '退回', 'selected_case_missing_or_failed'
                save(db, 'qa_execution', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '桌面回归执行', key, status, reason, source))
        return results
    finally:
        db.close()
