# vcp 不可變產物層設計（稽核 Wave 1a）

- 日期：2026-09-11
- 狀態：v1（brainstorming 逐節核可後寫成）
- 來源：`docs/audits/2026-09-11-vcp-improvement-audit.md`（VCP 使用後改進稽核）——VCP-005「artifact 路徑可被覆寫，破壞不可變證據」（第二次 submission 的 pre-submission receipt 在同一路徑被覆寫，原始 sha `4cc083…` 的 bytes 無法復原）、VCP-007「ID、seed、output root 與 audit pin 缺少共同 contract」（six-slot v2 的輸出 ID 寫 seed 42、CLI 仍收 seed 43）、§11 Wave 1 第 1–2 項、§12 release gate 第 4 條、§13「100% formal artifacts 由 immutable writer 建立」、§14「不把舊 artifact 的 source drift 用覆寫 config 修掉；應保留舊 snapshot 或建立新 ID」
- 前置：子專案 0–6（六層皆已交付，`v0.3.0`）；備份層 spec §4.1 的 `path` 規則（`check_relative_path`）在此沿用；`vcp.core.build.build_string()`（`v0.2.0`）是產物記的版本
- 分段：稽核 Wave 1 拆成三段——**1a 本 spec**（不可變產物基底）→ 1b 角色範圍存取與收據（VCP-001/002/003）→ 1c 程式碼快照與授權（VCP-004/006）。1b 的 `AccessReceipt` / `SourceAudit`、1c 的 `CodeSnapshot` / `ArtifactAuthorization` 都是本層的產物；先做本層，收據才不是「可以被覆寫的自我宣告」
- 後續：Wave 1 第 5 項（小型 synthetic plugin 端到端、RSNA 原型遷移）在 1c 之後；Wave 2 的 evidence graph 以 manifest sha 把產物收進 backup 清單

## 1. 目的

給 vcp 與比賽程式一個「寫一次、可驗證、可接替、不可覆寫」的正式產物容器：一個目錄、一份 manifest、由框架而非呼叫端決定的位置與 commit 語意。修的是一件真實事故（receipt 被同路徑覆寫）與一類靜默錯誤（ID 說一套、內容另一套、目錄存在就當快取）。之後每一層產生的收據、快照、選型輸出、feature 檔都經它落地，稽核的 lineage 才有可信的葉節點。

不在本 spec：per-unit / resume 收據（VCP-029）、content-addressed blob 與去重 GC（VCP-028）、kind 登記表與 plugin SDK（VCP-031）、跨程序鎖、把換寫留痕的卡（`run.yaml` / `train.yaml` / `fuse.json`）改成不可變、RSNA 原型的遷移（§14）。

## 2. 核心架構原則

- **有 `manifest.json` 才是產物。** 目錄由 `mkdir` 獨佔建立，manifest 最後才獨佔寫；commit 之前的任何崩潰只留下一個沒有 manifest 的目錄，沒有任何 consumer 會把它當成產物。對應稽核 VCP-005 驗收「crash 於 commit 前不能留下看似完成的 manifest」。
- **ID 在 open 那一刻被搶走。** 撞到既有目錄（完整或半途）立刻失敗，不是跑了三小時後才被拒絕。修正一律用新 id 並宣告 `supersedes`。
- **位置只由 spec 決定。** `<data_root>/artifacts/<kind>/<id>/`，CLI 與呼叫端都不能指定別處；`kind` 不是登記表，是 `validate_name` 過的路徑段（plugin SDK 是 Wave 3）。
- **ID 與結構化欄位分開存、但要互相對得上。** `seed` / `dataset` / `plan_id` / `params` 是欄位；`id_pattern` 若給，具名群組必須等於同名欄位。對應 VCP-007。
- **重用 = 完整 spec 相等，不是目錄存在。** 含每個 input 的現算 sha。
- **manifest 是真相，台帳是索引。** `supersession.jsonl` 只增、在 commit 之後追加、可由已 commit 的 manifest 重建；`clean` 沒有程式路徑能刪到有 manifest 的東西。
- **一個原語，多個使用者。** 「同目錄暫存 → fsync → 目標不存在 → `os.replace`」抽成 `vcp.core.atomic.write_once`，artifact writer 的每個檔與 vcp 自己四個寫一次的正式檔都經它。

### 2.1 擴充點

本層沒有登記表。要加一種產物 = 選一個 `kind` 名字並用 `ArtifactWriter`；要讓 kind 帶 schema、預設 `id_pattern` 或宣告位置，是 Wave 3 plugin SDK 的事，屆時在 kind 上加登記表、不改本層的目錄佈局與 manifest 形狀。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 容器形狀 | 目錄 + `manifest.json` 當 commit 標記（方案 A） | 不 rename 目錄，Windows / POSIX 行為一致；ID 在 open 就裁決；多檔輸出（npy + receipt）自然 |
| 否決 | `.partial-<nonce>` + 目錄 rename（方案 B）；單一 JSON 檔（方案 C） | B：POSIX `rename` 對已存在的空目錄靜默取代、ID 衝突到 commit 才知道；C：裝不下多檔 |
| 位置 | 只在 data root：`<data_root>/artifacts/<kind>/<id>/` | 產物可能很大、每個 job 一個，不進 git；要進 git 的是「指向它的決定」（預登記、提交台帳、backup manifest 已在 git） |
| 半途目錄 | 原地保留，`status` 列出，`clean` 依寬限期移除 | 稽核：interrupted temp 要能被辨識；失敗證據不該自動消失 |
| 大檔 | `reserve(name)` 回傳最終路徑，呼叫端自己寫，commit 時串流雜湊 | 20 GB 的 features 不該複製一次 |
| 重用 | `reuse(spec)` 逐欄相等（含 inputs 的現算 sha），`notes` 不比；預設不重雜湊檔案 | VCP-007：目錄存在 ≠ 可重用；大陣列完整性由 1b 的 source audit 負責 |
| 接替 | `supersedes` 必帶 `supersedes_reason`；commit 時驗舊產物（存在、已 commit、逐檔 sha 相符）並記其 manifest sha；分叉不禁止、`head` 回多個、`status` WARN | 稽核 VCP-005：supersession 只能指向存在且 hash 相符的 artifact；第二次修正同一個舊產物是合法的 |
| 錯誤類別 | 全部 FAIL（`ValidationFailed` / `IntegrityError`），沒有 ABORT 類 | 本層沒有登記表、沒有外部工具 |
| 版本 | 新命令群 + 新產物形態 → MINOR，`0.4.0` | CHANGELOG 規則 |

## 4. 資料模型（`src/vcp/artifact/schema.py`，pydantic，`extra="forbid"`）

```python
class InputRef(_Strict):
    name: str                 # job 給它的名字：plan / samples / source_audit / config / code_snapshot …
    path: str | None = None   # posix；在 data root 內存相對路徑（core.paths.store_path），外則絕對
    sha256: str | None = None # path / sha256 至少給一個；open 時有 path 就現算（同時給 sha 而不符 → mismatch），commit 時重驗

class ArtifactSpec(_Strict):
    kind: str                          # validate_name
    id: str                            # validate_name；呼叫端自訂
    dataset: str | None = None
    plan_id: str | None = None
    seed: int | None = None
    params: dict[str, str] = {}        # 結構化欄位；值是字串，同指標參數的慣例
    inputs: list[InputRef] = []        # 名字唯一
    id_pattern: str | None = None      # 帶具名群組的 regex；群組名 = seed | dataset | plan_id | params 的鍵
    supersedes: str | None = None      # 同 kind 的舊 id
    supersedes_reason: str | None = None  # supersedes 給了就必填
    notes: str = ""

class FileEntry(_Strict):
    name: str                 # 產物內的相對 posix 路徑（備份層的 check_relative_path 規則）
    bytes: int
    sha256: str

class ArtifactManifest(_Strict):
    schema_version: int = 1
    spec: ArtifactSpec        # 含 open 時解析後的 inputs
    files: list[FileEntry]    # 依 name 排序
    created_at: str           # commit 時的 stamp()
    vcp_version: str          # build_string()
    supersedes_sha256: str | None = None   # 舊產物 manifest.json 的 sha

class SupersessionRow(_Strict):
    ts: str
    kind: str
    id: str
    manifest_sha256: str
    supersedes_id: str
    supersedes_sha256: str
    reason: str
```

驗證器：`kind` / `id` 走 `validate_name`；`id_pattern` 給了就必須 `re.fullmatch` `id`，且每個具名群組的值等於 `str(seed)` / `dataset` / `plan_id` 或 `params[群組名]`（群組名不在這四類 → 拒絕）；`inputs` 名字不重複、每個至少給 `path` 或 `sha256`；`supersedes` 與 `supersedes_reason` 同進退；`supersedes` 不可等於 `id`；`FileEntry.name` 過 `check_relative_path`（從 `backup/schema.py` 搬到 `core/paths.py`，備份層改從那裡 import，行為不變）且不是保留名（`manifest.json`、`spec.json`、`failure.json`）；`ArtifactManifest.files` 名字唯一、`spec.inputs` 每個都有 `sha256`（open 時已解析）。pydantic 的 `ValidationError` 包成 `ValidationFailed(str(e), location=…)`，與 `load_yaml_model` 同慣例。

`spec.json`（open 時寫）：`{"spec": <解析後的 spec>, "opened_at": <stamp>, "vcp_version": <build string>}`。`failure.json`（例外離開 `with` 時寫）：`{"ts", "exception": <類別名>, "message": <redact 後>}`。

## 5. 目錄佈局

```
<VCP_DATA_ROOT>/artifacts/<kind>/<id>/spec.json          # open 時
<VCP_DATA_ROOT>/artifacts/<kind>/<id>/<檔案們>            # write_* / add_file / reserve
<VCP_DATA_ROOT>/artifacts/<kind>/<id>/manifest.json      # commit = 產物存在
<VCP_DATA_ROOT>/artifacts/<kind>/<id>/failure.json       # 只在例外離開時
<VCP_DATA_ROOT>/artifacts/<kind>/supersession.jsonl      # 只增的索引
```

`core/paths.py` 新增 `artifacts_root(data_root)`、`artifact_dir(data_root, kind, id)`（兩個名字都 `validate_name`）。`DatasetPaths` 不加成員：產物不按 dataset 分目錄，`spec.dataset` 是欄位。沒有 `spec.json` 的目錄是外來的（`status` 記 `foreign=`，任何命令都不碰）。

## 6. CLI 總表（`vcp artifact`，`src/vcp/cli_artifact.py`）

共用 `--json`、`--data-root`；全部以 VERDICT 收尾，exit 0 / 0 / 1；`run_command(context=)` 帶 `kind` / `id`。

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `create --kind K --id I --file PATH… / --file NAME=PATH… [--dataset D] [--plan P] [--seed N] [--param k=v]… [--input name=PATH]… [--supersedes OLD --reason R] [--notes T] [--id-pattern RE]` | 把既有檔案封成產物（`add_file` + `commit`）；給 shell 步驟與人寫的證據用 | §7 的錯誤 |
| `show --kind K --id I` | 印 manifest：spec 欄位、檔案表、`supersedes` 與接替者；唯讀 | `not_found:` / `partial:` → FAIL |
| `verify --kind K --id I` | §8 的檢查 | `mismatch=` / `missing=` / `extra=` 任一 > 0 → FAIL；`unlinked=1` → WARN |
| `lineage --kind K --id I` | 根 → 末端；`--json` 列每個 manifest；唯讀 | 分叉 → WARN `forks=` |
| `status [--kind K]` | 每個 kind：`complete=`、`partial=`（`--json` 附每個的 `opened_at` 與 `failure.json` 摘要）、`unlinked=`、`forks=`、`foreign=`；唯讀 | partial / unlinked / forks > 0 → WARN |
| `relink --kind K --id I` | 補台帳缺列，只從已 commit 的 manifest 推導，冪等 | `appended=0|1` |
| `clean [--kind K] [--older-than N{m\|h\|d}] [--apply]`（預設 `24h`，`0` 可） | 移除半途目錄與 `.tmp`（§8）；不給 `--apply` 只列 | 有候選未套用 → WARN `candidates=` |

程式產生的產物走 Python API（§7），不走 `create`。

## 7. 寫入協定（`src/vcp/artifact/writer.py`）

```python
with ArtifactWriter.create(spec, data_root=root) as art:
    art.write_json("receipt.json", payload)      # 也有 write_text / write_bytes
    art.add_file("weights.pt", src_path)         # 複製進來，邊複製邊算 sha
    out = art.reserve("features.npy")            # 只登記名字，回傳最終路徑；呼叫端用 numpy 寫
    manifest = art.commit()
```

1. **`create(spec)`**：`resolve_inputs(spec)`（帶 path 的 input 現算 sha；path 在 data root 內則相對化；同時給了 sha 而不符 → `IntegrityError("mismatch: input <name> …")`；path 不存在 → `not_found:`）→ 驗 `id_pattern` → `supersedes` 給了就先確認舊產物存在且已 commit（`not_found:` / `partial:`；跑完才被拒絕正是本層要修的事）→ `mkdir(parents)` 建 `<artifacts>/<kind>/`，`os.mkdir` 獨佔建 `<id>/`（`FileExistsError` → `ValidationFailed("exists: artifact <kind>/<id> already exists (complete or partial); pick a new id")`）→ `write_once` 寫 `spec.json`。
2. **`write_json / write_text / write_bytes(name, …)`**：名字過 `check_relative_path` 與保留名（`unsafe_path:` / `reserved_name:`）；已寫過或已 reserve 的名字 → `exists:`；父目錄在產物內建立；經 `write_once`；大小與 sha 在寫入時記錄。**`add_file(name, src)`**：串流複製並雜湊。**`reserve(name) -> Path`**：登記名字、回傳 `<id>/<name>`；呼叫端自己寫（numpy、memmap）；commit 時檔案不在 → `not_found:`。
3. **`commit() -> ArtifactManifest`**：(a) 每個 reserve 的檔存在，串流雜湊；(b) 每個帶 path 的 input 重算 sha，變了 → `IntegrityError("drift: input <name> changed during the job")`；(c) `spec.supersedes` 給了 → `load_manifest` 舊產物（缺 / 半途 → `not_found:` / `partial:`）並 `verify`（`mismatch` / `missing` / `extra` 任一 > 0 → `IntegrityError("mismatch: superseded artifact …")`；`unlinked` 不擋），記其 `manifest.json` 的 sha；(d) 組 manifest（`files` 依名排序、`created_at=stamp()`、`vcp_version=build_string()`），`write_once` 寫 `manifest.json` = **commit 點**；(e) 有 `supersedes` 就 append `SupersessionRow` 到 kind 的 `supersession.jsonl`；(f) 回傳 manifest，writer 關閉——之後任何寫入 → `closed:`。
4. **離開 `with` 而沒 commit**（正常或例外）：目錄原地保留；例外時多寫 `failure.json`（訊息經 `vcp.core.proc.redact`）。writer 沒有任何刪除方法：半途目錄只由 `clean` 移除。

崩潰語意（唯一判定：有 `manifest.json` 才是產物）：

| 崩在哪 | 磁碟上 | 判定 |
|---|---|---|
| open 後、任何檔前 | dir + `spec.json` | partial |
| `write_*` 到一半 | 先前的完整檔 + 一個 `.tmp` | partial；`clean` 移除 |
| `reserve` 的檔寫到一半 | 半個檔 | partial（writer 管不到它的原子性；沒有 manifest 就沒人信它） |
| `manifest.json` 寫到一半 | manifest 的 `.tmp` | partial |
| manifest 之後、台帳列之前 | 完整產物、台帳缺列 | 產物有效；`verify` WARN `unlinked`，`relink` 補列 |

兩個程序搶同一個 id 由 `os.mkdir` 裁決。

## 8. 載入、重用、verify、供給鏈、清理（`src/vcp/artifact/store.py`）

- **`load_manifest(data_root, kind, id)`**：無目錄 → `not_found:`；有目錄無 manifest → `partial:`（訊息分開，consumer 不會把半途當缺席）；manifest 壞 → `ValidationFailed` 帶 `location`。`is_partial(data_root, kind, id) -> bool`。
- **`reuse(spec, data_root, *, verify=False) -> ArtifactManifest | None`**：`resolve_inputs` 後——無目錄 → `None`；半途 → `partial:`；完整 → `manifest.spec` 與現在的 spec 逐欄相等（除 `notes`）才回傳，否則 `IntegrityError("spec_mismatch: … (seed, inputs.plan)")` 列出不同的欄位；`verify=True` 才重雜湊檔案（跑本節的 `verify`，`mismatch` / `missing` / `extra` 任一 > 0 → `IntegrityError mismatch:`）。
- **`verify(data_root, kind, id) -> VerifyResult(mismatch, missing, extra, unlinked)`**：manifest 每個檔重算 sha（不符 → `mismatch`，缺 → `missing`）；目錄裡不在 manifest 的檔（保留名除外，殘留的 `.tmp` 也算）→ `extra`（commit 之後不該有人往裡寫，FAIL；`clean` 移除 `.tmp` 後就乾淨）；`supersedes` 給了但台帳無對應列 → `unlinked`（WARN）；台帳列的 `manifest_sha256` 與現在的 `manifest.json` 不符 → `mismatch`。
- **`lineage(data_root, kind, id) -> list[ArtifactManifest]`**：從 id 沿 `supersedes` 往回到根，再往前掃同 kind 的 manifest 找接替者；**`head(data_root, kind, id) -> list[str]`**：沒被接替的末端，分叉時多個。
- **`relink(data_root, kind, id) -> bool`**：manifest 有 `supersedes` 且台帳無列 → 從 manifest 推導一列 append；已有列 → False。
- **`clean(data_root, kind=None, older_than=timedelta(hours=24), apply=False) -> CleanResult(candidates, removed)`**：候選 = 有 `spec.json`、無 `manifest.json`、`opened_at` 早於門檻的目錄，加上 `artifacts/` 下任何位置的 `.<name>.<nonce>.tmp`（含完整產物內的殘留；只刪 `.tmp`，不碰 manifest 與檔案）；沒有 `spec.json` 的目錄永不列入；`apply=False` 只列。

## 9. 原語與既有寫一次點的加固（`src/vcp/core/atomic.py`）

```python
def write_once(path: Path, data: bytes) -> str          # 回 sha256
def write_once_text(path: Path, text: str) -> str       # utf-8、LF
def write_once_stream(path: Path, chunks: Iterable[bytes]) -> tuple[int, str]  # (bytes, sha256)
```

同目錄 `.<name>.<nonce>.tmp` → 寫 → `flush` + `os.fsync` → 目標已存在則 `ValidationFailed("exists: …")` 並移除暫存 → `os.replace`（POSIX 再 fsync 父目錄；Windows 不支援則略過）；任何失敗都移除暫存。存在檢查與 `os.replace` 之間沒有跨程序互斥——產物層的互斥來自 `os.mkdir`，四個既有點的跨程序競爭不在範圍（§15）。改用它的四個既有點（行為不變，只換原語）：`data/split.save_plan`、`measure/prereg.create_prereg`（yaml）、`fuse/recipes.save_recipe`、`backup/manifest.write_manifest`。不動：`run.yaml` / `train.yaml` / `fuse.json`（換寫留痕）、`anchors.json`（整份原子換寫）、`stage.json`（已經是 tmp 目錄 rename）。

## 10. 錯誤處理與 VERDICT 字彙

| 情境 | 類型 / `reason=` | 狀態 |
|---|---|---|
| id 已存在（完整或半途）；檔名已寫過 | `ValidationFailed` `exists:` | FAIL |
| 產物 / input path / reserve 的檔不存在 | `ValidationFailed` `not_found:` | FAIL |
| 有目錄無 manifest | `ValidationFailed` `partial:` | FAIL |
| 檔名越界 / 保留名 | `ValidationFailed` `unsafe_path:` / `reserved_name:` | FAIL |
| commit 後再寫 | `ValidationFailed` `closed:` | FAIL |
| `id_pattern` 不符欄位；`supersedes` 無 reason；input 沒給 path 也沒給 sha | `ValidationFailed`（pydantic 訊息，與 `load_yaml_model` 同慣例） | FAIL |
| input 的 sha 給錯；verify 檔案不符；台帳與 manifest 不符 | `IntegrityError` `mismatch:` | FAIL |
| commit 時 input 變了 | `IntegrityError` `drift:` | FAIL |
| 重用時 spec 不同 | `IntegrityError` `spec_mismatch:` | FAIL |
| 台帳缺列；分叉；半途目錄存在 | — | WARN |

共用欄位：`kind=` `id=` `files=` `bytes=` `mismatch=` `missing=` `extra=` `unlinked=` `forks=` `complete=` `partial=` `foreign=` `candidates=` `removed=` `appended=`。

## 11. 隱私

manifest / `spec.json` / 台帳只含路徑、sha、大小、時戳、build string 與呼叫端的結構化參數；`failure.json` 的例外訊息經 `redact`；`params` 與 `inputs` 與各層台帳同一條規則——不放憑證；writer 不做憑證偵測（半吊子的偵測比規則更糟）。

## 12. 與其他子專案的介面

- **core**：新 `atomic.py`、`paths.artifacts_root` / `artifact_dir`、`check_relative_path` 搬進 `paths.py`；`build.build_string` 蓋進 manifest。
- **資料 / 量測 / 融合 / 備份層**：各一個寫一次點改用 `write_once`，訊息與行為不變。
- **1b、1c**：`AccessReceipt`、`SourceAudit`、`CodeSnapshot`、`ArtifactAuthorization` 各是一種 kind 的產物，用本層寫；它們的 schema 在各自的 spec。
- **備份層（Wave 2）**：evidence graph 加一條邊，把產物目錄（manifest + files）以 manifest sha 收進清單；本層不改備份層。
- **CLI**：`app.add_typer(artifact_app, name="artifact")`，接在 `data` 之後、`backup` 之前（現有順序：`data` 先，其餘字母序）。
- **文件**：README 新增「不可變產物命令 `vcp artifact`」一節；AGENTS.md / CLAUDE.md 路徑加 `artifacts/<kind>/<id>/`、常用命令加 `create` / `verify` / `status`。

## 13. 測試策略

- **單元**（`tests/unit/artifact/`）：schema（名字、`id_pattern` 逐群組、`supersedes` 與 reason 同進退、input 相對化、檔名規則）；`core/atomic`（目標存在 → `exists:` 且原檔位元組不變；`os.replace` 失敗 → 目標不存在、暫存已移除）；writer（搶 id、`id_pattern` 與 input sha 在 open 擋、檔名規則、`reserve` 沒寫、`drift:`、`supersedes` 三種拒絕、manifest 內容、`closed:`、例外離開的 `failure.json` 含 `<redacted>`、manifest 寫到一半失敗 → partial）；store（`load` 三態、`reuse` 逐欄相等與 `notes` 例外、`verify` 四項各一陽一陰、`lineage` / `head` / 分叉、`relink` 冪等、`clean` 寬限與 `--apply` 與外來目錄不碰）。
- **加固**：四個既有點各一個「第二次寫 → `exists:` 且原檔不變」。
- **CLI**（`tests/unit/test_cli_artifact.py`）：七個命令的 VERDICT 欄位、exit code、`--json`。
- **端到端**（`tests/unit/test_e2e_artifact.py`）：選型 job 劇本——`create` 一個帶 `reserve` npy + receipt → 例外中斷 → `status` 列 partial → `clean --older-than 0 --apply` → 新 id 接替 → `lineage` / `head` → 改檔 → `verify` FAIL → 改 manifest → `mismatch` → 刪台帳列 → `verify` WARN → `relink` → OK；假秘密進 `failure.json` → 兩個 root 掃描零命中。
- **真資料**：`vcp artifact status` 對真 data root 不炸（`artifacts/` 空或有外來目錄）。

## 14. 驗收條件

1. 相同 id 第二次寫入失敗，原檔 hash 不變。
2. commit 前的任何崩潰都不會留下看起來完整的 manifest。
3. supersession 只能指向存在、已 commit 且 hash 相符的產物；分叉可見。
4. `clean` 沒有程式路徑能刪到有 manifest 的產物、台帳或祖先。
5. vcp 自己的四個正式寫一次檔都經同一個原語。
6. 覆蓋率 ≥ 80%、ruff `check` 與 `format --check` 乾淨、每個命令 VERDICT。

## 15. 不在範圍

per-unit / resume 收據與 `mode="resume"`（VCP-029）；content-addressed blob 與 hardlink / reflink 去重、可達性 GC（VCP-028）；kind 登記表、kind 層級的 schema 與位置宣告、plugin API（VCP-031）；跨程序鎖；把換寫留痕的卡改成不可變；RSNA 原型的遷移（Wave 1 第 5 項）；憑證偵測。以上皆為已預留的擴充點，不是設計缺口。

## 16. 補充決定（實作期，Plan 8）

1. `reuse` 的關鍵字參數叫 `check_files`（`verify=` 會遮蔽同模組的 `verify`）。
2. `lineage` 回 `Lineage(chain, successors, heads, forks)`：`heads` 是 id 前向閉包的末端，`forks` 數 chain + successors 裡有 >1 接替者的節點；`head()` = `lineage().heads`。
3. `VerifyResult` 的 `mismatch` / `missing` / `extra` 是檔名清單、`unlinked: bool`；台帳列 sha 不符記成 `mismatch` 裡的 `"manifest.json"`。
4. `clean` 的 `.tmp` 也套 `--older-than`（mtime）；讀不到 `spec.json` 的半途目錄永不列入；移除每個目錄前再查一次 `manifest.json`；暫存在候選目錄之內時不另列。
5. 台帳讀寫沿用 `vcp.measure.ledger.append_row` / `read_rows`；`ledger.row_for(manifest, sha)` 是 writer 與 `relink` 共用的列建構。
6. `load_manifest` 多驗 manifest 裡的 kind / id 等於目錄的（複製來的 manifest → `mismatch:`）。
7. 保留名只在產物根目錄；`.<name>.<8 hex>.tmp` 形式的檔名也是保留名。
8. `ArtifactManifest.files` 必須已依 name 排序（validator）。
9. CLI `--file`：第一個 `=` 之前是名字，沒有就 basename；`--input` 路徑相對 CWD 解析成絕對後才交給 `resolve_inputs`；`create` 搶 id 前先確認每個 `--file` 存在。
10. `status` 的 VERDICT 欄位是各 kind 加總；明細在 human 與 `--json`。`show` 的 `superseded_by=` 只列直接接替者。
11. `write_once` 不建立跨程序互斥；Windows 不 fsync 目錄。
12. 四個既有寫一次點保留各自的存在預檢（訊息、例外類型、`fields` 不變），只換位元組落地方式。
13. 真資料整合測試只呼叫 `scan()`，並斷言 `artifacts/` 樹不變。
14. `artifact verify` 以回傳（不是拋例外）報 FAIL，VERDICT 欄位順序是 `kind= id=` 在 `reason=` 之前（`run_command` 先合併 context；`backup verify` 亦同）；VERDICT 欄位以鍵讀取，順序不是契約。
