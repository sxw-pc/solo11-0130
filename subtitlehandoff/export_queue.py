"""Offline desktop export planning.

Two execution paths live here:

* the original single-workstation FIFO planner (payload without
  ``workstations``/``license``/``contract``), kept for compatibility;
* the contract-aware workplan planner.  It schedules preparation, project
  load, encoding and local verification on two desktop stations, serialises
  every encode behind one shared license, and searches every feasible encode
  order for at most eight jobs.  The returned order is therefore optimal for
  the contract objective, not merely the order of the input file.

A job's completion is the end of local verification, never the end of encode.
Equal objectives are broken by the lexicographic encode order of job ids; the
input line order never decides who misses a deadline.
"""
import itertools
import json
from datetime import datetime, timedelta

from .ingest import capture
from .store import connect, save, decide, upsert, body

EPOCH = datetime(1970, 1, 1)
CONTRACTS = ('live', 'internal')
EPS = 1e-7
TIE_BREAK = ('目标值完全并列时取编码序列按 job id 字典序最小的方案；'
             '同一目标下编码站并列时取编号较小的站，装载次数更少者优先；'
             '输入行序不参与排序，因此同一输入重复规划结果稳定。')


# ---------------------------------------------------------------------------
# time helpers: schedules are held as minutes since the Unix epoch
# ---------------------------------------------------------------------------
def _minutes(value):
    if isinstance(value, (int, float)):
        return float(value)
    return (datetime.fromisoformat(str(value)) - EPOCH).total_seconds() / 60.0


def _iso(value):
    if value is None:
        return None
    if abs(value - round(value)) < EPS:
        return (EPOCH + timedelta(minutes=round(value))).isoformat(timespec='minutes')
    return (EPOCH + timedelta(minutes=value)).isoformat(timespec='seconds')


def _windows(raw, fallback):
    result = []
    for item in raw or []:
        if isinstance(item, dict):
            start, end = _minutes(item['start']), _minutes(item['end'])
        else:
            start, end = _minutes(item[0]), _minutes(item[1])
        if end > start:
            result.append((start, end))
    return sorted(result) or [fallback]


def _merge(intervals):
    out = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1] + EPS:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _free_gaps(windows, busy, earliest):
    gaps = []
    busy = _merge(busy)
    for window_start, window_end in windows:
        cursor = max(window_start, earliest)
        if cursor >= window_end - EPS:
            continue
        for busy_start, busy_end in busy:
            if busy_end <= cursor + EPS:
                continue
            if busy_start >= window_end - EPS:
                break
            if busy_start > cursor + EPS:
                gaps.append((cursor, min(busy_start, window_end)))
            cursor = max(cursor, busy_end)
            if cursor >= window_end - EPS:
                break
        if cursor < window_end - EPS:
            gaps.append((cursor, window_end))
    return _merge(gaps)


# ---------------------------------------------------------------------------
# payload normalisation
# ---------------------------------------------------------------------------
def _job(raw, contract):
    job_id = str(raw['id'])
    encode = float(raw.get('encode_minutes', raw.get('minutes', 0)))
    if encode < 0:
        raise ValueError('negative_duration')
    checkpoints = sorted({float(x) for x in raw.get('checkpoints', [])
                          if 0 < float(x) < encode - EPS})
    deadline = raw.get('deadline')
    target = raw.get('target', deadline)
    hard = bool(raw['hard']) if 'hard' in raw else contract == 'live'
    return {
        'id': job_id,
        'project': str(raw.get('project', job_id)),
        'weight': float(raw.get('weight', 1.0)),
        'prepare': float(raw.get('prepare_minutes', 0)),
        'encode': encode,
        'verify': float(raw.get('verify_minutes', 0)),
        'load': float(raw['load_minutes']) if raw.get('load_minutes') is not None else None,
        'deadline': _minutes(deadline) if deadline else None,
        'target': _minutes(target) if target else None,
        'hard': hard,
        'after': str(raw['after']) if raw.get('after') else None,
        'checkpoints': checkpoints,
    }


def _spec(payload):
    contract = str(payload.get('contract', 'live'))
    if contract not in CONTRACTS:
        raise ValueError('unknown_contract')
    jobs = {}
    for raw in payload.get('rows', []):
        job = _job(raw, contract)
        jobs[job['id']] = job
    if len(jobs) > 8:
        raise ValueError('exact_search_requires_at_most_8_jobs')
    license_raw = payload.get('license') or {}
    first_window = license_raw.get('windows') or payload.get('windows')
    if payload.get('start'):
        start = _minutes(payload['start'])
    elif first_window:
        item = first_window[0]
        start = _minutes(item['start']) if isinstance(item, dict) else _minutes(item[0])
    else:
        start = min([job['deadline'] for job in jobs.values()
                     if job['deadline'] is not None] or [0.0])
    latest = max([start] + [job['deadline'] for job in jobs.values()
                            if job['deadline'] is not None]
                 + [job['target'] for job in jobs.values()
                    if job['target'] is not None])
    horizon = max(start + 24 * 60, latest + 24 * 60)
    license_windows = _windows(first_window, (start, horizon))
    occupied = []
    for item in license_raw.get('occupied', []):
        occupied.extend(_windows([item], (start, horizon)))
    stations = {}
    for item in payload.get('workstations') or [{'id': 'A'}, {'id': 'B'}]:
        sid = str(item['id'])
        stations[sid] = {
            'windows': _windows(item.get('windows'), (start, horizon)),
            'preloaded': {str(x) for x in item.get('preloaded', [])},
        }
    return {
        'queue_id': str(payload['queue_id']),
        'contract': contract,
        'start': start,
        'stations': stations,
        'license_windows': license_windows,
        'license_occupied': occupied,
        'license_id': str(license_raw.get('id', 'license')),
        'load_default': float(payload.get('load_minutes', 0)),
        'jobs': jobs,
        'carried': set(),
    }


# ---------------------------------------------------------------------------
# one schedule for a fixed encode order
# ---------------------------------------------------------------------------
def _simulate(order, spec):
    """Build one schedule for a fixed encode order.

    Preparation is scheduled on the earliest free station independently of the
    license, so two stations prepare different projects in parallel while only
    one encode at a time may hold the license.  Project load is charged only
    when the encoding station switches project; it can run while the license is
    still busy (it does not hold the license itself).  Verification always
    starts after the complete encode, and its end - not encode end - is the
    completion time that is compared with the deadline.
    """
    stations = {}
    for sid, item in spec['stations'].items():
        stations[sid] = {'windows': item['windows'],
                         'busy': list(spec.get('reserved_stations', {}).get(sid, [])),
                         'loaded': set(item['preloaded'])}
    license_busy = list(spec['license_occupied']) + list(spec.get('reserved_license', []))
    planned, phases, unscheduled = {}, [], []
    completed = set()
    for job_id in order:
        job = spec['jobs'][job_id]
        if job['after'] and job['after'] not in completed \
                and job['after'] not in spec.get('carried', set()):
            unscheduled.append(job_id)
            planned[job_id] = None
            continue
        encode = job['encode']
        prep = 0.0 if job.get('encode_done') else job['prepare']
        earliest = max(spec['start'], job.get('not_before') or spec['start'])

        # 1. preparation: earliest placement on either station, parallel
        prep_best = None
        for sid in sorted(stations):
            for gap_start, gap_end in _free_gaps(stations[sid]['windows'],
                                                 stations[sid]['busy'], earliest):
                if gap_end - gap_start >= prep - EPS:
                    candidate = (gap_start + prep, sid, gap_start)
                    if prep_best is None or candidate < prep_best:
                        prep_best = candidate
                    break
        if prep_best is None:
            unscheduled.append(job_id)
            planned[job_id] = None
            continue
        prep_end, prep_sid, prep_start = prep_best

        # 2. encoding: choose station/license placement finishing earliest.
        #    Load may start at prep_end and does not require the license.
        best = None
        for sid in sorted(stations):
            station = stations[sid]
            load = 0.0
            if job['project'] not in station['loaded']:
                load = job['load'] if job['load'] is not None else spec['load_default']
            station_earliest = prep_end
            for gap_start, gap_end in _free_gaps(station['windows'], station['busy'],
                                                 station_earliest):
                if gap_end - gap_start < load + encode - EPS:
                    continue
                ready = gap_start + load
                if encode <= EPS:
                    candidate = ((ready, 0 if load <= EPS else 1, sid, ready),
                                 sid, ready)
                    if best is None or candidate[0] < best[0]:
                        best = candidate
                    continue
                for license_start, license_end in _free_gaps(spec['license_windows'],
                                                             license_busy, station_earliest):
                    encode_start = max(ready, license_start)
                    if encode_start + encode > min(gap_end, license_end) + EPS:
                        continue
                    key = (encode_start + encode, 0 if load <= EPS else 1, sid,
                           encode_start)
                    if best is None or key < best[0]:
                        best = (key, sid, encode_start)
        if best is None:
            unscheduled.append(job_id)
            planned[job_id] = None
            continue
        _, sid, encode_start = best
        load = 0.0
        if job['project'] not in stations[sid]['loaded']:
            load = job['load'] if job['load'] is not None else spec['load_default']
        load_start = encode_start - load
        encode_end = encode_start + encode

        # 3. local verification: earliest placement starting at encode end.
        verify = None
        for vsid in sorted(stations):
            for gap_start, gap_end in _free_gaps(stations[vsid]['windows'],
                                                 stations[vsid]['busy'], encode_end):
                if gap_end - gap_start >= job['verify'] - EPS:
                    candidate = (gap_start + job['verify'], vsid, gap_start)
                    if verify is None or candidate < verify:
                        verify = candidate
                    break
        if verify is None:
            unscheduled.append(job_id)
            planned[job_id] = None
            continue
        verify_end, vsid, verify_start = verify

        # commit the whole job only once every phase is placeable
        stations[prep_sid]['busy'].append((prep_start, prep_end))
        stations[sid]['busy'].append((load_start, encode_end))
        stations[sid]['loaded'] = {job['project']}
        if encode > EPS:
            license_busy.append((encode_start, encode_end))
        if job['verify'] > EPS:
            stations[vsid]['busy'].append((verify_start, verify_end))
        if prep > EPS:
            phases.append({'job': job_id, 'phase': 'prepare', 'station': prep_sid,
                           'start': prep_start, 'end': prep_end})
        if load > EPS:
            phases.append({'job': job_id, 'phase': 'load', 'station': sid,
                           'start': load_start, 'end': encode_start})
        if encode > EPS:
            phases.append({'job': job_id, 'phase': 'encode', 'station': sid,
                           'start': encode_start, 'end': encode_end})
        if job['verify'] > EPS:
            phases.append({'job': job_id, 'phase': 'verify', 'station': vsid,
                           'start': verify_start, 'end': verify_end})
        planned[job_id] = {
            'job': job_id, 'project': job['project'],
            'prep_station': prep_sid, 'prep_start': prep_start, 'prep_end': prep_end,
            'load_station': sid, 'load_start': load_start, 'load': load,
            'encode_station': sid,
            'encode_start': encode_start if encode > EPS else None,
            'encode_end': encode_end,
            'verify_station': vsid, 'verify_start': verify_start,
            'verify_end': verify_end,
        }
        completed.add(job_id)
    return {'order': list(order), 'jobs': planned, 'phases': phases,
            'unscheduled': unscheduled, 'license': _merge(license_busy)}


def _valid_order(order, spec):
    position = {job_id: index for index, job_id in enumerate(order)}
    carried = spec.get('carried', set())
    for job_id, job in spec['jobs'].items():
        predecessor = job['after']
        if not predecessor or predecessor in carried:
            continue
        if position.get(predecessor, len(order)) >= position[job_id]:
            return False
    return True


def _metrics(schedule, spec, already_done=()):
    hard_missed = hard_minutes = 0.0
    unscheduled_count = 0
    unscheduled_soft_weight = 0.0
    weighted_tardiness = max_tardiness = 0.0
    details = {}
    for job_id, job in spec['jobs'].items():
        if job_id in already_done:
            details[job_id] = {'scheduled': True, 'late': 0.0, 'carried': True}
            continue
        item = schedule['jobs'].get(job_id)
        if item is None:
            unscheduled_count += 1
            if job['hard']:
                hard_missed += 1
                hard_minutes += ((job['deadline'] - spec['start'])
                                 if job['deadline'] is not None else 1440.0)
            else:
                unscheduled_soft_weight += job['weight']
            details[job_id] = {'scheduled': False, 'late': None,
                               'reason': 'no_feasible_window'}
            continue
        finish = item['verify_end']
        late = max(0.0, finish - job['deadline']) if job['deadline'] is not None else 0.0
        if job['hard'] and late > EPS:
            hard_missed += 1
            hard_minutes += late
        target = job['target'] if job['target'] is not None else job['deadline']
        tardiness = max(0.0, finish - target) if target is not None else 0.0
        weighted_tardiness += job['weight'] * tardiness
        max_tardiness = max(max_tardiness, tardiness)
        details[job_id] = {'scheduled': True, 'late': late,
                           'tardiness': tardiness, 'finish': finish}
    stats = {'hard_missed': int(hard_missed), 'hard_late_minutes': hard_minutes,
             'unscheduled': unscheduled_count,
             'unscheduled_soft_weight': unscheduled_soft_weight,
             'weighted_tardiness': weighted_tardiness,
             'max_tardiness': max_tardiness}
    hard_first = tuple(0 if spec['jobs'][job]['hard'] else 1
                       for job in schedule['order'])
    if spec['contract'] == 'live':
        # Hard commitments first; leftover capacity follows waiting weight.
        key = (hard_missed, hard_minutes, unscheduled_soft_weight,
               weighted_tardiness, hard_first, tuple(schedule['order']))
    else:
        # Every job is soft: schedule as many as possible, then minimise
        # weighted waiting, then the worst single wait against starvation.
        key = (unscheduled_count, unscheduled_soft_weight,
               weighted_tardiness, max_tardiness, tuple(schedule['order']))
    return key, stats, details


def _search(spec, already_done=()):
    ids = sorted(spec['jobs'])
    if not ids:
        empty = {'order': [], 'jobs': {}, 'phases': [], 'unscheduled': [],
                 'license': []}
        key, stats, details = _metrics(empty, spec, already_done)
        return key, empty, stats, details
    best = None
    for order in itertools.permutations(ids):
        if not _valid_order(order, spec):
            continue
        schedule = _simulate(order, spec)
        key, stats, details = _metrics(schedule, spec, already_done)
        if best is None or key < best[0]:
            best = (key, schedule, stats, details)
    if best is None:
        # A dependency cycle leaves no feasible order: report everything as
        # unscheduled instead of returning a silent partial plan.
        empty = {'order': [], 'jobs': {}, 'phases': [],
                 'unscheduled': list(ids), 'license': []}
        key, stats, details = _metrics(empty, spec, already_done)
        best = (key, empty, stats, details)
    return best


# ---------------------------------------------------------------------------
# carry-over of completed / interrupted phases from previous execution
# ---------------------------------------------------------------------------
def _carry(db, queue_id, spec):
    carried = {}
    for row in db.execute('SELECT * FROM execution_event WHERE id LIKE ? ORDER BY id',
                          (f'event:{queue_id}:%',)):
        item = json.loads(row['body'])
        job_id = str(item['job'])
        if job_id not in spec['jobs']:
            continue
        slot = carried.setdefault(job_id, {'encode_done': False, 'verify_done': False,
                                           'checkpoint': 0.0, 'interrupted': False,
                                           'done_at': None, 'last_at': spec['start'],
                                           'intervals': []})
        phase, state = item.get('phase'), item.get('state')
        start, end = _minutes(item.get('start')), _minutes(item.get('end'))
        if phase and start is not None and end is not None and end > start + EPS:
            slot['intervals'].append({'phase': phase, 'station': item.get('station'),
                                      'start': start, 'end': end})
            slot['last_at'] = max(slot['last_at'], end)
        if phase == 'encode' and state == 'completed':
            slot['encode_done'] = True
            slot['done_at'] = max(slot['done_at'] or 0.0, end)
        elif phase == 'encode' and state == 'interrupted':
            slot['interrupted'] = True
            slot['checkpoint'] = max(slot['checkpoint'],
                                     float(item.get('checkpoint_minutes') or 0))
        elif phase == 'encode' and state == 'checkpoint':
            slot['checkpoint'] = max(slot['checkpoint'],
                                     float(item.get('checkpoint_minutes') or 0))
        elif phase == 'verify' and state == 'completed':
            slot['verify_done'] = True
    return carried


def _apply_carry(spec, carried):
    kept = {}
    for job_id, slot in carried.items():
        job = spec['jobs'].get(job_id)
        if not job:
            continue
        job['checkpoints'] = job.get('checkpoints', [])
        if slot['verify_done']:
            kept[job_id] = 'verify_done'
            continue
        if slot['encode_done']:
            kept[job_id] = 'encode_done'
            job.update({'prepare': 0.0, 'load': 0.0, 'encode': 0.0,
                        'encode_done': True,
                        'not_before': slot['done_at'] or spec['start']})
            continue
        if slot['interrupted']:
            job['prepare'] = 0.0
            job['not_before'] = slot['last_at']
            if slot['checkpoint'] > EPS:
                job['encode'] = max(0.0, job['encode'] - slot['checkpoint'])
                job['resume_from'] = slot['checkpoint']
                job['resume_state'] = 'checkpoint'
                job['checkpoints'] = [x for x in job['checkpoints']
                                      if x > slot['checkpoint'] + EPS]
            else:
                job['resume_from'] = 0.0
                job['resume_state'] = 'restart_no_checkpoint'
    # A fully verified job leaves the search: nothing left to place.
    spec['retired'] = {}
    for job_id in [k for k, v in kept.items() if v == 'verify_done']:
        spec['retired'][job_id] = spec['jobs'].pop(job_id)
    spec['carried'] = set(kept)
    # Executed phases stay occupied: a replan may not reuse their license or
    # station time, and may not pretend a completed phase never happened.
    license_reserved, station_reserved = [], {sid: [] for sid in spec['stations']}
    for slot in carried.values():
        for interval in slot['intervals']:
            if interval['phase'] == 'encode':
                license_reserved.append((interval['start'], interval['end']))
            if interval['station'] in station_reserved:
                station_reserved[interval['station']].append(
                    (interval['start'], interval['end']))
    spec['reserved_license'] = license_reserved
    spec['reserved_stations'] = station_reserved
    return kept


# ---------------------------------------------------------------------------
# evidence for a delayed or unscheduled job
# ---------------------------------------------------------------------------
def _capacity_evidence(spec, job, schedule, stats):
    """Earliest defensible evidence for why ``job`` is delayed / unscheduled."""
    deadline = job['deadline'] if job['deadline'] is not None else job['target']
    horizon = max([end for _, end in spec['license_windows']] or
                  [deadline if deadline is not None else spec['start'] + 24 * 60])
    committed = sum(other['encode'] for other in spec['jobs'].values()
                    if other['hard'] and other['id'] != job['id'])
    required = committed + job['encode']
    available = sum(max(0.0, min(end, horizon) - max(start, spec['start']))
                    for start, end in spec['license_windows'])
    occupied = sum(max(0.0, min(end, horizon) - max(start, spec['start']))
                   for start, end in spec['license_occupied'])
    available = max(0.0, available - occupied)
    item = schedule['jobs'].get(job['id'])
    if item is None:
        if job.get('after') and job['after'] in schedule['unscheduled']:
            reason, blocker = 'dependency_unscheduled', f"after:{job['after']}"
            genesis = _iso(spec['start'])
            detail = '前置任务未排入，本任务不能开始'
            required = job['encode']
        elif available < required - EPS:
            reason, blocker = 'license_capacity', 'license'
            genesis = _iso(deadline) if deadline is not None else _iso(horizon)
            detail = ('单一许可在全部可用时段内的容量不足以容纳该任务与更早的硬截止任务'
                      f'（需 {required:.0f} 分钟，可用 {available:.0f} 分钟）')
        else:
            reason, blocker = 'workstation_unavailable', 'workstation'
            genesis = _iso(spec['start'])
            detail = '许可有余量，但工作站可用时段无法容纳准备或本地校验'
    else:
        finish = item['verify_end']
        required = committed + job['encode']
        blocker = 'license'
        if finish <= (deadline if deadline is not None else finish) + EPS:
            reason, blocker = 'soft_target_wait', 'weighted_wait'
            genesis = _iso(finish)
            detail = '软截止目标已过，按等待权重排程并如实标逾期'
        elif item['encode_end'] is not None and deadline is not None \
                and item['encode_end'] <= deadline + EPS:
            reason, blocker = 'verify_tail', 'local_verification'
            genesis = _iso(max(item['encode_end'], deadline - job['verify']))
            detail = '编码在截止前结束，但本地校验被挤到截止之后，不能标按时完成'
        elif item['load'] > EPS:
            reason, blocker = 'project_load_switch', f"load:{item['encode_station']}"
            genesis = _iso(item['encode_start'])
            detail = '切换项目的装载时间推后了编码，许可在装载期间仍可能被占用'
        else:
            reason = 'license_serialization'
            genesis = _iso(item['encode_start'])
            detail = '单一许可被更早的硬截止任务或既有占用串行化'
    return {
        'job': job['id'], 'reason': reason, 'blocker': blocker,
        'deadline': _iso(deadline), 'evidence_at': genesis,
        'required_license_minutes': required,
        'available_license_minutes': available,
        'shortfall_minutes': max(0.0, required - available) if item is None else 0.0,
        'planned_finish': _iso(item['verify_end']) if item else None,
        'detail': detail,
    }


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def _persist(db, source, payload, spec, kept, best, queue_id, revision):
    key, schedule, stats, details = best
    plan_body = {
        'queue_id': queue_id, 'revision': revision, 'contract': spec['contract'],
        'start': _iso(spec['start']), 'order': schedule['order'],
        'objective': {'hard_missed': stats['hard_missed'],
                      'hard_late_minutes': stats['hard_late_minutes'],
                      'weighted_tardiness': stats['weighted_tardiness'],
                      'max_tardiness': stats['max_tardiness'],
                      'unscheduled': stats['unscheduled'],
                      'tie_break': TIE_BREAK},
        'unscheduled': schedule['unscheduled'],
        'carried': kept,
        'license': {'id': spec['license_id'],
                    'windows': [{'start': _iso(a), 'end': _iso(b)}
                                for a, b in spec['license_windows']],
                    'leases': [{'start': _iso(a), 'end': _iso(b)}
                               for a, b in schedule['license']]},
    }
    upsert(db, 'export_plan', queue_id, source, payload['version'], plan_body,
           '已冻结', f"contract={spec['contract']};revision={revision}")
    results = []
    slot_index = lease_index = 0
    ordered = [job_id for job_id in schedule['order'] if job_id in spec['jobs']]
    ordered += [job_id for job_id in sorted(spec['jobs']) if job_id not in ordered]
    ordered += [job_id for job_id in sorted(kept) if job_id not in spec['jobs']]
    for job_id in ordered:
        job = spec['jobs'].get(job_id) or spec.get('retired', {}).get(job_id)
        item = schedule['jobs'].get(job_id)
        carried_state = kept.get(job_id)
        if carried_state == 'verify_done':
            status, reason = '已完成', 'carried_verify_done'
            job_body = {'queue_id': queue_id, 'job': job['id'], 'carried': True,
                        'deadline': _iso(job['deadline']), 'planned': None}
            upsert(db, 'plan_job', f'plan:{queue_id}:{job_id}', source,
                   payload['version'], job_body, status, reason)
            results.append(decide(db, '桌面导出排程', job_id, status, reason, source))
            continue
        if item is None:
            status, reason = '未排入', 'license_capacity'
        else:
            target = job['target'] if job['target'] is not None else job['deadline']
            finish = item['verify_end']
            if finish is None:
                status, reason = '未排入', 'verify_no_slot'
            elif job['hard'] and job['deadline'] is not None and finish > job['deadline'] + EPS:
                status = '逾期'
                reason = 'verify_after_deadline' if item['encode_end'] is not None \
                    and item['encode_end'] <= job['deadline'] + EPS else 'deadline_missed'
            elif target is not None and finish > target + EPS:
                status, reason = '逾期', 'soft_target_missed'
            elif carried_state == 'encode_done':
                status, reason = '待校验', 'carried_encode_done'
            else:
                status = '按时'
                reason = 'hard_deadline_met' if job['hard'] else 'soft_target_met'
        job_body = dict(job)
        planned_view = None
        if item is not None:
            planned_view = {}
            for key, value in item.items():
                if key in ('job', 'project'):
                    continue
                if carried_state == 'encode_done' and \
                        key not in ('verify_station', 'verify_start', 'verify_end'):
                    continue
                planned_view[key] = _iso(value) if key.endswith(('start', 'end')) else value
        job_body.update({'deadline': _iso(job['deadline']),
                         'target': _iso(job['target']),
                         'carried': carried_state,
                         'planned': planned_view,
                         'resume_from': job.get('resume_from'),
                         'resume_state': job.get('resume_state')})
        job_body.pop('encode_done', None)
        upsert(db, 'plan_job', f'plan:{queue_id}:{job_id}', source, payload['version'],
               job_body, status, reason)
        if item is not None:
            for phase in schedule['phases']:
                if phase['job'] != job_id:
                    continue
                # carried work keeps its historical phase rows; only the part
                # still to run may be re-planned
                if carried_state == 'encode_done' and phase['phase'] != 'verify':
                    continue
                phase_body = {'queue_id': queue_id, 'job': job_id,
                              'phase': phase['phase'], 'station': phase['station'],
                              'start': _iso(phase['start']), 'end': _iso(phase['end'])}
                upsert(db, 'plan_phase', f"plan:{queue_id}:{job_id}:{phase['phase']}",
                       source, payload['version'], phase_body, status, reason)
                upsert(db, 'workstation_slot', f'slot:{queue_id}:{slot_index}', source,
                       payload['version'], phase_body, status, reason)
                slot_index += 1
            if item.get('encode_start') is not None and carried_state != 'encode_done':
                lease_body = {'queue_id': queue_id, 'license': spec['license_id'],
                              'job': job_id, 'station': item['encode_station'],
                              'start': _iso(item['encode_start']),
                              'end': _iso(item['encode_end'])}
                upsert(db, 'license_lease', f'lease:{queue_id}:{lease_index}', source,
                       payload['version'], lease_body, status, reason)
                lease_index += 1
        if status in ('逾期', '未排入') or reason == 'verify_after_deadline':
            evidence = _capacity_evidence(spec, job, schedule, stats)
            evidence['queue_id'] = queue_id
            upsert(db, 'bottleneck', f'bottleneck:{queue_id}:{job_id}', source,
                   payload['version'], evidence, status, evidence['reason'])
        results.append(decide(db, '桌面导出排程', job_id, status, reason, source))
    return results, schedule, stats


# ---------------------------------------------------------------------------
# legacy FIFO path
# ---------------------------------------------------------------------------
def _legacy_plan(db, source, payload):
    queue_id = str(payload['queue_id'])
    cursor = datetime.fromisoformat(payload['start'])
    planned, results = [], []
    for row in payload.get('rows', []):
        job_id = str(row['id'])
        begin = cursor
        cursor = begin + timedelta(minutes=float(row['minutes']))
        status = '可按时完成' if cursor <= datetime.fromisoformat(row['deadline']) else '预计逾期'
        job = dict(row, queue_id=queue_id, planned_start=begin.isoformat(),
                   planned_end=cursor.isoformat())
        save(db, 'export_job', job_id, source, payload['version'], job, status,
             'fifo_single_worker')
        planned.append(job)
        results.append(decide(db, '桌面导出排程', job_id, status, 'fifo_single_worker',
                              source))
    save(db, 'export_queue', queue_id, source, payload['version'],
         {'start': payload['start'], 'jobs': planned}, '已规划', 'fifo_single_worker')
    return results


# ---------------------------------------------------------------------------
# public planner
# ---------------------------------------------------------------------------
def plan(path, file):
    source, payload = capture(path, 'desktop_export_queue', file)
    db = connect(path)
    try:
        with db:
            if not ({'workstations', 'license', 'contract'} & set(payload)):
                return _legacy_plan(db, source, payload)
            spec = _spec(payload)
            queue_id = spec['queue_id']
            _, lock = body(db, 'contract_lock', f'contract:{queue_id}')
            if lock and lock['contract'] != spec['contract']:
                raise ValueError('contract_locked')
            previous = db.execute('SELECT * FROM export_plan WHERE id=?',
                                  (queue_id,)).fetchone()
            revision = (json.loads(previous['body'])['revision'] + 1) if previous else 1
            carried = _carry(db, queue_id, spec) if previous else {}
            upsert(db, 'contract_lock', f'contract:{queue_id}', source, payload['version'],
                   {'queue_id': queue_id, 'contract': spec['contract'],
                    'jobs': sorted(spec['jobs']), 'revision': revision},
                   '锁定', 'contract_frozen_before_queue')
            kept = _apply_carry(spec, carried)
            best = _search(spec, already_done=tuple(k for k, v in kept.items()
                                                    if v == 'verify_done'))
            results, schedule, stats = _persist(
                db, source, payload, spec, kept, best, queue_id, revision)
            if previous:
                old = json.loads(previous['body'])
                diff = {'from_revision': old.get('revision'), 'to_revision': revision,
                        'old_order': old.get('order'),
                        'new_order': schedule['order'],
                        'jobs_added': sorted((set(spec['jobs']) | set(kept))
                                             - set(old.get('order', [])) - set(kept)),
                        'carried': sorted(kept),
                        'policy': '旧计划冻结为历史，新计划显式重排未开始与可续跑部分，'
                                  '旧时段不会被悄悄沿用'}
                upsert(db, 'plan_revision', f'revision:{queue_id}:{revision}', source,
                       payload['version'], diff, '已重新规划', 'explicit_replan')
                decide(db, '桌面导出排程', queue_id, '已重新规划', 'explicit_replan',
                       source)
            return results
    finally:
        db.close()
