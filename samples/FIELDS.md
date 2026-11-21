# 离线合成 JSON 样例与字段说明

所有输入均为离线桌面导出的 JSON 文件，顶层必须含非空字符串 `version`；证据类别由
顶层 `kind` 决定（缺省按 `access_trace` 处理，兼容旧格式）。原始字节与 SHA-256
存入 SQLite `export_file`，同哈希文件重放为同一证据来源，不产生新评估版本。

入口（均为 `--db ledger.db 文件路径`）：

- `access-import`：采录全部七类证据并重评所有 trace；
- `incident-review`：按最新核证结果决定立案 / 待补证 / 关闭；
- `report`：回读违规见证或最小缺证，已签出报告不被原地改写。

## 1. 人事账号归属 `hr_directory`（01_hr_directory.json → `account_ownership`）

| 字段 | 说明 |
| --- | --- |
| `id` | 归属记录行 id。同一账号的新归属版本必须用**新行 id**；同 id 改正文触发 `identity_conflict` |
| `account` | 桌面系统账号名 |
| `person` | 账号归属的自然人标识。**账号名不同不等于两人**：E1、E2 同 `person` 即同一自然人 |
| `valid_from` | 归属生效时间（可选） |

缺少某账号的归属记录时，系统枚举该账号与其他未知账号“同人 / 不同人”的全部集合划分，
每条相容划分都是一条可能职责链。

## 2. 项目修订记录 `project_revision`（02 → `project_revision`）

| 字段 | 说明 |
| --- | --- |
| `id` / `project` | 行 id / 项目 id |
| `revision` | 版本号（如 r6、r7） |
| `editor` | 产生该版本的账号 |
| `parent` | 父版本，字符串或数组；数组表示**多分支合并**（r7 的 parent 为 r6a、r6b） |
| `content_hash` | 该版本内容指纹；审批回执按内容指纹核证，批准 r6 不能放行哈希不同的 r7 |

## 3. 权限合同 `permission_contract`（03 → `permission_contract`）

| 字段 | 说明 |
| --- | --- |
| `regime` | `regulated`（受监管客户）或 `drill`（内部演练） |
| `effective_at` | 合同固定时间，必须 **早于最早导出（含时钟偏差）**；事后固定不能追溯改类别 |

- `regulated`：编辑者之外的第二名自然人必须批准**同一版**内容，任何自批均违规；
- `drill`：允许值班人员在事前登记的紧急窗口内自批，但须有紧急原因并在 24 小时内
  由另一自然人补签；补签缺失时先 `待调查`，窗口过后仍无补签转 `确定违规`。

## 4. 审批回执 `approval_receipt`（04 → `approval_receipt` / `emergency_cosign`）

普通回执字段：`id`、`project`、`receipt`、`revision`、`content_hash`、`approver`、
`at`。行内 `kind: "emergency_cosign"` 的为紧急补签，字段为 `window`、`person`、`at`，
单独存入 `emergency_cosign`。

回执状态机（每个导出 × 每条回执都会落状态）：

- `valid`：同版本同内容、未撤销、时序相容；
- `stale`：内容指纹不符（含“同血统但旧版 r6 批准 r7”、合并版偷用旧分支签名）；
- `revoked`：导出前（含偏差）回执已被撤销；
- `after_export`：审批时间明确晚于导出超过偏差窗口；
- `unrelated`：既非同版本也不在版本血统上。

审批与导出时间差落在 `clock_skew_seconds` 内时，两种时序都视为相容，回执保留
`valid` 并附 `clock_order_ambiguous`，同时把时钟定序列入最小缺证。

## 5. 紧急授权窗口 `emergency_window`（05 → `emergency_window`）

字段：`id`、`project`、`start`、`end`、`reason`、`registered_at`。导出时间超出
`end + 偏差` 记 `emergency_window_expired`；窗口内但 `reason` 缺失记
`emergency_reason_missing`，两种情况都不构成有效授权。

## 6. 桌面访问日志 `access_trace`（06 → `access_trace`，会话入 `device_session`）

trace 行字段：`id`、`project`、`clock_skew_seconds`（设备时钟偏差秒数）、`events[]`。
事件字段：`id`、`kind`（login / logout / edit / export）、`account`、`device`、
`at`，编辑/导出另带 `revision` 与 `content_hash`。

会话归属规则：

1. login/logout 按设备+账号配成会话，未匹配 logout 视为仍登录；
2. 动作时间在某会话 `[login-偏差, logout+偏差]` 内才归属该会话，重叠时取最近登录；
3. 退出登录且超过偏差窗口的遗留动作标记 `orphan: true`，**不算在前一人名下**，
   受监管合同下直接导致自然人不可核（待调查）；
4. 重复事件 id 且正文相同 → 去重留痕；同 id 不同正文 → `event_body_conflict`，
   该事件剔除并进入待调查。

## 7. 审批撤销 `approval_revocation`（07 → `approval_revocation`）

字段：`id`、`project`、`receipt`、`at`、`by`、`reason`。撤销时间早于导出（含偏差）
时，对应回执状态变 `revoked`，见证 `revoked_approval_used`。

## 8. 复核请求（08，`incident-review` 专用，kind 缺省）

字段：`id`（案件 id）、`trace`、`reviewer`。`reviewer` 仅记录承办人，**不再单独
构成立案理由**。处置完全跟随最新 `audit_evaluation`：

| 核证状态 | 案件状态 | reason |
| --- | --- | --- |
| 确定违规 | 已立案 | `duty_violation_confirmed` |
| 待调查 | 待补证 | 核证 reason（如 `identity_unresolved`、`clock_skew_ambiguous`、`emergency_cosign_pending`） |
| 已排除 | 已关闭 | `duty_chain_verified` |
| 无核证记录 | 待核 | `trace_evaluation_missing` |

## 核证结论与持久化

`audit_evaluation` 每次证据变化追加一行（`seq` 递增），保存：三档 `status`、
`possible_chains`（相容职责链数）、合规/违规/不确定链计数、每条导出的回执状态、
会话归属、`witnesses`（确定违规见证）与 `missing_evidence`（待查最小缺证，含
`account_ownership` / `clock_ordering` / `emergency_cosign` / `permission_contract`
四类及中文 `detail`）。`evidence_change` 记录所有状态迁移；`report_snapshot`
按请求路径保存报告哈希历史，同路径新内容另存 `*.revN.json`，已签出报告不被改写。

结论判定：所有相容链都违规才 `确定违规`；部分链违规或存在最小缺证时 `待调查`；
所有链均合规且无缺证时 `已排除`。操作者超过 6 人时返回
`operator_scope_exceeded` 待查，不做无界枚举。
