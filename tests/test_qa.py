import json
from subtitlehandoff import qa_plan, qa_execute


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_offline_suite_and_result(tmp_path):
    db = tmp_path / 'ledger.db'
    plan = {'version': 'v1', 'suite_id': 'S1', 'rows': [
        {'id': 'C1', 'window': 'editor', 'action': 'open'},
        {'id': 'C2', 'window': 'editor', 'action': 'export'}]}
    assert qa_plan.plan(db, write(tmp_path, 'cases.json', plan))['status'] == '已规划'
    result = {'version': 'v1', 'rows': [{'id': 'R1', 'suite': 'S1',
              'results': [{'case': 'C1', 'passed': True}, {'case': 'C2', 'passed': True}]}]}
    assert qa_execute.summarize(db, write(tmp_path, 'result.json', result))[0]['status'] == '通过'
