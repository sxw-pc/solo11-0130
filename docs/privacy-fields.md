# 隐私扫描与脱敏样例：字段说明

所有输入均为离线合成 JSON，不含真实个人数据。入口：

```
python -m subtitlehandoff privacy-scan    --db ledger.db source.json
python -m subtitlehandoff privacy-fixture --db ledger.db fixture.json
python -m subtitlehandoff report          --db ledger.db --out report.json
python -m subtitlehandoff privacy-review  --db ledger.db --audience external
python -m subtitlehandoff privacy-export  --db ledger.db --out external.json \
       --audience external --project proj-alpha --vendor vendor-a
```

## 1. 扫描输入 `privacy-scan`

```json
{
  "version": "v1",
  "project": "proj-alpha",
  "authorization": {"audiences": [ ... ]},
  "quasi_fields": ["location", "job", "date"],
  "readability": {"max_suppressed_fields": 2, "suppress_cost": 4,
                  "require_retained_field": true},
  "rows": [ {"id": "C1", "scene": "S1", "speaker": "SPK-A",
             "start": 1.0, "end": 3.0, "text": "..."} ],
  "subjects": [ {"id": "A", "persona_names": ["阿岚"], "scenes": ["S1"]} ],
  "aliases":  [ {"id": "AL1", "alias": "阿岚", "subject": "A", "scene": "S1",
                 "evidence": {"kind": "signed_consent", "ref": "consent-AL1",
                              "authorized_by": "rights-lead",
                              "at": "2026-11-19T10:00:00"}} ],
  "quasi":    [ {"subject": "A", "field": "location", "value": "徐汇滨江",
                 "levels": {"0": "徐汇滨江", "1": "徐汇区", "2": "上海市"}} ]
}
```

| 字段 | 说明 |
| --- | --- |
| `rows[]` | 本地编辑器导出的块顺序。`id` 必填且唯一；`scene` 为场景边界；`speaker` 为说话人代号；`start`/`end` 为秒。**无 `scene` 的块自成场景，不会被并到相邻场景。** |
| `subjects[]` | 已知主体。`persona_names` 为该主体的本名/艺名/邮箱字面量；`scenes` 限定该字面量可出现（并可绑定）的场景。不同场景的同名短名各自绑定，不合并。 |
| `aliases[]` | 经授权维护的别名表。`evidence` 必须含 `kind`、`ref`、`authorized_by`、`at`（ISO 时间）；缺任一项即记 `待核`，绝不按时间远近猜身份。`alias` 只在其 `scene` 内生效。 |
| `quasi[]` | 准标识符。`levels` 是预先声明的有限泛化层级，`"0"` 必须为原值；未提供时使用按字段的可读回退阶梯。`value` 必须属于 `quasi_fields`。 |
| `authorization` | **取样前**确定的两类用途授权，见下节。 |
| `readability` | 可读性约束：`max_suppressed_fields` 每组最多抑制字段数；`suppress_cost` 每抑制一个成员的某字段的代价；`require_retained_field` 禁止整组全部抑制。 |

### 授权

```json
"authorization": {"audiences": [
  {"audience": "internal", "purpose": "...", "authorized_by": "...",
   "at": "2026-11-20T09:00:00"},
  {"audience": "external", "vendor": "vendor-a", "purpose": "...",
   "authorized_by": "...", "at": "2026-11-20T09:05:00"}
]}
```

- `external` 必须带 `vendor`；缺少即 `authorization_vendor_missing`。
- 授权在扫描前读取。未授权的外发用途直接 `待核`，**不会**在生成失败后临时降级成内审输出。
- 缺 `authorization` 的旧版载荷仅按 `internal` 处理（`legacy_internal_default`），外发入口报 `audience_not_authorized`。

## 2. 扫描结果（本地台账）

`privacy_scan` 把下列内容写入本地 SQLite，供有权复核员回读：

| 表 | 内容 |
| --- | --- |
| `privacy_source` | 每个块的原始文本、`findings`（完整邮箱串）、`occurrences`（逐位置片段）、`entity_spans`（同一片段跨哪些块）。 |
| `privacy_entity` | `subject:<id>` 主体画像（关联块、别名、准标识原值）；`pending:<id>` 待核项；`plan:external` 外发等价组方案。 |
| `privacy_alias` | 别名及证据；`authorized` 为假即证据不足。 |
| `privacy_quasi` | 准标识原值与全部泛化层级。 |
| `privacy_authorization` | 扫描前授权的快照。 |
| `privacy_pseudonym` | 按 `subject×audience×vendor` 派生的假名。 |

`occurrence` 字段：`cue`、`segment`（块内行号）、`start`/`end`（该行字符偏移）、`raw`、`kind`（`email`/`persona`/`alias`）、`subject`、`status`（`resolved`/`pending`/`ambiguous`）、`source`（如 `persona:A`、`alias:AL1`、`persona:out_of_scene`）。

外发等价组方案 `plan:external`：

```json
{"status": "ok" | "unsatisfiable",
 "reason": "groups_satisfied" | "group_size_unsatisfiable",
 "groups": [{"members": ["A","B","C"],
             "profile": {"location": "徐汇区", "job": "演员"},
             "cost": 1, "suppressed_fields": 0}],
 "conflict_fields": {"D": ["date"]},
 "skipped": {"D": ["date"]}}
```

- 组内每个已发布字段的取值必须相同，且每组至少 3 名**实际出现在样例中**的不同主体。只出现在邮箱里的联系方式主体不计入组人数。
- 采用集合划分动态规划求全局最少信息损失，**不是**逐字段贪心；不会出现“某字段刚好凑三人、却留下跨字段唯一组合”。
- 找不到满足条件的划分时 `status=unsatisfiable`，`conflict_fields` 给出每个无法成组主体的最小冲突字段集，外发样例整体 `待核`。

## 3. 生成输入 `privacy-fixture`

```json
{"version": "v1", "project": "proj-alpha", "audience": "external",
 "vendor": "vendor-a",
 "rows": [{"id": "X-C1", "source": "C1"}]}
```

`audience` 缺省为 `internal`（兼容旧样例）。`source` 指向已扫描的块 `id`。

生成时：

- 块数、顺序、`start`/`end` 原样保留。
- 跨块片段在**每一个**所在块按字符偏移遮盖；姓名跨两块时两块都不出现姓名。
- 说话人代号替换为按 `audience×project×vendor` 隔离的假名；同一镜头内不同说话人仍可区分，不同供应商不可链接。
- 内审：保留场景间稳定的主体假名（`角色…`）与具体准标识值；直接联系方式仍遮盖。
- 外发：应用 `plan:external` 的泛化/抑制；任一主体无法进入 ≥3 人组、或存在未决标识时，该块 `publish=false` 并写明原因。

`privacy_fixture` 台账每行含 `text`、`cue_index`、`start`、`end`、`speaker`、`publish`、`information_loss`。`information_loss` 项为 `{field, reason}`，`reason ∈ {generalised, field_suppressed, direct_identifier_masked, unresolved_identifier, alias_evidence_insufficient, same_short_name_other_scene, quasi_group_not_satisfied}`。

`privacy_leak_check` 逐受众记录是否泄漏 `leaked`、`clean`、`published_cues`；键含 vendor。

## 4. 报告与回读

- `report.build`：完整台账，附 `privacy.leak_checks`、`privacy.information_loss`、`privacy.external_plan`、`privacy.authorizations`。**不含**原始片段。
- `report.review`（`privacy-review` 命令）：仅限本地有权复核员，回读每个生成行的 `original`、`redacted`、`fragments`（含 `raw` 与来源 `source`）及 `loss`。**不得**用此视图对外。
- `privacy_fixture.export`：对外 JSON，只含 `id/cue_index/start/end/text/speaker/publish/information_loss`、`leak_checks`、`authorized`、`publishable`；不含可逆映射、原始别名、准标识原值或内审假名。

## 5. 稳定性

相同输入与受众重复扫描/生成结果逐字节稳定（假名由 `sha256(audience|project|vendor|subject)` 派生）。换供应商时 `vendor` 进入盐值，假名不同，两个项目不可链接。
