# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐检查段落数量与时间漂移，客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

入口 `track-import`、`review-decide`、`alignment-evaluate`、`rights-register`、`delivery-evaluate`、`rights-revoke` 均接收 `--db ledger.db export.json`。顶层 JSON 含 `version`，桌面导出的原始字节和 SHA-256 存在 SQLite，报告输出来源、持久化实体和业务决定。每条交付轨必须有通过的审校记录；新审校可通过 `supersedes` 解决同轨旧问题，但不能省略旧问题的证据。三语言及以上交付要求对齐关系连通全部轨道。测试涵盖媒体错版、负时长/重叠、严重意见未关闭与修订恢复、三语言对齐缺口、缺审校、缺语言、授权禁发期、过期、撤销和错版回执。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m subtitlehandoff demo`，`python -m subtitlehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材新增 `cache-import` 与 `cache-prune`，从离线编辑器缓存清单按最后使用时间标记预览缓存淘汰。入口使用 `--db ledger.db export.json`，可运行样例见 `tests/test_cache.py`。

## 可执行预览缓存清理与任务准入

v2 清单在原 `id/size_mb/last_used` 之外采录真实文件路径与 SHA-256、同内容副本的物理身份（`physical_id`，字节只计一次）、生成依赖、活动审校引用、生成耗时、临时工作空间与下一个预览任务；缓存目录外的耐久工程归档登记为受保护对象，永不清理。

`cache-prune` 对不超过十个缓存节点、两个预览任务穷举符合依赖先后、活动引用、任务开始时刻与磁盘峰值（含生成临时空间）的删除/重算序列，在可执行序列中选重算耗时最短者，报告可启动结论、逐步顺序与最大占用；无法启动时给出最少缺额峰值空间与阻碍步骤。`execute: true` 时逐步核对文件仍在缓存根、摘要未变、无新增占用，真实移走成功后才落账已释放字节；外部改写或移走失败会停步并按残存文件重算，重放同一请求不重复计账。两种事前选定用途：`online_review` 保留当前场景 A 并强制三分钟可看，`night_batch` 可清 A 但通过 `protected` 保留下一批输入 B；用途在请求中固定，计算失败后不会更改。

- 字段与语义：`docs/cache_prune_fields.md`
- 离线合成文件与 JSON 清单：`examples/cache_prune/`（`python3 examples/cache_prune/build_examples.py` 生成，随后用 `cache-import`/`cache-prune`/`report` 入口运行）
- 验收测试：`tests/test_cache_admission.py`，覆盖共享父资产、同内容不同名、活动引用、临时峰值、外部改写、两种用途、删除失败、重放与报告回读。
