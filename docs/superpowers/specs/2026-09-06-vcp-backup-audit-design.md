# vcp 備份審計層設計（子專案 6）

- 日期：2026-09-06
- 狀態：v1（brainstorming 逐節核可後寫成）
- 前置：子專案 0/1 資料層（`2026-09-02-vcp-skeleton-and-data-layer-design.md`）、子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md`）、子專案 4 融合層（`2026-09-05-vcp-fusion-layer-design.md`）、子專案 3 訓練層（`2026-09-05-vcp-training-layer-design.md` §10：「提交層讀 `train.yaml` 的 `uploads`……副本稽核屬子專案 6」）、子專案 5 提交治理（`2026-09-05-vcp-submission-governance-design.md` §13：「`submissions.jsonl`、`submit.yaml` 與 `submit/<test>/` 是它的稽核標的」；後記待辦 4：共用 `vcp/core/proc.py` 與 rclone 的 redact）
- 來源：賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md` §6「撤離清單偏誤」（量測型 log 救到了、分析型 log 幾乎全滅，十條信念淪為不可復驗；清單應以「支撐哪條結論」反向生成）、§7 三層備份（rclone check 0 差異；驗屍計算即備份體系的實戰驗收；rclone OAuth conf 用完即刪）、§9 第 8 點（撤離清單由「結論→證據」反向生成；備份完整性以 rclone check 級驗證；憑證用後即焚；時戳一律 UTC 程式生成）、§9 第 4 點（伺服器端 log 必留檔並即時同步，分析型與量測型同級）
- 後續：六個子專案到此完成；之後是各層後記的待辦與比賽專屬的 `projects/<contest>/`

## 1. 目的

讓「機器被回收前要救什麼」不再是靠目錄習慣拷貝，而是從結論反向算出來的清單：一發最終提交需要哪些檔才能重現與復驗（候選檔、快照、台帳、兩側 run 的預測與卡、融合紀錄與成員、判決與預登記、訓練紀錄與權重副本、logs），一份判決需要哪些，一個 run 需要哪些，整個 dataset 全部需要哪些。清單先小後大（決策層 KB、重現層 MB、權重層 GB），推送逐檔驗證、冪等，事後在任何機器上都能驗三件事：副本還在且 sha 相同、本機的檔彼此仍一致、時戳都是程式產的 UTC。憑證的收尾也是命令的一部分，不靠自覺。

不在本 spec：排程與自動每日備份、`raw/` 搬運、加密、清單與台帳自身的備份（靠 git 遠端）、AWS 重現包與 README 產生、rclone 設定的建立、非 rclone 的雲端 SDK、`train upload` 的錯誤類別（§14）。

## 2. 核心架構原則

- **清單從結論反向生成。** 起點是 `submission:<id>`、`judgement:<prereg>`、`run:<id>` 或 `all`，走證據圖（§7）得到每個檔的角色、tier 與它服務的結論。目錄習慣不是依據。對應報告 §6「撤離清單偏誤」、§9 第 8 點。
- **先小後大。** tier 1 決策層（台帳、卡、判決、預登記、配方、快照、候選檔）永遠先推；tier 2 重現層（預測檔、樣本、`train/`、logs）其次；tier 3 權重層最後且只在要求時。`push --tier N` 累積推到 N。
- **副本要驗過才算。** rclone 目的地以 `hashsum sha256` 逐檔比對、本機目的地讀回比對；已經由 `train upload` 驗證過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。對應報告 §7「rclone check 0 差異」。
- **稽核三層。** 副本（遠端 ↔ 清單）、一致性（本機檔彼此的 sha 鏈）、時戳（每個 `ts` 是 UTC stamp、只增台帳單調）。對應報告 §9 第 8 點、§6 #1。
- **清單與台帳進 git。** 清單只有路徑與 sha，賽後在新機器 `git pull` 就知道該有什麼、去哪裡拉。
- **憑證零接觸、用後即焚。** vcp 不讀 rclone 設定檔內容；rclone 的輸出落地前 redact；`--forget-remote` 在全數驗證通過後才 `rclone config delete <remote>`。對應報告 §7。
- **子程序 runner 收攏。** rclone（訓練層、本層）與 kaggle（提交層）共用 `vcp/core/proc.py` 的 `Runner` / `default_runner` / `redact`。

### 2.1 擴充點

本層沒有新的登記表：目的地只有 rclone 遠端與本機目錄兩種（由字串形狀決定，沿用訓練層 `dest_kind`）；結論種類是四個固定的圖走法。要加結論種類 = 加一個走法函式並登記在 `CONCLUSIONS` 對照表。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 範圍 | 證據清單 + 推送 + 驗證 + 拉回 + 狀態；不做全量鏡像、不只做稽核 | 撤離清單偏誤要機制接住；rclone 不在的機器也要能驗 |
| 結論單位 | `submission:<id>`、`judgement:<prereg>`、`run:<id>`、`all` | 回收前先救最終發，有空再救全部 |
| 憑證 | 可選的 `--forget-remote`（全驗證通過後 `rclone config delete`）；`status` 只報設定檔存在與否 | 報告 §7 的協定程式化；vcp 仍不讀設定檔內容 |
| 清單位置 | `configs/datasets/<name>/backup/<manifest_id>.json`（git）；台帳 `backup.log.jsonl`（git、只 append） | 決策紀錄跟預登記、提交台帳一樣要在 git |
| 目的地佈局 | `<dest>/data/<相對路徑>`、`<dest>/configs/<相對路徑>`、`<dest>/external/<絕對路徑去磁碟與冒號>` | 同一檔在任何清單都落同一處 → 跨清單冪等 |
| 權重副本 | `train.yaml.uploads` 有 `verified=true` 的 checkpoint 記 `remote_copy`，verify 到 `<其 dest>/<run_id>/<name>` 驗 | 不重推 GB 級檔；訓練層的佈局是既定介面 |
| 錯誤類別 | rclone 非 0 → `PlatformError`（FAIL，訊息 redact）；rclone 不在 → `VcpError`（ABORT） | 與提交層的 kaggle 一致；訓練層現有行為不動 |

## 4. 資料模型（`src/vcp/backup/schema.py`，pydantic，`extra="forbid"`）

### 4.1 清單（`configs/datasets/<name>/backup/<manifest_id>.json`，`Manifest`，寫一次不改）

```json
{
  "manifest_id": "submission-SUB34-20260906T120000Z",
  "dataset": "beach-trash-test",
  "conclusion": "submission:SUB34",
  "created_at": "<UTC stamp>",
  "vcp_version": "<vcp version 印的同一字串>",
  "data_root": "C:/vcp-data",
  "files": [
    {"root": "configs", "path": "datasets/beach-trash-test/submissions.jsonl", "sha256": "…", "bytes": 4096,
     "role": "submissions_log", "tier": 1, "kind": "file", "present": true, "for": ["submission:SUB34"]},
    {"root": "data", "path": "runs/fuse-golden12/predictions/valA.jsonl", "sha256": "…", "bytes": 1234567,
     "role": "prediction", "tier": 2, "kind": "file", "present": true, "for": ["submission:SUB34"]},
    {"root": "data", "path": "runs/y12x_r2/weights/best.pt", "sha256": "…", "bytes": 123456789,
     "role": "checkpoint", "tier": 3, "kind": "remote_copy", "present": true,
     "remote": {"dest": "gdrive:vcp/weights", "name": "best.pt"}, "for": ["submission:SUB34"]}
  ]
}
```

- `manifest_id` 預設 `<結論種類>-<id>-<UTC 時戳 YYYYMMDDTHHMMSSZ>`（`all` 為 `all-<時戳>`），`--id` 可改；走 `validate_name`。
- `root` ∈ `data` | `configs` | `external`；`path` 為該 root 下的 posix 相對路徑（`external` 為去掉磁碟與冒號、`\` 換 `/` 的絕對路徑）。
- `sha256` / `bytes`：本機有檔時現算；`present=false`（該有但本機沒有）時取紀錄裡的值（`train.yaml` 的 checkpoint sha、`run.yaml` 的預測 sha、`stage.json` 的候選 sha），沒有紀錄可取 → 該檔不進清單、VERDICT WARN `unlisted=`。
- `role` 為固定字彙（§7.1）；`tier` 由 role 決定；`kind` ∈ `file` | `remote_copy`；`for` 是服務的結論清單（去重後合併）。
- `files` 依 (`tier`, role 順序, `root`, `path`) 排序；同一 `(root, path)` 只出現一次。

### 4.2 台帳（`configs/datasets/<name>/backup.log.jsonl`，只 append）

每列 `{"event", "ts", "manifest_id", …}`：

| event | 欄位 |
|---|---|
| `manifest` | `conclusion`、`files`、`bytes_by_tier`（`{"1": …, "2": …, "3": …}`）、`missing`、`remote_copies` |
| `push` | `dest`、`tier`、`pushed`、`skipped`、`verified`、`failed`（清單）、`bytes` |
| `verify` | `dest`（可 null）、`copies`（`{"ok", "missing", "mismatch"}` 各為個數）、`drift`（個數）、`bad_stamps`（個數）、`first_bad`（`file:line` 或 null） |
| `pull` | `dest`、`tier`、`pulled`、`skipped`、`conflicts`（清單） |
| `remote_forgotten` | `remote` |

`ts` 一律由寫入時的 `stamp()` 產生。

## 5. 目錄佈局

```
<VCP_CONFIGS_ROOT>/datasets/<name>/backup/<manifest_id>.json
<VCP_CONFIGS_ROOT>/datasets/<name>/backup.log.jsonl
<dest>/data/<相對 data root 的路徑>          # 推送目的地（rclone 遠端或本機目錄）
<dest>/configs/<相對 configs root 的路徑>
<dest>/external/<絕對路徑去磁碟與冒號>
<其他 dest>/<run_id>/<checkpoint 檔名>       # remote_copy：訓練層 train upload 的既有佈局
```

`DatasetPaths` 新增 `backup_dir`、`backup_log`、`backup_manifest(manifest_id)`。

## 6. CLI 總表（`vcp backup`）

`--dataset` 是清單所屬 dataset（`submission:` 用 test dataset，其餘用 run / 預登記所屬的 dataset）。共用 `--json`、`--data-root`、`--configs-root`。所有命令以 VERDICT 收尾，exit 0 / 0 / 1 / 2。

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `manifest --dataset D --conclusion submission:<id>\|judgement:<prereg>\|run:<run>\|all [--id M]` | 走證據圖（§7）→ 寫 `backup/<M>.json` → `manifest` 列 | 結論不存在 → FAIL `reason=not_found`；id 已存在 → FAIL `reason=exists`；該有的檔本機缺 → WARN `missing=`（列仍寫、`present=false`）；缺檔且無 sha 紀錄 → WARN `unlisted=`；checkpoint 本機沒有也無副本紀錄 → 計入 `missing=` |
| `push --dataset D --manifest M --dest DEST [--tier 1\|2\|3] [--forget-remote]` | §6.1 | 任一檔驗不過 → FAIL `mismatch=`（已推的照記）；rclone 不在 → ABORT `rclone_not_found`；rclone 非 0 → FAIL；`--forget-remote` 配本機 dest 或本次有任何失敗 → FAIL `reason=forget_refused`（不刪） |
| `verify --dataset D --manifest M [--dest DEST]` | §6.2 三層 | 副本缺 / 不符 → FAIL `missing= mismatch=`；本機漂移 → FAIL `drift=`；壞時戳 → FAIL `bad_stamps= first_bad=`；沒給 `--dest` 只做一致性與時戳；`present=false` 的檔本機部分略過 |
| `pull --dataset D --manifest M --dest DEST [--tier N] [--overwrite]` | 從 dest 把清單裡本機缺的 `file` 條目拉回原相對路徑、讀回驗 sha；`remote_copy` 從它記錄的 dest 拉；`pull` 列 | 本機已有且 sha 不同 → FAIL `conflict=`（`--overwrite` 才蓋，舊檔留 `.bak-<時戳>`）；拉回驗不過 → FAIL；dest 缺 → FAIL `missing=` |
| `status --dataset D` | 唯讀：每份清單最新的 push / verify 結果與時間、從未推過的 tier、`rclone_conf=present\|absent` | 無清單 → WARN；有清單從未 verify 通過 → WARN；`rclone_conf=present` → WARN |

### 6.1 push 的順序

1. 載入清單與台帳；`dest_kind(dest)`（沿用訓練層：`remote:path` 是 rclone，Windows 磁碟不是）。
2. rclone 目的地：先各 root 一次 `rclone hashsum sha256 <dest>/<root>` 取現況；本機目的地：逐檔 `sha256_file`。
3. 依 tier 升冪、清單順序，對每個 `kind=file` 且 `present=true` 且 tier ≤ `--tier` 的檔：目的地 sha 相同 → `skipped`；否則 rclone `copyto --checksum` / 本機複製，計 `pushed`。`remote_copy` 條目不推。
4. 推完各 root 再一次 `hashsum` / 讀回，逐檔比對 → `verified`；不符者進 `failed`。
5. 寫 `push` 列（不論成敗，已發生的都記）。
6. `--forget-remote`：`failed` 為空且 dest 是 rclone → `rclone config delete <remote>` → `remote_forgotten` 列；否則 FAIL `forget_refused`。

### 6.2 verify 的三層

1. **副本**：給了 `--dest` 才做。清單每個 `kind=file` 條目：目的地 sha（rclone 每 root 一次 `hashsum`；本機讀回）等於清單 sha → `ok`，不在 → `missing`，不同 → `mismatch`。`remote_copy` 條目：`rclone hashsum sha256 <其 dest>/<run_id>` 找 `name`。
2. **一致性**（只對 `present=true` 且本機存在的檔）：`run.yaml` 的 `predictions[*].sha256` ↔ 預測檔；`train.yaml` 的 checkpoint sha ↔ 檔（每個檔名最新一筆，同 `train status`）；`fuse.json` 的 `output_sha256` ↔ 預測檔、`member_sha256` ↔ 成員預測檔；`stage.json` 的 `artifact.sha256` ↔ 候選檔；`dataset.yaml` 的 `samples_hash` ↔ `samples.jsonl`；`submissions.jsonl` 的 `staged.sha256` ↔ `stage.json`；清單 sha ↔ 現在的檔（清單之後被改）。每個不符計一個 `drift`，`--json` 列出 `(what, expected, actual)`。
3. **時戳**：清單裡每個 role 為台帳的 jsonl（`submissions_log`、`readings`、`judgements`、`sigma`、`anchors_log`、`prereg_log`、`train_log`、`history`、`unseal_log`、`backup_log`、`logs`）逐列 `ts` 經 `parse_stamp`，且與前一列相比不減；每張卡（`dataset.yaml`、`run.yaml`、`train.yaml`、`stage.json`、`fuse.json`、預登記、配方、plan）的 `created_at` / `*_at` / `ts` 欄位可解析。壞的計 `bad_stamps`，第一個位置記 `first_bad`。

寫 `verify` 列（三層結果）。三層任一有問題 → FAIL；`--dest` 缺席時 `copies` 記 null。

## 7. 證據圖

### 7.1 角色與 tier

| tier | role | 檔 |
|---|---|---|
| 1 | `submit_profile` | `configs/datasets/<test>/submit.yaml` |
| 1 | `submissions_log` | `configs/datasets/<test>/submissions.jsonl` |
| 1 | `stage` | `submit/<test>/<id>/stage.json` |
| 1 | `artifact` | `submit/<test>/<id>/<候選檔>` |
| 1 | `judgements` / `readings` / `sigma` / `anchors` / `anchors_log` | `measure/<dataset>/*.jsonl`、`anchors.json` |
| 1 | `prereg` / `prereg_log` | `configs/datasets/<dataset>/prereg/<id>.yaml`、`prereg.log.jsonl` |
| 1 | `recipe` | `configs/datasets/<dataset>/fuse/<id>.yaml` |
| 1 | `run_card` / `history` / `fuse_record` | `runs/<id>/run.yaml`、`history.jsonl`、`fuse.json` |
| 1 | `train_record` / `train_log` | `runs/<id>/train.yaml`、`train.log.jsonl` |
| 1 | `dataset_card` / `plan` / `unseal_log` | `configs/datasets/<dataset>/dataset.yaml`、`splits/<plan>.json`、`splits/<plan>.unseal.jsonl` |
| 2 | `prediction` | `runs/<id>/predictions/<subset>.jsonl` |
| 2 | `samples` / `raw_manifest` | `datasets/<dataset>/samples.jsonl`、`raw_manifest.txt` |
| 2 | `train_dir` | `runs/<id>/train/*`（console、env、config 副本） |
| 2 | `logs` | `logs/vcp-*.jsonl`（全部；小） |
| 3 | `checkpoint` | `train.yaml` 登記的每個 checkpoint（每個檔名最新一筆；`final` 排最前） |

`cache/`、`raw/`、`inputs/`（ingest `--keep-input`）不在任何清單裡。

### 7.2 走法

結果依 `(root, path)` 去重，`for` 合併；`present` 由本機 `is_file()` 決定。

- **`run:<id>`**：`run_card`、`history`（若有）、每個 `predictions[*]`；`fuse_record`（若有）→ 對每個成員遞迴 `run:<member>`，加該配方（`recipe_id` 對應的 `recipe`，若配方檔還在）；`train_record`、`train_log`、`train_dir`、`checkpoint`（若有 `train.yaml`）；該 run 的 dataset 的 `dataset_card`、`plan`（`plan_id`）與 `unseal_log`（若有）；該 dataset 的整份 measure 台帳（`readings`、`judgements`、`sigma`、`anchors`、`anchors_log`，存在的才列）。
- **`judgement:<prereg>`**：`prereg`、`prereg_log`、該 dataset 的 measure 台帳；`run:<baseline_run>`、`run:<candidate_run>`。
- **`submission:<id>`**（`--dataset` 是 test dataset）：`submit_profile`、`submissions_log`、`stage`、`artifact`（file 類）；`run:<eval_run>`；`run:<test_run>`（file 類）或 `artifact.weights[*].run` 的 `run:`（kernel 類）；`stage.json` `gate.judgements` 每份 `judgement:`；test dataset 的 `dataset_card` 與 `plan`（`test_plan`）。
- **`all`**：`runs/*/run.yaml` 中 `dataset` 等於本 dataset 的每個 run 的 `run:`；每份預登記的 `judgement:`；有 `submit.yaml` 時每個 staged id 的 `submission:`；`samples`、`raw_manifest`、`logs`。

`checkpoint` 條目：`train.yaml.uploads` 裡有 `(name, sha256)` 相符且 `verified=true` 的紀錄 → `kind=remote_copy`、`remote={dest, name}`（多筆取最新）；否則 `kind=file`。checkpoint 路徑經 `resolve_stored_path`，在 data root 外 → `root=external`。

## 8. 錯誤處理與 VERDICT 字彙

| 情境 | 類型 / `reason=` | 狀態 |
|---|---|---|
| 結論 / 清單找不到 | `ValidationFailed` `not_found` | FAIL |
| 清單 id 已存在；`--conclusion` 格式錯 | `ValidationFailed` `exists` / `bad conclusion` | FAIL |
| 該有的證據檔本機缺 | WARN `missing=`（`present=false`）；無 sha 可記 → WARN `unlisted=` | WARN |
| 副本不符 / 缺 | `IntegrityError` `mismatch=` `missing=` | FAIL |
| 本機漂移 / 壞時戳 | `IntegrityError` `drift=` / `ValidationFailed` `bad_stamps=` | FAIL |
| pull 衝突 | `ValidationFailed` `conflict=` | FAIL |
| `--forget-remote` 被拒 | `ValidationFailed` `forget_refused` | FAIL |
| rclone 找不到 | `VcpError` `rclone_not_found` | ABORT |
| rclone 非 0 | `PlatformError`（redact 後的最後一行） | FAIL |
| 台帳列壞 / 清單壞 | `ValidationFailed` 帶 `file:line` | FAIL |

共用欄位：`dataset=` `manifest=` `conclusion=` `files=` `tier=` `dest=` `pushed=` `skipped=` `verified=` `failed=` `ok=` `missing=` `mismatch=` `drift=` `bad_stamps=` `first_bad=` `pulled=` `conflicts=` `rclone_conf=`。

## 9. 隱私

同提交層 §11：vcp 不讀 rclone 設定檔的內容（`status` 只用 `rclone config file` 印出的路徑做 `is_file()`）；rclone 的 stdout / stderr 落地前一律 `redact`；子程序繼承環境、不記錄環境；`--forget-remote` 只呼叫 `rclone config delete <remote>`；清單與台帳只含路徑、sha、大小、時間、dest 字串（remote 名稱與路徑，不含憑證）。測試以假 runner 在 stderr 吐秘密，掃描兩個 root 與所有輸出零命中。

## 10. 與其他子專案的介面

- **`vcp/core/proc.py`（新）**：`Runner`、`default_runner`、`redact` 從 `submit/platforms/base.py` 搬來；`submit/platforms/*` 與 `train/upload.py` 改 import 它；`train/upload.py` 的 rclone 錯誤訊息經 redact（錯誤類別不變）；`PlatformError` 的 docstring 放寬為「外部工具的 CLI 失敗」。
- **資料層**：`DatasetPaths` 新增 `backup_dir`、`backup_log`、`backup_manifest(id)`；讀 `dataset.yaml`、`splits/`、`samples.jsonl`、`raw_manifest.txt`。
- **量測層**：`load_run`、`verify_prediction`（一致性）、台帳路徑常數、`prereg_path` / `list_preregs` / `load_prereg`；零改動。
- **融合層**：`load_record`、配方路徑；零改動。
- **訓練層**：讀 `train.yaml`（checkpoints、uploads）、`train/`；`dest_kind` 與 `<dest>/<run_id>/<name>` 佈局；零改動除 import 搬家。
- **提交層**：`load_profile`、`load_staged`、`SubmissionLedger`、`submission_dir`；零改動除 import 搬家。
- **文件**：README 新增「備份審計命令 `vcp backup`」一節（五個命令、機器回收前的撤離順序、賽後重建）；CLAUDE.md 路徑加 `configs/datasets/<name>/backup/` 與 `backup.log.jsonl`，常用命令加 `manifest` / `push` / `verify`。

## 11. 測試策略

- **單元**：schema（`extra="forbid"`、role / tier / kind 字彙、`present=false` 需有 sha）；四種結論的證據圖（以 `tests/submit_fixtures.py` 建 eval / test dataset、`good` / `bad` run、融合 run 與成員、預登記與判決、staged submission）：每種結論的檔集合、去重與 `for` 合併、排序、`present=false`、`remote_copy` 判定（有 verified upload 紀錄 vs 沒有）、`external` root；清單寫讀；push 本機 dest（複製、讀回、冪等、不符 FAIL、tier 過濾）；假 rclone runner（`hashsum` 前後兩次、`copyto` 只對不同的檔、失敗訊息 redact、`config delete` 只在全 verified 後、本機 dest 配 `--forget-remote` 拒絕）；verify 三層各一陽一陰（dest 少檔 → missing、改預測檔 → drift、改台帳 ts 順序 → bad_stamps、卡的 `created_at` 壞 → bad_stamps）；pull（缺檔拉回並驗、衝突、`--overwrite` 留 `.bak`）；status（無清單、從未 verify、rclone 設定檔存在）；`proc.py` 搬家後 submit 與 train 既有測試不變。
- **CLI**：每個命令的 VERDICT 欄位與 exit code；`--json`。
- **端到端** `tests/unit/test_e2e_backup.py`：延伸 Plan 6 的手動平台劇本到 final → `manifest submission:S1` → `push` 本機目錄 `--tier 2` → 第二次 push 全 skipped → `verify --dest --tier 2` OK（實作後加的 `--tier`，見 §14）→ 刪本機一個預測檔 → `pull` 拉回 → 改 `readings.jsonl` 一個 `ts` 順序 → `verify` FAIL `bad_stamps=1`；假 rclone runner 版本：`push --tier 1 --forget-remote` → `config delete` 被呼叫、`remote_forgotten` 列、隱私掃描零命中。
- **真資料**：RSNA `manifest --conclusion all` 只列不推（無資料即 skip）。

## 12. 驗收條件

1. 端到端劇本 exit 0，每個命令都有 VERDICT。
2. `push --tier 1` 不碰任何 checkpoint；tier 1 的檔全部 < 16 MB。
3. 第二次 `push` 全部 `skipped`，台帳多一列。
4. 篡改：改預測檔 → `verify` `drift=1`；亂台帳 ts → `bad_stamps=1`；dest 刪檔 → `missing=1`。
5. `--forget-remote` 只在全 verified 後呼叫 `rclone config delete`，任一失敗不呼叫。
6. 隱私：假 rclone stderr 帶秘密 → 兩個 root 與所有輸出零命中。
7. `pull` 拉回的檔 sha 與清單相同；衝突不覆蓋。
8. 覆蓋率 ≥ 80%、ruff `check` 與 `format --check` 乾淨。

## 13. 不在範圍

排程與自動每日備份；`raw/` 搬運（`raw_manifest.txt` 可重下）；加密；清單與台帳自身的備份（靠 git 遠端）；AWS 重現包與 README 產生（`projects/`）；rclone 設定的建立與 OAuth；非 rclone 的雲端 SDK；`train upload` 的錯誤類別變更；跨 dataset 的單一清單（每個 dataset 各自一份，`all` 已覆蓋）。以上皆為已預留的擴充點，不是設計缺口。

## 14. 補充決定（實作期，2026-09-06；以程式碼為準）

計畫層決定 1–21 與執行期裁決 R0–R7、最終審查修正波的結果（細節見 `docs/superpowers/plans/2026-09-06-vcp-plan7-followups.md`）：

- **資料模型**：`FileEntry` 多 `source`（external 的絕對路徑，Windows / posix 兩種絕對形式都認）；`RemoteCopy` 記 `{dest, run, name}`；tier 3 分 `checkpoint_final`（排前）與 `checkpoint`；`for` 以 `for_` 別名存；清單 JSON `indent=1`、LF；`path` 只收根下的相對 posix 路徑（無 `..`、`.`、空段、磁碟、`/` 開頭、反斜線）；`Manifest.data_root` 只是人讀的來源標記，程式不讀；台帳 pull 列有專屬欄位 `dest_missing`、`mismatch`、`external_skipped`。
- **證據圖**：`logs` 只在 `all`；`run:<id>` 必須屬於 `--dataset`；`unlisted` 去重；`all` 走法容忍失聯的引用（run 被刪的判決 / 提交），記在 `skipped` 並 WARN `skipped=`；單一結論的走法遇到失聯仍 FAIL `not_found`。
- **push**：任何位元組移動前逐檔預檢（不存在 → `not_found:`，sha 變 → `drift:`）；台帳角色的檔在清單之後只增長（前 N 位元組 sha 相同）→ 推清單那一刻的快照（前 N 位元組），不算 drift；`--forget-remote` 配本機 dest 在推送前就拒絕；push 列先寫再拋錯；複製或讀回失敗一律 `verified=0`、全部列入 `failed`；`--forget-remote` 要整份清單的每個 `kind=file` 條目（含更高 tier 與 `present=false`）都在目的地驗過且至少一個 verified，否則 `forget_refused` 帶 `unverified=`——所以帶 `present=false` 條目的清單不能 forget，先從別處 pull 回來或手動 `rclone config delete`。
- **verify**：回傳結果不拋錯，CLI 依結果定 FAIL 並帶 `reason=`（`mismatch` > `missing` > `drift` > `bad_stamps`）；`--tier N` 只限定副本層（預設 3 = §6.2 的全查；§11 的「push --tier 2 → verify OK」要配 `--tier 2`），有 `--dest` 時 verify 列記 `tier`；一致性層對台帳角色用前綴 sha（只增長不算 drift，被改或截短才算）；順帶檢查 `backup.log.jsonl` 自己的時戳；時戳掃描跳過 `downloaded_at`；`present=false` 且目的地也沒有的條目進 `absent` 桶、不算失敗；副本層的 rclone 失敗是 `PlatformError`（不寫 verify 列）。
- **pull**：目的地 sha 先比對（不符 → `mismatch`、不拉）；拉回不符就刪；本機已有且相同（台帳角色：長大也算）→ `skipped`；不同 → `conflict`，`--overwrite` 才蓋且舊檔留 `.bak-<UTC 時戳含微秒>`，新檔驗不過就把 `.bak` 還原；只寫到 data / configs 根內（`unsafe_path:`）；external 只還原到既有目錄，否則 `external_skipped=`（WARN）；先記列再拋錯，優先序 `mismatch` > `missing` > `conflict`。
- **status**：`verified` = 某個 verify 列有 `--dest`、tier 3、副本層與本機兩層皆無問題；另報 `local_ok`（一致性與時戳過）；「已推 tier」= `failed` 空的 push 的最大 `--tier` 以下全部；rclone 不在 → `rclone_conf=unknown`。
- **目的地**：rclone 命令前綴是 `vcp.backup.dest.RCLONE`（端到端測試指到假 rclone 腳本）；`hashsum` exit 3 / 4 才是空目錄，其餘非 0 是 `PlatformError`（redact 後的最後一行）；雜湊列不是 64 hex（後端不支援 sha256）也是 `PlatformError`；`hashsum` 解析 `<sha>  <相對路徑>` 沿用訓練層。
