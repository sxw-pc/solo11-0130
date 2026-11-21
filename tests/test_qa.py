import json
import copy
from pathlib import Path

import pytest

from subtitlehandoff import qa_plan, qa_execute, report
from subtitlehandoff.store import connect, load

SAMPLES = Path(__file__).resolve().parent.parent / 'samples'


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def load_json(name):
    return json.loads((SAMPLES / name).read_text(encoding='utf-8'))


# --------------------------------------------------------------------------- #
# Fixtures: build the canonical plan payload inline for full control
# --------------------------------------------------------------------------- #
@pytest.fixture
def smoke_payload():
    return load_json('qa_plan_smoke.json')


def plan_in(root, db_name, payload):
    return qa_plan.plan(root / db_name, write(root, f'{db_name}.json', payload))


def execute_in(root, db_name, payload):
    return qa_execute.summarize(root / db_name,
                                write(root, f'{db_name}-replay.json', payload))


# --------------------------------------------------------------------------- #
# Planning: graph caps, legal state paths, labels never trusted
# --------------------------------------------------------------------------- #
def test_graph_state_and_transition_caps_are_enforced(tmp_path, smoke_payload):
    too_many = copy.deepcopy(smoke_payload)
    states = too_many['graph']['states']
    # Six baseline states plus five more reach the eleven-state ceiling.
    for i in range(5):
        states.append({'id': f'extra-{i}', 'modal': False, 'terminal': False})
    with pytest.raises(ValueError, match='state_cap_exceeded'):
        qa_plan.plan(tmp_path / 'db', write(tmp_path, 'x.json', too_many))

    too_many_t = copy.deepcopy(smoke_payload)
    edges = too_many_t['graph']['transitions']
    base = copy.deepcopy(edges[-1])
    # Twelve baseline transitions plus one more exceeds the ceiling.
    edges.append({**base, 'id': 'one-too-many'})
    with pytest.raises(ValueError, match='transition_cap_exceeded'):
        qa_plan.plan(tmp_path / 'db2', write(tmp_path, 'y.json', too_many_t))


def test_target_cap_enforced(tmp_path, smoke_payload):
    p = copy.deepcopy(smoke_payload)
    p['graph']['targets'].append({'id': 'T4', 'transition': 'save_again'})
    with pytest.raises(ValueError, match='target_cap_exceeded'):
        qa_plan.plan(tmp_path / 'db', write(tmp_path, 'z.json', p))


def test_illegal_modal_transition_case_is_rejected(tmp_path, smoke_payload):
    out = plan_in(tmp_path, 'db', smoke_payload)
    rejected = {r['case']: r['reason'] for r in out['rejected_cases']}
    # open_export is not legal while the publish-confirm modal is on screen.
    assert rejected['MODAL-VIOLATION'] == 'illegal_transition_for_window'


def test_coverage_labels_are_not_trusted(tmp_path, smoke_payload):
    plan_in(tmp_path, 'db', smoke_payload)
    db = connect(tmp_path / 'db')
    case = load(db, 'ui_case', 'LABEL-INFLATED')
    # The case only walks save_draft, so T2/T3 labels are recorded as overclaim.
    assert case['body']['walk']['ok'] is True
    row = case['body']
    assert set(row['coverage_labels']) == {'T1', 'T2', 'T3'}
    db.close()


def test_unknown_start_snapshot_state_rejected(tmp_path, smoke_payload):
    p = copy.deepcopy(smoke_payload)
    p['cases'].append({'id': 'BAD-START', 'start_snapshot': 'snap-clean',
                       'start_snapshot_state': 'no-such-window',
                       'steps': [{'transition': 'save_draft'}]})
    out = plan_in(tmp_path, 'db', p)
    rejected = {r['case']: r['reason'] for r in out['rejected_cases']}
    assert rejected['BAD-START'] == 'unknown_start_snapshot'


def test_target_with_no_legal_carrier_is_reported_not_claimed(tmp_path, smoke_payload):
    p = copy.deepcopy(smoke_payload)
    # Drop every case capable of walking the export-failure branch (T3).
    p['cases'] = [c for c in p['cases'] if c['id'] in ('SMOKE-CHAIN', 'DRAFT-ONLY')]
    out = plan_in(tmp_path, 'db', p)
    blocker = next(u for u in out['uncovered_targets'] if u['target'] == 'T3')
    assert blocker['blocker'] == 'no_legal_path_from_start_snapshot'
    assert 'T3' not in out['covered_targets']


def test_case_ending_inside_unhandled_modal_is_rejected(tmp_path, smoke_payload):
    p = copy.deepcopy(smoke_payload)
    # Open the progress dialog then stop without handling it.
    p['cases'].append({
        'id': 'ENDS-IN-MODAL', 'start_snapshot': 'snap-draft',
        'start_snapshot_state': 'editor_saved', 'coverage_labels': [],
        'steps': [{'transition': 'open_export'}]})
    out = plan_in(tmp_path, 'db', p)
    rejected = {r['case']: r['reason'] for r in out['rejected_cases']}
    assert rejected['ENDS-IN-MODAL'] == 'case_ends_inside_unhandled_modal'


# --------------------------------------------------------------------------- #
# Smoke contract: one chained workspace covers T1 and T2
# --------------------------------------------------------------------------- #
def test_smoke_contract_chains_draft_and_publish_on_one_copy(tmp_path, smoke_payload):
    out = plan_in(tmp_path, 'db', smoke_payload)
    assert out['status'] == '已规划'
    assert set(out['covered_targets']) == {'T1', 'T2', 'T3'}
    assert out['workspace_copies_used'] == 1
    chain = next(s for s in out['scripts'] if s['case'] == 'SMOKE-CHAIN')
    assert chain['workspace'] == 'ws-1'
    # The failure-branch case mutates nothing, so it consumes no copy.
    fail = next(s for s in out['scripts'] if s['case'] == 'FAIL-PAUSE')
    assert fail['workspace'] is None
    # Every transition carries an independently reviewable checkpoint.
    assert [c['transition'] for c in chain['checkpoints']] == \
           ['save_draft', 'open_export', 'export_succeeds', 'open_publish', 'confirm_publish']
    assert out['uncovered_targets'] == []


def test_failure_branch_that_mutates_needs_its_own_copy(tmp_path, smoke_payload):
    """A failure recovery that rewrites files cannot share the smoke copy."""
    p = copy.deepcopy(smoke_payload)
    p['cases'] = [c for c in p['cases'] if c['id'] in
                  ('SMOKE-CHAIN', 'MODAL-VIOLATION', 'LABEL-INFLATED')]
    # retry_then_export re-exports after failure and mutates the workspace.
    p['cases'].append({
        'id': 'FAIL-MUTATE', 'start_snapshot': 'snap-draft',
        'start_snapshot_state': 'editor_saved', 'coverage_labels': ['T3'],
        'steps': [{'transition': 'open_export'}, {'transition': 'export_fails'},
                  {'transition': 'retry_then_export'}]})
    # Only one copy exists, already reserved by the chained smoke path.
    out = plan_in(tmp_path, 'db', p)
    assert set(out['covered_targets']) == {'T1', 'T2'}
    blocker = out['uncovered_targets'][0]
    assert blocker['target'] == 'T3'
    assert blocker['blocker'] == 'workspace_copy_shortage'


def test_mutating_failure_path_consumes_a_second_copy_when_added(tmp_path, smoke_payload):
    """With a second copy the mutating failure recovery can run isolated."""
    p = copy.deepcopy(smoke_payload)
    p['suite_id'] = 'SMOKE-S2'
    p['limits']['workspace_copies'] = 2
    p['workspaces'].append({'id': 'ws-2', 'snapshot': 'snap-draft'})
    p['cases'] = [c for c in p['cases'] if c['id'] in
                  ('SMOKE-CHAIN', 'MODAL-VIOLATION', 'LABEL-INFLATED')]
    p['cases'].append({
        'id': 'FAIL-MUTATE', 'start_snapshot': 'snap-draft',
        'start_snapshot_state': 'editor_saved', 'coverage_labels': ['T3'],
        'steps': [{'transition': 'open_export'}, {'transition': 'export_fails'},
                  {'transition': 'retry_then_export'}]})
    out = plan_in(tmp_path, 'db', p)
    assert out['status'] == '已规划'
    assert set(out['covered_targets']) == {'T1', 'T2', 'T3'}
    assert out['workspace_copies_used'] == 2
    workspaces = {s['case']: s['workspace'] for s in out['scripts']}
    assert workspaces['SMOKE-CHAIN'] == 'ws-1'
    assert workspaces['FAIL-MUTATE'] == 'ws-2'


# --------------------------------------------------------------------------- #
# Audit contract: isolation and the fixed one-copy shortage
# --------------------------------------------------------------------------- #
def test_audit_one_copy_reports_shortage_without_claiming_coverage(tmp_path):
    payload = load_json('qa_plan_audit_one_copy.json')
    out = plan_in(tmp_path, 'db', payload)
    assert out['status'] == '资源不足'
    assert out['reason'] == 'audit_workspace_copy_shortage'
    assert out['scripts'] == []
    assert out['covered_targets'] == []
    targets = {u['target']: u for u in out['uncovered_targets']}
    assert set(targets) == {'T1', 'T2', 'T3'}
    assert all(u['copies_available'] == 1 for u in targets.values())


def test_audit_three_copies_generates_independent_scripts(tmp_path):
    payload = load_json('qa_plan_audit_three_copies.json')
    out = plan_in(tmp_path, 'db', payload)
    assert out['status'] == '已规划'
    assert set(out['covered_targets']) == {'T1', 'T2', 'T3'}
    # Each mutating/irreversible path is isolated on its own clean copy.
    workspaces = [s['workspace'] for s in out['scripts']]
    assert len(set(workspaces)) == 3
    assert out['workspace_copies_used'] == 3
    # No script is allowed to chain two targets under the audit contract.
    assert all(len(s['covered_targets']) <= 1 for s in out['scripts'])


def test_audit_replay_consumes_each_clean_copy_exactly_once(tmp_path):
    payload = load_json('qa_plan_audit_three_copies.json')
    plan = plan_in(tmp_path, 'db', payload)
    recs = []
    for script in plan['scripts']:
        events = [{
            'frame_id': f"{script['case']}-{st['seq']}",
            'transition': st['transition'],
            'window_before': st['window_before'],
            'window_after': st['window_after'],
            'button_text': st['button_text'],
            'visible_evidence': st['required_evidence']}
            for st in script['steps']]
        recs.append({'id': f"R-{script['case']}", 'suite': 'AUDIT-A3',
                     'case': script['case'], 'passed': True, 'events': events})
    out = execute_in(tmp_path, 'db',
                     {'version': 'v1', 'rows': recs})
    assert all(r['status'] == '通过' for r in out)
    assert all(r['workspace_copy_consumed'] is True for r in out)
    db = connect(tmp_path / 'db')
    for script in plan['scripts']:
        ws = load(db, 'ui_workspace', script['workspace'])
        assert ws['body']['state'] == 'consumed'
        assert ws['body']['consumed_by'] == [f"R-{script['case']}"]
    db.close()


def test_contract_is_fixed_before_recording_no_downgrade(tmp_path):
    payload = load_json('qa_plan_audit_one_copy.json')
    plan_in(tmp_path, 'db', payload)
    # Replaying a smoke-style chained recording must not create audit coverage.
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [r for r in replay['rows'] if r['id'] == 'EXEC-OK']
    replay['rows'][0]['suite'] = 'AUDIT-A1'
    out = qa_execute.summarize(tmp_path / 'db', write(tmp_path, 'r.json', replay))
    # The audit suite has no scripts (shortage), so the case cannot be credited.
    assert out[0]['reason'] == 'case_not_in_plan'
    assert out[0]['status'] == '退回'


# --------------------------------------------------------------------------- #
# Runtime budget drives the shortest feasible set
# --------------------------------------------------------------------------- #
def test_runtime_budget_can_block_a_target(tmp_path, smoke_payload):
    p = copy.deepcopy(smoke_payload)
    p['limits']['max_total_runtime_s'] = 1  # nothing legal runs that fast
    out = plan_in(tmp_path, 'db', p)
    assert out['status'] == '资源不足'
    assert all(u['blocker'] == 'runtime_budget_exceeded'
               for u in out['uncovered_targets'])


# --------------------------------------------------------------------------- #
# Execution replay: evidence, missing frames, duplicate buttons
# --------------------------------------------------------------------------- #
@pytest.fixture
def planned_smoke(tmp_path, smoke_payload):
    plan_in(tmp_path, 'ledger.db', smoke_payload)
    return tmp_path


def test_successful_chain_replays_state_path(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-OK')]
    out = execute_in(planned_smoke, 'ledger.db', replay)
    assert out[0]['status'] == '通过'
    assert out[0]['reason'] == 'state_path_replayed'
    assert len(out[0]['steps']) == 5
    assert set(out[0]['target_verdicts']) == {'T1', 'T2'}


def test_passed_true_without_publish_confirm_modal_is_returned(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-NO-CONFIRM')]
    out = execute_in(planned_smoke, 'ledger.db', replay)
    # recorded passed:true cannot rescue a click whose window was not the modal
    assert out[0]['recorded_passed'] is True
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'window_state_mismatch'
    assert out[0]['failure_step'] == 4


def test_missing_frame_is_not_a_path_proof(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-MISSING-FRAME')]
    out = execute_in(planned_smoke, 'ledger.db', replay)
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'frame_missing'
    # T2's publish transition was never replayed.
    assert 'T2' not in out[0]['target_verdicts']


def test_visible_evidence_mismatch_detects_wrong_dialog(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-WRONG-EVIDENCE')]
    out = execute_in(planned_smoke, 'ledger.db', replay)
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'visible_evidence_mismatch'


def test_duplicate_confirm_button_is_disambiguated_by_window(planned_smoke):
    """The same 确认 text closes the failure dialog and confirms publish."""
    db = connect(planned_smoke / 'ledger.db')
    graph = load(db, 'ui_state_graph', 'graph:desktop-export-3state')['body']
    db.close()
    confirm_edges = [t for t in graph['transitions'] if t['button_text'] == '确认']
    windows = {t['from_state'] for t in confirm_edges}
    assert windows == {'export_failure_dialog', 'publish_confirm_dialog'}


# --------------------------------------------------------------------------- #
# Failure branch: legal checkpoint resume, unreached high-risk transfer
# --------------------------------------------------------------------------- #
def test_failure_pause_allows_resume_from_checkpoint(planned_smoke):
    replay = load_json('qa_replay_failure_resume.json')
    first = execute_in(planned_smoke, 'ledger.db',
                       {'version': 'v1', 'rows': [replay['rows'][0]]})
    assert first[0]['status'] == '待续跑'
    cp = first[0]['resume_checkpoint']
    assert cp['window'] == 'export_failure_dialog'
    assert cp['at_step'] == 2
    # The planned path continues past the failure dialog; that continuation
    # is the unreached high-risk work the resume must complete.
    assert cp['unreached_transitions'] == ['acknowledge_failure']

    second = execute_in(planned_smoke, 'ledger.db',
                        {'version': 'v1', 'rows': [replay['rows'][1]]})
    assert second[0]['status'] == '通过'
    assert second[0]['reason'] == 'resumed_state_path_replayed'
    assert second[0]['resumed_from'] == 'EXEC-FAIL-PAUSE'


def test_resume_requires_a_real_checkpoint(planned_smoke):
    bad = {'version': 'v1', 'rows': [{
        'id': 'EXEC-BAD-RESUME', 'suite': 'SMOKE-S1', 'case': 'FAIL-PAUSE',
        'resume_execution': 'EXEC-NEVER-HAPPENED', 'passed': True,
        'events': [{'frame_id': 'q1', 'transition': 'acknowledge_failure',
                    'window_before': 'export_failure_dialog',
                    'window_after': 'editor_saved', 'button_text': '确认',
                    'visible_evidence': '失败弹窗关闭回到草稿'}]}]}
    out = execute_in(planned_smoke, 'ledger.db', bad)
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'invalid_resume_checkpoint'


def test_truncated_failure_recording_does_not_credit_high_risk_transfer(planned_smoke):
    short = {'version': 'v1', 'rows': [{
        'id': 'EXEC-SHORT', 'suite': 'SMOKE-S1', 'case': 'FAIL-PAUSE',
        'passed': True,
        'events': [{'frame_id': 'z1', 'transition': 'open_export',
                    'window_before': 'editor_saved',
                    'window_after': 'export_progress_dialog',
                    'button_text': '导出', 'visible_evidence': '导出进度条出现'}]}]}
    out = execute_in(planned_smoke, 'ledger.db', short)
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'frame_missing'
    # The export_fails high-risk transfer was never observed, so T3 is absent.
    assert 'T3' not in out[0]['target_verdicts']


def test_extra_frame_recording_is_rejected_not_counted_as_proof(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    row = next(r for r in replay['rows'] if r['id'] == 'EXEC-OK')
    row = copy.deepcopy(row)
    row['id'] = 'EXEC-EXTRA'
    # Duplicate the final frame: six events for a five-step path.
    row['events'].append(dict(row['events'][-1]))
    out = execute_in(planned_smoke, 'ledger.db',
                     {'version': 'v1', 'rows': [row]})
    assert out[0]['status'] == '退回'
    assert out[0]['reason'] == 'unexpected_extra_frame'


# --------------------------------------------------------------------------- #
# Workspace copies: one burn per run, irreversible publish never re-run,
# same-log replay is free
# --------------------------------------------------------------------------- #
def test_workspace_copy_consumed_once_and_replay_is_free(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-OK')]
    first = execute_in(planned_smoke, 'ledger.db', replay)
    assert first[0]['workspace_copy_consumed'] is True
    db = connect(planned_smoke / 'ledger.db')
    ws = load(db, 'ui_workspace', 'ws-1')
    assert ws['body']['state'] == 'consumed'
    assert ws['body']['consumed_by'] == ['EXEC-OK']
    db.close()

    # Replaying the identical log returns the stored verdict, burns no copy.
    second = execute_in(planned_smoke, 'ledger.db', replay)
    assert second[0]['replayed'] is True
    assert second[0]['workspace_copy_consumed'] is False
    db = connect(planned_smoke / 'ledger.db')
    ws = load(db, 'ui_workspace', 'ws-1')
    assert ws['body']['consumed_by'] == ['EXEC-OK']
    db.close()


def test_irreversible_publish_is_not_executed_again(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-OK')]
    execute_in(planned_smoke, 'ledger.db', replay)
    # A different execution id re-walking the same irreversible publish on the
    # already-consumed copy must be flagged, not silently run again.
    again = copy.deepcopy(replay)
    again['rows'][0]['id'] = 'EXEC-OK-DUP'
    out = execute_in(planned_smoke, 'ledger.db', again)
    assert out[0]['reason'] == 'irreversible_publish_not_re_run'
    assert out[0]['workspace_copy_consumed'] is False


def test_suite_missing_is_pending_not_passed(tmp_path):
    replay = {'version': 'v1', 'rows': [{'id': 'X', 'suite': 'GHOST',
              'case': 'C', 'passed': True, 'events': []}]}
    out = qa_execute.summarize(tmp_path / 'db', write(tmp_path, 'r.json', replay))
    assert out[0]['status'] == '待核'
    assert out[0]['reason'] == 'suite_missing'


# --------------------------------------------------------------------------- #
# Report: every coverage conclusion traces to a legal action
# --------------------------------------------------------------------------- #
def test_report_traces_coverage_to_legal_action_evidence(planned_smoke):
    replay = load_json('qa_replay_smoke.json')
    replay['rows'] = [next(r for r in replay['rows'] if r['id'] == 'EXEC-OK')]
    execute_in(planned_smoke, 'ledger.db', replay)
    out_path = report.build(planned_smoke / 'ledger.db', planned_smoke / 'report.json')
    data = json.loads(out_path.read_text(encoding='utf-8'))
    cov = data['qa_coverage']
    assert cov['graph_present'] is True
    assert cov['state_count'] <= 10 and cov['transition_count'] <= 12
    suite = next(s for s in cov['suites'] if s['suite'] == 'SMOKE-S1')
    by_target = {t['target']: t for t in suite['target_coverage']}
    t2 = by_target['T2']
    assert t2['conclusion'] == 'covered'
    proof = t2['legal_action_evidence']
    assert proof['action'] == 'confirm_publish'
    assert proof['window_before'] == 'publish_confirm_dialog'
    assert proof['visible_evidence'] == '发布成功横幅与只读标记'
    # T3 was planned but never executed: planned yet no legal-action proof.
    t3 = by_target['T3']
    assert t3['planned'] is True
    assert t3['conclusion'] == 'not_replayed'
    assert t3['legal_action_evidence'] is None


def test_full_sample_files_run_end_to_end(tmp_path):
    """The shipped offline JSON samples drive plan, replay and report directly."""
    db = tmp_path / 'ledger.db'
    plan = qa_plan.plan(db, SAMPLES / 'qa_plan_smoke.json')
    assert plan['status'] == '已规划'
    results = qa_execute.summarize(db, SAMPLES / 'qa_replay_smoke.json')
    statuses = {r['id']: r['status'] for r in results}
    assert statuses['EXEC-OK'] == '通过'
    assert statuses['EXEC-NO-CONFIRM'] == '退回'
    assert statuses['EXEC-MISSING-FRAME'] == '退回'
    assert statuses['EXEC-WRONG-EVIDENCE'] == '退回'
    report.build(db, tmp_path / 'report.json')
