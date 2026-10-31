# vcp 稽核 Wave 1c：程式碼快照與產物授權 設計

- 日期：2026-10-31
- 來源：稽核 `docs/audits/2026-09-11-vcp-improvement-audit.md` 的 VCP-004（code inventory 漏列）、VCP-006（benchmark PASS 沒有綁定正式 job）、VCP-007 留給 1c 的「commit 前比對程式碼快照」、稽核 §11 Wave 1 第 1 項的 `CodeSnapshot` / `ArtifactAuthorization`、稽核 §12 release gate 第 10 條（歷史可重現）；RSNA 賽後報告 §8（程式碼盤點兩次在 review 時才發現漏列）。
- 前置：1a 不可變產物（`src/vcp/artifact/`）、1b 收據與來源稽核、VCP-040/042 的證據清單（`EvidenceRef`）、VCP-041 dirty 工作樹（`src/vcp/train/gitstate.py`）、0.16.0（`replace_text`、`own_writes`）。
- 決定：使用者跳過提問。選擇由 spec 作者裁決，集中在 §14；只有使用者能做的決定在 §16（附預設）。只寫設計；實作在課程凍結（2026-12-06）之後，實作期的裁決記在後記 `docs/superpowers/plans/<日期>-vcp-wave1c-followups.md`。
- 版本：0.17.0（MINOR，§9）；1.0.0 的門檻見 §13。

## 1. 問題

讀者拿到一個 run、一個產物或一發提交，今天證明不了：

1. **跑的是哪些程式碼。** `train/env.<n>.json` 的 `git` 只描述 `--cwd` 所在的 repo：HEAD、追蹤檔的 patch、未追蹤檔的路徑（VCP-041 決定 1：不讀內容）。於是：
   - 新增、還沒 `git add` 的模組只留路徑，內容無從證明。RSNA 兩次漏列的就是這類 helper（VCP-004 的實際觸發）；
   - `--cwd` 以外的程式（別的 repo 的 script、以 editable 安裝的專案套件、訓練 venv 裡的 vcp）完全沒記；vcp 自己只有 build string 的 `.dirty`；
   - 沒有紀錄說明盤點是否完整（VCP-004：無法證明完整時應報 `inventory_complete=false`，不能默認 PASS）。
2. **舊紀錄還能不能驗。** commit hash 會變（2026-10 的歷史改寫要靠 commit map 對回），patch 只含追蹤檔的差異；分不出「歷史產物可重現」與「目前 checkout 已漂移」（VCP-004 第三個觸發、稽核 §12 第 10 條）。
3. **產物的程式碼。** `ArtifactWriter` 的 job（特徵、benchmark）沒有程式碼身分；VCP-007 把「commit 前比對程式碼快照」留給 1c。
4. **benchmark 與正式 job 之間沒有綁定。** 50 筆的 benchmark 證明了速度與正確性，正式 job 卻可能換了 dataset、ids、source audit、程式碼、batch 或模型，沒有東西會擋（VCP-006）。
5. **提交的閘門只看資料。** `submit.yaml` 有 `require_provenance`（讀了什麼），沒有對應的程式碼要求。

## 2. 目標、非目標、反目標

做：

- 新產物 `code_snapshot`：一個程式碼根目錄（root）的內容定址快照，保留程式檔的位元組（§4.1）。
- `train run` 每個 attempt 在第一次寫入前掃描所有 root、寫或重用快照、掛進 run 紀錄；結束時重算（§5.1、§5.3）。
- 子程序的 import 追蹤：用 `Session` 的訓練迴圈結束時記下實際 import 的模組，寫成 `code_trace` 產物（§5.2）。
- 程式碼等級 `traced > snapshot > commit > declared`，讀取時算（§5.4）；`submit.yaml` 的 `require_code`（§5.8）。
- `ArtifactWriter` 的保留輸入名 `code_snapshot.<label>` 與 `authorization`（§5.5）。
- 新產物 `authorization` 與 `vcp artifact authorize`；`ArtifactWriter` 與 `train run --authorization` 消耗它（§5.6）。
- 命令群 `vcp code snapshot | verify | diff | export`（§5.7）。
- 備份、provenance 圖、`eval ingest`、`submit stage` 認得新產物（§4.7、§4.8、§5.8）。
- 順手：`vcp.core.build` 的 git 呼叫改用 `gitstate` 的子程序環境（dirty 工作樹後記 §3 第 3 條），`vcp_version` 才不會在 git hook 裡記成別的 repo 的 commit。

不做（非目標）：

- BenchmarkReceipt 的 schema 與 STOP/GO 判斷（VCP-025）：vcp 不判斷 benchmark 過了沒，授權只記錄人的決定與理由。
- `ArtifactWriter` job 的執行環境身分（Python ABI、CUDA、解碼器；VCP-024 `env doctor`）。`train run` 的環境照舊在 `env.<n>.json`。
- 訓練程序讀的非模組檔（資料、預訓練權重）的雜湊：那是收據與 `--evidence` 的事。
- 內容定址的 blob 去重與 GC（VCP-028）、`vcp audit lineage`（VCP-030）、plugin SDK（VCP-031）。
- 不裝 vcp 的 Kaggle kernel 內驗證腳本（§12）。授權的時效、單次使用、純撤銷（§12、§16）。可設定的納入／排除規則。
- 改變子程序的執行方式：不注入 `sitecustomize`、不改 `PYTHONPATH`、不包 `runpy`。

反目標（做到了也算失敗）：

- 把憑證或機密檔複製進 data root 或備份。
- 讓 `train run` 慢到感覺得到、或要人回答問題；快照失敗就擋下訓練（只 WARN；要擋用授權或 `require_code`）。
- 讓舊紀錄讀不動或被改寫；把舊 run 說成比它能證明的更好。
- 讓歷史驗證依賴目前的工作樹。
- 讓訓練程序的 exit code 因追蹤而改變。
- 把比賽專屬的 benchmark 內容寫進 `src/vcp`。

## 3. 建在什麼上面

| 既有 | 位置 | 本 spec 怎麼用 |
|---|---|---|
| 不可變產物、`reuse`、`verify`、supersession | `src/vcp/artifact/` | 三種新 kind 都用 `ArtifactWriter` 寫；撤銷授權 = supersede |
| 內容定址的重用 | `src/vcp/data/source_audit.py` | `code_snapshot` 照同一套：id 由內容決定、同內容重用、壞了 `mismatch:` |
| run 的證據清單 | `src/vcp/data/evidence_ref.py`、`evidence.py` | 新 kind 掛在同一個 `evidence` 清單；備份、完整性、provenance、`--verify` 沿用 |
| dirty 工作樹 | `src/vcp/train/gitstate.py` | 掃 git root 用同一個子程序環境（`_environment`）；`commit` 等級讀它的紀錄 |
| 環境快照 | `src/vcp/train/env.py` | PROBE 多回報 distribution 身分，並提前到第一次寫入前 |
| provenance 等級 | `src/vcp/measure/provenance.py` | 程式碼等級照同一個模式：讀取時算、不存、fused 取成員最低 |
| benchmark 的 run contract | `tests/performance/provenance/adaptive_benchmark.py`（`_source_tree_snapshot`、`CheckpointStore`） | 已證明「allowlist 程式碼的現行位元組 + 契約不符就拒絕續跑」可行；授權是它的通用版 |
| `own_writes` | `src/vcp/train/disk.py` | 寫快照算 vcp 自己的寫入，磁碟滿是 `disk_full:` |

## 4. 資料形態

### 4.1 `code_snapshot` 產物

- 位置 `artifacts/code_snapshot/code-<16 hex>/`，寫一次不改；id 是內容身分的前 16 碼。同內容重用；同 id、不同身分 → `mismatch:` FAIL（比照 source audit）。不做 supersedes：壞了搬走目錄、重跑。
- 一個 root 一份。root 的 label、絕對路徑、commit 不進產物（因機器、因 run 而異），記在 run 端（§4.3）。同一份 vcp 或專案套件在所有 run 之間只存一份。
- 檔案：
  - `snapshot.json`：`{schema_version: 1, rules: 1, identity_sha256, files, bytes, kept_files, kept_bytes}`。不含時戳與版本（`manifest.json` 有），所以同內容在任何機器上位元組相同：授權比對的是它的 sha（§4.6）。
  - `index.jsonl`：每個納入的檔一行 `{"path", "sha256", "bytes", "kept"}`，依 `path` 排序；`path` 是 root 內的相對 posix 路徑。
  - `files/<path>`：`kept: true` 的檔的原始位元組。
- 身分：`identity_sha256 = sha256("vcp-code/1\n" + 每個檔一行 "<sha256> <bytes> <path>\n")`，依 path 排序；`kept` 不進身分。
- 保留：單檔 ≤ 1 MiB、且這個 root 累計 ≤ 64 MiB（依 path 順序累加）才保留位元組，其餘只記 sha。

### 4.2 規則 v1（`vcp.code.rules`，`rules: 1`）

- **跳過，不讀也不列**：任何一段以 `.` 開頭的路徑（`.git`、`.venv`、`.env*`、`.claude`、`.vscode`…）；`__pycache__`、`node_modules`、`site-packages`、`*.egg-info`、含 `pyvenv.cfg` 的目錄；data root 與 configs root 底下的一切（它們由自己的機制釘住）；屬於其他 root 的檔（一個檔只屬於最內層的 root）。
- **git root**：在 root 目錄跑 `git ls-files -z --cached --others --exclude-standard`（只列它的子樹；子程序環境同 `gitstate._environment`）：追蹤檔加「未追蹤且未被忽略」的檔。被忽略的檔永不讀；追蹤但已刪除的不列；submodule 不展開。
- **一般目錄 root**：遞迴走訪，同樣的跳過規則；走訪超過 200,000 個項目 → `too_large`。
- **納入**（副檔名不分大小寫）：`.py .pyi .pyx .pxd .pyd .so .ipynb .c .cc .cpp .cu .cuh .h .hpp .sh .ps1 .bat .cmd .toml .cfg .ini .yaml .yml .json`，以及檔名 `requirements*.txt`、`constraints*.txt`、`*.lock`、`Makefile`、`Dockerfile`。其他（`.md`、圖片、資料）不讀。
- **機密檔名**（比對檔名，不分大小寫）：`*.pem *.key *.p12 *.pfx *.jks *.kdbx id_rsa* id_ed25519* id_ecdsa* kaggle.json rclone.conf pg_service.conf pgpass.conf _netrc credentials* *secret* *service-account* *service_account*` → 不開檔、不雜湊，只把路徑記進 run 端的 `excluded_paths`。
- symlink 不跟隨，算 `excluded`。
- 上限（每個 root）：納入 ≤ 20,000 檔、雜湊總量 ≤ 512 MiB；超過 → 這個 root `too_large`，不寫快照。

### 4.3 run 端的紀錄

`train/env.<n>.json`（`EnvSnapshot`）多三段，都可省、舊檔照讀：

- `code: {rules, roots: [CodeRoot]}`。`CodeRoot`：`label`、`source`（`cwd` / `script` / `editable` / `extra`）、`kind`（`git` / `dir`）、`path`（`store_path`）、`commit`（git root 所在 repo 的 HEAD，或 null）、`snapshot`（id 或 null）、`reason`（null / `too_large` / `unreadable` / `unwritable` / `not_found`）、`files`、`untracked_included`、`excluded`（數量）、`excluded_paths`（≤ 50）。
- `distributions: [{name, version, record_sha256, direct_url}]`：`record_sha256` 是該 distribution 的 `RECORD` 檔的 sha（沒有就 null）；`direct_url` 取自 PEP 610 的 `direct_url.json`：`{url, editable, commit_id}`，`url` 去掉 userinfo、query、fragment 再過 `redact`。
- `identity: {python, packages_sha256, gpus}`：`python` = `<implementation>-<version>-<cache_tag>-<sysconfig platform>`；`packages_sha256` = 非 editable distribution 的 `name==version record_sha256 [commit_id]` 排序後的 sha（editable 的程式碼由 root 涵蓋）；`gpus` = nvidia-smi 的名稱，逗號串接。

label：`cwd`、`script`、`dist-<PEP 503 名稱>`、`root-<n>`（`--code-root` 的順序）。

`EvidenceRef.kind`（`src/vcp/data/evidence_ref.py`）加三個值。這三個 role，以及名稱 `authorization` 與以 `code.`、`code_trace.` 開頭的名稱都保留，`--evidence` 與 `Session.attach_evidence` 不能用（`role_reserved:`，比照 `labels`）：

| kind | name | role | binding | 誰寫 |
|---|---|---|---|---|
| `code_snapshot` | `code.<label>` | `code` | `cli`／`manual` | `train run`／`eval ingest --code` |
| `code_trace` | `code_trace.a<n>.<seq>` | `code_trace` | `session` | 子程序的 `Session`（§5.2） |
| `authorization` | `authorization` | `authorization` | `cli` | `train run --authorization` |

- 掛在 `train.yaml` 與 `run.yaml` 的 `evidence` 清單。同名同 manifest 不再加列（既有的 `add_ref` 規則），所以下一個 attempt 沿用同一份快照時清單不變；每個 attempt 用了哪份快照，以 `env.<n>.json` 的 `code.roots[].snapshot` 為準。VERDICT 的 `evidence=` 與 `labels=` 只算原本的兩種 kind，意思不變。
- `train.log.jsonl`：掛上照既有的 `evidence` 事件記；新的 `note` 是 `key=code_changed`（§5.3）。不加事件種類。

### 4.4 `code_trace` 產物

`artifacts/code_trace/<run>-a<n>-t<seq>/trace.json`：`schema_version`、`run_id`、`attempt`、`executable`、`python`、`roots: [{label, snapshot}]`、`counts: {root, stdlib, distribution, outside, unlisted, changed}`、`outside`（絕對路徑，≤ 50）、`unlisted` 與 `changed`（`<label>:<path>`，各 ≤ 50）、`complete`（`outside + unlisted + changed == 0`）、`taken_at`、`vcp_version`。spec 的 `params` 有 `run`、`attempt`；`inputs` 是各 root 的 `snapshot.json`，provenance 因此連得到快照。

### 4.5 `authorization` 產物

`artifacts/authorization/<id>/authorization.json`（id 由人取，`validate_name`）：

```yaml
schema_version: 1
consumer: artifact               # 或 train
target_kind: features            # consumer=artifact 必填；train 為 null
basis: artifact:benchmark/b-50   # 或 run:<id>
claims: {dataset: demo, "input:code_snapshot.cwd": "…", "param:batch": "8", …}  # 依名稱排序，值都是字串
claims_sha256: "…"               # sha256_json(claims)
freed: [param:limit, input:ids]  # 依據有、刻意不綁的
reason: "50 筆 benchmark：速度與幾何都過"
```

- spec：`params` = `{consumer, target_kind, basis}`；依據是產物時，`inputs` 有 `basis`（它的 `manifest.json`，釘 sha）；`dataset` / `plan_id` 取自 claims（有的話）。
- 可以 `supersedes`；被取代的授權不能再用（§5.6）。

### 4.6 claims 字彙

| claim | `consumer: artifact`（取自 job 的 `ArtifactSpec`） | `consumer: train`（第一次寫入前算出） |
|---|---|---|
| `dataset`、`plan_id`、`seed` | 同名欄位 | `--dataset`、`--plan`、`--seed` |
| `param:<k>` | `params[k]`，任何鍵 | 只有 `param:samples_hash`、`param:trained_on`（排序、逗號）、`param:config_sha256`（`config_hash`）、`param:command_sha256`（`sha256_json(command)`） |
| `input:<name>` | 每個 input 的 sha（`authorization` 本身除外） | 只有 `input:plan`、`input:code_snapshot.<label>`、`input:evidence.<name>`（來源 sha）、`input:labels.<id>`（manifest sha） |
| `env:python`、`env:packages`、`env:gpus` | 沒有 | `identity` 的三欄（§4.3） |

- 慣例的 input 名（建議、不強制）：`card`、`plan`、`ids`（job 要處理的 id 清單檔）、`source_audit`、`model`、`code_snapshot.<label>`。這對應 VCP-006 列的 dataset card、plan、allowed ids、source audit、code、model；`param:*` 是 parameters；benchmark receipt 是 `basis`。
- `code_snapshot.<label>` 的值是 `snapshot.json` 的 sha：內容相同就相同，跨機器也一樣（§4.1）。
- 依據的 claims：產物依據用左欄算；run 依據用右欄，取它最後一個 finished attempt（沒有就最後一個）的紀錄。

### 4.7 提交

- `submit.yaml`：`require_code: declared | commit | snapshot | traced`，預設 `declared`，等於預設時不寫出（比照 `selection_warn_hours`）。
- `stage.json`：`Staged.code`（stage 當下算的等級，比照 `provenance`）；kernel 的 `Artifact.code_snapshot`（`--code` 給的快照 id）。None 時不寫出。

### 4.8 備份與 provenance

- 備份 role（`src/vcp/backup/schema.py`）：`code_snapshot`（tier 2）、`code_trace`（tier 1）、`authorization`（tier 1）、`authorization_basis`（tier 2）。`walk_run` 照 `_artifact(ref)` 收新 kind；`authorization` ref 另收它的依據：產物依據的 `manifest.json` 與檔案（`authorization_basis`），run 依據走 `walk_run`。`walk_submission` 收 kernel 的 `Artifact.code_snapshot`。`authorization`、`code_trace` 進 `CARD_ROLES`。
- 完整性（`src/vcp/backup/completeness.py`）不用改：清單建立前掛上的 ref 本來就要在清單裡。
- provenance（`src/vcp/provenance/graph.py`）：新 kind 是一般 artifact entity；快照、授權、依據之間的邊由既有的 `inputs` 路徑連出；run entity 多 `code_grade` 屬性；`_link_artifact_runs` 跳過 `code_trace`（經 run 的清單連）；run 依據 → 授權加 `PRODUCED_BY {input: basis}`；kernel 的快照 → submission 加 `CONSUMED_BY`。渲染不用改。

## 5. 行為

### 5.1 `train run` 的順序（`src/vcp/train/run.py`）

新選項：`--code-root DIR`（可重複）、`--authorization ID`。

1. 既有的輸入檢查，到 `check_names` 為止。
2. **PROBE**（唯讀；從原本的第 6 步提前）：多回報 `distributions` 與 `identity` 的欄位。PROBE 失敗 → ABORT，一個檔都沒寫（以前會留下 `running` 的 attempt）。
3. **子程序的 vcp**：給了 `--venv`、它的 distributions 裡有 `vcp`、而 `MAJOR.MINOR` 跟 wrapper 不同 → FAIL `vcp_mismatch:`（`child_vcp=`）。0.16 以前的 `Session` 讀不動 0.17 的 `train.yaml`，會到第一次登記 checkpoint 時才當掉。
4. **規劃 root**：
   - `cwd`：`--cwd` 所在 repo 的頂層；不在 git 裡就是 `--cwd` 本身；
   - `script`：命令裡第一個以 `.py` 結尾、存在、又不在其他 root 裡的檔：它所在 repo 的頂層，不在 git 裡就是它的目錄；
   - `editable`：PROBE 回報 `dir_info.editable` 為真的每個目錄，包括訓練 venv 裡的 vcp；
   - `extra`：`--code-root`。
   同一個路徑只留第一個；巢狀照「最內層」分檔（§4.2）。
5. **掃描**（唯讀、記憶體內）：每個 root 照 §4.2 算 index 與身分。`too_large` / `unreadable` 只標在那個 root。
6. **授權**（給了 `--authorization` 才做）：§5.6 的消耗檢查；不符 → FAIL `unauthorized:`，一個檔都沒寫。授權綁了某個 root 的快照、而那個 root 在第 5 步失敗 → 那個 claim 缺 → 同樣 `unauthorized:`。
7. 0.16 的磁碟檢查（`low_disk:`）。
8. 從這裡起在 `own_writes()` 裡：寫或重用每個 root 的快照。複製時重算的 sha 跟第 5 步不同 → FAIL `drift: code changed while it was being snapshotted`（run 的檔還沒寫；留下的半途產物由 `artifact clean` 清）。寫不進（例如路徑太長）→ 那個 root `unwritable`。
9. 既有的第一次寫入（`run.yaml`、`train.yaml`、`started`）；`env.<n>.json` 帶 §4.3 的三段；接著掛 `code.<label>` 與 `authorization` ref，跟 `--evidence` 同一處。
10. 子程序；之後是 §5.3 的結束比對。

- 任一 root 沒有快照 → WARN `code_snapshot=partial`（`code_missing=<labels>`）；一個都沒有 → `code_snapshot=none`，WARN。訓練照跑。
- `--resume` 的每個 attempt 各做一次；`--authorization` 只對該次呼叫有效。
- 速度：掃描只讀規則納入的檔；同內容重用時不複製。目標是一般比賽 repo（數千個檔、數十 MB）加訓練 venv 的 vcp 在幾秒內完成；上限（§4.2）擋住最壞情況。

### 5.2 子程序的 import 追蹤（`vcp.code.trace`）

- 何時登記：在 `vcp train run` 底下（`VCP_RUN_ID` 對得上），一個程序第一次呼叫 `Session.current()`（`MaterializedReader` 也會呼叫）時登記一個 `atexit`，同一程序只登記一次。
- 不登記：`VCP_CODE_TRACE=0`；`multiprocessing.parent_process()` 不是 None（DataLoader worker）；`RANK` 有值且不是 `0`（torchrun 的其他 rank，避免同時改寫 `train.yaml`）。`atexit` 再檢查 pid 等於登記時的 pid（fork 出來的程序不做）。
- 結束時：從 `train.yaml` 找到這個 attempt 的 `env.<n>.json`，取 roots 與快照的 index；把 `sys.modules` 每個有 `__file__` 的模組（加 `__main__`）依路徑分類：
  - 在某個 root 底下：在 index 裡、sha 相同 → `root`；sha 不同或檔不見 → `changed`；不在 index（被忽略、之後才建、只有 `.pyc`）→ `unlisted`；
  - 在 stdlib（`sysconfig` 的 `stdlib` / `platstdlib`）→ `stdlib`；在 site-packages（`purelib` / `platlib`、`site` 的目錄）→ `distribution`；
  - 其他（含 zip 裡的模組）→ `outside`。
- 只雜湊落在 root 底下的檔（通常幾百個小檔）。寫 `code_trace` 產物、掛 ref、append `evidence` 事件；`ArtifactWriter` 撞到 id 就 seq + 1 重試。
- **永不改變結果**：整段包在 `try` 裡；失敗就不寫，stderr 印一行 `[vcp] code trace skipped: <原因>`；exit code 不變。

### 5.3 結束時的比對

- 位置同 `git_changed`（子程序結束、`finished` 事件之後）。重算每個有快照的 root 在 index 裡列過的檔（不找新檔）。
- 有不同或不見的 → `note`：`key=code_changed`、`value=<n>`、`paths`（≤ 5，`<label>:<path>`）、`roots`；WARN `code_changed=<n>`。
- 讀這個 attempt 掛上的 trace：VERDICT `code_trace=complete | incomplete | none`；`incomplete` → WARN，帶 `code_gaps=<outside+unlisted+changed>`。

### 5.4 程式碼等級（`vcp.code.grade`，讀取時算，不存）

每個 attempt：

| 等級 | 條件 |
|---|---|
| `traced` | 「快照齊全」（這個 attempt 的 `env.<n>.json` 每個 root 都有快照 id，run 的清單裡都有指向該 id 的完好 ref，`ref_intact`），加上這個 attempt 至少一份 trace、每份都 `complete` 且完好。`code_changed` 不影響它：trace 已對被 import 的檔做過比對 |
| `snapshot` | 快照齊全，而且沒有這個 attempt 的 `code_changed` note |
| `commit` | `env.<n>.json` 的 `git` 有 commit、沒有這個 attempt 的 `git_changed` note，而且：有 `status_sha256`（0.11 起）時 `modified == 0` 或有 `diff_sha256`；沒有（0.11 以前）時 `dirty` 為 false |
| `declared` | 其他 |

- run 的等級 = 每個 attempt 的最低（resume 的權重來自每一個 attempt）。沒有 `train.yaml` 的 run（`eval ingest`）：有 `code.<label>` ref 且都完好 → `snapshot`，否則 `declared`。fused run：成員的最低（比照 `measure/provenance.py` 的 `_fused`）。
- vcp 從此讀回 `env.json`，只讀 `git` 與 `code` 兩段、容錯解析；讀不動 → 那個 attempt `declared`。
- 跟 provenance 等級一樣會往下掉（產物被刪、被改）；`train status --verify` 以 `broken_refs` 檢查新的 ref。

### 5.5 `ArtifactWriter` 的保留輸入（`src/vcp/artifact/writer.py`）

- `code_snapshot.<label>`：路徑必須是 `artifacts/code_snapshot/<id>/snapshot.json`，產物必須完好（`verify`）；否則 `not_found:` / `mismatch:`。
- `authorization`：路徑必須是 `artifacts/authorization/<id>/manifest.json`；做 §5.6 的消耗檢查。
- 兩者在 `create` 時、`mkdir` 之前檢查（被拒時什麼都不留），`commit` 時再檢查一次。
- **同程序的追蹤**：快照由 `vcp.code.process.snapshot_inputs(data_root, extra_roots=())` 在同一個程序取得時（root 規劃同 §5.1，`cwd` 是 `Path.cwd()` 所在 repo，`script` 是 `__main__` 的檔；任一 root `too_large` → `too_large:`），writer 記得 root 的路徑，在 `create` 與 `commit` 都對 `sys.modules` 做 §5.2 的分類：
  - `outside` 或 `unlisted` → FAIL `code_outside:`（列前 5 個路徑，訊息說加 `extra_roots`）；
  - 被 import 的檔 `changed` → `IntegrityError` `drift:`；
  - 沒被 import 的檔改了 → 不影響（VCP-004 驗收第 2 條）。
- 別的程序取得的快照（例如 `vcp artifact create --input code_snapshot.cwd=PATH`）只做靜態檢查：產物完好。
- `manifest.json` 與 `ArtifactSpec` 的形狀不變：程式碼與授權都是 `inputs`，`reuse` 的比對自然包含它們。授權的 input 由 `vcp.artifact.authorization.authorization_input(data_root, id)` 產生。

### 5.6 授權：建立與消耗

建立：`vcp artifact authorize`（`src/vcp/artifact/authorization.py`；run 依據的 claims 在 `src/vcp/train/authorize.py`，產物層不 import 訓練層）。

1. 依據要存在、完好、沒有被 supersede（`superseded:`）。
2. 用依據的種類算 claims（§4.6），拿掉 `--free NAME`，加上或覆寫 `--set NAME=VALUE`（`NAME=@PATH` 取檔案的 sha）。
3. 每個 claim 名稱都要在消費者的字彙裡，否則 FAIL `unbindable:`（列名稱，提示 `--free`）；claims 不能是空的（`invalid:`）。
4. 寫 `authorization` 產物。

消耗：`ArtifactWriter` 與 `train run` 共用 `consume(authorization, consumer, target_kind, claims)`：

1. 授權完好、沒有被 supersede，否則 `superseded:`。
2. 依據：產物依據完好、manifest sha 等於釘住的、沒有被 supersede；run 依據的 `run.yaml` 存在。
3. 消費者種類與 `target_kind` 相符。
4. 授權的每個 claim，消費者都有、值相同；消費者多出來的 claim 不管。不符 → FAIL `unauthorized:`，`differs=<名稱>`（訊息附兩邊前 12 碼）。

- 可以重複使用：每個消耗者在自己的紀錄裡釘住授權（產物：`authorization` input；run：`authorization` ref），provenance 圖列得出所有使用者。
- VCP-006 的 `expires_on_change` 就是比對本身：綁住的任何一項不同就不能用。撤銷 = 用新的授權 supersede 它。
- `train run` 只在開始時比對；中途的程式碼變化由 `code_changed` 與等級反映，不回頭作廢。

### 5.7 `vcp code`（`src/vcp/cli_code.py`）與驗證

- `vcp code snapshot --root DIR …`：寫或重用快照，依序印每個 root 的 id。給 `train run` 以外的地方用（kernel 的 bundle、外部訓練、benchmark）。`too_large` → FAIL `too_large:`（這裡是使用者明要的快照）。
- `vcp code verify --id ID`：產物完好、index 與保留的檔一致、身分重算相同 → OK；否則 FAIL `mismatch:`。**不看目前的工作樹**：程式碼漂移了，舊快照照樣驗得過（稽核 §12 第 10 條）。
- `vcp code verify --dir DIR`：驗 `export` 出來的目錄：`DIR/.vcp-code/` 的 index 裡每個檔都在、sha 相同；不需要 git，也不需要原 repo。缺或不符 → FAIL `mismatch:`；index 以外、符合規則的檔 → WARN `extra=`。
- `vcp code diff --id ID --root DIR` 或 `--run R [--attempt N]`：拿快照跟目前的檔比（同一套規則）；`changed=` `added=` `removed=`，任一 > 0 → WARN。唯讀。這是「目前 checkout 漂移了沒」，跟 `verify` 分開。
- `vcp code export --id ID --out DIR`：保留的檔寫到 `DIR/<path>`，index 與 `snapshot.json` 寫到 `DIR/.vcp-code/`。`DIR` 已存在且非空 → `exists:`；在 data root 裡 → `invalid:`；沒保留位元組的檔 → WARN `not_kept=`。

驗證一覽：`artifact verify`（任何 kind，逐檔 sha）；`code verify`（加 index 與身分）；`train status --verify`（run 的每個 ref 完好、manifest sha 等於釘住的）；`backup verify`（新 role 的檔，含完整性）；`ArtifactWriter.commit`（§5.5）；provenance rebuild（artifact 驗不過 → broken entity）。

### 5.8 讀取端與提交

- `train status`：VERDICT 一律帶 `code=`；`--json` 有每個 attempt 的等級與 root。
- `eval status`：每個 run 的 `code`（文字與 `--json`）；VERDICT 不變。
- `eval ingest --code LABEL=ID`（可重複）：掛 `code.<label>`（binding `manual`）；快照不存在或壞了 → `not_found:` / `mismatch:`；VERDICT 帶 `code=`。
- `submit stage`（`src/vcp/submit/stage.py`、`kernel.py`）：
  - 等級 = eval run、test run（有的話）、每個 weights run 的最低；VERDICT 帶 `code=`；
  - candidate 低於 `require_code` → FAIL `code_required:`（`run=`、`code=`），比照 `provenance_required:`；baseline 與 probe 的待遇照 `require_provenance`（probe 不擋，寫進 pairing checks `code:<run>=<grade>`）；
  - `--code ID` 只給 kernel（file 給了 → `invalid:`），快照要完好；`require_code` 是 `snapshot` 以上時，kernel candidate 沒給 → `code_required:`。
- `submit final`：重讀等級，不夠的 `why=code_required`；`final` 列的形狀不變。

## 6. VERDICT 與字彙

| 命令 | 變更 |
|---|---|
| `train run` | 新選項 `--code-root`、`--authorization`；一律帶 `code=` `code_snapshot=ok\|partial\|none` `code_trace=complete\|incomplete\|none`；`partial`/`none`、`incomplete`（`code_gaps=`）、`code_changed=<n>` → WARN；`authorization=<id>`（有給時）；FAIL `unauthorized:`（`authorization=` `differs=`）、`superseded:`、`vcp_mismatch:`（`child_vcp=`）、`drift:` |
| `train status` | `code=`（一律） |
| `eval ingest` | 新選項 `--code LABEL=ID`；`code=` |
| `submit stage` | 新選項 `--code ID`（kernel）；`code=`；FAIL `code_required:` |
| `submit final` | 表的 `why=code_required` |
| `code snapshot`（新） | `roots=` `ids=` `files=` `kept=` `reused=`；FAIL `too_large:`、`not_found:` |
| `code verify`（新） | `id=`／`dir=` `files=` `kept=`；FAIL `mismatch:`（`mismatch=` `missing=`）；WARN `extra=` |
| `code diff`（新） | `changed=` `added=` `removed=`（> 0 WARN） |
| `code export`（新） | `files=` `exported=` `not_kept=`（> 0 WARN）；FAIL `exists:`、`invalid:` |
| `artifact authorize`（新） | `id=` `basis=` `for=` `claims=` `freed=`；FAIL `unbindable:`、`superseded:`、`not_found:`、`exists:`、`invalid:` |
| `artifact create` | 保留輸入的 FAIL：`unauthorized:`、`superseded:`、`code_outside:`、`mismatch:` |

- 新 `reason=` 字彙：`unauthorized:`、`superseded:`、`unbindable:`、`code_outside:`、`code_required:`、`too_large:`、`vcp_mismatch:`。都是 FAIL（exit 1）；ABORT（exit 2）只留給環境（PROBE 失敗、磁碟滿）。
- `run_command(context=)`：`code *` 帶 `id` / `root` / `run`；`authorize` 帶 `id`、`basis`。命令永不提問；`--json` 時結果到 stdout、VERDICT 到 stderr。
- 追蹤（§5.2）不經 CLI、沒有 VERDICT；它的結果出現在 `train run` 的 `code_trace=`。

## 7. 安全邊界

- vcp 不讀：機密檔名（§4.2）、任何以 `.` 開頭的路徑（含 `.env*`、`.git-credentials`、`.netrc`、`.pgpass`）、被 git 忽略的檔、root 以外的檔、環境變數（PROBE 不印、trace 不記）。平台與 rclone 的憑證照舊不碰。
- vcp 記下：相對路徑、大小、sha256，以及符合規則、未超過上限的程式檔位元組。
- **未追蹤的檔**：刻意偏離 VCP-041 決定 1——「未追蹤但未被忽略」的程式檔會被讀、會保留，因為漏列的正是這種檔。被 `.gitignore` 忽略的照舊不讀。
- **程式碼裡寫死的機密**：vcp 不掃內容（`redact` 的長字串規則會把每個 sha 常數都當成機密），它會跟程式碼一起進 data root 與備份（tier 2，含異地），風險同 git 與 `git.<n>.patch`。文件寫明：機密放環境變數或憑證檔，vcp 不讀它們。
- URL：`direct_url` 去掉 userinfo、query、fragment 再過 `redact`。
- `export` 不寫進 data root、不覆寫既有內容；沒有任何命令把快照寫回工作樹。

## 8. 相容性與升級

- **舊紀錄在 0.17**：全部照讀、不改寫。等級照 §5.4 算：0.11 以後的訓練 run 多半是 `commit`；更早、或 dirty 而沒有 patch 的是 `declared`；沒有 `train.yaml` 的 run 是 `declared`。舊產物裡剛好叫 `code_snapshot.*` / `authorization` 的輸入只在新寫入時檢查，讀取不管。不補快照：補了也不是當時跑的程式碼。
- **新紀錄在舊 vcp**（0.16 以前讀到會 FAIL）：
  - `train.yaml` / `run.yaml` 的 `evidence` 有新 kind（每個 0.17 的 `train run` 都會有）；
  - 備份清單的新 role；`stage.json` 的 `code` / `code_snapshot`；寫了 `require_code` 的 `submit.yaml`（`extra_forbidden`，這正是要的）。
  - `env.<n>.json` 的新段落舊 vcp 不讀；三種新產物在舊 vcp 眼中是一般產物，`artifact verify` 照驗。
- **升級順序**：讀同一個 data root 的每個 checkout 與每個訓練 venv（editable 的 vcp）都先升到 0.17，再跑第一個 0.17 的 `train run`。訓練 venv 沒升 → §5.1 第 3 步的 `vcp_mismatch:`。
- **provenance 索引**：run entity 多了 `code_grade`，`sync` 會當成改寫而拒絕 → 每個 checkout `vcp provenance rebuild` 一次（SQLite 與 PostgreSQL 都是）。
- **位元組不變的**：沒有新 ref 的 `TrainRecord` / `RunCard`、`require_code: declared` 的 profile、沒有 `code` 的 `stage.json` 都跟 0.16 逐位元組相同（golden 測試）。

## 9. 版本

0.17.0（MINOR），依 CHANGELOG「版本規則」：三種新產物 kind；寫進紀錄的新內容（`evidence` 的新 kind、`env.<n>.json` 三段、`stage.json` 兩個欄位、`code_changed` note）；新命令群與選項、VERDICT 欄位、七個 `reason=` 字；`submit.yaml` 新鍵；`ArtifactWriter` 對兩個保留輸入名的新規則；PROBE 提前讓它的失敗變成不寫檔的 ABORT。`vcp.core.build` 的子程序環境是 PATCH 等級，隨本版發出。發版時 plugin 的版本跟著改。1.0.0 另發（§13、§16）。

## 10. 測試（先寫、先紅）

全部用 `tmp_path`，不碰真的 data root；git 用 VCP-041 的 `isolated_git` fixture，找不到 git 就 skip。Windows 與 Linux 的 CI 都要過（只在 Windows 跑過會漏掉 POSIX 的失敗）。

### 10.1 規則與掃描（`tests/unit/code/`）

- 規則：納入的副檔名與檔名；`.` 開頭的段、venv、`__pycache__`、data root、configs root 被跳過；機密檔名**從未被開啟**（monkeypatch `open` / `Path.open`，碰到就丟例外）；symlink 不跟隨。
- git root：追蹤檔與「未追蹤未忽略」的檔都在；被忽略的檔從未被開啟；子目錄 root 只列子樹；巢狀 root 分到最內層。
- 上限：> 1 MiB 不保留；累計 64 MiB 之後不保留；檔數與雜湊總量超過 → `too_large`；`kept` 的選法可重現。
- 身分：同內容在兩個絕對路徑 → 同 id、`snapshot.json` 位元組相同；改一個 `.py` → 新 id；改 `README.md` → 同 id；重用不寫新檔；同 id 不同身分 → `mismatch:`。
- root 規劃：`cwd`、`script`（命令裡的 `.py` 在別的 repo）、editable（假的 `direct_url.json`，userinfo 被去掉、過 `redact`）、`--code-root`；重複路徑只留一個。

### 10.2 `train run`

- 乾淨 repo、假 child 不用 `Session` → `code_snapshot=ok`、`code=snapshot`、`code_trace=none`；第二個 attempt 重用同一個快照。
- 新增、未追蹤的 helper 被 import → 它在 index 裡、位元組有保留（VCP-004 的實際觸發）。
- root `too_large` → WARN `partial`、訓練照跑；同樣情況而授權綁了那個 root → FAIL `unauthorized:`、沒有檔。
- child 中途改了列過的檔 → `code_changed=1` WARN、有 `note`、等級正確下降。
- 訓練 venv 的 vcp MINOR 不同 → FAIL `vcp_mismatch:`、沒有檔；PROBE 失敗 → ABORT、沒有檔。
- 掃描與複製之間檔案被改 → FAIL `drift:`、run 沒有檔。
- repo 裡未忽略的機密檔 → 沒被讀，路徑在 `excluded_paths`。

### 10.3 追蹤

- child 用 `Session`、只 import root 內的模組 → `code_trace=complete`、`code=traced`；import root 外的模組（`sys.path` 指向 tmp）→ `incomplete`、`code_gaps=1`、WARN。
- 分類單元測試：`root` / `changed` / `unlisted` / `stdlib` / `distribution` / `outside`（假的 `sys.modules`）。
- 不登記：`VCP_CODE_TRACE=0`、worker（`parent_process` 不是 None）、`RANK=1`；pid 不同時 atexit 不做事。
- trace 本身丟例外 → child 的 exit code 不變、stderr 一行。

### 10.4 等級

- §5.4 的每一列；0.11 以前的 `env.json`（只有 `commit`、`dirty`）；0.16 的 `env.json`（沒有 `code`）→ `commit`；`git_changed` note → `declared`；快照產物被刪 → 降級；fused 取最低；ingest 的 run。

### 10.5 產物 job 與授權

- writer：保留輸入的路徑形狀錯、產物壞 → 被拒且**沒有目錄**；同程序：被 import 的檔在 job 中改了 → commit `drift:`；沒被 import 的檔改了 → commit 成功（VCP-004 驗收第 1、2 條）；root 外的模組 → `create` 就 `code_outside:`；CLI 的 `artifact create` 只做靜態檢查。
- authorize：產物依據 → claims 正確；`--free`、`--set`、`@PATH`；`unbindable:`；依據被 supersede → `superseded:`；空 claims → `invalid:`。
- 消耗（VCP-006 的驗收）：benchmark → authorize → 正式 job 成功；改 batch（`param:`）、換程式碼、換 ids、換 source audit、換 model → 各自 `unauthorized:` 並列出那一項；授權被 supersede → `superseded:`；同一張授權給兩個 job → 都成功、都釘住它；`train run --authorization`（run 依據）同一組。

### 10.6 讀取端、提交、備份、provenance、CLI、相容

- `require_code`：預設不寫出；stage FAIL `code_required:`、probe 只記；`final` 的 `why=code_required`；kernel `--code` 記進 `stage.json`，file 給了 `invalid:`。
- 備份：新 role 與 tier；`walk_run` 收快照、trace、授權與依據；`manifest_gaps` 認得新 ref；`backup verify` OK。
- provenance：新 entity 與邊、`code_grade`；rebuild 兩次 graph hash 相同。
- `vcp code`：四個命令的 VERDICT 與 `--json`；`verify --dir` 在 PATH 上沒有 git 時照驗；`export` 拒絕 data root 與非空目錄；改了工作樹之後 `diff` WARN、而 `verify --id` 仍 OK（稽核 §12 release gate 第 10 條）。
- 相容：0.16 的 `train.yaml` / `run.yaml` / `env.json` / `stage.json` / `submit.yaml` golden 照讀；沒有新內容時寫出的位元組不變。
- `vcp.core.build`：設了 `GIT_DIR` 指向別的 repo 時，`vcp_version` 仍是 vcp 自己的 commit。

### 10.7 回歸門檻與範例

- `tests/unit/test_regression_gate.py` 加一列 `0.17.0`：VCP-004 驗收四條、VCP-006 消耗的拒絕、稽核 §12 第 10 條、追蹤不改 exit code、機密檔不被讀。
- `examples/quickstart.py`（CI 會跑）加一段：`train run` 之後 `code=snapshot`，再 `vcp code verify`。
- 選配的整合測試（`VCP_REALDATA_ROOT` 缺席就 skip）：對一份真實 data root 的每個 run 算等級，不得有例外。

## 11. 文件

- `CLAUDE.md` 與 `AGENTS.md`（整份複製、`diff` 無輸出）：「路徑」加 `artifacts/code_snapshot|code_trace|authorization`、保留輸入名、未追蹤程式檔會被讀與機密規則；「常用命令」加 `vcp code …`、`vcp artifact authorize`、`train run --code-root / --authorization`。
- `docs/reference/cli.md`：新命令與改了的命令；`README.md`、`README.zh-TW.md` 的功能清單各一行。
- skills（改 `.claude/skills/`，再 `cp -r` 到 `.agents/skills/`）：`vcp-train-submit-backup`（等級、`code_*` 的 WARN、`require_code`、kernel `--code`）、`vcp-running-contests`（正式訓練前的 benchmark → authorize）、`vcp-orientation`（地圖與版本沿革）、`vcp-provenance-graph`（新 kind）、`vcp-release-and-environments`（訓練 venv 的 vcp 要同 MINOR、升級順序）。
- spec 修訂：訓練層、不可變產物（保留輸入名）、提交治理（`require_code`）、備份（role）、dirty 工作樹（決定 1 對程式碼快照的偏離），各加一條指回本 spec。
- 稽核文件：VCP-004、VCP-006 的狀態行與第 1 節表格；VCP-007 狀態行的「等 Wave 1c」。dirty 工作樹後記 §3 第 3 條標為已處理。
- 後記 `docs/superpowers/plans/<日期>-vcp-wave1c-followups.md`；`HANDOVER.md`、`CODEX_PROMPT.md`；發版時的 `CHANGELOG.md`。

## 12. 已知限制

- **只追得到 Python 模組。** 不用 `Session` 的子程序、非 Python 程式、子程序再起的程序、DataLoader worker、非 0 rank 都沒有 trace，最多 `snapshot`；被 `os._exit` 或強制結束的程序也沒有。
- **「改過」是事後比對。** 結束時與 trace 時重算的是當下的位元組；改了又改回去看不到。
- **跨作業系統的身分。** Windows 上 `core.autocrlf=true` 的 checkout 是 CRLF，跟 Linux 上同一個 commit 的身分不同；建議 `.gitattributes` 設 `eol=lf`。各機器的全域 git exclude 也會讓列出的檔不同。
- **Windows 的路徑長度與大小寫**：保留位元組時路徑超過 260 字元、或只差大小寫的兩個檔，那個 root 記 `unwritable`。
- **儲存**：每種不同的 root 內容存一份完整副本，跨快照不去重（VCP-028）。
- **重用的粒度是 root。** 同一個 root 裡沒被 import 的程式檔改了，新 spec 的快照就不同，`reuse` 報 `spec_mismatch:`；舊產物本身照樣有效。精確到「被 import 的檔」的重用要把 trace 寫進產物，留到之後。
- **機密只靠檔名、`.` 開頭的規則與 `.gitignore`**（§7）。submodule 不展開，要涵蓋就用 `--code-root`。
- **產物 job 的環境**沒有身分（VCP-024）；別的程序取得的快照只做靜態檢查，manifest 也不記檢查的強度。
- **授權**不判斷 benchmark 的內容；沒有時效、沒有次數上限；只能用 supersede 撤銷；`ids` 之類的慣例輸入是 job 自己宣告的，實際讀了什麼仍要看收據；`train run` 只在開始時比對。
- **Kaggle kernel 裡驗證**要裝 vcp 跑 `code verify --dir`；`index.jsonl` 是純 JSONL 加 sha256，日後可補一個只用標準函式庫的腳本。
- 等級會隨產物損壞往下掉；provenance 索引要 rebuild 才跟得上。

## 13. 1.0.0 的門檻

稽核驗收與本 spec 的對應：

| 稽核 | 本 spec |
|---|---|
| VCP-004 驗收 1：改了被 import 的模組，重用被拒 | §5.5 commit 的 `drift:`；`reuse` 比對 `code_snapshot.*` input |
| VCP-004 驗收 2：改了沒被引用的檔，產物不失效 | 已 commit 的產物不因工作樹的任何變化失效（§5.7 `verify`）；文件與資料不進快照（§4.2）；job 中途只看被 import 的檔（§5.5）。重用的粒度見 §12 |
| VCP-004 驗收 3：dirty 工作樹留 diff hash 或拒絕 | VCP-041（0.11.0）的 `diff_sha256` 與 `--require-clean`；快照再保留位元組 |
| VCP-004 驗收 4：沒有原 repo 也能驗 | §5.7 `export` + `verify --dir` |
| VCP-006：任一欄位不一致直接拒絕 | §5.6 `unauthorized:` |
| 稽核 §12 release gate 第 10 條：歷史可驗、不誤判為目前一致 | §5.7 `verify` 與 `diff` 分開 |

Wave 1（1a、1b、1c）算完成，要同時成立：

1. 本 spec 的實作以 0.17.0 發出；稽核文件的 VCP-004、VCP-006 標為已實作。
2. 回歸門檻有 §10.7 那一列，上表每一行都有具名測試；Windows 與 Linux CI 都綠。
3. 凍結前的契約項（`CODEX_PROMPT.md` 第 3 項：各後記最後一節會改寫入內容、VERDICT、`reason=` 或 exit 類別的待辦，含本 spec 的後記）做完，或逐項寫明留到 1.0 之後。
4. 合成的端到端流程在 CI 裡跑：quickstart 的 `code verify`；benchmark → authorize → 正式 job → `stage`（`require_code: snapshot`）。
5. 1.0.0 的 CHANGELOG 條目寫明從此承諾的契約（產物佈局與 manifest、紀錄與台帳的形狀、VERDICT 欄位、`reason=` 字彙、exit code），之後改它們就是 MAJOR。

不在門檻內：稽核 §11 Wave 1 的第 4、5 項（已定排在 1.0 之後）；稽核 §12 release gate 的第 7、11、12 條屬 Wave 2、3。

## 14. spec 作者的裁決

1. 一個 root 一份快照、內容定址：同一份 vcp 或專案套件在所有 run 之間只存一份；label 與路徑留在 run 端。
2. `snapshot.json` 不含時戳與版本：同內容跨機器位元組相同，授權可以比它的 sha。
3. 保留程式檔的位元組（有上限）：沒有原 repo、commit 被改寫時也能驗與還原（§16 第 1 項）。
4. 讀「未追蹤但未被忽略」的程式檔：漏列的正是這種檔；被忽略的照舊不讀。
5. 規則 v1 固定、不給設定：範圍靠 `.gitignore` 與 `--code-root`；`rules` 記在身分裡，日後改規則就是新身分。
6. 跳過所有 `.` 開頭的路徑與 data / configs root：一條規則擋掉 `.env`、編輯器設定與 vcp 自己會一直變的紀錄。
7. root = `cwd` + `script` + editable + `--code-root`：靜態涵蓋 VCP-041 看不到的別的 repo 與專案套件，不動子程序。
8. 訓練 venv 裡的 vcp 也是 root：它在子程序裡跑，build string 的 `.dirty` 證明不了內容。
9. 快照失敗只 WARN：反目標（不擋訓練）；要擋用授權或 `require_code`。
10. 掃描在記憶體裡、早於磁碟檢查，寫入晚於它：授權不符是輸入錯誤，要比 `low_disk:` 先報，被拒時一個檔都不寫。
11. PROBE 提前：它本來就是唯讀；失敗不再留下 `running` 的 attempt。
12. 子程序 vcp 的 MINOR 不同就 FAIL：新紀錄會讓舊 `Session` 到第一次登記 checkpoint 才當掉，早拒比晚當好。
13. 追蹤靠 `Session` 的 `atexit`、自動登記、可關：不注入任何東西；已經用收據的訓練迴圈不改一行就有 `traced`。
14. 只在主程序、rank 0 追蹤：避免多個程序同時改寫 `train.yaml`。
15. 追蹤永不改變 exit code：紀錄的功能不能讓訓練失敗。
16. 等級 `traced > snapshot > commit > declared`，讀取時算、取所有 attempt 的最低：比照 provenance；resume 的權重來自每一個 attempt。
17. 只有 `incomplete` 才 WARN，沒有 trace 不 WARN：否則每個不用 `Session` 的 run 都 WARN，WARN 就沒有意思；完整性由等級與 gate 表達。
18. ref 掛在既有的 `evidence` 清單：備份、完整性、provenance、`--verify` 都不用另寫（代價見 §16 第 3 項）。
19. 結束比對只重算列過的檔：新檔若被 import，trace 會報 `unlisted`。
20. `ArtifactWriter` 用保留輸入名而不加欄位：manifest 形狀不變，`reuse` 自然比對程式碼與授權，舊 vcp 也讀得動新產物。
21. 產物 job 的 `code_outside:` 與被 import 檔的 `drift:` 都 FAIL：manifest 是永久的宣稱，沒有等級可以降；`create` 時先查一次，多數問題在開跑前浮出。
22. 沒被 import 的檔改了不影響產物：VCP-004 驗收第 2 條。
23. 授權存 claims 的明確快照，不在消耗時重算依據：run 依據之後可能被 resume；比對只看授權寫下的。
24. 消費者多出來的 claim 不管：授權只說「這些必須一樣」。
25. 授權可重複使用：條件全同就是同一個被證明過的設定；job 崩了不用重做 benchmark（§16 第 5 項）。
26. 撤銷 = supersede：沿用不可變產物的機制，不另建台帳。
27. claims 分兩種消費者，train 的字彙固定：那是 `train run` 在寫入前算得出的；產物的字彙開放，因為 `params` / `inputs` 本來就由 job 定。
28. `require_code` 比照 `require_provenance`，另看 test run：檔案提交的推論程式碼在 test run 裡。
29. kernel 的 `--code`：notebook 的 bundle 不屬於任何 run；`require_code ≥ snapshot` 時 candidate 必須給。
30. `verify` 與 `diff` 分開：歷史可驗與目前漂移是兩個問題。
31. `export` 只寫到新目錄：不提供寫回工作樹的命令。
32. 新字彙都是 FAIL：都是輸入或狀態不符，不是環境問題。
33. `vcp.core.build` 改用 `gitstate` 的子程序環境：`vcp_version` 是程式碼身分的一部分，1.0 前該準。

## 15. controller 裁決

尚無。實作前的審查與 controller 的裁決記在這裡，以 R1、R2… 編號。

## 16. 待使用者確認

依重要性排序；括號裡是本 spec 的預設。

1. **程式碼位元組進 data root 與備份**（保留：≤ 1 MiB／檔、≤ 64 MiB／root，含未追蹤未忽略的檔與 editable 套件）。替代：只記 sha，靠 git 與 patch 還原——省空間、少帶機密，但沒有原 repo 或 commit 被改寫時就驗不回內容。
2. **自動追蹤**（自動：子程序第一次用 `Session` 就登記，`VCP_CODE_TRACE=0` 關）。替代：opt-in——沒人開就沒有 `traced`。
3. **新 ref 放在 `train.yaml` / `run.yaml` 的 `evidence` 清單**（是：0.16 以前讀不動 0.17 的 run，整個 data root 一起升級）。替代：只寫在 `env.json` 與事件裡——舊版仍讀得動，但備份、完整性、provenance 都要另寫一套讀法。
4. **1.0 的範圍含 `train run --authorization`**（含）。替代：只做產物 job 的授權（稽核原文只說 artifact gate），訓練的授權留到 1.0 之後。
5. **授權可重複使用**（可）。替代：單次使用——要另建使用台帳與鎖，job 崩了要重新授權。
6. **`require_code` 的預設**（`declared`：不寫就不擋，跟 `require_provenance` 一樣）。替代：新的 `submit init` 寫 `snapshot`。
7. **發版方式**（1c 以 0.17.0 發出，契約項收完再發 1.0.0）。替代：1c 直接發 1.0.0。
8. **1.0 前要不要一場真實比賽用過 0.17**（不要求：CI 的合成端到端加選配的真實 data root 唯讀測試即可）。替代：等下一場比賽跑完再發 1.0。
