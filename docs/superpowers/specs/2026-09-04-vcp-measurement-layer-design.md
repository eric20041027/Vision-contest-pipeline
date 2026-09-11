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
  valA: {path: predictions/valA.jsonl, sha256: <...>, samples: 187, empty: 3, format_in: yolo_txt, format_version: "1", ingested_at: <stamp>}
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

## 15. v2 補充決定（Plan 3 實作與審查的定案，2026-09-05）

以下為實作期間由計畫或審查裁決、原 spec 未明說或已被推翻的規則，與前文衝突時以本節為準。

1. **CLI 拼字**：bootstrap 次數的選項一律是 `--resamples`（`judge` 與 `sigma` 同名），§6 表寫的 `--bootstrap` 作廢；`measure` 根本沒有 bootstrap 選項（它只算讀數）。`judge` 另加 `--unseal --reason`，與 `measure` 同樣走 `Dataset.subset` 的留痕：預登記在 sealed 子集上的主張，不給理由就判不了。`sigma --method bootstrap` 沒給 `--run` 時取該 (plan, 指標, 參數) 錨點的 run（§6.3 已如此規定，§6 表的選項列漏了）。
2. **指標宣告方向**：`Metric` 多一個 `higher_is_better`（`log_loss` / `rmse` / `mae` 為 False，其餘 True，插件必須明寫，否則 `register_metric` 拒收）。judge 以 `sign = ±1` 在算 delta 時套一次，之後 `delta > 0`、`t`、σ_p 條件都維持「正的就是變好」，判決列記下 `higher_is_better`，讀 `judgements.jsonl` 的人不必回頭查登記表。§7 未提方向。
3. **VERDICT 欄位的語意**：`measure` 的 `readings=` 是這次新寫入的列數（另有 `cached=`），`guardrail=` 為 `OK|partial|none|cached`（`cached` 只表示沒有新列，不表示沒驗）；`report` 的同類欄位叫 `rows=`，一個名字不得指兩個量。同一條規則下 `ingest` 的子集大小欄位叫 `in_subset=`（不是 `samples=`）：`run.yaml` 的 `PredictionFile.samples` 已經是「實際寫出的列數」，`predicted=` / `empty=` 把 `in_subset=` 拆成兩半。護欄中止的 VERDICT 除了 `reason=` 還帶 `guardrail=FAIL anchor=<reading_id> got=<值>`（`VcpError.fields`，spec 9 要的機器可讀）。
4. **數值與台帳的硬規則**：`--tolerance` 須有限且 ≥ 0（0 = 要求完全重現，合法；nan/inf 會讓護欄形同虛設）；讀數、判決、σ_p 的非有限值一律拒寫（pydantic 會把 nan 寫成 JSON null，只增不改的台帳事後救不回來）；`t` 在 se = 0 時以 ±1e9 代替 §6.2 寫的 inf；快取命中的 cell 仍重驗錨點；judge 缺讀數是 `verdict=FAIL reason=missing_readings`（不是例外）；σ_p 估到 0 時 `sigma` WARN、判決列記 `sigma_zero`（條件仍然成立，但要說出來）。
5. **status / report 唯讀**：兩個命令不 append 任何台帳，也不建立 measure 目錄（每個 CLI 命令都會寫 `logs/` 的一列，那是共同行為，不是這兩個命令的例外——它們不寫的是資料）。資料集存在但還沒量過任何東西時 `report` 答 `rows=0` 而不是報錯；資料集根本不存在時兩個命令都 FAIL（每次台帳讀取都有 `is_file()` 保護，否則打錯 `--dataset` 會答成 `preregs=0 judged=0`，正是孤兒偵測器自己給出的假安全）。孤兒預登記的年紀以 `prereg.log.jsonl` 的時戳算（不是 yaml 裡呼叫者填的 `created_at`），沒有 log 列的 yaml 不算孤兒。`status` 的 run 數只算 card 指向本資料集的 run；`runs/` 是全機共用，讀不動的 run 卡只計數不拋錯（`runs=N unreadable=M`，`M > 0` 即 WARN、`--json` 列出路徑），否則別人的一張壞 `run.yaml` 會讓所有資料集的 `status` 都 FAIL。`report` 每個 (run, subset, metric, params) 取最新那筆讀數、每個預登記取最新那筆判決（同毫秒以台帳順序後寫者勝），`--metric` / `--plan` 對讀數與判決兩個半邊一起套用，數值不四捨五入。
6. **預測來源**：`export_manifest_sha` 記在每個子集的 `PredictionFile` 上——`vcp data export` 一次只匯出一個子集，涵蓋兩個 eval 子集的 run 本來就有兩份 manifest；`RunSource.export_manifest_sha` 是建立該 run 的那一次匯出。`--framework` / `--notes` 是 run 層級的事實，第二次 ingest 給了不同值即 FAIL。
7. **登記表推導**：任務適用性一律從登記表推（`TaskSpec.pred_payload` → `payload_field(task)`），轉換器與指標都不得寫死任務名。det 指標與 `yolo_txt` 目前只認 view 0（YOLO 匯出 manifest 沒記匯出的是哪個 view），遇到 view ≠ 0 即定位的 FAIL（→ §16：manifest 現在記匯出的 view 索引，但 `yolo_txt` 仍只對 view 0 去正規化，限制不變）；seg 的 polygon 與壓縮 RLE 都走 pycocotools 光柵化，兩者才對得起來。
8. **錯誤一律定位**：空子集、缺 gold、缺預測、壞台帳列、壞 `anchors.json` 都是帶位置的 `ValidationFailed`；bootstrap 內某次重抽讓指標拒答時，連同重抽序號與 seed 一起拋出，絕不靜默跳過那次重抽（跳過會讓 σ_p 偏小）。→ 後半已由 §16-1 推翻：`bootstrap_sd` 改為跳過並計數，`paired_bootstrap`（judge 用）維持拋出。
9. **判決列記 `metric_version`**：§4.6 的欄位表漏了它。判決要能只靠台帳重現——§7 說實作一改就升版，沒有這個欄位的讀者得循 `reading_ids` 回查讀數才知道旁邊那些數字是哪一版算的。值取自該判決用到的讀數；基準與候選的版本不一致不是「要記哪一版」的問題，而是這個比較不該做（兩邊由不同的程式碼算出），與「兩個 plan」同級，是帶訊息的 `ValidationFailed`、不寫判決列。完全沒有讀數時（`missing_readings` 的 FAIL）記登記表當下的版本。
10. **`--plugin` 只有前六個命令有**：§6 的「共用選項」句子說八個命令都有，實際上 `status` / `report` 沒有——它們不碰任何登記表（README 已如此寫）。σ_p 方法軸的登記式是 `register_sigma_method(name, fn)`，估計器簽名為 `fn(SigmaContext) -> (value, inputs)`（§2.1 成本欄的 `estimate(ctx) -> SigmaEstimate` 是概念寫法：`estimate_sigma_result` 才把值與 inputs 包成 `SigmaEstimate` 並定 id）；重複登記即 `RegistryError`，未知 `--method` 的訊息讀活的登記表，所以插件登記的方法會出現在裡面。
11. **修正輪改了 `macro_f1` 的語意但沒升版**：子集裡沒有 gold 的類別改記 `None` 並排除在巨集平均外（與 `macro_auc` / `dice` / `miou` / `coco_map` 一致）。§7 說「實作變更就升版」，目的是保護既有讀數；合併前沒有任何已寫入的讀數綁著版本 1，升到版本 2 只會留下一個永遠不存在讀數的版本號，所以維持 `metric_version = "1"`。合併後任何改變指標數值的修正都必須升版。

## 16. v3 補充決定（Hygiene A 量測層，2026-09-06）

Hygiene C（2026-09-07）補充：ingest 的 weights_hash 衝突訊息提示 `(omit --weights to keep the recorded hash)`；省略旗標只保留已記錄的身分，不會接受不同權重，非空 hash 仍不可任意覆寫。

1. **bootstrap 對命中指標守門的重抽記 skipped**（推翻 §15-8 後半）：`bootstrap_sd` 遇到指標自己拋的 `ValidationFailed`（該次抽樣沒有 gold 框、每個類別都沒定義）就跳過該次並計數，不再整個中止。原本的行為在「小 eval 子集 + 多負樣本」下必然踩到，而使用者只能換 seed 或換子集，那不是可行動的訊息。跳過確實讓答案偏向可評分的抽樣，所以偏差有上界：`skipped > used`（超過一半）即 `ValidationFailed`（`too_many_skipped: …`，訊息帶三個數字）。σ_p 估計的 `inputs` 一律記 `resamples`（要求數）、`used`、`skipped`，`vcp eval sigma` 在 `skipped > 0` 時 VERDICT 帶 `skipped=` 並 WARN。指標的插件 bug（非 `ValidationFailed` 的例外）仍然 ABORT；`paired_bootstrap`（judge 用）維持全部拋出——判決比較的是兩個指名的 run，不得靜默丟掉比較的一部分。
2. **VERDICT 欄位改名**（接 §15-3 的「一個名字不指兩個量」）：`sigma` 的 `cached=`（bool）改為 `existing=`（int，0 或 1），與 `measure` 的 `cached=`（列數）同形；`report` 的 `judgements=` 改為 `deltas=`，因為它數的是判決 × 子集的列數，而 `status` 的 `judged=` 數的是主張；`anchor` 的 VERDICT 加 `dataset=`（它的資料集來自 run 而非選項，原本沒有任何欄位說出它改了誰的 `anchors.json`）。
3. **登記表名稱驗證**：`register_metric` 與 `register_sigma_method` 拒收不符 `^[A-Za-z0-9][A-Za-z0-9._-]*$` 的名稱（`RegistryError`）。名稱會進 VERDICT 的**欄位名**（`eval status` 的 `sigma[<metric>/<method>]=`），而 `Verdict.line()` 只跳脫值不跳脫欄位名，含空白或 `=` 的名字會產生無法解析的一行。
4. **單一來源**：三個台帳檔名（`READINGS_LEDGER` / `JUDGEMENTS_LEDGER` / `SIGMA_LEDGER`）宣告在 `vcp/measure/ledger.py`；`key=value` 選項的真值字彙是 `vcp.core.config.TRUE_VALUES` 與 `is_true(value)`（提交層的 `option_is_true(options, key)` 包在它外面）；主張的元件類別是 `vcp/measure/schema.py` 的 `ComponentClass` / `COMPONENT_CLASSES` / `TUNING_CLASS`。行為不變。
5. **`PredictionFile.format_version`**（§4.3 的欄位表漏了它）：`vcp eval ingest` 記下該次用的 `Converter.version`。轉換器改了讀法就升版，只記名稱的 run 卡說不出這批預測是哪一版讀出來的。欄位可為 null，舊 run 卡仍可載入；融合寫出的 `PredictionFile`（`format_in="fuse:<method>"`）不經轉換器，維持 null。
6. **σ_p bootstrap 記 `prediction_sha`**：估計是「從某個預測檔算出來的一個數」，`inputs` 記下該檔的 sha256。`estimate_id` 由 `inputs` 推導，所以 `ingest --replace` 之後同一組參數會算出新估計而不是拿回舊的；舊列留在只增不改的台帳裡。
7. **`create_prereg` 全有全無**：yaml 寫完之後 log append 失敗即刪掉 yaml 再往上拋。只有 yaml 沒有 log 列的 id 既判不了（judge 要求 log 列）也重登記不了（路徑已存在），等於白燒一個進 git 的 id；log 只增不改，所以能收回的是 yaml 那一半。
8. **預登記門檻的下界**：`t_min >= 0`、`min_bases >= 1`、`sigma_ratio > 0`。`min_bases 0` 讓「零個正基底」也過，`sigma_ratio <= 0` 讓 `mean_delta >= ratio × σ_p` 對任何候選都成立，負的 `t_min` 會把 bootstrap 說在變壞的子集算成基底。比 nan 輕，而且在已提交的 yaml 裡看得到，所以在邊界就擋。nan 由這些界擋掉（與 nan 的比較恆為 False），+inf 由既有的有限性檢查擋掉。
9. **`report --plan` 不藏 `missing_readings` 的判決**：這種判決沒有讀數（所以沒有 plan）也沒有 per-subset 結果（所以沒有列），兩頭都看不見。`last_vs_last` 改為：判決沒有任何 per-subset 結果時輸出一列，`subset` / `delta` / `t` 皆為 null；plan 從判決的讀數推導，沒有讀數時退回它比較的兩個 run 的 `run.yaml`（讀不動的卡不貢獻 plan，唯讀視圖不因一張壞卡而死）。`deltas=` 因此是「判決 × 子集列數，沒有子集的判決算一列」。
10. **`set_anchor` 的暫存檔名唯一**：`tempfile.NamedTemporaryFile(dir=…, prefix="anchors.", suffix=".tmp", delete=False)` + `os.replace`，不再是固定的 `anchors.json.tmp`（兩個程序共用同一個路徑時，後者會在任一方 `os.replace` 之前截斷前者的內容，而任一方失敗都會刪掉對方的檔）。上鎖不在範圍：`set_anchor` 仍是未上鎖的讀改寫。
11. **`--method` 的說明不寫死內建名**：σ_p 估法是擴充軸，help 字串列三個內建名等於宣告那就是全部答案。改為 `registered sigma_p method`；未知方法的 FAIL 訊息讀活的登記表，插件登記的方法會出現在那裡。

- **CLI 失敗身分**（2026-09-07）：八個 eval 命令透過 run_command(context=) 保留已知 dataset / run / plan / subset / prereg 等識別欄位。可選欄位未給時省略；VcpError.fields 優先，JSON fields 與 VERDICT 相同；不為識別額外讀檔。

## 17. 稽核 Wave 0 補充決定（2026-09-11，VCP-008）

- **預登記的身分是第一筆 log 列**：`load_prereg` 只信任 bytes 仍 hash 到 `prereg.log.jsonl` 裡該 id **第一筆**列的 yaml。沒有列的 yaml（手寫的，或 log 寫入失敗後留下的）→ `ValidationFailed("not_found: … never registered")`；hash 變了的 yaml（登記後被改）→ `IntegrityError("mismatch: …")`，`location` 是 yaml 路徑，兩者 `fields={"prereg": id}`。同一 id 之後再出現的列不改變綁定（§4.5 的「第一行贏」從時間延伸到內容）。要改主張就換 id。
- **傳播**：`judge`、提交層的 gate（準入判決對應的預登記）、備份層的 `judgement:` 走法都經 `load_prereg`，所以一份被竄改的預登記會讓判決、staging 與 `backup manifest --conclusion judgement:` 一起 FAIL；`backup manifest --conclusion all` 對 `not_found` 容忍（記進 `skipped`），對 `mismatch` 不容忍（`IntegrityError` 不是 `ValidationFailed`）——被竄改的證據不該靜靜地從撤離清單消失。`eval report` / `status` 的孤兒偵測只用 `prereg_time`，不受影響。
- **既有預登記的稽核**（不改歷史）：合併當天檢查 repo 內 `configs/datasets/rsna-knee/prereg/` 的三份 yaml 對第一筆 log 列——3/3 相符，無需新 id。
