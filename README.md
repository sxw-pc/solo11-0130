# 字幕交付采录台

后期团队从本地字幕编辑器导出的工程包采录媒体版本、语言轨与逐句时轴；独立审校系统记录问题定位、严重度与修订；语言交付系统冻结轨摘要和客户回执。跨语言对齐在编辑器导出的**翻译段落标识**上证明共同分段，并核对镜头边界与逐句时间证据；客户授权系统核对语言范围和使用窗口。任一时轴、审校、对齐、段落映射或授权证据不足都会改变交付结论，授权撤销还会把已交付包转入待回收。

## 为什么不再按行号或轨道图对齐

一条中文字幕块可以连续覆盖两个翻译段落（合句），英文、法文可以按停顿拆成两块（拆句），同一相邻段落也可能由相邻几条字幕共同表达。按序号 `zip` 只能在条数相等时比较时间，条数不同即退回；只检查两轨对齐记录是否把各语言“连通”，也不能证明三条轨道说的是同一组连续段落。系统因此以编辑器导出的规范分段（段落 ID，如 `U1`、`U2`）为唯一共享分段：

- 对应关系必须保持影片顺序：不得跳过中间段落，不得反向配对，两句文字完全相同也不能跨段落就近贪心配对；
- 一个字幕块在任何语言里都只能指向同一组连续段落，不存在互相矛盾的第二套指向；
- 每对轨允许连续拆句/合句，但全部交付语言最终必须共享同一套完整分段，找不到共同映射时输出冲突段落与来源，不以“轨道图连通”代替对齐证明；
- 镜头切换处不允许用跨镜头合并掩盖下一镜头缺句：跨镜头块必须为每个覆盖段落携带落在所属镜头内的逐句证据，否则记 `cross_shot_merge_gap`；恰好在镜头边界起止的块正常接受。

## 离线 JSON 输入说明

所有命令均为 `python -m subtitlehandoff <command> --db ledger.db export.json`，顶层 JSON 必须含字符串 `version`。原始字节与 SHA-256 冻结在 SQLite，报告回读全部来源、实体与业务决定。

### 1. `track-import`（desktop_subtitle_project）

在原有字段外新增三个可选字段：

- `shots`：镜头边界列表，`{"id":"S1","start":0,"end":10}`；
- `paragraphs`：规范翻译分段（全部语言共享），`{"id":"U1","shot":"S1"}`，顺序即影片顺序；
- 每条 cue 内的 `paragraphs`：该块覆盖的段落标识与逐句时间证据 `{"id":"U1","start":1.0,"end":3.5}`。逐句证据必须落在 cue 区间内；一条 cue 可列一个或连续几个段落，相邻 cue 也可重复同一段落（共同表达）。

含 `paragraphs` 的工程会逐轨冻结 `paragraph_evidence`（状态 `证据有效`/`证据冲突`），冲突代码包括 `paragraph_order_conflict`、`paragraph_missing`、`paragraph_unknown`、`paragraph_evidence_incomplete`、`sentence_evidence_drift`、`cross_shot_merge_gap`。无段落证据的旧轨仍走原有时间轴校验与逐 cue 对齐回退路径。

### 2. `alignment-evaluate`（desktop_alignment_request）

行格式不变：`{"id","left","right","max_total_shift"}`。两轨都有段落证据时进入段落模式：逐段落比较两侧逐句时间（`|Δstart|+|Δend|`），允许任意拆句/合句，通过原因记 `paragraph_correspondence_aligned`，逐段落漂移记 `paragraph_timing_drift`，对齐对中冻结每个段落两侧覆盖的 cue 索引与区间。局部对齐只回答“同 ID 段落时间是否吻合、覆盖是否齐全”；顺序由交付时的全局共同映射裁决，因此局部时间全过仍可能整体拒发。无证据轨保留 `zip` 回退（条数不等记 `cue_count_mismatch`）。

### 3. `service-register`（desktop_service_policy）

事前冻结服务口径，交付时只能选择已注册口径，不能临时改口径：

```json
{"version":"v1","rows":[
  {"id":"SVC_STANDARD","name":"standard_subtitle_service","sentence_cue_required":false},
  {"id":"SVC_ACCESSIBLE","name":"sentence_by_sentence_accessibility","sentence_cue_required":true}]}
```

普通服务允许镜头内合句；无障碍逐句提示服务要求每个段落至少有一条只含该段落的独立 cue，不满足则交付结论为 `返修 / sentence_cue_required`，需返修字幕轨而非改服务口径。

### 4. `review-decide`、`delivery-evaluate`

审校字段不变（`track`、`track_version`、`severity`、`closed`、`cue_index`、可选 `supersedes`）。交付行新增 `service`（已注册服务 ID）。多语言交付门依次要求：轨齐全、语言齐全、无退审意见、**每对语言轨都有已对齐记录**、规范分段证据齐全且存在共同顺序映射、服务门通过、审校通过、授权与回执哈希通过。冻结内容：

- `paragraph_correspondence`：每个交付包的共享映射，逐段落回读各语言块（cue 索引）覆盖的段落及轨版本；
- `mapping_conflict`：冲突段落、来源轨与拒绝理由（共同映射失败、服务门拒绝等）；
- `delivery_package` / `client_ack`：含所选服务与回执摘要（状态不写入冻结体，重放同一包幂等）。

迟到的编辑修订以**新轨 ID/新版本**采录，只影响使用新轨的新交付包；已签出的旧包重放时身份保护保证其逐段证据与冻结体不变。

### 5. `rights-register` / `rights-revoke`

字段与行为不变：语言范围、媒体哈希、授权窗口；撤销会把同客户已交付包转为 `已撤回` 并登记 `delivery_recall`。

## 样例与脚本

`examples/` 提供离线 JSON 样例：三轨 U1/U2 拆合句、法文段序倒置、镜头边界（恰切边界与跨镜头掩盖缺句）、重复文本与同刻相邻块、两种服务口径、三对轨对齐请求、两种服务的交付包。运行：

```
python3 -I scripts/run_examples.py
```

输出 `build/examples/ledger.db` 与 `report.json`：普通服务 `已交付`，无障碍服务 `返修/sentence_cue_required`。

## 测试

`python -m pip install . --no-deps --no-build-isolation` 后 `python -m pytest -q`。测试覆盖拆句合句、三轨传递（法文倒置）矛盾、镜头边界、重复文本、同刻相邻块、逐句时间漂移、轨修订后旧包证据不变，以及两种事前服务的不同交付结果；另含直接驱动 `examples/` 样例的端到端测试。全部使用虚构本地文件，不访问网络。
