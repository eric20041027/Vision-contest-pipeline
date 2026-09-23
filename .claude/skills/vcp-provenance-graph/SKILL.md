---
name: vcp-provenance-graph
description: Use when someone wants to see, draw, export or share the provenance or lineage graph of a vcp contest folder — 「畫 provenance 圖」「這個比賽的 lineage 長怎樣」「給我看哪些 run 壞了 / 過期」, a picture for a review, handover or slides — or when a `vcp provenance graph` VERDICT, WARN or FAIL needs explaining.
---

# 畫比賽的 provenance 圖

## 先記住
- 圖畫的是**索引**：`vcp provenance graph` 只讀 `<data_root>/indexes/provenance.sqlite3`（或 `--backend postgresql --pg-service S`），只寫 `--out` 一個檔。檔頭與 VERDICT 的 `hash=` 對得回 `rebuild` / `sync` 印的 `hash=`。
- 要 vcp ≥ 0.9.0（`uv run vcp provenance graph --help` 有這個命令）。比賽的 venv 釘在舊 tag 時，從 vcp 的 checkout 執行並用 `--data-root` / `--configs-root` 指過去；不要為了畫圖改比賽的 venv。
- 不自己寫腳本讀 SQLite 畫圖；圖不寫進 data root（FAIL `out_in_data_root:`）。

## 步驟
1. **找 root**：比賽 repo 的 `ENVIRONMENT.md` / `AGENTS.md` 寫的 `VCP_DATA_ROOT`、`VCP_CONFIGS_ROOT`；沒寫才用 `<repo>/vcp-data`、`<repo>/configs`。每個命令都帶 `--data-root` / `--configs-root`。
2. **索引能不動就不動**：
   - 在，而且沒有比它新的紀錄 → 直接畫。換了 vcp 版本不是 rebuild 的理由：索引 schema 沒變，真不相容時 graph 會 FAIL 並叫你 rebuild。
   - `runs/`、`measure/`、`artifacts/`、`submit/` 有檔案比索引新（[reference.md](reference.md) 有一行檢查）→ `vcp provenance sync`（只收新增；看到刪改會 fail closed，再改 `rebuild`）。
   - 不在 → `vcp provenance rebuild`。
   - `rebuild`、`sync`、`status`、`verify-index` 都重算每個 canonical 檔的 sha（RSNA 規模約 3 分鐘）：先講時間；訓練正在跑、或別的 session 正在用同一個 data root，先問。不必先跑 `stale`：狀態已經畫在圖上。
3. **畫**（預設 html）：
   ```bash
   uv run vcp provenance graph --data-root <data_root> --configs-root <configs_root> --out <repo>/reports/provenance-graph.html
   ```
   `.html` 用瀏覽器開（Mermaid 從 jsDelivr 載入，要網路）；要進 GitHub 或文件用 `.md`；貼 mermaid.live 用 `.mmd`。同一路徑可重畫：只覆寫自己產的圖，別的既有檔一律 FAIL `exists:`。
4. **交付**：打開檔案（Windows：`Start-Process <file>`）或把路徑交給使用者，一起回報 VERDICT 的 `nodes` `edges` `broken` `stale` `review` 與 human 輸出列的前幾個原因。
5. **往下追**：單一 run → `--entity run:<id> --detail full`；一個資料系列 → `--dataset <name>`；相對新版 → `--head <新版名>`；文字原因 → `vcp provenance explain --entity …`。

形狀、顏色、WARN 與 FAIL、常見的紅色來源見 [reference.md](reference.md)。

## 常見錯誤
- 沒帶 `--data-root`，畫到預設 root（別的比賽，或空的）。
- 為了「保險」先 `rebuild` 再 `verify-index`：兩次全掃，還整份換掉別人可能正在用的索引。
- 把 `WARN` 當失敗：圖已寫出，WARN 只代表範圍內有 BROKEN / REVIEW，或 `.md` 超過 Mermaid 上限。
- 看到紅色就刪 `indexes/` 或手改 artifact：先讀原因；證據壞了修證據，索引舊了才 `sync`。
