# PostgreSQL Adaptive Provenance 實作交接

- 日期：2026-09-13（2026-09-15 更新合併與 live 證據狀態）
- 狀態：Tasks 1–12 complete；2026-09-14 追加 live PostgreSQL 整合證據與記憶體護欄工作（commits `af03188`…`fd92de2`，無 ledger）；external evidence 部分完成，見 §6
- 分支：`codex/postgresql-adaptive-provenance`（已合併到 `main`）
- feature 起點：`codex/dataset-evolution-provenance@a625331`
- SDD start commit：`33dd3ba0c08ccdb4b9435f92fb36a91b2d197e6c`
- Task 12 implementation base：`f82fea4bb5a969804d6d68b583f701d510c836ac`
- 版本：`0.8.0`（tag `v0.8.0` 於 PR 合併 commit）

## 1. 結論

本分支已實作 optional PostgreSQL provenance backend、full/incremental/NO_OP maintenance、deterministic
adaptive selector、CLI wiring、opt-in integration harness、六方法 benchmark 與 calibration/held-out
runners。SQLite 仍是未指定 backend 時的 default；兩種 database 都只是 derived、noncanonical index，
canonical evidence 和 status semantics 沒有搬進 PostgreSQL。

Tasks 1–11 每一輪 task-scoped independent review 的 finding 已修正並 re-review clean。Final database、
security 與 whole-branch reviews 在 code head `7eaef001c987af4f341ec8533ca33b4d9e00fed9` 均為 code-clean。
這是 source-level review 結論，不等於完整 spec acceptance 或 external evidence completion。

外部證據大部分完成：2026-09-14 起本機有原生 PostgreSQL 17.11（非 Docker），live integration v3 PASS 51/51
（`docs/benchmarks/postgres-provenance-integration-v3.json`）；2026-09-16 正式 calibration v2 產出 policy
`postgres-adaptive-v1-9f4e58346529`；2026-09-18 正式 six-method v1（1K–100K 完整 matrix、648 列全 ok、parity
648/648）。仍沒有 held-out、live RSNA run 或效能 gate 的正式裁決（calibration v1 與 1M memory gate v2–v4 被 RAM
護欄中止；證據、數字與下一步見 `docs/benchmarks/postgres-provenance-v1.md`）。分支已於 2026-09-15 經使用者授權以
PR 合併並 tag `v0.8.0`。

## 2. 功能與安全邊界

- `ProvenanceBackend` protocol + thin `SQLiteBackend` 保留原 SQLite path與行為；PostgreSQL module只有被
  明確選取時才 lazy import Psycopg。
- PostgreSQL schema v1 使用 normalized typed tables、FK/CHECK/index、transaction advisory lock和
  generation pointer。Full rebuild先建/驗新 generation，最後才切 active pointer；任何失敗 rollback。
- Incremental ingest驗 verified diff、canonical snapshots/checkpoints、bounded affected paths，遞迴計算
  dirty closure並維持 exact graph/status/head parity。
- `auto` 用 immutable `provenance_policy` artifact內的 frozen nonnegative cost models與 RMSE band；沒有
  policy 時非零 semantic work安全選 FULL + WARN。explicit bad policy在寫入前 FAIL。
- VCP只接受 allowlisted libpq service name；不接受 connection URI/secret flags、不讀 credential file，
  driver failure只輸出 fixed reason、backend、allowlisted SQLSTATE。
- PostgreSQL decision rows、index與benchmark telemetry全是 derived；不能反向成為 canonical evidence。

## 3. CLI 與操作

```powershell
uv sync --extra postgres
uv run vcp provenance rebuild --backend postgresql --pg-service <SERVICE>
uv run vcp provenance sync --backend postgresql --pg-service <SERVICE>
uv run vcp provenance ingest --artifact DIFF_ID --backend postgresql --pg-service <SERVICE> --strategy incremental
uv run vcp provenance ingest --artifact DIFF_ID --backend postgresql --pg-service <SERVICE> --strategy full
uv run vcp provenance ingest --artifact DIFF_ID --backend postgresql --pg-service <SERVICE> --strategy auto --policy POLICY_ID
uv run vcp provenance impact --dataset DATASET --backend postgresql --pg-service <SERVICE>
uv run vcp provenance stale --head DATASET --backend postgresql --pg-service <SERVICE>
uv run vcp provenance explain --entity type:id --backend postgresql --pg-service <SERVICE>
uv run vcp provenance status --backend postgresql --pg-service <SERVICE>
uv run vcp provenance verify-index --backend postgresql --pg-service <SERVICE>
```

所有命令都有 `--json`、`--data-root`、`--configs-root`；實際參數以 `--help` 為準。完整 service setup、
result fields、failure/repair 與實驗命令見 `docs/guides/POSTGRESQL_PROVENANCE.md`。不要直接修改 schema或
active generation；不相容 schema 要保留調查證據後換乾淨 database再 rebuild。

## 4. Commit ledger

Feature/plan與 Tasks 1–11 commits（reverse order from feature start）：

```text
33dd3ba docs(provenance): plan adaptive postgres backend
065e0f0 feat(provenance): add backend contract
bb2b442 test(provenance): cover sqlite maintenance classifications
4477969 feat(provenance): add optional postgres connection
ed65bef feat(provenance): define postgres schema
b5d219f fix(provenance): protect postgres schema marker
a31722b feat(provenance): add postgres full rebuild
8941226 fix(provenance): preserve postgres rebuild inputs
c2f497b feat(provenance): add postgres incremental ingest
5a83025 fix(provenance): preserve oracle ordering and checkpoint pins
3d73f9a feat(provenance): add adaptive strategy policy
fc748e0 fix(provenance): validate policy evidence safely
9c98f3a fix(provenance): reject duplicate calibration keys
b8d183a feat(provenance): select adaptive maintenance
0c9a7d0 fix(docs): restore immutable adaptive implementation plan
0743d8d feat(cli): select provenance backend strategy
62eb3ce test(provenance): add postgres integration harness
37601c7 perf(provenance): compare adaptive postgres maintenance
45ad562 fix(provenance): release completed benchmark fixtures
ed7230c perf(provenance): calibrate and evaluate adaptive policy
00f60bd fix(provenance): enforce complete adaptive experiments
8c96468 fix(provenance): bind adaptive fitting to scenario identities
f82fea4 fix(provenance): reject calibration no-op workload leakage
```

Task 12 implementation document commit 是
`e505c3e61dc4be9937c5af204a61732a64ed48b9`（`docs(provenance): document postgres adaptive backend`）；
full-regression evidence refresh commit 是
`015fd70fb0858db3e00f88f8d5096ce919e66b94`（`docs(provenance): refresh postgres regression evidence`）。
不要以 `git log -1` 或 current HEAD 代替這兩個 attribution。`915073588a26fd468c626fc4874b315ac770d631`
只歸屬 historical full-suite execution，並非 Task 12 文件 commit，也不是 current HEAD 的宣告。

## 5. Code-head regression evidence

Task 12 local gates are recorded here. Earlier task evidence remains distinct and is not a substitute for the
current full regression:

- Task 9：236 task-scoped passed、43 unconfigured PostgreSQL skips；independent database review clean。
- Task 10：262 provenance passed、43 skips；offline 1K smoke為12 rows/24 samples、只含 canonical/SQLite；
  implementation review clean。
- Task 11 final focused re-review：142 passed；上一輪完整 provenance scope為338 passed、43 skips；
  implementation review clean。
- Task 12 package/document contract after an explicit editable-metadata refresh: 7 passed. The refresh command
  was `uv sync --extra postgres --reinstall-package vcp`; an ordinary sync did not refresh the installed
  `0.7.0` metadata, so the version contract first failed until that explicit reinstall.
- Task 12 provenance scope: 342 passed, 43 unconfigured PostgreSQL skips. It reported 51.52% total coverage and
  exit 1 only because this intentionally partial scope cannot satisfy repository-wide `fail_under = 80`; the
  PostgreSQL provenance module itself was 90% in that run. This is not full-suite coverage evidence.
- Historical Task 12 evidence at `e505c3e` recorded a collection block from a duplicate `conftest` module name.
  That originating integration-harness defect was independently corrected and reviewed in `9150735`; it is no
  longer a full-suite limitation of this branch.
- Historical full regression executed at `915073588a26fd468c626fc4874b315ac770d631`:
  `uv run pytest --cov=vcp` → 1,498 passed, 59 skipped, 94.73% total coverage, exit 0 (384.40s).
- Current code-head full regression executed at `7eaef001c987af4f341ec8533ca33b4d9e00fed9`:
  `uv run pytest --cov=vcp` → 1,539 passed, 59 skipped, 94.86% total coverage, exit 0 (464.94s). In both runs,
  skips, including unconfigured PostgreSQL cases, remain skips rather than live database passes.
- Task 12 continuation static gates passed: `uv run ruff check .`, `uv run ruff format --check .` (348 files),
  and `git diff --check`.
- Task 12 integration runner was re-run and failed at its fixed missing-runtime preflight with exit 1; no Docker,
  service, connection, credential read, or PostgreSQL test execution occurred.

## 6. PostgreSQL、policy 與實驗 evidence

| Required evidence | Current state |
|---|---|
| PostgreSQL version | 17.11 / 170011 live readback（原生 Windows 服務）；Compose image 仍 pin `17.11-bookworm` |
| Live integration | PASS 51/51 at `d8cc334`（integration-v3；rollback 28、concurrency 1、MVCC 1、parity 3）；Linux CI 仍缺 |
| Six-method scaled/large-scale | `postgres-provenance-six-method-v1.json`（2026-09-18，commit `6ab2b7a`，SHA-256 `47bc10ab…`）：1K/10K/100K × 9 ratios × chain/branched × 2 seeds，6 方法 × 7/7/3 次 = 648 列全 ok、parity 648/648；crossover 12/12 `not_observed`；adaptive 在 10K/100K 選 INCREMENTAL、在 1K 因絕對 RMSE 信心帶全部落入 FULL（後記 §5）；1M 已移出正式矩陣（memory gate v2–v4 ABORT，非正式） |
| Live RSNA six-method | Absent |
| Calibration artifact | `postgres-provenance-calibration-v2.json` + `-artifacts/`（2026-09-16，1224 次量測，從 checkpoint 發布，見 evidence record 發布註記） |
| Policy ID / exact policy hash | `postgres-adaptive-v1-9f4e58346529` / `policy.json` SHA-256 `a22d7067…03a2d78` |
| Held-out evaluation | Absent |
| Calibration/held-out separation | Offline tests enforce disjoint ID/hash/workload sets；無 live outputs可比較 |
| Adaptive p50/p95 gates | 正式裁決要等 held-out；calibration split 的預覽（aggregate p50 1.010 / p95 0.932；every-scenario 31/92 > 1.05）只是診斷，不是裁決 |

不要填 crossover、latency、policy version result、pass/fail 或 RSNA numbers 到 held-out 或 RSNA 的格子；
six-method 的正式數字只從 `postgres-provenance-six-method-v1.json` 回讀（逐格表在 evidence record
§六方法正式結果），不得從探路矩陣或 calibration 推導。

## 7. 驗收與尚缺項目

目前不能宣稱完整 spec acceptance。Acceptance criteria 1–12與15主要有 unit/offline contract evidence；
database、security 與 whole-branch reviews 在 `7eaef001c987af4f341ec8533ca33b4d9e00fed9` code-clean，
但 immutable plan未改且仍缺：

1. 本機原生服務已取得 v3 PASS 51/51（rollback / concurrency / MVCC / parity）；Linux CI evidence 仍要在
   可用 Docker Compose host 跑 `tests/integration/postgres/run.ps1` 取得。
2. six-method 1K/10K/100K matrix 已於 2026-09-18 完成（`postgres-provenance-six-method-v1.json`；1M 已於
   2026-09-15 移出正式矩陣，見 Plan 12 後記 §1）；real track（`real_validation.py --six-method
   --policy-from …calibration-v2.json`）仍缺，保存 machine-readable output 與 hash。
3. calibration-only 已完成（v2）；仍要於不同 process 跑 frozen held-out（seeds 20261001/20261002）；記 policy
   ID、精確 policy file hash、environment/version、完整 scenario/sample counts、zero leakage、parity 與 honest
   p50/p95 gate 結果。已知 1K 會因絕對 RMSE 信心帶落入 FULL（後記 §5）：照跑、照報，不得為此改 policy 或 selector。
4. push / PR / merge / tag 已於 2026-09-15 經使用者授權完成；後續證據工作在 `main` 之後的新分支進行。

## 8. 下一位 operator

先讀 `AGENTS.md`、本文件、`docs/guides/POSTGRESQL_PROVENANCE.md` 與
`docs/benchmarks/postgres-provenance-v1.md`。核對 branch/HEAD/status，不讀或抄出 operator credential
material。若只有 review權限就保持 read-only。Benchmark/calibration/held-out 前須完成其 separate opt-in
preflight：`VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE` 全部存在，後兩者為權限受限的 absolute
temporary file paths，且 disposable service 有 `CREATE DATABASE` privilege；這不能由 ordinary VCP/libpq
defaults 推定。再依操作指南的 integration → six-method → calibration → held-out順序執行；任何 failed
row或 gate必須保留並如實報告，不得用 held-out refit。
