# vcp — vision contest pipeline

可重複使用的影像競賽框架：標準資料格式、多重驗證集切分、lineage、進場稽核、materialize 快取；量測層接標準預測格式、指標、護欄、σ_p、預登記與判決，提交治理留後續子專案。設計文件見 `docs/superpowers/specs/`，操作慣例見 `CLAUDE.md`。

```bash
uv sync                      # 核心 venv；DICOM 支援：uv sync --extra dicom
uv run vcp --help
uv run pytest --cov=vcp
```

## 資料層命令

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp data import` | 原始資料 → `dataset.yaml` + `samples.jsonl` | `--importer`、`--src`、`--name`、`--license`、`--url`、`--downloaded-at`、`--opt k=v`、`--raw-manifest full\|sizes` |
| `vcp data validate` | 重驗 card、samples 與 hash | `--name` |
| `vcp data audit` | 座標 sanity、近重複與 test 重疊、來源檢查 | `--against`、`--max-bad-boxes`、`--min-box-px`、`--max-aspect`、`--max-cover`、`--hamming`、`--corr` |
| `vcp data split` | 固定多子集 plan（進 git、不可改） | `--plan-id`、`--subsets`、`--stratify-key`、`--group-key`、`--group-from-audit`、`--strategy` |
| `vcp data lineage` | 某訓練用了哪些子集 → 哪些驗證集還乾淨 | `--plan`、`--trained-on` |
| `vcp data export` | 子集 → COCO / YOLO 目錄 + manifest | `--plan`、`--subset`、`--format`、`--out`、`--opt view=`、`--opt copy=true`、`--unseal --reason` |
| `vcp data materialize` | 每個 view 解碼一次成 npy / png 快取 + manifest | `--mode`、`--resize`、`--stack-seq`、`--window`、`--workers`、`--force`、`--decoder` |

每個命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；`--json` 時結果到 stdout、VERDICT 到 stderr。

## 量測層命令 `vcp eval`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp eval ingest` | 框架輸出 → run 的標準預測檔（記 sha、建或更新 `run.yaml`） | `--run`、`--dataset`、`--plan`、`--subset`、`--format jsonl\|coco_results\|yolo_txt\|scores_csv`、`--src`、`--export-manifest`、`--trained-on`、`--framework`、`--notes`、`--keep-input`、`--replace`、`--opt allow_unknown=true` |
| `vcp eval measure` | 護欄 → 每個乾淨 eval 子集 × 適用指標一列讀數 | `--run`、`--metrics`、`--subsets`、`--params k=v`、`--unseal --reason` |
| `vcp eval anchor` | 把既有讀數設成該 plan/子集/指標的護欄 | `--run`、`--subset`、`--metric`、`--params`、`--tolerance`（須有限且 ≥ 0）、`--replace` |
| `vcp eval sigma` | 估 σ_p 並 append | `--dataset`、`--plan`、`--metric`、`--method splithalf\|bootstrap\|prior`、`--params`、`--subsets`、`--run`（bootstrap 預設取該 cell 的錨點 run）、`--prior --note`、`--resamples`、`--seed` |
| `vcp eval preregister` | 量候選之前先把主張寫死（進 git） | `--dataset`、`--id`、`--claim`、`--component`、`--class model\|tuning`、`--baseline-run`、`--candidate-run`、`--metric`、`--params`、`--subsets`、`--t-min`、`--min-bases`、`--sigma-method`、`--sigma-ratio` |
| `vcp eval judge` | 配對 bootstrap → Δ、se、t、基底數、σ_p 條件 → 判決 | `--dataset`、`--prereg`、`--resamples`、`--seed`、`--strict`、`--unseal --reason` |
| `vcp eval status` | 孤兒預登記、run / 預登記 / 判決 / 錨點數、最新 σ_p | `--dataset`、`--max-age-hours` |
| `vcp eval report` | 全部 run × subset 讀數（全精度）+ 每個判決的 last-vs-last | `--dataset`、`--metric`、`--plan` |

共用選項：`--json`、`--data-root`、`--configs-root`；前六個命令另有 `--plugin <module>`（可重複，import 該模組讓它登記指標、轉換器或 σ_p 估法），`status` / `report` 不碰登記表所以沒有。狀態與 exit code：未知 sample_id、缺讀數、選項不合法 → FAIL(1)；沒有錨點、σ_p 為 0、有孤兒預登記 → WARN(0)；護欄對不上 → ABORT(2) 且一列讀數都不寫，VERDICT 帶 `guardrail=FAIL anchor=<reading_id> got=<值>`。判決本身不是工具錯誤：`status=OK verdict=PASS|FAIL|INVALID`，要讓 FAIL 擋 CI 就加 `--strict`。

### 標準預測格式

`runs/<run_id>/predictions/<subset>.jsonl`，一列一個 sample，依 `sample_id` 排序、LF、UTF-8。payload 欄位由 task 決定，其餘為 null：

```json
{"sample_id": "s0001", "boxes": [{"x": 12.0, "y": 8.0, "w": 40.0, "h": 25.0, "category_id": 3, "score": 0.91, "view": 0}]}
{"sample_id": "s0002", "masks": [{"category_id": 1, "score": 0.88, "rle": "<COCO compressed RLE>", "meta": {"size": [512, 512]}}]}
{"sample_id": "s0003", "scores": {"cat": 0.7, "dog": 0.2, "bird": 0.1}}
{"sample_id": "s0004", "targets": {"age": 41.5}}
```

det → `boxes`、seg → `masks`（`rle` 與 `polygon` 恰一）、cls / multilabel → `scores`（鍵 = card 的類別名）、regression → `targets`。cls / multilabel / regression 每個 sample 都要有一列，缺列即 FAIL；det / seg 缺列視為零偵測，計入 `empty`。四種轉換器負責把框架輸出轉成這個格式，`yolo_txt` 與 `coco_results` 需要 `--export-manifest` 指向對應子集的 `vcp data export` 目錄。

### 一次判決的流程

```bash
uv run vcp eval ingest --run base --dataset D --plan fixed-v1 --subset valA --format yolo_txt \
  --src runs/base/valA --export-manifest exports/valA --trained-on train   # valB 同樣再跑一次
uv run vcp eval measure --run base
uv run vcp eval anchor --run base --subset valA --metric coco_map          # 之後每次 measure 都重驗
uv run vcp eval preregister --dataset D --id p1 --claim "新 backbone 更好" --component backbone-v2 \
  --class model --baseline-run base --candidate-run cand --metric coco_map # 先寫死，才准量候選
uv run vcp eval measure --run cand
uv run vcp eval judge --dataset D --prereg p1 --strict
```

`--class tuning` 的主張還要先有 σ_p（`vcp eval sigma --method splithalf|bootstrap|prior`），否則判決 `FAIL reason=no_sigma`。最後用 `vcp eval status --dataset D` 看有沒有寫了卻沒判的主張，`vcp eval report --dataset D` 看全部讀數與 last-vs-last。

比賽官方計分器、比賽專屬格式或自訂 σ_p 估法放在 `projects/<contest>/`，以 `--plugin projects.<contest>.metrics` 匯入，模組自己呼叫 `register_metric` / `register_converter` / `register_sigma_method` 登記，`src/vcp` 不出現比賽名稱。

## 匯入器與 `rows_read` 的語意

| 匯入器 | 來源 | `rows_read` 數的是 |
|---|---|---|
| `jsonl` | 已是標準格式的 `samples.jsonl` | sample 列 |
| `csv_boxes` | 一列一框的 CSV + 影像目錄 | CSV 資料列 |
| `coco` | instances JSON + 影像目錄 | annotations |
| `yolo` | `images/` + `labels/*.txt` + 類別表 | 影像 |
| `imagefolder` | `root/<class>/*` | 影像 |
| `image_csv` | CSV 路徑欄 + 標籤 / 目標欄 | CSV 資料列 |
| `dicom` | DICOM 目錄樹（header 分組） | 掃到的檔案 |

所有影像匯入器接受 `--opt exif=stored|oriented`（預設 `stored`），Orientation ≠ 1 的 view 會記進 `view.meta.exif_orientation` 並讓 VERDICT 帶 `exif_rotated=<n>` WARN；`materialize`（npy 與 png 皆然）是唯一會把方向烙進像素的步驟；`dicom` 匯入器不讀 EXIF。

## DICOM 形態的用法（以 RSNA Knee 為例）

```bash
uv run vcp data import --importer dicom --src C:/vcp-data/raw/rsna-knee/train_series --name rsna-knee \
  --license "Competition rules" --url https://www.kaggle.com/competitions/rsna-knee-abnormality-detection \
  --downloaded-at 2026-09-03 \
  --opt labels_csv=../train.csv --opt "target_cols=ACL,MCL,Medial Meniscus,Lateral Meniscus,Medial OA,Lateral OA,PF OA,Effusion,Synovitis,Baker's,Contusion,Fracture" \
  --opt meta_cols=Report --opt seq_csv=../train_series.csv --opt "seq_cols=Fluid_Sensitive,Fat_Suppression,Anatomical_Plane" \
  --opt role_from=Anatomical_Plane --opt workers=8
uv run vcp data materialize --name rsna-knee --mode png --resize 256 --workers 4
uv run vcp data materialize --name rsna-knee --mode npy --stack-seq --workers 4
```

`sample_level=study|series`、`view_level=slice|series`、`group_from=<tag>`（預設 PatientID）、`glob=`、`tags=` 為其餘選項。快取在 `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/`，`manifest.jsonl` 內路徑皆為相對路徑。

### 在 Kaggle notebook 上跑

```bash
pip install git+https://github.com/eric20041027/Vision-contest-pipeline
export VCP_DATA_ROOT=/kaggle/working/vcp-data VCP_CONFIGS_ROOT=/kaggle/working/vcp-configs
vcp data import --importer dicom --src /kaggle/input/<comp>/train_series --name <name> ... --raw-manifest sizes
vcp data materialize --name <name> --mode png --resize 256 --workers 4
```

把 `vcp-data/datasets/<name>/`（含 `cache/materialize/`）與 `vcp-configs/datasets/<name>/` 打包成 Kaggle Dataset 下載到本機同樣的相對位置即可沿用。`--raw-manifest sizes` 只記檔名與大小，card 的 `raw_manifest_mode` 會如實記錄。
