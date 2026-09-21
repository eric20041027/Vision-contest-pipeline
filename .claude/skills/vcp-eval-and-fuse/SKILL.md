---
name: vcp-eval-and-fuse
description: Use when turning model predictions into vcp readings and judgements (vcp eval ingest / measure / anchor / sigma / preregister / judge / status / report), when deciding whether a candidate or a fusion member is admitted, when a judge returns FAIL, INVALID, contaminated, no_sigma or candidate_measured, or when writing, building or ablating a fusion recipe with vcp fuse.
---

# 量測與融合：從預測到判決

## 核心原則
- **先寫死主張，再量候選**：`preregister` 必須早於候選的任何 `measure`；判決永遠不是 max-of-N。
- **乾淨基底**：一個 run 只能在它沒碰過的子集上量。乾淨 = `trained_on ∪ 收據觀測到的子集` 都不含；`measure --subsets` 點到讀過的子集 → `contaminated:`，judge 對這種 run 直接 `INVALID`。
- **sealed holdout** 只在最終決策前 `--unseal --reason` 開一次，全程留痕；平時只用 valA / valB。
- 比賽官方計分器、格式轉換、自訂 σ_p 都放 `projects/<contest>/`，以 `--plugin projects.<contest>.metrics` 匯入。
- 選項以 `uv run vcp eval <cmd> --help` 為準；欄位語意見 [reference.md](reference.md)。

## 標準流程（一個候選）
```bash
uv run vcp eval ingest --run base --dataset D --plan fixed-v1 --subset valA --format yolo_txt --src <preds> --export-manifest <export dir> --trained-on train   # valB 再一次
uv run vcp eval measure --run base
uv run vcp eval anchor --run base --subset valA --metric coco_map                       # 之後每次 measure 先過護欄
uv run vcp eval preregister --dataset D --id p1 --claim "…" --component backbone-v2 --class model --baseline-run base --candidate-run cand --metric coco_map
uv run vcp eval measure --run cand
uv run vcp eval judge --dataset D --prereg p1 --strict                                   # exit 1 除非 PASS
uv run vcp eval status --dataset D                                                       # 孤兒預登記、錨點、σ_p
```
`--class tuning` 的主張先要 `vcp eval sigma --method splithalf|bootstrap|prior`，否則 `FAIL reason=no_sigma`。訓練迴圈若用 `MaterializedReader` / `Session.access`，收據自動掛上；外部產生的預測用 `ingest --receipt <id>` 綁收據，`run.yaml` 的 `access` 等級 `receipt > export > declared`。

## 融合準入（有它 vs 沒它）
```bash
uv run vcp fuse recipe --dataset D --id r1 --plan fixed-v1 --method wbf --params iou=0.6 --member a --member b:0.5
uv run vcp fuse ablate --dataset D --recipe r1 --preregister --metric coco_map   # 寫 r1-minus-*、建 fuse-r1 與變體、寫 r1-admit-*
uv run vcp eval measure --run fuse-r1 && uv run vcp eval measure --run fuse-r1-minus-a …
uv run vcp eval judge --dataset D --prereg r1-admit-a                            # PASS = a 值得留；FAIL = 降權或移除 → 新配方 id
```
**先 ablate 再 measure**：完整配方量過就不能再寫準入預登記（`candidate_measured`）。配方寫了不改；成員重新 ingest 後要換配方 id。

## 判決速查
| 看到 | 意思 | 處置 |
|---|---|---|
| `judge status=OK verdict=FAIL` | 判決完成，t < `t-min` 或基底數不足 | 不準入；換候選，不調門檻 |
| `verdict=INVALID contaminated:<run>/<subset>` | 候選或基準讀過主張子集 | 該 run 不能當這個主張的證據；重訓或換基底 |
| `FAIL reason=no_sigma` | tuning 主張沒有 σ_p | 先 `eval sigma` |
| `FAIL candidate_measured` | 預登記寫在候選讀數之後 | 主張作廢。「同一份權重換個 run id 再 ingest」是事後預登記的變裝，一樣禁止（身分靠 `weights_hash`）；要重來就訓練新的候選，或把這次當探索、不提主張 |
| measure `contaminated:` | `--subsets` 指到讀過的子集 | 去掉該子集 |
| anchor 護欄 FAIL | 基準讀數偏離錨點超過 tolerance | 先查環境 / 資料 / 指標實作，不覆寫錨點 |
| `eval status` 有孤兒預登記 | 寫了沒判 | 判掉或留著，不刪 |

## 絕不做
事後預登記（含把量過的權重換 run id 重 ingest）；看到分數再放寬 `t-min` / `min-bases`；用 sealed 挑模型；把 `status=OK` 說成通過；手改 `readings/judgements/sigma.jsonl`；為了消 WARN 改指標參數。

## 常見錯誤
- cls / multilabel / regression 每個 sample 都要有一列，缺列即 FAIL；det / seg 缺列視為零預測。
- `--export-manifest` 給錯目錄會讓 `trained_on` 推錯，之後每個乾淨基底都錯。
- 融合 run 是普通 run：`trained_on` 取成員聯集，量測前先確認它仍有乾淨基底。
