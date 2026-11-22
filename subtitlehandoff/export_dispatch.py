"""Reconcile simulated desktop execution against a frozen export plan.

The legacy row-by-row mode is kept for queues planned by the old FIFO path.
For workplans this module reads the phase-level execution log and checks the
actual start, checkpoint, license occupancy and verification end - a bare
``completed`` timestamp is not evidence that a job met its deadline.

Rules enforced here:

* the single license may not be held by two jobs at once; overlapping encodes
  cannot both be reported complete;
* a resume on another station must start from a checkpoint recorded by the
  interrupted segment; a half-finished encode without a checkpoint can never be
  accepted as complete;
* verification must start after the actual encode and its end, not encode end,
  decides lateness;
* importing the same execution log again adds no license occupancy.
"""
import hashlib
import json
from datetime import datetime

from .ingest import capture
from .store import connect, save, decide, upsert, body

EPS = 1e-7


def _minutes(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value)).timestamp() / 60.0


def _iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value * 60).isoformat(timespec='minutes')


def _signature(queue_id, row):
    material = json.dumps({'queue': queue_id, 'job': str(row.get('job')),
                           'phase': row.get('phase', 'encode'),
                           'station': row.get('station'),
                           'state': row.get('state'),
                           'start': row.get('start'), 'end': row.get('end') or row.get('at'),
                           'checkpoint': row.get('checkpoint_minutes')},
                          sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _overlap(a, b):
    return a[0] < b[1] - EPS and b[0] < a[1] - EPS


# ---------------------------------------------------------------------------
# execution log ingestion (idempotent)
# ---------------------------------------------------------------------------
def _ingest_events(db, source, payload, queue_id):
    existing = {json.loads(row['body'])['signature']: row['id']
                for row in db.execute('SELECT * FROM execution_event')}
    stored, duplicates = [], 0
    for index, row in enumerate(payload.get('rows', []), 1):
        signature = _signature(queue_id, row)
        if signature in existing:
            duplicates += 1
            continue
        item = {'queue_id': queue_id, 'signature': signature,
                'label': str(row.get('id', f'row{index}')),
                'job': str(row['job']), 'phase': row.get('phase', 'encode'),
                'station': row.get('station'),
                'state': row.get('state'),
                'start': row.get('start'), 'end': row.get('end', row.get('at')),
                'checkpoint_minutes': row.get('checkpoint_minutes'),
                'resume_from': row.get('resume_from')}
        # the event identity is its content, not the log's row id: two log rows
        # may reuse an id without one silently overwriting the other
        save(db, 'execution_event', f'event:{queue_id}:{signature[:20]}', source,
             payload['version'], item, '已采录', 'execution_event_captured')
        existing[signature] = item['label']
        stored.append(item)
    return stored, duplicates


def _load_events(db, queue_id):
    events = []
    for row in db.execute('SELECT * FROM execution_event WHERE id LIKE ? ORDER BY id',
                          (f'event:{queue_id}:%',)):
        events.append(json.loads(row['body']))
    return events


# ---------------------------------------------------------------------------
# actual license occupancy
# ---------------------------------------------------------------------------
def _encode_intervals(events):
    """Map job -> ordered list of encode segments actually recorded."""
    segments = {}
    for item in events:
        if item['phase'] != 'encode':
            continue
        start = _minutes(item['start'])
        end = _minutes(item['end'])
        if start is None:
            continue
        if end is None:
            end = start
        segments.setdefault(item['job'], []).append({
            'station': item['station'], 'state': item['state'],
            'start': start, 'end': end,
            'checkpoint': float(item['checkpoint_minutes'] or 0),
            'resume_from': item['resume_from'],
        })
    for job in segments:
        segments[job].sort(key=lambda x: (x['start'], x['end']))
    return segments


def _license_conflicts(segments):
    """Find overlapping encode intervals belonging to different jobs.

    The segment that began later gives way.  When two segments start together
    the lexicographically larger job id is the loser, so the outcome does not
    depend on the order of the log rows.
    """
    flat = []
    for job, items in segments.items():
        for item in items:
            if item['end'] > item['start'] + EPS:
                flat.append((item['start'], item['end'], job, item))
    conflicts = []
    ordered = sorted(flat, key=lambda x: (x[0], x[1], x[2]))
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            left, right = ordered[i], ordered[j]
            if left[2] == right[2]:
                continue
            if _overlap((left[0], left[1]), (right[0], right[1])):
                if abs(right[0] - left[0]) < EPS and right[2] < left[2]:
                    loser, winner = left, right
                else:
                    loser, winner = right, left
                conflicts.append({'jobs': sorted({left[2], right[2]}),
                                  'loser': loser[2], 'winner': winner[2],
                                  'start': _iso(max(left[0], right[0])),
                                  'end': _iso(min(left[1], right[1])),
                                  'license': 'shared'})
    return conflicts


def _unavailable_violations(segments, unavailable):
    violations = []
    for job, items in segments.items():
        for item in items:
            for start, end in unavailable:
                if item['end'] > item['start'] + EPS and _overlap((item['start'], item['end']),
                                                                  (start, end)):
                    violations.append({'job': job, 'station': item['station'],
                                       'start': _iso(max(item['start'], start)),
                                       'end': _iso(min(item['end'], end))})
    return violations


# ---------------------------------------------------------------------------
# per-job reconciliation
# ---------------------------------------------------------------------------
def _phase_checks(events, failures, unavailable):
    problems = []
    by_job = {}
    for item in events:
        by_job.setdefault(item['job'], []).append(item)
    for job, items in by_job.items():
        order_key = {'prepare': 0, 'load': 1, 'encode': 2, 'verify': 3}
        items.sort(key=lambda x: (_minutes(x['start']) or 0,
                                  order_key.get(x['phase'], 9)))
        for item in items:
            start = _minutes(item['start'])
            end = _minutes(item['end'])
            if start is None or end is None or end < start - EPS:
                problems.append((job, 'invalid_event_window', item))
                continue
            for failure in failures:
                if item['station'] == failure['station'] and start >= failure['at'] - EPS:
                    problems.append((job, 'station_failed', item))
            for window in unavailable:
                if item['phase'] == 'encode' and _overlap((start, end), window):
                    problems.append((job, 'license_unavailable_violation', item))
        phases = {x['phase']: x for x in items if x['state'] == 'completed'}
        if 'prepare' in phases and 'encode' in phases:
            if _minutes(phases['encode']['start']) < _minutes(phases['prepare']['end']) - EPS:
                problems.append((job, 'encode_before_prepare', phases['encode']))
        if 'encode' in phases and 'verify' in phases:
            encodes = [x for x in items if x['phase'] == 'encode'
                       and x['state'] in ('completed', 'interrupted')]
            encode_end = max(_minutes(x['end']) for x in encodes)
            if _minutes(phases['verify']['start']) < encode_end - EPS:
                problems.append((job, 'verify_before_encode_end', phases['verify']))
    return problems


def _job_status(job_id, job, segments, events, conflicts, problems,
                unavailable, failures):
    job = dict(job)
    job['deadline'] = _minutes(job.get('deadline'))
    job['target'] = _minutes(job.get('target'))
    job_problems = [reason for j, reason, item in problems if j == job_id]
    if 'invalid_event_window' in job_problems:
        return '待核', 'invalid_event_window', None
    if 'encode_before_prepare' in job_problems:
        return '待核', 'encode_before_prepare', None
    if 'verify_before_encode_end' in job_problems:
        return '待核', 'verify_before_encode_end', None
    if 'station_failed' in job_problems:
        return '待改派', 'station_failed', None
    if 'license_unavailable_violation' in job_problems:
        return '冲突', 'license_unavailable_violation', None

    items = segments.get(job_id, [])
    completed_encode = [x for x in items if x['state'] == 'completed']
    interrupted = [x for x in items if x['state'] == 'interrupted']

    # a station that did not encode this job cannot declare it complete unless
    # it resumed from a recorded checkpoint
    if completed_encode and interrupted:
        last_interrupted = interrupted[-1]
        resumed = [x for x in completed_encode if x['start'] >= last_interrupted['start'] - EPS]
        if resumed:
            resume = resumed[0]
            planned = job.get('planned') or {}
            full = None
            if planned.get('encode_end') and planned.get('encode_start'):
                full = _minutes(planned['encode_end']) - _minutes(planned['encode_start'])
            # a segment may only count as a start-over if it is at least the
            # planned encode length; when no plan exists it cannot be verified
            redo = full is not None and (resume['end'] - resume['start']) >= full - EPS
            if redo:
                pass  # started over from zero: the interrupted work is discarded
            elif last_interrupted['checkpoint'] <= EPS:
                return '待重做', 'no_checkpoint_for_resume', None
            elif resume['resume_from'] is None or \
                    abs(float(resume['resume_from']) - last_interrupted['checkpoint']) > EPS:
                return '冲突', 'resume_without_matching_checkpoint', None

    losing = [c for c in conflicts if c['loser'] == job_id]
    if losing:
        winner = losing[0]['winner']
        return '冲突', f'license_conflict_with:{winner}', None

    verify = [x for x in events if x['job'] == job_id and x['phase'] == 'verify'
              and x['state'] == 'completed']
    encode_end = max((x['end'] for x in completed_encode), default=None)
    if not completed_encode:
        if interrupted:
            if interrupted[-1]['checkpoint'] > EPS:
                return '待续', 'resumable_from_checkpoint', None
            return '待重做', 'interrupted_without_checkpoint', None
        return '待执行', 'no_encode_event', None
    if not verify:
        return '待校验', 'verify_not_completed', encode_end
    finish = _minutes(verify[-1]['end'])
    deadline = job.get('deadline')
    if deadline is not None and finish > deadline + EPS:
        if encode_end <= deadline + EPS:
            return '逾期', 'verify_after_deadline', finish
        return '逾期', 'deadline_missed', finish
    return '已完成', 'verify_completed_on_time', finish


# ---------------------------------------------------------------------------
# diffs and reassignment
# ---------------------------------------------------------------------------
def _planned_phases(db, queue_id):
    planned = {}
    for row in db.execute('SELECT * FROM plan_phase WHERE id LIKE ?',
                          (f'plan:{queue_id}:%',)):
        item = json.loads(row['body'])
        planned[(item['job'], item['phase'])] = item
    return planned


def _diffs(queue_id, events, planned, kept):
    diffs = []
    for item in sorted(events, key=lambda x: (_minutes(x['start']) or 0, x['job'])):
        expected = planned.get((item['job'], item['phase']))
        actual_start = _minutes(item['start'])
        if item['job'] in kept:
            status, reason = 'carried', 'phase_carried_from_previous_plan'
        elif expected is None:
            status, reason = 'unplanned_phase', 'phase_not_in_frozen_plan'
        elif expected.get('station') != item['station']:
            status, reason = 'reassigned', 'station_changed'
        elif actual_start > _minutes(expected['start']) + EPS:
            status, reason = 'late_start', 'started_after_planned_slot'
        elif actual_start < _minutes(expected['start']) - EPS:
            status, reason = 'early_start', 'started_before_planned_slot'
        else:
            status, reason = 'matched', 'phase_on_plan'
        drift = None
        if expected is not None and actual_start is not None:
            drift = actual_start - _minutes(expected['start'])
        diffs.append({'queue_id': queue_id, 'job': item['job'],
                      'phase': item['phase'], 'station': item['station'],
                      'state': item['state'],
                      'planned_station': expected.get('station') if expected else None,
                      'planned_start': expected.get('start') if expected else None,
                      'actual_start': item['start'], 'actual_end': item['end'],
                      'drift_minutes': drift, 'status': status, 'reason': reason})
    return diffs


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def reconcile(path, file):
    source, payload = capture(path, 'desktop_export_dispatch', file)
    db = connect(path)
    try:
        with db:
            queue_id = str(payload.get('queue_id', ''))
            plan = None
            if queue_id:
                plan_row, plan = body(db, 'export_plan', queue_id)
            if not plan:
                return _legacy_reconcile(db, source, payload)
            events, duplicates = _ingest_events(db, source, payload, queue_id)
            all_events = _load_events(db, queue_id)
            failures = [{'station': str(x['station']), 'at': _minutes(x['at'])}
                        for x in payload.get('failures', [])]
            unavailable = [(_minutes(x['start']), _minutes(x['end']))
                           for x in payload.get('license_unavailable', [])]
            plan_body = plan
            kept = plan_body.get('carried', {})
            planned = _planned_phases(db, queue_id)
            jobs = {}
            for row in db.execute('SELECT * FROM plan_job WHERE id LIKE ?',
                                  (f'plan:{queue_id}:%',)):
                item = json.loads(row['body'])
                jobs[item['id']] = item

            segments = _encode_intervals(all_events)
            conflicts = _license_conflicts(segments)
            for conflict in conflicts:
                conflict['queue_id'] = queue_id
                upsert(db, 'bottleneck', f"bottleneck:{queue_id}:{conflict['loser']}:license",
                       source, payload['version'], conflict, '冲突', 'license_conflict')
            violations = _unavailable_violations(segments, unavailable)
            problems = _phase_checks(all_events, failures, unavailable)

            order = plan_body.get('order', [])
            ranked = order + [j for j in sorted(jobs) if j not in order]
            results = []
            for job_id in ranked:
                job = jobs.get(job_id)
                if not job:
                    continue
                status, reason, finish = _job_status(
                    job_id, job, segments, all_events, conflicts, problems,
                    unavailable, failures)
                diff_body = {'queue_id': queue_id, 'job': job_id, 'status': status,
                             'reason': reason,
                             'deadline': job.get('deadline'),
                             'encode_end': _iso(finish) if reason == 'deadline_missed' else None,
                             'verify_end': job.get('planned', {}).get('verify_end')}
                upsert(db, 'execution_diff', f'diff:{queue_id}:{job_id}', source,
                       payload['version'], diff_body, status, reason)
                results.append(decide(db, '桌面导出执行', job_id, status, reason, source))

            for diff in _diffs(queue_id, all_events, planned, kept):
                key = f"phase:{queue_id}:{diff['job']}:{diff['phase']}:{diff['station']}:{diff['actual_start']}"
                upsert(db, 'execution_diff', key, source, payload['version'], diff,
                       diff['status'], diff['reason'])

            reassigned = _reassignments(planned, all_events, failures)
            for item in reassigned:
                upsert(db, 'reassignment', f"reassign:{queue_id}:{item['job']}",
                       source, payload['version'], item, item['status'], item['reason'])

            lease_body = {'queue_id': queue_id,
                          'occupancy': [{'job': job_id, 'station': seg['station'],
                                         'start': _iso(seg['start']),
                                         'end': _iso(seg['end']),
                                         'state': seg['state']}
                                        for job_id, items in sorted(segments.items())
                                        for seg in items],
                          'conflicts': conflicts,
                          'unavailable_violations': violations,
                          'duplicate_events': duplicates,
                          'reimport_safe': True}
            upsert(db, 'license_lease', f'actual:{queue_id}', source, payload['version'],
                   lease_body, '已核对',
                   'license_occupancy_derived_from_event_log')
            if payload.get('replan'):
                decide(db, '桌面导出执行', queue_id, '需重新规划', payload['replan'], source)
            return results
    finally:
        db.close()


def _reassignments(planned, events, failures):
    assigned = {}
    for (job, phase), item in planned.items():
        if phase == 'encode':
            assigned[job] = item['station']
    out = []
    failed = {item['station'] for item in failures}
    interrupted_on = {}
    for item in sorted(events, key=lambda x: _minutes(x.get('start')) or 0):
        if item['phase'] == 'encode' and item['state'] == 'interrupted':
            interrupted_on[item['job']] = item['station']
    for item in events:
        if item['phase'] != 'encode':
            continue
        original = assigned.get(item['job'])
        if original and item['station'] != original:
            if original in failed or interrupted_on.get(item['job']) == original:
                reason = 'station_failed_reassignment'
            else:
                reason = 'manual_reassignment'
            out.append({'job': item['job'], 'from_station': original,
                        'to_station': item['station'], 'state': item['state'],
                        'start': item['start'], 'end': item['end'],
                        'status': '已改派', 'reason': reason,
                        'carried_phase': item['state'] == 'interrupted'})
    return out


# ---------------------------------------------------------------------------
# legacy row-by-row reconciliation
# ---------------------------------------------------------------------------
def _legacy_reconcile(db, source, payload):
    results = []
    for row in payload.get('rows', []):
        key = str(row['id'])
        job = db.execute('SELECT * FROM export_job WHERE id=?', (str(row['job']),)).fetchone()
        if not job:
            status, reason = '待核', 'job_missing'
        elif row.get('state') != 'completed':
            status, reason = '待续', 'export_not_completed'
        elif datetime.fromisoformat(row['at']) > datetime.fromisoformat(
                json.loads(job['body'])['deadline']):
            status, reason = '逾期', 'deadline_missed'
        else:
            status, reason = '已完成', 'export_on_time'
        save(db, 'dispatch_result', key, source, payload['version'], row, status, reason)
        results.append(decide(db, '桌面导出执行', key, status, reason, source))
    return results
