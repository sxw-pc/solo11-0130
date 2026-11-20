# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材新增 `plugin-import` 与 `project-open`，从离线编辑器插件清单按名称核对本地工程依赖。入口使用 `--db ledger.db export.json`，可运行样例见 `tests/test_plugin_environment.py`。

## 插件版本、ABI 与离线签发安装

`project-open` 不再只按名称放行：工程可声明版本范围（`>=2,<3` 等）、编辑器 ABI、插件依赖与排斥关系；同名异版只允许一个活动版本。`station-register` 在导入前固定工作站用途——`播出` 环境冻结禁止安装升级，`测试` 可用本地签发包更新并在失败时回滚；用途固定后不可更改。

`package-import` 导入离线签发包，逐包校验 SHA-256 摘要与 HMAC-SHA256 签发、工作站编辑器 ABI；未签发、篡改或 ABI 不符的包一律 `不可选`，且不从网络补包。`plan-solve` 对全部工程（≤8 插件 × 每插件 3 版）求一致版本组合，按最少改动、最少被迫关闭的旧工程排序，动作含卸载、依赖拓扑序安装（共享运行库先装）与保持；无解时给出最小冲突与隔离工作区方案。`plan-apply` 先冻结环境快照再执行，部分失败从快照整体回滚并落 `rollback_record`，成功计划重放不会二次安装。可选插件缺失只压低导出能力：报告把 `readable` 与 `export_complete` 分开。

新增状态表：`station_profile`、`environment_snapshot`、`signed_package`、`plugin_constraint`、`install_plan`、`install_run`、`rollback_record`（另有 `active_plugin` 活动版本表）。离线 JSON 样例与字段说明见 `examples/README.md`，验收测试见 `tests/test_plugin_versioning.py`，覆盖同名异版、共享库冲突、可选功能、旧工程占用、签发摘要、部分安装失败、两种工作站与重放幂等。
