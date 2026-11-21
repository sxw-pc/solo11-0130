"""职责分离核证测试：自然人/版本核证、两种权限合同、时钟偏差与证据变更。"""
import json

import pytest

from subtitlehandoff import access_audit, incident_review, report
from subtitlehandoff.store import connect


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def hr(rows):
    return {'version': 'v1', 'kind': 'hr_directory', 'rows': rows}


def revisions(rows):
    return {'version': 'v1', 'kind': 'project_revision', 'rows': rows}


def receipts(rows):
    return {'version': 'v1', 'kind': 'approval_receipt', 'rows': rows}


def contracts(rows):
    return {'version': 'v1', 'kind': 'permission_contract', 'rows': rows}


def windows(rows):
    return {'version': 'v1', 'kind': 'emergency_window', 'rows': rows}


def traces(rows):
    return {'version': 'v1', 'kind': 'access_trace', 'rows': rows}


def load_all(db, root, payloads):
    for i, payload in enumerate(payloads):
        access_audit.import_actions(db, write(root, f'p{i}.json', payload))


def trace_row(tid, project, events, skew=120):
    return {'id': tid, 'project': project, 'clock_skew_seconds': skew,
            'events': events}


def login(eid, account, device, at):
    return {'id': eid, 'kind': 'login', 'account': account, 'device': device, 'at': at}


def logout(eid, account, device, at):
    return {'id': eid, 'kind': 'logout', 'account': account, 'device': device, 'at': at}


def edit(eid, account, device, at, revision):
    return {'id': eid, 'kind': 'edit', 'account': account, 'device': device,
            'at': at, 'revision': revision}


def export_ev(eid, account, device, at, revision, h='HASH7'):
    return {'id': eid, 'kind': 'export', 'account': account, 'device': device,
            'at': at, 'revision': revision, 'content_hash': h}


def r7_receipt(rid, project, approver, revision='r7', h='HASH7',
               at='2026-03-01T09:10:00'):
    return {'id': rid, 'project': project, 'receipt': rid, 'revision': revision,
            'content_hash': h, 'approver': approver, 'at': at}


def std_contract(cid, project, regime):
    return {'id': cid, 'project': project, 'regime': regime,
            'effective_at': '2026-02-01T00:00:00'}


def std_revisions(project):
    return [
        {'id': f'{project}-r6', 'project': project, 'revision': 'r6',
         'editor': 'E1', 'parent': []},
        {'id': f'{project}-r7', 'project': project, 'revision': 'r7',
         'editor': 'E1', 'parent': ['r6']},
    ]


def solo_trace(tid, project, account='E1', device='D-01',
               events_extra=None, skew=120):
    events = [login('l1', account, device, '2026-03-01T08:00:00'),
              edit('ed1', account, device, '2026-03-01T08:30:00', 'r7'),
              export_ev('x1', account, device, '2026-03-01T10:00:00', 'r7'),
              logout('lo1', account, device, '2026-03-01T10:30:00')]
    if events_extra:
        events = events_extra
    return trace_row(tid, project, events, skew)


def latest_status(db, tid):
    row = connect(db).execute(
        'SELECT status,reason FROM audit_evaluation WHERE trace_id=? ORDER BY seq DESC LIMIT 1',
        (tid,)).fetchone()
    return row['status'], row['reason']


def evaluation(db, tid):
    row = connect(db).execute(
        'SELECT * FROM audit_evaluation WHERE trace_id=? ORDER BY seq DESC LIMIT 1',
        (tid,)).fetchone()
    return json.loads(row['body'])


# ---------------------------------------------------------------- 验收主场景
def test_same_natural_person_two_accounts_regulated_is_confirmed_violation(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    status, reason = latest_status(db, 'T1')
    assert status == '确定违规'
    body = evaluation(db, 'T1')
    assert body['possible_chains'] == 1
    assert any(w['code'] == 'self_approval' for w in body['witnesses'])

    # reviewer 不能改变结论；案件直接立案
    result = incident_review.review(
        db, write(tmp_path, 'rv.json',
                  {'version': 'v1', 'rows': [{'id': 'I1', 'trace': 'T1',
                                              'reviewer': 'audit1'}]}))
    assert result[0]['status'] == '已立案'
    assert result[0]['reason'] == 'duty_violation_confirmed'


def test_drill_emergency_window_with_timely_cosign_closes_compliant(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'},
            {'id': 'H3', 'account': 'E3', 'person': 'yi'}]),
        revisions(std_revisions('P2')),
        receipts([r7_receipt('A7D', 'P2', 'E2')]),
        contracts([std_contract('C2', 'P2', 'drill')]),
        windows([{'id': 'W1', 'project': 'P2',
                  'start': '2026-03-01T08:00:00', 'end': '2026-03-01T12:00:00',
                  'reason': 'oncall_urgent_fix'}]),
        traces([solo_trace('T2', 'P2', device='D-02')]),
        receipts([{'id': 'G1', 'kind': 'emergency_cosign', 'project': 'P2',
                   'window': 'W1', 'person': 'E3',
                   'at': '2026-03-01T22:00:00'}]),  # 12 小时后补签
    ])
    assert latest_status(db, 'T2') == ('已排除', 'separate_duty_chain_verified')
    result = incident_review.review(
        db, write(tmp_path, 'rv.json',
                  {'version': 'v1', 'rows': [{'id': 'I2', 'trace': 'T2'}]}))
    assert result[0]['status'] == '已关闭'


def test_drill_without_cosign_is_pending_then_violation_after_window(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P3')),
        receipts([r7_receipt('A7P3', 'P3', 'E2')]),
        contracts([std_contract('C3', 'P3', 'drill')]),
        windows([{'id': 'W3', 'project': 'P3',
                  'start': '2026-03-01T08:00:00', 'end': '2026-03-01T12:00:00',
                  'reason': 'oncall_urgent_fix'}]),
        traces([solo_trace('T3', 'P3', device='D-03')]),
    ])
    status, _ = latest_status(db, 'T3')
    assert status == '待调查'
    body = evaluation(db, 'T3')
    assert any(m['type'] == 'emergency_cosign' for m in body['missing_evidence'])

    # 24 小时窗口（含偏差）过后仍无补签 -> 确定违规
    load_all(db, tmp_path, [
        traces([solo_trace('T3', 'P3', device='D-03')]),  # 重放，不产生新版本
        receipts([{'id': 'LATE', 'kind': 'emergency_cosign', 'project': 'P3',
                   'window': 'W3', 'person': 'E2',
                   'at': '2026-03-02T11:00:00'}]),  # 25 小时后且仍是同一人
    ])
    assert latest_status(db, 'T3')[0] == '确定违规'


# ---------------------------------------------------------------- 身份不确定性
def test_missing_hr_keeps_all_chains_and_marks_pending(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),  # E1 与 E2 归属未知
    ])
    status, reason = latest_status(db, 'T1')
    assert status == '待调查'
    body = evaluation(db, 'T1')
    # 两账号：划分有 2 种（同人=违规 / 两人=合规）
    assert body['possible_chains'] == 2
    assert body['violating_chains'] == 1 and body['compliant_chains'] == 1
    assert any(m['type'] == 'account_ownership'
               and m['account'] in ('E1', 'E2') for m in body['missing_evidence'])


def test_hr_supplement_changes_pending_to_confirmed_violation(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    assert latest_status(db, 'T1')[0] == '待调查'
    # 先按待调查建过案并填写 reviewer
    first = incident_review.review(
        db, write(tmp_path, 'rv1.json',
                  {'version': 'v1', 'rows': [{'id': 'I1', 'trace': 'T1',
                                              'reviewer': 'audit1'}]}))
    assert first[0]['status'] == '待补证'
    # 补人事：E1、E2 同属甲
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
    ])
    assert latest_status(db, 'T1') == ('确定违规', 'self_approval')
    second = incident_review.review(
        db, write(tmp_path, 'rv2.json',
                  {'version': 'v2', 'rows': [{'id': 'I1', 'trace': 'T1',
                                              'reviewer': 'audit1'}]}))
    # 早先填写 reviewer 不能维持旧结论
    assert second[0]['status'] == '已立案'
    conn = connect(db)
    changes = [dict(r) for r in conn.execute(
        "SELECT * FROM evidence_change WHERE trace_id='T1' ORDER BY id")]
    assert any(c['old_status'] == '待调查' and c['new_status'] == '确定违规'
               for c in changes)
    # 评估历史逐版保留
    seqs = [r['seq'] for r in conn.execute(
        'SELECT DISTINCT seq FROM audit_evaluation WHERE trace_id=? ORDER BY seq',
        ('T1',))]
    assert seqs == [1, 2]


def test_genuine_second_natural_person_is_excluded(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    assert latest_status(db, 'T1') == ('已排除', 'separate_duty_chain_verified')
    result = incident_review.review(
        db, write(tmp_path, 'rv.json',
                  {'version': 'v1', 'rows': [{'id': 'I1', 'trace': 'T1'}]}))
    assert result[0]['status'] == '已关闭'


# ---------------------------------------------------------------- 版本核证
def test_approval_of_r6_cannot_release_r7(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A6', 'P1', 'E2', revision='r6', h='HASH6')]),  # 只批了 r6
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),  # 导出 r7
    ])
    status, reason = latest_status(db, 'T1')
    assert status == '确定违规'
    body = evaluation(db, 'T1')
    assert any(w['code'] == 'stale_revision_approval' for w in body['witnesses'])
    assert body['export_outcomes'][0]['receipts'][0]['state'] == 'stale'


def test_branch_merge_requires_approval_of_merged_content(tmp_path):
    db = tmp_path / 'ledger.db'
    # r7 是 r6a 与 r6b 两个编辑分支的合并版
    revs = [
        {'id': 'R6A', 'project': 'P1', 'revision': 'r6a', 'editor': 'E1', 'parent': []},
        {'id': 'R6B', 'project': 'P1', 'revision': 'r6b', 'editor': 'E3', 'parent': []},
        {'id': 'R7', 'project': 'P1', 'revision': 'r7', 'editor': 'E1',
         'parent': ['r6a', 'r6b']},
    ]
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'},
            {'id': 'H3', 'account': 'E3', 'person': 'bing'}]),
        revisions(revs),
        receipts([r7_receipt('AOLD', 'P1', 'E2', revision='r6a', h='HASH6A')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    body = evaluation(db, 'T1')
    assert body['status'] == '确定违规'
    assert any(w['code'] in ('stale_revision_approval',
                             'merge_or_old_branch_signature')
               for w in body['witnesses'])
    # 补上针对合并版 r7 的第二人批准后排除
    load_all(db, tmp_path, [receipts([r7_receipt('AMERGE', 'P1', 'E2')])])
    assert latest_status(db, 'T1')[0] == '已排除'


# ---------------------------------------------------------------- 时钟偏差与遗留窗口
def test_clock_skew_keeps_logout_window_action_attributable_but_far_action_orphan(tmp_path):
    db = tmp_path / 'ledger.db'
    events = [
        login('l1', 'E1', 'D-01', '2026-03-01T08:00:00'),
        edit('ed1', 'E1', 'D-01', '2026-03-01T08:30:00', 'r7'),
        logout('lo1', 'E1', 'D-01', '2026-03-01T09:00:00'),
        # 偏差窗口（120 秒）内的遗留动作仍可归属
        export_ev('x-near', 'E1', 'D-01', '2026-03-01T09:01:30', 'r7'),
    ]
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2', at='2026-03-01T08:55:00')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([trace_row('T1', 'P1', events, skew=120)]),
    ])
    body = evaluation(db, 'T1')
    assert body['attribution']['x-near']['orphan'] is False
    assert latest_status(db, 'T1')[0] == '已排除'

    # 超出偏差窗口 5 分钟的动作成为无主动作，不能算在 E1 名下
    events_far = events[:-1] + [
        export_ev('x-far', 'E1', 'D-01', '2026-03-01T09:05:00', 'r7')]
    db2 = tmp_path / 'ledger2.db'
    load_all(db2, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2', at='2026-03-01T08:55:00')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([trace_row('T1', 'P1', events_far, skew=120)]),
    ])
    body2 = evaluation(db2, 'T1')
    assert body2['attribution']['x-far']['orphan'] is True
    assert body2['attribution']['x-far']['person'] is None
    assert latest_status(db2, 'T1')[0] == '待调查'


def test_ambiguous_approval_export_ordering_requests_clock_evidence(tmp_path):
    db = tmp_path / 'ledger.db'
    # 批准 09:59:30、导出 10:00:00，相差 30 秒落在偏差 120 秒窗口 -> 先后不明
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2', at='2026-03-01T09:59:30')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    body = evaluation(db, 'T1')
    assert any(m['type'] == 'clock_ordering' for m in body['missing_evidence'])
    # 时序不明时仍保留相容链：有合规可能，不能直接定违规
    assert body['compliant_chains'] >= 1
    assert latest_status(db, 'T1')[0] == '待调查'


# ---------------------------------------------------------------- 撤销与重放
def test_approval_revocation_before_export_invalidates_signature(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    assert latest_status(db, 'T1')[0] == '已排除'
    # 导出前撤销该回执
    load_all(db, tmp_path, [{
        'version': 'v1', 'kind': 'approval_revocation', 'rows': [
            {'id': 'V1', 'project': 'P1', 'receipt': 'A7',
             'at': '2026-03-01T09:40:00', 'reason': 'approver_leave'}]}])
    status, reason = latest_status(db, 'T1')
    assert status == '确定违规'
    assert any(w['code'] == 'revoked_approval_used'
               for w in evaluation(db, 'T1')['witnesses'])


def test_duplicate_events_dedup_and_conflicting_bodies_flagged(tmp_path):
    db = tmp_path / 'ledger.db'
    base = solo_trace('T1', 'P1')
    dup = dict(base)
    dup['events'] = base['events'] + [dict(base['events'][2])]  # 完全重复导出事件
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([dup]),
    ])
    body = evaluation(db, 'T1')
    assert any(n['code'] == 'duplicate_event_ignored' for n in body['notes'])
    assert latest_status(db, 'T1')[0] == '已排除'

    # 同事件 id 不同正文 -> 证据冲突，事件被剔除
    conflict = dict(base)
    changed = dict(base['events'][2], content_hash='OTHER-HASH')
    conflict['events'] = base['events'] + [changed]
    db2 = tmp_path / 'ledger2.db'
    load_all(db2, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([conflict]),
    ])
    assert latest_status(db2, 'T1')[0] == '待调查'
    assert any(n['code'] == 'event_body_conflict'
               for n in evaluation(db2, 'T1')['notes'])


def test_replay_same_evidence_does_not_create_new_evaluation_version(tmp_path):
    db = tmp_path / 'ledger.db'
    payloads = [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'yi'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ]
    load_all(db, tmp_path, payloads)
    seqs_before = [r['seq'] for r in connect(db).execute(
        'SELECT seq FROM audit_evaluation WHERE trace_id=?', ('T1',))]
    # 同文件重放（capture 按哈希识别同一来源）
    for i, payload in enumerate(payloads):
        access_audit.import_actions(db, write(tmp_path, f'rep{i}.json', payload))
    seqs_after = [r['seq'] for r in connect(db).execute(
        'SELECT seq FROM audit_evaluation WHERE trace_id=?', ('T1',))]
    assert seqs_before == seqs_after == [1]


# ---------------------------------------------------------------- 紧急授权与合同固定
def test_expired_emergency_window_does_not_authorize_self_approval(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P3')),
        receipts([r7_receipt('A7P3', 'P3', 'E2')]),
        contracts([std_contract('C3', 'P3', 'drill')]),
        windows([{'id': 'W3', 'project': 'P3',
                  'start': '2026-03-01T05:00:00', 'end': '2026-03-01T07:00:00',
                  'reason': 'oncall_urgent_fix'}]),  # 导出 10:00 时已过期
        traces([solo_trace('T3', 'P3', device='D-03')]),
    ])
    assert latest_status(db, 'T3')[0] == '确定违规'
    assert any(n['code'] == 'emergency_window_expired'
               for n in evaluation(db, 'T3')['notes'])


def test_emergency_window_without_reason_is_not_authorization(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P3')),
        receipts([r7_receipt('A7P3', 'P3', 'E2')]),
        contracts([std_contract('C3', 'P3', 'drill')]),
        windows([{'id': 'W3', 'project': 'P3',
                  'start': '2026-03-01T08:00:00', 'end': '2026-03-01T12:00:00'}]),
        traces([solo_trace('T3', 'P3', device='D-03')]),
    ])
    assert latest_status(db, 'T3')[0] == '确定违规'
    assert any(n['code'] == 'emergency_reason_missing'
               for n in evaluation(db, 'T3')['notes'])


def test_contract_fixed_after_export_cannot_retrofit_regime(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    assert latest_status(db, 'T1') == ('待调查', 'permission_contract_missing')
    # 导出之后才固定合同 -> 不能追溯改变类别
    load_all(db, tmp_path, [contracts([
        {'id': 'C1LATE', 'project': 'P1', 'regime': 'drill',
         'effective_at': '2026-03-01T11:00:00'}])])
    assert latest_status(db, 'T1') == ('待调查', 'permission_contract_missing')


# ---------------------------------------------------------------- 报告留痕
def test_report_records_witnesses_and_immutable_snapshots(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}]),
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A6', 'P1', 'E2', revision='r6', h='HASH6')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    out1 = report.build(db, tmp_path / 'report.json')
    data1 = json.loads(out1.read_text(encoding='utf-8'))
    duty = next(d for d in data1['duty_audit'] if d['trace_id'] == 'T1')
    assert duty['status'] == '确定违规'
    assert any(w['code'] == 'stale_revision_approval'
               for w in duty['violation_witnesses'])
    first_text = out1.read_text(encoding='utf-8')

    # 新证据改变结论后重新出报告：原签出文件不被原地改写
    load_all(db, tmp_path, [
        hr([{'id': 'H3', 'account': 'E2', 'person': 'yi'}]),  # 新归属版本：与 E1 不同人
        receipts([r7_receipt('A7', 'P1', 'E2')]),
    ])
    out2 = report.build(db, tmp_path / 'report.json')
    assert out1.read_text(encoding='utf-8') == first_text  # 原报告原样保留
    assert out2 != out1 and out2.exists()
    data2 = json.loads(out2.read_text(encoding='utf-8'))
    assert any(d['trace_id'] == 'T1' and d['status'] == '已排除'
               for d in data2['duty_audit'])


def test_report_pending_case_lists_minimal_missing_evidence(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    duty = next(d for d in data['duty_audit'] if d['trace_id'] == 'T1')
    assert duty['status'] == '待调查'
    assert duty['minimal_missing_evidence']
    assert all('detail' in m for m in duty['minimal_missing_evidence'])


def test_review_without_evaluation_is_held_not_opened(tmp_path):
    db = tmp_path / 'ledger.db'
    result = incident_review.review(
        db, write(tmp_path, 'rv.json',
                  {'version': 'v1', 'rows': [{'id': 'I9', 'trace': 'GHOST',
                                              'reviewer': 'audit1'}]}))
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'trace_evaluation_missing'


def test_case_history_keeps_every_evidence_version(tmp_path):
    db = tmp_path / 'ledger.db'
    load_all(db, tmp_path, [
        revisions(std_revisions('P1')),
        receipts([r7_receipt('A7', 'P1', 'E2')]),
        contracts([std_contract('C1', 'P1', 'regulated')]),
        traces([solo_trace('T1', 'P1')]),
    ])
    incident_review.review(db, write(tmp_path, 'rv1.json', {
        'version': 'v1', 'rows': [{'id': 'I1', 'trace': 'T1',
                                   'reviewer': 'audit1'}]}))
    load_all(db, tmp_path, [
        hr([{'id': 'H1', 'account': 'E1', 'person': 'jia'},
            {'id': 'H2', 'account': 'E2', 'person': 'jia'}])])
    incident_review.review(db, write(tmp_path, 'rv2.json', {
        'version': 'v2', 'rows': [{'id': 'I1', 'trace': 'T1',
                                   'reviewer': 'audit1'}]}))
    conn = connect(db)
    case = conn.execute("SELECT body FROM incident_case WHERE id='I1'").fetchone()
    body = json.loads(case['body'])
    assert body['possible_chains'] == 1
    assert body['violating_chains'] == 1
    history = [dict(r) for r in conn.execute(
        'SELECT status,reason FROM audit_evaluation WHERE trace_id=? ORDER BY seq',
        ('T1',))]
    assert [h['status'] for h in history] == ['待调查', '确定违规']
