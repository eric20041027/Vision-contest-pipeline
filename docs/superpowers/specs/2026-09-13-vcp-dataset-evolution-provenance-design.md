# VCP Dataset Evolution and Incremental Provenance 設計

- 日期：2026-09-13
- 狀態：v1（依使用者提供並核可的 implementation handoff 落成 repo 內規格）
- 前置：v0.4 immutable artifacts、v0.5 access receipts、v0.6 source audit
- 版本方向：新增命令與正式產物形態，屬 MINOR 變更

## 1. 目的與邊界

VCP 要能把兩個不可變 dataset version 的差異封成可驗證的 `dataset_diff` artifact，並把差異對 run、reading、judgement、fusion、artifact、backup 與 submission 的影響建成可查詢的 provenance graph。SQLite 只是可刪除、可重建的衍生索引；JSONL、YAML、manifest 與 source audit 仍是權威。

不做 dashboard、遠端服務、通用 event bus、逐一記錄 DICOM read、就地修改 dataset、或把 SQLite 變成 canonical store。`src/vcp` 不含比賽名；dataset-specific nested metadata policy 由 plugin 登記。

## 2. 身分與不可變產物

- dataset version id：`dataset:<name>@<samples_hash>`；不修改 `DatasetCard` schema。
- version edge：每個已驗證 `dataset_diff` artifact 明示 `from` 與 `to`，不靠名稱猜 parent。
- diff id：若 CLI 未給 `--id`，由兩端名稱與 hash 決定；同一 id 不覆寫。
- layout：`artifacts/dataset_diff/<id>/{spec.json,changes.jsonl,summary.json,manifest.json}`。
- `ArtifactWriter` 在所有輸入驗完後才 open；`manifest.json` 仍是最後 commit 點。
- inputs 至少 pin 兩份 `samples.jsonl`；source audit 存在時也 pin 其 manifest。

## 3. SampleChange 與分類

`SampleChange` 為 strict pydantic model，含 schema version、deterministic change id、兩端 dataset/hash、sample id、`ADDED|REMOVED|MODIFIED`、changed domains、changed fields、semantic effects、before/after row hash。沒有 `UNCHANGED` event；no-op 只在 summary 記零。

top-level domain：`views -> VIEWS`、`labels -> LABELS`、`label_source -> LABEL_SOURCE`、`group -> GROUP`、`meta -> META`，其餘 `UNKNOWN`。recursive field path 使用 dotted key 與 list index。

預設 effect：

- views：`INPUT_AFFECTING`
- labels / label_source：`TRAINING_AFFECTING` + `EVALUATION_AFFECTING`
- group：`SPLIT_AFFECTING`
- nested meta：`UNKNOWN`
- added / removed：input、training、split、evaluation 全部保守標記

plugin policy 可把 field path 分類為 `INPUT_AFFECTING|TRAINING_AFFECTING|SPLIT_AFFECTING|EVALUATION_AFFECTING|DISPLAY_ONLY|UNKNOWN`；沒有命中的 nested metadata 永遠是 `UNKNOWN`。

## 4. Diff 驗證與演算法

先載 card、核對 `samples.jsonl` 整檔 hash、載入並 verify 可用 source audit。audit 存在時以 `sample_id -> offset,length,row_sha256` 驗證 row identity 並找 candidates；兩端每一列仍在發布前完整 schema-validate，避免 unchanged 但 schema-invalid 的列被略過。audit 缺席走同一完整驗證並在 summary/VERDICT 標 `grade=fallback`。duplicate id、schema 錯、card/hash mismatch 必須在 artifact id 被 claim 前失敗。

輸出以 `(sample_id, change_type)` 排序。change id 對穩定欄位做 SHA-256。summary 包含總數、各 change/domain/effect 計數、兩端 identity、grade 與 artifact id。

## 5. Canonical provenance graph

entity id 與 edge id 都 deterministic。掃描並驗證現有 canonical record，只建證據支持的
entity：dataset version、changed sample version、split plan、verified export pin、materialized
cache、run、reading、append-only judgement event、fusion run、artifact、backup manifest、submission。
edge 包含 `DERIVED_FROM`、`CONTAINS_CHANGE`、`USES_SPLIT`、`CONSUMED_BY`、`PRODUCED_BY`、
`EVALUATED_BY`、`COMBINED_INTO`、`SUPERSEDES`、`BACKED_UP_AS`、`SUBMITTED_AS`。方向永遠由
upstream 指 downstream；缺 evidence 以 gap 或 `BROKEN` 呈現，不補猜測。export manifest 只有
RunCard 的已驗 hash pin 時，以 `evidence=run_card_pin` 明示證據邊界；不虛構已遺失的 path。

sample-level impact 優先級：verified access receipt > verified export manifest > declared dataset/subset。split assignment 可證明 membership 時才精準判斷；否則 `REVIEW`。

status：`VALID|STALE|REVIEW|BROKEN`。training/input change 命中 trained subset 時 run stale；evaluation-only change 只使相關 reading stale；group change 使 plan 與 downstream stale；unknown 使相關實體 review；missing/corrupt required canonical dependency 為 broken。

## 6. SQLite 衍生索引

預設 `<data_root>/indexes/provenance.sqlite3`，單 writer、transactional update。tables 至少含
metadata、dataset_edges、sample_changes、entities、provenance_edges、entity_status、
ingest_checkpoints。unique deterministic ids 保證 duplicate ingest idempotent。rebuild 對所有 dataset
versions 物化 status；ingest 從 source version status 出發，只載 graph topology 和本次 delta，不載歷史
`sample_changes`。graph 同步 fingerprint 使用 record hash 的 order-independent accumulator，權威驗證仍是
canonical replay 的逐 record exact comparison，不以 accumulator 取代資料完整性。

每次 ingest：verify manifest 與 inputs，在一個 transaction 內 insert unseen rows、找 downstream dirty closure、只重算 dirty status、寫 checkpoint，最後 commit。例外 rollback。rebuild 以 temporary DB 完整重建、verify parity 後原子 replace。immutable artifact checkpoint pin manifest hash；append-only ledger checkpoint pin path、bytes、prefix hash。prefix drift fail closed。

## 7. CLI

- `vcp data diff --from A --to B [--id I] [--plugin M --policy P]`
- `vcp provenance ingest --artifact I`
- `vcp provenance sync`（只 ingest 新增 canonical records；既有 indexed evidence 消失/變動則 fail closed）
- `vcp provenance impact --dataset D [--sample S]`
- `vcp provenance stale --head D`
- `vcp provenance explain --entity type:id`
- `vcp provenance status`
- `vcp provenance rebuild`
- `vcp provenance verify-index`

全部沿用 `run_command`、`--json` stdout / VERDICT stderr、exit 0/1/2 與 machine-readable context。index 缺席只影響 provenance 命令，不得影響既有命令。

## 8. 評估與驗收

真實軌只讀重算 r3->r4、r4->r5、r5->r6，驗 no-op、domain、run linkage 與 full/incremental parity。scaled 軌以固定 seed 產 1k、10k、100k、1m schema-preserving opaque events，比 full replay、full SQLite recompute、dirty-subgraph incremental；記錄 p50/p95、rebuild、throughput、index ratio、parity、crash recovery 與環境。

普通 CI 只跑小型 deterministic/property tests；大型 benchmark 明確 opt-in。完成門檻以使用者 handoff 的 acceptance criteria 為準，尤其是 exact parity、idempotency、rollback/rebuild、未知 metadata 保守處理、既有回歸與 scoped/final review。

## 9. 與 active checkout 的裁決

1. 原工作樹 `codex/vcp-visual-guide` 落後 `main` 且有未提交文件，因此實作改在由 `main@09af0cc` 建立的 `codex/dataset-evolution-provenance` worktree，避免污染使用者變更。
2. handoff 所稱 artifact/source-audit/access-receipt 在 `main` 已存在，無功能衝突；本功能直接重用，不另造 writer。
3. dataset card 沒有 parent/family 欄位，因此以 explicit diff edge 建 evolution family；避免 mandatory migration。
4. source audit 是 row index，不單獨證明目前 `samples.jsonl` 每個 byte 未變；diff 先做整檔 samples hash 驗證，再以 audit 避免解析 unchanged row。
5. `SampleChange` 額外保存 `semantic_effects`，因為它是重建 impact view 的 canonical 分類結果；policy 名稱與版本會 pin 在 artifact spec/summary。
