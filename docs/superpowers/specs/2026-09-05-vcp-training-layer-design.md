# vcp 訓練層設計（子專案 3）

- 日期：2026-09-05
- 狀態：v1（brainstorming 逐節核可後寫成）
- 前置：子專案 0/1 資料層（`2026-09-02-vcp-skeleton-and-data-layer-design.md`，§15.8 把「訓練層對 materialize 快取的讀取介面」留給本 spec）、子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md` §11：「訓練層負責寫 `run.yaml` 的 `source` 與 `trained_on`，並呼叫 ingest」）、子專案 4 融合層（`2026-09-05-vcp-fusion-layer-design.md`）
- 來源：賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md` §9 藍圖第 3 點（每 run 自動記錄 config / seed / 環境快照；checkpoint 自動上雲——權重遺失是四個成員無法重跑的根因；工具鏈 venv 隔離）、§6 錯誤目錄 #4（環境污染）與 #5（記憶過期）、§7（AWS 從權重全集重跑的驗證閉環、三層備份）
- 後續：子專案 5 提交治理 → 6 備份審計

## 1. 目的

讓每一次訓練在開始的那一刻就成為一個有身分的 run：訓練用了哪些子集（由匯出憑證推導，不由人填）、哪份 config、哪個 seed、哪個環境；結束時每個 checkpoint 都有 sha256，而且有一份帶 sha 的異地副本紀錄。之後的量測、融合、提交都只認這個 run 目錄。

框架不綁訓練工具：ultralytics 的 `yolo train`、mmdetection 的 `tools/train.py`、自寫的 PyTorch loop 都是「一條在某個 venv 裡執行的命令」。vcp 包在外面記錄，不介入訓練本身。自寫 loop 唯一需要碰 vcp 的地方是讀 materialize 快取與登記 checkpoint。

不在本 spec：框架適配器、超參搜尋 / 排程 / 佇列、分散式訓練、notebook 產生器、跨機器搬 run 目錄、備份稽核、rclone 設定與憑證、推論與預測轉檔（`vcp eval ingest` 已有）。

## 2. 核心架構原則

- **產出就是 run。** `vcp train run` 一開始就寫 `runs/<run_id>/run.yaml`（量測層的 `RunCard`，不加欄位），之後 `vcp eval ingest --run R` 直接接上；訓練專屬的紀錄住在同目錄的 `train.yaml`、`train.log.jsonl`、`train/`。
- **憑證推導，不靠記憶。** `trained_on` 從 `vcp data export` 的 `manifest.json` 讀（資料層 spec §7.3 的承諾）；`config_hash` 永遠非空；環境快照由框架 venv 的 python 自己回報。對應報告 §6 #5「記憶過期」。
- **venv 隔離落地。** `--venv DIR` 是命令執行與快照的環境，vcp 核心 venv 從不 import 訓練框架；沒給 `--venv` 只警告不拒絕。對應報告 §6 #4。
- **checkpoint 有身分、有副本。** 登記 = sha256 + 大小 + 時間；上傳 = 複製 + 讀回或 hashsum 驗證，驗不過就 FAIL；`train status` 把沒有異地副本的 checkpoint 標成 WARN。對應報告 §9 第 3 點。
- **只增事件、換寫留痕。** `train.log.jsonl` 只 append；`train.yaml` 是快照，每次事件後整份重寫；console 逐行落檔——伺服器上訓練到一半機器被回收，留下的是能重建結論的紀錄（報告 §6「撤離清單偏誤」）。

### 2.1 擴充點

本層沒有新的登記表：訓練工具是命令，不是插件。上傳目的地只有 rclone 與本機目錄兩種，由字串形狀決定（§6.3）。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 接入形狀 | CLI 包裝器（`vcp train run … -- <命令>`）+ 小 Python API（`MaterializedReader`、`Session`） | 第三方框架零改碼即有紀錄；自寫 loop 只需 import 兩個名字 |
| 上傳機制 | `--upload remote:path`（rclone）與 `--upload DIR`（本機 / 掛載目錄）兩種，皆驗 sha | 租用機上有 rclone，本機 Windows 可用同步資料夾；rclone 不進依賴 |
| 架構 | 包裝器 + 紀錄器，產出是 run；不做事後補記模式、不做框架適配器 | 藍圖要「自動」；適配器綁框架版本 |
| `trained_on` 來源 | `--export DIR`（可重複）推導；自寫 loop 讀快取時 `--trained-on` 明寫；兩者都給須一致 | 匯出憑證是資料層設計好的「我用什麼訓的」證據 |
| `config_hash` | 有 `--config` 檔取檔案 sha256；否則取整條命令 canonical JSON 的 sha256 | 永遠非空，命令列式框架也有重現身分 |
| seed | 只記錄並以環境變數傳給子程序；缺席 WARN | vcp 不能替框架決定隨機性，但能讓「沒設 seed」被看見 |
| 中斷 | Ctrl+C 終止子程序，attempt 記 `interrupted`，VERDICT FAIL | 半成品 checkpoint 仍登記，讓失敗也有身分 |
| torch 包裝 | 不進 vcp；README 給十行示範 | vcp 核心不依賴任何訓練框架 |

## 4. 資料模型（`src/vcp/train/schema.py`，pydantic，`extra="forbid"`）

### 4.1 `runs/<run_id>/run.yaml`（量測層的 `RunCard`，本層只填值）

| 欄位 | 值 |
|---|---|
| `run_id` / `dataset` / `samples_hash` / `plan_id` | 命令列與 dataset |
| `trained_on` | 由 exports 推導或明寫的子集，排序 |
| `source.framework` | `--framework` 的自由文字（預設空字串） |
| `source.config_hash` | §3 的規則，非空 |
| `source.weights_hash` | `--final` 命中的 checkpoint 的 sha256；沒有則 `null` |
| `source.export_manifest_sha` | 第一個 `--export` 的 `manifest.json` sha256；沒有 export 則 `null` |
| `source.notes` | `--notes` |
| `predictions` | 空；之後由 `vcp eval ingest` 填 |

### 4.2 `runs/<run_id>/train.yaml`（`TrainRecord`）

```yaml
run_id: y12x_r2
dataset: marine
plan_id: p1
trained_on: [train]
exports:
  - {dir: exports/marine-train-yolo, subset: train, format: yolo, manifest_sha256: "…", sample_count: 13259}
config: {path: projects/marine/cfg/y12x.yaml, sha256: "…", copy: train/config.1.yaml}   # 沒有 config 檔時為 null
config_hash: "…"                 # 與 run.yaml 相同
seed: 42                         # 或 null
framework: "ultralytics 8.3.0"   # 與 run.yaml 相同
venv: C:/venvs/ultra             # store_path 規則；null = inherited
cwd: projects/marine
command: [yolo, train, model=yolo12x.pt, data=exports/marine-train-yolo/data.yaml, epochs=60]
attempts:
  - {n: 1, started_at: <stamp>, finished_at: <stamp>, duration_s: 5231.4, exit_code: 0,
     status: finished, console: train/console.1.log, env: train/env.1.json}
checkpoints:
  - {path: runs-ultra/y12x_r2/weights/best.pt, sha256: "…", bytes: 118234567,
     registered_at: <stamp>, attempt: 1, final: true, source: glob}
uploads:
  - {dest: "gdrive:vcp/weights", kind: rclone, name: best.pt, sha256: "…", verified: true, uploaded_at: <stamp>}
notes: ""
```

- `attempts[].status` ∈ `running | finished | failed | interrupted`；`exit_code` 在 `running` 時為 `null`。
- `checkpoints[].source` ∈ `glob | session`；同一路徑同一 sha 只登記一次（resume 重複命中即略過）。
- `uploads[].kind` ∈ `rclone | local`；`name` 是目的地 `<dest>/<run_id>/<name>` 的檔名。
- 路徑一律經 `store_path`（在 `data_root` 下存相對 posix，否則絕對）；`train/…` 相對 run 目錄。
- 每次事件後整份重寫（`dump_yaml_model`），舊值可在 `train.log.jsonl` 找到。

### 4.3 `runs/<run_id>/train.log.jsonl`（只 append）

每列 `{"ts", "event", "attempt", …}`，`event` ∈ `started | env | checkpoint | uploaded | finished | note`。`ts` 由寫入者取時鐘。`note` 列帶 `key` / `value`（`Session.note`）。

### 4.4 `runs/<run_id>/train/env.<n>.json`（`EnvSnapshot`）

`python`（版本字串）、`executable`、`platform`、`hostname`、`packages`（`{name: version}`，`importlib.metadata`）、`torch` / `cuda` / `cudnn`（可 import 才有，否則 `null`）、`gpus`（`nvidia-smi` 的名稱清單，沒有即 `[]`）、`nvidia_driver`、`vcp_version`、`git`（`--cwd` 所在 repo 的 `commit` 與 `dirty`，不在 repo 內為 `null`）、`taken_at`。快照由 venv 的 python 執行一段固定的探針程式印 JSON 取得；探針失敗 → ABORT（環境本身有問題）。

## 5. 目錄佈局

```
runs/<run_id>/run.yaml                 # RunCard（量測層格式）
runs/<run_id>/train.yaml               # TrainRecord 快照
runs/<run_id>/train.log.jsonl          # 只增事件
runs/<run_id>/train/console.<n>.log    # 子程序 stdout+stderr 逐行
runs/<run_id>/train/config.<n>.<ext>   # --config 原檔副本
runs/<run_id>/train/env.<n>.json       # 環境快照
<dest>/<run_id>/<checkpoint 檔名>       # 上傳目的地（rclone 遠端或本機目錄）
```

程式碼：`src/vcp/train/{schema,records,env,checkpoints,upload,run,reader,session}.py`、`src/vcp/cli_train.py`（群組 `vcp train`，於 `cli.py` 以 `add_typer` 掛上）。

## 6. CLI 總表（`vcp train`）

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `run --run R --dataset D --plan P [--export DIR]… [--trained-on a,b] [--venv DIR] [--config PATH] [--seed N] [--framework TEXT] [--cwd DIR] [--checkpoints GLOB]… [--final GLOB] [--upload DEST]… [--resume] [--notes TEXT] -- COMMAND…` | 寫 run → 快照 → 執行 → 登記 checkpoint → 上傳 | 命令 exit ≠ 0 → FAIL `exit_code=`；驗證失敗 → FAIL；seed / venv / final 缺 → WARN；export 不符 → ABORT；run 已存在且無 `--resume` → FAIL `reason=run_exists` |
| `upload --run R --dest DEST [--only final]` | 事後或換目的地上傳已登記的 checkpoint，冪等 | 任一檔驗不過 → FAIL；rclone 不存在 → ABORT |
| `status --run R [--verify]` | 印 attempts / checkpoints / uploads（唯讀） | 有 checkpoint 無驗證過的副本 → WARN `unbacked=`；有 `running` 的 attempt → WARN `running=`；`--verify` 重算 sha，不符 → WARN `drift=` |

共用選項：`--json`、`--data-root`、`--configs-root`。`--json` 時子程序的 console 回顯改到 stderr（stdout 只有結果 JSON）。三個命令以 VERDICT 收尾，exit 0 / 0 / 1 / 2；沒有 `--plugin`（本層不碰登記表）。

VERDICT 欄位：

- `run`：`cmd=train.run status=… run=R attempt=N exit_code=0 duration_s=… checkpoints=K final=<sha12|none> uploaded=U verified=U seed=<n|none> venv=<name|inherited>`
- `upload`：`cmd=train.upload status=… run=R dest=… uploaded=U verified=U skipped=S`
- `status`：`cmd=train.status status=… run=R attempts=N checkpoints=K backed=B unbacked=U running=R`

`--json` 時 `run` / `upload` / `status` 都回 `train.yaml` 的內容（`status --verify` 另帶 `drift` 清單）。

### 6.1 `run` 的順序（寫入之前做完所有檢查）

1. 解析路徑；載 dataset（驗 hash）與 plan（`dataset_hash` 須等於 card 的 `samples_hash`，否則 ABORT）。
2. 推導 `trained_on`：每個 `--export DIR` 讀 `manifest.json`，其 `dataset` / `samples_hash` / `plan_id` 須與命令列及 dataset 一致、`subset` 須在 plan 裡，否則 `PlanMismatchError`（ABORT，`fields={"export": DIR}`）；聯集所有 `subset`。`--trained-on` 給了就與推導值比對，不同 → FAIL；兩者都沒給 → FAIL（`trained_on` 是必要資訊）。
3. `config_hash`：`--config` 存在則取檔案 sha256；否則取 `canonical_json(command)` 的 sha256。`--venv` 給了則其 python（Windows `Scripts/python.exe`、其餘 `bin/python`）必須存在，否則 ABORT。`--final` 自動也算一個 `--checkpoints` glob。
4. run 目錄：`train.yaml` 已存在且無 `--resume` → FAIL `reason=run_exists`；`--resume` 時紀錄的 `config_hash` 須相同（不同 = 新 run，FAIL）且 `run.yaml` 的 dataset / plan / `trained_on` 須相同，attempt 編號 +1；`run.yaml` 存在但沒有 `train.yaml`（ingest 或 fuse 產的 run）→ FAIL `reason=run_bound_elsewhere`。
5. 寫入：新 run 寫 `run.yaml`；寫 `train.yaml`（attempt `running`）；複製 config 到 `train/config.<n>.<ext>`；`started` 事件。
6. 環境快照 → `train/env.<n>.json`；`env` 事件。
7. 執行命令：`cwd` = `--cwd`（預設當前目錄）；環境變數 = 當前環境 + `VCP_RUN_ID` / `VCP_DATA_ROOT` / `VCP_CONFIGS_ROOT` +（有 seed 時）`VCP_SEED` / `PYTHONHASHSEED` +（有 venv 時）`VIRTUAL_ENV` 與 PATH 前置；stdout+stderr 合流，逐行寫 `train/console.<n>.log`（utf-8，`errors="replace"`）並回顯；結束記 `exit_code`、`finished_at`、`duration_s`、`status`（0 → `finished`，其餘 → `failed`）；`finished` 事件。Ctrl+C：終止子程序、等待、`status=interrupted`。
8. 登記 checkpoint（不論成敗）：每個 glob 相對 `--cwd` 展開，只取檔案，算 sha256 與 bytes，寫 `checkpoints[]`（`source: glob`）與 `checkpoint` 事件。`--final`：命令成功時須恰好命中一個檔（0 或 >1 → FAIL `reason=final_ambiguous`，`fields={"checkpoint": glob}`），其 sha 寫進 `run.yaml` 的 `weights_hash`；命令失敗時略過並 WARN；沒給 `--final` 但 `Session` 登記過 `final=true` 的 checkpoint，取那一個；都沒有 → WARN `final=none`。
9. 上傳（§6.3）：只在命令成功時做；每個 `--upload DEST` 一輪。
10. VERDICT。

vcp 自己在第 5 步之後崩潰會留下 `status: running` 的 attempt——`train status` 以 WARN `running=` 揭露，`--resume` 可續。

### 6.2 checkpoint 登記

- 身分 = `(path, sha256)`；同身分不重複登記。大小以 bytes 記，時間為登記時刻（不取檔案 mtime）。
- 登記不搬檔：checkpoint 留在框架寫的位置；`train.yaml` 記 `store_path` 後的路徑。
- `Session.register_checkpoint(path, *, final=False)` 在訓練中登記（`source: session`），與 glob 登記寫同一張表；同一路徑先由 session 登記、結束後又被 glob 命中，以 sha 判定是否同一份。

### 6.3 上傳與驗證

- 目的地形狀：`remote:path`（第一個 `:` 前只有 `[A-Za-z0-9_-]`，且不是 Windows 磁碟機 `X:\` / `X:/`）→ `rclone`；其餘 → 本機 / 掛載目錄。
- 目標路徑 `<dest>/<run_id>/<name>`，`name` = checkpoint 檔名；兩個 checkpoint 檔名相同 → FAIL `reason=name_collision`。
- rclone：逐檔 `rclone copyto <src> <dest>/<run_id>/<name> --checksum`，之後 `rclone hashsum sha256 <dest>/<run_id>` 解析結果逐檔比對登記的 sha；rclone 不在 PATH → `VcpError`（ABORT）。呼叫經可注入的 runner（測試用假 runner）。
- 本機：`shutil.copy2` 到目標，讀回算 sha256 比對。
- 每檔一筆 `uploads[]`（`verified` 為比對結果）與 `uploaded` 事件；任一檔 `verified=false` → 命令 FAIL `verified<uploaded`。
- 冪等：目的地已有同名且 sha 相同的檔（rclone 以 hashsum、本機以讀回）→ 略過並計 `skipped=`；`train upload --only final` 只傳 `final=true` 的檔。

## 7. 環境快照（`src/vcp/train/env.py`）

`snapshot(python: Path | None, cwd: Path) -> EnvSnapshot`：以指定的 python（`--venv` 的，或 `sys.executable`）執行內建探針（`python -c <固定字串>`）印 JSON：`sys.version`、`sys.executable`、`platform.platform()`、`socket.gethostname()`、`importlib.metadata.distributions()` 的 `{name: version}`、`torch.__version__` / `torch.version.cuda` / `torch.backends.cudnn.version()`（`ImportError` → `null`）。vcp 端補 `gpus` / `nvidia_driver`（`nvidia-smi --query-gpu=name,driver_version --format=csv,noheader`，找不到即空）、`vcp_version`、`git`（`git -C <cwd> rev-parse HEAD` 與 `git status --porcelain` 是否為空；不在 repo → `null`）、`taken_at`。探針非零退出或輸出非 JSON → ABORT，訊息含探針 stderr。

## 8. Python API（`vcp.train`）

### 8.1 `MaterializedReader`

```python
reader = MaterializedReader("rsna-knee", "png-r256", plan_id="fixed-v1", subset="train")
for rec in reader:                      # 依 samples.jsonl 順序
    rec.sample_id, rec.labels           # Labels | None（label_source=none 時）
    rec.arrays["0"]                     # 逐 view 列：鍵 = str(view 索引)
    rec.arrays["SER123"]                # --stack-seq 列：鍵 = seq_id，S×H×W
rec = reader["1.2.826…"]                # 依 id 取
len(reader); reader.ids
```

- 建構參數：`name`、`mode_dir`（`cache/materialize/` 下的目錄名，如 `npy`、`png-r256`）、`plan_id` / `subset`（給了才限子集，走 `Dataset.subset`，sealed 需 `unseal=True, reason=`）、`data_root` / `configs_root`、`verify=False`（True 時讀取後重算 sha 比對 manifest）。
- 建構時載 dataset 與 manifest；子集中任一 sample 在 manifest 沒有任何列 → `ValidationFailed`（列出前幾個 id）；`mode_dir` 不存在 → `ValidationFailed`。
- 陣列惰性載入：`.npy` 用 `np.load`，`.png` 用 Pillow 轉 `np.asarray`；只依賴 numpy / Pillow / pydantic（框架 venv 以 editable 裝 vcp 即有）。
- 不做任何增強、不做 batch；torch `Dataset` 包一層是使用者的十行（README 示範）。

### 8.2 `Session`

```python
from vcp.train import Session
s = Session.current()                   # 讀 VCP_RUN_ID / VCP_DATA_ROOT；不在 vcp train run 底下 → ValidationFailed
s.register_checkpoint("ckpt/epoch12.pt")            # sha256 + 事件 + 重寫 train.yaml
s.register_checkpoint("ckpt/best.pt", final=True)   # 沒給 --final 時作為 weights_hash 的來源
s.note("val_auc", 0.912)                            # note 事件
```

`train.yaml` 在命令執行期間只有 Session 會寫（包裝器在開始前與結束後才寫），所以沒有並行寫入。

## 9. 錯誤處理與判決字彙

| 情況 | 例外 / 狀態 | 狀態 | 額外欄位 |
|---|---|---|---|
| run 已存在無 `--resume`、`--resume` 但 config 不同、run 是 ingest / fuse 產的、`trained_on` 缺或不一致、`--final` 命中 ≠ 1、檔名撞名、驗證失敗、沒有 `--` 命令、`--config` 檔不存在 | `ValidationFailed` | FAIL | `run=` `attempt=` `checkpoint=` `dest=` |
| export manifest 與 dataset / plan / samples_hash 不符、plan 與 dataset 不符 | `PlanMismatchError` | ABORT | `export=` |
| venv 的 python 不存在、探針失敗、rclone 不在 PATH、`nvidia-smi` 以外的系統錯誤 | `VcpError` | ABORT | — |
| 訓練命令 exit ≠ 0 或被中斷 | 不是例外 | FAIL `exit_code=` | `run=` `attempt=` |
| seed 缺、venv 繼承、final 缺、有 checkpoint 無副本 | — | WARN | — |

`reason=` 固定字彙：`run_exists`、`run_bound_elsewhere`、`final_ambiguous`、`name_collision`、`trained_on_mismatch`（訊息前綴，同量測 / 融合層）。

## 10. 與其他子專案的介面

- **資料層**：讀 export `manifest.json`（`dataset`、`samples_hash`、`plan_id`、`subset`、`format`、`sample_count`、`files`）、materialize `manifest.jsonl`（`ManifestRow`）、`Dataset` / `SplitPlan` / `Dataset.subset`；零改動。
- **量測層**：寫 `RunCard`（`save_run` / `load_run`）；`ingest --run R` 對訓練 run 不必再給 `--trained-on` / `--framework`（給了必須一致，既有規則）；零改動。
- **融合層**：無接口（融合 run 不是訓練 run；`train run` 拒絕覆蓋任何既有非訓練 run）。
- **提交層（5）**：讀 `run.yaml` 的 `weights_hash` 與 `train.yaml` 的 `uploads`，作為「從權重全集重跑」的憑證。
- **備份審計（6）**：`uploads` 清單就是要以 `rclone check` 級驗證的標的；`train status --verify` 是本機側的一半。
- **文件**：README 新增「訓練層命令 `vcp train`」一節（三個命令、一次訓練到 ingest 的流程、`MaterializedReader` + torch 十行示範、`Session` 用法）；CLAUDE.md 台帳路徑加 `runs/<id>/train.yaml`（快照）與 `train.log.jsonl`（只增），常用命令加 `uv run vcp train run --run R --dataset D --plan P --export DIR --venv ENV --checkpoints "…" --final "…" -- <命令>`。

## 11. 測試策略

- **單元**：schema（status / kind / source 的 Literal、`extra="forbid"`）；`trained_on` 推導（manifest 不符的每一種 → `PlanMismatchError` 帶 `export=`；明寫與推導不一致 → FAIL）；`config_hash` 兩條路；run 目錄的四種既有狀態（無、訓練 run、ingest run、fuse run）；子程序執行以 `python -c` 假腳本（寫假 checkpoint、印幾行、依參數 exit 0 / 1）——console 檔內容、exit code、attempt 欄位、環境變數（假腳本印出 `VCP_RUN_ID` / `VCP_SEED`）；中斷（假腳本 sleep，測試送 terminate）；checkpoint glob 登記與 `--final` 的 0 / 1 / 多命中；本機上傳的複製、讀回驗證、冪等、撞名；rclone 上傳以假 runner（記錄呼叫、模擬 copyto 與 hashsum 輸出，含一個 sha 不符的案例 → FAIL）；環境快照在假 venv 目錄（放一個印固定 JSON 的 python 替身腳本）上測，探針失敗 → ABORT；`MaterializedReader` 用 Plan 2b 的合成 materialize 夾具（逐 view 與 `--stack-seq` 兩種列、缺列 → FAIL、`verify=True` 抓改動）；`Session` 在設了環境變數的子程序裡登記 checkpoint 與 note，父程序看得到。
- **端到端（CLI）**：合成 det 資料集 → export yolo → `vcp train run --export … --checkpoints … --final … --upload <tmp 目錄> -- python fake_train.py` → `train status` → 造預測 → `vcp eval ingest --run R`（不給 `--trained-on`）→ `eval measure`；`--resume` 產生第二個 attempt；`train upload --dest <另一個 tmp 目錄>` 後 `status` 的 `unbacked=0`；`train upload` 再跑一次 `skipped=` 等於檔數。
- **真資料**：RSNA 子集 materialize（`png --resize 256`）後 `MaterializedReader` 逐 sample 讀回，shape 與 manifest 一致；資料缺席即 skip。
- 覆蓋率 ≥ 80%；ruff 乾淨；不新增執行期依賴。

## 12. 驗收條件

1. `vcp train run` / `upload` / `status` 各在合成資料集上跑通一次，產出符合 §4、§5 的檔案。
2. `run.yaml` 直接被 `vcp eval ingest` 與 `measure` 接受；`trained_on` 由 export manifest 推導，不必人填。
3. 登記過的 checkpoint 改一個位元：`train status --verify` WARN `drift=1`，`train upload` 拒傳該檔（sha 與登記不符）。
4. 上傳後目的地檔案的 sha256 與 `train.yaml` 紀錄相同；再跑一次 `upload` 全部 `skipped`。
5. `MaterializedReader` 在合成夾具與 RSNA 子集讀回的 shape 與 manifest 一致；`Session` 登記的 checkpoint 出現在 `train.yaml`。
6. `uv run pytest` 全綠、覆蓋率 ≥ 80%、ruff 乾淨；README 與 CLAUDE.md 依 §10 更新。

## 13. 不在範圍

框架適配器與 config 產生、超參搜尋 / 排程 / 佇列、分散式與多機訓練、Kaggle / Colab notebook 產生器、跨機器搬 run 目錄的工具、備份完整性稽核（子專案 6）、rclone 設定與憑證管理、推論與預測轉檔（`vcp eval ingest`）、torch `Dataset` 包裝、資料增強、訓練指標的即時視覺化。以上皆為已預留的擴充點，不是設計缺口。

## 14. v2 補充決定（Plan 5 實作與審查的定案，2026-09-05）

以下為實作期間由計畫或審查裁決、原 spec 未明說或已被推翻的規則，與前文衝突時以本節為準。

1. **`ConfigRef` 的欄位名是 `copied_to`**（§4.2 的 yaml 鍵 `copy:` 作廢）：`copy` 會遮蔽 pydantic `BaseModel.copy` 並在 import 時發警告。
2. **`--` 無法偵測**：Typer / Click 把 `--` 之後的參數原封放進命令並拿掉 `--` 本身，所以「沒有 `--` 就 FAIL」改為「命令非空且第一個 token 不以 `-` 開頭，否則 FAIL」；`--` 仍是文件上的分隔符（訓練命令的選項名與 vcp 的撞名時必要）。
3. **包裝器在命令結束後重讀 `train.yaml`**：§8.2「命令執行期間只有 Session 會寫，所以沒有並行寫入」是真的，但不夠——包裝器在命令後的寫入是整檔重寫，必須先從磁碟重讀（否則 Session 登記的 checkpoint 與 final 會被蓋掉）。§6.1 第 7 步的「結束記 exit code」隱含這條。
4. **checkpoint 的「現在」是每個檔名最新的一筆**：同一路徑可能有多筆 `(path, sha256)` 紀錄（`--resume` 換了權重）。上傳 / 備份的標的是每個檔名最後登記的一筆；舊紀錄是歷史，不是撞名。撞名只指「不同路徑、同檔名、不同 sha」。`train status` 的 backed / unbacked 以 sha256 判斷、涵蓋全部紀錄。（後半的「舊 bytes 沒有副本就列為 unbacked」見第 12 條修訂。）
5. **`--resume` 換寫 `weights_hash` 要留痕**：既有 `weights_hash` 非空且與新 final 不同時，先在 `history.jsonl` 記 `{"event": "replace", "field": "weights_hash", "old_sha256", "via": "train.run"}` 再寫 `run.yaml`；有 predictions 的 run 一樣允許 resume（讀數綁的是 `prediction_sha`，不是權重）。
6. **`uploaded` 事件只記新出現的副本**：`(dest, name, sha256)` 已在 `uploads[]` 裡的重驗證只更新 `train.yaml`（`uploaded_at`），不追加事件；已在目的地且 sha 相同的檔一樣回 `verified=True` 的紀錄（手動放上去的副本也算）。
7. **命令可執行性預檢對 `--cwd` 解析**：`command_found(token, cwd, PATH)` = PATH 上找得到、或 `cwd / token` 是檔案（絕對路徑自然成立）。
8. **殘留的 `running` attempt 由 `--resume` 調和**（2026-09-07 修訂，原「只揭露不調和」作廢）：`train status` 仍只揭露（唯讀，WARN `running=`），但 `--resume` 在寫新 attempt 之前，把每個還是 `running` 的 attempt 標成 `interrupted`、補 `finished_at`、`exit_code` 維持 `None`（沒有結束碼就不編一個出來），並在 `train.log.jsonl` 記一列 `note`（`attempt=<n>`、`value="attempt <n> found running at resume; marked interrupted"`），yaml 的新狀態不會是唯一的痕跡。編號照常 +1。
9. **環境探針**：探針程式的 cuDNN 行以 `if` 守衛寫法（等價於 §7 的條件式）；`gpus()` 以每列的第二欄為 driver，缺欄位的列略過。
10. **`assert_plan_matches(plan, card)` 新增於 `vcp/data/split.py`**：本層唯一的資料層改動；既有三份同義檢查留待後續統一。→ 2026-09-07 已統一：`data/dataset.py`、`measure/ingest.py`、`fuse/members.check_plan` 都改呼叫它，全庫只剩這一份 plan hash 檢查。
11. **每個 attempt 記自己的 `command` / `seed` / `venv`**（`Attempt` 的三個選填欄位，2026-09-07）：`--resume` 可以換命令（`--config` 釘住 `config_hash` 時）、換 seed、換 venv，紀錄要說得出「第 n 次是拿什麼跑的」。紀錄層的 `command` / `seed` / `venv` 刻意維持**第一個 attempt** 的值——它描述這個 run 是怎麼開始的，`run.yaml` 的 `config_hash` 也綁在那一刻；要看最新的就看 `attempts[-1]`（`train status` 的人類行印的就是它）。舊的 `train.yaml` 三個欄位皆無，照樣載入（`None`）。
12. **`train status` 另計 `superseded=`**（2026-09-07，修訂第 4 條後半）：同一路徑被後來的登記取代、又從沒上傳過的舊 bytes，不再算進 `unbacked=`，改成 `superseded=`（`--json` 另附路徑清單），也不列入 WARN 條件。理由：那些 bytes 已經不在檔案裡，副本再也不可能出現，永遠掛在 `unbacked` 只是噪音；`unbacked=` 因此收斂成「目前的 bytes 沒有副本」——唯一可行動的數字。`backed=` 的定義不變（sha256 有驗過副本的紀錄數，含舊的）。
13. **`--cwd` 與兩個 CLI 細節**（2026-09-07）：`--cwd` 不是目錄 → `ValidationFailed("not_found: --cwd <path> is not a directory")`，與其他預檢一起在第一次寫入之前（原本是 `Popen` 丟裸 `OSError`〔ABORT〕，而且 `run.yaml` 與一個 `running` attempt 已經落地）；`not_found:` 沿用全庫既有前綴，§9 的 `reason=` 清單照此補。`--final` 沒解出來時的 WARN 由「`final=skipped (command failed)`」改為「`final=skipped (attempt <status>)`」（`failed` / `interrupted`）。`train upload` 與 `train status` 移除宣告了卻沒用的 `--configs-root`（兩者只讀資料根目錄下的 run），只有 `train run` 保留。
