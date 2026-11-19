import json
from subtitlehandoff import access_audit, incident_review


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_access_trace_to_case(tmp_path):
    db = tmp_path / 'ledger.db'
    trace = {'version': 'v1', 'rows': [{'id': 'T1', 'actions': [{'kind': 'export'}]}]}
    assert access_audit.import_actions(db, write(tmp_path, 'trace.json', trace))[0]['status'] == '待调查'
    review = {'version': 'v1', 'rows': [{'id': 'I1', 'trace': 'T1', 'reviewer': 'audit1'}]}
    assert incident_review.review(db, write(tmp_path, 'review.json', review))[0]['status'] == '已立案'
