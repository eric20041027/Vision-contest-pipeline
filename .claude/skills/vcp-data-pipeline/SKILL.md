---
name: vcp-data-pipeline
description: Use when taking a contest's raw data through this repo's data layer (vcp data import / audit / split / export / materialize / lineage), when a vcp command ends with WARN, FAIL or ABORT and you must decide what to do, or when you need an importer's or exporter's --opt keys (categories=, on_bad_row=, view=, copy=, labels_csv=, ...).
---

# vcp 資料層操作

## 核心原則
每個命令以 `VERDICT cmd=<名> status=OK|WARN|FAIL|ABORT k=v ...` 收尾（exit 0 / 0 / 1 / 2；`--json` 時 JSON 到 stdout、VERDICT 到 stderr）。
WARN 是「要你看一眼再決定」，FAIL 是「資料有問題，命令沒動手」，ABORT 是「環境或程式問題」。不要為了消掉 WARN 調參數；先讀欄位。
`--opt` 的 key 不在 `--help` 裡，在 [reference.md](reference.md)。`--src` 可以是任何目錄；資料根目錄 `raw/<name>/` 只是慣例，永不修改。`--opt` 指到的輔助檔可用絕對路徑，不必塞進 raw。

## 標準流程（一場比賽）
```bash
uv run vcp data import --importer csv_boxes --src <raw dir> --name <ds> --license "<文字>" --url <url> --downloaded-at <UTC 日期> --opt csv=<檔> --opt images=<目錄> --opt categories=<json> --opt on_bad_row=skip
uv run vcp data audit --name <ds> --against <test ds> [--max-bad-boxes <rows_skipped>]
uv run vcp data split --name <ds> --plan-id fixed-v1 --group-from-audit
uv run vcp data export --name <ds> --plan fixed-v1 --subset train --format yolo --out <空目錄>
uv run vcp data materialize --name <ds> --mode png --resize 256
uv run vcp data lineage --name <ds> --plan fixed-v1 --trained-on train,valA
```
順序有意義：audit 先於 split（`--group-from-audit` 讀 `cache/audit/groups.json`，把近重複綁成同一切分單位）；plan 檔進 git 後不可改，要改就換 `--plan-id`。`materialize` 作用於整個資料集（沒有 `--plan` / `--subset`），子集由訓練層拿 plan 的 assignment 去對 manifest。

## 判決速查
| 看到 | 意思 | 處置 |
|---|---|---|
| import FAIL `box exceeds image bounds` / `unknown image` | csv_boxes 預設 `on_bad_row=abort` | 加 `--opt on_bad_row=skip`，WARN 帶 `rows_skipped=N`，原因在 `cache/import_skipped.jsonl` |
| audit.coords FAIL `import_skipped=N max_bad=0` | 壞框預算 = `out_of_bounds` + `import_skipped`（`suspicious` 只會 WARN，不算在內）；匯入時跳過的列也算，這是刻意的第二道關 | 確認 N 就是你已看過的壞列後 `--max-bad-boxes N`；不要開大到蓋過其他問題 |
| audit.dedup WARN `overlap_pairs` | 訓練圖與 `--against` 資料集近乎相同（`cache/audit/overlap.jsonl`） | 記下來、決定策略；切分擋不了 test 洩漏 |
| audit.dedup WARN `dup_groups` | 資料集內近重複（`cache/audit/groups.json`） | split 加 `--group-from-audit` |
| import WARN `exif_rotated=N` | 有 EXIF 方向 ≠ 1 的圖 | 抽幾張人工核對框在哪個空間；政策見 reference |
| import WARN `plans_invalidated=N` | 重匯改了 samples_hash，舊 plan 失效 | 用新的 `--plan-id` 重切 |
| split WARN `empty_subsets=` | 樣本太少撐不起四個子集 | `--subsets train:train:0.8,val:eval:0.2` 之類 |
| export WARN `images=copied` | Windows 無 symlink 權限，已自動複製 | 正常；`--opt copy=true` 可直接複製 |
| export ABORT `pass --opt view=` | 多 view 樣本 | `--opt view=<索引或 role>` |
| lineage `clean=` | 這個模型還能用哪些驗證集 | holdout 帶 `(sealed)`：只能 `--unseal --reason` 開一次並留痕 |

## 無標註資料（test 集）
沒有匯入器直接吃「純影像資料夾、無標籤」。正確做法是 jsonl 直通、`label_source: none`（食譜在 reference.md），不要用空 CSV 走 csv_boxes（那會變成 gold 的負樣本）。

## 常見錯誤
- `--opt categories=` 只吃 JSON 陣列或 JSON 檔（`[{"id":0,"name":"bottle"}]`）；`classes.txt` 要先轉。
- `--group-from-audit` 只用 `--name` 資料集自己的近重複群，跟 `--against` 的重疊無關。
- lineage 只看 sample_id 集合互斥，對像素重複是盲的；它的可信度來自前面的 audit + `--group-from-audit`。
- 匯出目錄必須是空的；`manifest.json` 是訓練層宣告「用什麼訓的」的憑證，不要手動改。
