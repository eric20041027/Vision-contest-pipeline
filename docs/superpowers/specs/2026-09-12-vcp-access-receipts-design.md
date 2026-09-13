# vcp 角色範圍存取與收據設計（稽核 Wave 1b-1）

- 日期：2026-09-12
- 狀態：v1（brainstorming 逐節核可後寫成）
- 來源：`docs/audits/2026-09-11-vcp-improvement-audit.md`——VCP-001「`Dataset.load()` 無法證明 train-only 存取」、VCP-003「access flags 是自我宣告，無法代表真實 I/O」、VCP-002 的階段 B（downstream 只驗身分、按 allowlist 讀）；§11 Wave 1 第 1、3 項；§12 release gate 1「train-only privacy」與 2「sealed boundary」；§13「100% train-only jobs 具有 runtime-generated access receipt」、「0 個 formal manifest 以呼叫端自填的 `eval_accessed` 作唯一證據」
- 前置：`v0.4.0` 不可變產物層（`docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md`）——收據是它的一種 kind；資料層 spec §7.3 lineage 與 §7.4 sealed 留痕；量測層 spec §6.1 乾淨基底；訓練層 spec §6.1 `trained_on` 推導與 §8 `Session` / `MaterializedReader`
- 分段：Wave 1b 拆兩段。**1b-1 本 spec**：角色範圍存取、`AccessReceipt`、四個強制點。**1b-2**（另一份 spec）：`SourceAudit` 與選取列陣列存取器（VCP-002 階段 A、adapter 面向）。之後 1c：`CodeSnapshot` 與授權綁定 benchmark（VCP-004/006）
- 後續：Wave 2 的 `vcp audit lineage` 遍歷收據；C 路線（子程序 I/O 稽核 hook）可日後附加在本層之上

## 1. 目的

今天「一個 job 讀了哪些資料」整條鏈都是自我宣告：`Dataset.load()` 整份解析 `samples.jsonl`（含 eval 與 sealed 的標籤）再由 `subset()` 過濾；`trained_on` 由呼叫端傳的 export 目錄決定，`ingest` 照抄，`measure` 靠它挑乾淨基底，`judge` 與 `submit` 建在那些讀數上。本 spec 讓「讀了什麼」變成框架的觀測：card-only 載入、按 plan 角色授權的列讀取、由存取器在關閉時產生的收據（不可變產物）、以及 `train run` / `ingest` / `measure` / `judge` / `submit` 改吃「宣告 ∪ 觀測」而不是宣告。

不在本 spec：`SourceAudit` 與大陣列的選取列存取器（1b-2）、`CodeSnapshot`（1c）、子程序 I/O 稽核 hook、`fields` 欄位過濾、`vcp audit lineage`（Wave 2）、repo 外 RSNA 原型的遷移、跨程序鎖。

## 2. 核心架構原則

- **未授權的列從不被解析。** 存取器以 plan 的 assignment 決定每一行能不能解析；未授權角色的行只經過 hasher 算身分。稽核驗收 1（非 train 列放會在解析時炸的 sentinel，train-only job 仍 PASS）靠這一條達成。
- **收據由存取器累計，呼叫端只能加 `notes`。** `AccessReceipt` 沒有任何呼叫端可設定的觀測欄位；它在 `__exit__` 時由存取器內部狀態組成、經 `ArtifactWriter` 落地。
- **產物在 open 時就 claim。** 沒關閉的存取器留下半途產物，`vcp artifact status` 看得到；例外離開一樣 commit 收據（`outcome=failed`）——讀了什麼就是讀了什麼。
- **宣告蓋不掉觀測。** 乾淨基底 = `clean_eval_subsets(plan, trained_on ∪ observed)`；收據看到讀過 valA，valA 就不再是這個 run 的乾淨基底，不管 `trained_on` 怎麼寫。
- **provenance 是算出來的，不存。** `receipt > export > declared` 三個等級在讀取時由收據與 run 的現況算出；舊 run、舊台帳不改、不補。
- **信任邊界明寫。** 收據證明的是**經此存取器**的讀取。process 直接 `open()` 檔案不在證明範圍；那是日後 I/O hook（C 路線）與 Wave 2 lineage audit 的事。
- **資料準備期維持全存取。** `data validate / split / audit / materialize / import` 本來就要全部角色，繼續用 `Dataset.load`；本層改的是消費者。

### 2.1 擴充點

`purpose` 是封閉字彙（§4：`train` / `export` / `measure` / `submit` / `custom`；準備期命令不產生收據，所以沒有 `materialize` / `import`）；`fields` 固定 `["all"]`，日後要做欄位過濾時在 schema 加值不改形狀；`ReceiptBinding` 是協定（§6.6），訓練層之外的執行環境（Kaggle kernel wrapper、1c 的 benchmark runner）各自實作綁定；1b-2 的 `SourceAudit` 可取代 open 時的整檔 hash 作身分來源。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 路線 | **A：框架內 `DatasetAccess` 存取器 + 收據** | B（實體切檔 + ACL）改磁碟佈局、Windows ACL 不可靠、process 層仍自我宣告；C（`sys.addaudithook` 記所有 `open()`）噪音大、DataLoader worker / memmap / C extension 是洞，只適合日後附加 |
| 身分與索引 | open 時 binary 串流一次：整檔 sha256 + 只 peek 行首 `sample_id` 建 offset 索引 | 不解析任何一行就能同時驗 `card.samples_hash` 與定位授權列；1b-2 可換成 `SourceAudit` 引用 |
| 未授權存取 | `ids()` / `iter()` / `records()` / `by_id()` 一律 `AccessDeniedError`（FAIL）並計數 | 稽核：未授權角色在 API 層立即拒絕；plan 雖進 git，但收據算的是讀取 |
| 收據落地 | open 時 `ArtifactWriter.create`，close（含例外）時寫 `receipt.json` 並 commit | 沒收尾的 job 留半途產物；失敗的讀取也留證據 |
| `fields` | 不過濾，收據記 `["all"]` | 洩漏向量是角色不是欄位；YAGNI |
| 等級 | `receipt`（有效且 `purpose=train` 的收據）> `export`（無收據但有 `source.export_manifest_sha`）> `declared`；`submit.yaml` 的 `require_provenance` 預設 `declared` | 舊 run 與直接讀 export 的訓練流程不被擋；要嚴就調高 |
| measure 的收據 | 不掛到 run，只存在 `artifacts/`；`run_id` 填被量的 run | run 的 `access` 是訓練讀取的證據；量測讀 eval 是應該的 |
| 匯出 | `export_subset` 以授權子集組一個 `Dataset(card, samples)` 視圖交給 exporter | exporter 登記表簽名不變；exporter 只看得到授權樣本 |
| 訓練 reader | `MaterializedReader` 在 `VCP_RUN_ID` 下必須給 `plan_id` / `subset`，並改成 context manager | 訓練 job 必須宣告範圍；離開才有收據 |
| 綁定 | 只有 `purpose=train` 的收據給 `receipt` 等級；`run_id=None` 的收據可手動掛上（`binding=manual`） | Kaggle 等無 `train run` 的訓練仍能有收據；lineage audit 日後可區分 |
| 版本 | 新產物 kind、台帳新欄位、VERDICT 新欄位與字彙 → MINOR，`0.5.0` | CHANGELOG 規則 |

## 4. 資料模型

### 4.1 收據（`src/vcp/data/access/schema.py`，pydantic，`extra="forbid"`）

```python
Purpose = Literal["train", "export", "measure", "submit", "custom"]
Binding = Literal["session", "manual"]
Grade = Literal["receipt", "export", "declared"]
GRADE_RANK: dict[str, int] = {"declared": 0, "export": 1, "receipt": 2}


class AccessedSubset(_Strict):
    role: Literal["train", "eval", "sealed"]
    ids_count: int             # 迭代到的不重複 id 數
    ids_sha256: str            # 排序後的 id 以 "\n" 串接的 sha256
    records_parsed: int        # 實際解析的行數；同一筆重複取只算一次


class AccessReceipt(_Strict):
    schema_version: int = 1
    dataset: str
    samples_hash: str          # card.samples_hash，亦即 open 時整檔 sha
    card_sha256: str           # dataset.yaml 的檔案 sha
    plan_id: str
    plan_sha256: str           # <plan_id>.json 的檔案 sha
    authorization_sha256: str  # sha256_json({card_sha256, samples_hash, plan_sha256, allowed})
    purpose: Purpose
    run_id: str | None = None  # 在 VCP_RUN_ID 之下才有
    attempt: int | None = None
    allowed: list[str]         # 授權子集，排序
    roles: dict[str, str]      # allowed 每個子集的角色
    accessed: dict[str, AccessedSubset]   # 只有真的迭代過的子集
    fields: list[str] = ["all"]
    denied: int = 0
    denied_first: list[str] = []          # 最多 5 筆，"<subset>" 或 "<subset>:<sample_id>"
    sealed_accessed: bool = False         # accessed 裡有 sealed 角色
    unseal_event_sha256: str | None = None  # open 時追加到 <plan>.unseal.jsonl 那一行的 sha
    outcome: Literal["completed", "failed"]
    exception: str | None = None          # failed 時的例外類別名
    started_at: str
    finished_at: str
    vcp_version: str
    notes: str = ""            # 呼叫端唯一能給的欄位


class AccessRef(_Strict):
    """run 上的一筆收據參照（train.yaml 與 run.yaml 同型）。"""

    artifact_id: str
    purpose: Purpose
    subsets: list[str]         # accessed 的鍵，排序
    sealed_accessed: bool
    denied: int
    receipt_sha256: str        # receipt.json 的 sha
    binding: Binding
```

驗證器：`allowed` 排序且不重複；`accessed` 的鍵 ⊆ `allowed`；`roles` 的鍵 = `allowed`；`denied_first` ≤ 5 筆；`outcome=failed` 時 `exception` 必填；`finished_at ≥ started_at`（都是 `stamp()`）。

### 4.2 既有模型的新欄位（都有預設，舊檔照讀）

| 模型（檔） | 新欄位 |
|---|---|
| `TrainRecord`（`train.yaml`） | `access: list[AccessRef] = []` |
| `RunCard`（`run.yaml`） | `access: list[AccessRef] = []` |
| `Reading`（`readings.jsonl`） | `provenance: Grade \| None = None`（被量 run 當時的等級） |
| `Judgement`（`judgements.jsonl`） | `provenance: Grade \| None = None`（候選 run 的等級） |
| `Staged`（`stage.json`） | `provenance: Grade \| None = None` |
| `PlatformProfile`（`submit.yaml`） | `require_provenance: Grade = "declared"` |
| `train/schema.EVENTS` | 加 `"access"` |
| `backup/schema.ROLES` | 加 `"access_receipt"`（緊接 `"run_card"` 之後，tier 1；也進 `CARD_ROLES`，時戳稽核讀 `started_at` / `finished_at` / `created_at`） |

### 4.3 provenance（`src/vcp/measure/provenance.py`）

```python
@dataclass(frozen=True)
class ProvenanceInfo:
    grade: Grade
    observed: list[str]      # 有效收據 accessed 子集的聯集，排序
    invalid: list[str]       # 失效收據的 artifact id
    receipts: list[AccessRef]


def read_receipt(data_root: Path, artifact_id: str) -> AccessReceipt
def provenance(card: RunCard, *, data_root: Path, configs_root: Path | None = None) -> ProvenanceInfo
```

一份收據**有效**的條件：產物 `verify` 無 mismatch / missing / extra；`receipt.json` 可解析且 sha 等於 `AccessRef.receipt_sha256`；`samples_hash == card.samples_hash`；`plan_id == card.plan_id`；`plan_sha256` 與 `card_sha256` 等於現在 plan 檔與 card 檔的 sha（其一變了，授權就失效——稽核驗收 4）。不符 → 進 `invalid`，不拋例外。等級：有 ≥1 份有效且 `purpose=train` 的收據 → `receipt`；否則 `card.source.export_manifest_sha` 有值 → `export`；否則 `declared`。`observed` 只算有效收據。融合 run（`source.framework == "fuse"`）自己沒有 `access`：等級 = 成員等級的最小值、`observed` = 成員 `observed` 的聯集、`invalid` 取聯集（沿 `fuse.json` 的成員遞迴）。

## 5. 目錄佈局

```
<VCP_DATA_ROOT>/artifacts/access_receipt/<id>/spec.json      # open 時 claim
<VCP_DATA_ROOT>/artifacts/access_receipt/<id>/receipt.json   # close 時
<VCP_DATA_ROOT>/artifacts/access_receipt/<id>/manifest.json  # commit
<VCP_CONFIGS_ROOT>/datasets/<name>/splits/<plan_id>.unseal.jsonl  # 沿用，開 sealed 子集時追加一行
```

`ArtifactSpec`：`kind="access_receipt"`、`id`（§6.5）、`dataset`、`plan_id`、`params={"purpose": …}`（在 run 下再加 `"run"`、`"attempt"`）、`inputs=[InputRef("card", card.yaml), InputRef("samples", samples.jsonl, sha256=samples_hash), InputRef("plan", <plan>.json, sha256=plan_sha256)]`。`samples.jsonl` 因此在 open 被讀兩次（索引 + `resolve_inputs`）、commit 再讀一次（drift）；它是 metadata 檔，可接受，1b-2 再省。

## 6. 存取協定（`src/vcp/data/access/`）

### 6.1 card-only

`Dataset.load_card(name, *, data_root=None, configs_root=None) -> DatasetCard`：讀 card、比名字，不碰 `samples.jsonl`。`assert_run_matches(card, dataset_card: DatasetCard)`（`measure/runs.py`）改收 card；`derive_trained_on` 改收 card。

### 6.2 API

```python
with DatasetAccess.open(
    name, plan_id,
    subsets={"train"},            # 或 roles={"train"}：二選一，必給一個
    purpose="train",              # Purpose
    unseal_reason=None,           # allowed 含 sealed 子集時必填
    caller=None,                  # 寫進 unseal 留痕；預設 purpose
    binding=None,                 # ReceiptBinding（§6.6）；None = 獨立收據
    notes="",
    data_root=None, configs_root=None,
) as access:
    access.card                   # DatasetCard
    access.plan                   # SplitPlan（已 assert_plan_matches）
    access.allowed                # frozenset[str]
    access.roles                  # dict[subset, role]
    access.ids("train")           # list[str]，只查 plan
    access.iter("train")          # Iterator[Sample]，只解析該子集的行
    access.records("train")       # dict[str, Sample]，第一次迭代後快取
    access.by_id("s0001")         # 該 id 的子集被授權才回傳
    access.subset_of("s0001")     # plan 查表，不算存取
receipt = access.receipt          # AccessReceipt；close 前為 None
receipt_id = access.receipt_id    # 產物 id
```

### 6.3 open

1. `load_card` → `load_plan` → `assert_plan_matches(plan, card)`。
2. `allowed` = `subsets` 或 `roles` 展開的子集（`plan.subset(name)` 不存在 → `PlanMismatchError`）；`allowed` 含 `role=sealed` 的子集而 `unseal_reason` 為空 → `SealedSubsetError`；給了 → 對每個 sealed 子集追加一行到 `<plan_id>.unseal.jsonl`（現有格式：`ts, plan_id, dataset_hash, subset, reason, caller`；`caller = caller or purpose`），記該行的 sha（多個 sealed 子集時記最後一行）。
3. **身分 + 索引**：以 `"rb"` 串流讀 `samples.jsonl`，每行：整行進 sha256；用 `rb'^\{"sample_id":\s*"((?:[^"\\]|\\.)*)"'` 取 id（`sample_json_line` 保證 `sample_id` 是第一個 key，單元測試釘住）；不合此形狀的行 → `ValidationFailed("not a samples.jsonl line", location=<檔>:<行號>)`（仍不解析）；重複 id → `ValidationFailed`；id 不在 `plan.assignment` → `InvariantError("assignment does not cover …")`；索引 `id → (offset, length)`。讀完：sha ≠ `card.samples_hash` → `IntegrityError("mismatch: samples.jsonl …")`；assignment 裡有檔案沒有的 id → `InvariantError`。**沒有任何一行被 `json` 解析。**
4. `authorization_sha256` = `sha256_json({"card_sha256", "samples_hash", "plan_sha256", "allowed"})`；`started_at = stamp()`。
5. claim 產物：`ArtifactWriter.create(spec)`（§5）；有 `binding` 時 id 由它給，撞到 `exists:` 就換下一個 seq 重試（上限 100 次），無 binding 用 §6.5 的獨立 id。

### 6.4 讀取與拒絕

- `ids(s)` / `iter(s)` / `records(s)`：`s ∉ allowed` → `AccessDeniedError(f"denied: subset {s!r} is not authorized (allowed: {sorted(allowed)})", fields={"subset": s})`，`denied += 1`、`denied_first` 加 `s`。授權時 `iter` 依 `sorted(plan.ids_in(s))` seek 每行、`Sample.model_validate_json`、`get_task(card.task).validate(sample, card)`，並累計 `accessed[s]`（ids、parsed）。
- `by_id(id)`：`subset_of(id)` 不在 `allowed` → 同上，`denied_first` 加 `f"{subset}:{id}"`；id 不在 plan → `ValidationFailed("not_found: sample …")`。
- 拒絕**不**關閉存取器：job 自己決定要不要炸；`denied` 進收據。

### 6.5 close 與收據

`__exit__`（含例外）與 `close()`：`finished_at = stamp()`；`outcome = "failed"` + `exception` 類別名（例外離開時）否則 `"completed"`；`sealed_accessed = any(accessed[s].role == "sealed")`；組 `AccessReceipt` → `write_json("receipt.json")` → `commit()`；有 `binding` 就呼叫 `binding.on_commit(AccessRef(...))`。二次 close 是 no-op。commit 失敗（例如 samples.jsonl 在 job 中被改 → `drift:`）：例外照拋，目錄留半途 + `failure.json`，`receipt` 屬性維持 None。

獨立收據 id：`f"{purpose}-{dataset}-{plan_id}-{utc_now():%Y%m%dT%H%M%S}-{os.urandom(2).hex()}"`（過 `validate_name`）。

### 6.6 綁定協定（data 層定義，train 層實作；data 不 import train）

```python
class ReceiptBinding(Protocol):
    run_id: str
    attempt: int
    def receipt_id(self, seq: int) -> str        # f"{run_id}-a{attempt}-{seq}"
    def next_seq(self) -> int                    # 該 attempt 已登記的收據數 + 1
    def on_commit(self, ref: AccessRef) -> None  # 追加 AccessRef 到 train.yaml、記 access 事件
```

### 6.7 消費者

| 呼叫點 | 改法 |
|---|---|
| `train/reader.MaterializedReader` | 內部改走存取器：`plan_id` / `subset` 給了 → 有注入的 `access` 就用它，否則 `VCP_RUN_ID` 下走 `Session.current().access(subsets={subset}, purpose="train", unseal_reason=reason if unseal else None)`，否則 `DatasetAccess.open(..., purpose=purpose or "custom")`；manifest 列只保留授權 id，陣列只載授權 id；改成 context manager（`with … as reader` / `close()`），離開才有收據。**`VCP_RUN_ID` 下沒給 `plan_id` / `subset` → `AccessDeniedError("denied: a training reader must name plan_id and subset")`。** `reader.dataset` 屬性移除，改 `reader.card` / `reader.access`（`0.5.0` MINOR）。無 plan / subset 且不在 run 下：維持 `Dataset.load`（準備 / 診斷用途，無收據）。 |
| `data/exporters/base.export_subset` | `DatasetAccess.open(name, plan_id, subsets={subset}, purpose="export", unseal_reason=reason if unseal else None, caller="vcp data export")` → `samples = list(access.iter(subset))` → `Dataset(access.card, samples)` 視圖交給 `exporter.run`（簽名不變）；export `manifest.json` 加 `"receipt": <artifact id>`；VERDICT 加 `receipt=`。 |
| `measure/measure.measure_run` | `load_context` 改 `load_card` + plan；量測前 `DatasetAccess.open(card.dataset, card.plan_id, subsets=subsets, purpose="measure", unseal_reason=…, caller=CALLER)`，`run_id` 填 `card.run_id`；每個子集 `access.records(subset)`。 |
| `train/run.train_run` 父程序、`measure/ingest`、`fuse/ablate`、`fuse/build`、`submit/{stage,profile,final}` | 需要 rows 的才走存取器，否則 `load_card` + plan；計畫期逐一確認哪些真的需要 rows（預期：`fuse build` 需要子集 id → plan 即可；`stage` 讀 test 資料集寫候選檔 → 存取器 `purpose="submit"`，收據 `run_id` 填候選 eval run）。 |
| `cli.py` 的 `data validate/split/lineage/audit/materialize`、importers | 維持 `Dataset.load`。 |

`Dataset.subset()` 保留（準備期程式與測試），不再是正式消費者的路。

## 7. 收據綁定到 run

### 7.1 在 `vcp train run` 之下

- `Session.current().access(subsets=…|roles=…, purpose="train", unseal_reason=None, notes="")`：dataset / plan 從 `train.yaml` 來；建 `SessionBinding(run_id, attempt=current_attempt(record))`：`receipt_id(seq) = f"{run_id}-a{attempt}-{seq}"`；`next_seq()` = `train.yaml` 裡該 attempt 前綴的 `access` 筆數 + 1；`on_commit(ref)` = `load_record` → `record.access + [ref]` → `save_record` → `append_event("access", attempt, artifact_id=, subsets=, denied=, sealed_accessed=)`。多個 DataLoader worker 各開各的存取器：`exists:` 就 seq+1 重試（Wave 1a 的 `mkdir` 是裁判）。run 期間只有子程序這一族寫 `train.yaml`（訓練層 spec §8.2 不變）。
- `train run` 命令結束後（它本來就重讀 `train.yaml`）：`card.access = record.access` → `save_run`；`info = provenance(card)`；VERDICT 加 `receipts=len(record.access)`、`denied=sum(ref.denied)`、`provenance=info.grade`；`observed_beyond = sorted(set(info.observed) - set(trained_on))` 非空 → **WARN** `observed_beyond_trained_on=<csv>`；`info.invalid` 非空 → WARN `receipt_invalid=<n>`。run 照樣存在；§8 的 measure / judge 會把那些子集踢出乾淨基底。`--resume` 的 attempt 各自累積（`-a2-1`…），觀測取聯集。

### 7.2 不在 `train run` 之下

`vcp eval ingest --receipt <artifact id>`（可重複；`IngestSpec.receipts: list[str]`）：`read_receipt` → 產物 `verify` 乾淨、`receipt.json` 解析；`receipt.dataset` / `plan_id` ≠ run 的 → `IntegrityError("mismatch: receipt … belongs to …")`；`receipt.run_id` 非 None 且 ≠ 此 run → `mismatch:`；`run_id=None` → `AccessRef.binding="manual"`，否則 `"session"`；同 `artifact_id` 已掛 → 跳過（冪等）；追加到 `card.access` 並 `save_run`。VERDICT 加 `receipts=`、`provenance=`。

## 8. 消費者與強制點

| 命令 | 改變 | 狀態規則 |
|---|---|---|
| `eval measure` | `info = provenance(card)`；預設子集 = `clean_eval_subsets(plan, trained_on ∪ observed)` 去掉 sealed（除非 `--unseal`）；`--subsets` 點名到 `observed` → FAIL `contaminated: subset 'valA' was read by the run (receipt <id>)`；每筆 `Reading.provenance = info.grade`；讀 eval 走存取器（`purpose=measure`） | 欄位 `provenance=`、`observed=`（非空時）；`receipt_invalid=` → WARN |
| `eval judge` | 候選**或**基準 run 的 `observed` 與預登記 `subsets` 相交 → verdict `INVALID`、reason `contaminated:<run>/<subset>`（每個相交的子集一條）；`Judgement.provenance` = 候選 run 的等級 | 欄位 `provenance=`；INVALID 的 exit 規則沿用現狀 |
| `submit stage` | `Staged.provenance` = 候選 eval run 的等級；現有 `trained_on_sealed` 檢查擴到 `observed` 含 `profile.sealed_subset` → FAIL `observed_sealed:`；`kind=candidate` 且 `GRADE_RANK[grade] < GRADE_RANK[profile.require_provenance]` → FAIL `provenance_required: run 'r' is declared, profile requires receipt`（非候選 waived 不套） | 欄位 `provenance=` |
| `submit final` | 每筆候選用**當下重算**的 `provenance()`；低於要求 → `eligible=False`、`why="provenance_required"`；`observed` 含 sealed → `why="observed_sealed"` | 欄位 `provenance=` 於各候選行 |
| `eval status` / `eval report` / `submit status` | 每個 run / staged 多印 `provenance`（`observed` 非空時附上） | 唯讀，不改狀態規則 |
| `train run` / `eval ingest` / `data export` | §7 / §6.7 | 見各節 |
| 備份證據圖 | `walk_run` 對 `card.access` 每筆：`artifacts/access_receipt/<id>/manifest.json` 與 `receipt.json` 以角色 `access_receipt` 進清單（sha 取自產物 manifest 的 `FileEntry`）；`judgement:` / `submission:` 走法經 run 自然帶到 | — |

## 9. CLI 總表（沒有新命令群）

| 命令 | 新選項 / 新欄位 |
|---|---|
| `vcp train run` | 欄位 `receipts=` `denied=` `provenance=`；WARN `observed_beyond_trained_on=` `receipt_invalid=` |
| `vcp eval ingest` | `--receipt ID`（可重複）；欄位 `receipts=` `provenance=` |
| `vcp eval measure` | 欄位 `provenance=` `observed=` `receipt_invalid=` |
| `vcp eval judge` | 欄位 `provenance=` |
| `vcp eval status` / `report` | 每 run 一個 `provenance` |
| `vcp data export` | 欄位 `receipt=` |
| `vcp submit stage` / `final` / `status` | 欄位 `provenance=` |
| `vcp artifact show/verify --kind access_receipt --id …` | 既有命令，看收據 |

## 10. 錯誤處理與 VERDICT 字彙

| 情境 | 類型 / `reason=` | 狀態 |
|---|---|---|
| 未授權子集的 `ids` / `iter` / `records` / `by_id`；`VCP_RUN_ID` 下 reader 沒宣告範圍 | `AccessDeniedError`（新，`core/errors.py`，`status="FAIL"`）`denied:` | FAIL |
| allowed 含 sealed 而無 `unseal_reason` | `SealedSubsetError`（現有） | ABORT（沿用） |
| `samples.jsonl` sha ≠ card；收據與 run 的 dataset / plan / run_id 不符 | `IntegrityError` `mismatch:` | FAIL |
| `--subsets` 點名被 run 讀過的子集 | `ValidationFailed` `contaminated:` | FAIL |
| 候選 / 基準讀過主張的子集 | `Judgement.verdict=INVALID`，reason `contaminated:<run>/<subset>` | 沿用 INVALID 規則 |
| 候選 `observed` 含 sealed | `ValidationFailed` `observed_sealed:` | FAIL |
| 等級低於 `require_provenance` | `ValidationFailed` `provenance_required:` | FAIL |
| samples 行不合形狀、重複 id、plan 未涵蓋 | `ValidationFailed` / `InvariantError`（現有語意） | FAIL / ABORT |
| 收據失效、觀測超出宣告 | — | WARN `receipt_invalid=` `observed_beyond_trained_on=` |

## 11. 隱私

收據只含 sha、計數、子集名、data root 相對路徑、時戳、build string 與 `notes`；不含樣本內容、標籤、影像路徑；sample id 只出現在 `denied_first`（≤ 5 筆）。`failure.json` 沿用 Wave 1a 的 redact。收據不記環境變數。

## 12. 與其他子專案的介面

- **core**：`AccessDeniedError`。
- **資料層**：`Dataset.load_card`；新套件 `data/access/`（`schema.py`、`access.py`、`receipt.py`）；`export_subset` 走存取器、manifest 加 `receipt`。
- **訓練層**：`MaterializedReader` context manager 與範圍規則；`Session.access` 與 `SessionBinding`；`train run` 抄收據進 `run.yaml` 與 WARN；`TrainRecord.access`、`EVENTS`。
- **量測層**：`measure/provenance.py`；`RunCard.access`、`Reading.provenance`、`Judgement.provenance`；`measure` / `judge` / `ingest --receipt` / `status` / `report`；`assert_run_matches` 收 card。
- **融合層**：`ablate` / `build` 改 `load_card` + plan（若需 rows 才走存取器）；融合 run 的 `access` 為空、等級取成員最低（`provenance()` 對 `framework=fuse` 的 run：成員 `provenance` 的最小 `GRADE_RANK`，`observed` 取聯集）。
- **提交層**：`PlatformProfile.require_provenance`、`Staged.provenance`、`stage` / `final` / `status`。
- **備份層**：`ROLES` / `CARD_ROLES` 加 `access_receipt`；`walk_run`。
- **產物層（1a）**：只消費，不改；`access_receipt` 是一種 kind。
- **專案**：`projects/rsna-knee/rsna_knee/training.py` 改 `with MaterializedReader(...) as reader:`（in-repo 唯一真消費者）。
- **文件**：README 資料 / 訓練 / 量測 / 提交四節各加收據與 provenance；AGENTS.md / CLAUDE.md 路徑（`artifacts/access_receipt/`）與常用命令（`--receipt`、`require_provenance`）；HANDOVER；CHANGELOG `0.5.0`。

## 13. 測試策略

- **單元**：`tests/unit/data/test_access.py`——行首 peek 規則（`sample_json_line` 第一個 key）、整檔 hash 身分、offset 索引、`subsets` / `roles` 展開、sealed / unseal 留痕與 `unseal_event_sha256`、四種拒絕與 `denied` / `denied_first`、**sentinel**（非 train 列的標籤部分改成壞 JSON，train-only 仍 PASS；把 train 列改壞則 FAIL）、收據內容（ids_sha256、parsed、authorization）、例外離開 `outcome=failed` 仍 commit、沒關閉 → 半途產物、二次 close no-op、commit 時 samples 漂移 → `drift:`；`tests/unit/data/test_access_schema.py`——模型驗證器。
- **訓練層**：`test_reader.py`（範圍規則、授權外 id 不載、context manager 收據、`VCP_RUN_ID` 下的 id 與 `train.yaml` 登記、seq 撞號重試）、`test_run.py`（抄進 `run.yaml`、WARN 欄位、`--resume` 累積）。
- **量測層**：`test_provenance.py`（三個等級、四種失效、observed 聯集、fuse run 取最低）、`test_measure.py`（乾淨基底踢除、`contaminated:`、`Reading.provenance`）、`test_prereg_judge.py`（INVALID contaminated、`Judgement.provenance`）、`test_cli_eval.py`（`--receipt` 三種拒絕與冪等）。
- **提交層**：`test_stage.py` / `test_final.py`（`require_provenance` 三檔、`observed_sealed:`、final 當下重算）。
- **備份層**：`test_evidence_run.py`（`access_receipt` 角色與 sha）。
- **端到端** `tests/unit/test_e2e_access.py`（稽核 release gate 1、2）：假訓練命令在 `vcp train run` 下用 `MaterializedReader` → 收據 → `ingest`（不給 `--trained-on`）→ `measure` → `judge` PASS，`Reading` / `Judgement` 的 `provenance=receipt`；第二個 run 的命令多讀 valA → `train run` WARN → `measure` 預設踢掉 valA、`--subsets valA` FAIL → 在 valA 上的 judge `INVALID contaminated`；`submit.yaml` `require_provenance: receipt` 擋一個 `declared` 的舊 run、放收據 run；sealed：`iter("holdout")` 與 `by_id(holdout id)` 都擋且不解析任何行；`--unseal --reason` 後留痕、收據 `sealed_accessed=true`；兩個 root 掃描沒有樣本內容外洩（收據裡沒有影像路徑與標籤）。
- **真資料**：整合測試把真資料的 `dataset.yaml`、`<plan>.json` 與 `samples.jsonl` 複製到暫時的 data / configs root（metadata 檔都小），在那裡 `DatasetAccess.open(rsna-knee, plan, subsets={"train"})` 並斷言：train 子集全部可迭代、`ids("valA")` 被拒、收據的 `ids_sha256` 等於由 plan 算出的值；真 data root 前後樹不變（收據只寫在暫時 root）。

## 14. 驗收條件

1. 非 train 列放解析時會炸的 sentinel，train-only job PASS，且 `samples.jsonl` 的整檔 sha 仍被驗證。
2. train-only 存取器上 `iter("valA")` / `ids("valA")` / `by_id(<valA id>)` 在解析任何一行之前 fail closed；`denied` 與 `denied_first` 進收據。
3. 收據的 `accessed` / `allowed` / `denied` / `sealed_accessed` 只由存取器累計；呼叫端能給的只有 `notes`。
4. plan、card 或 `samples.jsonl` 任一 sha 改變，既有收據失效（`invalid`），run 等級退到 `export` / `declared` 並 WARN。
5. sealed 子集即使知道 sample id 也繞不過；`unseal_reason` 才開，留痕且收據標記。
6. 經 `vcp train run` + `MaterializedReader` 的 job 100% 有 runtime 收據；舊 `run.yaml` / `train.yaml` / 三本台帳 / `stage.json` 照讀，等級為 `declared` 或 `export`。
7. 覆蓋率 ≥ 80%、ruff `check` 與 `format --check` 乾淨、每個命令 VERDICT。

## 15. 不在範圍

`SourceAudit` 與選取列陣列存取器（1b-2）；`CodeSnapshot`、授權綁定 benchmark（1c）；子程序 I/O 稽核 hook（C 路線）；`fields` 欄位過濾；`vcp audit lineage`（Wave 2）；repo 外 RSNA 原型遷移；跨程序鎖；VERDICT JSON schema（VCP-032）。以上皆為已預留的擴充點，不是設計缺口。

## 16. 補充決定（實作期，Plan 9）

1. `read_receipt` 放 `data/access/receipt.py`（收據格式是資料層的），`measure/provenance.py` import 它。
2. `DatasetAccess.open` 多 `run_id=None`：無 binding 的收據（measure、stage）用它填 `run_id`；有 binding 時 binding 優先。
3. `ingest`、`fuse build/ablate`、`submit profile`、`submit verify` 維持 `Dataset.load`（各自要 eval / 全部 rows 或只是重渲染驗檔，不是訓練讀）；`stage` 的 eval 側 `load_card`、test 側走存取器（`purpose=submit`，`run_id` 填候選 eval run）；`judge` / `sigma` / `anchor` 沿用 `load_context`，`measure_run` 用 `load_card_context`。
4. `assert_run_matches(card, dataset_card)` 第二個參數改收 card。
5. unseal 留痕抽成 `data/dataset.append_unseal`，存取器與 `Dataset.subset` 共用。
6. `MaterializedReader` 建構時迭代授權子集；`reader.dataset` 移除，改 `card` / `access` / `sample(id)`；不在 run 下且無 plan / subset 維持 `Dataset.load`。
7. `train run` 用 `provenance()` 算等級，`RunResult` 多 `receipts / denied / provenance / observed_beyond / receipt_invalid`。
8. 融合 run 的 provenance 遞迴用 lazy import 避免 measure ↔ fuse 循環。
9. `FinalEntry.provenance`；`submit status` 只在 human / payload 印每筆等級；`eval status` 多 `receipt_runs=` `export_runs=` `declared_runs=`。
10. judge 的 contaminated 檢查在 measured-before-prereg 之前，INVALID 後不跑 bootstrap。
11. 索引時只 `json.loads` `sample_id` 那個字串，整行不解析；`iter()` 立刻授權（不是 generator）。
12. `data/access/__init__.py` 不 import 任何東西、`schema.py` 只 import core——避免 measure.schema ↔ access 的 import 環。
13. 存取器一次只開一個 sealed 子集（多於一個 → `ValidationFailed("sealed: an access may open at most one sealed subset per receipt …; open them separately")`），`unseal_event_sha256` 維持單值；unseal 留痕是 open 的最後一步（身分與覆蓋檢查、產物 claim 之後），留痕本身失敗時收據以 `outcome=failed` 結案而不留孤兒 claim。
14. `roles=` 展開後必須至少對到一個子集，否則 `ValidationFailed("roles: no subset of plan … has role(s) …")`。
15. 身分（`samples_hash`）與覆蓋（plan 的 id 不在 `samples.jsonl` 裡）失敗都丟 `IntegrityError("mismatch: …")`（FAIL），不是 `InvariantError`（ABORT）。
16. `_read` 解析後驗 `sample_id` 與索引一致（`samples.jsonl` 在 open 後被改寫 → `mismatch:`），不只靠 close 時的 drift 檢查。
17. 收據產物的 `inputs` 只列 `samples.jsonl`（在 data root 下，manifest 記相對路徑）；`card_sha256` / `plan_sha256` 進產物 `params`——configs root 在 data root 之外，列成 input 會把絕對路徑寫進 manifest，違反 §11。
18. `MaterializedReader` 建構在存取器開啟後失敗（materialize 快取缺列等）會先把自己開的存取器以 `failed` 關閉再拋出；注入的存取器留給擁有者。
19. `vcp eval status` 對算不出 provenance 的 run（例如融合 run 的成員 `run.yaml` 遺失）記 `provenance_failed=`（WARN，該 run 仍計入 `runs=`）而不中止；`vcp submit status` 對缺 `stage.json` 的提交印 `-`；兩者維持唯讀且不依賴資料根完整。`judge` 對沒有 `run.yaml` 的 run 以 `declared` 計（仍因缺讀數 FAIL）；`measure` / `stage` / `final` 對壞掉的融合 run 維持 fail-fast。
20. `measure` 的 `--unseal` 無 `--reason` 沿用舊訊息 `SealedSubsetError("unseal requires a non-empty reason")`，在開存取器之前檢查。`assert_run_matches` 的呼叫點實為八處（`measure/measure.py` 三處：`load_context`、`load_card_context`、錨點檢查；其餘 `fuse/build.py`、`fuse/members.py`、`measure/ingest.py`、`submit/stage.py`、`train/run.py` 各一處）。
