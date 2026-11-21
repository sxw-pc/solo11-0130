# 离线桌面导出回归：字段与合同说明

本说明描述状态图驱动的 QA 规划/执行台账。所有数据都是离线 JSON，原始字节与
SHA-256 存入 SQLite（`export_file`），结论写入 `decision_log`，报告由
`report.build` 回读每条覆盖结论对应的合法动作证据。

## 1. 状态图输入（plan 入参 `graph`）

| 字段 | 含义 |
| --- | --- |
| `graph.states[].id` | 界面状态标识，全图唯一（≤10 个状态） |
| `states[].modal` | 是否为弹窗。弹窗出现后必须先处理，才能继续主窗口动作 |
| `states[].terminal` | 是否终态（如已发布）。终态无出边，发布后不能假装回到未发布 |
| `graph.transitions[]` | 合法转移（≤12 条） |
| `transitions[].from_state` / `to_state` | 动作前后窗口，必须引用已声明状态 |
| `transitions[].button_text` | 按钮文案。同名按钮靠 `from_state` 窗口区分，不靠文案碰运气 |
| `transitions[].evidence` | 动作后唯一可见证据，录制事件必须逐字匹配才算点击落地 |
| `transitions[].risk` | `high` 标记高风险转移 |
| `transitions[].mutates` | 是否改动文件/发布状态；为真才消耗工作区副本 |
| `transitions[].irreversible` | 是否不可逆（正式发布）。已执行的不可逆发布不会二次运行 |
| `transitions[].runtime_s` | 该转移模拟运行秒数，用于总运行时间预算 |
| `graph.targets[]` | 高风险目标（≤3 个） |
| `targets[].transition` | 目标对应的精确转移 |
| `targets[].required_modal` | 必须实际经过的弹窗（如 `publish_confirm_dialog`）。缺弹窗则 `passed:true` 也退回 |

## 2. 快照与工作区

| 字段 | 含义 |
| --- | --- |
| `snapshots[].id` / `state` / `published` | 可重置干净快照及其起始窗口、发布标记 |
| `workspaces[].id` / `snapshot` | 工作区副本及其来源快照 |
| `ui_workspace.body.state` | `available` → 执行后 `consumed` |
| `ui_workspace.body.consumed_by` | 消耗该副本的执行 id 列表；同一日志重放不再追加 |

规则：每条路径从明确快照开始；只有 `mutates` 的路径消耗副本；只读的失败观察
不消耗；不可逆发布独占的副本被标记 consumed 后，再次走发布会被判
`irreversible_publish_not_re_run` 并退回。

## 3. 候选用例与规划输出

候选 `cases[]` 携带 `start_snapshot_state` 与有序 `steps[].transition`，以及仅作
参考的 `coverage_labels`。规划器不信任标签：逐跳重放，校验起点窗口、弹窗优先、
终态无出边，并计算真正命中的目标；标签多报记入 `label_overclaim`。

`qa_suite.body` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `contract` | `smoke` 或 `audit`，录制前固定，资源不足也不临时降级 |
| `limits.workspace_copies` / `max_total_runtime_s` | 副本与运行时间上限 |
| `scripts[].start_snapshot` / `start_window` | 每条脚本的起始快照与窗口 |
| `scripts[].action_sequence` | 动作转移 id 序列 |
| `scripts[].checkpoints[]` | 每跳独立检查点：前后窗口、按钮、消歧标记、可见证据、弹窗标记 |
| `scripts[].expected_end_window` | 预期结束窗口 |
| `scripts[].paused_in_failure_modal` | 是否停在导出失败弹窗（可续跑） |
| `covered_targets` / `uncovered_targets` | 覆盖与未覆盖目标 |
| `uncovered_targets[].blocker` | 最小阻碍：副本不足/运行时间超限/无合法前置路径 |
| `workspace_copies_used` / `workspace_reservations` | 实际副本与预留关系 |
| `rejected_cases[]` | 被拒收用例及结构性原因 |

## 4. 两类合同

- **smoke（内部冒烟）**：保存草稿与正式发布可在同一份工作区连续路径完成，只要
  每跳都有独立检查点。三状态图下一份副本即可同时覆盖保存草稿与发布两个目标；
  只读失败分支不另耗副本。
- **audit（对外审计）**：每个不可逆发布目标从干净快照独立跑，保存草稿对照路径也
  单独占一份工作区，禁止单脚本串联两个目标。资源不足时整个审计报
  `audit_workspace_copy_shortage`，`scripts` 为空、不宣称任何覆盖；再补一份副本
  即可生成独立脚本。

## 5. 执行重放入参与结论

录制 `rows[].events[]` 每帧含 `frame_id`、`transition`、`window_before`、
`window_after`、`button_text`、`visible_evidence`。逐跳校验：转移一致、点击前
窗口与图一致、同名按钮按窗口消歧、弹窗先处理、可见证据逐字匹配、帧不缺失。

`qa_execution` 结论取值：

| status | reason 触发条件 |
| --- | --- |
| `通过` | `state_path_replayed` / `resumed_state_path_replayed`，全部检查点成立 |
| `退回` | `window_state_mismatch`、`frame_missing`、`unexpected_extra_frame`、`visible_evidence_mismatch`、`transition_mismatch`、`modal_not_handled`、`ambiguous_button_unresolved`、`publish_confirm_missing`、`irreversible_publish_not_re_run`、`case_not_in_plan`、`invalid_resume_checkpoint` |
| `待续跑` | `paused_at_export_failure_dialog`，携带 `resume_checkpoint` |
| `待核` | `suite_missing` / `state_graph_missing` |

续跑：`resume_execution` 引用处于 `待续跑` 的执行 id，从失败弹窗检查点继续；
引用不存在或状态不符判 `invalid_resume_checkpoint`。重放同一执行日志（来源
hash 相同）幂等返回原结论，`workspace_copy_consumed=false`，不重复消耗副本。

## 6. 报告 `qa_coverage`

每个目标回读：`conclusion`（`covered` / `evidence_replayed_but_case_returned` /
`uncovered` / `not_replayed`）、`legal_action_evidence`（执行 id、case、动作、
前后窗口、按钮、可见证据、帧号）、规划级 `blocker`、以及
`disambiguated_by_window`。工作区段列出每个副本的 `state` 与 `consumed_by`。
