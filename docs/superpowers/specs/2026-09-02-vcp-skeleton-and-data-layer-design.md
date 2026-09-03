# vcp 骨架與資料層設計（子專案 0 + 1）

- 日期：2026-09-02
- 版本：v2。v1 依對話逐段核可；v2 依使用者要求提高通用性，移除核心 schema 與 CLI 中所有綁定特定比賽的假設，並把每一個變異軸改為登記表。
- 狀態：待使用者審閱
- 素材：`docs/postmortems/2026-08-aidea-marine-debris-detection.md`（下稱「報告」）§6、§8、§9
- 後續：核可後交由 writing-plans 產生實作計畫

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
    path: str  # 相對 DatasetCard.image_root
    width: int | None = None
    height: int | None = None
    role: str | None = (
        None  # 此 view 在 sample 內的語意：rgb / nir / t1 / front ...；單 view 為 None
    )
    seq_id: str | None = None  # 所屬序列：DICOM series、影片、時間序列；無序列為 None
    seq_index: int | None = None  # 序列內順序，0 起
    meta: dict[str, Any] = {}


class Box(BaseModel):  # 絕對像素、左上原點、xywh
    x: float
    y: float
    w: float
    h: float
    category_id: int
    view: int = 0  # views 索引
    meta: dict[
        str, Any
    ] = {}  # 實例屬性：truncated / occluded / track_id；旋轉框的旋轉參數存 meta.rotated，x,y,w,h 存軸對齊外接矩形


class Mask(BaseModel):
    category_id: int
    view: int = 0
    rle: str | None = None  # COCO 壓縮 RLE
    polygon: list[list[float]] | None = None  # COCO 多邊形 [[x1, y1, x2, y2, ...], ...]
    path: str | None = None  # PNG 路徑（相對 image_root）
    meta: dict[str, Any] = {}  # rle / polygon / path 三者恰一


class Labels(BaseModel):
    cls: int | None = None
    targets: dict[str, float] | None = None  # 多標籤 0/1、回歸值、軟標籤、序數；鍵為 target 名
    boxes: list[Box] | None = None
    masks: list[Mask] | None = None
    extra: dict[str, Any] = {}  # 尚未建模的標籤型態（關鍵點、文字、圖說）；框架只存不驗


class Sample(BaseModel):
    sample_id: str
    views: list[View]  # 長度 ≥ 1
    labels: Labels | None = None
    label_source: Literal["gold", "derived", "pseudo", "none"]
    group: str | None = None  # 防洩漏分組鍵（病人、場景、近重複群）
    meta: dict[str, Any] = {}


class Category(BaseModel):
    id: int
    name: str
    meta: dict[str, Any] = {}


class SourceInfo(BaseModel):
    importer: str
    importer_version: str
    raw_path: str
    raw_hash: str  # sha256 over sorted "relpath\tsize\tmd5" 行
    license: str
    url: str
    downloaded_at: str  # UTC
    notes: str = ""


class DatasetCard(BaseModel):
    name: str
    task: str  # 必須在任務登記表內
    categories: list[
        Category
    ] = []  # cls / det / seg 的類別；multilabel / regression 的 target 名；可為空
    image_root: str  # 絕對路徑，或相對資料根目錄
    source: SourceInfo
    created_at: str
    sample_count: int
    samples_hash: str  # samples.jsonl 的 sha256
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
| `regression` | `labels.targets` | 鍵集合 ⊆ categories 的 name 集合；值為有限浮點數 | 首個 target 的分位數桶 |
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
    dataset_hash: str  # 產生時的 samples_hash
    strategy: str  # 必須在切分策略登記表內；v1 只有 "fixed"
    params: dict[str, Any]  # seed, stratify_key, group_key, eval_gold_only
    subsets: list[SubsetSpec]
    assignment: dict[str, str]  # sample_id → subset name
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
