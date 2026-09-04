# vcp 資料層參考：選項、檔案位置、食譜

所有匯入器共用：`--importer`、`--src`、`--name`、`--license`、`--url`、`--downloaded-at`、`--notes`、`--raw-manifest full|sizes`（sizes 只記檔名與大小，供 Kaggle 端超大樹）、`--opt exif=stored|oriented`（影像匯入器；預設 stored，見下方 EXIF）。

## 匯入器 `--opt`

| 匯入器 | task | `--opt` key（預設） | 要點 |
|---|---|---|---|
| `csv_boxes` | det | `csv=labels.csv`、`images=images`、`categories=<json>`、`col_image=image_filename`、`col_label=label_id`、`col_x=x`、`col_y=y`、`col_w=w`、`col_h=h`、`box_format=xywh\|xyxy\|cxcywh`、`coords=abs\|norm`、`on_bad_row=abort\|skip` | 影像目錄裡沒有框的圖成為負樣本（gold、boxes=[]）；`categories` 缺省時類別名 = id 字串；`rows_read` = CSV 列 |
| `coco` | det / seg | `json=instances.json`、`images=images`、`task=det\|seg` | 類別 id 原樣；JSON 的 width/height 視為儲存像素尺寸；`rows_read` = annotations |
| `yolo` | det | `images=images`、`labels=labels`、`names=classes.txt`（或 data.yaml） | labels 目錄不存在即 FAIL；沒有標籤檔的影像計入 `unlabeled=`；`rows_read` = 影像 |
| `imagefolder` | cls | `root=.` | `root/<class>/*`，類別 id 依資料夾名排序 |
| `image_csv` | cls / multilabel / regression | `csv=labels.csv`、`images=images`、`path_col=path`、`target_cols=a,b`、`task=`、`gold_col=` | task 缺省時自動推定：單欄整數 → cls，0/1 → multilabel，含浮點 → regression |
| `jsonl` | 任意 | `task=`（必填）、`categories=<json>`、`image_root=`、`samples=samples.jsonl` | 直通：任何格式的最後出口，驗證與 hash 仍由框架做 |
| `dicom` | multilabel / regression / 無標籤 | `sample_level=study\|series`、`view_level=slice\|series`、`glob=**/*.dcm`、`workers=4`、`tags=Kw1,Kw2`、`group_from=PatientID`、`role_from=<keyword 或 seq_csv 欄>`、`labels_csv=`、`id_col=`、`target_cols=`、`task=`、`meta_cols=`、`seq_csv=`、`seq_id_col=SeriesInstanceUID`、`seq_cols=` | 以 header UID 分組，不信資料夾名；需要 `uv sync --extra dicom`；目標欄全空 → `label_source=none`，部分空 → skipped `partial_targets` |

`categories=` 的格式：內嵌 JSON 陣列或 JSON 檔路徑（相對 `--src` 或絕對），例如 `[{"id":0,"name":"bottle"},{"id":1,"name":"bag"}]`。把 `id name` 一行一類的 `classes.txt` 轉成 JSON：

```bash
uv run python -c "import json,sys; rows=[l.split(maxsplit=1) for l in open(sys.argv[1],encoding='utf-8') if l.strip()]; json.dump([{'id':int(i),'name':n.strip()} for i,n in rows], open(sys.argv[2],'w',encoding='utf-8'))" classes.txt categories.json
```

## 無標註資料集（test 集）的食譜

寫一個只含 `sample_id`、`views`、`label_source` 的 `samples.jsonl`，用 `jsonl` 匯入：

```bash
uv run python - <<'EOF'
import json
from pathlib import Path
images = Path("C:/data/raw/contest/test")          # 影像目錄
out = Path("C:/data/work/contest-test")             # 放 samples.jsonl 的工作目錄（任意位置）
out.mkdir(parents=True, exist_ok=True)
with (out / "samples.jsonl").open("w", encoding="utf-8", newline="\n") as f:
    for p in sorted(images.rglob("*")):
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
            rel = p.relative_to(images).as_posix()
            f.write(json.dumps({"sample_id": rel, "views": [{"path": rel}], "label_source": "none"}) + "\n")
EOF
uv run vcp data import --importer jsonl --src C:/data/work/contest-test --name contest-test \
  --license "..." --url "..." --downloaded-at 2026-09-04 \
  --opt task=det --opt categories=<與訓練集相同的 json> --opt image_root=C:/data/raw/contest/test
```

`audit --against contest-test` 只看像素（dHash），不看標籤。

## 稽核 `vcp data audit`

`--against <ds>`、`--max-bad-boxes 0`、`--min-box-px 2`、`--max-aspect 20`、`--max-cover 0.98`、`--hamming 4`、`--corr 0.95`、`--view-hits 1`、`--recompute`。
三個檢查各一行 VERDICT：`coords`（`suspicious` WARN、`out_of_bounds`+`import_skipped` 超過 `max_bad` → FAIL；只在有 boxes/masks 的任務跑）、`dedup`（只在所有 view 副檔名是常見影像格式時跑；DICOM 資料集會略過）、`provenance`。
輸出在 `datasets/<name>/cache/audit/`：`coords_bad.jsonl`（每列 `kind`）、`groups.json`（`sample_id → dupNNNN`，split 的輸入）、`overlap.jsonl`、`summary.json`。dHash 快取 `cache/dhash.jsonl`。

## 切分 `vcp data split`

`--plan-id`（不可重複；plan 進 git 後不可改）、`--seed 42`、`--subsets name:role:ratio,...`（role = train | eval | sealed；預設 `train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1`）、`--stratify-key auto|none|meta.<field>`、`--group-key auto|meta.<field>`、`--group-from-audit`、`--no-eval-gold-only`、`--strategy fixed`。
單位是 group（`Sample.group`，或 `meta.<field>`，或稽核群），group 永不跨子集；eval / sealed 子集只收 gold 樣本。plan 綁 `samples_hash`，資料一變 plan 自動失效。

## 匯出 `vcp data export`

`--plan`、`--subset`、`--format coco|yolo`、`--out <空目錄>`、`--opt view=<索引或 role>`（多 view 樣本必填）、`--opt copy=true`（yolo 直接複製不試 symlink）、`--unseal --reason "<文字>"`（sealed 子集，留痕於 `splits/<plan>.unseal.jsonl`）。
輸出附 `manifest.json`：`dataset`、`samples_hash`、`plan_id`、`subset`、`exif_policy`、`exif_rotated`、每個檔案的 sha256；yolo 另有 `categories: [{index,id,name}]`（class index ↔ category id）。

## materialize `vcp data materialize`

`--mode npy|png`、`--resize <長邊>`（只對 png）、`--stack-seq`（同 seq_id 的 view 依 seq_index 堆成 S×H×W；只對 npy 有意義）、`--window dicom|minmax|percentile`（只對 png 的 8-bit 映射；png 未給時預設 `dicom`，npy 明確給了即 FAIL）、`--workers 1`、`--force`、`--decoder image|dicom`。
輸出 `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/<sample dir>/<view>.<ext>`（`--stack-seq` 的堆疊輸出改以 `<seq_id>.<ext>` 命名，只有一個 view 的序列也一樣） 與 `manifest.jsonl`（相對路徑，可整包搬到另一台機器）；失敗列在 `failed.jsonl`，`failed>0` → FAIL。重跑會跳過 resize / window / decoder / exif_policy 與來源清單都相同，且輸出檔仍在 manifest 記錄的路徑與大小的輸出。

## EXIF 方向政策

card 的 `exif_policy` 預設 `stored`（檔內像素為正）；`--opt exif=oriented` 以轉正後為正（尺寸對調）。任一政策下 Orientation ≠ 1 的 view 都記進 `view.meta.exif_orientation` 並 WARN `exif_rotated=`。匯出不改像素；只有 materialize（npy、png 皆然）會把方向烙進像素。COCO 帶 width/height 的資料集在 `oriented` 下遇到會對調的方向會 FAIL：改用 `stored` 或拿掉 JSON 尺寸。

## 檔案位置

- 資料根目錄 `VCP_DATA_ROOT`（預設 `C:/vcp-data`）：`raw/<name>/`（永不修改）、`datasets/<name>/{samples.jsonl, raw_manifest.txt, cache/}`。
- 設定根目錄 `VCP_CONFIGS_ROOT`（預設 repo `configs/`）：`datasets/<name>/dataset.yaml`、`splits/<plan>.json`、`splits/<plan>.unseal.jsonl`。
- 跑試驗或整合測試時用環境變數把兩個根目錄指到別處，避免污染真資料。
