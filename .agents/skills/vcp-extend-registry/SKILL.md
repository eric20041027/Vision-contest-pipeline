---
name: vcp-extend-registry
description: Use when adding or changing a registry entry in this repo — a new importer, exporter, decoder, audit check, split strategy or task type under src/vcp/data — or when asked "what must an exporter/importer return" or "where do I register X".
---

# 擴充 vcp 的登記表

## 核心原則
六個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核）都是登記表：加一種形態 = 一個新模組 + 一行登記，不改 schema、不改 CLI 行為。`src/vcp` 不得出現比賽名稱或比賽專屬欄名；比賽專屬轉換放 `projects/<contest>/`，出口是 `jsonl` 匯入器。
每個軸的精確契約（簽名、回傳型別、框架替你做的事）在 [contracts.md](contracts.md)。先讀對應那一節，再讀一個現有的同軸實作當範本（匯出器看 `yolo.py`，匯入器看 `csv_boxes.py`，稽核看 `coords.py`，解碼器看 `dicom.py`）。

## 工作流程
1. 讀 contracts.md 該軸的一節，記下「框架替你做」的清單，別重做（manifest、hash、sealed 開封、VERDICT 狀態對應、跳過列檔案、輸出目錄檢查都是框架的事）。
2. 先寫失敗測試：放在該軸既有的測試檔（匯出器全在 `tests/unit/data/exporters/test_exporters.py`；匯入器一檔一個 `tests/unit/data/importers/test_<名>.py`；稽核在 `tests/unit/data/audit/test_checks.py`；解碼器與 materialize 在 `tests/unit/data/materialize/`）。用 `roots` fixture 與 `tests/helpers.py`（`det_samples`、`make_card`、`write_images`、`write_dicom_study`、`write_exif_image`），永不碰真實根目錄。
3. 實作：一個模組一個類別，結構相符即可（Protocol，不繼承）。類別屬性：匯入器 / 匯出器 / 解碼器要 `name` 與 `version`（version 進 manifest，變了會讓快取重做）；稽核檢查與任務只要 `name`；切分策略是函式。
4. 登記：該套件的 `__init__.py` 依字母順序 import、`register_*(Instance())`、加進 `__all__`（順序 = 稽核執行順序）。
5. 既有測試會因登記表變長而改變：`grep -rn "AUDITS\|EXPORTERS\|IMPORTERS\|DECODERS" tests/` 找出斷言登記表內容的測試，`grep -rn "cmd=audit\.\|coords=OK\|dedup=" tests/` 找出斷言 VERDICT 字面值的 CLI 測試；新檢查會多一行 `VERDICT cmd=audit.<名>` 和一個 `<名>=<狀態>` 欄位，用共用夾具（`det_samples` 是隨機框）時先算清楚它會回什麼狀態再改期望值。
6. 收尾清單：`uv run pytest -q`、`uv run ruff check .`、`uv run ruff format --check .`；`src/vcp/cli.py` 裡列舉格式的 help 字串（`--format` 的 `coco | yolo`）與 `README.md` 的表格要一起更新；commit 訊息用 `feat(<軸>): ...`。

## 慣例（違反會被審查退回）
- 使用者資料的問題 → `ValidationFailed`（FAIL）；缺套件、程式錯 → `VcpError`（ABORT）。選項值不合法用 `importers.common.choice_option(opts, key, allowed, default)`，布林選項比對 `{"1","true","yes"}`。
- 寫出的文字檔 `encoding="utf-8", newline="\n"`；CSV 用 `open(..., newline="")` + `csv.writer(f, lineterminator="\n")`；讀檔一律指定 `encoding="utf-8"`。
- 取時只用 `vcp.core.time.stamp()`（ruff TID251 會擋 `datetime.now`）。
- 多 view 樣本：匯出器用 `select_view(sample, options.get("view"))`，只輸出該 view 的框，其餘計入 warning `"<n> boxes on non-exported views dropped"`。
- 兩個模組要共用的 helper（如 `yolo._place_image`）先搬到該軸的 `base.py` 或 `common.py` 再共用，不要跨模組 import 私有名稱。
- 匯出器回傳的每個檔案都要放進 `ExportOutput.files`，否則 manifest 的 sha256 表會漏掉它而沒有任何錯誤。

## 常見錯誤
- 在匯出器裡自己寫 manifest 或處理 sealed 子集（都是 `export_subset` 的事）。
- 新匯入器忘了走 `finalize_import`（驗證、raw manifest、card、`import_skipped.jsonl` 都在裡面）。
- 稽核檢查的 `applies` 對所有資料集回 True（DICOM 或目錄 view 會讓 Pillow 崩）。
- 加了登記項卻沒更新 `--format` help 字串與 README 表。
