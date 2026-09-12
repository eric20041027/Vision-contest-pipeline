<!-- 副本：來源是比賽工作區 RSNA_Knee_Abnormality_Detection/projects/rsna-knee/VCP_IMPROVEMENT_AUDIT_20260911.md（2026-09-11，由 Codex 在跑完 RSNA Knee 後寫成）。第 15 節的相對連結指向該工作區，不在本 repo。本 repo 的 Wave 0 已於 v0.3.0 完成（見 CHANGELOG）；Wave 1 拆成 1a 不可變產物（spec：docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md）、1b 角色範圍存取與收據、1c 程式碼快照與授權。 -->

# VCP 使用後改進稽核

- 稽核日期：2026-09-11
- 比賽工作區：`RSNA_Knee_Abnormality_Detection`
- VCP 工作區：`Vision-contest-pipeline`
- VCP main 基準：`03aecc3`
- 文件目的：將 RSNA Knee 實際跑過資料註冊、train-only 選型、正式訓練、Kaggle 部署、submission 同步與異地備份後暴露的通用問題，整理成可實作、可測試的 VCP 改進 backlog。

## 1. 結論與優先順序

VCP 現有的 append-only 台帳、預登記、sealed holdout、submission quota 與分層備份已經能建立相當完整的比賽治理流程。這次真正阻礙 RSNA 工作的核心問題，不是缺少更多模型功能，而是**執行程式讀過哪些資料，仍主要依賴程式自己的宣告；VCP 還不能從實際檔案與 row access 證明 train-only 邊界**。

因此建議先做以下三個 P0 主軸，再擴充便利功能：

1. **Role-scoped data access**：把「只能讀 train」從慣例變成 VCP 可強制的能力。
2. **Truthful artifact provenance**：access receipt、source audit、code inventory 與不可覆寫產物由框架產生及驗證，不再接受自我宣告。
3. **Submission ledger correctness**：把已完成但未進 main 的 prereg SHA 與 foreign score refresh 修正整合進去。

建議 backlog：

| 優先級 | 主題 | 目前狀態 | 建議處置 |
|---|---|---|---|
| P0 | Role-scoped dataset/card/row access | 開放問題；RSNA 有專案 workaround | 新增核心存取 API 與 OS-level/reader-level access receipt |
| P0 | 大型 array 的 selected-row integrity | 開放問題；RSNA 已做局部原型 | source audit 與 downstream audit inheritance 分層 |
| P0 | 產物不可覆寫與 supersession | 開放問題；曾發生 evidence overwrite | 提供 `ImmutableArtifactWriter` 與 append-only supersession |
| P0 | prereg file/log SHA 綁定 | `d193113` 已修、已上遠端分支、未進 main | rebase、重跑全套測試、合併 |
| P0 | foreign PENDING → COMPLETE 分數刷新 | `d881a1d` 已修、本機分支、未進 main | rebase、推送、review、合併 |
| P0 | 自動 code/environment inventory | 開放問題 | bundle/manifest 自動收集 transitive code 與 runtime identity |
| P1 | Data reference deep validation | 開放問題 | 外部 image root 與所有 view 預設 deep validate |
| P1 | Submission watch/reconcile/idempotency | 開放問題 | 新增 `submit watch` 與 `submit reconcile` |
| P1 | Backup health/status/evidence addendum | 部分完成 | 分離健康度與 credential 提示；擴充 evidence graph |
| P1 | Windows environment doctor | 開放問題 | 絕對 Python、CUDA/DICOM smoke test、lock receipt |
| P2 | Contest scaffold、lint/test wrapper | 開放問題 | 產生 `pyproject.toml` 與安全排除規則 |
| P2 | Content-addressed cache 與安全 GC | 開放問題 | 只清不可達暫存；歷史 receipt 永不刪除 |
| P2 | Visual guide/onboarding | 外部工作分支進行中 | 完成、驗證連結與 render 後另行整合 |

## 2. 稽核邊界與判定方式

本文件只把可以跨比賽重用的能力列為 VCP core 候選。下列內容仍應留在 RSNA 專案：

- 膝關節 MRI 的 44/64-slice slot layout。
- physical geometry、series boundary 與 2.5D triplet 的醫學影像邏輯。
- weak-label schema、unknown mask、label confidence 規則。
- Raptor、DINOv2、specific head 與 competition target 的模型程式。
- Kaggle competition 特有的 notebook entrypoint 與輸出欄位。

VCP core 應提供這些專案程式共用的**治理原語**：存取範圍、不可變產物、hash 綁定、append-only 狀態、環境與 code identity、平台同步、備份 evidence graph。若需要競賽邏輯，應透過 project plugin 或 adapter 接入，不應把 RSNA 規則寫進 `src/vcp`。

狀態分類如下：

- **已在 main 修正**：目前 `main` 已包含對應 commit，後續主要工作是保留 regression tests。
- **已有修正、未整合**：獨立分支已有 patch 和測試，但 `main` 仍受影響。
- **專案 workaround**：RSNA 目前能繼續，但能力沒有進通用框架。
- **設計缺口**：尚沒有完整修正或只有零散保護。

## 3. P0：資料邊界與證據正確性

### VCP-001：`Dataset.load()` 無法證明 train-only 存取

**狀態：設計缺口；RSNA 已有 selected-only workaround。**

#### 實際觸發

目前 `Dataset.load()` 會：

1. 讀取 card；
2. 對完整 `samples.jsonl` 計算 SHA256；
3. 逐行解析所有 sample；
4. 對全部 sample 執行 task validation；
5. 將全部 sample 排序並建立 `by_id`。

對一般資料驗證這是合理行為，但 train-only selection 只需要 dataset identity、frozen split plan 與 train IDs。只要呼叫 `Dataset.load()`，程式就已讀取 valA、valB，甚至可能包含 sealed holdout 的 metadata/labels。之後再宣告 `eval_accessed=False` 無法抵消先前發生的讀取。

RSNA whole-plan review 因此把早期 label-quality v3 與 geometry v3 的上游 provenance 判為 **INVALID**。其數值判決原本都是 REJECT，但這些數值只能作診斷，不能作正式選型證據。

#### 風險

- train-only 選型可能在不知情下讀到 eval labels。
- sealed holdout 的保護只發生在 `Dataset.subset()`，無法防止其他程式直接碰完整 sample registry。
- manifest 中的 `eval_accessed=False` 是自我宣告，審查者無法由檔案證據重建真相。
- 大型 JSONL 會帶來多餘 I/O、解析時間與記憶體占用。

#### 建議介面

```python
card = DatasetCardRef.load(name, verify_samples_identity=True)

with DatasetAccess.open(
    dataset=name,
    plan=plan_id,
    allowed_roles={"train"},
    fields={"sample_id", "group_id", "image"},
) as access:
    for sample in access.iter_role("train"):
        ...
receipt = access.finalize_receipt()
```

可拆成較小 API：

- `Dataset.load_card()`：只讀 card，不解析 samples。
- `DatasetIdentity.verify()`：驗證 card、samples identity 與 plan binding；可使用既有 source audit，不必每次解析所有 rows。
- `Dataset.iter_ids(plan, role)`：只返回 frozen plan 已授權的 IDs。
- `Dataset.iter_records(ids, fields=...)`：按 allowlist 串流讀指定 records。
- `RoleScopedDataset`：封裝 allowed roles，未授權角色在 API 層立即拒絕。

#### 必要驗收測試

1. 把 `samples.jsonl` 中非 train rows 包成會在 read 時拋錯的 sentinel，train-only job 必須 PASS。
2. 嘗試 `iter_role("valA")` 必須在開檔前 fail closed。
3. receipt 必須由 accessor 實際累計 IDs/roles/fields，呼叫端不能覆寫。
4. plan、card 或 source audit 任一 SHA 改變時，既有 access authorization 必須失效。
5. sealed role 即使知道 sample ID，也不能透過 `by_id` 或直接 iterator 繞過。

### VCP-002：大型 memmap 每次全檔 SHA 與 train-only row access 衝突

**狀態：設計缺口；RSNA six-slot v2 已實作局部原型。**

#### 實際觸發

- Raptor volume loader 曾對約 21.89 GiB full volume 計算 SHA256，並掃描全部 mask。
- six-slot cache 開啟 `verify_hashes=True` 時，也會逐次重讀約 11 GiB arrays。
- train-only feature extraction 實際只需要 frozen train IDs 的 rows，但完整 hash 會讀過非 train pixel bytes。

完整 hash 對「來源檔是否改變」有價值，但不應由每一個 downstream job 重複執行，更不應讓 selected-row job 因 integrity check 讀完整 corpus。

#### 建議架構：兩階段完整性

**階段 A：一次性的 source audit**

- 在允許完整來源存取的資料準備階段，計算 full-file SHA256。
- 記錄 path、size、mtime 僅作診斷，SHA256 作 identity。
- 對 NPY/memmap 記錄 header、shape、dtype、order、ID index SHA。
- source audit 為不可變、content-addressed artifact。

**階段 B：downstream audit inheritance**

- downstream job 只驗證 source audit SHA、file size、header、ID index 與 selected rows。
- array 以 read-only mmap 開啟。
- row accessor 只接受 frozen allowlist ID；禁止暴露底層 ndarray 給呼叫端。
- receipt 記錄實際讀取 row IDs、範圍與 count。

#### 必要驗收測試

- monkeypatch `sha256_file()`：selected-row job 對 pixel arrays 呼叫它即失敗。
- proxy array：任何非 selected row access 即失敗。
- 篡改 source audit、header、ID mapping 或 selected row 時均 fail closed。
- 相同 selected IDs 的新流程與已驗證舊流程輸出一致。
- source audit 可以被多個 downstream artifacts 引用，不重算 full hash。

### VCP-003：access flags 是自我宣告，無法代表真實 I/O

**狀態：設計缺口。**

#### 實際觸發

早期 artifact manifest 可寫出：

```json
{
  "pixel_access_scope": "requested-train-rows-only",
  "eval_accessed": false,
  "sealed_accessed": false
}
```

但 loader 已經計算過 full-corpus hash 或完整解析 metadata。這些欄位描述的是程式作者的意圖，不是 runtime evidence。

#### 改進方向

新增由框架控制的 `AccessReceipt`：

```yaml
schema_version: 1
dataset: rsna-knee-...
plan: ...
authorization_sha256: ...
allowed_roles: [train]
opened_resources:
  - resource_id: sixslot-volume
    access_mode: mmap-readonly
    rows_sha256: ...
    rows_count: 4367
denied_attempts: 0
sealed_accessed: false
started_at: ...
finished_at: ...
```

receipt 應由 `DatasetAccess`/`ArrayAccessor` 在 context manager 結束時生成，並以 SHA 綁定到 artifact manifest。呼叫端只能補充 notes，不能指定 `opened_resources`、roles 或 booleans。

測試必須走 production loader，不接受只驗證 manifest 字面值的測試。

### VCP-004：code inventory 容易漏掉真正影響行為的模組

**狀態：設計缺口；RSNA 曾兩次由 review 找到漏列。**

#### 實際觸發

- benchmark/feature artifact 的 code inventory 曾漏掉行為相關 helper。
- train-only runner 的 inventory 曾漏列 `raptor44_data.py`。
- source code 已修改時，舊 immutable config 仍 pin 舊 hash，root `pytest` 會把它看成錯誤；實際上要區分「歷史 artifact 可重現」與「目前 checkout 已漂移」。

#### 改進方向

建立 `CodeSnapshot`，由 entrypoint 自動收集：

- 入口 script/module。
- project package 的 transitive imports。
- VCP version、Git commit、dirty diff hash。
- 外部 Python distributions 的 name/version/wheel hash 或 lockfile hash。
- model source/revision 與離線權重 SHA。
- Python executable、ABI、OS、CUDA、Torch 與 DICOM decoder identity。

對 Python，可先採「module allow-root + import tracing」的保守模式；無法證明完整時應報 `inventory_complete=false`，而不是默認 PASS。Kaggle bundle 應攜帶已凍結 source snapshot，歷史驗證應以 snapshot/commit 為基準，不強迫與目前工作樹相同。

#### 驗收

- 修改任何被 import 的 project module，cache reuse 必須被拒絕。
- 修改未被引用的文件不應使 model artifact 失效。
- dirty worktree 必須留下 diff hash 或明確拒絕 formal build。
- 離線 Kaggle bundle 能在沒有原 Git repo 時驗證完整 inventory。

### VCP-005：artifact 路徑可被覆寫，破壞不可變證據

**狀態：設計缺口；RSNA 已改用 exclusive-create workaround。**

#### 實際事故

第二次 submission 的 pre-submission receipt 曾在同一路徑被覆寫。原始 SHA256 為 `4cc083...` 的 bytes 無法復原，現存 evidence chain 只能從後續 `712cc...` 開始。後來雖保留 incident report 並改寫入方式，但框架本身仍沒有統一禁止 overwrite。

#### 改進方向

提供核心 `ImmutableArtifactWriter`：

```python
with ImmutableArtifactWriter(spec, mode="create") as artifact:
    artifact.write_json("receipt.json", payload)
    artifact.commit()
```

規則：

- 所有正式 artifact 以 `x`/`O_EXCL` 建立。
- 先寫同目錄 `.partial-<nonce>`，fsync/驗證後 atomic rename。
- target 已存在即失敗；不得提供正式產物 `--force`/`--replace`。
- 修正必須使用新 ID，並追加 `supersedes_sha256`/`supersedes_id`。
- append-only `supersession.jsonl` 提供 active lineage；舊 bytes 永不修改。
- interrupted temp 可由 `artifact status` 辨識；只有未 commit 且無引用的 temp 可清理。

#### 驗收

- 相同 ID 第二次寫入必須失敗且原檔 hash 不變。
- crash 於 commit 前不能留下看似完成的 manifest。
- supersession 只能指向存在且 hash 相符的 artifact。
- GC 不得刪除 registered artifact、ledger、manifest 或其 ancestry。

### VCP-006：benchmark PASS 沒有與正式 full job 做不可替換綁定

**狀態：設計缺口。**

50-study geometry/decode benchmark 可以證明固定樣本上的速度、decode 與 geometry，但若 full extraction 只靠人手沿用設定，仍可能更換 dataset、IDs、source audit、code、batch 或模型。

建議新增 `ArtifactAuthorization`：

```yaml
benchmark_id: ...
benchmark_receipt_sha256: ...
dataset_card_sha256: ...
plan_sha256: ...
allowed_ids_sha256: ...
source_audit_sha256: ...
code_snapshot_sha256: ...
model_sha256: ...
parameters_sha256: ...
expires_on_change: true
```

正式 full job 必須消耗這張 authorization；任一欄位不一致直接拒絕。這個能力應是通用 artifact gate，RSNA benchmark 的內容仍留在專案 plugin。

### VCP-007：ID、seed、output root 與 audit pin 缺少共同 contract

**狀態：設計缺口；由 review 發現並修正專案設定。**

six-slot v2 曾出現輸出 ID 表示 seed 42，但 CLI 仍可接受 seed 43；audit path/SHA 也曾 pin 到不一致版本。這類錯誤不一定造成例外，卻會讓 artifact 名稱、內容與 lineage 分離。

建議提供 `ImmutableArtifactSpec`：

- ID pattern 與 `seed`/dataset/plan 的結構化欄位分開儲存。
- output root 由 spec 解析，不讓 CLI 任意指定另一處。
- manifest commit 前再次比較 expected/actual seed、source audit、plan、code snapshot。
- cache reuse 必須通過完整 spec equality，而不是只看目錄存在。

## 4. P0：已完成但尚未進 main 的通用修正

### VCP-008：prereg YAML 未與第一筆 append-only log SHA 綁定

**狀態：已在 `codex/prereg-sha-integrity` 修正；commit `d193113`；遠端分支存在；未進 main。**

#### 原問題

`prereg_time()` 使用第一筆 append-only log 的時間，卻沒有驗證目前 YAML bytes 是否仍等於該筆 log 的 SHA。攻擊或誤操作可以在預登記後修改 `t_min`、`min_bases` 或 claim，再保留較早時間，形成事後 preregistration。

#### 既有 patch

- `load_prereg()` 讀第一筆 prereg log entry。
- 驗證 YAML 實際 SHA256 等於第一筆 log SHA。
- 不一致時拋出 `IntegrityError`。
- 補 judgement 與 submit gate 測試。

#### 整合前要求

1. 以最新 main rebase。
2. 跑 measure、fuse、submit gate 與完整 unit suite。
3. 稽核既有 prereg：不修改歷史檔，另產出 mismatch report；需要修正時建立新 ID。
4. 文件更新：第一筆 log 決定 identity，後續同 ID rows 不得改變 binding。

### VCP-009：foreign submission 已知 ref 的新分數不會刷新

**狀態：已在 `codex/submit-foreign-score-refresh` 修正；commit `d881a1d`；本機分支；未進 main。**

#### 原問題

第一次真實 submission 沒有事前 VCP stage，因此依法記為 foreign。第一次 sync 在 PENDING 時寫入 ref；第二次 sync 看到同一 ref COMPLETE/0.935 時，舊程式因 ref 已知直接跳過，導致台帳與 report 永遠沒有分數。

#### 既有 patch

- foreign ref 可追加多筆 state snapshots。
- 只有狀態或分數改變時才 append。
- `arrivals()` 對每個 foreign ref 只採最新 snapshot。
- quota 與 foreign count 仍只算一次。
- 重複 sync 相同 COMPLETE row 為 idempotent。

#### 整合前要求

1. rebase 最新 main 並處理 submission hygiene commits 的互動。
2. 跑全部 121+ submit unit tests及 E2E tests。
3. 推送分支並做獨立 review。
4. 增加狀態序列：PENDING → COMPLETE、PENDING → ERROR、COMPLETE score correction、缺少 ref、同時間重複 rows。

## 5. P1：資料註冊、來源與 deep validation

### VCP-010：dataset card 可存在，但外部影像 reference 實際失效

**狀態：設計缺口。**

RSNA r5 dataset 曾成功註冊，但 `image_root` 指向 import source，其中 8 個 shard references 不存在。保留 r5 歷史後，專案只能建立 r6 修正。這顯示 schema/card hash 正確不代表 data references 可解析。

建議：

- 對 external-reference dataset，`data validate` 預設執行 `--resolve-views`。
- 統計 samples、views、resolved、missing、root escape、zero-byte、header failure。
- 對所有 path 先 `resolve()` 並確認仍在宣告 root 內。
- import finalization 時若沒有任何有效 view，直接 FAIL。
- 允許明確 `reference-only` resource；不能把它誤報為 materialized pixels。
- 記錄 failure sample IDs 的 digest，避免在 VERDICT 輸出敏感/過長清單。

### VCP-011：缺少來源平台、版本、授權與 cache authenticity contract

**狀態：設計缺口。**

公開 Kaggle cache 即使結構可讀，也不代表來源完整、版本固定、授權允許或與 competition dataset 一致。VCP 目前缺乏一致的 `SourceContract`。

建議 schema：

```yaml
source_type: kaggle_dataset
owner_slug: ...
dataset_slug: ...
version_number: ...
metadata_sha256: ...
license: ...
competition_rules_acknowledged: true
upstream_files:
  - logical_name: ...
    size: ...
    sha256: ...
derived_by: ...
```

VCP 不管理 Kaggle token，但可驗證使用者已取得的 metadata、version、files 與 license 欄位。若來源只經結構盤點，狀態應是 `STRUCTURALLY_COMPATIBLE`，不能寫成 `AUTHENTICATED`。

### VCP-012：資料資源缺少明確類別與存取政策

**狀態：設計缺口。**

建議 card 將資源區分為：

- `raw_immutable`：永不修改，只能讀。
- `external_reference`：不複製，由 source contract 固定。
- `cache`：可重建的大型衍生資料。
- `artifact`：正式、不可變、可進 evidence graph。
- `temporary`：可由安全 GC 清理。

每一類有不同 hash、備份、Git、GC 與 access receipt 規則。這可以避免把 500+ GiB raw、20 GiB cache、模型 checkpoint 與 audit receipt 用同一套政策處理。

## 6. P1：submission 可觀察性與平台同步

### VCP-013：缺少長時間 PENDING 的可見進度與狀態變化紀錄

**狀態：設計缺口。**

Kaggle hidden rerun 可能長時間 PENDING，且平台不公開逐-study log。使用者因此無法分辨正常排隊、kernel 執行、平台無回應或本地同步失敗。

建議新增：

```powershell
vcp submit watch --dataset ... --ref 56131417 --interval 60
```

行為：

- 只在平台狀態改變時 append ledger/console event，固定 heartbeat 可選。
- 顯示 submitted_at、last_checked_at、elapsed、platform status、public/private score。
- 顯示平台 URL/ref，但不得輸出 token 或原始 credential 路徑。
- 沒有可觀測 log 時明確顯示 `progress=unknown`。
- ETA 只能是以歷史同 competition/runtime 得到的區間估計；樣本不足時顯示 unavailable。
- COMPLETE/ERROR 後自動停止，不重送 submission。

### VCP-014：upload 回覆不確定時，缺少內建 reconciliation 與 retry safety

**狀態：設計缺口。**

正式 self submission 曾得到 `status=WARN confirmed=false`，但 Kaggle 其實已受理並產生 ref。若使用者直接 retry，可能浪費 quota 或形成重複 submission。

建議 `vcp submit reconcile`：

1. 以 staged file SHA、file name、message、submitted time window 比對平台 rows。
2. 唯一匹配時追加 `uploaded-confirmed`/state snapshot。
3. 多重匹配時保留 WARN 並要求人工選 ref，不自動重送。
4. 找不到時保留 quota reservation，經過 profile 定義的 grace period 才允許 retry。
5. retry 使用 idempotency key；平台 adapter 若不支援，VCP 至少先做 reconcile。

quota 應區分 `reserved`、`platform_seen`、`completed`，但計數仍以平台實際 arrival 為準。

### VCP-015：execution status 與 domain verdict 容易混淆

**狀態：介面缺口。**

`eval judge` 可回傳：

```text
VERDICT cmd=eval.judge status=OK verdict=FAIL
```

其中 `status=OK` 只表示命令成功完成，模型仍未通過。這個設計有理由，但人與自動化都容易只看 exit code 或第一個 status。

建議：

- JSON 固定輸出 `execution_status` 與 `domain_verdict`，避免兩個概念共用 status。
- 所有有領域判決的命令都提供 `--require PASS`；CI/formal recipe 預設啟用。
- 人類輸出最後一行同時顯示兩者，domain FAIL 使用醒目文字。
- `vcp eval status` 要分列 command health 與 admission verdict。
- 文件範例不得只示範 exit code。

### VCP-016：foreign provenance 被當成整體 WARN，健康度訊號失真

**狀態：UX 缺口。**

第一次真實 score 同步健康、unscored=0，但 `submit.status` 因 `foreign=1` 顯示 WARN。foreign 是 provenance 類別，不一定是系統故障。

建議拆成：

```yaml
health: OK
provenance: FOREIGN_PRESENT
quota: OK
score_sync: OK
final_eligibility: INELIGIBLE_FOREIGN
```

這仍必須保留「foreign submission 不能事後補 stage，也不能進 sealed final」的規則，只改善訊號語意。

### VCP-017：平台 parser 與狀態轉移需保留 regression matrix

**狀態：CSV/header/event-source 問題已在 main 透過 `6a4cc58`、`12a9cd4` 修正或補測；需持續防回歸。**

應保留至少以下測試：

- 多餘 CSV fields、header drift、nested match。
- uploaded、scored、foreign 使用同一 normalized event source。
- 平台 status 大小寫/enum prefix 差異。
- response success 但平台尚未列出，以及 response failure 但平台已接受。
- 同 ref 的 score/status 更新不重複計 quota。

## 7. P1：備份、failure domain 與 evidence graph

### VCP-018：evidence graph 不易包含競賽專屬 supporting artifacts

**狀態：設計缺口；RSNA 目前以手工 addendum 補足。**

VCP 能從 conclusion 遍歷正式資料、run、reading、judgement 等核心證據，但 Kaggle notebook bundle、source snapshot、visible-run log、platform receipt、研究診斷、公開來源 metadata 等競賽 supporting evidence 不一定在 graph 中。RSNA 多次需要手工組 addendum、INDEX 與 rclone check。

建議：

- evidence graph 支援 plugin-provided edge providers。
- `vcp backup addendum --include <artifact-id>` 只接受已註冊 artifact，不接受任意 glob 默默擴張。
- INDEX 記錄 logical role、source SHA、destination、exclusion policy、Git commit。
- 支援 `--explain`: 顯示每個檔案因哪一條 edge 被納入。
- 預設排除 raw、cache、venv、credential、rclone config、sealed contents。
- verify receipt 記錄 download-based/remote-hash/local-only 的驗證強度。

### VCP-019：`backup status` 把 credential residue 提示混成 backup WARN

**狀態：UX 缺口。**

目前遠端 26/26 或 19/19 完整驗證後，只因 rclone config 仍存在，`backup status` 仍是 WARN。依專案規則不應在未授權時執行 `--forget-remote`；config 存在是操作狀態，不等於備份失敗。

建議拆欄位：

- `backup_health=OK|WARN|FAIL`
- `offsite_verified=true|false`
- `verification_tier=1|2|3`
- `credential_residue=PRESENT|ABSENT|UNKNOWN`
- `forget_eligible=true|false`

總體 exit/status 以備份完整性為主，credential residue 作 policy notice。VCP 只顯示 config 是否存在，不讀、不保存、不輸出 credential 內容。

### VCP-020：同機備份與真正異地備份缺少 failure-domain 分類

**狀態：設計缺口。**

`C:/vcp-backup/rsna-knee` 與工作區都在同一台機器，不能稱作異地備份。建議 destination profile 要宣告：

- `same_volume`
- `same_host_other_volume`
- `network_host`
- `remote_provider`

只有後兩者且完成指定 tier verify 時才能輸出 `offsite_verified=true`。若無法自動判定，要求 profile 明確聲明並在 report 顯示 `operator_asserted`。

### VCP-021：遠端路徑容易因同名檔案碰撞

**狀態：設計缺口。**

不同候選 checkpoint 都可能名為 `model.pt`。手工 addendum 必須建立 baseline/candidate 子目錄才能避免碰撞。

建議遠端 key 由下列欄位組合：

```text
<dataset>/<run-id>/<artifact-role>/<sha256-prefix>/<basename>
```

INDEX 使用完整 SHA 判定 identity；basename 只供閱讀。push 前若目的 key 已存在但 SHA 不同，必須 fail closed。

### VCP-022：已進 main 的 backup 安全修正需列為永久回歸門檻

**狀態：已在 main；相關 commit `8c6b5c4`、`883da62`。**

目前 main 已包含：

- append-only log 的 prefix snapshot/hash 語意。
- manifest path 必須是資料根目錄下的相對路徑。
- pull path containment，避免 path traversal/還原到根外。
- `--forget-remote` 只在完整 tier-3 verify 後執行。
- 本機 vault、假 rclone、privacy scan 的 E2E coverage。

後續不得把這些視為已結案後可刪的測試。另建議補明確的 zero-file/empty-manifest case：沒有任何檔案完成驗證時，不得因 vacuous success 刪除 remote credential。

## 8. P1：Windows、GPU 與環境重現

### VCP-023：child process 可能落到 base Python

**狀態：設計缺口；RUNBOOK 目前以絕對 training Python 避免。**

第一次 seed 42 執行曾啟動沒有 `typer` 的 base Python，而不是專案 training venv。Windows 上 PATH、PowerShell activation 與子程序繼承容易產生這類靜默漂移。

建議：

- `vcp train run --python <absolute-path>`，並在 run receipt 記錄 resolved executable。
- Windows formal run 拒絕 bare `python`/`py`，除非 profile 明確允許。
- 執行前驗證 `sys.executable`、Python 3.12、editable VCP path、Torch、CUDA device、DICOM decoder。
- child process 由 VCP 用絕對 interpreter 建立，不依賴 activation shell。
- 記錄 `pip freeze`/lock SHA、CUDA runtime、driver、GPU name、VRAM。

### VCP-024：核心環境與訓練環境分離是好政策，但建立與驗證太手工

**狀態：DX 缺口。**

建議 `vcp env doctor`：

```text
core_python=... status=PASS
training_python=... status=PASS
editable_vcp=... commit=...
python_abi=cp312
torch_cuda=... status=PASS
dicom_decode_smoke=PASS
disk_free_gib=...
```

另提供：

- core lock 與 training lock 分開。
- Windows local 與 Kaggle Linux/T4 wheel/ABI compatibility check。
- 離線 wheelhouse/bundle inventory。
- requirements 中的 direct URL、editable source 與 local path 不能只靠版本號識別。

### VCP-025：GPU/時間/磁碟預估沒有統一 receipt

**狀態：設計缺口。**

50-study benchmark 已能提供 decode throughput、GPU time 與 storage projection，但目前格式在專案內。VCP 可定義通用 `BenchmarkReceipt`，內容包括：

- 固定抽樣 IDs SHA 與抽樣規則。
- hardware/runtime identity。
- warmup、measured samples、wall/GPU time、peak VRAM/RAM。
- decode errors/fallback count。
- projection model 與 confidence range。
- STOP/GO criteria 與 authorization binding。

實際 MRI geometry fields 由 RSNA plugin 擴充。

## 9. P2：開發者體驗、測試與空間管理

### VCP-026：contest repo 沒有統一 lint 設定

**狀態：DX 缺口。**

外部 VCP 設定 Ruff line length 100；contest repo 若直接執行裸 Ruff，可能使用預設 88，造成假性 review finding。反過來，也曾有新增 test 未被正確 formatter 覆蓋。

建議 VCP onboarding 產生 contest-local `pyproject.toml`：

```toml
[tool.ruff]
line-length = 100
src = ["projects/<project>"]

[tool.pytest.ini_options]
testpaths = ["tests"]
norecursedirs = ["vcp-data", ".venv", ".git"]
```

也可提供 `vcp dev lint`/`vcp dev test` wrapper，明確輸出使用的 config path。

### VCP-027：workspace root pytest 會收集備份或大型資料目錄

**狀態：DX 缺口。**

contest root 的裸 `pytest` 曾收集 `vcp-data` 內備份 copies，並因 PYTHONPATH 不正確而報錯。這不是產品 bug，但容易讓審查與 CI 得到錯誤結論。

建議 scaffold 預設：

- `testpaths = ["tests"]`
- `norecursedirs = ["vcp-data", "artifacts", "checkpoints", ".venv"]`
- project package 的明確 `pythonpath` 或 editable install check。
- `vcp dev test --project ...` 只跑正式測試樹。
- 輸出 collected test roots，讓誤收集可見。

### VCP-028：大型不可變 v1/v2/v3/v4 會快速耗盡磁碟

**狀態：設計缺口。**

新 ID 不覆寫是正確治理，但若每次修正都複製 10–20 GiB arrays，磁碟很快不足。不能用刪除歷史來解決。

建議：

- 大型 bytes 使用 content-addressed blob store；不同 artifact receipt 可引用同一 blob SHA。
- receipt、manifest、ledger 永遠不可變；blob 用引用計數/graph reachability 管理。
- hardlink/reflink 僅在平台支援且驗證安全時使用，report 要記錄 storage mode。
- `vcp artifact gc --dry-run` 只列出未 commit temp、無任何 manifest 引用的 blobs。
- 真正刪除需明確操作，且不能碰 raw、registered artifact 或 evidence ancestry。

### VCP-029：interrupted jobs 缺少一致的 resume/failure receipt

**狀態：設計缺口；RSNA 各 runner 各自實作。**

建議框架提供：

- start receipt：完整 spec/inputs/seed/code/environment。
- per-unit/fold append-only receipts。
- resume 時逐欄比較 start identity；不同即拒絕。
- final manifest 只在全部 unit 完成且 hash 驗證後建立。
- failure receipt 記錄 exception class、最後完成單元與 sanitized reason，不記 credentials。
- interrupted `.partial` 可被 `artifact status` 列出，但不被 evidence graph 當完成品。

### VCP-030：缺少跨層 lineage audit

**狀態：設計缺口；目前依靠人工 whole-plan review 才找到 false provenance。**

建議：

```powershell
vcp audit lineage --conclusion <id> --require-role-boundary train-only
```

遍歷並驗證：

- dataset card、plan 與 source contract。
- access authorization/receipt。
- benchmark gate 與 full artifact。
- code/environment snapshot。
- prereg、run、reading、judgement。
- staged bundle、platform ref、score state。
- backup manifest、remote verify receipt。
- supersession chain 與所有 SHA。
- ancestry 中是否出現 val/holdout/sealed/test access。

輸出每一層 `PASS|FAIL|INVALID|UNKNOWN`，不能因 leaf manifest 自稱 PASS 就停止遍歷。

### VCP-031：project plugin SDK 尚未承接重複治理程式

**狀態：架構缺口。**

RSNA 專案內重複實作了 immutable JSON writer、manifest、code inventory、access counters、GPU telemetry、per-fold receipt、Kaggle bundle validation。這些機制適合通用化，但 MRI/label/model 邏輯不適合。

建議穩定 plugin API：

- `ArtifactBuilder`
- `AccessProvider`
- `EvidenceEdgeProvider`
- `BenchmarkExtension`
- `PlatformBundleValidator`
- `VerdictEmitter`

plugin 只能透過公開 API 使用 data/config roots，不 import VCP 私有內部模組。VCP 的 integration test 應包含一個極小 demo contest plugin，證明跨 Windows/Linux 的生命週期。

### VCP-032：VERDICT 文字格式應有正式 JSON schema

**狀態：介面改善。**

目前最後一行 VERDICT 很實用，且 `d9b2331` 已修正失敗時命令識別欄位。下一步應定義 versioned schema：

```json
{
  "schema_version": 1,
  "command": "eval.judge",
  "execution_status": "OK",
  "domain_verdict": "FAIL",
  "dataset": "...",
  "artifact_ids": ["..."],
  "reason_code": "BASES_NOT_POSITIVE"
}
```

文字 VERDICT 可保留作 shell interface；`--json` 必須與同一 typed result 產生，避免兩套語意漂移。reason 使用穩定 code，長 prose 放 report。

### VCP-033：host 絕對路徑與歷史命令降低可攜性

**狀態：文件與 schema 缺口。**

RUNBOOK 歷史上保留 `C:/vcp-data` 與舊 framework path 是合理的證據，但新 artifact 若只記錄 host absolute path，搬到 Kaggle/Linux/另一台 Windows 便難以重現。

建議 manifest 同時記：

- logical root：`data://raw/...`、`artifact://...`
- root profile ID 與 profile SHA。
- build 時 resolved path，標記為 diagnostic。
- portable relative path 與 content SHA。

identity 不應只由 host absolute path 決定。

### VCP-034：操作與視覺 onboarding 尚未完成整合

**狀態：外部 `codex/vcp-visual-guide` 工作中；不是 main 已完成能力。**

目前外部工作樹有 README、handover 與 `docs/guides/` 未提交變更。完成前應：

- 補齊 human-agent boundary 與 first-run assets。
- 驗證所有 Mermaid/SVG/link。
- 以新使用者流程走一次 Windows first run。
- 清楚區分 raw、cache、artifact、ledger、sealed、offsite。
- 文件示例顯示 `execution_status` 與 domain verdict 的差異。
- 以獨立 commit/branch 整合，避免污染通用修正分支。

## 10. Core、adapter 與專案程式的責任切分

| 能力 | VCP core | Adapter/SDK | RSNA 專案 |
|---|---:|---:|---:|
| append-only ledger、prereg SHA、supersession | 是 |  |  |
| role-scoped ID/record/array access | 是 | array/source adapter | 只宣告需要的角色與欄位 |
| source audit 與 access receipt schema | 是 | DICOM/NPY/Kaggle adapter | MRI 特定 audit fields |
| physical geometry、series boundary |  | benchmark extension | 是 |
| weak-label/unknown/confidence 規則 |  |  | 是 |
| artifact immutable writer、atomic commit | 是 |  | 使用它 |
| transitive code/environment snapshot | 是 | platform resolver | 提供 package root/entrypoint |
| submit watch/reconcile/quota | 是 | Kaggle platform adapter | profile/competition slug |
| evidence graph | 是 | plugin edge provider | 登記 supporting artifacts |
| backup failure-domain 與 verify tier | 是 | rclone/local destination | 選目的地與排除 policy |
| Raptor/DINOv2/2.5D 模型 |  |  | 是 |

## 11. 建議的導入順序

### Wave 0：先整合已完成的 correctness fixes

1. 合併 `d193113` prereg SHA binding。
2. 合併 `d881a1d` foreign state refresh。
3. 補 migration/audit report，不改寫既有 ledger/prereg。
4. 將 `8c6b5c4`、`883da62`、`d9b2331`、`7c31c3d`、`12a9cd4`、`6a4cc58` 列入 release regression gate。

### Wave 1：建立 provenance substrate

1. 定義 `ArtifactSpec`、`SourceAudit`、`AccessAuthorization`、`AccessReceipt`、`CodeSnapshot` schema。
2. 實作 `ImmutableArtifactWriter`。
3. 實作 card-only 與 role-scoped dataset API。
4. 實作 selected-row array accessor。
5. 先用一個小型 synthetic plugin 做完整 E2E，再讓 RSNA prototype 遷移。

### Wave 2：跨層 audit、submission 與 backup

1. `vcp audit lineage`。
2. `vcp submit watch`、`reconcile`、quota reservation。
3. backup status 拆 health/policy/failure-domain。
4. evidence addendum/edge provider。

### Wave 3：環境、DX 與儲存效率

1. `vcp env doctor` 與絕對 interpreter contract。
2. contest scaffold 的 Ruff/pytest config。
3. content-addressed artifact blobs 與 dry-run GC。
4. visual guide、first-run 與 plugin authoring guide。

## 12. 建議 release gate

VCP 下一個含上述改動的 release，不應只以 unit tests 數量判定。建議至少有以下跨層 scenarios：

1. **Train-only privacy**：非 train metadata/pixel 一旦被開啟就拋錯，完整 selection 仍 PASS。
2. **Sealed boundary**：知道 holdout ID 也無法繞過 role accessor；unseal 必須有正式 append-only event。
3. **Large array reuse**：source audit 只做一次，downstream selected-row job 不 full-hash pixels。
4. **Artifact immutability**：相同 ID/路徑重跑不覆寫；supersession 產生新 ID。
5. **Prereg tamper**：修改 YAML 後 judgement 與 submit gate 都拒絕。
6. **Foreign lifecycle**：PENDING → COMPLETE score 刷新、quota 不重複、再次 sync 無新 row。
7. **Ambiguous upload**：API 回覆失敗但平台已有 ref；reconcile 成功且不重送。
8. **Backup containment**：惡意 `../` manifest、prefix drift、zero-file、tier<3 forget 均拒絕。
9. **Failure domain**：同磁碟 copy 不得輸出 offsite verified。
10. **Historical reproducibility**：目前 code 漂移時，舊 source snapshot 仍可驗證；不能把它誤判為目前 code 一致。
11. **Windows interpreter**：base Python 與 training venv 同時存在時，formal child process 仍使用固定絕對路徑。
12. **Workspace collection**：root test 不收集 `vcp-data`、venv、backup artifacts。

## 13. 可量化的完成標準

建議把下列指標放入 VCP release checklist：

- 100% formal artifacts 由 immutable writer 建立。
- 100% train-only jobs 具有 runtime-generated access receipt。
- 0 個 formal manifest 使用呼叫端自填的 `eval_accessed` 作唯一證據。
- 100% prereg loads 驗證第一筆 append-only log SHA。
- 100% submission platform refs 支援 append-only state transition，quota 按唯一 arrival 計算。
- 100% offsite claims 包含 failure-domain 與 download/remote verification tier。
- 100% child training/inference receipts 記錄 resolved Python 與 code/environment snapshot。
- 完整 lineage audit 對任何缺失、漂移或未知 ancestry 回傳 FAIL/INVALID/UNKNOWN，不能默認 PASS。

## 14. 目前不應做的事

- 不把 RSNA MRI geometry、weak-label 或 Raptor 模型寫入 `src/vcp`。
- 不為了讓新驗證通過而重寫既有 prereg、plan、run、manifest 或 ledger。
- 不把舊 artifact 的 source drift 用覆寫 config 修掉；應保留舊 snapshot 或建立新 ID。
- 不把 rclone/Kaggle credential 放進 manifest、log、test fixture 或 support bundle。
- 不把同機 `C:` 備份宣稱為異地備份。
- 不因磁碟不足直接刪除 registered history；先導入 content-addressed storage 與可證明安全的 GC。

## 15. 相關證據文件

- [RUNBOOK.md](RUNBOOK.md)：完整操作歷史與正式 VERDICT。
- [SECOND_SUBMISSION_EVIDENCE_INCIDENT_20260909.md](SECOND_SUBMISSION_EVIDENCE_INCIDENT_20260909.md)：不可覆寫 evidence 的實際事故。
- [KAGGLE_SUBMISSION_RESULT_20260908.md](KAGGLE_SUBMISSION_RESULT_20260908.md)：foreign submission 與 PENDING → COMPLETE 同步缺陷。
- [THIRD_SUBMISSION_PREFLIGHT_20260909.md](THIRD_SUBMISSION_PREFLIGHT_20260909.md)：bundle、expanded mount、stage 與 upload reconciliation。
- [THIRD_SUBMISSION_RESULT_20260909.md](THIRD_SUBMISSION_RESULT_20260909.md)：正式 score、同步與備份鏈。
- [FOURTH_CANDIDATE_RESEARCH_20260909.md](FOURTH_CANDIDATE_RESEARCH_20260909.md)：label、geometry、dense 2.5D 的研究邊界。
- [FOURTH_CANDIDATE_SELECTION_20260909.md](FOURTH_CANDIDATE_SELECTION_20260909.md)：train-only 選型與 provenance review。

這份稽核的首要判斷是：VCP 下一階段最有價值的工作，是讓「沒有讀到 eval/sealed data」成為可由框架獨立驗證的事實，並讓每一個正式產物的來源、程式、環境、授權與 supersession 都能被機器沿 lineage 重建。完成這層之後，submission 監控、備份狀態與開發體驗改善才會建立在可信的證據鏈上。
