"""职责分离核证：把桌面操作日志、审批回执、人事账号归属、设备登录与项目修订
连成可核查的自然人职责链。

结论三档：
- 确定违规：所有与已知身份/时序约束相容的职责链都违反分离要求；
- 待调查：仅部分链违规，或证据不足（列出最少还需核实的账号归属/时间证据）；
- 已排除：所有相容链均合规。

账号名不同不等于两个自然人；批准 r6 不能放行 r7；退出登录后超出时钟偏差
窗口的遗留动作不归属前一人。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from .ingest import capture
from .store import connect, save, decide, latest_evaluation, append_evaluation

COSIGN_WINDOW = timedelta(hours=24)
MAX_OPERATORS = 6

# payload 的 kind 标签 -> 原始证据归档类型
KIND_MAP = {
    'access_trace': 'desktop_access_trace',
    'approval_receipt': 'desktop_approval_receipt',
    'approval_revocation': 'desktop_approval_revocation',
    'hr_directory': 'desktop_hr_directory',
    'project_revision': 'desktop_project_revision',
    'permission_contract': 'desktop_permission_contract',
    'emergency_window': 'desktop_emergency_window',
}
TABLE_FOR_KIND = {
    'desktop_access_trace': 'access_trace',
    'desktop_hr_directory': 'account_ownership',
    'desktop_approval_receipt': 'approval_receipt',
    'desktop_approval_revocation': 'approval_revocation',
    'desktop_project_revision': 'project_revision',
    'desktop_permission_contract': 'permission_contract',
    'desktop_emergency_window': 'emergency_window',
}


def _ts(value):
    return datetime.fromisoformat(value)


def _all_rows(db, table):
    rows = []
    for r in db.execute(f'SELECT id,body FROM {table} ORDER BY id'):
        body = json.loads(r['body'])
        body.setdefault('id', r['id'])
        rows.append(body)
    return rows


# ---------------------------------------------------------------- 原始事件整理
def _dedup_events(raw_events):
    """重复日志去重；同一事件 id 出现不同正文则保留证据冲突并剔除该事件。"""
    seen, notes, dropped = {}, [], set()
    for ev in raw_events:
        eid = str(ev['id'])
        encoded = json.dumps(ev, sort_keys=True, ensure_ascii=False)
        if eid in seen:
            if seen[eid] == encoded:
                notes.append({'code': 'duplicate_event_ignored', 'event': eid})
            else:
                notes.append({'code': 'event_body_conflict', 'event': eid})
                dropped.add(eid)
        else:
            seen[eid] = encoded
    events = [ev for ev in raw_events if str(ev['id']) not in dropped]
    # 去重后保序
    unique, kept = [], set()
    for ev in events:
        eid = str(ev['id'])
        if eid not in kept:
            kept.add(eid)
            unique.append(ev)
    return unique, notes


def _build_sessions(events, skew):
    """从 login/logout 构造设备会话。未匹配 logout 的会话视为仍在登录。"""
    sessions = []
    logins = sorted((e for e in events if e.get('kind') == 'login'),
                    key=lambda e: _ts(e['at']))
    for login in logins:
        logout = None
        for e in events:
            if (e.get('kind') == 'logout' and e.get('account') == login.get('account')
                    and e.get('device') == login.get('device')
                    and _ts(e['at']) >= _ts(login['at']) - skew):
                if logout is None or _ts(e['at']) < _ts(logout['at']):
                    logout = e
        sessions.append({'login': login, 'logout': logout})
    return sessions


def _attribute(events, sessions, skew):
    """把每个动作归属到设备会话；退出登录后超出偏差窗口的遗留动作成为无主动作。"""
    attributed = {}
    for ev in events:
        if ev.get('kind') in ('login', 'logout'):
            continue
        t = _ts(ev['at'])
        match = None
        for s in sessions:
            login_t = _ts(s['login']['at'])
            if t < login_t - skew:
                continue
            if s['logout'] is not None and t > _ts(s['logout']['at']) + skew:
                continue  # 退出后的遗留窗口动作，不算在前一人名下
            if match is None or login_t > _ts(match['login']['at']):
                match = s
        if match is None:
            # 无会话可归属：事件自带账号仅作弱线索，仍标记为无主
            attributed[str(ev['id'])] = {'account': ev.get('account'), 'person': None,
                                         'orphan': True}
        else:
            attributed[str(ev['id'])] = {'account': match['login'].get('account'),
                                         'person': None, 'orphan': False}
    return attributed


# ---------------------------------------------------------------- 账号/自然人划分
def _set_partitions(elements):
    """枚举一个集合的全部集合划分（Bell 数，操作者不超过 6 人）。"""
    elements = tuple(elements)
    if not elements:
        yield ()
        return
    head, rest = elements[0], elements[1:]
    for partition in _set_partitions(rest):
        block = frozenset([head])
        yield (block,) + partition
        for i in range(len(partition)):
            yield partition[:i] + (partition[i] | block,) + partition[i + 1:]


def _forced_groups(forced):
    groups = {}
    for account, person in forced.items():
        groups.setdefault(person, set()).add(account)
    return groups


def _person_labels(partition, forced):
    labels, used = {}, set()
    for person, accounts in _forced_groups(forced).items():
        used.add(person)
        for account in accounts:
            labels[account] = person
    idx = 1
    for block in partition:
        while f'P{idx}' in used:
            idx += 1
        name = f'P{idx}'
        used.add(name)
        for account in block:
            labels[account] = name
    return labels


# ---------------------------------------------------------------- 版本血统
def _ancestors(revision, revisions):
    rev_by_id = {r['revision']: r for r in revisions}
    seen, stack = set(), [revision]
    while stack:
        cur = stack.pop()
        row = rev_by_id.get(cur)
        if not row:
            continue
        parents = row.get('parent') or []
        if isinstance(parents, str):
            parents = [parents]
        for p in parents:
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


# ---------------------------------------------------------------- 单条 trace 核证
def evaluate_trace(trace, context):
    project = trace.get('project')
    skew = timedelta(seconds=float(trace.get('clock_skew_seconds', 0)))
    raw_events = trace.get('events') or trace.get('actions') or []
    events, notes = _dedup_events(raw_events)
    sessions = _build_sessions(events, skew)
    attribution = _attribute(events, sessions, skew)

    revisions = [r for r in context['revisions'] if r.get('project') == project]
    receipts = [r for r in context['receipts'] if r.get('project') == project]
    revocations = [r for r in context['revocations'] if r.get('project') == project]
    cosigns = [c for c in context['cosigns'] if c.get('project') == project]
    windows = [w for w in context['windows'] if w.get('project') == project]
    forced = {a['account']: a['person'] for a in context['ownership'] if a.get('person')}
    contracts = [c for c in context['contracts'] if c.get('project') == project]

    rev_by_id = {r['revision']: r for r in revisions}
    revoked_at = {}
    for rev in revocations:
        revoked_at.setdefault(rev['receipt'], []).append(rev)
        notes.append({'code': 'approval_revoked', 'receipt': rev['receipt'],
                      'at': rev['at'], 'reason': rev.get('reason')})

    exports = [e for e in events if e.get('kind') == 'export']
    if not exports:
        return _result(trace, '待调查', 'export_missing', notes, sessions, attribution,
                       partitions=0, outcomes=[], missing=[], witnesses=[])

    # 权限合同必须在操作前固定
    contract = _pre_fixed_contract(contracts, exports, skew, notes)
    if contract is None:
        return _result(trace, '待调查', 'permission_contract_missing', notes, sessions,
                       attribution, partitions=0, outcomes=[],
                       missing=[{'type': 'permission_contract', 'project': project}],
                       witnesses=[])
    regime = contract['regime']
    if regime not in ('regulated', 'drill'):
        return _result(trace, '待调查', 'unknown_permission_regime', notes, sessions,
                       attribution, partitions=0, outcomes=[],
                       missing=[{'type': 'permission_contract', 'project': project}],
                       witnesses=[])
    as_of = _trace_as_of(events, receipts, revocations, cosigns)

    # 涉及账号：会话账号 + 编辑/导出归属账号 + 审批人 + 修订编辑者 + 补签人
    accounts = set()
    for s in sessions:
        if s['login'].get('account'):
            accounts.add(s['login']['account'])
    for info in attribution.values():
        if info.get('account'):
            accounts.add(info['account'])
    for r in receipts:
        accounts.add(r['approver'])
    for r in revisions:
        if r.get('editor'):
            accounts.add(r['editor'])
    for c in cosigns:
        accounts.add(c['person'])
    accounts |= set(forced)
    if len(accounts) > MAX_OPERATORS:
        return _result(trace, '待调查', 'operator_scope_exceeded', notes, sessions,
                       attribution, partitions=0, outcomes=[],
                       missing=[{'type': 'operator_scope', 'project': project,
                                 'accounts': sorted(accounts)}], witnesses=[])

    unknown = sorted(a for a in accounts if a not in forced)
    window_notes = _precheck_emergency_windows(windows, exports, skew)
    notes = notes + window_notes
    partition_outcomes = []
    for partition in _set_partitions(unknown):
        labels = _person_labels(partition, forced)
        outcome = _evaluate_partition(
            labels, exports, attribution, revisions, rev_by_id, receipts,
            revoked_at, cosigns, windows, regime, skew, as_of)
        partition_outcomes.append(outcome)

    return _aggregate(trace, regime, notes, sessions, attribution, partition_outcomes,
                      unknown, forced, skew)


def _trace_as_of(events, receipts, revocations, cosigns):
    """以该 trace 相关证据中的最晚时间作为“当前时点”，判断补签窗口是否已到期。"""
    stamps = []
    for ev in events:
        stamps.append(_ts(ev['at']))
    for group in (receipts, revocations, cosigns):
        for row in group:
            if row.get('at'):
                stamps.append(_ts(row['at']))
    return max(stamps) if stamps else datetime.max


def _pre_fixed_contract(contracts, exports, skew, notes):
    if not contracts:
        return None
    earliest_export = min(_ts(e['at']) for e in exports)
    fixed = [c for c in contracts if _ts(c['effective_at']) <= earliest_export + skew]
    if not fixed:
        notes.append({'code': 'contract_not_pre_fixed'})
        return None
    chosen = sorted(fixed, key=lambda c: (_ts(c['effective_at']), c['id']))[0]
    regimes = {c['regime'] for c in fixed}
    if len(regimes) > 1:
        notes.append({'code': 'contract_regime_conflict', 'kept': chosen['id']})
    return chosen


def _receipt_state(receipt, export_event, ancestors, revoked_at, skew):
    """返回 (state, note)。state: valid / stale / revoked / after_export / unrelated。

    版本核证先看内容哈希：批准 r6 的回执即使与 r7 同属一条血统，也不能放行 r7。
    """
    rev = export_event.get('revision')
    content_hash = export_event.get('content_hash')
    same_lineage = receipt['revision'] == rev or receipt['revision'] in ancestors
    if receipt.get('content_hash') != content_hash:
        return 'stale', {'code': 'stale_revision_approval', 'receipt': receipt['id'],
                         'approved': receipt['revision'], 'exported': rev,
                         'same_lineage': same_lineage}
    if not same_lineage:
        return 'unrelated', None
    for revocation in revoked_at.get(receipt['id'], []):
        if _ts(revocation['at']) <= _ts(export_event['at']) + skew:
            return 'revoked', {'code': 'revoked_approval_used', 'receipt': receipt['id'],
                               'revoked_at': revocation['at']}
    if _ts(receipt['at']) > _ts(export_event['at']) + skew:
        return 'after_export', {'code': 'approval_after_export', 'receipt': receipt['id']}
    if _ts(receipt['at']) > _ts(export_event['at']) - skew:
        # 时间差落在时钟偏差窗口内：两种时序都相容，保留但要求时钟证据定序
        return 'valid', {'code': 'clock_order_ambiguous', 'receipt': receipt['id'],
                         'export': export_event['id']}
    return 'valid', None


def _precheck_emergency_windows(windows, exports, skew):
    """在分区枚举前固定紧急窗口证据问题（过期/缺原因），保证留痕进入结论。"""
    notes = []
    for export in exports:
        t = _ts(export['at'])
        for w in windows:
            # 窗口与导出在偏差窗口内有重合才相关；已在窗口结束之后超出偏差即过期
            if t < _ts(w['start']) - skew:
                continue
            if t > _ts(w['end']) + skew:
                notes.append({'code': 'emergency_window_expired',
                              'window': w['id'], 'export': export['id']})
            elif not w.get('reason'):
                notes.append({'code': 'emergency_reason_missing',
                              'window': w['id'], 'export': export['id']})
    unique = []
    for n in notes:
        if n not in unique:
            unique.append(n)
    return unique


def _emergency_window(windows, export_event, skew, notes):
    t = _ts(export_event['at'])
    window = None
    for w in windows:
        # 窗口与导出时间在偏差窗口内才可能重合
        if t < _ts(w['start']) - skew or t > _ts(w['end']) + skew:
            continue
        if window is None or _ts(w['start']) < _ts(window['start']):
            window = w
    if window is None:
        return None
    if t > _ts(window['end']) + skew:
        notes.append({'code': 'emergency_window_expired', 'window': window['id']})
        return None
    if not window.get('reason'):
        notes.append({'code': 'emergency_reason_missing', 'window': window['id']})
        return None
    return window


def _evaluate_partition(labels, exports, attribution, revisions, rev_by_id, receipts,
                        revoked_at, cosigns, windows, regime, skew, as_of):
    verdict, export_reports, witnesses = 'compliant', [], []
    for export in exports:
        info = attribution[str(export['id'])]
        rev = export.get('revision')
        ancestors = _ancestors(rev, revisions)
        rev_row = rev_by_id.get(rev, {})
        editor_accounts = set()
        orphan_edit = False
        if rev_row.get('editor'):
            editor_accounts.add(rev_row['editor'])
        for ev_id, att in attribution.items():
            if att.get('revision') == rev:
                if att.get('account'):
                    editor_accounts.add(att['account'])
                if att.get('orphan'):
                    orphan_edit = True
        editors = {labels[a] for a in editor_accounts if a in labels}
        exporter = labels.get(info['account']) if not info['orphan'] else None

        report = {'export': export['id'], 'revision': rev,
                  'content_hash': export.get('content_hash'),
                  'attributed_account': info['account'],
                  'attributed_person': exporter, 'orphan': info['orphan'],
                  'orphan_edit': orphan_edit, 'receipts': []}
        distinct_approval, self_approval = False, False
        for receipt in receipts:
            state, note = _receipt_state(receipt, export, ancestors, revoked_at, skew)
            report['receipts'].append({'receipt': receipt['id'], 'state': state})
            if note and note['code'] in ('stale_revision_approval',
                                         'revoked_approval_used'):
                witnesses.append(note)
            if note and note['code'] == 'stale_revision_approval':
                witnesses.append({'code': 'merge_or_old_branch_signature',
                                  'receipt': receipt['id'], 'exported': rev})
            if state != 'valid':
                continue
            approver = labels.get(receipt['approver'])
            if note is not None and note['code'] == 'clock_order_ambiguous':
                # 时序证据不足：该回执在本划分中保留为相容，同时记录最小缺证
                report.setdefault('clock_ambiguous', []).append(
                    {'receipt': receipt['id'], 'export': export['id']})
            if approver is not None and approver not in editors and approver != exporter:
                distinct_approval = True
            else:
                self_approval = True

        if regime == 'regulated':
            outcome = _regulated_outcome(distinct_approval, self_approval, report,
                                         exporter, editors, orphan_edit, witnesses)
        else:
            outcome = _drill_outcome(distinct_approval, self_approval, windows, export,
                                     cosigns, labels, editors, exporter, orphan_edit,
                                     skew, as_of, witnesses, report)
        report['outcome'] = outcome
        export_reports.append(report)
        if outcome == 'violation':
            verdict = 'violation'
        elif outcome == 'indeterminate' and verdict != 'violation':
            verdict = 'indeterminate'
    return {'verdict': verdict, 'labels': labels, 'exports': export_reports,
            'witnesses': witnesses}


def _regulated_outcome(distinct_approval, self_approval, report, exporter, editors,
                       orphan_edit, witnesses):
    # 导出或编辑动作无主（退出登录后的遗留窗口动作）：自然人身份不可核，分离无法证明
    if exporter is None or orphan_edit:
        witnesses.append({'code': 'operator_unattributed', 'export': report['export']})
        return 'indeterminate'
    if distinct_approval:
        return 'compliant'
    if self_approval:
        witnesses.append({'code': 'self_approval', 'export': report['export']})
        return 'violation'
    if not editors:
        return 'indeterminate'
    witnesses.append({'code': 'separate_approval_missing', 'export': report['export']})
    return 'violation'


def _drill_outcome(distinct_approval, self_approval, windows, export, cosigns, labels,
                   editors, exporter, orphan_edit, skew, as_of, witnesses, report):
    if distinct_approval:
        return 'compliant'
    if exporter is None or orphan_edit or not editors:
        witnesses.append({'code': 'operator_unattributed', 'export': export['id']})
        return 'indeterminate'
    window = _emergency_window(windows, export, skew, [])
    if window is None:
        witnesses.append({'code': 'separate_approval_missing', 'export': export['id']})
        return 'violation'
    # 紧急窗口路径：自批 + 原因已填，需要 24 小时内另一自然人补签
    if not self_approval:
        return 'indeterminate'
    deadline = _ts(export['at']) + COSIGN_WINDOW + skew
    valid_cosign = None
    for c in cosigns:
        if c.get('window') != window['id']:
            continue
        person = labels.get(c['person'])
        if person is None or person in editors or person == exporter:
            continue
        if _ts(c['at']) <= deadline:
            valid_cosign = c
            break
    if valid_cosign:
        report['cosign'] = valid_cosign['id']
        return 'compliant'
    if as_of <= deadline:
        report['cosign_deadline'] = deadline.isoformat()
        return 'indeterminate'
    witnesses.append({'code': 'emergency_cosign_overdue', 'export': export['id'],
                      'window': window['id']})
    return 'violation'


def _aggregate(trace, regime, notes, sessions, attribution, outcomes, unknown, forced,
               skew):
    total = len(outcomes)
    n_violation = sum(1 for o in outcomes if o['verdict'] == 'violation')
    n_indeterminate = sum(1 for o in outcomes if o['verdict'] == 'indeterminate')
    n_compliant = total - n_violation - n_indeterminate

    witnesses, reason_counts = [], {}
    for o in outcomes:
        for w in o['witnesses']:
            key = w['code']
            reason_counts[key] = reason_counts.get(key, 0) + 1
            if w not in witnesses:
                witnesses.append(w)

    missing = _minimal_missing(outcomes, unknown, forced, trace, skew, regime)

    if total and n_violation == total:
        # 旧版/旧分支签名偷用是确定性的版本核证失败，优先于自批等原因
        if reason_counts.get('stale_revision_approval'):
            reason = 'stale_revision_approval'
        elif reason_counts.get('revoked_approval_used'):
            reason = 'revoked_approval_used'
        else:
            reason = _primary_reason(reason_counts, 'duty_violation_confirmed')
        status = '确定违规'
    elif n_compliant == total and not missing:
        status, reason = '已排除', 'separate_duty_chain_verified'
    else:
        status = '待调查'
        if any(m['type'] == 'emergency_cosign' for m in missing):
            reason = 'emergency_cosign_pending'
        elif any(m['type'] == 'clock_ordering' for m in missing):
            reason = 'clock_skew_ambiguous'
        elif any(m['type'] == 'account_ownership' for m in missing):
            reason = 'identity_unresolved'
        else:
            reason = _primary_reason(reason_counts, 'duty_chain_unresolved')

    return _result(trace, status, reason, notes + _partition_notes(outcomes), sessions,
                   attribution, partitions=total,
                   compliant=n_compliant, violating=n_violation,
                   indeterminate=n_indeterminate, outcomes=outcomes,
                   missing=missing, witnesses=witnesses, regime=regime)


def _partition_notes(outcomes):
    notes = []
    for o in outcomes:
        for report in o['exports']:
            for r in report['receipts']:
                if r['state'] in ('stale', 'revoked', 'after_export', 'unrelated'):
                    notes.append({'code': f'receipt_{r["state"]}',
                                  'export': report['export'], 'receipt': r['receipt']})
    # 去重保序
    unique = []
    for n in notes:
        if n not in unique:
            unique.append(n)
    return unique


def _minimal_missing(outcomes, unknown, forced, trace, skew, regime):
    missing = []
    decisive = set()
    involved = set()
    for o in outcomes:
        if o['verdict'] == 'compliant':
            continue
        for report in o['exports']:
            involved.add(report['attributed_account'])
            for r in report['receipts']:
                if r['state'] == 'valid':
                    involved.add(r['receipt'])
    # 账号归属：仅列出会改变结论的未知账号（在某些划分同人、某些划分不同人）
    for account in unknown:
        same_as, distinct_from = set(), set()
        for o in outcomes:
            label = o['labels'].get(account)
            for other, other_label in o['labels'].items():
                if other == account:
                    continue
                if other_label == label:
                    same_as.add(other)
                else:
                    distinct_from.add(other)
        if same_as and distinct_from:
            decisive.add(account)
    for account in sorted(decisive | {a for a in involved if a and a in unknown}):
        missing.append({'type': 'account_ownership', 'account': account,
                        'detail': '需人事记录确认该账号归属的自然人'})
    # 时钟偏差导致的先后不明
    ambiguous = set()
    for o in outcomes:
        for note in o.get('witnesses', []):
            if note.get('code') == 'clock_order_ambiguous':
                ambiguous.add((note['receipt'], note['export']))
        for report in o['exports']:
            for item in report.get('clock_ambiguous', []):
                ambiguous.add((item['receipt'], item['export']))
    for receipt, export in sorted(ambiguous):
        missing.append({'type': 'clock_ordering', 'receipt': receipt, 'export': export,
                        'detail': '审批与导出时间差落在时钟偏差窗口内，需设备时钟证据定序'})
    for o in outcomes:
        for report in o['exports']:
            if report.get('cosign_deadline'):
                missing.append({'type': 'emergency_cosign', 'export': report['export'],
                                'deadline': report['cosign_deadline'],
                                'detail': '紧急自批后 24 小时补签尚未到位'})
    return missing


def _primary_reason(reason_counts, default):
    if not reason_counts:
        return default
    return sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _result(trace, status, reason, notes, sessions, attribution, partitions, outcomes,
            missing, witnesses, compliant=0, violating=0, indeterminate=0, regime=None):
    return {
        'trace_id': str(trace['id']), 'project': trace.get('project'), 'regime': regime,
        'status': status, 'reason': reason,
        'operator_count': len({s['login'].get('account') for s in sessions
                               if s['login'].get('account')}),
        'key_event_count': len(trace.get('events') or trace.get('actions') or []),
        'possible_chains': partitions,
        'compliant_chains': compliant, 'violating_chains': violating,
        'indeterminate_chains': indeterminate,
        'sessions': [{'account': s['login'].get('account'),
                      'device': s['login'].get('device'),
                      'login_at': s['login']['at'],
                      'logout_at': s['logout']['at'] if s['logout'] else None}
                     for s in sessions],
        'attribution': attribution,
        'export_outcomes': [r for o in outcomes for r in o['exports']],
        'missing_evidence': missing, 'witnesses': witnesses, 'notes': notes,
    }


# ---------------------------------------------------------------- 采录与重评
def import_actions(path, file):
    # 先读 payload 的 kind 标签决定归档类型（capture 仍校验 version 与哈希）
    payload = json.loads(Path(file).read_text(encoding='utf-8'))
    raw_kind = KIND_MAP.get(payload.get('kind'), 'desktop_access_trace')
    source, payload = capture(path, raw_kind, file)
    db = connect(path)
    results = []
    try:
        with db:
            table = TABLE_FOR_KIND[raw_kind]
            for row in payload.get('rows', []):
                key = str(row['id'])
                if table == 'access_trace':
                    save(db, table, key, source, payload['version'], row, '已采录',
                         'trace_captured')
                    _persist_sessions(db, row, source, payload['version'])
                elif table == 'approval_receipt':
                    if row.get('kind') == 'emergency_cosign':
                        save(db, 'emergency_cosign', key, source, payload['version'],
                             row, '已采录', 'cosign_captured')
                    else:
                        save(db, table, key, source, payload['version'], row, '已采录',
                             'receipt_captured')
                else:
                    save(db, table, key, source, payload['version'], row, '已采录',
                         f'{table}_captured')
            context = _build_context(db)
            traces = _all_rows(db, 'access_trace')
            for trace in traces:
                outcome = evaluate_trace(trace, context)
                latest = latest_evaluation(db, trace['id'])
                if latest is not None and latest['body'] == json.dumps(
                        outcome, sort_keys=True, ensure_ascii=False):
                    # 重放：证据未变，不新增评估版本
                    seq = latest['seq']
                else:
                    seq = append_evaluation(db, trace['id'], source,
                                            payload['version'], outcome,
                                            outcome['status'], outcome['reason'])
                row_ref = db.execute(
                    'SELECT source_id,version FROM access_trace WHERE id=?',
                    (trace['id'],)).fetchone()
                save(db, 'access_trace', trace['id'], row_ref['source_id'],
                     row_ref['version'], _trace_body(db, trace['id']),
                     outcome['status'], outcome['reason'])
                results.append(decide(db, '职责分离核证', trace['id'],
                                      outcome['status'], outcome['reason'], source))
                results[-1]['seq'] = seq
                results[-1]['possible_chains'] = outcome['possible_chains']
        return results
    finally:
        db.close()


def _trace_body(db, key):
    return json.loads(db.execute('SELECT body FROM access_trace WHERE id=?',
                                 (key,)).fetchone()['body'])


def _persist_sessions(db, trace, source, version):
    events = trace.get('events') or trace.get('actions') or []
    skew = timedelta(seconds=float(trace.get('clock_skew_seconds', 0)))
    for s in _build_sessions(events, skew):
        key = f"session:{trace['id']}:{s['login']['id']}"
        save(db, 'device_session', key, source, version,
             {'trace': trace['id'], 'account': s['login'].get('account'),
              'device': s['login'].get('device'), 'login_at': s['login']['at'],
              'logout_at': s['logout']['at'] if s['logout'] else None},
             '已采录', 'session_captured')


def _build_context(db):
    ownership = _all_rows(db, 'account_ownership')
    receipts = _all_rows(db, 'approval_receipt')
    revocations = _all_rows(db, 'approval_revocation')
    revisions = _all_rows(db, 'project_revision')
    contracts = _all_rows(db, 'permission_contract')
    windows = _all_rows(db, 'emergency_window')
    cosigns = _all_rows(db, 'emergency_cosign')
    stamps = []
    for table in ('access_trace', 'approval_receipt', 'emergency_cosign',
                  'approval_revocation'):
        for row in _all_rows(db, table):
            for field in ('at', 'start', 'end', 'effective_at'):
                if isinstance(row.get(field), str):
                    try:
                        stamps.append(_ts(row[field]))
                    except ValueError:
                        pass
    as_of = max(stamps) if stamps else datetime.max
    return {'ownership': ownership, 'receipts': receipts, 'revocations': revocations,
            'revisions': revisions, 'contracts': contracts, 'windows': windows,
            'cosigns': cosigns, 'as_of': as_of}

