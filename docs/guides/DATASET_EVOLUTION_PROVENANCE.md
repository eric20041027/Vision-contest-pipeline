# Dataset Evolution 與 Impact Provenance

## 儲存與權威邊界

`vcp data diff` 會把兩個 dataset version 的變更寫成不可變
`artifacts/dataset_diff/<id>/`。`changes.jsonl` 只含 ADDED、REMOVED、MODIFIED；完全相同的
版本仍有 `summary.json`，但不製造假事件。dataset、run、reading、manifest、ledger 仍是權威；
`<data_root>/indexes/provenance.sqlite3` 只是可刪除的查詢索引，不進 Git。

```powershell
uv run vcp data diff --from dataset-v1 --to dataset-v2 --id v1-to-v2
uv run vcp provenance rebuild
uv run vcp provenance ingest --artifact v1-to-v2
uv run vcp provenance impact --dataset dataset-v1 --sample SAMPLE_ID
uv run vcp provenance stale --head dataset-v2
uv run vcp provenance explain --entity run:model-a
uv run vcp provenance verify-index
```

所有命令都支援 `--json`、`--data-root`、`--configs-root`。`data diff` 發布 canonical artifact；
其餘查詢不修改 canonical 記錄。

## Status 語意

- `VALID`：現有 canonical evidence 可證明沒有受到影響。
- `STALE`：training、evaluation、input 或 split change 已使 claim/reading 過期。
- `REVIEW`：例如未知 nested metadata，證據不足以安全分類。
- `BROKEN`：必要 dataset、plan、prediction、receipt、manifest 或其他 upstream 缺失/損壞。

樣本級查詢先用 split assignment 限縮 subset。RunCard 上有效 receipt 最強，其次是 verified export
identity，最後才是 declared dataset/subset；`provenance_grade`、observed subsets 和 access refs 都保留在
graph entity attributes 中。無法證明時不猜成 VALID。

## 同步、修復與失敗邊界

- 新 diff：`provenance ingest --artifact ID`，同一 manifest 重跑為 no-op。
- 新 run/reading/artifact 等 canonical record：`provenance sync`。只允許新增；既有 indexed record
  消失或改寫會 fail closed，要求人工查明後 `rebuild`。
- `verify-index` 會以 full canonical replay 比 graph、每個 head 的 status 與 recorded graph hash。
- ledger checkpoint 記 consumed bytes/prefix SHA；既有 prefix 被改會 `prefix_drift`。
- rebuild 在 temporary SQLite 完成 graph/status parity 與 snapshot recheck 後才 atomic replace。
- 任一 ingest transaction 中斷皆 rollback；刪除整個 SQLite 後執行 `rebuild` 可恢復。

目前 writer model 是單一 SQLite writer。WAL 允許一般 concurrent readers；沒有宣稱多 writer
協調。Committed diff artifact 不得刪改；修正請換新 ID。

## Policy plugin

核心只分類通用 top-level fields。未知 `meta.*` 預設 `UNKNOWN`。比賽 adapter 可在自己的模組呼叫
`vcp.provenance.policy.register_impact_policy(name, version, classifier)`，再以 `data diff --plugin MODULE
--policy NAME` 使用；比賽名稱或 nested key 不得進 `src/vcp`。

## 驗證與基準

```powershell
uv run python tests/performance/provenance/real_validation.py `
  --data-root C:\path\to\vcp-data --configs-root C:\path\to\configs `
  --output docs/benchmarks/provenance-real.json

uv run python tests/performance/provenance/benchmark.py `
  --output docs/benchmarks/provenance-scaled.json

uv run python tests/performance/provenance/production_benchmark.py `
  --output docs/benchmarks/provenance-production.json
```

real runner 只從來源讀取，將四個 dataset 的 metadata、samples、相關 runs/readings 複製到 temporary
directory，再於副本建立 diff/index。scaled runner 使用固定 seed 和 opaque IDs，預設跑 1k、10k、
100k、1M 事件；大型測試不進一般 CI。2026-09-13 的 machine-readable evidence 位於
`benchmark.py` 是 schema-preserving algorithm microbenchmark；不得把它的 speedup 當成正式 index
效能。`production_benchmark.py` 才會建立 production SQLite schema、發布已驗 `dataset_diff` 並直接呼叫
`ProvenanceIndex.ingest_diff()`。證據檔為 `docs/benchmarks/provenance-real-2026-09-13.json`、
`docs/benchmarks/provenance-scaled-2026-09-13.json` 與
`docs/benchmarks/provenance-production-2026-09-13.json`。
