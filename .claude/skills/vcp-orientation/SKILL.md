---
name: vcp-orientation
description: Use at the start of any session in the vcp repository, or whenever an agent or person needs to understand what vcp is, how its layers, ledgers, VERDICT contract and immutability rules fit together, where files live, which document is authoritative for a question, or which vcp-* skill to load next.
---

# 三分鐘理解 vcp

## 一句話
vcp 管**流程、身分、證據與治理**：資料 → 切分 → 訓練 → 量測 → 融合 → 提交 → 備份，每一步留下可驗證、不可竄改的紀錄。模型、訓練迴圈、比賽格式都在 `projects/<contest>/`，`src/vcp` 永遠不出現比賽名。

## 八層與它們留下的證據
| 層 | 命令群 | 留下什麼 |
|---|---|---|
| 資料 | `vcp data import/validate/audit/split/lineage/export/materialize/diff` | card + `samples.jsonl`、不可變 plan、export manifest、`source_audit`、`dataset_diff` |
| 量測 | `vcp eval ingest/measure/anchor/sigma/preregister/judge/status/report` | run 預測檔、`readings.jsonl`、預登記（git）、`judgements.jsonl` |
| 融合 | `vcp fuse recipe/build/ablate` | 配方（git）、`runs/fuse-<id>/fuse.json` |
| 訓練 | `vcp train run/upload/status` | `run.yaml`、`train.yaml`、checkpoint sha、access receipt |
| 提交 | `vcp submit init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report` | `submit.yaml`、`submissions.jsonl`、`stage.json` |
| 備份 | `vcp backup manifest/push/verify/pull/status` | manifest（git）、`backup.log.jsonl` |
| 產物 | `vcp artifact create/show/verify/lineage/status/relink/clean` | `artifacts/<kind>/<id>/manifest.json`（有它才是產物） |
| provenance | `vcp provenance rebuild/sync/ingest/impact/stale/explain/status/verify-index` | 可刪除的衍生索引（SQLite 預設；PostgreSQL optional） |

## 三條鐵則與 VERDICT
1. 取時只用 `vcp.core.time.utc_now()/stamp()`。
2. 每個命令以 `VERDICT cmd=… status=OK|WARN|FAIL|ABORT k=v…` 收尾：OK/WARN exit 0、FAIL 1（資料或行為不合格，沒動手）、ABORT 2（環境或程式）。`--json` 時 JSON 到 stdout、VERDICT 到 stderr。命令永不互動提問。
3. 核心 venv 只裝 vcp；訓練框架各自 venv、editable 裝 vcp；量測 venv 凍結後禁 install。

`judge status=OK` 只代表判決完成，準入看 `verdict=PASS|FAIL|INVALID`。integration 測試 `skipped` 不是 pass。

## 四種不可變等級
- **永不改**：`raw/<name>/`。
- **只增**：`readings/judgements/sigma/submissions/backup.log/history/prereg.log/supersession.jsonl`。
- **寫一次不改，要改就換 id**：plan、預登記、融合配方、backup manifest、每個 artifact 的檔案。
- **換寫留痕**：`run.yaml`、預測檔（`--replace` 舊 sha 進 `history.jsonl`）、`anchors.json`（先寫 `anchors.log.jsonl`）。

## 怎麼證明「做了」
文件與 RUNBOOK 是主張，不是證據。證據 = 該命令的 VERDICT + 磁碟上的產物 + 台帳列 + sha。例：「baseline 量過 valA/valB」要在 `vcp eval report --dataset D` 看到該 run × valA、valB 的讀數列（或 `measure/D/readings.jsonl` 有列），不是 RUNBOOK 寫了就算。核對用唯讀命令：`eval status/report`、`train status`、`submit status/report`、`backup status`、`artifact status`、`provenance status`。

## 哪裡找答案（由權威到背景）
`uv run vcp <group> <cmd> --help` → `docs/reference/cli.md` → `docs/guides/*` → `docs/superpowers/specs/*-design.md` → 對應 plan 的 `*-followups.md`（裁決與待辦）→ `docs/handover/HANDOVER.md`。比賽現況只看 `projects/<contest>/RUNBOOK.md` 與台帳。細節地圖在 [map.md](map.md)。

## 接下來讀哪個 skill
| 情境 | skill |
|---|---|
| 跑或接手一場比賽 | `vcp-running-contests` |
| 新比賽 Day 1 | `vcp-contest-onboarding` |
| 匯入 / 稽核 / 切分 / 匯出，或 data 命令的 WARN/FAIL | `vcp-data-pipeline` |
| 預測 → 讀數 → 預登記 → 判決；融合準入 | `vcp-eval-and-fuse` |
| 訓練包裝、checkpoint、提交、備份 | `vcp-train-submit-backup` |
| 資料集改版、provenance 索引、PostgreSQL、基準量測 | `vcp-provenance` |
| 發版、venv / worktree、訓練中能不能動 main、PR 慣例 | `vcp-release-and-environments` |
| 新增匯入器 / 指標 / 融合器等登記項 | `vcp-extend-registry` |
