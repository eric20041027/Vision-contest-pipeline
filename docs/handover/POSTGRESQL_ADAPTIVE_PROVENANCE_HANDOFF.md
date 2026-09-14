# PostgreSQL Adaptive Provenance 實作交接

- 日期：2026-09-13
- 狀態：Tasks 1–11 implementation/review complete；Task 12 local verification complete；external evidence pending
- 分支：`codex/postgresql-adaptive-provenance`
- feature 起點：`codex/dataset-evolution-provenance@a625331`
- SDD start commit：`33dd3ba0c08ccdb4b9435f92fb36a91b2d197e6c`
- Task 12 implementation base：`f82fea4bb5a969804d6d68b583f701d510c836ac`
- 版本：`0.8.0` candidate；不是 released version

## 1. 結論

本分支已實作 optional PostgreSQL provenance backend、full/incremental/NO_OP maintenance、deterministic
adaptive selector、CLI wiring、opt-in integration harness、六方法 benchmark 與 calibration/held-out
runners。SQLite 仍是未指定 backend 時的 default；兩種 database 都只是 derived、noncanonical index，
canonical evidence 和 status semantics 沒有搬進 PostgreSQL。

Tasks 1–11 每一輪 task-scoped independent review 的 finding 已修正並 re-review clean。Final database、
security 與 whole-branch reviews 在 code head `7eaef001c987af4f341ec8533ca33b4d9e00fed9` 均為 code-clean。
這是 source-level review 結論，不等於完整 spec acceptance 或 external evidence completion。

外部證據沒有完成：本機沒有 Docker/PostgreSQL runtime，沒有 live database execution、完整
large-scale matrix、calibration、held-out、policy artifact、live RSNA run 或效能 gate裁決。沒有 push、
PR、merge、tag 或 release。

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
| PostgreSQL version | Compose image pinned `17.11-bookworm`；無 live server readback |
| Live integration | Absent；43 cases是 skips，不是 passes |
| Six-method scaled/large-scale | Absent |
| Live RSNA six-method | Absent |
| Calibration artifact | Absent |
| Policy ID / exact policy hash | Absent |
| Held-out evaluation | Absent |
| Calibration/held-out separation | Offline tests enforce disjoint ID/hash/workload sets；無 live outputs可比較 |
| Adaptive p50/p95 gates | 未執行、未裁決 |

不要填 crossover、latency、policy version result、pass/fail或RSNA numbers。唯一 performance-related
execution是 Task 10 small offline SQLite/canonical smoke，不含 PostgreSQL。

## 7. 驗收與尚缺項目

目前不能宣稱完整 spec acceptance。Acceptance criteria 1–12與15主要有 unit/offline contract evidence；
database、security 與 whole-branch reviews 在 `7eaef001c987af4f341ec8533ca33b4d9e00fed9` code-clean，
但 immutable plan未改且仍缺：

1. 在可用 Docker Compose host 跑 `tests/integration/postgres/run.ps1`，取得 PostgreSQL 17.11真實
   SQL/constraint/rollback/concurrency/MVCC及 Linux CI evidence。
2. 跑完整 six-method 1K/10K/100K/1M matrix與 real track，保存 machine-readable output與hash。
3. 先跑 calibration-only，再於不同 process跑 frozen held-out；記 policy ID、精確 policy file hash、
   environment/version、完整 scenario/sample counts、zero leakage、parity與 honest p50/p95 gate結果。
4. 只有另行授權後才能 push/open PR/merge/tag/release；`0.8.0` 在此前始終只是 candidate。

## 8. 下一位 operator

先讀 `AGENTS.md`、本文件、`docs/guides/POSTGRESQL_PROVENANCE.md` 與
`docs/benchmarks/postgres-provenance-v1.md`。核對 branch/HEAD/status，不讀或抄出 operator credential
material。若只有 review權限就保持 read-only。Benchmark/calibration/held-out 前須完成其 separate opt-in
preflight：`VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE` 全部存在，後兩者為權限受限的 absolute
temporary file paths，且 disposable service 有 `CREATE DATABASE` privilege；這不能由 ordinary VCP/libpq
defaults 推定。再依操作指南的 integration → six-method → calibration → held-out順序執行；任何 failed
row或 gate必須保留並如實報告，不得用 held-out refit。
