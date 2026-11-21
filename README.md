# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `access-import` 与 `incident-review`，把离线桌面操作日志、审批回执、
人事账号归属、设备登录与项目修订连成可核查的自然人职责链。输入 JSON 以顶层
`kind` 区分证据类别（`hr_directory`、`project_revision`、`permission_contract`、
`approval_receipt`、`emergency_window`、`approval_revocation`、`access_trace`），
可运行样例与字段说明见 `samples/`。

核证规则：账号名不同不等于两个自然人（按人事归属枚举未知账号的同人/不同人划分，
最多六名操作者）；批准 r6 不能放行哈希不同的 r7，分支合并必须按合并版重新批准；
退出登录后超出时钟偏差窗口的遗留动作不归属前一人。受监管项目必须由编辑者之外的
第二名自然人批准同一版；内部演练项目允许事前登记紧急窗口内自批，但须填原因并在
24 小时内由另一人补签。所有相容职责链都违规才定 `确定违规`，部分链违规或证据不足
定 `待调查` 并给出最小缺证，全部合规才 `已排除`。每次证据版本追加 `audit_evaluation`
历史并由 `evidence_change` 记录结论迁移，`incident-review` 据核证结果立案/待补证/
关闭，报告（`report`）回读违规见证或最小缺证且已签出快照不被原地改写。测试覆盖
同人双账号、旧版批准、分支合并、时钟偏差、缺身份、审批撤销、紧急补签、两种合同
与重放，见 `tests/test_access_audit.py`。
