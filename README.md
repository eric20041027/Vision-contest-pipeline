# vcp — vision contest pipeline

可重複使用的影像競賽框架：標準資料格式、多重驗證集切分、lineage、進場稽核、materialize 快取；後續子專案接量測護欄與提交治理。設計文件見 `docs/superpowers/specs/`，操作慣例見 `CLAUDE.md`。

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
