"""Replay simulated desktop-export recordings against the frozen state graph.

A ``passed:true`` boolean can never prove a path. Each recorded event must
replay one planned transition: the window before the click has to match, the
clicked button is disambiguated by window state when the same text appears in
two dialogs, the post-action visible evidence must match the graph, and no
frame may be missing. A case that never walks the publish-confirmation modal
is returned even if its recorded boolean claims success.

The replay is deterministic and offline. Replaying the *same* execution log
returns the stored verdict and consumes no additional workspace copy; an
irreversible publish already executed is never run again.
"""
from .ingest import capture
from .store import connect, save, load, replace, decide
import json


def _index_graph(graph):
    edges = {tr['id']: tr for tr in graph['transitions']}
    by_button = {}
    for tr in graph['transitions']:
        by_button.setdefault((tr['from_state'], tr['button_text']), []).append(tr)
    modal_states = {s['id'] for s in graph['states'] if s.get('modal')}
    terminals = {s['id'] for s in graph['states'] if s.get('terminal')}
    return edges, by_button, modal_states, terminals


def _event_evidence(ev):
    return str(ev.get('visible_evidence', '') or ev.get('evidence', ''))


def replay(recording, suite, graph):
    """Replay one recording against one planned script.

    Returns a verdict dict with per-step evidence, target verdicts and either a
    hard failure or a resumable checkpoint at the export-failure dialog.
    """
    edges, by_button, modal_states, terminals = _index_graph(graph)
    script = next((s for s in suite['body']['scripts']
                   if s['case'] == str(recording['case'])), None)
    if script is None:
        return {'status': '退回', 'reason': 'case_not_in_plan',
                'steps': [], 'target_verdicts': {}, 'replay_completed': False}

    events = recording.get('events', [])
    planned = script['steps']
    step_evidence, current = [], script['start_window']
    failure = None
    failure_step = None

    for i, tr_plan in enumerate(planned):
        if i >= len(events):
            # Running out of frames exactly at the failure dialog is a legal
            # resumable pause; anywhere else is a genuine missing frame.
            if current == 'export_failure_dialog':
                break
            failure = ('frame_missing', i)
            failure_step = i
            break
        ev = events[i]
        tid = str(ev.get('transition', ''))
        # 1. transition identity must match the plan.
        if tid != tr_plan['transition']:
            failure = ('transition_mismatch', i)
            failure_step = i
            break
        tr = edges[tid]
        # 2. the window actually visible before the click must match the graph.
        window_before = ev.get('window_before')
        if window_before != tr_plan['window_before'] or window_before != tr['from_state']:
            failure = ('window_state_mismatch', i)
            failure_step = i
            break
        # 3. duplicate button text is resolved by window, never by the label.
        candidates = by_button.get((window_before, ev.get('button_text')), [])
        if tr_plan['ambiguous_label']:
            matching = [c for c in candidates if c['id'] == tid]
            if not candidates or not matching:
                failure = ('ambiguous_button_unresolved', i)
                failure_step = i
                break
        elif ev.get('button_text') != tr['button_text']:
            failure = ('button_text_mismatch', i)
            failure_step = i
            break
        # 4. a modal on screen must be handled before any other action.
        if current in modal_states and tr['from_state'] not in modal_states:
            failure = ('modal_not_handled', i)
            failure_step = i
            break
        # 5. post-action visible evidence is the proof the click landed.
        if _event_evidence(ev) != tr['evidence']:
            failure = ('visible_evidence_mismatch', i)
            failure_step = i
            break
        if not ev.get('frame_id'):
            failure = ('frame_missing', i)
            failure_step = i
            break
        window_after = ev.get('window_after')
        if window_after != tr['to_state']:
            failure = ('window_state_mismatch', i)
            failure_step = i
            break
        step_evidence.append({
            'seq': i, 'transition': tid,
            'window_before': window_before, 'window_after': window_after,
            'button_text': ev.get('button_text'),
            'visible_evidence': _event_evidence(ev),
            'frame_id': ev['frame_id'],
            'modal_open_after': window_after in modal_states,
            'irreversible': bool(tr.get('irreversible')),
            'mutates': bool(tr.get('mutates')),
            'verified': True,
        })
        current = window_after

    extra_frames = len(events) - len(planned)
    paused_early = failure is None and len(step_evidence) < len(planned) \
        and current == 'export_failure_dialog'
    if failure is None and extra_frames < 0 and not paused_early:
        failure = ('frame_missing', len(planned) + extra_frames)
        failure_step = len(planned) + extra_frames
    elif failure is None and extra_frames > 0:
        # More frames than the planned path: the recording does not match the
        # contracted sequence, so it cannot be used as proof of that path.
        failure = ('unexpected_extra_frame', len(planned))
        failure_step = len(planned)

    # Target verdicts: only credited when the exact transition was replayed
    # with its modal window and evidence actually observed.
    target_verdicts = {}
    verified_transitions = {s['transition'] for s in step_evidence}
    verified_modals = {s['window_before'] for s in step_evidence} | \
                      {s['window_after'] for s in step_evidence}
    for tg in graph['targets']:
        if tg['transition'] not in verified_transitions:
            continue
        ok = True
        note = 'transition_and_evidence_replayed'
        if tg.get('required_modal') and tg['required_modal'] not in verified_modals:
            ok = False
            note = 'publish_confirm_missing'
        target_verdicts[tg['id']] = {'covered': ok, 'reason': note,
                                     'case': str(recording['case'])}

    paused = current == 'export_failure_dialog' and failure is None
    resume_checkpoint = None
    if paused:
        # The failure branch itself is a valid checkpoint; the case can resume.
        resume_checkpoint = {
            'case': str(recording['case']),
            'at_step': len(step_evidence),
            'window': current,
            'unreached_transitions': [s['transition']
                                      for s in planned[len(step_evidence):]],
        }

    if failure is not None:
        reason, seq = failure
        status = '退回'
        completed = False
    elif paused:
        status = '待续跑'
        reason = 'paused_at_export_failure_dialog'
        completed = False
    elif len(step_evidence) != len(planned) or extra_frames != 0:
        status, reason, completed = '退回', 'frame_missing', False
    else:
        claimed = {t: v for t, v in target_verdicts.items() if not v['covered']}
        if claimed:
            status, reason, completed = '退回', 'publish_confirm_missing', True
        else:
            status, reason, completed = '通过', 'state_path_replayed', True

    return {
        'status': status, 'reason': reason,
        'failure_step': failure_step,
        'steps': step_evidence,
        'end_window': current,
        'target_verdicts': target_verdicts,
        'replay_completed': completed,
        'resume_checkpoint': resume_checkpoint,
        'recorded_passed': bool(recording.get('passed')),
    }


def _resume_replay(recording, prior, suite, graph):
    """Continue a case paused at the export-failure dialog from its checkpoint."""
    if str(recording.get('resume_execution')) != str(prior['id']):
        return None
    if prior['body'].get('status') != '待续跑':
        return None
    checkpoint = prior['body'].get('resume_checkpoint')
    if not checkpoint:
        return None
    script = next(s for s in suite['body']['scripts']
                  if s['case'] == str(recording['case']))
    if checkpoint['case'] != str(recording['case']):
        return None
    edges, by_button, modal_states, terminals = _index_graph(graph)
    current = checkpoint['window']
    offset = checkpoint['at_step']
    events = recording.get('events', [])
    planned = script['steps'][offset:]
    steps = list(prior['body']['steps'])
    failure = None
    failure_step = None
    for j, tr_plan in enumerate(planned):
        if j >= len(events):
            failure = ('frame_missing', offset + j)
            failure_step = offset + j
            break
        ev = events[j]
        tid = str(ev.get('transition', ''))
        if tid != tr_plan['transition']:
            failure = ('transition_mismatch', offset + j)
            failure_step = offset + j
            break
        tr = edges[tid]
        if ev.get('window_before') != current or ev.get('window_before') != tr['from_state']:
            failure = ('window_state_mismatch', offset + j)
            failure_step = offset + j
            break
        if current in modal_states and tr['from_state'] not in modal_states:
            failure = ('modal_not_handled', offset + j)
            failure_step = offset + j
            break
        if _event_evidence(ev) != tr['evidence'] or not ev.get('frame_id'):
            failure = ('visible_evidence_mismatch' if ev.get('frame_id') else 'frame_missing',
                       offset + j)
            failure_step = offset + j
            break
        if ev.get('window_after') != tr['to_state']:
            failure = ('window_state_mismatch', offset + j)
            failure_step = offset + j
            break
        steps.append({'seq': offset + j, 'transition': tid,
                      'window_before': ev['window_before'],
                      'window_after': ev['window_after'],
                      'button_text': ev.get('button_text'),
                      'visible_evidence': _event_evidence(ev),
                      'frame_id': ev['frame_id'],
                      'modal_open_after': ev['window_after'] in modal_states,
                      'irreversible': bool(tr.get('irreversible')),
                      'mutates': bool(tr.get('mutates')),
                      'verified': True})
        current = ev['window_after']

    if failure is None and len(events) != len(planned):
        failure = (('frame_missing' if len(events) < len(planned)
                    else 'unexpected_extra_frame'),
                   offset + min(len(events), len(planned)))
        failure_step = failure[1]

    target_verdicts = {}
    verified_transitions = {s['transition'] for s in steps}
    verified_modals = {s['window_before'] for s in steps} | \
                      {s['window_after'] for s in steps}
    for tg in graph['targets']:
        if tg['transition'] in verified_transitions:
            ok = not (tg.get('required_modal') and
                      tg['required_modal'] not in verified_modals)
            target_verdicts[tg['id']] = {
                'covered': ok,
                'reason': 'transition_and_evidence_replayed' if ok else 'publish_confirm_missing',
                'case': str(recording['case'])}
    if failure is not None:
        reason, seq = failure
        return {'status': '退回', 'reason': reason, 'failure_step': seq,
                'steps': steps, 'end_window': current,
                'target_verdicts': target_verdicts, 'replay_completed': False,
                'resume_checkpoint': None, 'recorded_passed': bool(recording.get('passed')),
                'resumed_from': prior['id']}
    return {'status': '通过', 'reason': 'resumed_state_path_replayed',
            'failure_step': None, 'steps': steps, 'end_window': current,
            'target_verdicts': target_verdicts, 'replay_completed': True,
            'resume_checkpoint': None, 'recorded_passed': bool(recording.get('passed')),
            'resumed_from': prior['id']}


def summarize(path, file):
    source, payload = capture(path, 'desktop_qa_replay', file)
    db = connect(path)
    results = []
    try:
        with db:
            graph_row = db.execute(
                'SELECT * FROM ui_state_graph ORDER BY id LIMIT 1').fetchone()
            graph = json.loads(graph_row['body']) if graph_row else None
            for row in payload.get('rows', []):
                key = str(row['id'])
                suite = load(db, 'qa_suite', str(row['suite']))
                if not suite:
                    save(db, 'qa_execution', key, source, payload['version'], row,
                         '待核', 'suite_missing')
                    results.append(decide(db, '桌面回归执行', key, '待核',
                                          'suite_missing', source))
                    continue
                if graph is None:
                    save(db, 'qa_execution', key, source, payload['version'], row,
                         '待核', 'state_graph_missing')
                    results.append(decide(db, '桌面回归执行', key, '待核',
                                          'state_graph_missing', source))
                    continue

                prior = load(db, 'qa_execution', key)
                # Same execution log replay is idempotent: the captured source
                # hash identifies the identical recording, so no second copy
                # is burned and the stored verdict is returned verbatim.
                if prior and prior['source_id'] == source:
                    stored = dict(prior['body'])
                    stored.update({'status': prior['status'], 'reason': prior['reason'],
                                   'replayed': True, 'workspace_copy_consumed': False})
                    results.append(decide(db, '桌面回归执行', key, prior['status'],
                                          prior['reason'], source))
                    results[-1].update(stored)
                    continue

                resume_id = row.get('resume_execution')
                if resume_id:
                    prior_run = load(db, 'qa_execution', str(resume_id))
                    verdict = _resume_replay(row, prior_run, suite, graph) \
                        if prior_run else None
                    if verdict is None:
                        save(db, 'qa_execution', key, source, payload['version'], row,
                             '退回', 'invalid_resume_checkpoint')
                        results.append(decide(db, '桌面回归执行', key, '退回',
                                              'invalid_resume_checkpoint', source))
                        continue
                else:
                    verdict = replay(row, suite, graph)

                script = next((s for s in suite['body']['scripts']
                               if s['case'] == str(row['case'])), None)
                workspace = script['workspace'] if script else None
                copy_consumed = False
                if workspace:
                    ws = load(db, 'ui_workspace', workspace)
                    if ws:
                        consumed = list(ws['body'].get('consumed_by', []))
                        irreversible_done = any(
                            e.get('irreversible') for e in verdict['steps'])
                        already_burned = (key in consumed or
                                          ws['body'].get('state') == 'consumed')
                        if already_burned:
                            # The copy was already burned. An irreversible
                            # publish is never executed a second time; the run
                            # is returned so it cannot masquerade as new proof.
                            if irreversible_done:
                                verdict['status'] = '退回'
                                verdict['reason'] = 'irreversible_publish_not_re_run'
                                verdict['replay_completed'] = False
                        else:
                            consumed.append(key)
                            replace(db, 'ui_workspace', workspace, source,
                                    payload['version'],
                                    {**ws['body'], 'consumed_by': consumed,
                                     'state': 'consumed'},
                                    '已消耗', 'copy_consumed_by_execution')
                            copy_consumed = True

                body = {**row, **verdict, 'workspace': workspace,
                        'workspace_copy_consumed': copy_consumed}
                save(db, 'qa_execution', key, source, payload['version'], body,
                     verdict['status'], verdict['reason'])
                out = decide(db, '桌面回归执行', key, verdict['status'],
                             verdict['reason'], source)
                out.update(body)
                results.append(out)
            return results
    finally:
        db.close()
