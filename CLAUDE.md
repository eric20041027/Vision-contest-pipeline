# vcp 操作慣例（給 agent 與人看）

## 三條機械鐵則
1. 取時只能用 `vcp.core.time.utc_now()` / `stamp()`。ruff TID251 會擋 `datetime.now` / `utcnow` / `today` / `time.time`；唯一例外是 `src/vcp/core/time.py`。
2. 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 1（FAIL）/ 2（ABORT）。命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。
3. venv 隔離：核心 `vcp` 一個 venv（`uv sync`）；訓練框架各自 venv，以 editable 裝 `vcp`；量測 venv 凍結後禁 install。

## 通用性原則
以資料形態與任務類型為軸，不以特定比賽為軸。比賽專屬程式碼放 `projects/<contest>/`，不進 `src/vcp`。六個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核）都是登記表；加一種形態 = 加一個登記項，不改 schema、不改 CLI。

## 路徑
- 資料根目錄：`VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）。`raw/<name>/` 永不修改；`datasets/<name>/` 放 `samples.jsonl`、`raw_manifest.txt`、`cache/`。
- 設定根目錄：`VCP_CONFIGS_ROOT`（預設 repo 的 `configs/`）。`configs/datasets/<name>/dataset.yaml` 與 `splits/*.json` 進 git；plan 檔一旦寫入不可修改，要改就換 plan-id。
- 整合測試讀 `VCP_REALDATA_ROOT` / `VCP_REALDATA_CONFIGS`，資料缺席即 skip；`raw/<name>/` 永不修改。
- `datasets/<name>/cache/materialize/<mode>/` 是可搬移的解碼快取，`manifest.jsonl` 為權威對照。

## 常用命令
- `uv sync` / `uv run vcp --help` / `uv run pytest --cov=vcp` / `uv run ruff check .`
- `uv run vcp data export --name X --plan P --subset S --format coco|yolo --out DIR` / `uv run vcp data audit --name X [--against TEST]`
- `uv run vcp data materialize --name X --mode npy|png [--resize L] [--stack-seq]`

## 文件
- 設計 spec：`docs/superpowers/specs/`；實作計畫：`docs/superpowers/plans/`；賽後報告：`docs/postmortems/`
