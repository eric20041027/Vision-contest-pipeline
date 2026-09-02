# vcp 骨架與資料層設計（子專案 0 + 1）

- 日期：2026-09-02
- 狀態：設計已於對話中逐段核可，待使用者審閱本文件
- 素材：`docs/postmortems/2026-08-aidea-marine-debris-detection.md`（下稱「報告」）§6、§8、§9
- 後續：核可後交由 writing-plans 產生實作計畫

## 1. 目的

建立一個可重複使用的影像競賽框架 `vcp`（vision contest pipeline），支援 Kaggle 與台灣賽事（AIdea 等）的分類、偵測、分割任務。本份 spec 只涵蓋整個框架的前兩個子專案：

- **子專案 0 骨架**：repo 佈局、工具鏈、時戳與日誌規範、資料根目錄、venv 政策。
- **子專案 1 資料層**：標準資料格式、匯入與匯出、materialize 快取、切分與 lineage、進場稽核。

整體框架的節點沿用報告 §9：

```
[資料進場] → [切分] → [訓練] → [量測] → [門檻判決] → [上傳決策] → [台帳] → [備份/審計]
```

本 spec 覆蓋前兩個節點。後續子專案（量測、融合、訓練、提交治理、備份審計）各自有 spec，建造順序暫定 0 → 1 → 2（量測）→ 4（融合）→ 3（訓練）→ 5（提交）→ 6（備份），若使用者確定參加 RSNA Knee（截止 2026-10-22）則 3 與 5 可能提前。

## 2. 核心架構原則

**合約定在預測檔層級，不在模型層級。** 報告的最終配方混用 mmdetection、DEIMv2、ultralytics 三套訓練框架；框架不該綁定任何一套。資料層的責任是把任何來源的資料變成一種標準格式，並把「哪些集合是乾淨的」這件事變成可推導的資料而不是記憶。

**樣本的原子是 sample，不是影像。** 海廢是 1 sample = 1 圖；RSNA Knee 是 1 sample = 1 study = 多個 series × 多張切片。單圖是退化情況。

**標籤來源必須記錄。** RSNA 只有少數 study 有人工標籤，其餘由報告文字推導。驗證集與 holdout 若混入推導標籤，就是拿雜訊當尺，與報告的病根同構。

**機械防護優先於自覺。** 報告 §6 的七類錯誤中，時區、座標空間、台帳失同步都由程式擋下，不寫在提醒裡。

## 3. 已定案的決策

| 決策 | 選擇 | 理由摘要 |
|---|---|---|
| 執行環境 | 框架 OS 無關；本機 Windows 原生（uv 管理 Python）為主，WSL2 待訓練層接 mmdetection 時再裝 | 訓練歷史橫跨本機與租用 Linux 機器，綁 Windows 就帶不走 |
| 任務涵蓋 | 資料模型一次涵蓋 cls / multilabel / det / seg；匯入器只做眼前需要的五個 | 標準格式是一次性架構決定，匯入器可插拔 |
| 標準格式 | 自定義輕量 schema：`dataset.yaml` + `samples.jsonl`，pydantic 驗證；COCO / YOLO 靠匯出器 | COCO 無法表達 study 階層與多標籤；Parquet 對此規模過重且 agent 無法 grep |
| 切分政策 | 固定多重切分：`train` / `valA` / `valB` / `holdout(sealed)`；plan 格式通用，K-fold 產生器留待需要 | 每個模型天生有兩個互斥乾淨基底，對應報告 §8 缺陷 4 |

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
      dataset.py           # Dataset：載入、驗證、迭代、取子集、sealed 檢查
      importers/           # base.py, coco.py, yolo.py, csv_boxes.py, imagefolder.py, dicom_study.py
      exporters/           # base.py, coco.py, yolo.py
      materialize.py
      split.py             # SubsetSpec / SplitPlan / 固定切分產生器
      lineage.py           # clean_eval_subsets()
      audit/               # coords.py, dhash.py, overlap.py, provenance.py
  configs/datasets/<name>/
    dataset.yaml           # 進 git
    splits/<plan_id>.json  # 進 git
    splits/<plan_id>.unseal.jsonl   # 進 git
  tests/unit/  tests/integration/  tests/fixtures/
  docs/postmortems/  docs/superpowers/specs/
```

`CLAUDE.md` 記錄 agent 操作慣例：三條鐵則、CLI 用法、資料根目錄、venv 政策。

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
    series_id: str | None = None    # RSNA: SeriesInstanceUID
    index: int | None = None        # series 內切片序，0 起
    meta: dict[str, Any] = {}

class Box(BaseModel):               # 絕對像素、左上原點、xywh
    x: float; y: float; w: float; h: float
    category_id: int
    view: int = 0                   # views 索引

class Mask(BaseModel):
    category_id: int
    view: int = 0
    rle: str | None = None          # COCO 壓縮 RLE
    path: str | None = None         # 或 PNG 路徑（相對 image_root）；兩者恰一

class Labels(BaseModel):
    cls: int | None = None
    multi: dict[str, int | float] | None = None
    boxes: list[Box] | None = None
    masks: list[Mask] | None = None

class Sample(BaseModel):
    sample_id: str
    views: list[View]               # 長度 ≥ 1
    labels: Labels | None = None
    label_source: Literal["gold", "derived", "pseudo", "none"]
    group: str | None = None
    meta: dict[str, Any] = {}

class Category(BaseModel):
    id: int
    name: str

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
    task: Literal["cls", "multilabel", "det", "seg"]
    categories: list[Category]
    image_root: str                 # 絕對路徑，或相對資料根目錄
    source: SourceInfo
    created_at: str
    sample_count: int
    samples_hash: str               # samples.jsonl 的 sha256
    schema_version: int = 1
```

### 5.2 跨欄位驗證（`Dataset.load` 時執行）

- `label_source != "none"` ⇔ `labels is not None`。
- task = `cls`：有標籤的 sample 必有 `labels.cls`，且值在 categories 的 id 內。
- task = `multilabel`：必有 `labels.multi`，鍵集合 = categories 的 name 集合。
- task = `det`：必有 `labels.boxes`（可為空 list，代表負樣本）；每個 box 的 `view` 在範圍內，`category_id` 在 categories 內；若 view 有尺寸則 box 不得超出（容忍 1 px）。
- task = `seg`：必有 `labels.masks`；`rle` 與 `path` 恰一。
- `sample_id` 全域唯一；`views` 非空。
- 任一列失敗：報 sample_id 與行號後中止，不跳過。

### 5.3 檔案格式

- `samples.jsonl`：每列一個 Sample 的 JSON，依 `sample_id` 排序寫出，鍵順序固定，因此檔案 hash 穩定、可 diff、可 grep、可流式讀。
- `dataset.yaml`：DatasetCard。

### 5.4 各任務的表達方式

| 任務 | sample | views | labels |
|---|---|---|---|
| 海廢偵測 | 1 圖 | 1 個 | `boxes`，34 類 |
| RSNA Knee | 1 study | 全部切片，依 series 再依 InstanceNumber 排序 | `multi`，12 個標籤名 |
| ImageFolder 分類 | 1 圖 | 1 個 | `cls` |
| COCO 分割 | 1 圖 | 1 個 | `masks` |

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

第一版五個：

| 匯入器 | 來源 | task | 要點 |
|---|---|---|---|
| `coco` | instances JSON + 影像目錄 | det 或 seg（由 `--opt task=`） | 類別 id 原樣保留；bbox 已是 xywh 絕對像素 |
| `yolo` | `labels/*.txt` + 影像 + 類別表 | det | 正規化 cxcywh → 絕對 xywh；尺寸以 Pillow 讀 header |
| `csv_boxes` | `--src` 目錄內的 CSV 與影像目錄，以 `--opt csv=<相對路徑>` 與 `--opt images=<相對目錄>` 指定；欄位對照可用 `--opt col_<標準名>=<CSV 欄名>` 覆寫 | det | 預設對照 `image_filename,label_id,x,y,w,h`（海廢格式）；sample_id = 檔名；影像目錄中沒有任何框的影像補為負樣本（`boxes=[]`） |
| `imagefolder` | `root/<class>/*.jpg`，或 CSV `path,label`（label 可為多欄 0/1） | cls 或 multilabel | |
| `dicom_study` | `train.csv` + `train_series.csv` + `train_series/<study>/<series>/<sop>.dcm` | multilabel | sample = study；views = 全部切片，只讀 DICOM header（`stop_before_pixels`）取 InstanceNumber 與尺寸；`view.meta` 帶 `plane` / `fluid_sensitive` / `fat_suppression`；12 標籤齊全者 `gold`，否則 `none`；`Report` 進 `meta.report`；test 目錄同樣可匯入（無標籤） |

匯入完成後寫出 `dataset.yaml`（含 `raw_manifest.txt` 的 hash）與 `samples.jsonl`，並回報統計。跳過列數 > 0 → `WARN`，原因寫入 `cache/import_skipped.jsonl`。

### 6.2 匯出器

```
vcp data export --name <名> --plan <plan_id> --subset <子集> --format coco|yolo --out <目錄>
```

- `coco`：det / seg → `instances.json`，類別 id 與名稱原樣。
- `yolo`：det → `labels/*.txt` + `images/`（以符號連結或複製，`--opt copy=true`）+ `data.yaml`。
- 輸出目錄必附 `manifest.json`：`dataset`、`samples_hash`、`plan_id`、`subset`、`exported_at`、每個輸出檔的 hash。這是日後訓練層宣告「我用什麼訓的」的憑證。
- 對 sealed 子集匯出走與 §7.4 相同的開封機制。

### 6.3 materialize

```
vcp data materialize --name <名> --mode volume-npy|slice-png [--workers N] [--force]
```

- `volume-npy`：每個 sample 的每個 series 堆成一個 `.npy`，shape `(S, H, W)`，dtype 依 DICOM 原始位深（套用 RescaleSlope / Intercept 後轉 int16 或 uint16）。series 內切片尺寸不一致時 WARN 並退回逐切片存檔。
- `slice-png`：每個 view 一張 8-bit PNG，逐切片 min-max 正規化（有損，適合 2D 模型的快速實驗）。
- 輸出 `cache/materialize/<mode>/` 與 `cache/materialize/<mode>/manifest.jsonl`（sample_id、series_id、路徑、shape、dtype、hash）。
- 解碼走 pydicom + pylibjpeg 外掛；未安裝 `dicom` extra 時明確報錯。
- JPEG / PNG 資料集不需要此步。

## 7. 切分與 lineage

### 7.1 Plan 格式

```python
class SubsetSpec(BaseModel):
    name: str
    role: Literal["train", "eval", "sealed"]

class SplitPlan(BaseModel):
    plan_id: str
    dataset: str
    dataset_hash: str                     # 產生時的 samples_hash
    strategy: Literal["fixed"]            # 日後加 "kfold"
    params: dict[str, Any]                # ratios, seed, stratify_key, group_key, eval_gold_only
    subsets: list[SubsetSpec]
    assignment: dict[str, str]            # sample_id → subset name
    created_at: str
```

存於 `configs/datasets/<name>/splits/<plan_id>.json`，進 git。`Dataset.subset(name, plan)` 會先核對 `plan.dataset_hash == card.samples_hash`，不符即 ABORT。

### 7.2 固定切分產生器

```
vcp data split --name <名> --plan-id <id> --ratios 0.7,0.1,0.1,0.1 --seed 42 \
    [--group-key <meta 鍵> | --group-from-audit] [--no-eval-gold-only]
```

比例順序固定為 `train, valA, valB, holdout`，四數和為 1.0，只套用於 gold 池。子集名固定；角色 train → `train`，valA / valB → `eval`，holdout → `sealed`。

演算法：

1. eval 候選池 = `label_source == "gold"` 的 sample（`eval_gold_only` 預設 True）。
2. 分層鍵依 task 自動決定：cls → 類別 id；multilabel → 標籤向量；det / seg → 影像含哪些類別的 multi-hot。單標籤用分層抽樣；向量用 iterative stratification。
3. 分組：`Sample.group` 非空者整群同進同出；`--group-from-audit` 時用稽核產生的近重複群補上沒有 group 的 sample（明確 group 優先，衝突報 WARN）。
4. 依序切出 holdout → valB → valA，剩餘 gold 與所有非 gold 進 train。
5. 寫檔前斷言：子集兩兩互斥、聯集 = 全部 sample、eval 與 sealed 子集零非 gold、group 未被拆。任一失敗 → ABORT 不寫檔。
6. 輸出每子集每類分布表（stdout 表格；`--json` 時為結構化欄位），終局 `VERDICT cmd=split status=OK plan=<id> train=<n> valA=<n> valB=<n> holdout=<n>`。

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
vcp data audit --name <名> [--against <test 資料集名>] [--max-bad-boxes 0] [--hamming 4] [--corr 0.95] [--study-hits 3]
```

三項檢查各出一行 VERDICT，最後一行 `VERDICT cmd=audit status=<三者最差>`。輸出寫入 `cache/audit/`。

1. **座標 sanity**（task 為 det / seg 時）：`x, y ≥ 0`、`w, h > 0`、`x + w ≤ width`、`y + h ≤ height`，容忍 1 px。列出違規 sample_id、box 索引與原因到 `coords_bad.jsonl`。違規 box 數 > `--max-bad-boxes` → FAIL。對應報告錯誤 #2。
2. **近重複與 test 重疊**：對每個 view 計算 64-bit dHash（灰階 → 9×8 → 相鄰像素比較），快取於 `cache/dhash.jsonl`（以 path 為鍵，`--recompute` 強制重算）。Hamming ≤ `--hamming` 的配對再以 64×64 灰階 Pearson 相關 ≥ `--corr` 確認。
   - 資料集內：確認配對做 union-find 得近重複群，寫 `groups.json`（`sample_id → group_id`，僅多成員群），供 `split --group-from-audit` 使用。
   - 與 `--against`：輸出 `overlap.jsonl`（本集 sample、對方 sample、距離、相關）。有重疊 → WARN 不 FAIL，剔除與否是策略決定，但數字必須先看到。
   - DICOM 以切片為單位計算；study 級判定為 ≥ `--study-hits` 張切片命中。
3. **來源揭露**：`card.source` 的 `license`、`url`、`downloaded_at`、`raw_hash` 任一為空 → FAIL。對應海廢「開源揭露」規則與 Kaggle 外部資料規定。

## 9. CLI 總表

| 命令 | 用途 |
|---|---|
| `vcp data import` | 原始資料 → 標準格式 |
| `vcp data validate --name` | 重新驗證 card + samples，核對 samples_hash |
| `vcp data split` | 產生固定切分方案 |
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

- **單元測試**：程式內產生迷你夾具（Pillow 畫 8×8 圖；pydicom 寫未壓縮合成 DICOM），幾 KB。每個 importer、exporter、split、audit、lineage、schema 模組獨立可測。
- **切分不變量**：對隨機產生的資料集以多個 seed 參數化跑產生器，斷言互斥、覆蓋、eval 零非 gold、group 不被拆。
- **整合測試** `tests/integration/`，標記 `realdata`，資料根目錄有對應資料才跑：
  - 海廢 train CSV 匯入應得 15,163 個 sample；座標稽核應抓出方向錯誤的影像（報告記為 36 張，實際以稽核結果為準）。
  - 依 Drive 上 `filter282.py` 的邏輯重建 val282，應得 282 圖、1,093 框、33 類（報告 §5.1）。具體過濾規則在實作階段讀該腳本後確定。
  - JPEG 2000 DICOM 解碼需真實 RSNA 檔，待使用者加入比賽下載後啟用。
- 覆蓋率門檻 80%（`uv run pytest --cov=vcp`）。CI 暫不建。

## 12. 不在本 spec 範圍內

訓練層、量測指標與護欄、融合、提交治理、備份審計（各自後續 spec）；K-fold 產生器；分割 mask PNG 的獨立匯入器（COCO 匯入器已涵蓋 RLE）；報告文字推導標籤的工具（屬 RSNA 專案層，非框架）；任何 GUI；CI。

## 13. 驗收條件

1. Windows 原生：`uv sync` 後 `uv run vcp --help` 可用；`uv run pytest` 全綠，覆蓋率 ≥ 80%。
2. 海廢 train CSV 經 `vcp data import --importer csv_boxes` 得 card + samples.jsonl，`vcp data validate` OK，sample 數 15,163。
3. 合成 DICOM 夾具經 `dicom_study` 匯入得 multilabel 資料集，views 依 series / InstanceNumber 排序，gold / none 判定正確。
4. `vcp data split` 在海廢資料集產生 `fixed-v1`，五項斷言通過，分布表印出，plan 檔進 git。
5. `vcp data audit` 在海廢資料集產出座標違規清單與近重複群；`--against` test 集產出重疊清單。
6. `vcp data export` 對 train 子集輸出 COCO 與 YOLO，附 manifest；COCO 輸出結構驗證通過。
7. `vcp data lineage` 對 `train`、`train,valA`、`train,valA,valB` 三種輸入回傳正確乾淨基底。
8. 對 holdout 取子集拋 `SealedSubsetError`；`--unseal --reason` 後回傳子集並留下 unseal 記錄。
9. ruff 通過；另有一個單元測試把含 `datetime.now()` 的程式碼寫到暫存檔後對它執行 ruff，斷言 banned-api 規則確實報錯。
