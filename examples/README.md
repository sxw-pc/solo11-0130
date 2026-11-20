# 离线插件环境 JSON 字段说明

所有输入均为本地离线 JSON，顶层必须含字符串 `version`；原始字节与 SHA-256
存入 SQLite `export_file`，不访问网络。样例见本目录 `*.json`。

## 1. 工作站用途 `station-register`

```json
{"version": "station-1", "rows": [{
  "id": "station-test",
  "purpose": "测试",
  "editor_abi": "abi-9",
  "signing_key": "studio-offline-key-test-2026",
  "note": "可选说明"
}]}
```

| 字段 | 含义 |
| --- | --- |
| `id` | 工作站标识，导入前固定 |
| `purpose` | 仅允许 `播出`（环境冻结，禁止安装/升级）或 `测试`（可本地签发升级，失败回滚） |
| `editor_abi` | 该工作站编辑器 ABI；ABI 不符的签发包不可选 |
| `signing_key` | 离线 HMAC-SHA256 预置密钥；用途固定后禁止更改 |

同 `id` 重放：相同正文幂等；`purpose` 变化直接拒绝（`station_purpose_change_forbidden`）。

## 2. 插件清单 `plugin-import`

```json
{"version": "inv-2026.1", "rows": [{
  "id": "I-OCR-1", "name": "OCR", "release": "1", "abi": "abi-9",
  "requires": {"runtime": "1"}, "conflicts": [], "active": true
}]}
```

| 字段 | 含义 |
| --- | --- |
| `name` / `release` | 插件名与版本号；同名插件只允许一个活动版本 |
| `abi` | 该版本构建对应的编辑器 ABI |
| `requires` | 依赖映射：`{插件名: 版本范围}`，空对象表示无依赖 |
| `conflicts` | 排斥列表：`[{"name": "x", "spec": "==2"}]`，命中即冲突 |
| `active` | 是否活动版本；同名第二个活动行记为 `未激活/duplicate_active_version` |

同一 `id` 的正文变化触发 `identity_conflict`，清单证据不可被静默改写。

## 3. 离线签发包 `package-import`

```json
{"version": "pkgs-2026.1", "rows": [{
  "id": "station-test:pkg-ocr2", "station": "station-test",
  "name": "OCR", "release": "2", "abi": "abi-9",
  "requires": {"runtime": "2"}, "conflicts": [],
  "payload_path": "offline/ocr-2.zip", "payload_sha256": "…64 位十六进制…",
  "digest": "…对除 digest/signature 外字段规范化后的 SHA-256…",
  "signature": "…HMAC-SHA256(signing_key, digest)…"
}]}
```

- `digest`：对除 `digest`、`signature` 外全部字段做
  `json.dumps(..., sort_keys=True, separators=(',', ':'))` 后取 SHA-256。
- `signature`：用工作站 `signing_key` 对 `digest` 做 HMAC-SHA256。
- 制备签发用 `plugin_inventory.sign_package_row(row, key)`。
- 校验顺序：缺签发 → `package_unsigned`；摘要不符 → `digest_mismatch`；
  HMAC 不符 → `signature_invalid`；ABI 不符 → `abi_mismatch`。任一不过即 `不可选`。
- 安装执行前会对整组包再校验一次，防止入库后被篡改。

## 4. 工程需求 `project-open`

```json
{"version": "proj-2026.1", "rows": [{
  "id": "P-NEW", "name": "新字幕工程",
  "required_plugins": [
    {"name": "OCR", "spec": ">=2,<3", "optional": false, "feature": "OCR 识别"},
    {"name": "ocr-pro", "spec": ">=1", "optional": true, "feature": "高级校对"}
  ]
}]}
```

- 兼容旧格式：`required_plugins` 元素可以是纯字符串（任意版本、必选）。
- `spec` 版本范围支持 `> >= < <= == =`，逗号分隔取交集；空/缺省匹配任意版本。
- `optional=true` 缺失只压低导出能力：工程状态为 `可读导出受限`，
  `readable=true`、`export_complete=false`，不阻断打开。
- 核验状态：`可打开` / `可读导出受限` / `待处理`（版本、ABI、依赖、排斥冲突）/
  `待安装`（连名称都缺失）。

## 5. 安装计划 `plan-solve` / `plan-apply`

`plan-solve --db … --station … [--projects P1,P2]` 对全部工程求一致组合：

- 规模上限：不超过 8 种插件、每插件 3 个本地版本，笛卡尔枚举后按
  依赖闭包、排斥关系、工程版本范围过滤。
- 排序键：① 相对当前活动环境的版本变更数最少；② 被迫无法打开的旧工程最少；
  ③ 可选降级最少；④ 组合字典序（确定可重放）。
- 计划含 `actions`：`卸载` → 按依赖拓扑 `安装`（被依赖的运行库先装）→ `保持`。
- 无解时返回 `feasible=false`、`isolated_projects` 与双工作区方案
  （`main` 用新组合，`isolation` 沿用冻结时的当前环境）及 `offline_gap`；
  绝不联网补包。
- `plan-apply <plan_id> [--fail-step N]`：先冻结环境快照，再逐步执行；
  任一步失败（`--fail-step` 为离线演练注入点）即从快照整体还原，落
  `rollback_record/已回滚`，不允许“新运行库配旧插件”的混合态。
- 成功计划重放抛 `install_already_completed`；回滚后允许用新 `install_run` 重试。
- 播出工作站执行安装抛 `station_frozen`，环境保持原样。

## 6. 报告 `report`

`plugin_environment` 派生区段：`active_plugins`、`stations`、`packages`、
`projects`（含 `readable`、`export_complete`、`unsatisfied_required`、
`missing_optional`、`conflict_basis`）、`snapshots`（保留冻结时的清单版本）、
`plans`、`runs`、`rollbacks`。新增实体表同时出现在 `entities` 下。
