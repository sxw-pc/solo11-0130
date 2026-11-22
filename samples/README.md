# 离线导出排程字段说明

`export-queue` 与 `export-dispatch` 都读取顶层 JSON（必须含 `version`），
原始字节与 SHA-256 由 `export_file` 冻结。下列字段全部为本地虚构数据，
不访问网络。

## 排程输入（export-queue）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `queue_id` | string | 队列标识；冻结计划、契约锁、执行事件都以它为前缀 |
| `contract` | `live` \| `internal` | 任务**进入队列前**固定。`live` 为直播交付：只有硬截止，低权重内部样片可整条不排入；`internal` 为内部素材整理：全部软截止，允许个别逾期但必须如实标记 |
| `start` | ISO 时间 | 规划起点，缺省取许可首个窗口起点 |
| `load_minutes` | number | 工作站切换项目时的默认装载分钟数，可被单任务 `load_minutes` 覆盖 |
| `license.id` | string | 编码许可标识；全库只有一把，编码阶段独占 |
| `license.windows` | `[{start,end}]` | 许可可用时段；许可从 10:00 才可用时，起点就在第一个窗口 |
| `license.occupied` | `[{start,end}]` | 许可在任务开始前已被别的任务/外部占用的时段，排程会避开 |
| `workstations[].id` | string | 工作站编号（示例 A/B） |
| `workstations[].windows` | `[{start,end}]` | 该站可用时段 |
| `workstations[].preloaded` | string[] | 该站已装载的项目，命中则不付装载时间 |
| `rows[].id` | string | 任务号 |
| `rows[].project` | string | 项目名；同一项目连续编码不付装载时间 |
| `rows[].encode_minutes` / `minutes` | number | 语言轨编码时长（独占许可） |
| `rows[].prepare_minutes` | number | 准备阶段时长，可在任一工作站并行进行 |
| `rows[].verify_minutes` | number | 本地校验时长，必须在编码完整结束后进行 |
| `rows[].load_minutes` | number? | 本项目装载覆盖值 |
| `rows[].deadline` | ISO? | 硬/软截止时刻 |
| `rows[].target` | ISO? | 软截止目标；缺省同 `deadline` |
| `rows[].hard` | bool? | 覆盖合同默认；`live` 缺省 true，`internal` 缺省 false |
| `rows[].weight` | number | 等候权重；剩余容量按它分配 |
| `rows[].after` | string? | 硬先后依赖，前置任务必须先编码 |
| `rows[].checkpoints` | number[] | 允许续跑的编码完成分钟数（相对编码起点） |

同一 `queue_id` 的 `contract` 首次规划即写入 `contract_lock`，之后换合同会
报 `contract_locked`：不能排失败后再改合同。

## 排程输出（落库）

| 表 | 内容 |
| --- | --- |
| `export_plan` | 冻结计划：合同、契约、最优顺序、目标值、并列规则、许可租约 |
| `plan_job` | 每个任务的计划阶段、状态（按时/逾期/未排入）与原因 |
| `plan_phase` | 准备、装载、编码、校验的站号与起止 |
| `workstation_slot` | 按站汇总的阶段时段，供执行对照 |
| `license_lease` | 计划租约与执行后的实际占用（`actual:<queue>`） |
| `contract_lock` | 任务入队前固定的合同 |
| `plan_revision` | 重新规划前后的顺序、改派与保留阶段 |
| `bottleneck` | 逾期/未排入的真正瓶颈与最早证据时刻 |
| `execution_event` | 采录的桌面执行事件（按内容去重） |
| `execution_diff` | 计划与执行差异：站点改动、晚开工、未计划阶段 |
| `reassignment` | A 站故障后改派到 B 站的记录 |

## 目标与并列规则

`live`：先最小化未达成的硬截止数，其次硬逾期分钟、被挤出队列的软任务权重，
再按权重加权等候时间。

`internal`：先保证排入任务数最多，再最小化加权等候，最后压最低权重任务
最坏等候，避免某项目长期饥饿；逾期任务如实标 `逾期`。

两者最终都以编码序列 `(job_id)` 字典序最小打破完全并列；同一目标下站号小者优，
装载少者优。输入行序不参与排序。至多 8 个任务时穷举全部可行顺序，结果为
该目标下的最优可行顺序。

## 执行输入（export-dispatch）

| 字段 | 说明 |
| --- | --- |
| `queue_id` | 对应已冻结计划 |
| `rows[].id` | 执行日志行号 |
| `rows[].job` | 任务号 |
| `rows[].phase` | `prepare` / `load` / `encode` / `verify` |
| `rows[].station` | 实际工作站 |
| `rows[].state` | `completed` / `interrupted` / `checkpoint` |
| `rows[].start` / `rows[].end` | 实际起止（旧格式用 `at`） |
| `rows[].checkpoint_minutes` | 断点在编码内完成的分钟数 |
| `rows[].resume_from` | 续跑起点，必须与已记录的检查点一致 |
| `failures[].station` / `failures[].at` | 工作站故障站号与时刻 |
| `license_unavailable[]` | 许可临时不可用窗口 |
| `replan` | 出现新硬截止任务时的重新规划说明；旧计划不会被悄悄挤占 |

执行核对规则：

1. 完成时间取 `verify` 结束时刻，不是 `completed`/编码结束时刻；校验被挤到
   截止后仍会标 `verify_after_deadline`。
2. 同一许可的两次编码不得重叠；重叠时后开工者标
   `license_conflict_with:<job>`，不能两个都判完成。
3. 断点续跑必须从已记录的检查点开始；没有完整检查点的半成品不能由第二台
   工作站当作完成，标 `no_checkpoint_for_resume` / `resume_without_matching_checkpoint`。
4. 执行事件按内容 SHA-256 去重，重复导入同一日志不会重复占用许可
   （`duplicate_events` 记录被忽略行数）。
5. A 站故障后迁到 B 站，若 B 的许可正被别的任务占用，改派与冲突分别落库，
   不会把两个任务都判完成。
