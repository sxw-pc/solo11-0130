"""复核立案：案件处置由 access_audit 的自然人/版本核证结果驱动。

- 确定违规 -> 已立案（reviewer 仅记录承办人，不再单独构成立案理由）
- 待调查   -> 待补证（回读最少缺证清单）
- 已排除   -> 已关闭
同一 trace 的每次证据版本、可能链数量与结论变更均留痕；已签出的报告不在原地改写。
"""
import json

from .ingest import capture
from .store import (connect, save_case, decide, latest_evaluation,
                    record_case_change)

REVIEW_KIND = 'desktop_incident_review'


def review(path, file):
    source, payload = capture(path, REVIEW_KIND, file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                trace_id = str(row['trace'])
                evaluation = latest_evaluation(db, trace_id)
                old_case = db.execute('SELECT * FROM incident_case WHERE id=?',
                                      (key,)).fetchone()
                old_status = old_case['status'] if old_case else None

                if not evaluation:
                    status, reason = '待核', 'trace_evaluation_missing'
                    body = dict(row)
                else:
                    audit = json.loads(evaluation['body'])
                    audit_status = evaluation['status']
                    if audit_status == '确定违规':
                        status, reason = '已立案', 'duty_violation_confirmed'
                    elif audit_status == '待调查':
                        status, reason = '待补证', audit['reason']
                    elif audit_status == '已排除':
                        status, reason = '已关闭', 'duty_chain_verified'
                    else:
                        status, reason = '待核', 'audit_status_unknown'
                    body = {
                        'review_request': row, 'trace': trace_id,
                        'audit_seq': evaluation['seq'],
                        'audit_version': evaluation['version'],
                        'possible_chains': audit.get('possible_chains', 0),
                        'compliant_chains': audit.get('compliant_chains', 0),
                        'violating_chains': audit.get('violating_chains', 0),
                        'indeterminate_chains': audit.get('indeterminate_chains', 0),
                        'regime': audit.get('regime'),
                        'missing_evidence': audit.get('missing_evidence', []),
                        'witnesses': audit.get('witnesses', []),
                        'reviewer': row.get('reviewer'),
                    }

                save_case(db, key, source, payload['version'], body,
                          status, reason)
                if old_status != status:
                    record_case_change(db, trace_id, old_status, status, source,
                                       f"case {key}: {reason}"
                                       + (f"; reviewer={row.get('reviewer')}"
                                          if row.get('reviewer') else ''))
                results.append(decide(db, '访问异常复核', key, status, reason, source))
                if evaluation:
                    results[-1]['possible_chains'] = json.loads(
                        evaluation['body']).get('possible_chains', 0)
                    results[-1]['missing_evidence'] = json.loads(
                        evaluation['body']).get('missing_evidence', [])
            return results
    finally:
        db.close()
