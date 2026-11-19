import json
from subtitlehandoff import export_queue, export_dispatch


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_queue_and_dispatch_are_executable(tmp_path):
    db = tmp_path / 'ledger.db'
    queue = {'version': 'v1', 'queue_id': 'Q1', 'start': '2026-01-01T10:00:00',
             'rows': [{'id': 'J1', 'project': 'P1', 'minutes': 10,
                       'deadline': '2026-01-01T10:15:00'},
                      {'id': 'J2', 'project': 'P2', 'minutes': 10,
                       'deadline': '2026-01-01T10:15:00'}]}
    result = export_queue.plan(db, write(tmp_path, 'queue.json', queue))
    assert [x['status'] for x in result] == ['可按时完成', '预计逾期']
    events = {'version': 'v1', 'rows': [{'id': 'E1', 'job': 'J1', 'state': 'completed',
                                      'at': '2026-01-01T10:10:00'}]}
    assert export_dispatch.reconcile(db, write(tmp_path, 'events.json', events))[0]['status'] == '已完成'
