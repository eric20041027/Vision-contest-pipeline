# vcp run 的證據檔與標籤集（VCP-040 + VCP-042）設計

- 日期：2026-09-26
- 來源：RSNA 第二輪回報的 VCP-040（沒有把證據檔以 SHA256＋角色綁進 run 紀錄的正式 API）與 VCP-042（run card 無法宣告實際使用的標籤集），兩件合為一件；稽核文件第 16 節。
- 使用者的決定（2026-09-26）：
  1. 證據檔**複製成不可變產物**，run 紀錄只放參照。
  2. 標籤集要**檢查並擋外洩**，而不是只記錄。
  3. 做法採「兩種產物＋參照清單」：標籤集由資料命令驗證後建立、可跨 run 共用；一般證據複製成產物；`run.yaml` / `train.yaml` 各加一張清單。
- 版本：隨 0.11.0 發出（MINOR）。

## 1. 問題

run 實際讀的東西，除了 dataset 本身與 `vcp data export` 目錄，vcp 都沒有地方記：

- **衍生標籤**：偽標籤、soft label、蒸餾標籤。同一個 dataset / plan / 訓練列只換標籤時，`run.yaml` 的 `export_manifest_sha` 仍指向原標籤的 export，只看 `run.yaml` 會誤判這個 run 用的是 dataset 的標籤。
- **其他輸入**：teacher 預測、外部像素語料的存取收據、推導出的 manifest。

比賽端的替代做法是拿 `Session.register_checkpoint(path, final=True)` 登記 JSON。這讓 `weights_hash` 變成那份 JSON 的 sha，該檔也被算進 `unbacked`、被 `train upload` 上傳。更嚴重的是**外洩沒有防線**：偽標籤若標到了驗證或 sealed 子集的樣本，vcp 完全看不到。

## 2. 範圍

做：

- 新產物種類 `label_set`，由新命令 `vcp data labels` 驗證後建立（§3.1、§4.1、§5.1）。
- 新產物種類 `evidence`：一般證據檔的不可變副本（§3.2）。
- `train.yaml` 與 `run.yaml` 的 `evidence` 參照清單（§3.3）。
- 附上的入口：`vcp train run --evidence / --labels`、`Session.attach_evidence / attach_labels`、`vcp eval ingest --evidence / --labels`（§5）。
- 下游接上：`train status`、`eval status`、備份清單與 verify、provenance 圖（§5.5、§6）。

不做：

- 標籤**內容**的驗證（類別、值域）。vcp 只記錄與防外洩，怎麼解讀由訓練程式決定。
- 事後替已跑完的 run 補掛證據的專用命令；`vcp eval ingest` 的選項已涵蓋 ingest 流程。
- 目錄型證據（一次附一個檔；要整批就先打包）。
- 角色登記表：`role` 是自由字串，只有 `labels` 保留給標籤集。
- 證據對 provenance 等級（`receipt > export > declared`）的影響：證據不改等級。

## 3. 資料模型

### 3.1 `label_set/<id>` 產物

由 `vcp data labels` 經 `ArtifactWriter` 建立，寫一次不改：

| 檔案 | 內容 |
|---|---|
| `labels.csv` 或 `labels.jsonl` | 標籤檔的副本（檔名固定，副檔名依來源） |
| `label_set.json` | 驗證摘要：`dataset`、`samples_hash`、`plan_id`、`subsets`（允許的子集，排序後）、`id_field`、`id_col`、`format`（`csv` / `jsonl`）、`rows`、`matched`（每個允許子集的列數）、`external`（不在 dataset 裡的列數） |

`ArtifactSpec`：`kind="label_set"`、`id=<--id>`、`dataset`、`plan_id`、`params={"samples_hash", "subsets"（逗號串）, "id_field", "id_col", "format"}`、`inputs=[InputRef(name="labels", sha256=<來源檔 sha>)]`。

- 同 id 同 spec（含來源 sha）重跑 → 沿用（`store.reuse`），VERDICT `reused=true`。
- 同 id 但 spec 不同 → `store.reuse` 的 `spec_mismatch:` FAIL。
- `params` 帶 `dataset` 與 `samples_hash`，provenance 圖會自動連出「dataset → label_set」的 `PRODUCED_BY` 邊（§6.2）。

### 3.2 `evidence/<id>` 產物

一般證據檔的副本，不驗內容：

- id：`<run>-<name>-<來源 sha 前 12 碼>`，符合 `validate_name`。`--resume` 時同名同 bytes 會算出同一個 id，`store.reuse` 直接沿用，不再複製。
- 檔案：一個，保留來源的檔名（`check_relative_path` 不過 → FAIL）。
- `ArtifactSpec`：`kind="evidence"`、`dataset`、`plan_id`、`params={"run", "name", "role"}`、`inputs=[InputRef(name="source", sha256=<sha>)]`。

### 3.3 `EvidenceRef` 與 run 紀錄

```python
class EvidenceRef(_Strict):
    name: str                 # validate_name；一個 run 內的身分
    role: str                 # validate_name；"labels" 只給 label_set
    kind: Literal["evidence", "label_set"]
    artifact_id: str
    manifest_sha256: str      # 附上時該產物 manifest.json 的 sha256
    attempt: int | None       # 附上時的 attempt；ingest 附上時為 None
    attached_at: str          # UTC stamp
    binding: Literal["cli", "session", "manual"]
```

- `TrainRecord.evidence: list[EvidenceRef]` 與 `RunCard.evidence: list[EvidenceRef]`，預設空清單。
- **空清單不寫出**：兩個模型的序列化在 `evidence` 為空時省略這個鍵。沒用到證據的 run，舊版 vcp 仍讀得動它的 `run.yaml` / `train.yaml`；用了證據的只有 0.11.0 以上讀得動。
- 標籤集的參照：`name` = label_set 的 id、`role="labels"`、`kind="label_set"`。
- **清單只增**：
  - 同名同 `manifest_sha256` 再附一次，不增列（冪等）。
  - 同名不同 bytes → 新增一列；每個名稱**最新的那列是現行版本**，較舊的列是歷史。規則跟 checkpoint 的路徑一樣。
  - 同名但 `role` 或 `kind` 不同 → FAIL `evidence_conflict:`。
- `train run` 結束時把 `train.yaml` 的清單合進 `run.yaml`，做法跟 `access` 相同：`train.yaml` 的列在前，`run.yaml` 上有、`train.yaml` 沒有的列（例如 ingest 掛的）接在後面；以 `(kind, artifact_id)` 去重。
- `train.log.jsonl` 新增事件 `evidence`，payload：`name`、`role`、`kind`、`artifact_id`、`manifest_sha256`、`binding`。

## 4. 驗證規則

### 4.1 標籤集的分類（`vcp data labels`）

在 vcp 自己的命令程序裡讀整個 dataset，跟 `vcp data audit` 一樣，不經存取器、不留收據、不算 unseal。依 `--id-field` 建出「外部 id → sample_id」的對應：

| `--id-field` | 一個樣本貢獻的 id |
|---|---|
| `sample_id` | `sample_id` |
| `view_path` | 每個 view 的 `path` |
| `view_stem` | 每個 view 的檔名去副檔名 |
| `meta.<key>` | `sample.meta[<key>]`（缺這個鍵的樣本不貢獻 id） |

- 對應本身有兩個樣本共用同一個 id → FAIL `duplicate_id:`（這個 id 欄位分不開樣本）。
- 標籤檔每列取 `--id-col`（預設 `id`）的值，轉成字串、去掉頭尾空白後分類：
  1. 對應到的樣本在 `--subset` 列出的子集 → **通過**，計入 `matched[<子集>]`。
  2. 對應到的樣本在 dataset 裡、卻不在那些子集（其他子集，或沒被 plan 分配）→ **FAIL** `labels_outside_subsets:`。訊息只列各子集與角色的計數和前 5 個 sample id，不列標籤內容；例如 `12 in valA (eval), 3 in holdout (sealed)`。
  3. 對應不到任何樣本 → **外部**（外部語料、比賽測試集），允許，計入 `external`。
- 同一個 id 在標籤檔出現兩列 → FAIL `duplicate_id:`。
- `--subset` 不在 plan 裡 → `PlanMismatchError`；子集角色是 `sealed` → FAIL `labels_on_sealed:`。
- plan 與 dataset 不相符（`assert_plan_matches`）→ 照既有規則 ABORT。

### 4.2 掛上時的相容檢查

把 `label_set` 掛到 run（三個入口都一樣）時：

- label_set 的 `dataset`、`samples_hash`、`plan_id` 都要等於 run card 的。
- 它的 `subsets` 要 ⊆ run 的 `trained_on`。
- 產物本身用產物層自己的字：找不到 → FAIL `not_found:`；有目錄、沒有 `manifest.json`（沒 commit）→ FAIL `partial:`；`store.verify` 不過 → FAIL `mismatch:`。

`labels_mismatch:` 只給「產物好好的、但不合這個 run」的 label_set：`dataset`、`samples_hash` 或 `plan_id` 不同，或 `subsets` ⊄ `trained_on`；訊息指出是哪一項。`evidence` 產物掛上時只檢查存在與 verify。

### 4.3 證據原檔在訓練期間變動

`vcp train run --evidence NAME=PATH` 在子程序啟動前複製並雜湊原檔；訓練結束後再雜湊一次。原檔不見了或 bytes 變了 → WARN `evidence_changed=<名稱,…>`，因為訓練可能讀到的不是附上的那一版。附上的副本不變，事件裡記下這個 WARN。Session 附上的證據不做結束時的比對：它附上的就是當下讀的那一版。

## 5. 命令與 API

### 5.1 `vcp data labels`

```
vcp data labels --name D --plan P --subset S [--subset S2 …] --file PATH
                --id-field sample_id|view_path|view_stem|meta.<key> [--id-col C] --id L
                [--notes TEXT] [--json] [--data-root …] [--configs-root …]
```

- 格式看副檔名：`.csv`（UTF-8，容許 BOM，需要標頭列）、`.jsonl`（每行一個物件）。其他副檔名 → FAIL `unsupported_format:`。
- 找不到檔 → FAIL `not_found:`；缺 `--id-col` 那一欄或那個鍵 → FAIL `not_found: column`。
- VERDICT：`id= dataset= plan= subsets= rows= matched= external= reused=`；`matched` 是所有允許子集的合計。
- 只讀 dataset，只寫 `artifacts/label_set/<id>/`。

### 5.2 `vcp train run --evidence NAME=PATH --labels L`

兩個選項都可重複。

- `--evidence` 的路徑相對於執行 vcp 的目錄（同 `--config`、`--export`）；`role` 預設等於 `NAME`。
- **預檢**，與其他預檢一起、在第一次寫入之前：
  - 每個 `--evidence` 的檔存在，而且是檔案（否則 FAIL `not_found:` / `not_a_file:`）。
  - 每個 `--labels` 的 label_set 存在且過 §4.2。新 run 還沒有 card，就跟這次要寫下的比：dataset 與它的 samples_hash、`--plan`、推導出的 `trained_on`。
  - 名稱不衝突。
  - 任何一項不過 → FAIL，不寫任何檔。
- **附上**：寫下 `run.yaml` / `train.yaml`、子程序啟動之前，建 `evidence` 產物（或沿用），把參照寫進 `train.yaml`（`binding="cli"`、`attempt=n`），記 `evidence` 事件。
- 結束時做 §4.3 的比對，把清單合進 `run.yaml`。
- VERDICT 新欄位：
  - `evidence=<參照數>`
  - `labels=<現行 label_set id，逗號分隔>`；沒掛就是 `labels=dataset`
  - 變動時加 `evidence_changed=`（WARN）

### 5.3 訓練迴圈的 API

```python
s = Session.current()
s.attach_evidence("teacher", "preds/teacher.jsonl", role="teacher")  # -> EvidenceRef
s.attach_labels("pseudo-v3")                                          # -> EvidenceRef
```

- 在 `vcp train run` 底下執行，attempt 取 `Session.attempt`，`binding="session"`。
- `attach_evidence` 的 `role="labels"` → FAIL `role_reserved:`（標籤要走 `attach_labels`）。
- 寫 `train.yaml` 的時機與 `register_checkpoint` 相同（包裝器只在開始前與結束後寫，沒有並行寫入）。
- `Session.register_checkpoint` 的說明改成明寫只給權重用。

### 5.4 `vcp eval ingest --evidence NAME=PATH --labels L`

給不是用 `vcp train run` 訓練的 run，模式同 `--receipt`：

- 產物 `params.run` = 這個 run。
- 參照 `binding="manual"`、`attempt=None`。
- 可在第一次 ingest 或之後任何一次 ingest 附上。
- VERDICT 加 `evidence=` 與 `labels=`。

### 5.5 查看

- `vcp train status --run R`：VERDICT 加 `evidence=` 與 `labels=`。`--verify` 另對每筆現行參照跑 `store.verify`，並比對 `manifest_sha256`；不過的列進 `drift=`，寫成 `evidence:<名稱>`。
- `vcp eval status --dataset D`：每個 run 多一欄 labels（`dataset` 或 label_set id），`--json` 的 result 同步。VERDICT 不變。
- `vcp provenance explain --entity run:R`：靠 §6.2 的邊自然列出。

## 6. 下游

### 6.1 備份

- `backup manifest` 走到 run 時，對 `run.yaml` 與 `train.yaml` 清單裡的**每一筆**參照（含歷史列），收進該產物的 `manifest.json` 與檔案，角色標 `evidence` 或 `label_set`。
- `backup verify` 的一致性層加一項：每筆參照的 `manifest_sha256` 等於清單裡那份 `manifest.json` 的 sha256。

### 6.2 provenance 圖

- `_scan_runs` 對 run card 上每筆參照，連一條「產物 → run」的 `CONSUMED_BY` 邊，不帶屬性。`evidence` 產物跟存取收據一樣，只經由 run 的清單連到 run：`params.run` 只說是哪個 run 做了這份副本，不算消費的證明。所以沒被引用的副本（例如 ingest 失敗時留下的）不會畫成被 run 消費。
- label_set 可跨 run 共用，靠的就是這條邊；它的 spec 帶 `dataset`、`params` 帶 `samples_hash`，`_artifact_dataset_sources` 據此自動連出「dataset → label_set」。所以 dataset 改版時，`impact` / `stale` 會經過它走到用它訓練的 run。
- 參照指到的產物不見了、驗不過或 `manifest_sha256` 不符 → 這個 run 的 `broken_reason` 加上 `missing or invalid evidence/<kind>/<id>`，跟收據壞掉的處理相同。
- 不改索引 schema：實體與邊的種類都是既有的。

## 7. 錯誤與判決字彙

| 情況 | 例外 | 狀態 |
|---|---|---|
| 標籤列落在允許子集以外的 dataset 樣本 | `ValidationFailed` `labels_outside_subsets:` | FAIL |
| 對應或標籤檔有重複 id | `ValidationFailed` `duplicate_id:` | FAIL |
| `--id-field` 不是 `sample_id` / `view_path` / `view_stem` / `meta.<key>` | `ValidationFailed` `id_field:` | FAIL |
| `--subset` 是 sealed 角色 | `ValidationFailed` `labels_on_sealed:` | FAIL |
| 副檔名不是 `.csv` / `.jsonl` | `ValidationFailed` `unsupported_format:` | FAIL |
| 標籤列格式不對（壞 JSON 列、空 id）或 `--evidence` 不是 `NAME=PATH` | `ValidationFailed` `invalid:` | FAIL |
| 標籤檔、欄位、證據檔或 label_set 找不到 | `ValidationFailed` `not_found:` | FAIL |
| 證據路徑不是檔案 | `ValidationFailed` `not_a_file:` | FAIL |
| label_set 與 run 不相容 | `ValidationFailed` `labels_mismatch:` | FAIL |
| 同名參照的角色或種類不同 | `ValidationFailed` `evidence_conflict:` | FAIL |
| `attach_evidence(role="labels")` | `ValidationFailed` `role_reserved:` | FAIL |
| 同 id 不同 spec 的 label_set | `IntegrityError` `spec_mismatch:`（既有） | FAIL |
| `--subset` 不在 plan | `PlanMismatchError`（既有） | ABORT |
| 證據原檔在訓練期間變動 | WARN `evidence_changed=` | WARN |

新 VERDICT 欄位：

- `vcp data labels`：`rows=` `matched=` `external=` `reused=` `subsets=`
- `train run` / `ingest` / `train status`：`evidence=` `labels=` `evidence_changed=`

## 8. 相容性與版本

- 0.11.0（MINOR）：新命令、新選項、新 VERDICT 欄位與 `reason=` 字彙、新產物種類、`run.yaml` / `train.yaml` / `train.log.jsonl` 的新內容。
- 舊的 `run.yaml` / `train.yaml` 照舊讀得動；沒用證據的新檔舊版也讀得動（§3.3 空清單不寫出）。
- provenance 索引 schema 不變；用了證據的 run 在下一次 `provenance sync` / `rebuild` 時長出新邊。

## 9. 測試

- **單元**
  - 標籤分類：四種 `--id-field`、多 view 樣本、對應重複、標籤檔重複、外部列、落在 eval / sealed / 未分配的樣本、sealed 的 `--subset`。
  - CSV 帶 BOM、JSONL 的非字串 id。
  - 重跑沿用，spec 不同 → `spec_mismatch:`。
  - `EvidenceRef` 的冪等、同名新 bytes、衝突。
  - 空清單不寫出：舊版模型（去掉新欄位的複本）讀得動新寫出的檔。
  - 相容檢查的每一項。
- **訓練**
  - `--evidence` / `--labels` 的預檢失敗時不留任何檔。
  - 附上後子程序啟動；`--resume` 沿用同一個證據產物。
  - 原檔在訓練中被改 → WARN。
  - 子程序裡用 `Session.attach_evidence` / `attach_labels`，結束後 `run.yaml` 有參照。
  - `role_reserved:`。
- **下游**
  - `eval ingest` 附上並合併。
  - `train status --verify` 抓到被改的證據產物。
  - `backup manifest` 收到證據、`verify` 抓到 sha 不符。
  - provenance 圖有邊、壞產物讓 run broken。
  - `eval status` 的 labels 欄。
- **端到端**：`data labels` → `train run --labels --evidence`（子程序另附一份）→ `backup manifest` / `verify` → `provenance explain`，最後掃 VERDICT 與 `logs/`：只出現 id 與計數，不含標籤檔的內容。

## 10. 文件與 skill

- `docs/reference/cli.md`：`data labels`、`train run`、`eval ingest`、`train status`、`eval status` 的列，以及訓練迴圈範例。
- 修訂條目：
  - 訓練層 spec：`--evidence` / `--labels`、Session API、`evidence` 事件。
  - 量測層 spec：`ingest` 選項、`RunCard.evidence`。
  - 備份 spec：walk 與一致性層。
  - 資料層 spec：`data labels`。
  - 四條都指回本 spec。
- `CLAUDE.md` 與 `AGENTS.md` 的路徑段：`artifacts/label_set/<id>/`、`artifacts/evidence/<id>/`、run 紀錄的 `evidence` 清單。
- skill：`vcp-data-pipeline`（`data labels`）、`vcp-train-submit-backup`（附證據、附標籤、`register_checkpoint` 只給權重），鏡射 `.agents/skills/`。
- 稽核文件第 16 節把 VCP-040 / 042 標為已修（隨 0.11.0）。
