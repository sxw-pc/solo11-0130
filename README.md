# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材另有 `export-queue` 和 `export-dispatch` 两个离线入口，都使用 `--db ledger.db export.json`。旧格式（无 `workstations`/`license`/`contract`）仍按单站 FIFO 预计时段；新格式按合同排程：

- `contract: "live"` 直播交付：硬截止优先，低权重的内部样片宁可不排入；剩余容量再按等候权重分配。
- `contract: "internal"` 内部整理：全部软截止，优先避免某项目长期饥饿，个别逾期如实标 `逾期`。

规划在双站上并行准备项目、串行独占同一把编码许可，只在切换项目时付装载时间；完成时刻取本地校验结束而非编码结束，校验被挤到截止后不会被标按时。合同在入队前固定，之后换合同报 `contract_locked`。至多八个任务时穷举全部可行顺序，取该目标下的最优；完全并列时按编码序列 job id 字典序稳定选择，输入行序不参与。

`export-dispatch` 采入模拟桌面执行日志，核对实际开工、检查点续跑、许可占用与校验结束；同一许可上重叠的两次编码只有先开工者算完成，无检查点的半成品不能被第二站当作完成，重复导入同一日志不重复占用许可。冻结计划、阶段与许可租约、改派和执行差异落在 `export_plan`/`plan_phase`/`license_lease`/`reassignment`/`execution_diff`/`bottleneck`，`report` 可按任务回读被延后或逾期的瓶颈。可运行样例见 `samples/` 与字段说明 `samples/README.md`，测试见 `tests/test_export_planning.py`。
