# Plan 11：Dataset Evolution and Incremental Provenance

- 分支：`codex/dataset-evolution-provenance`
- 基底：`main@09af0cc`（v0.6.0）
- 規格：`docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md`
- 流程：每個 phase 先加入失敗測試，再最小實作、局部驗證、task-scoped review；全部完成後全分支 review 與完整 regression。

## Task 0 — Phase 0 與計畫落地

1. 核對 branch/worktree/dirty state、最近 commits、artifact/access/source-audit 介面。
2. 記錄 active checkout 裁決與相依性。
3. 跑現有 artifact/source-audit/CLI baseline 測試。

## Task 1 — Dataset diff schema、policy 與 artifact

1. 先測 deterministic id、ADDED/REMOVED/MODIFIED/no-op、排序、recursive fields、domains/effects、unknown meta、plugin policy。
2. 實作 `provenance/schema.py`、`policy.py`。
3. 先測兩端 hash/schema/duplicate/audit/fallback 與 publish failure boundary。
4. 實作 source-audit-assisted `diff.py`，所有驗證完成後才開 `ArtifactWriter`，輸出 `changes.jsonl`、`summary.json`。
5. 整合 `vcp data diff` 與 JSON/human parity；局部測試與 review。

## Task 2 — Full-recompute canonical graph 與 views

1. 先測 deterministic entities/edges、dataset->plan->run->reading、fusion、artifact input、submission、gap/broken。
2. 實作 canonical scanner 與 normalized graph model。
3. 先測 training/evaluation/split/display/unknown/no-op status 及 sample/subset membership。
4. 實作 impact、stale、explain 的 full reference views；局部測試與 review。

## Task 3 — Incremental SQLite index

1. 先測 schema version、idempotent ingest、dirty closure、rollback、checkpoint prefix drift。
2. 實作 transaction ingest、entities/edges/changes/checkpoints/status tables 與必要 indexes。
3. 先測 delete/rebuild parity、temporary rebuild failure 不破壞舊 index、verify-index drift。
4. 實作 rebuild、canonical sync、verify；局部測試與 review。

## Task 4 — Provenance CLI

1. 先測 ingest/impact/stale/explain/status/rebuild/verify-index 的 human/JSON、VERDICT/exit code/context。
2. 實作 `cli_provenance.py` 並註冊 app。
3. 驗證 index 缺席不改任何既有 data/eval/fuse/train/submit/backup/artifact 命令。

## Task 5 — Parity/property/fault tests

1. 固定 seed 生成 branching/no-op/mixed-effect histories；每 event 比 full 與 incremental normalized result。
2. 覆蓋 duplicate ingest、missing/corrupt manifest、interrupted transaction、custom roots。
3. 加端到端 fixture：diff artifact -> rebuild -> incremental ingest -> queries。

## Task 6 — Real + Scaled benchmark

1. 實作 deterministic opaque workload generator，不含 raw medical content。
2. 實作三 baseline runner 與 JSON result；large scales 不進普通 CI。
3. 只讀偵測可用真實 dataset transitions，重算 counts/domain/linkage/parity；缺資料就明列 skip/阻塞。
4. 跑 1k/10k/100k/1m，記環境、seed、warm-up、repetitions、hash、latency、throughput、size、parity與負結果。

## Task 7 — 文件、版本、review、完整驗證

1. 更新 README、AGENTS/HANDOVER、CHANGELOG、package version 與操作/repair/benchmark 文件。
2. task-scoped code review；修正後 narrow re-review。
3. `uv run ruff check .`、`uv run ruff format --check .`、局部與完整 `uv run pytest --cov=vcp`。
4. final whole-branch review，建立 followups，記 exact branch/commit/test evidence；沒有直接證據不宣稱 push/PR/merge。

## Completion checklist

- [x] verified immutable diff artifact；unchanged 不產 event；no-op 正確
- [x] conservative generic policy 與 plugin extension
- [x] canonical graph、impact/stale/explain、visible gaps
- [x] incremental exact parity、idempotency、rollback、rebuild、checkpoint drift fail-closed
- [x] CLI 契約與 legacy compatibility
- [x] real read-only results 與 scaled machine-readable results
- [x] lint/format/full coverage/final review evidence

## 執行期修正

- Source audit 可快速找 candidate rows，但 artifact 發布前仍完整 schema-validate 每一列；這是為了滿足
  「兩端 schema 都已驗證」的較強安全條件。summary 明示 `source_audit|fallback` grade。
- 新 canonical run/reading/artifact 使用明確 `provenance sync`；diff 使用 `ingest`。兩者分開，避免每個
  小 diff 偽裝成 incremental、實際卻 full scan。
- task-scoped review 找到 branching union、sample precision、receipt/reading identity、BROKEN propagation、
  deleted artifact、snapshot/checkpoint與 multi-hop dirty context 問題；均以 regression test 修正。
- Real validation 首跑抓到 multi-hop head status parity false；修正 dirty closure 納入所有 dataset
  ancestors 後重跑，graph/status parity與 `verify-index` 均通過。這項負結果保留在本段，沒有隱藏。
- final whole-branch review 另抓到本次 diff input 未重驗、合法 re-judgement identity、fusion missing
  member、evolution cycle、`SUPERSEDES` 與 sample/export/materialized-cache graph families 遺漏；皆先加
  regression 再修正。diff inputs 現在於 write transaction 內、任何 derived row 前逐一 re-hash。
- reviewer 也證明舊 ingest 仍兩次載入歷史 changes，且原 57×/59× 數字只量 toy kernel。正式路徑已改
  為 topology + delta + source-status materialization 與增量 fingerprint；另建直接呼叫 production API
  的四級 benchmark。原 runner 保留但清楚標成 algorithm microbenchmark，不再作 production claim。
- 最後 gate：`ruff check .`、`ruff format --check .`、`git diff --check` 通過；完整測試為
  `1202 passed, 16 skipped`、coverage 95.05%。whole-branch re-review 為 APPROVE，最終四級 severity
  均為 0；證據只代表本機分支，沒有宣稱 push、PR 或 merge。
