"""Contract-aware offline export planning and execution reconciliation."""
import itertools
import json

from subtitlehandoff import export_dispatch, export_queue, report
from subtitlehandoff.store import connect


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def at(minute):
    hour, minute = divmod(minute, 60)
    return f'2026-01-01T{10 + hour:02d}:{minute:02d}:00'


def queue_payload(queue_id='Q1', contract='live', rows=None, windows=None, start=at(0)):
    return {
        'version': 'v1', 'queue_id': queue_id, 'contract': contract, 'start': start,
        'load_minutes': 0,
        'license': {'id': 'ENC', 'windows': windows or [
            {'start': at(0), 'end': at(40)}, {'start': at(60), 'end': at(120)}]},
        'workstations': [{'id': 'A'}, {'id': 'B'}],
        'rows': rows if rows is not None else [],
    }


def contract_rows():
    return [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 20, 'deadline': at(30),
         'hard': True, 'weight': 1},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 20, 'deadline': at(30),
         'hard': True, 'weight': 1},
        {'id': 'J3', 'project': 'P3', 'encode_minutes': 20, 'target': at(30),
         'hard': False, 'weight': 5},
    ]


def plan_body(db, queue_id='Q1'):
    row = db.execute('SELECT body FROM export_plan WHERE id=?', (queue_id,)).fetchone()
    return json.loads(row['body'])


def job_status(db, queue_id='Q1'):
    out = {}
    for row in db.execute('SELECT * FROM plan_job WHERE id LIKE ? ORDER BY id',
                          (f'plan:{queue_id}:%',)):
        out[json.loads(row['body'])['id']] = (row['status'], row['reason'])
    return out


def leases(db, queue_id='Q1'):
    return [json.loads(row['body']) for row in
            db.execute('SELECT * FROM license_lease WHERE id LIKE ? ORDER BY id',
                       (f'lease:{queue_id}:%',))]


# ---------------------------------------------------------------------------
# the two contracts, same inputs
# ---------------------------------------------------------------------------
def test_live_contract_protects_hard_deadlines_and_leaves_internal_sample_out(tmp_path):
    db = tmp_path / 'db'
    result = export_queue.plan(db, write(tmp_path, 'q.json',
                                         queue_payload(contract='live', rows=contract_rows())))
    status = {x['subject']: x['status'] for x in result}
    assert status['J1'] == '按时' and status['J2'] == '逾期'
    assert status['J3'] == '逾期'
    plan = plan_body(connect(db))
    assert plan['order'][:2] == ['J1', 'J2']
    assert plan['objective']['hard_missed'] == 1


def test_live_contract_can_drop_low_weight_sample_instead_of_missing_hard_deadline(tmp_path):
    db = tmp_path / 'db'
    rows = [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 15, 'deadline': at(30),
         'hard': True, 'weight': 1},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 15, 'deadline': at(30),
         'hard': True, 'weight': 1},
        # the only window ends at 10:40; after the two hard jobs nothing fits
        {'id': 'J3', 'project': 'P3', 'encode_minutes': 20, 'target': at(20),
         'hard': False, 'weight': 1},
    ]
    windows = [{'start': at(0), 'end': at(40)}]
    result = export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(
        contract='live', rows=rows, windows=windows)))
    conn = connect(db)
    status = job_status(conn)
    assert status['J1'][0] == '按时' and status['J2'][0] == '按时'
    assert plan_body(conn)['objective']['hard_missed'] == 0


def test_internal_contract_prioritises_weight_and_honestly_reports_lateness(tmp_path):
    db = tmp_path / 'db'
    rows = [dict(row, hard=False) for row in contract_rows()]
    for row in rows:
        row.pop('deadline', None)
        row['target'] = at(30)
    result = export_queue.plan(db, write(tmp_path, 'q.json',
                                         queue_payload(contract='internal', rows=rows)))
    conn = connect(db)
    assert plan_body(conn)['order'][0] == 'J3'
    status = {x['subject']: x['status'] for x in result}
    assert status['J3'] == '按时'
    assert status['J1'] == '逾期' and status['J2'] == '逾期'
    assert any(item['job'] in ('J1', 'J2') for item in
               (json.loads(r['body']) for r in conn.execute('SELECT * FROM bottleneck')))


def test_contract_is_frozen_before_the_queue_and_cannot_be_swapped(tmp_path):
    db = tmp_path / 'db'
    export_queue.plan(db, write(tmp_path, 'a.json',
                                queue_payload(contract='live', rows=contract_rows())))
    try:
        export_queue.plan(db, write(tmp_path, 'b.json',
                                    queue_payload(contract='internal', rows=contract_rows())))
    except ValueError as error:
        assert str(error) == 'contract_locked'
    else:
        raise AssertionError('contract swap must be rejected')


# ---------------------------------------------------------------------------
# dual stations, one license, load, phases
# ---------------------------------------------------------------------------
def test_two_stations_prepare_in_parallel_but_never_encode_in_parallel(tmp_path):
    db = tmp_path / 'db'
    rows = [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 15, 'prepare_minutes': 10,
         'verify_minutes': 2, 'deadline': '2026-01-01T10:45:00', 'hard': True},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 15, 'prepare_minutes': 10,
         'verify_minutes': 2, 'deadline': '2026-01-01T10:50:00', 'hard': True},
    ]
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    conn = connect(db)
    plan = plan_body(conn)
    jobs = {j['id']: j for j in plan['jobs']} if 'jobs' in plan else None
    phases = [json.loads(r['body']) for r in
              conn.execute("SELECT * FROM plan_phase ORDER BY id")]
    prepares = [p for p in phases if p['phase'] == 'prepare']
    encodes = [p for p in phases if p['phase'] == 'encode']
    assert len({p['job'] for p in prepares}) == 2
    assert len({p['station'] for p in prepares}) == 2          # prepared in parallel
    intervals = sorted((p['start'], p['end']) for p in encodes)
    assert all(a[1] <= b[0] for a, b in itertools.combinations(intervals, 2))
    lease_bodies = leases(conn)
    assert len(lease_bodies) == 2
    lease_intervals = sorted((l['start'], l['end']) for l in lease_bodies)
    assert all(a[1] <= b[0] for a, b in itertools.combinations(lease_intervals, 2))
    for job, phases_for_job in _phases_by_job(phases).items():
        assert phases_for_job['encode'][0] >= phases_for_job['prepare'][1]
        assert phases_for_job['verify'][0] >= phases_for_job['encode'][1]


def _phases_by_job(phases):
    out = {}
    for phase in phases:
        out.setdefault(phase['job'], {})[phase['phase']] = (phase['start'], phase['end'])
    return out


def test_load_is_charged_only_when_project_changes_and_license_start_delays_queue(tmp_path):
    db = tmp_path / 'db'
    rows = [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'load_minutes': 5,
         'deadline': at(40), 'hard': True},
        {'id': 'J2', 'project': 'P1', 'encode_minutes': 10, 'load_minutes': 5,
         'deadline': at(40), 'hard': True},
        {'id': 'J3', 'project': 'P2', 'encode_minutes': 10, 'load_minutes': 5,
         'deadline': at(40), 'hard': True},
    ]
    windows = [{'start': at(10), 'end': at(50)}]
    export_queue.plan(db, write(tmp_path, 'q.json',
                                queue_payload(rows=rows, windows=windows, start=at(0))))
    conn = connect(db)
    plan = plan_body(conn)
    # the license cannot be taken before 10:10; the J1 load may run while the
    # license is still unavailable, so the first lease starts at 10:10
    assert plan['license']['leases'][0]['start'] == at(10)[:16]
    loads = {json.loads(r['body'])['job'] for r in
             conn.execute("SELECT * FROM plan_phase WHERE id LIKE '%:load'")}
    # J1 and J2 share P1; only the project switch to J3 pays a second load
    assert loads == {'J1', 'J3'}


# ---------------------------------------------------------------------------
# exact optimum and stable tie-break
# ---------------------------------------------------------------------------
def test_exact_search_finds_optimal_order_not_input_order(tmp_path):
    db = tmp_path / 'db'
    # input order puts the low-weight long job first; optimal is the high weight
    rows = [
        {'id': 'Z9', 'project': 'PZ', 'encode_minutes': 20, 'target': at(30),
         'hard': False, 'weight': 1},
        {'id': 'A1', 'project': 'PA', 'encode_minutes': 20, 'target': at(30),
         'hard': False, 'weight': 100},
    ]
    export_queue.plan(db, write(tmp_path, 'q.json',
                                queue_payload(contract='internal', rows=rows)))
    plan = plan_body(connect(db))
    assert plan['order'] == ['A1', 'Z9']


def test_equal_objectives_fall_back_to_lexicographic_job_order(tmp_path):
    db = tmp_path / 'a'
    rows = [
        {'id': 'B', 'project': 'PB', 'encode_minutes': 10, 'target': at(30),
         'hard': False, 'weight': 1},
        {'id': 'A', 'project': 'PA', 'encode_minutes': 10, 'target': at(30),
         'hard': False, 'weight': 1},
    ]
    export_queue.plan(db, write(tmp_path, 'a.json',
                                queue_payload(contract='internal', rows=rows)))
    first = plan_body(connect(db))['order']
    assert first == ['A', 'B']
    # reversing the input lines must not change the tie-broken result
    export_queue.plan(tmp_path / 'b', write(tmp_path, 'b.json', queue_payload(
        queue_id='Q2', contract='internal', rows=list(reversed(rows)))))
    second = plan_body(connect(tmp_path / 'b'), 'Q2')['order']
    assert second == first


# ---------------------------------------------------------------------------
# execution reconciliation
# ---------------------------------------------------------------------------
def _plan_for_execution(db, tmp_path, rows=None):
    rows = rows or [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'prepare_minutes': 5,
         'verify_minutes': 2, 'deadline': at(30), 'hard': True},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 10, 'verify_minutes': 2,
         'deadline': at(30), 'hard': True},
    ]
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    return rows


def test_verify_end_not_completed_timestamp_decides_lateness(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'e1', 'job': 'J1', 'phase': 'prepare', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(5)},
        {'id': 'e2', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(5), 'end': at(15)},
        {'id': 'e3', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(15), 'end': at(17)},
        {'id': 'e4', 'job': 'J2', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(15), 'end': at(25)},
        {'id': 'e5', 'job': 'J2', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(29), 'end': at(31)},
    ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '已完成'
    # job J2 encoded before 10:30 but verification ended after it
    assert result['J2']['status'] == '逾期'
    assert result['J2']['reason'] == 'verify_after_deadline'


def test_one_license_cannot_be_held_by_two_jobs(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'a1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(20)},
        {'id': 'a2', 'job': 'J2', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(10), 'end': at(30)},
        {'id': 'a3', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(20), 'end': at(21)},
    ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '已完成'
    assert result['J2']['status'] == '冲突'
    assert result['J2']['reason'] == 'license_conflict_with:J1'
    conn = connect(db)
    occupancy = json.loads(conn.execute(
        "SELECT body FROM license_lease WHERE id='actual:Q1'").fetchone()['body'])
    assert occupancy['conflicts']


def test_interrupted_encode_resumes_only_from_a_recorded_checkpoint(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'c1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'interrupted', 'start': at(0), 'end': at(4),
         'checkpoint_minutes': 4},
        # B claims completion with a segment shorter than a full encode and
        # without pointing at the recorded checkpoint
        {'id': 'c2', 'job': 'J1', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(5), 'end': at(12)},
        {'id': 'c3', 'job': 'J1', 'phase': 'verify', 'station': 'B',
         'state': 'completed', 'start': at(12), 'end': at(16)},
    ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '冲突'
    assert result['J1']['reason'] == 'resume_without_matching_checkpoint'


def test_resume_with_matching_checkpoint_is_accepted(tmp_path):
    db = tmp_path / 'db'
    rows = [{'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'verify_minutes': 2,
             'deadline': at(40), 'hard': True, 'checkpoints': [4, 8]}]
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'c1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'interrupted', 'start': at(0), 'end': at(4),
         'checkpoint_minutes': 4},
        {'id': 'c2', 'job': 'J1', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(6), 'end': at(12), 'resume_from': 4},
        {'id': 'c3', 'job': 'J1', 'phase': 'verify', 'station': 'B',
         'state': 'completed', 'start': at(12), 'end': at(14)},
    ]}
    result = export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))
    assert result[0]['status'] == '已完成'


def test_half_finished_without_checkpoint_cannot_be_completed_by_second_station(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'h1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'interrupted', 'start': at(0), 'end': at(4)},
        {'id': 'h2', 'job': 'J1', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(5), 'end': at(12)},
    ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] in ('待重做', '冲突')
    assert 'checkpoint' in result['J1']['reason']


def test_station_failure_reassignment_cannot_finish_on_a_busy_license(tmp_path):
    db = tmp_path / 'db'
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=[
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'deadline': at(30), 'hard': True},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 10, 'deadline': at(30), 'hard': True},
    ])))
    log = {'version': 'v1', 'queue_id': 'Q1',
           'failures': [{'station': 'A', 'at': at(5)}],
           'rows': [
               # J1 failed on A, moved to B but B's license is taken by J2
               {'id': 'f1', 'job': 'J1', 'phase': 'encode', 'station': 'B',
                'state': 'completed', 'start': at(10), 'end': at(20)},
               {'id': 'f2', 'job': 'J2', 'phase': 'encode', 'station': 'B',
                'state': 'completed', 'start': at(12), 'end': at(22)},
           ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '冲突' or result['J2']['status'] == '冲突'
    assert not (result['J1']['status'] == '已完成' and result['J2']['status'] == '已完成')
    conn = connect(db)
    reassign = [json.loads(r['body']) for r in
                conn.execute('SELECT * FROM reassignment')]
    assert reassign and reassign[0]['from_station'] == 'A'


def test_reimporting_same_execution_log_does_not_double_book_the_license(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'r1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(10)},
        {'id': 'r2', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(10), 'end': at(12)},
    ]}
    file = write(tmp_path, 'log.json', log)
    export_dispatch.reconcile(db, file)
    conn = connect(db)
    before = json.loads(conn.execute(
        "SELECT body FROM license_lease WHERE id='actual:Q1'").fetchone()['body'])
    export_dispatch.reconcile(db, file)
    after = json.loads(conn.execute(
        "SELECT body FROM license_lease WHERE id='actual:Q1'").fetchone()['body'])
    assert before['occupancy'] == after['occupancy']
    assert len(after['occupancy']) == 1
    assert after['duplicate_events'] == 2


# ---------------------------------------------------------------------------
# replanning keeps done work and reports the real bottleneck
# ---------------------------------------------------------------------------
def test_new_hard_deadline_forces_explicit_replan_without_stealing_frozen_slots(tmp_path):
    db = tmp_path / 'db'
    rows = [
        {'id': 'J1', 'project': 'P1', 'encode_minutes': 20, 'deadline': at(40),
         'hard': True},
        {'id': 'J2', 'project': 'P2', 'encode_minutes': 20, 'target': at(40),
         'hard': False, 'weight': 1},
    ]
    export_queue.plan(db, write(tmp_path, 'q1.json',
                                queue_payload(rows=rows, start=at(0))))
    # J1 finishes and verifies; then a new hard deadline job arrives
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'x1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(20)},
        {'id': 'x2', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(20), 'end': at(22)},
    ]}
    export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))
    rows.append({'id': 'J9', 'project': 'P9', 'encode_minutes': 15,
                 'deadline': at(35), 'hard': True})
    export_queue.plan(db, write(tmp_path, 'q2.json',
                                queue_payload(rows=rows, start=at(0))))
    conn = connect(db)
    plan = plan_body(conn)
    assert plan['revision'] == 2
    assert 'J9' in plan['order']
    assert 'J1' in plan['carried']
    revision = json.loads(conn.execute(
        "SELECT body FROM plan_revision WHERE id=?",
        ('revision:Q1:2',)).fetchone()['body'])
    assert revision['carried'] == ['J1']
    # frozen J1 verification is preserved, not rescheduled
    phases = {json.loads(r['body'])['phase']: json.loads(r['body']) for r in
              conn.execute("SELECT * FROM plan_phase WHERE id LIKE 'plan:Q1:J1:%'")}
    assert phases['encode']['start'] == at(0)[:16]


def test_bottleneck_report_explains_why_a_job_was_pushed(tmp_path):
    db = tmp_path / 'db'
    rows = [dict(row, hard=False) for row in contract_rows()]
    for row in rows:
        row.pop('deadline', None)
        row['target'] = at(30)
    export_queue.plan(db, write(tmp_path, 'q.json',
                                queue_payload(contract='internal', rows=rows)))
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['plans'][0]['order'][0] == 'J3'
    delayed = [b for b in data['bottlenecks'] if b['job'] in ('J1', 'J2')]
    assert delayed
    assert all('reason' in b and 'evidence_at' in b for b in delayed)
    explained = report.explain(db, job_id='J1')
    assert explained['bottlenecks']


def test_report_and_store_round_trip_the_frozen_plan(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert len(data['sources']) == 1 and all(x['verified'] for x in data['sources'])
    plan = data['plans'][0]
    assert plan['contract'] == 'live' and plan['order']
    assert all(job['phases'] for job in plan['jobs'])
    assert all(job['license_leases'] for job in plan['jobs'])
    # frozen plan survives a second identical planning input
    export_queue.plan(db, write(tmp_path, 'q.json',
                                queue_payload(rows=[
                                    {'id': 'J1', 'project': 'P1', 'encode_minutes': 10,
                                     'prepare_minutes': 5, 'verify_minutes': 2,
                                     'deadline': at(30), 'hard': True},
                                    {'id': 'J2', 'project': 'P2', 'encode_minutes': 10,
                                     'verify_minutes': 2, 'deadline': at(30),
                                     'hard': True}])))
    conn = connect(db)
    assert plan_body(conn)['revision'] == 2


def test_legacy_fifo_payload_still_works(tmp_path):
    db = tmp_path / 'db'
    queue = {'version': 'v1', 'queue_id': 'OLD', 'start': '2026-01-01T10:00:00',
             'rows': [{'id': 'J1', 'project': 'P1', 'minutes': 10,
                       'deadline': '2026-01-01T10:15:00'},
                      {'id': 'J2', 'project': 'P2', 'minutes': 10,
                       'deadline': '2026-01-01T10:15:00'}]}
    result = export_queue.plan(db, write(tmp_path, 'q.json', queue))
    assert [x['status'] for x in result] == ['可按时完成', '预计逾期']
    events = {'version': 'v1', 'rows': [{'id': 'E1', 'job': 'J1',
                                         'state': 'completed',
                                         'at': '2026-01-01T10:10:00'}]}
    assert export_dispatch.reconcile(db, write(tmp_path, 'e.json', events))[0]['status'] == '已完成'


def test_sample_inputs_are_executable_offline(tmp_path):
    from pathlib import Path
    samples = Path(__file__).resolve().parent.parent / 'samples'
    db = tmp_path / 'db'
    live = export_queue.plan(db, samples / 'export_queue_live.json')
    internal = export_queue.plan(db, samples / 'export_queue_internal.json')
    dual = export_queue.plan(db, samples / 'export_dual_station.json')
    live_status = {x['subject']: x['status'] for x in live}
    assert live_status['J1'] == '按时' and live_status['J2'] == '逾期'
    assert {x['subject']: x['status'] for x in internal}['J3'] == '按时'
    assert [x['status'] for x in dual] == ['按时', '按时']
    conn = connect(db)
    plans = {body['queue_id']: body for _, body in
             ((row, json.loads(row['body'])) for row in
              conn.execute('SELECT * FROM export_plan'))}
    assert plans['LIVE-2026-01-15']['order'][:2] == ['J1', 'J2']
    assert plans['INTERNAL-2026-01-15']['order'][0] == 'J3'
    assert plans['DUAL-2026-01-15']['objective']['hard_missed'] == 0


def test_log_row_ids_may_repeat_without_losing_events(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'dup', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(10)},
        {'id': 'dup', 'job': 'J2', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(10), 'end': at(20)},
        {'id': 'dup', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(10), 'end': at(12)},
        {'id': 'dup', 'job': 'J2', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(20), 'end': at(22)},
    ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '已完成'
    assert result['J2']['status'] == '已完成'
    conn = connect(db)
    assert len(list(conn.execute('SELECT id FROM execution_event'))) == 4


def test_more_than_eight_jobs_is_rejected_rather_than_greedy(tmp_path):
    db = tmp_path / 'db'
    rows = [{'id': f'J{i}', 'project': f'P{i}', 'encode_minutes': 5,
             'deadline': at(60), 'hard': True} for i in range(9)]
    try:
        export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    except ValueError as error:
        assert str(error) == 'exact_search_requires_at_most_8_jobs'
    else:
        raise AssertionError('nine jobs must not silently fall back to a greedy order')


def test_license_temporarily_unavailable_is_reported_as_violation(tmp_path):
    db = tmp_path / 'db'
    _plan_for_execution(db, tmp_path)
    log = {'version': 'v1', 'queue_id': 'Q1',
           'license_unavailable': [{'start': at(5), 'end': at(30)}],
           'rows': [
               {'id': 'u1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
                'state': 'completed', 'start': at(0), 'end': at(10)},
           ]}
    result = {x['subject']: x for x in
              export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))}
    assert result['J1']['status'] == '冲突'
    assert result['J1']['reason'] == 'license_unavailable_violation'


def test_full_reencode_from_zero_is_a_restart_not_a_checkpoint_fake(tmp_path):
    db = tmp_path / 'db'
    rows = [{'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'verify_minutes': 2,
             'deadline': at(50), 'hard': True}]
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 's1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'interrupted', 'start': at(0), 'end': at(3)},
        # no checkpoint, but B redoes the entire ten minutes from zero
        {'id': 's2', 'job': 'J1', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(5), 'end': at(15)},
        {'id': 's3', 'job': 'J1', 'phase': 'verify', 'station': 'B',
         'state': 'completed', 'start': at(15), 'end': at(17)},
    ]}
    result = export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))
    assert result[0]['status'] == '已完成'


def test_replan_never_reuses_a_license_interval_already_executed(tmp_path):
    db = tmp_path / 'db'
    rows = [{'id': 'J1', 'project': 'P1', 'encode_minutes': 20, 'deadline': at(40),
             'hard': True},
            {'id': 'J2', 'project': 'P2', 'encode_minutes': 20, 'target': at(40),
             'hard': False, 'weight': 1}]
    export_queue.plan(db, write(tmp_path, 'q1.json',
                                queue_payload(rows=rows, start=at(0))))
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'y1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'completed', 'start': at(0), 'end': at(20)},
        {'id': 'y2', 'job': 'J1', 'phase': 'verify', 'station': 'A',
         'state': 'completed', 'start': at(20), 'end': at(22)},
    ]}
    export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))
    rows.append({'id': 'J9', 'project': 'P9', 'encode_minutes': 15,
                 'deadline': at(35), 'hard': True})
    export_queue.plan(db, write(tmp_path, 'q2.json',
                                queue_payload(rows=rows, start=at(0))))
    conn = connect(db)
    leases = [json.loads(r['body']) for r in
              conn.execute("SELECT * FROM license_lease WHERE id LIKE 'lease:Q1:%'")]
    for lease in leases:
        for other in leases:
            if lease is other:
                continue
            assert lease['end'] <= other['start'] or other['end'] <= lease['start']
    executed_end = at(20)[:16]
    assert all(lease['start'] >= executed_end for lease in leases)


def test_interruption_moves_to_other_station_and_is_recorded_as_failure_reassignment(tmp_path):
    db = tmp_path / 'db'
    rows = [{'id': 'J1', 'project': 'P1', 'encode_minutes': 10, 'deadline': at(40),
             'hard': True, 'checkpoints': [4]}]
    export_queue.plan(db, write(tmp_path, 'q.json', queue_payload(rows=rows)))
    log = {'version': 'v1', 'queue_id': 'Q1', 'rows': [
        {'id': 'm1', 'job': 'J1', 'phase': 'encode', 'station': 'A',
         'state': 'interrupted', 'start': at(0), 'end': at(4),
         'checkpoint_minutes': 4},
        {'id': 'm2', 'job': 'J1', 'phase': 'encode', 'station': 'B',
         'state': 'completed', 'start': at(5), 'end': at(11), 'resume_from': 4},
        {'id': 'm3', 'job': 'J1', 'phase': 'verify', 'station': 'B',
         'state': 'completed', 'start': at(11), 'end': at(13)},
    ]}
    export_dispatch.reconcile(db, write(tmp_path, 'log.json', log))
    conn = connect(db)
    record = json.loads(conn.execute('SELECT body FROM reassignment').fetchone()['body'])
    assert record['from_station'] == 'A' and record['to_station'] == 'B'
    assert record['reason'] == 'station_failed_reassignment'
