# vcp 融合層設計（子專案 4）

- 日期：2026-09-05
- 狀態：v1（brainstorming 逐節核可後寫成）
- 前置：子專案 0/1 資料層（`2026-09-02-vcp-skeleton-and-data-layer-design.md`）、子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md`，含 §15 v2 補充決定）
- 來源：賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md` §3（最終配方）、§4（成功線 / 否證線）、§8（決策系統評估，缺陷 2）、§9 藍圖第 6 點（組件準入）
- 後續：子專案 3 訓練層 → 5 提交治理 → 6 備份審計

## 1. 目的

把多個 run 的預測依一份**寫在 git 裡、寫了不改**的配方合成一個新的 run，並用量測層既有的預登記 / 判決機制，機械地回答「每一位成員是否證明了自己的位置」。

海廢報告的兩個結論決定了這一層的形狀：

- 融合是最大單步增益來源（§4.1 golden7 +0.0023、成員軸轉移率最高），所以它是框架的一層，不是每場比賽重寫一次的腳本。
- 對不同組件的驗證標準不一致是結構性缺陷（§8 缺陷 2：skip 被要求 ≥2 基底並做到，rescorer 從未受同標準檢驗卻進了配方）。所以準入不是這一層自己發明的門檻，而是**把「有它 vs 沒它」變成量測層的一份預登記**，讓 `vcp eval judge` 用同一把尺判。

不在本 spec：權重 / iou / skip 的搜尋、class-aware 權重、seg 融合、TTA、內建 rescorer、跨 dataset 套用配方、上傳。

## 2. 核心架構原則

- **融合結果就是 run。** 產出落在 `runs/<run_id>/`，有 `run.yaml` 與 `predictions/<subset>.jsonl`，量測層看不出它是融合來的；`trained_on` 取成員聯集，所以乾淨子集的判定自動正確。
- **配方進 git、CLI 寫、寫了不改。** 與 plan、prereg 同規矩：要改就換 id。
- **位元級重現是預設，不是選項。** 同成員預測 sha + 同配方 ⇒ 同輸出 sha。`fuse.json` 記下每個成員檔的 sha 與輸出 sha，就是報告 §3 那行「member_csv 12 檔 → wbf_fuse → md5」。
- **以資料形態為軸。** 融合器依 payload（boxes / scores / targets）登記；加一種融合 = 加一個登記項；比賽專屬的後處理（rescorer 之類）以插件融合器放 `projects/<contest>/`，核心不內建、不出現比賽名稱。
- **機械防護先於自覺。** 成員不同源、payload 不符、sha 不符、候選已量測——在命令層擋下，什麼都不寫。

### 2.1 擴充點

| 軸 | 登記式 | v1 內建 | 加一項的成本 |
|---|---|---|---|
| 融合器（`FUSERS`） | `register_fuser(fuser)`，同指標登記表 | `wbf`（boxes）、`mean`（scores、targets）、`rank_mean`（scores） | 一個檔案；宣告 `payloads` 與 `defaults`，實作 `fuse(members, ctx)` |

`--plugin <module>` 在三個命令上都有：匯入該模組讓它登記融合器（同 `vcp eval` 的插件語意）。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 輸入單位 | 只融合預測檔（run 層級）；TTA、多 checkpoint 由推論端各自產 run 再進來 | 不需要任何框架 venv；與量測 spec §11「融合後的預測也是一個 run」一致 |
| 任務覆蓋 | boxes（det）、scores（cls / multilabel）、targets（regression）；masks 留登記項 | 海廢是 det，RSNA 是 multilabel；實例分割的融合定義不清、目前無賽事 |
| 命令 | `recipe` / `build` / `ablate`，無權重搜尋 | 移除軸（M1）與準入（§9.6）需要消融；搜尋是 val282 名義峰三度不轉移的主要入口，留 v2 |
| 架構 | 產 run 的一層；準入 = 既有 `vcp eval judge` | 量測層零新機制；唯一改動是把 `prereg._measured_subsets` 公開（行為不變） |
| WBF 實作 | numpy 自實作，語意對齊 ensemble-boxes `weighted_boxes_fusion(allows_overflow=False)`，在像素座標運算 | 不引入 numba / pandas；不需影像尺寸；ensemble-boxes 只當可選的測試 oracle，不進依賴 |
| 配方綁定 | 配方綁 dataset + plan；test 集是另一個 dataset，另寫一份配方 | run 綁 dataset，配方裡的成員是 run id；兩側 method / params / weights 的核對留子專案 5，資料在 `fuse.json` |
| 準入預登記 | 固定 `component_class=model`，不暴露 σ_p 選項 | 成員去留是模型軸的變化；σ_p 條件只對 tuning 類生效 |
| rescorer 類後處理 | 不內建；以插件融合器實作 | 報告 §9.6：自家後處理是儀器過擬合的頭號宿主，進核心等於默許 |
| 單成員配方 | `build` 允許（WBF 仍會合併同模型的重疊框）；`ablate` 拒絕 | 少一名成員後的變體可能只剩一名；消融一名成員的配方沒有意義 |

## 4. 資料模型（`src/vcp/fuse/schema.py`，pydantic，`extra="forbid"`）

### 4.1 配方（`configs/datasets/<name>/fuse/<recipe_id>.yaml`，進 git）

```yaml
recipe_id: golden12
dataset: marine
plan_id: p1
method: wbf
params: {iou: "0.6", skip: "0", min_score: "0.02", max_per_image: "300", conf_type: "avg"}
members:
  - {run: codino_ft_tta, weight: 1.0}
  - {run: ccbb_swin, weight: 0.5}
notes: ""
created_at: <stamp>
```

- `recipe_id` 經 `validate_name`（path-safe）。`params` 一律字串、寫入的是**有效參數**（預設值已填滿，同 prereg 對指標參數的做法）。
- `members` 至少一名；`run` 不得重複；`weight` 為有限正數；**順序有意義**（融合器的 tie-break 依成員順序），寫入時保留。
- 配方 sha = 檔案位元組的 sha256，寫進融合 run 的 `source.config_hash`，一個 run id 只綁一份配方。
- 檔案由 `vcp fuse recipe` 或 `vcp fuse ablate` 寫；已存在即 FAIL；沒有任何命令會改寫它。

### 4.2 融合紀錄（`runs/<run_id>/fuse.json`）

```json
{
  "run_id": "fuse-golden12",
  "recipe_id": "golden12",
  "recipe_sha256": "…",
  "method": "wbf",
  "method_version": "1",
  "params": {"iou": "0.6", "skip": "0", "min_score": "0.02", "max_per_image": "300", "conf_type": "avg"},
  "members": [{"run": "codino_ft_tta", "weight": 1.0, "trained_on": ["train"]}],
  "subsets": {
    "valA": {"member_sha256": {"codino_ft_tta": "…"}, "output_sha256": "…", "samples": 279, "empty": 3, "built_at": "<stamp>"}
  },
  "vcp_version": "<vcp version 印的同一字串>"
}
```

每次 `build` 整檔重寫（快照，subset 逐次累積），舊值由 `history.jsonl` 留痕——與 `runs/` 的「換寫留痕」規矩相同。`member_sha256` 是該成員**該 subset** 預測檔在融合當下的 sha，`output_sha256` 與 `run.yaml` 的 `predictions[subset].sha256` 相等。

### 4.3 融合 run 的 `run.yaml`

沿用量測層的 `RunCard`，不加欄位：

| 欄位 | 值 |
|---|---|
| `run_id` | 預設 `fuse-<recipe_id>`，`--run` 可改 |
| `dataset` / `samples_hash` / `plan_id` | 取自配方與 dataset |
| `trained_on` | 所有成員 `trained_on` 的聯集，排序 |
| `source.framework` | `vcp.fuse` |
| `source.config_hash` | 配方 sha |
| `source.notes` | 配方 `notes` |
| `predictions[subset]` | `PredictionFile`：`format_in="fuse:<method>"`，`samples` / `empty` 同 ingest 的語意，`export_manifest_sha=None` |

成員可以本身就是融合 run（巢狀融合）；聯集規則讓乾淨子集的判定仍然正確。

## 5. 目錄佈局

```
configs/datasets/<name>/fuse/<recipe_id>.yaml          # 配方（git）
configs/datasets/<name>/fuse/<recipe_id>-minus-<X>.yaml  # ablate 產的變體（git）
configs/datasets/<name>/prereg/<recipe_id>-admit-<X>.yaml # ablate --preregister 產的準入預登記（既有機制）
runs/fuse-<recipe_id>/run.yaml
runs/fuse-<recipe_id>/predictions/<subset>.jsonl
runs/fuse-<recipe_id>/fuse.json
runs/fuse-<recipe_id>/history.jsonl                    # --replace 時記舊 sha
runs/fuse-<recipe_id>-minus-<X>/…                       # 變體 run，結構相同
```

程式碼：`src/vcp/fuse/{schema,recipes,build,ablate}.py`、`src/vcp/fuse/fusers/{base,wbf,scores}.py`、`src/vcp/cli_fuse.py`（群組 `vcp fuse`，於 `cli.py` 以 `add_typer` 掛上）。

## 6. CLI 總表（`vcp fuse`）

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `recipe --dataset D --id R --plan P --method M [--params k=v]… --member RUN[:WEIGHT]… [--notes …]` | 驗成員後寫配方 | 已存在 → FAIL `reason=recipe_exists`；成員不存在 / 缺 → FAIL；成員 dataset / hash / plan 不符 → ABORT；payload 與 method 不符 → FAIL；params 錯 → FAIL |
| `build --dataset D --recipe R [--run ID] [--subsets a,b] [--replace]` | 驗成員與 sha → 逐 subset 融合 → 一次寫入 | 全部快取命中 → OK `cached=`；輸出已存在且不同、無 `--replace` → FAIL；成員缺該 subset → FAIL；成員 sha 不符 → FAIL；run id 已綁別的配方 → FAIL |
| `ablate --dataset D --recipe R [--subsets a,b] [--no-build] [--preregister --metric M [--metric-params k=v]… [--bases valA,valB] [--t-min 2.0] [--min-bases 2]]` | 每位成員一份 minus 變體：寫配方、建 run、（選）寫準入預登記 | 單成員 → FAIL `reason=single_member`；變體配方已存在且內容不同 → FAIL；候選 `fuse-R` 已量測 → FAIL `reason=candidate_measured`；預登記 id 已存在 → FAIL |

共用選項：`--json`、`--data-root`、`--configs-root`、`--plugin <module>`（可重複）。三個命令都寫 `logs/` 一行、以 VERDICT 收尾，exit 0 / 0 / 1 / 2。沒有唯讀命令：看配方讀 yaml，看成員與 sha 讀 `fuse.json`，看讀數與判決用 `vcp eval status` / `report`。

VERDICT 欄位：

- `recipe`：`cmd=fuse.recipe status=OK recipe=R method=M members=N`
- `build`：`cmd=fuse.build status=OK run=fuse-R recipe=R members=N subsets=valA,valB built=2 cached=0`（`built=` 新寫的預測檔數，`cached=` 內容相同而未寫的檔數；一個名字不指兩個量）
- `ablate`：`cmd=fuse.ablate status=OK recipe=R variants=N runs=N+1 built=<檔數> cached=<檔數> preregs=N|0`

`--json` 時結果 JSON 到 stdout（`recipe` 回配方；`build` 回 `{run_id, recipe_id, trained_on, subsets: {subset: {sha256, samples, empty, cached}}}`；`ablate` 回 `{variants, runs, preregs}` 三個 id 清單），VERDICT 到 stderr。

### 6.1 build 的子集選擇

- 未指定 `--subsets`：取**每位成員都有預測檔**的 subset 交集，依 plan 宣告順序；交集為空 → FAIL `reason=no_common_subset`。
- 指定時：每個名字必須在 plan 裡（否則 ABORT）且每位成員都有（否則 FAIL 帶 `member=` `subset=`）。
- sealed 子集：融合只讀成員的預測檔，不讀標籤，所以**不需** `--unseal`（與 ingest 相同）；量測它時才需要。
- 快取與取代：對每個 subset 先算出輸出並驗證，再比對既有檔：sha 相同 → `cached`，不寫；不同 → 無 `--replace` 即 FAIL，有則先在 `history.jsonl` 記 `{"event": "replace", "subset", "old_sha256", "via": "fuse.build"}` 再覆寫。

### 6.2 build 的寫入順序（全有或全無）

1. 讀配方、載 dataset 與 plan、`get_fuser`、驗每位成員（§8）。
2. 解析 subsets，對每位成員每個 subset 跑 `verify_prediction`（sha）。
3. 逐 subset 融合成記憶體內的 `list[Prediction]`，每個都過 `check_predictions`（插件融合器輸出的錯在這裡擋下）。
4. 比對既有輸出（快取 / 衝突）。到這裡任何失敗都還沒寫任何東西。
5. 寫入：取代事件 → 預測檔 → `run.yaml` → `fuse.json`。寫入途中的非預期例外是 ABORT；下一次 `build` 會由 `verify_prediction` 或缺項發現不一致，`--replace` 可修復。

### 6.3 ablate 的流程

輸入配方 R、成員 M（|M| ≥ 2）。對每位 X ∈ M（依配方順序）：

- 變體配方 `R-minus-X`：同 dataset / plan / method / params / notes，成員 = M 去掉 X（權重與順序不變）。已存在 → 載入比對五個欄位，相同即沿用，不同即 FAIL。
- 變體 run `fuse-R-minus-X`、完整 run `fuse-R`：以**完整配方**成員集算出的 subsets 統一建（`--subsets` 或 §6.1 交集），所有變體共用同一組 subset。`--no-build` 只寫配方（與預登記）。
- `--preregister`：每位 X 一份 `PreRegistration`：`prereg_id=R-admit-X`、`claim="recipe R: member X contributes (fused with it beats fused without it)"`、`component=X`、`component_class=model`、`baseline_run=fuse-R-minus-X`、`candidate_run=fuse-R`、`metric`、`params=--metric-params`、`subsets=--bases`（預設 `valA,valB`）、`t_min`、`min_bases`、`sigma_method=splithalf`、`sigma_ratio=1.0`。經量測層 `create_prereg` 寫入與記 log，語意不變。

全有或全無：先做完所有檢查——配方可載、|M| ≥ 2、成員有效、變體配方無衝突、對應 run id 若已存在必須綁同一份配方、`--preregister` 時指標適用於任務、候選 `fuse-R` 在該指標與各基底**沒有任何讀數**（`measured_subsets`）、預登記 id 不存在——才開始寫：變體配方 → 各 run 的 build → 預登記（最後寫，讓預登記永遠指向已存在的 run）。

### 6.4 識別字

| 東西 | 規則 |
|---|---|
| 融合 run | `fuse-<recipe_id>`（`build --run` 可改；ablate 不可改） |
| 變體配方 / run | `<recipe_id>-minus-<member run id>` / `fuse-<recipe_id>-minus-<member run id>` |
| 準入預登記 | `<recipe_id>-admit-<member run id>` |

全部經 `validate_name`；成員 run id 本來就是 path-safe，串接後仍是。

## 7. 融合器登記表

### 7.1 介面（`src/vcp/fuse/fusers/base.py`）

```python
@dataclass(frozen=True)
class MemberPredictions:
    run_id: str
    weight: float
    predictions: dict[str, Prediction]        # sample_id → 該成員的預測

@dataclass(frozen=True)
class FuseContext:
    dataset: Dataset
    subset: str
    ids: list[str]                            # subset 的 sample id，依 samples.jsonl 順序
    samples: dict[str, Sample]                # 取 view 尺寸用
    params: dict[str, str]                    # 有效參數

class Fuser(Protocol):
    name: str
    version: str
    payloads: frozenset[str]                  # ⊆ {"boxes", "scores", "targets", "masks"}
    defaults: dict[str, str]
    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]: ...

FUSERS: dict[str, Fuser]
def register_fuser(fuser: Fuser) -> None     # 重複 → RegistryError
def get_fuser(name: str) -> Fuser            # 未知 → RegistryError，訊息列出活的登記表
def effective_params(fuser: Fuser, params: dict[str, str]) -> dict[str, str]   # 未知鍵 → ValidationFailed
```

適用性由登記表推導：dataset 任務的 `TASKS[task].pred_payload` 必須在 `fuser.payloads` 裡，否則 FAIL（同量測層「指標適用性由登記表推導」）。`version` 寫進 `fuse.json`；改語意就升版。

### 7.2 `wbf`（payloads `{boxes}`，version `1`）

語意 = ensemble-boxes `weighted_boxes_fusion(iou_thr, skip_box_thr, conf_type, allows_overflow=False)`，差異只有：像素座標、依 view 分群、有尺寸才裁邊、超過 1 的 score 夾到 1。

參數（有效參數全為字串，解析失敗 → FAIL 帶 `param=`）：

| 參數 | 型別 / 範圍 | 預設 | 意義 |
|---|---|---|---|
| `iou` | float，(0, 1] | `0.55` | 併入既有融合框的 IoU 門檻（嚴格大於） |
| `skip` | float，≥ 0 | `0` | 輸入框 score 低於此值先丟（ensemble-boxes 的 `skip_box_thr`） |
| `min_score` | float，≥ 0 | `0` | 融合**後** score 低於此值丟（海廢配方的「WBF 後砍 0.02」） |
| `max_per_image` | int，≥ 0 | `0` | 每個 view 只留 score 前 N 個；0 = 不限 |
| `conf_type` | `avg` \| `max` | `avg` | 融合 score 的算法 |

每個 sample、每個 view、每個 `category_id` 獨立處理：

1. 收集所有成員在此 (view, category) 的框；`score < skip` 者丟棄。
2. 每框 q = score × 成員 weight；依 q 降冪穩定排序，tie 依（成員順序、成員檔內順序）。
3. 依序把每框與現有融合框算 IoU（x1y1x2y2 空間）；取最大者，若 > `iou` 併入該群並重算融合框：座標 = Σ q·座標 / Σ q；否則自成新群。
4. 群的最終 score：`avg` → mean(q) × min(成員數, 群大小) / Σ weight；`max` → max(q) / max weight。成員數 = 配方成員數（含在此圖上沒有框的成員）。結果夾到 [0, 1]。
5. 轉回 x, y, w, h；view 的 `width` 與 `height` 都已知時裁到 [0, width] × [0, height]。
6. 丟掉 `score < min_score` 的融合框。
7. `max_per_image > 0` 時，每個 view 依（−score、群建立順序）穩定排序取前 N。

輸出框的 `view` 與 `category_id` 沿用成員的值；一個 sample 的框依 view、群建立順序輸出。同輸入必得同輸出：所有排序皆穩定，沒有隨機。

### 7.3 `mean`（payloads `{scores, targets}`，version `1`，無參數）

每個 sample、每個 key：Σ w·v / Σ w。每位成員必須有該 sample 且 key 集合完全相同，否則 FAIL 帶 `sample=` `member=`。cls 的 scores 一樣平均，argmax 留給指標。

### 7.4 `rank_mean`（payloads `{scores}`，version `1`，無參數）

以**該 subset 的全部 sample 為母體**，每個 key、每位成員：把值換成名次（同值取平均名次），正規化為 (rank − 1) / (n − 1)（n = 1 時為 0.5），再 Σ w·r / Σ w。輸出在 [0, 1] 但不是機率——AUC 型指標用；對 `log_loss` 沒有意義（spec 記明，不擋）。不開放給 targets：RMSE 需要校準值。缺 sample / key 不齊的規則同 `mean`。

### 7.5 通用規則

- det 成員缺某 sample = 該成員對它無框（ingest 本來就允許省略）；輸出只寫有框的 sample，`empty` 由 `run.yaml` 計，與 ingest 語意一致。
- 分數類的輸出對 subset 每個 sample 各一列（ingest 對 mapping payload 的規則一致，`check_predictions` 會再驗一次）。
- 融合器只看成員預測與 sample 的 view 尺寸；**不讀標籤**。
- 融合器擲出的 `VcpError` 依其類別映射狀態；其他例外 = ABORT。

## 8. 成員驗證與不可變性

`recipe` 與 `build` 都跑（`build` 再多驗 sha）：

| 檢查 | 失敗 |
|---|---|
| 成員 run 存在（`load_run`） | `ValidationFailed` FAIL `member=` |
| `assert_run_matches`（dataset 名與 `samples_hash`）與 `plan_id` 相同 | `PlanMismatchError` ABORT `member=` |
| 任務 payload ∈ `fuser.payloads` | `ValidationFailed` FAIL `method=` `payload=` |
| `members` 無重複、weight 有限正數、至少一名 | schema 驗證 → `ValidationFailed` FAIL |
| params 鍵已知且值合法 | `ValidationFailed` FAIL `param=` |
| （build）每位成員每個 subset `verify_prediction` | 缺 → `ValidationFailed` FAIL；sha 不符 → `IntegrityError` FAIL，皆帶 `member=` `subset=` |
| （build）既有 `run.yaml` 是融合 run 且 `config_hash` = 配方 sha、`trained_on` = 聯集 | 否則 `ValidationFailed` FAIL `run=` |

不可變性：配方檔與變體配方檔寫後不改；`run.yaml` 與 `fuse.json` 換寫留痕；預測檔取代必經 `--replace` 並記舊 sha；預登記走量測層的規矩。

## 9. 錯誤處理與 VERDICT 字彙

| 情況 | 例外 | 狀態 | 額外欄位 |
|---|---|---|---|
| 配方 / 變體配方 / 預登記 id 已存在 | `ValidationFailed` | FAIL | `recipe=` 或 `prereg=` |
| 成員不存在、缺 subset 預測檔、payload 不符、params 錯、無共同 subset、輸出衝突、run id 已綁他配方、單成員消融、候選已量測、融合器輸出未過驗證 | `ValidationFailed` | FAIL | `member=` `subset=` `run=` `param=` `reason=` 視情況 |
| 成員 dataset / hash / plan 不符、subset 不在 plan | `PlanMismatchError` | ABORT | `member=` 或 `subset=` |
| 成員預測檔 sha 與 `run.yaml` 不符 | `IntegrityError` | FAIL | `member=` `subset=` |
| 未知 method、插件重複登記 | `RegistryError` | ABORT | `method=` |
| 融合器非 `VcpError` 例外、寫入途中 I/O 錯 | — | ABORT | `reason=` |

`reason=` 的固定字彙：`recipe_exists`、`no_common_subset`、`single_member`、`candidate_measured`、`output_exists`、`run_bound_elsewhere`、`variant_conflict`。全部經 `VcpError.fields` 進 VERDICT（量測 spec §15 第 3 點的機器可讀規矩）。

## 10. 與其他子專案的介面

- **資料層**：讀 `Dataset`、`SplitPlan`（`subsets` 順序、`ids_in`）、`TASKS[...].pred_payload`、`Sample.views[].width/height`；零改動。
- **量測層**：呼叫 `load_run` / `save_run` / `assert_run_matches` / `verify_prediction` / `append_history`、`read_predictions` / `write_predictions` / `check_predictions`、`create_prereg`、`get_metric` / `effective_params`（指標）、`ReadingsLedger`；唯一改動是把 `prereg._measured_subsets` 公開為 `measured_subsets(readings, pr, params_key)`（簽名與行為不變）。準入 = `vcp eval judge`。
- **訓練層（3）**：成員來自訓練層的 `ingest`；本層不需要它。
- **提交層（5）**：讀 `fuse.json` 取成員 sha 與輸出 sha；test 側的配方另寫；eval 側與 test 側 method / params / weights 的核對規則由 5 定。
- **文件**：README 新增「融合層命令 `vcp fuse`」一節（三個命令、一輪準入的流程）；CLAUDE.md 台帳路徑加 `configs/datasets/<name>/fuse/` 與 `runs/<id>/fuse.json`，常用命令加 `uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M`，通用性原則的變異軸清單補上轉換器、指標、σ_p 方法、融合器。

## 11. 測試策略

- **單元**：配方 schema（重複成員、非正 weight、空成員、未知 params）與不可變（重寫 → FAIL）；§8 每條失敗路徑各一個測試；`wbf` 手算案例——兩成員重疊框的座標加權與 `avg` / `max` 公式、`skip`、`min_score`、`max_per_image`、單成員自合併、view 與 category 分群、夾到 1、無尺寸不裁邊；ensemble-boxes 當可選 oracle（`pytest.importorskip`，不進任何依賴群組）；`mean` 與 `rank_mean` 手算（含同值平均名次、n = 1）；決定性（同輸入兩次同 sha、成員順序不同但 q 無 tie 時同結果）；`build` 的 cached / replace / 衝突 / 寫入順序；`ablate` 的變體內容、沿用與衝突、全有或全無（候選已量測 → 一個檔都沒寫）、預登記欄位；`fuse.json` 內容與 `run.yaml` 一致。
- **端到端（CLI）**：合成 det 資料集 + 三個合成 run（對 / 噪音 / 半對）→ `fuse recipe` → `fuse ablate --preregister --metric coco_map` → `eval measure` × 4 → `eval judge` × 3：「對」PASS、「噪音」FAIL、「半對」的 per_subset Δ 符號正確；合成 multilabel 資料集 + 兩個 run → `mean` 與 `rank_mean` 各 build 一次 → `eval measure` macro_auc 有值；`--plugin` 登記一個測試融合器後可用於 `--method`。
- **真資料**：`tests/integration/` 對 RSNA 子集以標籤造兩個完美 run → `rank_mean` 融合 → macro AUC = 1.0；資料缺席即 skip。
- 覆蓋率 ≥ 80%；ruff 乾淨；不新增執行期依賴。

## 12. 驗收條件

1. `vcp fuse recipe` / `build` / `ablate` 在合成 det 與合成 multilabel 各成功一次，產出符合 §4 的檔案。
2. 同配方 `build` 兩次，第二次 `cached=` 等於 subset 數且不寫檔；把一位成員的預測檔改一個位元再 `build` → `IntegrityError` FAIL 且沒有任何新檔。
3. `ablate --preregister` 對 N 名成員產 N 份變體配方、N + 1 個 run、N 份預登記；候選已量測時 FAIL `candidate_measured` 且一個檔都沒寫；§11 的三個合成 run 判決符合預期。
4. `--plugin` 登記的融合器可用於 `--method`；未知 method 的錯誤訊息列出登記表內容。
5. `uv run pytest` 全綠、覆蓋率 ≥ 80%、ruff 乾淨；README 與 CLAUDE.md 依 §10 更新。

## 13. 不在範圍

權重 / iou / skip 的搜尋與 OOF 防過擬合、class-aware 權重、seg（masks）融合、TTA 與多 checkpoint 推論、內建 rescorer 或任何學習型後處理、跨 dataset 套用配方與 eval/test 兩側核對（子專案 5）、NMS / soft-NMS（日後登記項）、per-class 門檻、唯讀的 `vcp fuse show`。以上皆為已預留的擴充點，不是設計缺口。
