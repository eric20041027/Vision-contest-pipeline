# Plan 11 後記

## 2026-09-23 SQLite graph gaps 修復

RSNA Knee 的 canonical replay 有 22 條 invalid-manifest gaps。舊 SQLite index 的 entity、edge、change、transition 與 replay 一致，但讀回時 `gaps=[]`，導致剛 rebuild 的 `verify-index` 仍回報 `graph differs from canonical replay`。PostgreSQL backend 已保存 `graph_gaps` metadata。

SQLite 現在於 rebuild/sync 寫入排序後的 `graph_gaps` metadata，讀回時嚴格驗證型別與順序；缺失或損壞時 fail closed 並要求 rebuild。保留 SQLite schema version 2，因為表結構不變，且舊版 reader 可忽略新增 metadata；新版 reader 對缺少 metadata 的舊衍生索引要求 rebuild。Ingest 不改動 gap 清單，使用原索引的 metadata。

回歸覆蓋 invalid manifest 的 rebuild、sync、metadata 篡改與舊索引重建。此修復不改 canonical records、graph hash 演算法或 PostgreSQL backend。RSNA 的 pinned v0.8.1 訓練環境不在本次分支上變更；正式遷移須獨立驗證其環境與索引重建時機。

驗證：`tests/unit/provenance/test_index.py` 20 passed；全套 `pytest --cov=vcp -q` exit 0、coverage 94.88%；ruff check/format 通過。用 RSNA 真實 roots 對系統暫存 SQLite 索引執行 rebuild + verify，得到 217 entities、347 edges、22 gaps、hash `4c98320eaa666076ce025de747fed62537b94711d7776ed7f3fca65b29b5c14b`，`verify_ok=true`。現用 RSNA 索引和 v0.8.1 pinned worktree 沒有修改。
