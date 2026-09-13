# Dataset Evolution and Incremental Provenance 實作交接

- 日期：2026-09-13
- 狀態：implementation complete、local verification complete、whole-branch review approved
- 功能分支：`codex/dataset-evolution-provenance`
- 基底：`main@09af0cc`
- 功能 commit：`6ba42b8 feat(provenance): add dataset evolution tracking`
- 版本：`0.7.0` candidate；尚未 tag 或 release
- 工作目錄：`.claude/worktrees/dataset-evolution-provenance`

## 1. 給接手 agent 的結論

Plan 11「Dataset Evolution and Incremental Provenance」已完成設計、TDD 實作、真實資料唯讀驗證、
production-scale benchmark、完整 regression 與 whole-branch review。功能可把兩個 immutable dataset
version 的差異發布成 verified `dataset_diff` artifact，再計算它對 split、run、reading、judgement、
fusion、artifact、submission 與 backup 的影響。

SQLite index 是可刪除、可重建的衍生快取；canonical authority 仍是既有 JSONL、YAML、artifact
manifest、source audit 與 append-only ledger。實作沒有修改 DatasetCard schema，也沒有加入任何
特定比賽邏輯。

目前完成邊界：

- 功能 commit 已存在於本機隔離分支，工作樹乾淨。
- `1202 passed, 16 skipped`，coverage `95.05%`。
- `ruff check .`、`ruff format --check .`、`git diff --check` 通過。
- final whole-branch review 為 `APPROVE`，CRITICAL/HIGH/MEDIUM/LOW 均為 0。
- 尚未 push、建立 PR、merge、tag 或 release；不要把 `0.7.0` 稱為已發布版本。
- 原 checkout `codex/vcp-visual-guide` 的未提交文件修改未被碰觸。

## 2. 使用者需求如何落地

### 2.1 Dataset diff

`vcp data diff` 會：

1. 載入兩端 DatasetCard，核對 `samples.jsonl` 整檔 hash。
2. 驗證可用的 `source_audit`；缺席時完整解析並明示 `grade=fallback`。
3. 在 claim artifact id 前拒絕 duplicate sample id、schema error 與 hash mismatch。
4. 產生 deterministic `SampleChange`：`ADDED|REMOVED|MODIFIED`；unchanged 不產 event。
5. 依 recursive field path 分類 domain 與 semantic effect。
6. 以 `ArtifactWriter` 發布：
   `artifacts/dataset_diff/<id>/{spec.json,changes.jsonl,summary.json,manifest.json}`。

預設分類：

| Field/domain | Effect |
|---|---|
| `views` | `INPUT_AFFECTING` |
| `labels`、`label_source` | `TRAINING_AFFECTING` + `EVALUATION_AFFECTING` |
| `group` | `SPLIT_AFFECTING` |
| 未登記的 nested `meta.*` | `UNKNOWN`，保守要求 REVIEW |
| added / removed sample | input、training、split、evaluation 全部受影響 |

比賽或資料集特有規則只能透過 plugin registry 擴充，不得寫入 `src/vcp`。

### 2.2 Canonical graph 與狀態

Canonical graph 的 entity 包括：

- dataset version、changed sample version、split plan
- verified export pin、materialized cache
- run、access receipt、prediction、reading、append-only judgement event
- fusion run、immutable artifact、submission、backup manifest

主要 edge：`DERIVED_FROM`、`CONTAINS_CHANGE`、`USES_SPLIT`、`CONSUMED_BY`、
`PRODUCED_BY`、`EVALUATED_BY`、`COMBINED_INTO`、`SUPERSEDES`、`BACKED_UP_AS`、
`SUBMITTED_AS`。方向固定由 upstream 指向 downstream。

狀態語意：

| Status | 語意 |
|---|---|
| `VALID` | 對指定 dataset head 沒有已知失效證據 |
| `STALE` | 已知 training/input/split/evaluation change 命中依賴 |
| `REVIEW` | UNKNOWN effect 或現有證據不足以精準裁決 |
| `BROKEN` | 必要 canonical dependency 遺失、損壞或宣告無法解析 |

Sample-level impact 的證據優先順序為 verified access receipt、verified export manifest、declared
dataset/subset。缺證據時明示 gap 或 REVIEW，不猜測血緣。

### 2.3 Incremental SQLite index

預設位置為 `<data_root>/indexes/provenance.sqlite3`，不進 Git。它使用 WAL、單 writer 與 transactional
update，包含 metadata、dataset transitions、sample changes、entities、edges、materialized statuses、
ingest checkpoints 與 ingested artifacts。

`ingest_diff()` 的重要安全與效能性質：

- transaction 開始後重驗 checkpoint、artifact manifest 與所有 pinned inputs。
- 寫入 derived rows 前驗一次，commit boundary 前再驗一次；發現 drift 全部 rollback。
- duplicate artifact ingest 為 idempotent no-op；相同 id 不同內容為 integrity failure。
- 只載入歷史 graph topology，不 deserialize 全部歷史 `sample_changes.event_json`。
- 從 source-head materialized status 加本次 delta，重算 downstream dirty closure。
- graph fingerprint 以 record-count 與 order-independent sum 增量前進；`verify-index` 仍以 canonical
  full replay 做逐 record exact parity，不把 accumulator 當權威。
- rebuild 在 temporary database 完成、核對 canonical snapshot 與 parity 後才原子發布。
- ledger prefix drift、immutable evidence 改寫或刪除一律 fail closed。

## 3. CLI 介面

```powershell
uv run vcp data diff --from OLD --to NEW [--id DIFF_ID] [--plugin MODULE --policy NAME]

uv run vcp provenance rebuild
uv run vcp provenance sync
uv run vcp provenance ingest --artifact DIFF_ID
uv run vcp provenance impact --dataset DATASET [--sample SAMPLE_ID]
uv run vcp provenance stale --head DATASET
uv run vcp provenance explain --entity type:id
uv run vcp provenance status
uv run vcp provenance verify-index
```

所有命令沿用 VCP 契約：human/JSON parity、`--json` 時 stdout 只有 JSON、VERDICT 到 stderr、
exit `0/1/2` 對應 OK或WARN/FAIL/ABORT。實際參數以 `uv run vcp <group> <command> --help` 為準。

正常操作順序：

1. 第一次使用或 index 可疑時執行 `provenance rebuild`。
2. canonical run/reading/artifact/ledger 新增後執行 `provenance sync`。
3. 建立新的 `dataset_diff` 後執行 `provenance ingest --artifact ...`。
4. 以 `impact`、`stale`、`explain` 查詢。
5. 重要決策前執行 `verify-index`。

不要手動編輯 SQLite。若 sync 報告既有 canonical evidence drift，先調查來源；只有確認 canonical
records 本身正確後才刪除或 rebuild index。

## 4. 程式碼與文件地圖

| 路徑 | 責任 |
|---|---|
| `src/vcp/provenance/schema.py` | strict events、summary、entity/edge/status models |
| `src/vcp/provenance/policy.py` | generic field-domain/effect policy registry |
| `src/vcp/provenance/diff.py` | verified dataset diff 計算與 immutable artifact 發布 |
| `src/vcp/provenance/graph.py` | canonical scanners、deterministic graph、visible gaps |
| `src/vcp/provenance/views.py` | impact、status、explain 的 full reference semantics |
| `src/vcp/provenance/index.py` | SQLite schema、rebuild/sync/ingest/checkpoint/verify |
| `src/vcp/cli_provenance.py` | provenance CLI 與 VERDICT/JSON wiring |
| `src/vcp/cli.py` | `vcp data diff` 與 provenance app 註冊 |
| `tests/unit/provenance/` | 48 個 provenance unit/regression tests |
| `tests/performance/provenance/` | algorithm、production API、real metadata-copy runners |

權威文件：

- 設計：`docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md`
- 執行計畫與修正：`docs/superpowers/plans/2026-09-13-vcp-plan11-dataset-evolution-provenance.md`
- 操作與 repair：`docs/guides/DATASET_EVOLUTION_PROVENANCE.md`
- benchmark 解讀：`docs/benchmarks/provenance-2026-09-13.md`
- machine-readable results：`docs/benchmarks/provenance-*-2026-09-13.json`

## 5. 驗證證據

### 5.1 完整 regression

```text
uv run pytest --cov=vcp
1202 passed, 16 skipped in 288.07s
Total coverage: 95.05%
```

另外已通過：

```powershell
uv run ruff check .
uv run ruff format --check .
git diff --check
uv run vcp provenance --help
uv run vcp data diff --help
```

### 5.2 Real metadata-copy validation

Runner 對 live RSNA roots 唯讀，將 metadata 複製到 OS temporary directory；diff artifact 與 SQLite
只寫入副本。結果：

- 13,196 entities
- 17,569 edges
- 8,752 changes
- 0 gaps
- 7 selected runs
- `graph_parity=true`
- `status_parity=true`
- `verify_index.ok=true`

`fallback` grade 是 temporary copy 刻意不複製 live source-audit artifacts；兩端 `samples.jsonl` 仍有
完整 hash 與 schema 驗證。這不是未驗證的 production dataset mutation。

### 5.3 Production-schema/API benchmark

正式 runner 直接計時 `ProvenanceIndex.ingest_diff()`，fixture 隨 scale 建立 runs、readings、artifacts、
fusion 與 submission topology；不是 orphan changes 或 toy kernel。

| Historical events | Incremental + query p95 | Full load + fingerprint p95 | Speedup | Parity |
|---:|---:|---:|---:|---:|
| 1,000 | 55.218 ms | 21.384 ms | 0.39× | 100% |
| 10,000 | 61.275 ms | 176.223 ms | 2.88× | 100% |
| 100,000 | 204.521 ms | 2,114.040 ms | 10.34× | 100% |
| 1,000,000 | 2,427.051 ms | 23,731.252 ms | 9.78× | 100% |

1M workload 有 3,404 個 regular runs；delta 傳播後為 7,423 STALE、205 VALID。小資料固定成本使
1k 比 full load 慢，因此不得宣稱所有尺度都有加速。另一份 algorithm microbenchmark 只用來理解
演算法形狀，不能作 production API 效能證據。

## 6. Review 中發現並已修正的問題

Reviewer 不是只檢查 happy path；以下問題都已有 regression test：

- branching evolution 的 change union 與 multi-hop dirty closure 不完整。
- sample-level impact 精度、receipt/reading identity 與 BROKEN propagation 不正確。
- deleted artifact、checkpoint snapshot、materialized-cache canonical snapshot 漏掃。
- diff endpoint/source-audit inputs 在 ingest 中未重新 hash，以及 commit-boundary TOCTOU。
- 合法 append-only re-judgement 被錯誤合併成同一 identity。
- missing fusion member 沒有標 BROKEN。
- dataset evolution cycle 沒有 fail closed。
- changed sample、export pin、materialized cache、artifact `SUPERSEDES` graph families 遺漏。
- 未驗證或 supersession ledger unlinked 的 artifact 曾可能建立信任邊。
- 舊 incremental ingest 仍載入全部歷史 changes，fingerprint verifier 未核對 accumulator metadata。
- 初版效能結果使用 trivial topology；已改成正式 API 加 realistic downstream graph 並重跑結果。

Final whole-branch re-review 結果：APPROVE，所有已知 HIGH/MEDIUM 均關閉，最終四級 severity 均為 0。

## 7. 尚未做與接手界線

以下不是缺陷，而是尚未獲授權或本計畫刻意不包含：

- 沒有 push branch、建立 PR、merge 到 main、建立 `v0.7.0` tag 或發布套件。
- 沒有修改 live `raw/`、canonical ledger 或正式比賽 submission。
- 沒有 dashboard、遠端 provenance service、通用 event bus 或 DICOM read-level tracing。
- 沒有讓 SQLite 成為 source of truth；它必須保持可刪除與可重建。
- 沒有改動原 `codex/vcp-visual-guide` checkout 的使用者文件。

## 8. 下一個 agent 的接手清單

1. 先讀根目錄 `AGENTS.md`、本文件、設計 spec、Plan 11 與操作指南。
2. 確認自己位於 `codex/dataset-evolution-provenance`，HEAD 至少包含 `6ba42b8`；不要在原本 dirty
   `codex/vcp-visual-guide` checkout 合併或覆寫文件。
3. 若只做 review，保持唯讀並核對 commit、tests、benchmark JSON 與報告，不重跑 live mutation。
4. 若獲授權整合，先 fetch/rebase 或 merge 最新 main、處理衝突，再完整重跑：

   ```powershell
   uv sync
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest --cov=vcp
   ```

5. 合併前重新執行 whole-branch review；本文件的 1,202 tests 只證明 `6ba42b8` 加本交接文件時的
   本機狀態，不證明未來 main 或其他環境。
6. 只有在 remote push/PR/tag/merge 有直接回讀證據時，才把相應狀態改為已完成。

## 9. 可直接貼給下一個 agent 的提示

```text
請接手 VCP Dataset Evolution and Incremental Provenance。先讀 AGENTS.md、
docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md、設計 spec、Plan 11 與操作指南。
功能分支是 codex/dataset-evolution-provenance，基底 main@09af0cc，功能 commit 6ba42b8。
目前只有本機實作與驗證完成，尚未 push/PR/merge/tag/release。請先核對 git status、commit、
benchmark JSON 與測試證據；不要碰 codex/vcp-visual-guide checkout 的既有 dirty 文件。
若任務是整合，先同步最新 main、解決衝突，完整跑 ruff、format check、pytest --cov=vcp，
再做 whole-branch review。沒有直接 remote 證據時不要宣稱已 push、PR、merge 或 release。
```
