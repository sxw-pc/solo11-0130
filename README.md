# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `privacy-scan`、`privacy-fixture`、`privacy-review`。扫描理解本地编辑器导出的块顺序、场景边界与说话人代号，跨相邻块还原被换行拆开的邮箱、姓名与艺名；姓名只在声明场景内绑定，同名不同场景不合并；别名仅在别名表证据完整时绑定，否则列 `待核`，不按时间猜身份。外发样例在块数、顺序与起止时刻不变的前提下，对预先声明的准标识字段做有限层级泛化或整字段抑制，用集合划分动态规划找到每组至少三名不同主体、且满足可读性约束的全局最少信息损失方案（非逐字段贪心），无法满足时给出最小冲突字段并整体 `待核`。内审保留稳定假名与具体场景信息，外发假名按项目与供应商隔离。`report` 回读泄漏检查与信息损失原因，`privacy-review` 仅在本地向复核员回读原片段与脱敏片段。字段与样例见 `docs/privacy-fields.md`、`samples/`。
