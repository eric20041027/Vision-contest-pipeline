# vcp 骨架與資料層設計（子專案 0 + 1）

- 日期：2026-09-02
- 版本：v5。v1 依對話逐段核可；v2 依使用者要求提高通用性，移除核心 schema 與 CLI 中所有綁定特定比賽的假設，並把每一個變異軸改為登記表；v3（2026-09-03）依 Plan 1 執行後的整支審查，新增 §14 補充決定並修正 §5.2 的 regression 規則；v4（2026-09-03）依 Plan 2a 後記與 RSNA Knee 真實資料形態，新增 §15 供 Plan 2b 使用；v5（2026-09-04）依 Plan 2b 後記與 Plan 2c hygiene，新增 §16 收錄計畫層決定。
- 狀態：v2 由 Plan 1 實作合併；v3 由 Plan 2a 實作合併；v4 §15 已於對話逐節核可，供 Plan 2b 使用；v5 §16 為 Plan 2b / 2c 實作結果的紀錄（裁決已記在後記，不另行核可）
- 素材：`docs/postmortems/2026-08-aidea-marine-debris-detection.md`（下稱「報告」）§6、§8、§9；Plan 1 後記 `docs/superpowers/plans/2026-09-02-vcp-skeleton-data-core-followups.md`
- 後續：Plan 2a（海廢形態，已合併）→ Plan 2b（RSNA 形態：dicom 匯入器、materialize、EXIF 與 coords 稽核的落地、Plan 2a 遺留 hygiene）→ Plan 2c（hygiene）→ 子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md`）

## 1. 目的

建立一個可重複使用的影像競賽框架 `vcp`（vision contest pipeline），適用於 Kaggle 與台灣賽事（AIdea 等）的大多數影像任務：分類、多標籤、回歸、偵測、分割。本份 spec 只涵蓋整個框架的前兩個子專案：

- **子專案 0 骨架**：repo 佈局、工具鏈、時戳與日誌規範、資料根目錄、venv 政策。
- **子專案 1 資料層**：標準資料格式、匯入與匯出、materialize 快取、切分與 lineage、進場稽核。

整體框架的節點沿用報告 §9：

```
[資料進場] → [切分] → [訓練] → [量測] → [門檻判決] → [上傳決策] → [台帳] → [備份/審計]
```

本 spec 覆蓋前兩個節點。後續子專案（量測、融合、訓練、提交治理、備份審計）各自有 spec，建造順序暫定 0 → 1 → 2（量測）→ 4（融合）→ 3（訓練）→ 5（提交）→ 6（備份），可依當時要打的比賽調整。

## 2. 核心架構原則

**以資料形態與任務類型為軸，不以特定比賽為軸。** 海廢偵測與 RSNA Knee MRI 只是兩個用來檢驗設計的例子；核心 schema 與 CLI 不得出現任何比賽專有的假設。比賽專有的東西（特定 CSV 欄名、由報告文字推導標籤、特殊目錄佈局）一律落在三個地方：匯入器的 `--opt` 選項、`projects/<contest>/` 的專案層腳本、或 `jsonl` 直通匯入器之前的一段轉換程式。

**合約定在預測檔層級，不在模型層級。** 報告的最終配方混用 mmdetection、DEIMv2、ultralytics 三套訓練框架；框架不該綁定任何一套。資料層的責任是把任何來源的資料變成一種標準格式，並把「哪些集合是乾淨的」這件事變成可推導的資料而不是記憶。

**樣本的原子是 sample，不是影像。** 一個 sample 可含任意多個 view；view 可屬於某個序列（DICOM series、影片幀、時間序列）並帶語意角色（RGB / NIR / T1 / 正面 / 側面）。單圖是退化情況。

**標籤來源必須記錄。** 人工標籤、程式推導標籤、偽標籤混在一起時，驗證集若混入非人工標籤就是拿雜訊當尺，與報告的病根同構。

**每一個變異軸都是登記表。** 任務類型、匯入器、匯出器、解碼器、切分策略、稽核項目六個軸各自是一張登記表；新增一種比賽形態 = 新增一個登記項，不改 schema、不改 CLI。

**機械防護優先於自覺。** 報告 §6 的七類錯誤中，時區、座標空間、台帳失同步都由程式擋下，不寫在提醒裡。

### 2.1 擴充點總表

| 軸 | 位置 | v1 內容 | 加一項的成本 |
|---|---|---|---|
| 任務 | `data/tasks.py` | cls、multilabel、regression、det、seg | 一個登記項：名稱、必填標籤欄、驗證函式、分層鍵函式 |
| 匯入器 | `data/importers/` | coco、yolo、csv_boxes、imagefolder、image_csv、dicom、jsonl | 一個實作 `Importer` 的模組 |
| 匯出器 | `data/exporters/` | coco、yolo | 同上 |
| 解碼器 | `data/materialize/decoders/` | image（Pillow）、dicom | 一個 `decode(view) -> ndarray` |
| 切分策略 | `data/split.py` | fixed | 一個 `generate(dataset, params) -> assignment` |
| 稽核項目 | `data/audit/` | coords、dedup、provenance | 一個 `check(dataset, opts) -> Verdict` |

`jsonl` 直通匯入器是最後的安全閥：任何登記表都沒涵蓋的格式，寫一段轉換程式產出 `samples.jsonl` 即可進入框架，驗證、hash 與 card 產生仍由框架負責。

## 3. 已定案的決策

| 決策 | 選擇 | 理由摘要 |
|---|---|---|
| 執行環境 | 框架 OS 無關；本機 Windows 原生（uv 管理 Python）為主，WSL2 待訓練層接 mmdetection 時再裝 | 訓練歷史橫跨本機與租用 Linux 機器，綁 Windows 就帶不走 |
| 任務涵蓋 | 資料模型一次涵蓋 cls / multilabel / regression / det / seg，任務為登記表；匯入器 v1 七個 | 標準格式是一次性架構決定，匯入器可插拔 |
| 標準格式 | 自定義輕量 schema：`dataset.yaml` + `samples.jsonl`，pydantic 驗證；COCO / YOLO 靠匯出器 | COCO 無法表達多 view 階層與多標籤；Parquet 對此規模過重且 agent 無法 grep |
| 切分政策 | 固定多重切分，預設 `train` / `valA` / `valB` / `holdout(sealed)`，子集名與比例可改；plan 格式通用，K-fold 產生器留待需要 | 每個模型天生有兩個互斥乾淨基底，對應報告 §8 缺陷 4 |

## 4. 骨架

### 4.1 工具鏈

- 套件名 `vcp`，`src/` 佈局，uv 管理，Python 3.12。
- pydantic v2（schema 與設定）、typer（CLI）、PyYAML、numpy、Pillow、iterative-stratification。
- 開發：pytest、pytest-cov、ruff。
- 可選 extra `dicom`：pydicom、pylibjpeg、pylibjpeg-libjpeg、pylibjpeg-openjpeg。

### 4.2 Repo 佈局

```
Vision-contest-pipeline/
  pyproject.toml  uv.lock  README.md  CLAUDE.md  .gitignore
  src/vcp/
    __init__.py
    cli.py                 # typer app；`vcp data <子命令>`
    core/
      time.py              # utc_now() / stamp()
      paths.py             # 資料根目錄解析
      config.py            # YAML → pydantic
      log.py               # JSON lines 日誌、verdict()
      hashing.py           # 檔案 md5 / sha256、canonical JSON hash、目錄 manifest hash
      errors.py            # VcpError 階層
    data/
      schema.py            # View / Box / Mask / Labels / Sample / Category / SourceInfo / DatasetCard
      tasks.py             # 任務登記表
      dataset.py           # Dataset：載入、驗證、迭代、取子集、sealed 檢查
      importers/           # base.py, coco.py, yolo.py, csv_boxes.py, imagefolder.py, image_csv.py, dicom.py, jsonl.py
      exporters/           # base.py, coco.py, yolo.py
      materialize/         # __init__.py, decoders/image.py, decoders/dicom.py
      split.py             # SubsetSpec / SplitPlan / 策略登記表 / fixed 產生器
      lineage.py           # clean_eval_subsets()
      audit/               # coords.py, dhash.py, dedup.py, provenance.py
  projects/<contest>/      # 比賽專屬腳本與設定：以 vcp 為函式庫；不屬於套件、不計覆蓋率
  configs/datasets/<name>/
    dataset.yaml           # 進 git
    splits/<plan_id>.json  # 進 git
    splits/<plan_id>.unseal.jsonl   # 進 git
  tests/unit/  tests/integration/  tests/fixtures/
  docs/postmortems/  docs/superpowers/specs/
```

`CLAUDE.md` 記錄 agent 操作慣例：三條鐵則、CLI 用法、資料根目錄、venv 政策、「比賽專屬程式碼放 `projects/`，不進 `src/vcp`」。

### 4.3 資料根目錄

環境變數 `VCP_DATA_ROOT`；未設時 Windows 預設 `C:/vcp-data`，Linux 預設 `~/vcp-data`。不進 git。佈局：

```
<root>/
  raw/<name>/             # 原始下載，永不修改
  datasets/<name>/
    samples.jsonl         # 標準格式
    raw_manifest.txt      # 原始檔清單（relpath, size, md5），一次計算
    cache/                # dhash.jsonl、materialize 輸出、audit 輸出
  logs/                   # JSON lines 日誌
```

`dataset.yaml` 與切分方案放 repo 的 `configs/datasets/<name>/`，因為它們小、需要 diff，而且 git 提交時間就是「holdout 何時定義」的 birth 時戳證據（報告 §9.5）。`samples.jsonl` 可由 raw 重新產生，放資料根目錄。

### 4.4 三條機械鐵則

1. **時戳**：全 repo 只能透過 `vcp.core.time.utc_now()`（tz-aware UTC）與 `stamp()`（ISO-8601 `Z` 結尾字串）取時。ruff `flake8-tidy-imports` banned-api 禁止 `datetime.now`、`datetime.utcnow`、`time.time`。對應報告錯誤 #1。
2. **終局行**：每個 CLI 命令結束前必輸出一行 `VERDICT cmd=<名> status=OK|WARN|FAIL|ABORT key=value ...`。exit code：OK / WARN → 0，FAIL → 1，ABORT（未預期例外）→ 2。所有命令支援 `--json`（把完整結果以單一 JSON 物件輸出到 stdout，VERDICT 行輸出到 stderr）。命令永不互動提問。
3. **venv 隔離**：核心 `vcp` 一個 venv。日後每個訓練框架各自一個 venv，以 editable 方式安裝 `vcp`。量測用 venv 凍結後禁止 install。本階段只建核心 venv。對應報告錯誤 #4。

## 5. 資料模型

### 5.1 Schema

```python
class View(BaseModel):
    path: str                       # 相對 DatasetCard.image_root
    width: int | None = None
    height: int | None = None
    role: str | None = None         # 此 view 在 sample 內的語意：rgb / nir / t1 / front ...；單 view 為 None
    seq_id: str | None = None       # 所屬序列：DICOM series、影片、時間序列；無序列為 None
    seq_index: int | None = None    # 序列內順序，0 起
    meta: dict[str, Any] = {}

class Box(BaseModel):               # 絕對像素、左上原點、xywh
    x: float; y: float; w: float; h: float
    category_id: int
    view: int = 0                   # views 索引
    meta: dict[str, Any] = {}       # 實例屬性：truncated / occluded / track_id；旋轉框的旋轉參數存 meta.rotated，x,y,w,h 存軸對齊外接矩形

class Mask(BaseModel):
    category_id: int
    view: int = 0
    rle: str | None = None          # COCO 壓縮 RLE
    polygon: list[list[float]] | None = None   # COCO 多邊形 [[x1, y1, x2, y2, ...], ...]
    path: str | None = None         # PNG 路徑（相對 image_root）
    meta: dict[str, Any] = {}       # rle / polygon / path 三者恰一

class Labels(BaseModel):
    cls: int | None = None
    targets: dict[str, float] | None = None    # 多標籤 0/1、回歸值、軟標籤、序數；鍵為 target 名
    boxes: list[Box] | None = None
    masks: list[Mask] | None = None
    extra: dict[str, Any] = {}      # 尚未建模的標籤型態（關鍵點、文字、圖說）；框架只存不驗

class Sample(BaseModel):
    sample_id: str
    views: list[View]               # 長度 ≥ 1
    labels: Labels | None = None
    label_source: Literal["gold", "derived", "pseudo", "none"]
    group: str | None = None        # 防洩漏分組鍵（病人、場景、近重複群）
    meta: dict[str, Any] = {}

class Category(BaseModel):
    id: int
    name: str
    meta: dict[str, Any] = {}

class SourceInfo(BaseModel):
    importer: str
    importer_version: str
    raw_path: str
    raw_hash: str                   # sha256 over sorted "relpath\tsize\tmd5" 行
    license: str
    url: str
    downloaded_at: str              # UTC
    notes: str = ""

class DatasetCard(BaseModel):
    name: str
    task: str                       # 必須在任務登記表內
    categories: list[Category] = [] # cls / det / seg 的類別；multilabel / regression 的 target 名；可為空
    image_root: str                 # 絕對路徑，或相對資料根目錄
    source: SourceInfo
    created_at: str
    sample_count: int
    samples_hash: str               # samples.jsonl 的 sha256
    schema_version: int = 1
```

### 5.2 任務登記表與跨欄位驗證（`Dataset.load` 時執行）

登記項欄位：`name`、`label_field`（`cls` / `targets` / `boxes` / `masks`）、`validate(sample, card)`、`stratify_key(sample, card)`。

共通規則：

- `label_source != "none"` ⇔ `labels is not None`。
- `sample_id` 全域唯一；`views` 非空；同一 sample 內 `seq_index` 在同一 `seq_id` 下不重複。
- `Labels.extra` 永不驗證。
- 任一列失敗：報 sample_id 與行號後中止，不跳過。

v1 登記項：

| 任務 | 必填 | 驗證 | 分層鍵 |
|---|---|---|---|
| `cls` | `labels.cls` | 值在 categories 的 id 內 | 類別 id |
| `multilabel` | `labels.targets` | 鍵集合 = categories 的 name 集合；值 ∈ {0, 1} | 標籤向量（iterative stratification） |
| `regression` | `labels.targets` | 非空，且鍵集合 ⊆ categories 的 name 集合；值為有限浮點數（v3：空 `targets` 不再合法，否則樣本通過驗證卻無法分層） | 首個 target 的分位數桶 |
| `det` | `labels.boxes`（可為空 list = 負樣本） | `view` 在範圍內；`category_id` 在 categories 內；view 有尺寸則 box 不得超出（容忍 1 px） | 影像含哪些類別的 multi-hot |
| `seg` | `labels.masks` | `rle` / `polygon` / `path` 恰一；`view` 與 `category_id` 同 det | 同 det |

### 5.3 檔案格式

- `samples.jsonl`：每列一個 Sample 的 JSON，依 `sample_id` 排序寫出，鍵順序固定，因此檔案 hash 穩定、可 diff、可 grep、可流式讀。
- `dataset.yaml`：DatasetCard。

### 5.4 表達方式範例

| 資料形態 | sample | views | labels |
|---|---|---|---|
| 單圖偵測（海廢） | 1 圖 | 1 個 | `boxes` |
| 多序列 3D 醫影（RSNA Knee MRI） | 1 study | 全部切片；`seq_id` = series，`seq_index` = 切片序 | `targets`，12 個 0/1 |
| 影片片段分類 | 1 clip | 幀；`seq_id` = clip，`seq_index` = 幀序 | `cls` |
| 多波段衛星 | 1 地塊 | 每波段一個 view，`role` = B02 / B03 / ... | `masks` 或 `targets` |
| 回歸（年齡、品質分） | 1 圖 | 1 個 | `targets` = {"age": 37.0} |
| 資料夾分類 | 1 圖 | 1 個 | `cls` |
| COCO 分割 | 1 圖 | 1 個 | `masks`（rle 或 polygon） |

## 6. 匯入、匯出、materialize

### 6.1 匯入器

介面：

```python
class ImportSpec(BaseModel):
    importer: str
    src: Path                       # 來源目錄；各匯入器以 options 指定目錄內的檔案
    name: str
    options: dict[str, str] = {}
    license: str                    # 以下四欄由 CLI 旗標傳入，匯入器寫進 SourceInfo
    url: str
    downloaded_at: str
    notes: str = ""

class ImportResult(BaseModel):
    dataset: Dataset
    rows_read: int
    samples_written: int
    rows_skipped: int
    skipped_reasons_path: Path | None

class Importer(Protocol):
    name: str
    version: str
    def run(self, spec: ImportSpec) -> ImportResult
```

匯入器自行填 `SourceInfo` 的 `importer`、`importer_version`、`raw_path`（= `src`）、`raw_hash`（掃描 `src` 產生 `raw_manifest.txt` 後計算）。

登記於 `importers/__init__.py` 的字典。CLI：

```
vcp data import --importer <名> --src <路徑> --name <資料集名> \
    --license <文字> --url <文字> --downloaded-at <UTC 日期> [--notes ...] [--opt k=v ...]
```

v1 七個：

| 匯入器 | 來源與選項 | task | 要點 |
|---|---|---|---|
| `coco` | instances JSON + 影像目錄；`--opt task=det\|seg` | det / seg | 類別 id 原樣保留；bbox 已是 xywh 絕對像素；polygon 存 `polygon`、RLE 存 `rle`，不做轉換 |
| `yolo` | `labels/*.txt` + 影像 + 類別表 | det | 正規化 cxcywh → 絕對 xywh；尺寸以 Pillow 讀 header |
| `csv_boxes` | CSV + 影像目錄：`--opt csv=`、`--opt images=`；欄位對照 `--opt col_<標準名>=<CSV 欄名>`；`--opt box_format=xywh\|xyxy\|cxcywh`；`--opt coords=abs\|norm` | det | 預設對照 `image_filename,label_id,x,y,w,h`、`xywh`、`abs`，純預設無特殊地位；sample_id = 檔名；影像目錄中無框的影像補為負樣本 |
| `imagefolder` | `root/<class>/*` | cls | 類別 id 依資料夾名排序指派 |
| `image_csv` | CSV：`--opt path_col=`、`--opt target_cols=a,b,c`、`--opt task=cls\|multilabel\|regression`、`--opt gold_col=`（可選，0/1 欄決定 gold / derived） | cls / multilabel / regression | `task` 未指定時自動判定：單欄整數 → `cls`，多欄 0/1 → `multilabel`，含浮點 → `regression`；指定則以指定為準並驗證欄位相容；Kaggle 最常見格式 |
| `dicom` | 任意 DICOM 目錄樹；以 header 的 StudyInstanceUID / SeriesInstanceUID 分組，不信任資料夾名；`--opt sample_level=study\|series`；可選 `--opt labels_csv=`、`--opt id_col=`、`--opt target_cols=` 做 join；可選 `--opt seq_csv=`、`--opt seq_id_col=` 把序列屬性表併入 `view.meta` | multilabel / regression / 無標籤 | 只讀 header（`stop_before_pixels`）；`seq_index` 依 InstanceNumber，缺則 ImagePositionPatient 投影，再缺則檔名；有標籤者 `gold`，否則 `none`；其餘 CSV 欄進 `meta` |
| `jsonl` | 已是標準格式的 `samples.jsonl`，加 `--opt task=`、`--opt categories=<json 或檔案>`、`--opt image_root=` | 任意 | 直通：驗證、算 hash、產 card；比賽專屬轉換程式的出口 |

匯入完成後寫出 `dataset.yaml`（含 `raw_manifest.txt` 的 hash）與 `samples.jsonl`，並回報統計。跳過列數 > 0 → `WARN`，原因寫入 `cache/import_skipped.jsonl`。

### 6.2 匯出器

```
vcp data export --name <名> --plan <plan_id> --subset <子集> --format coco|yolo --out <目錄>
```

- `coco`：det / seg → `instances.json`，類別 id 與名稱原樣；polygon 與 RLE 原樣輸出。
- `yolo`：det → `labels/*.txt` + `images/`（以符號連結或複製，`--opt copy=true`）+ `data.yaml`。
- 多 view 的 sample：匯出器以 `--opt view=<index 或 role>` 指定用哪個 view，未指定且 sample 有多個 view 則 ABORT。
- 輸出目錄必附 `manifest.json`：`dataset`、`samples_hash`、`plan_id`、`subset`、`exported_at`、每個輸出檔的 hash。這是日後訓練層宣告「我用什麼訓的」的憑證。
- 對 sealed 子集匯出走與 §7.4 相同的開封機制。

### 6.3 materialize

```
vcp data materialize --name <名> --mode npy|png [--resize <長邊>] [--stack-seq] [--workers N] [--force]
```

- 解碼器依 view 的副檔名自動選擇：`.dcm` → `dicom`（需 `dicom` extra，未裝則明確報錯）；其餘 → `image`（Pillow）。登記表可加 tiff / nifti 等解碼器。
- `npy`：每個 view 一個陣列，保留原始位深（DICOM 套用 RescaleSlope / Intercept 後 int16 或 uint16；一般影像 uint8 HWC）。`--stack-seq` 時同一 `seq_id` 的 views 依 `seq_index` 堆成 `(S, H, W[, C])`；尺寸不一致 WARN 並退回逐 view。
- `png`：8-bit；`--resize` 依長邊等比縮放。DICOM 逐 view min-max 正規化（有損）；一般影像直接轉存。這是「大圖資料集先縮小快取一次」的通用效率功能。
- 輸出 `cache/materialize/<mode>/` 與 `cache/materialize/<mode>/manifest.jsonl`（sample_id、view 索引、seq_id、路徑、shape、dtype、hash、resize 參數）。
- 已是小圖的 JPEG / PNG 資料集不需要此步。

## 7. 切分與 lineage

### 7.1 Plan 格式

```python
class SubsetSpec(BaseModel):
    name: str
    role: Literal["train", "eval", "sealed"]
    ratio: float

class SplitPlan(BaseModel):
    plan_id: str
    dataset: str
    dataset_hash: str                     # 產生時的 samples_hash
    strategy: str                         # 必須在切分策略登記表內；v1 只有 "fixed"
    params: dict[str, Any]                # seed, stratify_key, group_key, eval_gold_only
    subsets: list[SubsetSpec]
    assignment: dict[str, str]            # sample_id → subset name
    created_at: str
```

存於 `configs/datasets/<name>/splits/<plan_id>.json`，進 git。`Dataset.subset(name, plan)` 會先核對 `plan.dataset_hash == card.samples_hash`，不符即 ABORT。

### 7.2 固定切分產生器

```
vcp data split --name <名> --plan-id <id> --seed 42 \
    [--subsets train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1] \
    [--stratify-key auto|none|meta.<欄>] \
    [--group-key auto|meta.<欄>] [--group-from-audit] [--no-eval-gold-only]
```

- `--subsets` 格式 `名:角色:比例`，預設即四子集；可改名、改比例、改數量，唯需恰一個 `train` 角色、比例和為 1.0。比例只套用於 gold 池。
- `--stratify-key auto`（預設）用任務登記表的分層鍵；`meta.<欄>` 改以站點、來源等分層；`none` 純隨機。
- `--group-key auto`（預設）用 `Sample.group`；`meta.<欄>` 例如病人 id。`--group-from-audit` 用稽核產生的近重複群補上沒有 group 的 sample（明確 group 優先，衝突報 WARN）。

演算法：

1. eval 候選池 = `label_source == "gold"` 的 sample（`eval_gold_only` 預設 True）。
2. 分層：單值鍵用分層抽樣；向量鍵用 iterative stratification。
3. 分組：同 group 整群同進同出。
4. 先切所有非 train 子集（sealed 最先，其餘依 `--subsets` 宣告的反序），剩餘 gold 與所有非 gold 進 train。
5. 寫檔前斷言：子集兩兩互斥、聯集 = 全部 sample、eval 與 sealed 子集零非 gold、group 未被拆。任一失敗 → ABORT 不寫檔。
6. 輸出每子集的分層鍵分布表（stdout 表格；`--json` 時為結構化欄位），終局 `VERDICT cmd=split status=OK plan=<id> <子集名>=<n> ...`。

### 7.3 Lineage

```python
def clean_eval_subsets(plan: SplitPlan, trained_on: set[str]) -> list[str]
```

回傳 role 為 `eval` 或 `sealed`、且與 `trained_on` 各子集聯集不相交的子集名，sealed 者標註 `(sealed)`。`trained_on` 含未知子集名即 ABORT。CLI：

```
vcp data lineage --name <名> --plan <id> --trained-on train,valA
→ clean=[valB, holdout(sealed)]
```

訓練層日後從 export manifest 的 `subset` 讀取 `trained_on`，不由人填。

### 7.4 Sealed 的機械執行

`Dataset.subset()` 對 `role == "sealed"` 的子集拋 `SealedSubsetError`。唯一例外：呼叫時傳 `unseal=True` 與 `reason`，此時先追加一筆到 `configs/datasets/<name>/splits/<plan_id>.unseal.jsonl`（`ts`、`reason`、`caller`、`plan_id`、`dataset_hash`）再回傳子集。開封是留痕的一次性動作，不是設定旗標。CLI 對應 `--unseal --reason "..."`。

## 8. 進場稽核

```
vcp data audit --name <名> [--against <test 資料集名>] [--max-bad-boxes 0] [--hamming 4] [--corr 0.95] [--view-hits 1]
```

稽核項目為登記表，各出一行 VERDICT，最後一行 `VERDICT cmd=audit status=<最差者>`。輸出寫入 `cache/audit/`。

1. **座標 sanity**（`coords`，任務的 `label_field` 為 `boxes` 或 `masks` 時執行）：`x, y ≥ 0`、`w, h > 0`、`x + w ≤ width`、`y + h ≤ height`，容忍 1 px；polygon 各頂點同樣檢查。列出違規 sample_id、索引與原因到 `coords_bad.jsonl`。違規數 > `--max-bad-boxes` → FAIL。對應報告錯誤 #2。
2. **近重複與 test 重疊**（`dedup`）：對每個 view 計算 64-bit dHash（灰階 → 9×8 → 相鄰像素比較），快取於 `cache/dhash.jsonl`（以 path 為鍵，`--recompute` 強制重算）。Hamming ≤ `--hamming` 的配對再以 64×64 灰階 Pearson 相關 ≥ `--corr` 確認。多 view 的 sample 以 view 為單位計算，sample 級判定為 ≥ `--view-hits` 個 view 命中。
   - 資料集內：確認配對做 union-find 得近重複群，寫 `groups.json`（`sample_id → group_id`，僅多成員群），供 `split --group-from-audit` 使用。
   - 與 `--against`：輸出 `overlap.jsonl`（本集 sample、對方 sample、距離、相關）。有重疊 → WARN 不 FAIL，剔除與否是策略決定，但數字必須先看到。
3. **來源揭露**（`provenance`）：`card.source` 的 `license`、`url`、`downloaded_at`、`raw_hash` 任一為空 → FAIL。對應多數賽事的外部資料揭露規定。

## 9. CLI 總表

| 命令 | 用途 |
|---|---|
| `vcp data import` | 原始資料 → 標準格式 |
| `vcp data validate --name` | 重新驗證 card + samples，核對 samples_hash |
| `vcp data split` | 產生切分方案 |
| `vcp data export` | 子集 → COCO / YOLO + manifest |
| `vcp data materialize` | 解碼快取 |
| `vcp data audit` | 座標、重複、來源三項稽核 |
| `vcp data lineage` | 由 plan 與訓練子集推導乾淨基底 |

共通旗標：`--json`、`--data-root`（覆寫環境變數）。所有命令以 VERDICT 行收尾。

## 10. 錯誤處理

- 檔案邊界（card、samples、plan、manifest、unseal 記錄）進出皆過 pydantic；壞資料報位置後中止。
- 未預期例外 → `VERDICT status=ABORT reason=<例外類別: 訊息>`，exit 2，完整 traceback 進日誌檔而非 stdout。
- 沒有靜默跳過：任何被略過的列都寫進原因檔並在 VERDICT 帶計數。
- 路徑一律 `pathlib`，不呼叫 shell；Windows 與 Linux 皆可跑。

## 11. 測試策略

- **單元測試**：程式內產生迷你夾具（Pillow 畫 8×8 圖；pydicom 寫未壓縮合成 DICOM），幾 KB。每個 importer、exporter、decoder、split、audit、lineage、schema、tasks 模組獨立可測。
- **切分不變量**：對隨機產生的資料集以多個 seed 與多種 `--subsets` 組合參數化跑產生器，斷言互斥、覆蓋、eval 零非 gold、group 不被拆。
- **整合測試** `tests/integration/`，標記 `realdata`，資料根目錄有對應資料才跑：
  - 海廢 train CSV 經 `csv_boxes` 匯入應得 15,163 個 sample；座標稽核應抓出方向錯誤的影像（報告記為 36 張，實際以稽核結果為準）。
  - 依 Drive 上 `filter282.py` 的邏輯重建 val282，應得 282 圖、1,093 框、33 類（報告 §5.1）。具體過濾規則在實作階段讀該腳本後確定。
  - JPEG 2000 DICOM 解碼需真實壓縮檔，待有資料時啟用。
- 覆蓋率門檻 80%（`uv run pytest --cov=vcp`）。CI 暫不建。

## 12. 不在本 spec 範圍內

訓練層、量測指標與護欄、融合、提交治理、備份審計（各自後續 spec）；K-fold 產生器；tiff / nifti / 影片解碼器；關鍵點與旋轉框升格為正式欄位；由報告文字推導標籤的工具（屬 `projects/` 層）；任何 GUI；CI。以上皆為已預留的擴充點，不是設計缺口。

## 13. 驗收條件

1. Windows 原生：`uv sync` 後 `uv run vcp --help` 可用；`uv run pytest` 全綠，覆蓋率 ≥ 80%。
2. 海廢 train CSV 經 `vcp data import --importer csv_boxes` 得 card + samples.jsonl，`vcp data validate` OK，sample 數 15,163。
3. 合成 DICOM 夾具經 `dicom` 匯入（含 `labels_csv` join）得 multilabel 資料集，views 依 seq_id / seq_index 排序，gold / none 判定正確。
4. `vcp data split` 在海廢資料集以預設四子集產生 `fixed-v1`，五項斷言通過，分布表印出，plan 檔進 git。
5. `vcp data split --subsets train:train:0.8,val:eval:0.2` 產生兩子集方案並通過斷言，證明子集不綁死四個。
6. `vcp data audit` 在海廢資料集產出座標違規清單與近重複群；`--against` test 集產出重疊清單。
7. `vcp data export` 對 train 子集輸出 COCO 與 YOLO，附 manifest；COCO 輸出結構驗證通過。
8. `vcp data lineage` 對 `train`、`train,valA`、`train,valA,valB` 三種輸入回傳正確乾淨基底。
9. 對 holdout 取子集拋 `SealedSubsetError`；`--unseal --reason` 後回傳子集並留下 unseal 記錄。
10. `image_csv` 匯入一個回歸 CSV 得 `task=regression` 資料集並通過驗證；`jsonl` 直通匯入手寫檔通過驗證並產出 card。
11. ruff 通過；另有一個單元測試把含 `datetime.now()` 的程式碼寫到暫存檔後對它執行 ruff，斷言 banned-api 規則確實報錯。

## 14. v3 補充決定（Plan 1 整支審查後，2026-09-03 核可）

以下條目補足 v2 未講清楚或審查發現的缺口；與前文衝突時以本節為準。Plan 1 已落地的實作與本節不一致者（14.14–14.17）由 Plan 2a 的 hygiene 任務修正。

### 14.1 路徑可攜性與匯入行為

1. **`image_root` / `raw_path` 儲存規則**：路徑若位於 `data_root` 之下，存相對於 `data_root` 的 posix 相對路徑；否則存絕對路徑。card 不加欄位。新增 `DatasetPaths.resolve_image_root(card) -> Path`（相對路徑接回 `data_root`，絕對路徑原樣），匯出器與 materialize 一律經由它取影像根目錄。效果：資料照慣例放 `<data_root>/raw/<name>/` 時，進 git 的 `dataset.yaml` 跨機器可用。
2. **`import` 覆寫既有資料集**：card 已存在、新 `samples_hash` 與舊值不同、且 `splits/` 下已有 plan 檔 → 命令狀態 `WARN`，VERDICT 帶 `plans_invalidated=<n>`。plan 檔不刪（hash 鏈會在 `subset()` 擋下失效的 plan），只提早警告。`ImportResult` 加 `plans_invalidated: int`。
3. **`finalize_import` 先驗證再算 manifest**：以空 `raw_hash` 組 card 跑 `Dataset.from_parts` 驗證，通過後才掃 `dir_manifest`（大資料集 md5 成本高，不在壞資料上白算），最後以 `model_copy` 補入 `raw_hash` 再 `save`。
4. **sample_id 慣例**：所有以影像為單位的匯入器（`csv_boxes`、`coco`、`yolo`、`imagefolder`、`image_csv`）一律 `sample_id = view.path`（相對 `image_root`、含副檔名的 posix 路徑）；重複 → `ValidationFailed`。
5. **影像尺寸**：上述匯入器一律用 Pillow 讀 header（`Image.open` 不解碼像素）填 `width` / `height`；影像缺檔 → `ValidationFailed` 列出檔名（不跳過）。
6. **`image_csv` 的類別對照**：`task=cls` 時以標籤欄不重複值排序，`id` = 序號、`name` = `str(值)`；`task=multilabel` / `regression` 時 `target_cols` 的欄名即 category 名（id 依序）。`gold_col`（可選，0/1）決定 `gold` / `derived`，缺省全 gold。

### 14.2 匯出器細節

7. **YOLO**：輸出 `images/`、`labels/`、`data.yaml`（`path: <匯出目錄絕對路徑>`、`train: images`、`names: {id: name}`），一個匯出目錄對應一個子集。影像預設嘗試符號連結，失敗（Windows 無權限等）時**自動退回複製並 WARN**（VERDICT 帶 `images=copied`）；`--opt copy=true` 強制複製。標籤列為 `class_index cx cy w h` 正規化到 0–1，class_index 依 `card.categories` 宣告序。
8. **COCO**：`instances.json` 的 `images[].file_name` = view 路徑（相對 `image_root`）、`id` = 匯出時的連續整數（另附 `sample_id` 欄位保留對應）；`annotations[]` 為 `bbox` xywh、`area = w*h`、`iscrowd = 0`、`segmentation` 原樣（polygon 或 RLE）；`categories` 原樣。
9. **多 view 樣本**：`--opt view=<索引或 role>` 指定用哪個 view；未指定且任一樣本有多個 view → ABORT。
10. **`manifest.json`**：`dataset`、`samples_hash`、`plan_id`、`subset`、`exported_at`、`format`、`files: {相對匯出目錄的路徑: sha256}`。匯出 sealed 子集需 `--unseal --reason`（走 §7.4 留痕）。

### 14.3 稽核細節

11. **dHash**：Pillow 轉灰階縮到 9×8，相鄰像素比較得 64-bit 整數；不新增 imagehash 依賴。快取 `cache/dhash.jsonl`（每列 `path`、`size`、`hash`；`path` 相同且 `size` 相同即命中，`--recompute` 強制重算）。配對搜尋以 numpy 分塊 XOR + popcount（15k 圖數秒內）；Hamming ≤ `--hamming`（預設 4）的配對再以 64×64 灰階 Pearson 相關 ≥ `--corr`（預設 0.95）確認。
12. **輸出**：`cache/audit/coords_bad.jsonl`、`cache/audit/groups.json`（`sample_id → group_id`，僅多成員群；即 `split --group-from-audit` 的輸入）、`cache/audit/overlap.jsonl`、`cache/audit/summary.json`。三項檢查各一行 `VERDICT cmd=audit.<項目>`，最後一行 `VERDICT cmd=audit status=<最差者>`。
13. **多 view 樣本**依 view 計算；sample 級命中 = 至少 `--view-hits`（預設 1）個 view 命中。稽核項目為登記表，每項 `check(dataset, opts) -> Verdict`。

### 14.4 Plan 1 遺留的修正（Plan 2a 的 hygiene 任務）

14. `resolve_configs_root` 找不到 `pyproject.toml` + `configs/` 時明確 `ValidationFailed`（提示設 `VCP_CONFIGS_ROOT`），不再退回套件相對路徑。
15. `Dataset.subset()` 在核對 hash 之後、回傳之前跑一次 `assert_plan_invariants(plan, self)`，擋下手動編輯過的 plan 檔。
16. §5.2 的 regression 規則改為「非空」（已修正於前文）；`_validate_regression` 同步。
17. 切分每步的 seed 改為 `seed * 1000 + step`；`Dataset.load(verify_hash=False)` 加「僅供測試」警語；`normalize_keys` docstring 補充零寬向量退回字串分層；比例超額訂閱時的 `InvariantError` 訊息改為說明「四捨五入後的子集大小總和超過可用池」。

### 14.5 Plan 2 的切分

- **Plan 2a（海廢形態）**：`csv_boxes`、`coco`、`yolo`、`imagefolder`、`image_csv` 匯入器；`coco` / `yolo` 匯出器；稽核三項；`tests/integration/` 與海廢真實資料測試；14.4 的 hygiene 任務。
- **Plan 2b（RSNA 形態）**：`dicom` 匯入器；materialize（`npy` / `png`、`--resize`、`--stack-seq`）與解碼器登記表；合成 DICOM 測試。
- 仍不在範圍：K-fold、`generate_fixed` 拆函式、tiff / nifti / 影片解碼器。

## 15. v4 補充決定（Plan 2a 後記與 RSNA 真實資料形態，2026-09-03 逐節核可）

以下條目落實 Plan 2a 後記 §5 的待辦，並依 RSNA Knee 的真實資料形態（4,407 個 study、24,371 個 series、82 萬張切片、570 GB、transfer syntax 混雜、僅 58 個 study 有完整人工標籤）把 §6.1 的 `dicom` 列與 §6.3 講清楚。與前文衝突時以本節為準。

### 15.1 EXIF 方向政策

1. **政策記在 card**：`DatasetCard.exif_policy: Literal["stored", "oriented"] = "stored"`。`stored` 以檔內像素為正；`oriented` 以 EXIF 轉正後為正。`schema_version` 不變，舊 card 讀入時取預設。
2. **匯入**：影像匯入器（`csv_boxes`、`coco`、`yolo`、`imagefolder`、`image_csv`）接受 `--opt exif=stored|oriented`，寫進 card。`common.make_view` 讀 Orientation 標籤（0x0112）：值不在 {None, 1} 時記 `view.meta["exif_orientation"] = <int>`；政策為 `oriented` 且值在 {5, 6, 7, 8} 時 `width` / `height` 對調。`jsonl` 直通匯入器只依選項寫政策，不掃描影像。
3. **可見性**：`ImportResult.exif_rotated: int`（`finalize_import` 加同名關鍵字參數）；大於 0 時 VERDICT 帶 `exif_rotated=<n>` 且狀態 WARN，不論政策，提醒抽幾張人工核對框所在的空間。
4. **一致性**：尺寸、座標驗證、匯出、materialize 全部依 card 政策。解碼器 `image` 只在 `oriented` 政策下套 `ImageOps.exif_transpose`。匯出器不改像素；`manifest.json` 加 `exif_policy` 與 `exif_rotated`，後者大於 0 時匯出 VERDICT 帶同名欄位並 WARN。要把方向烙進像素只有一條路：materialize `png`。

### 15.2 coords 稽核的職責

5. **載入驗證維持嚴格**：資料集內的框永遠幾何合法（失敗封閉在邊界），`on_bad_row` 語意不變。
6. **稽核改查三件事**，全部寫入 `cache/audit/coords_bad.jsonl`，每列帶 `kind`：
   - 有尺寸 view 上的可疑框（WARN）：`tiny`（`w` 或 `h` < `min_box_px`）、`aspect`（`max(w/h, h/w)` > `max_aspect`）、`cover`（`w*h` ≥ `max_cover × W×H`）、`duplicate`（同 view 上 `category_id, x, y, w, h` 完全相同）。
   - 無尺寸 view（`width` / `height` 為 None）：以 `common.image_size` 讀 header 補尺寸後查越界，`kind=out_of_bounds`，狀態 FAIL；讀不到尺寸的 view 記 `kind=unsized`（WARN），不計入越界。
   - 匯入時被擋的列：讀 `cache/import_skipped.jsonl`（存在時），每列併入為 `kind=import_skipped` 並保留原因。
7. **門檻**：`AuditOptions` 新增 `min_box_px: float = 2.0`、`max_aspect: float = 20.0`、`max_cover: float = 0.98`；CLI 對應 `--min-box-px`、`--max-aspect`、`--max-cover`。`--max-bad-boxes` 的計數 = `out_of_bounds` + `import_skipped`；超過即 FAIL。`summary.json` 與 `VERDICT cmd=audit.coords` 帶 `suspicious=<n> out_of_bounds=<n> import_skipped=<n>`。
8. **整合測試**：海廢資料集的 coords 斷言改為「`import_skipped` 計數等於 `cache/import_skipped.jsonl` 列數」，不再用 `max_bad_boxes=10**9`。

### 15.3 `dicom` 匯入器

9. **依賴**：需要 `dicom` extra；未安裝 pydicom 時 `VcpError`（ABORT）並提示 `uv sync --extra dicom`。
10. **探索**：`--opt glob=**/*.dcm`（預設，副檔名不分大小寫）；`--opt glob=**/*` 收所有檔案，非 DICOM 者進 skipped（`not_dicom`）。只讀 header（`stop_before_pixels=True`），`--opt workers=4` 個執行緒平行讀。`rows_read` = 掃到的檔案數。
11. **必要 tag 與跳過原因**：`StudyInstanceUID`、`SeriesInstanceUID`、`SOPInstanceUID`、`Rows`、`Columns` 缺一即 skipped（`missing_tag:<keyword>`）；`NumberOfFrames` > 1 → skipped（`multiframe`，多幀不在本 spec 範圍）。以 header 的 UID 分組，不信任資料夾名。
12. **層級選項**：
    - `--opt sample_level=study|series`（預設 `study`）：sample_id 為該層級的 UID。
    - `--opt view_level=slice|series`（預設 `slice`）。`slice`：每張切片一個 view，`path` 為檔案相對路徑，`seq_id` = SeriesInstanceUID，`seq_index` = 排序後的名次（0 起，非原始 InstanceNumber），`meta` 只記 `instance_number` 與（有則記）`slice_location`。`series`：每個 series 一個 view，`path` 為 series 目錄相對路徑，尺寸取名次 0 的切片；切片尺寸不一致時尺寸留 None、`meta["inconsistent_size"]=true`，並計入 VERDICT `inconsistent_series=<n>`（WARN）。
    - 排序：InstanceNumber；缺則 ImagePositionPatient 沿 ImageOrientationPatient 法向量的投影；再缺則檔名。view 順序為（SeriesNumber, SeriesInstanceUID, seq_index）。
13. **series 層級摘要**放 `sample.meta["series"][<SeriesInstanceUID>]`，切片不重複攜帶：`description`、`number`、`n_slices`、`modality`、`rows`、`columns`、`pixel_spacing`、`slice_thickness`、`transfer_syntax`（取名次 0 的切片）；`--opt tags=Kw1,Kw2` 追加任意 header keyword；`--opt seq_csv=`、`--opt seq_id_col=`（預設 `SeriesInstanceUID`）、`--opt seq_cols=a,b` 把序列屬性表的欄併入同一處。`--opt role_from=<DICOM keyword 或 seq_csv 欄名>` 填 `view.role`；預設不填。
14. **標籤**：`--opt labels_csv=`（相對 `src` 或絕對）、`--opt id_col=`（預設依 sample_level 為 `StudyInstanceUID` / `SeriesInstanceUID`）、`--opt target_cols=`、`--opt task=multilabel|regression`（缺省時依 §6.1 `image_csv` 列的推定規則）、`--opt meta_cols=`（其餘欄原字串進 `sample.meta[<col>]`）。categories 為 `target_cols` 欄名、id 依序。目標欄全有值 → `gold`；全空 → `none`（`labels=None`）；部分有值 → skipped（`partial_targets`）。樹裡有檔案但 CSV 沒有的 sample → `none`；CSV 有 id 但沒有檔案 → skipped（`no_files`）。`unlabeled` = `label_source=none` 的 sample 數。
15. **分組**：`--opt group_from=PatientID`（預設）→ `sample.group` 取名次 0 切片的該 tag 值，缺則不分組；`--opt group_from=none` 關閉。同一病患的多次檢查因此不會跨子集。
16. **其餘**：`image_root` = `src`，依 §14.1-1 規則儲存（Kaggle 的 `/kaggle/input/...` 為絕對路徑）；sample 依 sample_id 排序，輸出決定性。RSNA 的用法：`sample_level=study`、`labels_csv=train.csv`、`target_cols=<12 欄>`、`meta_cols=Report`、`seq_csv=train_series.csv`、`seq_cols=Fluid_Sensitive,Fat_Suppression,Anatomical_Plane`、`role_from=Anatomical_Plane`。

### 15.4 materialize 與解碼器登記表

17. **模組**：`data/materialize/`（`base.py` 介面與登記表、`run.py` 執行、`manifest.py`）與 `data/materialize/decoders/`（`image.py`、`dicom.py`）。`Decoder` 協定：`name`、`version`、`decode(path, *, exif_policy) -> ndarray`（HW 或 HWC）、`decode_series(paths) -> ndarray`（依呼叫端給定順序堆成 S×H×W）。`view_level=slice` 時順序來自 dataset 的 `seq_index`，不重讀 header；view 為 series 目錄時（`view_level=series`）由 `dicom` 解碼器讀 header 依 15.3-12 的規則排序。登記表 `DECODERS` 依名稱；`decoder_for(path)`：副檔名 `.dcm` → `dicom`，其餘 → `image`；`--decoder <名>` 可強制。
18. **`dicom` 解碼**：`pixel_array` 套 RescaleSlope / RescaleIntercept；slope 與 intercept 為整數時保留 int16 / uint16（依 PixelRepresentation），否則 float32。壓縮 transfer syntax 由 pylibjpeg 家族處理。`MONOCHROME1` 在 `png` 模式反相，`npy` 模式保留原值並在 manifest 記 `photometric`。
19. **命令**：`vcp data materialize --name <名> --mode npy|png [--resize <長邊>] [--stack-seq] [--window dicom|minmax|percentile] [--workers N] [--force] [--decoder <名>]`。`--window` 只影響 `png`：`dicom` 用 WindowCenter / WindowWidth，缺 tag 退回 `minmax`；`percentile` 取 0.5 / 99.5 百分位。`image` 解碼器在 `png` 模式重存為 8-bit 並以 LANCZOS 依長邊等比縮放。materialize 涵蓋全部 sample，不分子集，不觸發開封（像素不是標籤）。`--workers` 用 process pool，解碼是 CPU 工作。
20. **輸出佈局**：`cache/materialize/<mode>[-r<長邊>]/<dir>/<view_index>.npy|png`；`--stack-seq` 時 `<dir>/<seq_id>.npy`，同 seq 尺寸不一致則 WARN 並退回逐 view；`view_level=series` 的 view 本身即 S×H×W 體積，輸出 `<dir>/<view_index>.npy`，`--stack-seq` 對其無作用。`<dir>` 為 sample_id 經路徑安全化：`/` → `__`；仍含檔名不可用字元時改用 sample_id 的 sha256 前 16 碼。manifest 為權威對照。
21. **manifest**：`cache/materialize/<mode>[-r<長邊>]/manifest.jsonl`，每列 `sample_id`、`view`（int，堆疊時 null）、`seq_id`、`src`（view.path）、`out`（相對該 mode 目錄）、`shape`、`dtype`、`resize`、`window`、`exif_policy`、`decoder`、`decoder_version`、`sha256`、`materialized_at`。路徑一律相對，快取目錄可在 Kaggle 與本機之間搬移。重跑時已有同 `src` / `resize` / `window` / `decoder_version` 的列且輸出檔存在即 skip；`--force` 重做並覆寫該列。
22. **失敗與判決**：解碼失敗記 `failed.jsonl`（`sample_id`、`view`、`src`、`error`），不中斷其他工作；VERDICT `cmd=materialize` 帶 `mode= resize= materialized=<n> skipped=<n> failed=<n>`，`failed` > 0 → FAIL。
23. **Kaggle 用法**（寫進 README）：notebook 內 `pip install git+<repo>`，`VCP_DATA_ROOT=/kaggle/working/vcp-data`，`vcp data import --importer dicom --src /kaggle/input/<comp>/train_series ... --raw-manifest sizes`，`vcp data materialize --mode png --resize 256`，把 `cache/materialize/` 打包成 Kaggle Dataset 下載；本機以同一份 `dataset.yaml` + `samples.jsonl` + manifest 使用。

### 15.5 大資料集的 raw manifest

24. `vcp data import --raw-manifest full|sizes`（預設 `full`）→ `ImportSpec.raw_manifest`。`sizes` 模式的 `raw_manifest.txt` 每列 `relpath<TAB>size<TAB>-`，不算 md5；`SourceInfo.raw_manifest_mode: Literal["full", "sizes"] = "full"` 讓 card 誠實記錄來源證據強度。`raw_hash` 仍為 manifest 檔內容的 sha256。

### 15.6 依賴、夾具與驗收

25. **dev 群組**加入 `pydicom>=3.0`、`pylibjpeg>=2.0`、`pylibjpeg-libjpeg>=2.0`、`pylibjpeg-openjpeg>=2.0`，完整測試套件不需另裝 extra；runtime 仍以 `dicom` extra 宣告。
26. **合成夾具**：`tests/helpers.py` 提供 `write_dicom_study(dir, *, series, slices, size=(16, 16), missing_instance_number=False, compress=None|"rle")`，用 pydicom 產生多 series 的 study（含 PatientID、SeriesDescription、InstanceNumber、ImagePositionPatient、Rescale、Window），RLE Lossless 由 pydicom 內建編碼，驗證壓縮路徑不需外部編碼器。
27. **真資料整合測試**：`tests/integration/test_rsna_knee.py`，讀 `VCP_REALDATA_ROOT/rsna-knee/`（佈局同 Kaggle：`train.csv`、`train_series.csv`、`train_series/<study>/<series>/*.dcm`），資料缺席即 skip。斷言：sample 數等於 `train_series/` 下的 study 目錄數、`gold` 數等於子集內 `train.csv` 十二欄皆有值的列數、`unlabeled` 等於其餘、每 sample 的 series 數等於 `train_series.csv` 該 study 的列數、view 依 seq_index 遞增；對 3 個 sample 跑 `png --resize 256` 與 `--stack-seq npy`，manifest 列數與 sha256 可重現。
28. **驗收條件補充**（接 §13）：12. RSNA 子集依第 27 條通過；13. materialize 在合成夾具重跑第二次 `skipped` 等於第一次的 `materialized`；14. 海廢資料集 `audit.coords` 的 `import_skipped` 等於 `import_skipped.jsonl` 列數；15. 帶 Orientation=6 的 JPEG 夾具在 `stored` / `oriented` 兩政策下尺寸分別為原始與對調，兩者 VERDICT 都帶 `exif_rotated=1` 且 WARN。

### 15.7 Plan 2a 遺留的小項（Plan 2b 的 hygiene 任務）

29. YOLO 匯出的 `manifest.json` 加 `categories: [{index, id, name}]`。
30. `ExportResult.fields: dict[str, FieldValue]` 讓匯出器回傳額外 VERDICT 欄位；YOLO 填 `images=copied|symlinked`。
31. `_place_image` 只在符號連結權限不足（`OSError` 之 EPERM / EACCES / WinError 1314）時退回複製，其他 I/O 錯誤傳播。
32. CLI 層測試：`import` 的 `unlabeled=` 欄位、空子集匯出 `status=WARN`、`plans_invalidated` 的 WARN 行；`count_invalidated_plans` 因舊 card 不可讀而計數時 VERDICT 帶 `old_card=unreadable`。
33. README 記各匯入器 `rows_read` 的語意（csv 列 / coco annotations / yolo 影像 / imagefolder 影像 / dicom 檔案）。

### 15.8 不在 Plan 2b 範圍

多幀 DICOM；NIfTI、TIFF、影片解碼器；由報告文字推導標籤的工具（屬 `projects/rsna-knee/`）；Kaggle Dataset 上傳自動化；訓練層對 materialize 快取的讀取介面（子專案 3 的 spec 決定，資料層只保證 manifest 穩定）；dedup 對多 view 共享檔案的判定單位（留待有真實案例）。

## 16. v5 補充決定（Plan 2b 實作與 Plan 2c hygiene 的定案，2026-09-04）

以下為實作期間由計畫或審查裁決、原 spec 未明說的規則，與前文衝突時以本節為準。

1. **materialize**：`--resize` 只對 `png`（npy 保留原解析度，給了即 FAIL）；`--window` 只對 `png`（npy 明確給了即 FAIL，未給時 png 預設 `dicom`）；png 模式遇到體積（series 層級 view 或堆疊）記入 `failed.jsonl` 而非另開目錄；`--workers` 預設 1；manifest 列多 `bytes`（skip 判斷用）與 `srcs`（每列都寫，堆疊列為完整來源，`src` 恆等於 `srcs[0]`；舊 manifest 無此欄仍可讀）；堆疊工作只認自己的列，尺寸不一致的序列每次重試並 WARN；成功的堆疊列取代其 views 的逐 view 列；每輪結束後 manifest 只保留本輪規劃且未失敗的列，未被任何列引用的輸出檔會被刪除（VERDICT `orphans_removed=`）；快取比對含解碼器名與版本、resize、window、exif_policy、檔案大小、來源清單（`srcs`；舊 manifest 無此欄者仍可跳過）與該列記錄的輸出路徑（路徑與本輪會寫的不同即重做，換過命名規則的舊快取因此會自己修正）；`--stack-seq` 下只有一個 view 的序列仍是堆疊工作，輸出與多 slice 序列一樣以 `seq_id` 命名；堆疊列取代逐 view 列的動作在合併結果與剔除失敗列之後、對本輪所有工作執行（不限本輪重做的工作），所以已是最新的堆疊列仍會清掉它取代的逐 view 列；`to_uint8` 對 uint8 輸入不重新映射，但 MONOCHROME1 仍反相；uint8 MONOCHROME1 的反相與 series 目錄的 `exif_policy` 是 2026-09-04（commit `92570e4`）才修的，解碼器版本未跟著跳號，因此在那之前產生的快取若來自 8-bit MONOCHROME1 或以 `--decoder image` 讀 series 目錄，需要跑一次 `--force` 才會更新。同一個 `out_dir` 不要並行跑兩種規劃（例如同時跑 `--stack-seq` 與不加），先完成的一輪會刪掉另一輪的輸出。
2. **dicom 匯入器**：沒有 `labels_csv` 時 task 預設 `multilabel`（可 `--opt task=regression`）；series 層級 view 的 `seq_index` 為 None；目錄 view 一律由 dicom 解碼器讀取（`Path.suffix` 對 UID 目錄名不可靠）；所有選項在 header 掃描之前驗證。
3. **COCO 匯入 + EXIF**：COCO 的 `width`/`height` 視為儲存像素尺寸；`--opt exif=oriented` 下遇到會對調軸的方向即 `ValidationFailed`，提示改用 `stored` 或拿掉 JSON 尺寸。
4. **檢查適用性**：dedup 只在所有 view 副檔名屬 `IMAGE_EXTS` 時執行；不適用的檢查（登記順序）記在 `summary.json["skipped"]` 與 audit VERDICT 的 `skipped=`，不改狀態。
5. **coords**：越界檢查先於重複檢查（重複的越界框計入 out_of_bounds）；boxes 與 polygons 共用每個 sample 的尺寸快取。
6. **import 的 `old_card=unreadable`**：只在 `plans_invalidated > 0` 時輸出。
7. **依賴**：選用 extra 的每個 pin 都同時列在 dev 群組，由 `tests/unit/test_package.py` 守門；不改成 dev 依賴 `vcp[dicom]`。
