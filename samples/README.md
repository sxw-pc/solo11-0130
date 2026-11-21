# 三盘分片归档样例

本目录是一次真实离线运行（合成数据，不访问网络）导出的样例，与
`tests/test_archive.py` 使用的格式一致。

## 文件清单

- `project-v1.bin` / `project-v2.bin`：合成的字幕工程导出字节（v2 为 580 字节）。
- `backup-request.json`：`archive-backup` 请求样例。`disks` 列出任务锁定的三盘
  名册（顺序即片号分配：`DISK-A→片0`、`DISK-B→片1`、`DISK-C→片2` 校验片），
  `path` 为 null 表示该盘本轮未挂载。`purpose` 在任务建立时锁定，不得改名。
- `restore-request.json`：`archive-restore` 请求样例，只列当前挂载的盘（C 遗失）。
- `generation-manifest.json`：`archive_generation` 账本记录：工程版本、世代序号
  `seq`、整文件长度 `file_length`、整文件摘要 `file_sha256`、分片长度 `chunk`
  及三片的 `index/disk/length/sha256`。
- `receipts/DISK-*/receipt-*.json`：各盘上的片回执（`kind: shard`），含工程版本、
  片号、片长、片摘要、整文件摘要与状态 `state`（`written` 已写完 /
  `verified` 已校验）。
- `receipts/DISK-*/commit.json`：各盘上的提交回执（`kind: commit`），是该盘对
  整份清单的签收；至少两盘 `verified` 世代才已提交，保全用途要求三盘齐全。
- `restore-proof.json`：`restore_record` 恢复证明：实际用了哪些盘
  （`disks_used`）、哪些片由校验片重建（`shards_rebuilt`，本例 A 盘遗失，
  片 0 由 `DISK-B` 数据片与 `DISK-C` 校验片异或重建）、被拒绝的更新世代及原因
  （`rejected`）、损坏片清单（`corrupt`）。

## 状态与决定规则

- 片：`已写完`（落盘未重核）→ `已校验`（重启核对摘要通过）/ `已损坏`。
- 提交回执：同样区分 `已写完` 与 `已校验`；只有写入完成而未验证的回执
  不计入提交仲裁。
- 世代：`写入中` → `待提交`（三片皆已校验）→ `已提交`（≥2 盘提交回执已验证）。
- 恢复：只选已提交世代；同世代可用片 ≥2 才可重建，缺一片用校验片异或重建并
  回验整文件摘要；两片缺失或剩余片分属不同世代 → `待核`。
- 用途：`disaster_recovery` 凭同世代任意两片恢复；`preservation` 另需三盘
  签收回执都在案，缺一份 → `待补签收`，即使技术上能重建。
