# vcp 操作慣例（給 agent 與人看）

## 三條機械鐵則
1. 取時只能用 `vcp.core.time.utc_now()` / `stamp()`。ruff TID251 會擋 `datetime.now` / `utcnow` / `today` / `time.time`；唯一例外是 `src/vcp/core/time.py`。
2. 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 1（FAIL）/ 2（ABORT）。命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。
3. venv 隔離：核心 `vcp` 一個 venv（`uv sync`）；訓練框架各自 venv，以 editable 裝 `vcp`；量測 venv 凍結後禁 install。

## 通用性原則
以資料形態與任務類型為軸，不以特定比賽為軸。比賽專屬程式碼放 `projects/<contest>/`，不進 `src/vcp`。十個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核、轉換器、指標、σ_p 方法、融合器）都是登記表；加一種形態 = 加一個登記項，不改 schema、不改 CLI。

## 路徑
- 資料根目錄：`VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）。`raw/<name>/` 永不修改；`datasets/<name>/` 放 `samples.jsonl`、`raw_manifest.txt`、`cache/`。
- 設定根目錄：`VCP_CONFIGS_ROOT`（預設 repo 的 `configs/`）。`configs/datasets/<name>/dataset.yaml` 與 `splits/*.json` 進 git；plan 檔一旦寫入不可修改，要改就換 plan-id。
- 整合測試讀 `VCP_REALDATA_ROOT` / `VCP_REALDATA_CONFIGS`，資料缺席即 skip；`raw/<name>/` 永不修改。
- `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/` 是可搬移的解碼快取，`manifest.jsonl` 為權威對照。
- `measure/<name>/` 只增不改：`readings.jsonl`、`judgements.jsonl`、`sigma.jsonl` 是 append-only 台帳，錨點 `anchors.json` 整份換寫但每次都先寫 `anchors.log.jsonl`。`runs/<run_id>/`（`run.yaml`、`predictions/<subset>.jsonl`）不是只增不改，而是「換寫留痕」：`ingest --replace` 會重寫 `run.yaml` 與該子集的預測檔，舊 sha 進 `history.jsonl`（`history.jsonl` 本身只增不改）。預登記 `configs/datasets/<name>/prereg/<id>.yaml` 與 `prereg.log.jsonl` 進 git，寫下就不改，要改就換 id。融合配方 `configs/datasets/<name>/fuse/<id>.yaml` 進 git、寫了不改；融合 run 是普通 run，另有 `runs/<id>/fuse.json`（每次 build 整份換寫，記每個成員預測檔的 sha 與輸出 sha）。

## 常用命令
- `uv sync` / `uv run vcp --help` / `uv run pytest --cov=vcp` / `uv run ruff check .`
- `uv run vcp data export --name X --plan P --subset S --format coco|yolo --out DIR` / `uv run vcp data audit --name X [--against TEST]`
- `uv run vcp data materialize --name X --mode npy|png [--resize L] [--stack-seq]`
- `uv run vcp eval measure --run R`（護欄 → 讀數；`--unseal --reason` 才動 sealed 子集）/ `uv run vcp eval judge --dataset D --prereg ID [--strict]`
- `uv run vcp eval status --dataset D` / `uv run vcp eval report --dataset D`（兩者唯讀）；比賽自己的指標或格式以 `--plugin projects.<contest>.metrics` 登記
- `uv run vcp fuse recipe --dataset D --id R --plan P --method wbf|mean|rank_mean --member RUN[:W]…` / `uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M`（每位成員一份準入預登記，交給 `vcp eval judge`；先 ablate 再 measure）

## 文件
- 設計 spec：`docs/superpowers/specs/`；實作計畫：`docs/superpowers/plans/`；賽後報告：`docs/postmortems/`
