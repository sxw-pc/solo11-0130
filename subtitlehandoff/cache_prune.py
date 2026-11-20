"""Executable preview-cache cleanup and next-task admission.

A prune request no longer just marks rows as retired.  The planner models the
cache as dependency-ordered physical files (several records may point at one
physical content and therefore share its bytes), protects active review
references and their full ancestor chains, and searches every delete/recompute
ordering that respects dependencies, task start times, review deadlines and the
disk quota *including transient workspace peaks*.  Among feasible sequences it
picks the one with the shortest total recompute time, then the lowest peak.

Execution is opt-in: when ``execute`` is set, every step first re-verifies the
file still lives inside the cache root with an unchanged digest and no new
occupancy, and only a successful physical move is booked as released bytes.
External rewrites or failed moves stop that step and trigger a replan against
the surviving files; replaying a request never books released bytes twice.
"""
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from .ingest import capture
from .store import (connect, save, decide, active_references, save_plan,
                    replace_plan_steps, save_release, save_admission, issue,
                    load_preview_rows)

PLANNED = '已规划'
STARTABLE = '可启动'
BLOCKED = '待空间'
EXECUTED = '已执行'
INTERRUPTED = '已中断'


def _ts(value):
    if value in (None, ''):
        return None
    return datetime.fromisoformat(value)


class State:
    def __init__(self, rows, request, refs):
        self.cache_root = request.get('cache_root')
        self.quota = float(request['quota_mb'])
        self.mode = request.get('mode', 'online_review')
        self.at = _ts(request.get('at')) or datetime(2026, 1, 1, 12, 0, 0)
        self.rows = {str(r['id']): r for r in rows}
        self.body = {k: r['body'] for k, r in self.rows.items()}
        self.parents = {k: [str(x) for x in self.body[k].get('depends_on', []) or []]
                        for k in self.rows}
        self.phys = {k: self.body[k]['physical_id'] for k in self.rows}
        self.size = {k: float(self.body[k].get('size_mb', 0)) for k in self.rows}
        self.gen_cost = {k: float(self.body[k].get('gen_cost_seconds', 0)) for k in self.rows}
        self.durable = {k: bool(self.body[k].get('durable')) for k in self.rows}
        # Physical paths that survived an external event but can no longer be
        # trusted: scans must recognize them instead of flagging new occupancy.
        self.blocked_paths = {str(b['blocked_path']) for b in self.body.values()
                              if b.get('blocked_path')}
        self.protected = self._protected_closure(request, refs)
        self.tasks = self._tasks(request)

    def _protected_closure(self, request, refs):
        # Explicit request-level pins plus every node an active review session
        # references.
        kept = set(str(x) for x in request.get('protected', []) or [])
        referenced = set()
        for ref in refs:
            referenced.add(ref['target_id'])
        kept |= referenced
        # A referenced preview is only watchable while every dependency it was
        # generated from stays in place (ancestors), and its own derived
        # previews stay consistent with it (descendants).
        children = {}
        for node, parents in self.parents.items():
            for parent in parents:
                children.setdefault(parent, []).append(node)

        def spread(seed, edges):
            pending = list(seed)
            while pending:
                cur = pending.pop()
                for nxt in edges.get(cur, []):
                    if nxt in self.rows and nxt not in kept:
                        kept.add(nxt)
                        pending.append(nxt)

        spread(kept, self.parents)
        spread(referenced, children)
        for node, d in self.durable.items():
            if d:
                kept.add(node)
        # A record whose physical path was detached after an external event
        # cannot be safely moved, so it can never be an eviction candidate.
        for node, b in self.body.items():
            if not b.get('path'):
                kept.add(node)
        return kept

    def _tasks(self, request):
        tasks = []
        for t in request.get('tasks', []) or []:
            task = dict(t)
            task['inputs'] = [str(x) for x in t.get('inputs', [])]
            task['start'] = _ts(t.get('start_at')) or self.at
            deadline = t.get('deadline_seconds')
            if deadline is None and self.mode == 'online_review':
                # Online review must make the next scene watchable within 3 min.
                deadline = 180.0
            task['deadline_seconds'] = float(deadline) if deadline is not None else None
            task['temp_mb'] = float(t.get('temp_mb', 0))
            out = t.get('output')
            if out:
                out = dict(out)
                out['id'] = str(out['id'])
                out['depends_on'] = [str(x) for x in out.get('depends_on', []) or task['inputs']]
                out.setdefault('gen_cost_seconds', float(t.get('gen_cost_seconds', 0)))
                out.setdefault('size_mb', float(t.get('output_size_mb', 0)))
                out.setdefault('physical_id', f'phys:new:{out["id"]}')
            task['output'] = out
            # When the requested preview already exists as a healthy cache
            # record, the task starts with zero recompute: its output counts as
            # present from the beginning and neither it nor its ancestors may
            # be evicted.
            task['pre_satisfied'] = bool(out and out['id'] in self.rows
                                         and not self.durable.get(out['id'], False))
            if task['pre_satisfied']:
                self.protected.add(out['id'])
                for parent in self.ancestors(out['id']):
                    self.protected.add(parent)
            tasks.append(task)
        tasks.sort(key=lambda x: x['start'])
        return tasks

    def ancestors(self, node):
        seen, stack = set(), [node]
        while stack:
            cur = stack.pop()
            for parent in self.parents.get(cur, []):
                if parent in self.rows and parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return seen

    def present_physical(self, evicted, done):
        """Distinct physical identities still occupying the disk."""
        ids = set()
        for node in self.rows:
            if node not in evicted and self.body[node].get('present', True):
                ids.add(self.phys[node])
        for task in self.tasks:
            if task['output'] and task['id'] in done:
                ids.add(task['output']['physical_id'])
        return ids

    def physical_size(self, physical_id):
        for node, pid in self.phys.items():
            if pid == physical_id:
                return self.size[node]
        for task in self.tasks:
            out = task['output']
            if out and out['physical_id'] == physical_id:
                return float(out['size_mb'])
        return 0.0

    def usage_mb(self, evicted, done):
        return sum(self.physical_size(pid) for pid in self.present_physical(evicted, done))


def _eviction_legal(state, node, evicted):
    if node in evicted or node in state.protected:
        return False
    # Children die before parents: nothing still present may depend on node.
    for other, parents in state.parents.items():
        if other not in evicted and node in parents:
            return False
    return True


def plan(state):
    missing_inputs = []
    for task in state.tasks:
        for node in task['inputs']:
            if node not in state.rows:
                missing_inputs.append((task['id'], node))
    if missing_inputs:
        task_id, node = missing_inputs[0]
        return {'startable': False, 'reason': 'task_input_missing',
                'shortfall_mb': None, 'blocking_step': {'kind': 'generate', 'subject': task_id,
                                                        'detail': f'missing_input:{node}'},
                'steps': [], 'peak_mb': state.usage_mb(set(), set()),
                'recompute_seconds': None}

    memo = {}

    def search(evicted, done, enforce_quota, deadline_ok, near_miss, depth=0):
        key = (frozenset(evicted), frozenset(done), enforce_quota, deadline_ok)
        if key in memo:
            return memo[key]
        current = state.usage_mb(evicted, done)
        best = None

        def consider(candidate):
            nonlocal best
            if not candidate:
                return
            if best is None or (candidate['recompute'], candidate['peak'],
                                [s['subject'] for s in candidate['steps']]) < \
                               (best['recompute'], best['peak'],
                                [s['subject'] for s in best['steps']]):
                best = candidate

        # Eviction branch: pick any currently removable node.
        for node in sorted(state.rows):
            if not _eviction_legal(state, node, evicted):
                continue
            new_evicted = evicted | {node}
            # Named copies of one physical content free 0MB until the last
            # copy leaves; the branch is still required to reach that point.
            peak = max(current, state.usage_mb(new_evicted, done))
            sub = search(new_evicted, done, enforce_quota, deadline_ok,
                         near_miss, depth + 1)
            if sub:
                step = {'kind': 'evict', 'subject': node,
                        'physical_id': state.phys[node], 'size_mb': state.size[node]}
                consider({'steps': [step] + sub['steps'], 'peak': max(peak, sub['peak']),
                          'recompute': sub['recompute']})

        # Generation branch: start tasks whose start time has come.
        for index, task in enumerate(state.tasks):
            if task['id'] in done:
                continue
            if index > 0 and state.tasks[index - 1]['id'] not in done and \
                    task['start'] > state.tasks[index - 1]['start']:
                continue
            deps = task['output']['depends_on'] if task['output'] else task['inputs']
            missing = [d for d in deps if d in state.rows and d in evicted]
            if missing:
                near_miss.append({'kind': 'generate', 'subject': task['id'],
                                  'detail': 'dependency_evicted:' + ','.join(sorted(missing))})
                continue
            unknown = [d for d in deps if d not in state.rows]
            if unknown:
                near_miss.append({'kind': 'generate', 'subject': task['id'],
                                  'detail': 'dependency_missing:' + ','.join(sorted(unknown))})
                continue
            cost = float(task['output']['gen_cost_seconds']) if task['output'] else 0.0
            if deadline_ok and task['deadline_seconds'] is not None \
                    and cost > task['deadline_seconds']:
                near_miss.append({'kind': 'generate', 'subject': task['id'],
                                  'detail': f'deadline:{cost}>{task["deadline_seconds"]}'})
                continue
            peak = current + task['temp_mb']
            if enforce_quota and peak > state.quota:
                near_miss.append({'kind': 'generate', 'subject': task['id'],
                                  'detail': f'peak:{peak}>{state.quota}',
                                  'shortfall_mb': peak - state.quota,
                                  'peak_mb': peak})
                continue
            new_done = done | {task['id']}
            sub = search(evicted, new_done, enforce_quota, deadline_ok,
                         near_miss, depth + 1)
            if sub:
                step = {'kind': 'generate', 'subject': task['id'],
                        'output': task['output']['id'] if task['output'] else None,
                        'gen_cost_seconds': cost, 'temp_mb': task['temp_mb'],
                        'peak_mb': peak}
                consider({'steps': [step] + sub['steps'],
                          'peak': max(peak, sub['peak']),
                          'recompute': sub['recompute'] + cost})

        if len(done) == len(state.tasks) and current <= state.quota + 1e-9:
            result = {'steps': [], 'peak': current, 'recompute': 0.0}
        else:
            result = best
        memo[key] = result
        return result

    near_miss = []
    initially_done = {t['id'] for t in state.tasks if t.get('pre_satisfied')}
    found = search(set(), initially_done, True, True, near_miss)
    if not found:
        # Rerun without the quota to measure the minimum achievable peak and
        # name the blocking step; deadline failures keep being recorded.
        unconstrained = search(set(), initially_done, False, True, [])
        peak_mb = unconstrained['peak'] if unconstrained else state.usage_mb(set(), set())
        quota_misses = [m for m in near_miss if m['detail'].startswith('peak:')]
        deadline_misses = [m for m in near_miss if m['detail'].startswith('deadline:')]
        if deadline_misses and not quota_misses:
            blocker = min(deadline_misses, key=lambda m: m['subject'])
            reason = 'recompute_deadline_unmet'
            shortfall = None
        else:
            blocker = min(quota_misses, key=lambda m: m['shortfall_mb'],
                          default=(near_miss[0] if near_miss else None))
            reason = 'peak_quota_exceeded'
            shortfall = round(min((m['shortfall_mb'] for m in quota_misses),
                                  default=peak_mb - state.quota), 3)
            if shortfall is not None and shortfall < 0:
                shortfall = 0.0
        return {'startable': False, 'reason': reason, 'shortfall_mb': shortfall,
                'blocking_step': blocker, 'steps': [],
                'peak_mb': round(peak_mb, 3), 'recompute_seconds': None}

    return {'startable': True, 'reason': 'sequence_found',
            'shortfall_mb': 0.0, 'blocking_step': None,
            'steps': found['steps'], 'peak_mb': round(found['peak'], 3),
            'recompute_seconds': round(found['recompute'], 3)}


def _scan_cache(root):
    """Map every regular file under the cache root to size and digest."""
    found = {}
    for p in Path(root).rglob('*'):
        if p.is_file():
            found[str(p.resolve())] = {
                'bytes': p.stat().st_size,
                'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    return found


def _resolve_path(root, raw_path):
    p = Path(raw_path)
    if not p.is_absolute():
        p = Path(root) / p
    return p.resolve()


def _default_generator(task, output_target):
    """Offline stand-in: materialize the generated preview as a small marker.

    Real workstation installs inject their own generator; this one never
    touches the network and only writes inside the requested target path.
    """
    target = Path(output_target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'generated-preview:' + task['id'].encode('utf-8'))
    return target


def execute_plan(state, request, source_id, db, generator=None, prefix_steps=None):
    generator = generator or _default_generator
    root = Path(request['cache_root']).resolve()
    moved_root = Path(request.get('evict_root') or (root.parent / (root.name + '-retired'))).resolve()
    if moved_root == root or root in moved_root.parents:
        raise ValueError('evict_root_must_sit_outside_cache_root')
    moved_root.mkdir(parents=True, exist_ok=True)
    # Steps already completed before an external-event replan stay on record.
    result_steps = list(prefix_steps or [])
    admissions = [{'task_id': t['id'], 'status': STARTABLE,
                   'reason': 'already_cached', 'recompute_seconds': 0.0}
                  for t in state.tasks if t.get('pre_satisfied')]
    for adm in admissions:
        save_admission(db, request['request_id'], adm['task_id'], STARTABLE,
                       'already_cached', None, 0.0, adm)
        decide(db, '任务准入', f'{request["request_id"]}:{adm["task_id"]}',
               STARTABLE, 'already_cached', source_id)
    # Outputs this plan itself will produce are expected to appear under the
    # cache root, so they are not treated as undeclared external occupancy.
    planned_outputs = {str(_resolve_path(root, t['output']['path']))
                       for t in state.tasks if t.get('output') and t['output'].get('path')}
    for seq, step in enumerate(state.plan['steps']):
        scan = _scan_cache(root)
        known_paths = {str(_resolve_path(root, b['path'])): node
                       for node, b in state.body.items() if b.get('path')}
        unexpected = sorted(p for p in set(scan) - set(known_paths)
                            if p not in planned_outputs
                            and p not in state.blocked_paths)
        if unexpected:
            # New, undeclared occupancy is external state the ledger cannot
            # reason about: stop without touching anything and let the station
            # re-capture the inventory instead of looping replans.
            issue(db, source_id, 'new_occupancy_detected', json.dumps(unexpected))
            return _halt(db, request, source_id, result_steps, admissions,
                         'new_occupancy_detected', unexpected)
        if step['kind'] == 'evict':
            node = step['subject']
            body = state.body[node]
            raw_path = body.get('path')
            if not raw_path:
                return _halt(db, request, source_id, result_steps, admissions,
                             'evict_target_without_path', node)
            target = _resolve_path(root, raw_path)
            if root not in target.parents and target != root:
                return _halt(db, request, source_id, result_steps, admissions,
                             'path_escapes_cache_root', node)
            if str(target) not in scan:
                return _replan(state, request, source_id, db, generator, seq,
                               'cache_file_missing', str(target), result_steps, node)
            if body.get('sha256') and scan[str(target)]['sha256'] != body['sha256']:
                return _replan(state, request, source_id, db, generator, seq,
                               'cache_file_rewritten', str(target), result_steps, node)
            physical_id = state.phys[node]
            before_bytes = scan[str(target)]['bytes']
            dest = moved_root / f'{physical_id.replace(":", "_")}__{target.name}'
            try:
                shutil.move(str(target), str(dest))
            except OSError as exc:
                return _replan(state, request, source_id, db, generator, seq,
                               'move_failed', f'{target}:{exc}', result_steps, node)
            after_scan = _scan_cache(root)
            disappeared = str(target) not in after_scan
            moved_nodes = {s.get('subject') for s in result_steps
                           if s['kind'] == 'evict'} | {node}
            # Bytes are released only when this physical content actually
            # leaves the disk.  A named copy whose twin still exists frees zero
            # bytes, so one identity's bytes are never counted twice.
            content_still_present = any(
                state.phys[other] == physical_id and other not in moved_nodes
                for other in state.rows)
            freed = before_bytes if disappeared and not content_still_present else 0
            # Every physical move is audited, but bytes are booked only when
            # the whole physical content leaves: a twin still present means a
            # zero-byte row, so one identity's bytes are never counted twice.
            save_release(db, request['request_id'], physical_id, str(target),
                         str(dest), freed, body.get('sha256', ''),
                         {'node': node, 'seq': seq,
                          'content_still_present': content_still_present})
            save(db, 'preview_cache', node, state.rows[node]['source_id'],
                 state.rows[node]['version'], body, '已淘汰', 'physically_evicted')
            done_step = dict(step, status='done', moved_to=str(dest),
                             released_bytes=freed)
            result_steps.append(done_step)
            decide(db, '缓存清理', f'{request["request_id"]}:{node}', '已淘汰',
                   'physically_evicted', source_id)
        else:
            task = next(t for t in state.tasks if t['id'] == step['subject'])
            out = task['output']
            output_target = _resolve_path(root, out['path']) if out.get('path') \
                else root / f'generated-{out["id"]}.preview'
            try:
                produced = generator(task, str(output_target))
            except OSError as exc:
                return _halt(db, request, source_id, result_steps, admissions,
                             'generate_failed', f'{task["id"]}:{exc}')
            produced = Path(produced)
            actual_bytes = produced.stat().st_size if produced.exists() else 0
            done_step = dict(step, status='done', output_path=str(produced),
                             output_bytes=actual_bytes)
            result_steps.append(done_step)
            admissions.append({'task_id': task['id'], 'status': STARTABLE,
                               'reason': 'generated', 'output_path': str(produced)})
            decide(db, '任务准入', f'{request["request_id"]}:{task["id"]}',
                   STARTABLE, 'generated', source_id)

    final_peak = state.plan['peak_mb']
    executed = {'request_id': request['request_id'], 'startable': True,
                'status': EXECUTED, 'reason': 'plan_executed',
                'mode': state.mode, 'quota_mb': state.quota,
                'peak_mb': final_peak, 'recompute_seconds': state.plan['recompute_seconds'],
                'steps': result_steps, 'admissions': admissions,
                'released_bytes_total': sum(s.get('released_bytes', 0) for s in result_steps
                                            if s['kind'] == 'evict')}
    save_plan(db, request['request_id'], source_id, state.mode, state.quota,
              EXECUTED, 'plan_executed',
              {'steps': result_steps, 'peak_mb': final_peak,
               'recompute_seconds': state.plan['recompute_seconds'],
               'released_bytes_total': executed['released_bytes_total']},
              attempt=getattr(state, 'attempt', 1))
    replace_plan_steps(db, request['request_id'], result_steps)
    return executed


def _halt(db, request, source_id, steps, admissions, reason, subject):
    for s in steps:
        s.setdefault('status', 'done')
    save_plan(db, request['request_id'], source_id, request.get('mode'),
              float(request['quota_mb']), INTERRUPTED, reason,
              {'steps': steps, 'halted_at': subject})
    return {'request_id': request['request_id'], 'startable': False,
            'status': INTERRUPTED, 'reason': reason, 'halted_at': subject,
            'steps': steps, 'admissions': admissions}


def _replan(state, request, source_id, db, generator, seq, reason, evidence,
            done_steps, affected_node=None):
    """Rebuild the plan from the files that actually survive.

    The affected record is not deleted: its physical file may still be on disk
    (rewrite, failed move), so it is detached from its path and marked blocked.
    That keeps it recognizable during pre-flight scans instead of looking like
    undeclared new occupancy, while the planner can no longer evict it.
    """
    issue(db, source_id, reason, str(evidence))
    surviving = []
    for r in load_preview_rows(db):
        if r['status'] == '已淘汰':
            continue
        if affected_node is not None and r['id'] == affected_node:
            r = dict(r)
            r['body'] = dict(r['body'])
            original_path = r['body'].get('path')
            if original_path:
                r['body']['blocked_path'] = str(
                    _resolve_path(request['cache_root'], original_path))
            r['body']['path'] = None
            r['body']['blocked'] = reason
            # A file an external process already removed occupies nothing;
            # a rewritten/failed-move file is still physically on disk and its
            # (untrusted) bytes keep counting against the quota.
            r['body']['present'] = reason != 'cache_file_missing'
            r['status'] = '待核'
        surviving.append(r)
    refs = active_references(db, state.at.isoformat())
    attempt = getattr(state, 'attempt', 1) + 1
    if attempt > 3:
        return _halt(db, request, source_id, done_steps, [],
                     'replan_attempt_limit', reason)
    rebuilt = State(surviving, request, refs)
    rebuilt.attempt = attempt
    rebuilt.plan = plan(rebuilt)
    if not rebuilt.plan['startable']:
        save_plan(db, request['request_id'], source_id, request.get('mode'),
                  float(request['quota_mb']), BLOCKED,
                  'replan:' + rebuilt.plan['reason'],
                  {'original_reason': reason, 'evidence': evidence,
                   'shortfall_mb': rebuilt.plan['shortfall_mb'],
                   'blocking_step': rebuilt.plan['blocking_step']})
        return {'request_id': request['request_id'], 'startable': False,
                'status': INTERRUPTED, 'reason': 'replan_blocked:' + rebuilt.plan['reason'],
                'shortfall_mb': rebuilt.plan['shortfall_mb'],
                'blocking_step': rebuilt.plan['blocking_step'], 'steps': done_steps}
    return execute_plan(rebuilt, request, source_id, db, generator,
                        prefix_steps=done_steps)


def prune(path, file, generator=None):
    source, payload = capture(path, 'desktop_cache_prune', file)
    db = connect(path)
    try:
        with db:
            request_id = str(payload['request_id'])
            replay = db.execute('SELECT * FROM cache_plan WHERE id=?', (request_id,)).fetchone()
            if replay:
                stored = json.loads(replay['body'])
                admissions = [dict(r) for r in db.execute(
                    'SELECT * FROM cache_task_admission WHERE request_id=?', (request_id,))]
                return {'request_id': request_id,
                        'status': replay['status'], 'reason': replay['reason'],
                        'replayed': True,
                        'startable': replay['status'] in (PLANNED, STARTABLE, EXECUTED),
                        'mode': replay['mode'], 'quota_mb': replay['quota_mb'],
                        'peak_mb': stored.get('peak_mb'),
                        'recompute_seconds': stored.get('recompute_seconds'),
                        'shortfall_mb': stored.get('shortfall_mb'),
                        'blocking_step': stored.get('blocking_step'),
                        'steps': stored.get('steps', []),
                        'admissions': admissions}

            # Legacy record-only request: preserve the historical oldest-first
            # behaviour so existing inventories keep their semantics.
            if not payload.get('tasks') and not payload.get('mode'):
                entries = [(x, json.loads(x['body'])) for x in
                           db.execute("SELECT * FROM preview_cache WHERE status='已缓存'")]
                total = sum(float(body['size_mb']) for _, body in entries)
                removed = []
                for item, body in sorted(entries, key=lambda pair: pair[1]['last_used']):
                    if total <= float(payload['quota_mb']):
                        break
                    total -= float(body['size_mb'])
                    removed.append(item['id'])
                    save(db, 'preview_cache', item['id'], item['source_id'], item['version'],
                         body, '已淘汰', 'oldest_first')
                save(db, 'cache_prune', request_id, source, payload['version'],
                     {'removed': removed, 'remaining_mb': total}, PLANNED, 'oldest_first')
                return decide(db, '缓存清理', request_id, PLANNED, 'oldest_first', source)

            refs = active_references(db, payload.get('at') or '9999-12-31T00:00:00')
            state = State(load_preview_rows(db), payload, refs)
            state.plan = plan(state)
            verdict = state.plan
            steps = [dict(s, status='planned') for s in verdict['steps']]
            save_plan(db, request_id, source, state.mode, state.quota,
                      STARTABLE if verdict['startable'] else BLOCKED,
                      verdict['reason'], {'steps': steps,
                                          'peak_mb': verdict['peak_mb'],
                                          'recompute_seconds': verdict['recompute_seconds'],
                                          'shortfall_mb': verdict['shortfall_mb'],
                                          'blocking_step': verdict['blocking_step']})
            replace_plan_steps(db, request_id, steps)
            for task in state.tasks:
                save_admission(db, request_id, task['id'],
                               STARTABLE if verdict['startable'] else BLOCKED,
                               verdict['reason'], task['deadline_seconds'],
                               verdict['recompute_seconds'],
                               {'peak_mb': verdict['peak_mb'],
                                'shortfall_mb': verdict['shortfall_mb'],
                                'blocking_step': verdict['blocking_step']})
                decide(db, '任务准入', f'{request_id}:{task["id"]}',
                       STARTABLE if verdict['startable'] else BLOCKED,
                       verdict['reason'], source)
            decide(db, '缓存清理', request_id,
                   STARTABLE if verdict['startable'] else BLOCKED,
                   verdict['reason'], source)

            if not verdict['startable'] or not payload.get('execute'):
                return {'request_id': request_id,
                        'startable': verdict['startable'],
                        'status': STARTABLE if verdict['startable'] else BLOCKED,
                        'reason': verdict['reason'], 'mode': state.mode,
                        'quota_mb': state.quota, 'peak_mb': verdict['peak_mb'],
                        'shortfall_mb': verdict['shortfall_mb'],
                        'recompute_seconds': verdict['recompute_seconds'],
                        'blocking_step': verdict['blocking_step'], 'steps': steps,
                        'admissions': [dict(r) for r in db.execute(
                            'SELECT * FROM cache_task_admission WHERE request_id=?',
                            (request_id,))]}
            return execute_plan(state, payload, source, db, generator)
    finally:
        db.close()
