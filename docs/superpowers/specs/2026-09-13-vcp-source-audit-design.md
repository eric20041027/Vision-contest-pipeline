# vcp 來源稽核與選取列存取設計（稽核 Wave 1b-2）

日期：2026-09-13。來源：`docs/audits/2026-09-11-vcp-improvement-audit.md` VCP-002（大型來源每個 job 都整檔 hash，與 train-only 列存取衝突）。前置：v0.4.0 不可變產物層（`docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md`）、v0.5.0 角色範圍存取與收據（`docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md`，下稱 1b-1 spec）。目標版本 `0.6.0`。

## 1. 目的

1b-1 的 `DatasetAccess` 保證未授權的列**不被解析**，但每次 open 仍把整個 `samples.jsonl` 讀過一遍算身分。VCP-002 要的是：整檔 hash 在資料準備期做**一次**，之後每個 job 只驗**自己讀到的列**；未授權的列連讀都不讀。本層加一種不可變產物 `source_audit`（逐列 sha 索引），讓存取器以它作身分來源，收據記下用了哪一份稽核。

範圍（使用者決定）：現有資料形態——`samples.jsonl` 與每樣本一檔的 materialize 快取；稽核由準備期命令自動產生；沒有稽核的舊資料集退回整檔 hash 並 WARN。單一大陣列（整個資料集一個 npy memmap）不在本層。

## 2. 核心架構原則

- **整檔 hash 只在準備期做一次。** `vcp data import` / `vcp data validate` 本來就要整檔存取，由它們寫 `source_audit`；消費者（train / measure / export / stage）不再整檔讀。
- **有稽核時，未授權的列不被讀取。** 索引來自稽核產物；存取器只 `seek` 到授權的列。沒稽核時維持 1b-1 的「只過 hasher、不解析」。
- **讀到的每一列各自驗證。** 索引記每列 bytes 的 sha；`_read` 先比 sha 再比 `sample_id` 再解析。同長度改一列也會被抓到。
- **稽核是內容定址的不可變產物。** id 由 `samples.jsonl` 的 sha 決定；同內容重用不重算；重新匯入就是新 id，舊的留著（1a 規矩）。
- **收據說出身分來源。** `identity: source_audit | full_hash` 與稽核 id 進收據與 `AccessRef`；provenance 等級**不變**（等級只看 purpose 與有效性），`identity` 給 WARN 與 Wave 2 lineage 用。
- **稽核壞掉就 fail closed，稽核缺席就退回。** 找不到稽核 → 整檔 hash + WARN；找到但驗不過 → `mismatch:` FAIL，不退回。
- **信任邊界不變。** 收據證明的仍是經存取器的讀取；I/O hook 留 C 路線。

### 2.1 擴充點

`SourceAudit.schema_version`；日後單一大陣列模式是另一個 kind（例如 `array_audit`）與另一個存取器，`identity` 字彙屆時加值；materialize 快取的 `manifest.jsonl` sha 若要進證據鏈，加進稽核的 `caches` 欄位即可（本層不做）。

## 3. 決策表

| 問題 | 決定 | 理由 |
|---|---|---|
| 稽核覆蓋什麼 | `samples.jsonl` 的逐列索引與整檔身分 | 現有形態；materialize 快取已有逐檔 sha 且 reader 只驗讀到的檔 |
| 誰產生 | `vcp data import`、`vcp data validate` 自動產生；`Dataset.save` 不碰產物 | 準備期有全存取；程式庫用法與測試不變 |
| id | `src-<dataset>-<samples_hash 前 16 碼>` | 內容定址；同內容重用；不做 supersedes 鏈 |
| 沒稽核 | 退回整檔 hash，收據 `identity=full_hash`，train run / measure WARN | 使用者選；舊資料集重跑 `validate` 就升級 |
| 稽核壞掉 | `IntegrityError("mismatch: …")`，不退回 | fail closed |
| 列驗證 | 索引記每列 raw bytes（含換行）的 sha256，`_read` 先驗 | VCP-002 驗收：篡改 selected row 必炸 |
| 收據 inputs | 走稽核時列稽核的 `manifest.json`，不列 `samples.jsonl` | `ArtifactWriter.create` 會 hash inputs，列 samples 等於白做 |
| provenance | 不變 | identity 不是可信度等級，是事實 |
| 備份 | 角色 `source_audit`，tier 2，不進 `CARD_ROLES` | `index.jsonl` 隨樣本數長；jsonl 不是 card |

## 4. 資料模型

### 4.1 `source_audit` 產物

`src/vcp/data/source_audit.py`：

```python
KIND = "source_audit"
AUDIT_FILE = "audit.json"
INDEX_FILE = "index.jsonl"
Identity = Literal["source_audit", "full_hash"]

class SourceAudit(_Strict):            # audit.json
    schema_version: int = 1
    dataset: str
    samples_hash: str                  # 64 hex，== card.samples_hash
    size_bytes: int                    # samples.jsonl 位元組數
    line_count: int
    created_at: str
    vcp_version: str

class IndexRow(_Strict):               # index.jsonl 每行一筆，檔案順序
    sample_id: str
    offset: int                        # 該列在檔案中的起點
    length: int                        # 含結尾換行
    sha256: str                        # 該列 raw bytes（含換行）的 sha256

def audit_id(dataset: str, samples_hash: str) -> str      # f"src-{dataset}-{samples_hash[:16]}"
def audit_spec(paths, card) -> ArtifactSpec               # kind/id/dataset/params/inputs/id_pattern
def write_source_audit(paths, card, *, data_root) -> SourceAuditResult
def load_source_audit(paths, card, *, data_root) -> LoadedAudit | None
```

`ArtifactSpec`：`kind="source_audit"`、`id=audit_id(...)`、`dataset=card.name`、`params={"samples_hash": …, "size_bytes": "…", "line_count": "…"}`、`inputs=[InputRef(name="samples", path=samples.jsonl, sha256=card.samples_hash)]`、`id_pattern=r"^src-(?P<dataset>.+)-[0-9a-f]{16}$"`。

`SourceAuditResult(artifact_id, state: Literal["created","reused"], manifest_sha256)`；`LoadedAudit(artifact_id, manifest_sha256, audit: SourceAudit, index: dict[str, tuple[int, int, str]])`。

### 4.2 收據與 ref 的新欄位

`AccessReceipt` 加 `identity: Identity = "full_hash"`、`source_audit: str | None = None`、`source_audit_sha256: str | None = None`（64 hex，與 `source_audit` 同時有或同時無）；`AccessRef` 加 `identity: Identity = "full_hash"`、`source_audit: str | None = None`。預設值讓 v0.5.0 的收據與 `run.yaml` / `train.yaml` 照讀。

`Identity` 定義在 `vcp.data.access.schema`（core-only 模組，`source_audit.py` 從那裡 import，避免環）。

### 4.3 收據產物的 spec

走稽核時 `receipt_spec(...)` 的 `inputs=[InputRef(name="source_audit", path=<artifacts/source_audit/<id>/manifest.json>, sha256=<manifest sha>)]`，`params` 多 `samples_hash`；退回路線維持 1b-1（`inputs=[samples]`）。

## 5. 佈局

```
<data_root>/artifacts/source_audit/src-<dataset>-<hash16>/
    spec.json        # open 時
    audit.json       # SourceAudit
    index.jsonl      # IndexRow × line_count（streaming 寫，reserve）
    manifest.json    # commit 點
```

`artifacts/source_audit/supersession.jsonl` 不會有內容（本層不用 supersedes）。

## 6. 稽核產生協定（`write_source_audit`）

1. `spec = audit_spec(paths, card)`；`reuse(spec, data_root, check_files=True)` 命中 → `state="reused"`（`spec_mismatch:` / 檔案不符 → `IntegrityError` 往上丟；半途目錄 → 1a 的 `partial:`，CLI FAIL 並提示 `vcp artifact clean`）。
2. 沒命中 → `ArtifactWriter.create(spec, data_root=)`（它先 hash inputs：這是準備期，兩趟可接受）。
3. `with writer:`：`reserve(INDEX_FILE)` 取得路徑，一趟讀 `samples.jsonl`：每行 `_LINE` peek 取 `sample_id`（沿用 `access.py` 的規則：不解析整行、重複 id 即 `ValidationFailed`）、算該行 sha、累計整檔 sha、寫一行 `IndexRow`；結束後整檔 sha 必須等於 `card.samples_hash`，否則 `IntegrityError("mismatch: …")`（writer 的 `__exit__` 留 `failure.json`）。
4. `write_json(AUDIT_FILE, SourceAudit(...))`，`commit()`；回 `state="created"`。
5. 呼叫點：`vcp data import`（`Dataset.save` 之後，importer 回傳後在 CLI 層呼叫）、`vcp data validate`（`Dataset.load` 驗過 hash 之後）。兩者 VERDICT 多 `source_audit=<id>`、`source_audit_state=created|reused`。

## 7. 存取協定變更（`DatasetAccess.open` / `_read`）

### 7.1 open 的三條路

1. `load_source_audit(paths, card, data_root)`：目錄不在或沒有 `manifest.json`（半途）→ `None`。有 → `store.verify` 必須乾淨、`audit.samples_hash == card.samples_hash`、`audit.size_bytes == samples.jsonl.stat().st_size`、`audit.line_count == len(index)`，任一不符 → `IntegrityError("mismatch: source audit <id> …")`。回 `LoadedAudit`。
2. `LoadedAudit` 有 → `identity="source_audit"`，索引 = `{id: (offset, length, sha)}`，**不呼叫 `index_samples`**，不讀 `samples.jsonl`。
3. `None` → 1b-1 路線：`index_samples` 整檔一趟，`identity="full_hash"`，索引 sha 欄位為 `None`。

coverage 檢查（plan assignment 與索引互相覆蓋）、sealed / roles / unseal 規則全部不變。

### 7.2 `_read`

讀 `raw = f.read(length)` 後：有索引 sha 時 `sha256(raw) != sha` → `IntegrityError("mismatch: row <id> differs from source audit <audit id>", fields={"sample": id})`；再 `rstrip(b"\r\n")`、比 `sample_id`、解析、`task.validate`。

### 7.3 收據

`_build_receipt` 填 `identity` / `source_audit` / `source_audit_sha256`；`_close` 的 `AccessRef` 帶 `identity` 與 `source_audit`。

### 7.4 `MaterializedReader`

不變（它走 `DatasetAccess`）。

## 8. 消費者與 CLI

| 消費者 | 變更 |
|---|---|
| `vcp data import` / `validate` | 產稽核；VERDICT `source_audit=` `source_audit_state=` |
| `vcp train run` | 結束時任一 `card.access` 的 `identity == "full_hash"` → warning `source_audit=missing`（VERDICT 欄位 `source_audit=missing`，status WARN） |
| `vcp eval measure` | 自己開的收據 `identity` 進 VERDICT `identity=`；`full_hash` → warning `source_audit=missing` |
| `vcp data export` | VERDICT `identity=`；`full_hash` → WARN |
| `vcp submit stage` | VERDICT `identity=`（不 WARN；stage 的 WARN 留給 pairing） |
| `eval status` / `submit status` / `report` / provenance | 不變 |
| `vcp artifact status/show/verify --kind source_audit` | 現成 |
| 備份 `walk_run` | 每份 ref 若有 `source_audit`：加 `artifacts/source_audit/<id>/{manifest.json, audit.json, index.jsonl}`，角色 `source_audit`（`ROLES` 在 `access_receipt` 之後，`_TIER2` 加入），不進 `CARD_ROLES` |
| RSNA 專案 | 程式不改；RUNBOOK 加「匯入後 `vcp data validate` 產 source audit」 |

## 9. 錯誤字彙

- `mismatch:`（`IntegrityError`，FAIL）：稽核 verify 失敗、`samples_hash` / `size_bytes` / `line_count` 不符、列 sha 不符、產稽核時整檔 sha ≠ card。
- `partial:` / `exists:` / `spec_mismatch:`：沿用 1a（半途目錄、重 claim、spec 不同）。
- WARN 欄位：`source_audit=missing`（train run / measure / export）。

## 10. 隱私

稽核只含 `sample_id`、offset、length、sha、計數、時戳、build string；不含列內容。`sample_id` 本來就在 git 內的 plan 檔裡，不是新的暴露。收據新欄位只多稽核 id 與 sha。

## 11. 介面

- `vcp.data.source_audit`：`KIND`、`AUDIT_FILE`、`INDEX_FILE`、`SourceAudit`、`IndexRow`、`SourceAuditResult`、`LoadedAudit`、`audit_id`、`audit_spec`、`write_source_audit`、`load_source_audit`。
- `vcp.data.access.schema`：`Identity`；`AccessReceipt` / `AccessRef` 新欄位。
- `vcp.data.access.receipt.receipt_spec(..., source_audit: LoadedAudit | None)`。
- `vcp.data.access.access.DatasetAccess`：`identity`、`source_audit_id` 屬性；`index` 內部型別改 `dict[str, tuple[int, int, str | None]]`。
- `vcp.train.run.RunResult.source_audit_missing: int`；`MeasureResult.identity`；`ExportResult.identity`；`Staged` 不加欄位（VERDICT 欄位即可）。
- `vcp.backup.schema.ROLES` + `source_audit`。

## 12. 測試與驗收（對應稽核 VCP-002 的五條）

1. **不再整檔讀**：有稽核時 monkeypatch `vcp.data.access.access.index_samples` 與 `vcp.core.hashing.sha256_file` 為 raise → train-only open + iter 全部成功；收據 `identity == "source_audit"`。
2. **未授權列不被讀**：非 train 列改成無效 bytes（連 UTF-8 都不是）→ train-only job PASS（1b-1 的 sentinel 只證明「不解析」，這裡證明「不讀」——無效 bytes 若被讀進 hasher 也不會炸，所以改用 1. 的「不呼叫整檔 hash」加上 3. 的「篡改未選列不影響」共同證明）。
3. **fail closed**：篡改 `audit.json`、`index.jsonl`、`manifest.json` 任一 → open `mismatch:`；同長度改一個 train 列 → `_read` `mismatch:`；改一個 valA 列 → train-only job 照常 PASS；`samples.jsonl` 被 append（size 變）→ open `mismatch:`。
4. **等價**：同一組 id，有稽核與退回路線的 `records()` 完全相同；`records_parsed` 相同。
5. **重用**：`validate` 兩次第二次 `reused`；兩份收據引用同一稽核 id；`vcp artifact status --kind source_audit` 算到 1。
6. 退回：沒稽核 → `identity == "full_hash"`，`train run` VERDICT `source_audit=missing` status WARN；`validate` 之後重跑 → 不再 WARN。
7. 端到端 CLI：`import` → `train run`（無 WARN）→ `measure` → 刪掉稽核目錄 → `measure` WARN。
8. 真資料唯讀整合測試：複製 card / plan / samples.jsonl 到 tmp，`write_source_audit`，open train-only，斷言 `index_samples` 未被呼叫、真根目錄不變。
9. release 回歸門檻多一列 `wave 1b-2 (VCP-002)`。

## 13. 不在範圍

單一大陣列 materialize 模式與 mmap 選取列存取器；materialize 快取進稽核；raw 檔案稽核（`raw_manifest.txt` 已存在）；I/O hook；judge / sigma / anchor 改走存取器（1b-1 後記待辦）；1c。

## 14. 版本

`0.6.0`（MINOR）：新產物 kind、收據與 ref 新欄位、VERDICT 新欄位與 WARN 字彙。

## 15. 補充決定（實作期）

由 Plan 10 執行時填寫。
