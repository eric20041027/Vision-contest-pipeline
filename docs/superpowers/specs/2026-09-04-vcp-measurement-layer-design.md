# vcp 量測層設計（子專案 2：量測、門檻判決、σ_p、組件準入判定）

- 日期：2026-09-04
- 版本：v1（十節設計已於對話逐節核可）
- 狀態：待使用者審閱後進 writing-plans
- 素材：`docs/postmortems/2026-08-aidea-marine-debris-detection.md` §5（量測體系）、§6（錯誤目錄）、§8（決策系統評估）、§9（藍圖第 4、5、6、9 點）；資料層 spec `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`（v4）；Plan 2b 後記 `docs/superpowers/plans/2026-09-03-vcp-plan2b-followups.md`
- 前置：資料層 Plan 1、2a、2b 已合併（匯入、切分、lineage、匯出 manifest、materialize）

## 1. 目的

把「一個模型在哪些驗證集上讀到什麼數字、拿什麼門檻判、σ_p 是多少」全部變成不可變的檔案與機械判決，取代報告 §5 的人肉流程。要擋的失敗模式，依報告：

| 報告發現 | 本 spec 的對策 |
|---|---|
| 調參類增益在單一儀器（282）上轉移率 0.15 到 0.36；rescorer 從未在第二基底驗證 | 每次量測對所有乾淨 eval 子集同時讀數；準入規則要求 ≥2 個互斥基底皆為正 |
| σ_p 估太晚且沒進決策 | `vcp eval sigma` 第一週就能算；judge 沒有 σ_p 估計即不判 PASS |
| max-of-N 偽增益、上傳看榜才決定去留 | 預登記先寫死基準與候選 run；judge 只比預登記指名的兩個 run；候選在預登記前就有讀數 → INVALID |
| 預登記→執行無閉環，孤兒預登記 | `vcp eval status` 列出超時未判決的預登記並 WARN |
| 護欄重現已知值多次攔下真問題 | 錨點讀數；measure 前重現不了就 ABORT 且不寫任何讀數 |
| 台帳失同步 | 讀數、判決、σ_p 由命令自動 append，不可改，帶 hash 鏈 |
| 量測 venv 被污染 | 指標版本進讀數；護欄會抓到數值漂移 |

不在本 spec：融合技術（WBF、TTA）、上傳決策與配額、榜面回讀、備份審計、可視化。

## 2. 核心架構原則

- **合約定在預測檔層級。** 任何訓練框架的輸出先轉成標準預測格式，之後的量測、判決、準入都只認標準格式與 hash。
- **以任務型態與格式為軸。** 指標依任務登記，轉換器依格式登記，σ_p 方法登記；比賽官方計分器與比賽專屬格式在 `projects/<contest>/` 以 `--plugin` 匯入登記。`src/vcp/measure` 不出現比賽名稱。
- **先寫死，再量，再判。** 預登記檔的 append log 時戳早於候選讀數才算合法。
- **每個數字都能回溯。** 讀數綁預測檔 sha、預測檔綁 `samples_hash`、判決綁讀數 id、σ_p 綁它用到的讀數。
- **機械防護優先於自覺。** 違規在命令層擋下：未知 sample_id、缺預測、護欄不符、後設的候選、沒有 σ_p。

### 2.1 擴充點

| 軸 | 位置 | v1 內容 | 加一項的成本 |
|---|---|---|---|
| 指標 | `measure/metrics/` | `coco_map`、`macro_auc`、`accuracy`、`macro_f1`、`log_loss`、`rmse`、`mae`、`dice`、`miou` | 一個實作 `Metric` 的模組 + 登記 |
| 預測轉換器 | `measure/converters/` | `jsonl`、`coco_results`、`yolo_txt`、`scores_csv` | 一個實作 `Converter` 的模組 + 登記 |
| σ_p 方法 | `measure/sigma.py` | `splithalf`、`bootstrap`、`prior` | 一個 `estimate(ctx) -> SigmaEstimate` |
| 外掛 | `--plugin <module>` | 無 | `import` 時自行呼叫 `register_*` |

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 範圍 | 量測 + 門檻判決 + σ_p + 準入規則判定；融合技術留子專案 4，上傳與台帳留子專案 5 | 準入只是讀數的函式，與判決同源 |
| 架構 | run 目錄 + 不可變 jsonl 台帳 | 可 grep、可 diff、與 `samples.jsonl` 同慣例；SQLite 索引留待量大 |
| 轉換器 v1 | COCO results、YOLO txt、分數 CSV、標準 jsonl | 覆蓋 mmdet / DEIM、ultralytics、Kaggle 提交格式 |
| 指標 v1 | 五組全做，含 seg 的 Dice / mIoU | 一次把五種任務型態的量測補齊 |
| 判決的 exit code | 命令 `status=OK`，結論在 `verdict=` 欄位；`--strict` 讓 FAIL / INVALID 變 exit 1 | 負判決不是工具錯誤；腳本要硬擋時用 `--strict` |
| 預登記位置 | `configs/datasets/<name>/prereg/` 進 git；讀數、判決、σ_p 在 data root | 決策要可審閱；讀數量大且機器寫 |

## 4. 資料模型（`src/vcp/measure/schema.py`，pydantic，`extra="forbid"`）

### 4.1 標準預測格式

`runs/<run_id>/predictions/<subset>.jsonl`，一列一個 sample，排序依 `sample_id`，LF、UTF-8：

```python
class PredBox(BaseModel):   # 絕對像素 xywh，與 schema.Box 同空間
    x: float; y: float; w: float; h: float
    category_id: int
    score: float            # 0..1
    view: int = 0

class PredMask(BaseModel):
    category_id: int
    score: float
    rle: str | None = None
    polygon: list[list[float]] | None = None   # 二者恰一

class Prediction(BaseModel):
    sample_id: str
    boxes: list[PredBox] | None = None        # det
    masks: list[PredMask] | None = None       # seg
    scores: dict[str, float] | None = None    # cls / multilabel：類別名 → 機率
    targets: dict[str, float] | None = None   # regression：目標名 → 值
```

規則：payload 欄位與資料集 task 的 `label_field` 對應（det → boxes、seg → masks、cls / multilabel → scores、regression → targets），其餘必須為 None；`category_id` 必須在 card 的 categories；`scores` 的鍵必須等於 categories 名稱集合（cls 為機率分布，multilabel 各自 0..1）；數值必須有限。cls / multilabel / regression 子集內每個 sample 都要有一列，缺 → `ValidationFailed`；det / seg 缺列視為零偵測，計入 `empty`。

### 4.2 Run

`runs/<run_id>/run.yaml`（`RunCard`）：

```yaml
run_id: yolo11m-e50-s0          # 路徑安全名稱，不可重複
dataset: beach-trash
samples_hash: <sha256>          # 建立時的資料集 hash；不符即 PlanMismatchError
plan_id: fixed-v1
trained_on: [train]             # 餵 lineage；空清單 = 未訓練（純推論模型），一切 eval 子集皆乾淨
source:
  framework: ultralytics 8.3.0  # 自由文字
  config_hash: <sha256|null>
  weights_hash: <sha256|null>
  export_manifest_sha: <sha256|null>   # 訓練用的 vcp data export manifest.json 的 sha256
  notes: ""
created_at: <UTC stamp>
predictions:
  valA: {path: predictions/valA.jsonl, sha256: <...>, samples: 187, empty: 3, format_in: yolo_txt, ingested_at: <stamp>}
```

### 4.3 讀數（`measure/<dataset>/readings.jsonl`，只 append）

```json
{"reading_id": "<sha256 of run_id|plan_id|subset|metric|metric_version|params|prediction_sha>",
 "ts": "...", "run_id": "...", "dataset": "...", "samples_hash": "...", "plan_id": "...",
 "subset": "valA", "metric": "coco_map", "metric_version": "1", "params": {"iou": "50:95"},
 "value": 0.4123456789, "per_class": {"bottle": 0.51, ...}, "n_samples": 187,
 "prediction_sha": "...", "guardrail": {"anchor_reading_id": "...", "ok": true}}
```

`reading_id` 由身份欄位決定，所以同一組輸入永遠只有一列；重量即回傳既有列。

### 4.4 錨點（`measure/<dataset>/anchors.json`）

`{"<plan_id>/<subset>/<metric>": {"run_id", "reading_id", "value", "tolerance", "set_at"}}`。每個 plan × subset × metric 最多一個；`vcp eval anchor` 設定或以 `--replace` 換掉（記進 `anchors.log.jsonl`）。

### 4.5 預登記（`configs/datasets/<name>/prereg/<prereg_id>.yaml`，進 git）

```yaml
prereg_id: p003-rescorer-v2
claim: "rescorer v2 在 valA/valB 都比基準高"
component: rescorer-v2
component_class: tuning        # model | tuning；tuning 類另受 σ_p 條件
baseline_run: joint6
candidate_run: joint6-rescorer-v2
metric: coco_map
params: {iou: "50:95"}
subsets: [valA, valB]
t_min: 2.0
min_bases: 2
sigma_method: splithalf
sigma_ratio: 1.0
created_at: <stamp>
```

`configs/datasets/<name>/prereg.log.jsonl` 每列 `{"prereg_id", "sha256", "ts"}`，只 append；judge 以 log 的 `ts` 為預登記時間。

### 4.6 判決（`measure/<dataset>/judgements.jsonl`）

`{"prereg_id", "ts", "baseline_run", "candidate_run", "metric", "params", "per_subset": {"valA": {"baseline", "candidate", "delta", "se", "t", "n"}}, "bases_positive", "sigma_p": {"method", "value", "estimate_id"} | null, "verdict": "PASS|FAIL|INVALID", "reasons": [...], "reading_ids": [...], "bootstrap": {"resamples": 200, "seed": 0}}`

### 4.7 σ_p 估計（`measure/<dataset>/sigma.jsonl`）

`{"estimate_id", "ts", "plan_id", "metric", "params", "method", "value", "inputs": {...}, "note"}`。`splithalf` 的 inputs 記用到的 run 與讀數 id；`prior` 記來源文字。

## 5. 目錄佈局

```
<VCP_DATA_ROOT>/
  runs/<run_id>/run.yaml
  runs/<run_id>/predictions/<subset>.jsonl
  runs/<run_id>/inputs/            # --keep-input 時保留原始格式檔的複製
  measure/<dataset>/readings.jsonl | judgements.jsonl | sigma.jsonl | anchors.json | anchors.log.jsonl
<VCP_CONFIGS_ROOT>/
  datasets/<name>/prereg/<prereg_id>.yaml
  datasets/<name>/prereg.log.jsonl
```

`DatasetPaths` 新增 `runs_dir`、`run_dir(run_id)`、`measure_dir`、`prereg_dir`、`prereg_log`。run_id 與 prereg_id 走 `validate_name`。

## 6. CLI 總表（`vcp eval`）

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `ingest --run R --dataset D --plan P --subset S --format F --src PATH [--export-manifest DIR] [--trained-on a,b] [--framework ...] [--notes ...] [--keep-input] [--opt k=v]` | 轉標準格式、驗證、記 sha、建或更新 run.yaml | 未知 sample_id → FAIL（`--opt allow_unknown=skip` → WARN 帶 `unknown=`）；cls 類缺預測 → FAIL；det 全部為空 → WARN；已存在且 sha 不同的預測檔 → FAIL（要重匯入先 `--replace`，舊 sha 記 log） |
| `measure --run R [--metrics a,b] [--subsets a,b] [--params k=v] [--bootstrap N] [--unseal --reason ...]` | 護欄 → 讀數 → append | 護欄不符 → ABORT 不寫；無錨點 → WARN `guardrail=none`；預測 sha 不符 → FAIL；全部快取命中 → OK `cached=` |
| `anchor --run R --subset S --metric M [--params] [--tolerance 1e-6] [--replace]` | 設錨點 | 讀數不存在 → FAIL |
| `preregister --dataset D --id ID --claim ... --component ... --class model\|tuning --baseline-run R0 --candidate-run R1 --metric M [--params] [--subsets] [--t-min 2.0] [--min-bases 2] [--sigma-method splithalf] [--sigma-ratio 1.0]` | 寫檔 + log | 候選 run 在該指標與子集已有讀數 → FAIL `reason=already_measured`；id 重複 → FAIL |
| `judge --dataset D --prereg ID [--bootstrap 200] [--seed 0] [--strict]` | 算 Δ、SE、t、基底數、σ_p 條件，寫判決 | `status=OK verdict=PASS\|FAIL\|INVALID`；`--strict` 時 FAIL / INVALID → exit 1 |
| `sigma --dataset D --plan P --metric M --method splithalf\|bootstrap\|prior [--params] [--prior 0.008 --note ...] [--run R]` | 估 σ_p 並 append | splithalf 少於 3 個 run → FAIL；bootstrap 需 `--run` 或錨點 |
| `status --dataset D [--max-age-hours 48]` | 孤兒預登記、錨點、最新 σ_p、run 數 | 有孤兒 → WARN `orphans=` |
| `report --dataset D [--metric M] [--plan P]` | 全部 run × subset 讀數表（全精度）、每個 run 對其預登記基準的 last-vs-last | OK |

共用選項：`--json`、`--data-root`、`--configs-root`、`--plugin <module>`（可重複；`import` 該模組讓它登記指標或轉換器）。所有命令以 VERDICT 收尾，exit 0 / 0 / 1 / 2。

### 6.1 measure 的子集選擇

預設 = `clean_eval_subsets(plan, set(run.trained_on))` 去掉 sealed；`trained_on` 為空時全部 eval 子集皆乾淨。`--subsets` 指定時仍拒絕與 `trained_on` 重疊的子集（FAIL），sealed 子集需 `--unseal --reason`（走 `Dataset.subset` 的留痕）。

### 6.2 judge 的演算法

1. 讀預登記與 log；候選與基準在 `subsets` 的讀數必須存在（否則 FAIL `reason=missing_readings`）；候選任一讀數 `ts` 早於預登記 log `ts` → `verdict=INVALID reason=measured_before_prereg`。
2. 每個子集：載入兩個 run 的預測與子集樣本，做配對 bootstrap（`--bootstrap` 次、固定 `--seed`）：重抽 sample_id，對兩邊各算一次指標，得 Δ_b；`delta` = 全樣本 Δ，`se` = std(Δ_b)，`t = delta / se`（se 為 0 時 t 記 `inf`）。
3. `bases_positive` = 子集中 `delta > 0` 且 `t ≥ t_min` 的個數。
4. σ_p：取 `sigma.jsonl` 中該 plan、指標、參數、方法的最新估計；沒有 → `verdict=FAIL reason=no_sigma`（`component_class=model` 例外，不需 σ_p）。
5. PASS 條件：`bases_positive ≥ min_bases`，且（tuning 類）子集 Δ 的平均 ≥ `sigma_ratio × σ_p`。
6. 寫判決；`reasons` 列出每個不成立的條件。

### 6.3 σ_p 三法

- `splithalf`：所有在該 plan、指標下同時有兩個 eval 子集讀數的 run（至少 3 個），d_r = value(子集 1) − value(子集 2)，σ_p = std(d_r, ddof=1) / √2。子集對由 `--subsets` 指定，預設取 plan 前兩個 eval 子集。
- `bootstrap`：對 `--run`（預設錨點 run）在指定子集重抽 N 次算指標的標準差，代表抽樣雜訊下限。
- `prior`：手填數值與來源（例如同平台歷史賽事的 public → private 位移分布），`note` 必填。

## 7. 指標登記表

```python
class MetricResult(BaseModel):
    value: float
    per_class: dict[str, float] | None = None
    n: int

class Metric(Protocol):
    name: str
    version: str
    tasks: frozenset[str]                       # 適用的 task 名稱
    def compute(self, samples: list[Sample], predictions: dict[str, Prediction],
                card: DatasetCard, params: dict[str, str]) -> MetricResult: ...
```

- 必須對任意 `samples` 子集可算（bootstrap 依賴）；`predictions` 缺鍵依 4.1 規則處理（det / seg 視為空）。
- 內建：`coco_map`（params `iou=50|75|50:95`、`max_dets=100`；逐類 AP；pycocotools 放 `eval` extra，缺 → `VcpError` 提示 `uv sync --extra eval`）、`macro_auc`（逐類 AUC，某類全正或全負時該類記 None 並在 WARN 欄位 `undefined_classes=`）、`accuracy`、`macro_f1`、`log_loss`、`rmse`、`mae`、`dice`、`miou`（seg，mask 以 view 尺寸柵格化；polygon 與 RLE 都支援）。
- `metric_version` 進讀數；實作變更就升版，舊讀數不受影響。
- `params` 正規化為排序後的 `k=v` 字串進 `reading_id`。

## 8. 轉換器登記表

```python
class Converter(Protocol):
    name: str
    version: str
    def convert(self, src: Path, *, dataset: Dataset, subset_ids: set[str],
                export_dir: Path | None, options: dict[str, str]) -> list[Prediction]: ...
```

| 轉換器 | 來源 | 對照 sample_id 的方式 | 要點 |
|---|---|---|---|
| `jsonl` | 標準格式檔 | 直通 | 驗證後原樣 |
| `coco_results` | `[{image_id, category_id, bbox, score, segmentation?}]` | `--export-manifest DIR` 的 `instances.json` 中 `images[].id → sample_id`；或 `--opt id_map=<json>` | bbox xywh 絕對像素直接對應；segmentation 有值走 masks |
| `yolo_txt` | `labels/*.txt`，每列 `class cx cy w h conf` | `--export-manifest DIR` 的 `manifest.json["images"]`（扁平檔名 → sample_id，本計畫新增）；class index → category id 靠 `manifest.json["categories"]` | 尺寸取資料集 view 的 width / height 反正規化 |
| `scores_csv` | 一列一個 sample 的 CSV | `--opt id_col=`（預設第一欄）對 sample_id | 欄名 = 類別名（cls / multilabel）或目標名（regression）；`--opt columns=a:b,...` 重命名；多餘欄 → FAIL 除非 `--opt ignore_extra=true` |

## 9. 護欄與不可變性

- **錨點重現**：measure 對每個要算的 (subset, metric, params) 若有錨點，先用錨點 run 的預測重算；`abs(value − anchor.value) > tolerance` → ABORT，VERDICT 帶 `guardrail=FAIL anchor=<reading_id> got=<value>`，不寫任何讀數。沒有錨點 → 讀數照寫但 `guardrail=none` 且 WARN。
- **預測不可變**：ingest 記 sha；measure 與 judge 前核對，不符 → `IntegrityError`（FAIL）。重匯需 `--replace`，舊 sha 與時戳記入 `runs/<run_id>/history.jsonl`。
- **台帳只 append**：任何命令不得改寫既有列；`reading_id` 去重。
- **時間只從 `vcp.core.time`**。
- **sealed**：holdout 讀數需 `--unseal --reason`，留痕沿用資料層。

## 10. 錯誤處理與判決字彙

| 情境 | 類型 | 狀態 |
|---|---|---|
| 預測含未知 sample_id / 未知類別 / 非有限數值 | `ValidationFailed` | FAIL（`allow_unknown=skip` 時跳過並 WARN） |
| cls 類任務缺預測列 | `ValidationFailed` | FAIL |
| run 的 `samples_hash` 與資料集不符 | `PlanMismatchError` | ABORT |
| 護欄不符 | `GuardrailError`（新，`VcpError` 子類） | ABORT |
| 預測 sha 不符 | `IntegrityError` | FAIL |
| 候選早於預登記 | 判決 `INVALID` | 命令 OK（`--strict` → exit 1） |
| 缺 σ_p、基底不足、Δ 不夠 | 判決 `FAIL` + reasons | 命令 OK（`--strict` → exit 1） |
| 缺 pycocotools | `VcpError` | ABORT |

## 11. 與其他子專案的介面

- **資料層**：讀 `Dataset`、`SplitPlan`、`clean_eval_subsets`、匯出 manifest；本計畫改動資料層一處：YOLO 匯出 `manifest.json` 加 `images: {扁平檔名: sample_id}`。
- **融合層（4）**：融合後的預測也是一個 run（`trained_on` 取成員的聯集）；準入判定就是 `vcp eval judge`。
- **訓練層（3）**：訓練層負責寫 `run.yaml` 的 `source` 與 `trained_on`，並呼叫 `ingest`。
- **提交層（5）**：上傳決策讀 `judgements.jsonl` 與 `report`，本 spec 不定上傳格式。

## 12. 測試策略

- 單元：schema 驗證（payload 對 task、缺列規則）；四個轉換器各對「以既有匯出器產出的真匯出目錄」造預測檔；每個指標的手算小案例（完美預測 → mAP 1.0、AUC 1.0、accuracy 1.0、Dice 1.0；隨機 → AUC ≈ 0.5）與 sklearn / pycocotools 對照；`reading_id` 去重與 append-only；護欄對擾動預測 ABORT；preregister 拒絕已量候選、judge 對後設候選 INVALID；bootstrap 決定性（同 seed 同結果）；三種 σ_p 在合成 run 上的值。
- 端到端（CLI）：合成 det 資料集 → export yolo → 造預測 → `ingest` → `anchor` → `measure` → `preregister` → 第二個 run → `measure` → `judge` → `status` → `report`。
- 真資料：`tests/integration/` 對 RSNA 子集以標籤造「完美預測」與「隨機預測」，斷言 macro AUC 為 1.0 與約 0.5；資料缺席即 skip。
- 覆蓋率 ≥ 80%；ruff 乾淨；pycocotools 進 dev 群組與 `eval` extra。

## 13. 驗收條件

1. `vcp eval ingest` 四種格式各成功一次並產生符合 4.1 的檔案；未知 id 預設 FAIL。
2. `vcp eval measure` 對 det、multilabel、cls、regression、seg 五種合成資料集各產出讀數；重跑回傳 `cached=`。
3. 錨點設定後，對預測檔動一個數字再 measure → ABORT 且台帳無新列。
4. 預登記後量候選 → judge 產生 PASS 或 FAIL 判決並含 per_subset 的 Δ、se、t；候選先量再預登記 → INVALID。
5. `vcp eval sigma --method splithalf` 在 ≥3 個 run 上給出數值，少於 3 個 FAIL；judge 在缺 σ_p 時 FAIL `no_sigma`。
6. `vcp eval status` 對超時預登記 WARN `orphans=`。
7. `--plugin` 匯入一個測試模組登記的自訂指標後可被 `measure --metrics` 使用。
8. `uv run pytest` 全綠、覆蓋率 ≥ 80%、ruff 乾淨；README 命令表新增 `vcp eval` 一節。

## 14. 不在範圍

WBF 與其他融合技術、TTA、per-class 門檻調整工具、上傳配額與榜面回讀、備份審計、可視化、SQLite 索引、多 view 樣本的逐 view 指標（先以 sample 為單位）。
