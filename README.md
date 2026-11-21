# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `qa-plan` 与 `qa-execute`，采录离线模拟桌面自动化用例。升级版不再
只取前三条、不再以 `passed:true` 判定通过，而是以显式三状态界面图（未保存项目、
导出/发布弹窗、发布终态，最多 10 状态 12 转移 3 高风险目标）驱动：

- `qa-plan` 对每个候选用例按状态图逐跳重放，校验起始快照、弹窗必须先处理、终态
  无出边，同名按钮按窗口状态消歧，标签多报不计入覆盖；在副本与运行时间上限内求
  最短可行脚本组，并给出未覆盖目标的最小阻碍。
- `qa-execute` 对模拟录制逐帧重放：窗口前后状态、按钮、动作后可见证据与帧号缺一
  不可。漏“发布确认”弹窗即使 `passed:true` 也改判退回；停在导出失败弹窗给“待续跑”
  并允许从合法检查点续跑；已执行的不可逆发布不二次运行，同一执行日志重放幂等且不
  再消耗工作区副本。
- 两类合同在录制前固定：`smoke` 允许保存草稿与发布在一份工作区连续覆盖（各转移
  独立检查点）；`audit` 要求每个不可逆目标与草稿对照路径各自从干净快照独立跑，
  副本不足时整体报缺、不降级宣称覆盖。
- `store.py` 持久化状态图、快照、副本消耗、计划路径、逐步证据与未覆盖目标；
  `report.py` 的 `qa_coverage` 把每条覆盖结论回溯到具体合法动作证据。

离线样例见 `samples/`（`qa_plan_smoke.json`、`qa_plan_audit_one_copy.json`、
`qa_plan_audit_three_copies.json`、`qa_replay_*.json`、`QA_FIELDS.md` 字段说明）。
测试覆盖同名按钮、失败弹窗、发布终态、缺帧、资源上限、两类合同、合法续跑与执行
重放。运行 `python -m pytest -q`（无 pytest 的标准库环境会使用仓库内的轻量兼容
运行器）。
