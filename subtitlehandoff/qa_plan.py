"""Offline desktop-export regression planning over an explicit UI state graph.

The planner never trusts coverage labels on candidate cases. Every candidate is
replayed structurally against the captured state graph: each step must be a
legal transition from the current window, duplicate button text is resolved by
window state, and every high-risk target is only credited when the exact
transition (with its modal) is walked from a legal starting snapshot.

Two contracts are fixed in the input *before* recording:
  * smoke  - draft-save and the irreversible publish may be chained on one copy
             as long as each transition keeps its own checkpoint;
  * audit  - every irreversible target starts from a clean snapshot on its own
             workspace copy and the draft-only control path gets a separate
             copy. A shortage of copies is reported as an uncovered target,
             never silently downgraded.
"""
from .ingest import capture
from .store import connect, save, decide

STATE_CAP = 10
TRANSITION_CAP = 12
TARGET_CAP = 3
SMOKE = 'smoke'
AUDIT = 'audit'
CONTRACTS = (SMOKE, AUDIT)


# --------------------------------------------------------------------------- #
# Graph validation
# --------------------------------------------------------------------------- #
def validate_graph(graph):
    """Return (states_by_id, transitions_by_id, targets_by_id) or raise."""
    states = graph.get('states')
    transitions = graph.get('transitions')
    targets = graph.get('targets')
    if not isinstance(states, list) or not states:
        raise ValueError('graph_states_required')
    if not isinstance(transitions, list) or not transitions:
        raise ValueError('graph_transitions_required')
    if not isinstance(targets, list) or not targets:
        raise ValueError('graph_targets_required')
    if len(states) > STATE_CAP:
        raise ValueError('state_cap_exceeded')
    if len(transitions) > TRANSITION_CAP:
        raise ValueError('transition_cap_exceeded')
    if len(targets) > TARGET_CAP:
        raise ValueError('target_cap_exceeded')
    state_ids, modal_states, terminals = set(), set(), set()
    for st in states:
        sid = str(st.get('id'))
        if not sid or sid in state_ids:
            raise ValueError('duplicate_or_blank_state_id')
        state_ids.add(sid)
        if st.get('modal'):
            modal_states.add(sid)
        if st.get('terminal'):
            terminals.add(sid)
    edge_ids = set()
    edges = {}
    for tr in transitions:
        tid = str(tr.get('id'))
        if not tid or tid in edge_ids:
            raise ValueError('duplicate_or_blank_transition_id')
        edge_ids.add(tid)
        if tr.get('from_state') not in state_ids or tr.get('to_state') not in state_ids:
            raise ValueError('transition_references_unknown_state')
        if not str(tr.get('button_text', '')).strip():
            raise ValueError('button_text_required')
        if not str(tr.get('evidence', '')).strip():
            raise ValueError('evidence_required')
        edges[tid] = {
            'id': tid,
            'from_state': tr['from_state'],
            'to_state': tr['to_state'],
            'button_text': tr['button_text'],
            'evidence': tr['evidence'],
            'risk': str(tr.get('risk', 'low')),          # low|high
            'mutates': bool(tr.get('mutates', False)),   # changes files/publish
            'irreversible': bool(tr.get('irreversible', False)),
            'runtime_s': float(tr.get('runtime_s', 1.0)),
        }
    target_ids = set()
    target_map = {}
    for tg in targets:
        gid = str(tg.get('id'))
        if not gid or gid in target_ids:
            raise ValueError('duplicate_or_blank_target_id')
        target_ids.add(gid)
        if tg.get('transition') not in edges:
            raise ValueError('target_references_unknown_transition')
        target_map[gid] = {'id': gid, 'name': tg.get('name', gid),
                           'transition': tg['transition'],
                           'required_modal': tg.get('required_modal')}
    return ({s['id']: dict(s, modal=s['id'] in modal_states,
                           terminal=s['id'] in terminals) for s in states},
            edges, target_map, modal_states, terminals)


def _build_indexes(edges):
    """Map state -> outgoing transitions and (state,button_text) -> edges."""
    outgoing, by_button = {}, {}
    for tr in edges.values():
        outgoing.setdefault(tr['from_state'], []).append(tr)
        by_button.setdefault((tr['from_state'], tr['button_text']), []).append(tr)
    return outgoing, by_button


# --------------------------------------------------------------------------- #
# Candidate-case structural replay (planning time, no execution events yet)
# --------------------------------------------------------------------------- #
def walk_case(case, states, edges, by_button, modal_states, terminals):
    """Turn a candidate case into a legal, checkpointed state path.

    Returns dict with steps, runtime, covered targets and structural defects.
    A rejected case carries reason and no legal path.
    """
    start = str(case.get('start_snapshot_state', ''))
    if start not in states:
        return {'ok': False, 'reason': 'unknown_start_snapshot'}
    current = start
    steps, runtime, walked = [], 0.0, set()
    for i, raw in enumerate(case.get('steps', [])):
        tid = str(raw.get('transition', ''))
        tr = edges.get(tid)
        if tr is None:
            return {'ok': False, 'reason': 'unknown_transition', 'step_index': i}
        if tr['from_state'] != current:
            return {'ok': False, 'reason': 'illegal_transition_for_window',
                    'step_index': i, 'window': current, 'transition': tid}
        # A modal on screen must be handled before a main-window action; the
        # handling transition itself starts inside the modal and is allowed.
        if current in modal_states and tr['from_state'] not in modal_states:
            return {'ok': False, 'reason': 'modal_must_be_handled_first',
                    'step_index': i, 'window': current}
        if current in terminals:
            return {'ok': False, 'reason': 'terminal_state_has_no_exit',
                    'step_index': i, 'window': current}
        ambiguous = len(by_button.get((current, tr['button_text']), [])) > 1
        # Publish confirmation specifically requires its modal as evidence.
        checkpoint = {
            'seq': i,
            'transition': tid,
            'window_before': current,
            'window_after': tr['to_state'],
            'button_text': tr['button_text'],
            'ambiguous_label': ambiguous,
            'required_evidence': tr['evidence'],
            'mutates': tr['mutates'],
            'irreversible': tr['irreversible'],
            'modal_open_after': tr['to_state'] in modal_states,
        }
        steps.append(checkpoint)
        walked.add(tid)
        runtime += tr['runtime_s']
        current = tr['to_state']
    if current in modal_states and current != 'export_failure_dialog':
        return {'ok': False, 'reason': 'case_ends_inside_unhandled_modal',
                'window': current}
    return {'ok': True, 'steps': steps, 'end_window': current,
            'paused_in_failure_modal': current == 'export_failure_dialog',
            'runtime_s': runtime, 'walked_transitions': sorted(walked)}


def _targets_for_steps(walked, targets):
    hit = set()
    for tg in targets.values():
        if tg['transition'] in walked:
            hit.add(tg['id'])
    return hit


# --------------------------------------------------------------------------- #
# Workspace copy accounting and shortest feasible subset
# --------------------------------------------------------------------------- #
def _copies_needed(case_ids, legal, contract):
    """Copies a chosen set of cases must consume under the contract.

    Only cases that mutate files or publish state burn a workspace copy. A
    case that merely opens/observes the failure dialog runs on the reset
    baseline and consumes nothing.
    """
    mutating = [cid for cid in case_ids if legal[cid]['mutates']]
    if contract == SMOKE:
        # The chained smoke path shares one copy; every other mutating case
        # that cannot join the chain needs its own copy.
        chain = 1 if any(legal[cid]['chained'] for cid in mutating) else 0
        extra = sum(1 for cid in mutating if not legal[cid]['chained'])
        return chain + extra
    # Audit: every mutating/irreversible case is isolated on a fresh copy.
    return len(mutating)


def _candidate_options(legal, targets, contract, copy_budget, runtime_budget):
    """All feasible non-empty case subsets ranked for the shortest script set."""
    ids = sorted(legal)
    options = []
    for mask in range(1, 1 << len(ids)):
        chosen = [ids[i] for i in range(len(ids)) if mask & (1 << i)]
        covered = set()
        for cid in chosen:
            covered |= legal[cid]['targets']
        copies = _copies_needed(chosen, legal, contract)
        runtime = sum(legal[cid]['runtime_s'] for cid in chosen)
        if copies > copy_budget or runtime > runtime_budget:
            continue
        if contract == AUDIT:
            # No chaining: a script covering an irreversible target must start
            # from a clean snapshot, so a case cannot carry two targets.
            if any(len(legal[cid]['targets']) > 1 for cid in chosen):
                continue
        options.append({'cases': chosen, 'covered': covered,
                        'copies': copies, 'runtime_s': runtime,
                        'scripts': len(chosen)})
    # Max coverage first, then least runtime, fewest copies, fewest scripts.
    options.sort(key=lambda o: (-len(o['covered']), o['runtime_s'],
                                o['copies'], o['scripts'], o['cases']))
    return options


def _minimal_blocker(target, legal, contract, copies_available, runtime_available):
    """Cheapest reason a target cannot be covered with the remaining budget."""
    for cid, info in sorted(legal.items(), key=lambda kv: kv[1]['runtime_s']):
        if target not in info['targets']:
            continue
        need = _copies_needed([cid], legal, contract)
        if need > copies_available:
            return {'target': target, 'blocker': 'workspace_copy_shortage',
                    'cheapest_case': cid,
                    'copies_required': need, 'copies_available': copies_available}
        if info['runtime_s'] > runtime_available:
            return {'target': target, 'blocker': 'runtime_budget_exceeded',
                    'cheapest_case': cid,
                    'runtime_required_s': info['runtime_s'],
                    'runtime_available_s': runtime_available}
    return {'target': target, 'blocker': 'no_legal_path_from_start_snapshot'}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def plan(path, file):
    source, payload = capture(path, 'desktop_ui_state_model', file)
    db = connect(path)
    try:
        with db:
            states, edges, targets, modal_states, terminals = validate_graph(payload['graph'])
            contract = payload.get('contract', SMOKE)
            if contract not in CONTRACTS:
                raise ValueError('unknown_contract')
            copy_budget = int(payload['limits']['workspace_copies'])
            runtime_budget = float(payload['limits']['max_total_runtime_s'])
            if copy_budget < 1 or runtime_budget <= 0:
                raise ValueError('invalid_limits')
            _, by_button = _build_indexes(edges)

            # Persist the immutable state graph and the clean snapshots.
            save(db, 'ui_state_graph', payload['graph'].get('graph_id', 'graph:default'),
                 source, payload['version'], payload['graph'], '已采录', 'state_graph_frozen')
            snapshots = {}
            for snap in payload.get('snapshots', []):
                sid = str(snap['id'])
                if snap['state'] not in states:
                    raise ValueError('snapshot_references_unknown_state')
                snapshots[sid] = snap
                save(db, 'ui_snapshot', sid, source, payload['version'], snap,
                     '可重置', 'clean_workspace_snapshot')
            for ws in payload.get('workspaces', []):
                if ws['snapshot'] not in snapshots:
                    raise ValueError('workspace_references_unknown_snapshot')
                save(db, 'ui_workspace', str(ws['id']), source, payload['version'],
                     {**ws, 'consumed_by': [], 'state': 'available'},
                     '可用', 'workspace_registered')

            # Structural replay of every candidate case; labels never trusted.
            legal, rejected = {}, []
            for case in payload.get('cases', []):
                cid = str(case['id'])
                walk = walk_case(case, states, edges, by_button, modal_states, terminals)
                save(db, 'ui_case', cid, source, payload['version'],
                     {**case, 'walk': walk},
                     '合法' if walk['ok'] else '拒收',
                     walk.get('reason', 'legal_state_path'))
                if not walk['ok']:
                    rejected.append({'case': cid, 'reason': walk['reason']})
                    continue
                hit = _targets_for_steps(set(walk['walked_transitions']), targets)
                label_overclaim = sorted(set(map(str, case.get('coverage_labels', []))) - hit)
                mutates = any(s['mutates'] for s in walk['steps'])
                irreversible = any(s['irreversible'] for s in walk['steps'])
                # Only the smoke contract allows one case to chain two targets.
                chained = len(hit) > 1 and contract == SMOKE
                legal[cid] = {
                    'case_id': cid,
                    'start_snapshot': case.get('start_snapshot'),
                    'start_snapshot_state': case['start_snapshot_state'],
                    'steps': walk['steps'],
                    'end_window': walk['end_window'],
                    'paused_in_failure_modal': walk['paused_in_failure_modal'],
                    'runtime_s': walk['runtime_s'],
                    'targets': hit,
                    'label_overclaim': label_overclaim,
                    'mutates': mutates,
                    'irreversible': irreversible,
                    'chained': chained,
                }

            options = _candidate_options(legal, targets, contract, copy_budget, runtime_budget)
            suite_key = str(payload['suite_id'])
            all_targets = set(targets)
            if not options:
                uncovered = [_minimal_blocker(t, legal, contract, copy_budget, runtime_budget)
                             for t in sorted(targets)]
                body = {'contract': contract, 'limits': payload['limits'],
                        'scripts': [], 'covered_targets': [],
                        'uncovered_targets': uncovered,
                        'rejected_cases': rejected,
                        'workspace_reservations': []}
                save(db, 'qa_suite', suite_key, source, payload['version'], body,
                     '资源不足', 'no_feasible_script_set')
                out = decide(db, '桌面回归规划', suite_key, '资源不足',
                             'no_feasible_script_set', source)
                out.update(body)
                return out

            best = options[0]
            # Audit contract is all-or-nothing: every high-risk target needs
            # its own isolated clean copy. If the fixed budget cannot isolate
            # all mutating paths the whole audit is reported short, never
            # downgraded to partial coverage.
            mutating_legal = [cid for cid in legal if legal[cid]['mutates']]
            full_isolation_copies = _copies_needed(mutating_legal, legal, AUDIT)
            short = sorted(all_targets - best['covered'])
            if contract == AUDIT and (short or full_isolation_copies > copy_budget):
                # No target may be claimed: report every target's isolation gap.
                missing = sorted(all_targets)
                # Each uncovered target needs its own isolated clean copy; the
                # fixed budget cannot be downgraded, so report the exact gap.
                uncovered = []
                for t in missing:
                    carrier = min((cid for cid in legal if t in legal[cid]['targets']),
                                  key=lambda cid: legal[cid]['runtime_s'], default=None)
                    uncovered.append({
                        'target': t,
                        'blocker': 'audit_workspace_copy_shortage',
                        'cheapest_case': carrier,
                        'copies_required_per_target': 1,
                        'copies_available': copy_budget,
                        'total_isolated_copies_needed':
                            sum(1 for cid in legal if legal[cid]['mutates']
                                and t in legal[cid]['targets']) or 1,
                        'note': 'audit_contract_requires_clean_snapshot_per_irreversible_target'})
                body = {'contract': contract, 'limits': payload['limits'],
                        'scripts': [], 'covered_targets': [],
                        'uncovered_targets': uncovered,
                        'rejected_cases': rejected,
                        'workspace_reservations': []}
                save(db, 'qa_suite', suite_key, source, payload['version'], body,
                     '资源不足', 'audit_workspace_copy_shortage')
                out = decide(db, '桌面回归规划', suite_key, '资源不足',
                             'audit_workspace_copy_shortage', source)
                out.update(body)
                return out

            workspace_pool = [str(w['id']) for w in payload.get('workspaces', [])]
            pool_used = set()
            chain_workspace = None

            def take_workspace(snapshot_id):
                for wid in workspace_pool:
                    if wid in pool_used:
                        continue
                    ws = next(w for w in payload['workspaces'] if str(w['id']) == wid)
                    if str(ws['snapshot']) == str(snapshot_id):
                        pool_used.add(wid)
                        return wid
                return None

            scripts, reservations = [], []
            for cid in best['cases']:
                info = legal[cid]
                if contract == SMOKE and info['chained'] and chain_workspace is None:
                    chain_workspace = take_workspace(info['start_snapshot'])
                    reservations.append({'workspace': chain_workspace,
                                         'snapshot': info['start_snapshot'],
                                         'cases': [cid], 'shared_chain': True})
                    wid = chain_workspace
                elif info['mutates']:
                    wid = take_workspace(info['start_snapshot'])
                    if wid is None:
                        raise ValueError('workspace_pool_exhausted')
                    reservations.append({'workspace': wid,
                                         'snapshot': info['start_snapshot'],
                                         'cases': [cid], 'shared_chain': False})
                else:
                    wid = chain_workspace
                scripts.append({
                    'case': cid,
                    'start_snapshot': info['start_snapshot'],
                    'start_window': info['start_snapshot_state'],
                    'workspace': wid,
                    'action_sequence': [s['transition'] for s in info['steps']],
                    'steps': info['steps'],
                    'expected_end_window': info['end_window'],
                    'paused_in_failure_modal': info['paused_in_failure_modal'],
                    'runtime_s': info['runtime_s'],
                    'covered_targets': sorted(info['targets']),
                    'checkpoints': [
                        {'seq': s['seq'], 'transition': s['transition'],
                         'window_before': s['window_before'],
                         'window_after': s['window_after'],
                         'button_text': s['button_text'],
                         'ambiguous_label': s['ambiguous_label'],
                         'required_evidence': s['required_evidence'],
                         'modal_open_after': s['modal_open_after']}
                        for s in info['steps']],
                })
            covered = sorted(best['covered'])
            uncovered = []
            for t in short:
                b = _minimal_blocker(t, legal, contract,
                                     max(0, copy_budget - best['copies']),
                                     max(0.0, runtime_budget - best['runtime_s']))
                uncovered.append(b)
            body = {'contract': contract, 'limits': payload['limits'],
                    'scripts': scripts,
                    'covered_targets': covered,
                    'uncovered_targets': uncovered,
                    'rejected_cases': rejected,
                    'total_runtime_s': best['runtime_s'],
                    'workspace_copies_used': best['copies'],
                    'workspace_reservations': reservations}
            status = '已规划' if not uncovered else '部分规划'
            reason = 'shortest_feasible_scripts' if not uncovered else 'partial_target_coverage'
            save(db, 'qa_suite', suite_key, source, payload['version'], body, status, reason)
            out = decide(db, '桌面回归规划', suite_key, status, reason, source)
            out.update(body)
            return out
    finally:
        db.close()
