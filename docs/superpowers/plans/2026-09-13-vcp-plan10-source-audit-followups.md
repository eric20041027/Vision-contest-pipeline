# Plan 10 後記：來源稽核與選取列存取（稽核 Wave 1b-2，v0.6.0）

計畫：`2026-09-13-vcp-plan10-source-audit.md`；spec：`../specs/2026-09-13-vcp-source-audit-design.md`（§15 記 13 條實作期決定）。分支 `wave1b2-source-audit`，base `main` 19864ac（v0.5.0）。6 個任務各經一次任務審查，Task 1 走了一輪修正、Task 5 一次審前修正、Task 6 一次解阻；最終整支審查（fable）給 2 個 Important，一輪修正後乾淨。全套 `tests/unit` 1153 passed / 2 skipped（torch 專用），覆蓋率 96.66%。

## 1. 執行期裁決（spec §15 之外）

spec §15 第 1–13 條是設計層面的裁決；下面是計畫或 brief 有缺漏時的處置：

1. 最終審查發現 `vcp data export` 只在 `warnings=` 文字裡帶 `source_audit=missing`、沒有獨立 VERDICT 欄位（1b-1 時的假設錯了）→ 補欄位與 CLI 測試。
2. 已 commit 但驗不過的稽核沒有處置路徑（內容定址、無 supersedes、`clean` 只清半途）→ 所有 `mismatch:` 訊息直接說「搬走 `artifacts/source_audit/<id>/` 再跑 `vcp data validate`」；結構性方案（讓 `source_audit` 走 `supersedes`，或加 `vcp artifact quarantine`）留給後續 wave。
3. 半途稽核目錄的 `partial:` 訊息提示 `vcp artifact clean --older-than 0 --apply`。
4. `tests/integration/test_source_audit.py` 與 `tests/unit/data/test_source_audit.py` 同名讓整套收集 import file mismatch → 改名 `test_audited_access.py`（不加 `tests/integration/__init__.py`，那會壞掉 `from conftest import …`）。
5. F1 的 CLI 測試要 `_import_tiny(..., with_images=True)`（yolo 匯出要真圖檔）——測試鷹架修正，斷言不變。

## 2. 已知限制

- 真資料整合測試 `tests/integration/test_audited_access.py` 在開發機 skip（`C:/vcp-data/datasets` 不在）——稽核路徑尚未在真的 200 個 study 的 `samples.jsonl` 上跑過；有資料的機器請跑 `uv run pytest tests/integration -o addopts="" -q -m realdata`。
- 稽核只覆蓋 `samples.jsonl`；materialize 快取靠自己的 `manifest.jsonl` 逐檔 sha（reader 只驗讀到的檔）；單一大陣列模式不在本層（spec §13）。
- VCP-002 在 `train run` / `measure` / `export` / `stage` 關閉；`judge` / `sigma` / `anchor`、`fuse build` / `ablate`、`ingest`、`data audit`、`materialize`、`submit init` / `verify` 仍走 `Dataset.load`（解析 + 整檔 hash）——準備期的那些本來就該如此，量測期的（judge / sigma / anchor）是下一個候選（1b-1 後記 §4.2）。
- 每次 open 的成本 = 讀 `index.jsonl` 兩趟（`store.verify` hash 一次、逐列 `IndexRow` 解析一次）+ 三次 `manifest.json` 小 hash；O(索引) 而非 O(語料)×3，百萬列時逐列 pydantic 解析會是主要成本。

## 3. 開放待辦（依優先順序）

1. `load_source_audit` 改單趟（hash 與解析同時做）；`write_source_audit` 抽 `_index_lines()` 降巢狀深度。
2. 稽核壞掉的結構性處置：`source_audit` 允許 `supersedes`，或加 `vcp artifact quarantine`（把驗不過的產物搬到 `artifacts/<kind>/.quarantine/` 並記台帳）——同一個死角會出現在任何未來的內容定址 kind（`array_audit`）。
3. 兩個 `validate` 同時寫同一稽核時輸家看到 `exists: … pick a new id, or supersede it`——對內容定址 kind 訊息不對，應說「另一個 validate 正在寫，稍後重跑」。
4. `bad source audit:` / `bad index row:`（自洽但格式壞掉的 audit.json / index 列）沒有 `mismatch:` 前綴（仍 FAIL、fail closed）——統一字彙或寫進 spec §9。
5. 測試缺口：空 `samples.jsonl`（`line_count 0`）、CRLF 檔案、`peek_sample_id` 遇到非法 JSON escape（`\z`）丟 `JSONDecodeError` 而非 `ValidationFailed`（沿自 `index_samples`）；e2e 首次匯出在沒 Developer Mode 的 Windows 上是 `WARN images=copied`。
6. 備份：`AccessRef` 沒有 `source_audit_sha256`，證據圖只能把稽核檔當無擔保檔（缺席 → `unlisted`）；要 vouch 得在 `AccessRef` 加欄位（MINOR）。
7. 文件：HANDOVER 程式碼地圖缺 `src/vcp/data/access/` 與 `source_audit.py`（1b-1 起）、真資料測試數字過時；README 命令表的 `measure` / `train run` / `stage` 列沒提 `identity=` / `source_audit=missing`（v0.6.0 段落有）；`data export --json` 的 payload 缺 `identity`（`fields` 有）。
8. 型別：`LoadedAudit.index` 與存取器放寬後的索引型別（`str | None`）在 mypy 下不相容（repo 未跑 mypy）。
9. 1b-1 後記 §4 的待辦全數仍在（`receipt_empty=` / `receipt_partial=`、judge / sigma / anchor 走存取器、`eval_gold_only`、unseal sha 驗證、多程序 `train.yaml`、選取列陣列存取器…）。
