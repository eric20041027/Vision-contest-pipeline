# 量測與融合參考

## 標準預測格式 `runs/<run>/predictions/<subset>.jsonl`
一列一個 sample，依 `sample_id` 排序、LF、UTF-8。det → `boxes`（x, y, w, h, category_id, score, view）；seg → `masks`（`rle` 與 `polygon` 恰一）；cls / multilabel → `scores`（鍵 = card 的類別名）；regression → `targets`。轉換器：`--format jsonl|coco_results|yolo_txt|scores_csv`，比賽格式以 `--plugin` 登記 `register_converter`。

## 命令與主要選項
| 命令 | 必填 | 其餘 |
|---|---|---|
| `eval ingest` | `--run --dataset --plan --subset --format --src` | `--export-manifest`（推 `trained_on`）、`--trained-on`（無 export 時）、`--weights`（run 身分 = weights_hash）、`--config`、`--framework`、`--notes`、`--keep-input`、`--replace`（舊 sha 進 `history.jsonl`）、`--opt k=v`、`--receipt <artifact id>`、`--plugin` |
| `eval measure` | `--run` | `--metrics`（預設全部適用）、`--subsets`（預設乾淨 eval 子集）、`--params k=v`、`--unseal --reason`、`--plugin` |
| `eval anchor` | `--run --subset --metric` | `--params`、`--tolerance`（有限且 ≥ 0）、`--replace`（先寫 `anchors.log.jsonl`） |
| `eval sigma` | `--dataset --plan --metric` | `--method splithalf|bootstrap|prior`（自訂以 `register_sigma_method`）、`--params`、`--subsets`、`--run` |
| `eval preregister` | `--dataset --id --claim --component --class model|tuning --baseline-run --candidate-run --metric` | `--params`、`--subsets`、`--t-min`（預設 2.0）、`--min-bases`（預設 2）、`--sigma-method`、`--sigma-ratio` |
| `eval judge` | `--dataset --prereg` | `--resamples`（200）、`--seed`、`--strict`（非 PASS 即 exit 1）、`--unseal --reason` |
| `eval status` | `--dataset` | `--max-age-hours` |
| `eval report` | `--dataset` | `--metric`、`--plan` |
| `fuse recipe` | `--dataset --id --plan --method wbf|mean|rank_mean --member RUN[:W]…` | `--params k=v`；成員順序有意義 |
| `fuse build` | `--dataset --recipe` | `--run`、`--subsets`、`--replace`（`fuse.json` 遺失時重建全部子集） |
| `fuse ablate` | `--dataset --recipe` | `--preregister --metric`、`--metric-params`、`--bases`、`--t-min`、`--min-bases`、`--no-build`、`--replace` |

## 判決記錄（`judgements.jsonl`）
配對 bootstrap → Δ、se、t、基底數、σ_p 條件 → `verdict=PASS|FAIL|INVALID` + `reason`。判決同時記錄候選的 provenance 等級（`receipt > export > declared`）；`submit.yaml` 的 `require_provenance` 決定 stage 時是否擋低等級。`min_bases` 預設 2：少於兩個 eval 子集時不會自動成立，要在 RUNBOOK 記錄放棄了什麼保證。

## σ_p
`splithalf`（同一 run 切半的離散度）、`bootstrap`（`--run` 預設取該 claim 的候選）、`prior`（`--params` 給值，必須寫理由）。`--class tuning` 的主張要求 Δ 超過 `sigma_ratio × σ_p`，`model` 類不看 σ_p。

## 融合器參數
`wbf`（boxes）：`iou`、`skip`（輸入框門檻）、`min_score`（融合後門檻）、`max_per_image`、`conf_type=avg|max`；像素座標、依 (view, category) 分組。`mean` / `rank_mean`（scores）。自訂融合器 `register_fuser`，以 `--plugin` 匯入。

## 收據與 provenance 等級
- `receipt`：訓練迴圈用 `MaterializedReader(name, mode, plan_id=, subset=)` 或 `Session.current().access(subsets=)`，或 `ingest --receipt` 綁外部收據；收據記實際讀到的子集與 ID 集合 sha，未授權列 `denied:`。
- `export`：`vcp data export` 的 manifest（每次匯出自帶一份 access_receipt）。
- `declared`：只有 `--trained-on`。
`vcp eval status --dataset D` 顯示每個 run 的等級。

## 常見 VERDICT 欄位
measure：`readings=N contaminated=… guardrail=OK|FAIL`；judge：`verdict= reason= delta= t= bases=`；ablate：寫出的配方與預登記 id 清單；build：`subsets= members=`。
