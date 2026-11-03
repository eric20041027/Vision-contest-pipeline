# VCP provenance in PostgreSQL: explainer for the final report (v1, 2026-11-03)

> 這份是期末報告與發表用的英文素材，回應課程 2026-11-01 的四點意見：相關研究、VCP 怎麼和 PostgreSQL 配合（head、dirty
> closure、generation、RMSE band、H1、E1–E5）、溯源記下了什麼、PostgreSQL 裡怎麼實作。正文是英文，可以直接貼進報告或改寫成投影片。
> 每個數字都來自 repo 的程式碼或 `docs/benchmarks/` 的證據檔（§9 列出來源）；程式碼是 0.17.0（tag `v0.17.0`）。

| Feedback point | Where it is answered |
|---|---|
| Find 2–3 papers or systems and say how similar or different our work is | §8 |
| How VCP works with PostgreSQL; what the policies are (head, dirty closure, generation, RMSE band, H1, E1–E5); each experiment | §1, §2, §5, §6, §7 |
| What information the captured provenance includes | §3 |
| How it is implemented in PostgreSQL | §4 |

---

## 1. The system in one page

**The problem.** A competition dataset is released in versions. A new version can relabel, regroup, add or drop samples,
and each change may invalidate results that were computed on the old version: the split, the models trained on it, their
scores, ensembles built from those models, exported artifacts and submissions. The question we answer is: *after a new
dataset version arrives, which recorded results are still valid, which must be redone, and which need a human to look?*

**Where the truth lives.** VCP (the competition pipeline this project extends) records every step as files that are
written once or only appended to: dataset cards and sample tables, split plans, run records with code and checkpoint
hashes, score ledgers, artifact manifests, submission and backup ledgers. These files are the *canonical evidence*.

**What we added.** A version change is recorded as an immutable `dataset_diff` artifact with one event per changed
sample. From the evidence files VCP builds a *provenance graph* and gives every node a *status* under each dataset
version. Rebuilding that graph from the files (the *canonical replay*) is always correct but slow: about 6 s at the
100K scale, paid on every status question. So the graph and its statuses are kept in a **derived index** in
PostgreSQL, where reading every status under one version takes about 0.7 s (100K, after incremental maintenance). The index can be dropped and rebuilt at any time, it is checked against
the replay, and it is never the source of truth.

**The research question.** When a new diff arrives, the index can be updated in two exactly equivalent ways:

- **INCREMENTAL**: insert the new rows and recompute statuses only for the part of the graph the change can reach (the
  *dirty closure*, §2);
- **FULL**: rebuild the whole index from the replay as a new *generation* and switch to it atomically (§4.4).

Which one is cheaper depends on the size of the change relative to the graph. A **policy** (§5) predicts both costs with
calibrated linear models and picks one per diff; a zero-change diff is a **NO_OP**. The experiments E1–E5 (§6) test that
the index is always exactly the replay (E1), whether a crossover between the two methods exists (E2, E5), whether the
selector chooses well on unseen data (E3, E5), and how it behaves on real dataset versions (E4).

```text
evidence files (canonical, write-once / append-only)
   │  canonical replay (Python)
   ▼
provenance graph + statuses per head  ──►  PostgreSQL index (derived, disposable)
                                               ▲
new dataset_diff ── policy picks NO_OP / INCREMENTAL / FULL ──┘
```

---

## 2. Terms the report uses

| Term | Precise meaning in VCP |
|---|---|
| Canonical evidence | The files VCP writes (ledgers, cards, manifests, `dataset_diff` artifacts). They are the only source of truth. |
| Canonical replay | Reading all evidence files and building the provenance graph and every status from scratch, in Python. It is the correctness oracle in every experiment. |
| Dataset version | One dataset name plus the SHA-256 of its sample table. A new version is a new dataset (for example `r4` after `r3`); old versions are never edited. |
| `dataset_diff` | An immutable artifact (`changes.jsonl` + `summary.json`) describing version A → B: one `ADDED`, `REMOVED` or `MODIFIED` event per changed sample, with the changed domains and semantic effects (§3.3). |
| Head | The dataset version a status is computed against: "is this result still valid if the current data is version H?". The index stores statuses under every dataset version; the user-facing commands accept only leaf versions (no newer version derives from them). An entity pinned to version A is judged by the sample changes on the version path A → H. |
| Status | One of `VALID`, `STALE`, `REVIEW`, `BROKEN`, stored per (head, entity) with a reason and a predecessor (§3.4). |
| Dirty closure | The entities whose status can change when one diff A → B is ingested: A and all its older versions (unless the diff is empty and nothing newer depends on B) plus the entities this ingest adds, and everything reachable from them along provenance edges. A new transition only adds changes to version paths that start at A or its ancestors, so nothing outside this set can change. INCREMENTAL recomputes only these (§4.3). |
| Generation | One complete copy of the index inside PostgreSQL, identified by a UUID. Every row of every table carries its `generation_id`. A FULL rebuild writes a new generation; a singleton pointer (`active_generation`) says which one readers use (§4.4). |
| NO_OP / INCREMENTAL / FULL | The three maintenance paths. All three leave the index exactly equal to the replay; they differ only in cost. |
| Policy | A frozen, immutable artifact holding two cost models and an uncertainty band, used by `--strategy auto` to choose the path (§5). v1 and v2 differ only in the band. |
| RMSE band | The margin INCREMENTAL must win by: the root-mean-square error of each cost model on the calibration data. v1 uses one global pair; v2 uses one pair per order of magnitude of graph size (§5.3). |
| Gate | The pre-registered pass rule for a selector: pooled p50 ≤ 1.05× and pooled p95 ≤ 1.10× the better fixed method (§6). |
| H1, H1a | Pre-registered hypotheses of E3: v2 exceeds 1.05× the better fixed method in fewer scenarios than v1, overall (H1) and at 1K entities (H1a) (§6.3). |
| H5a–H5c | Pre-registered hypotheses of E5 (§6.5). |
| Density | changed samples ÷ graph edges. The cost models break even near 0.5 (§5.2). |

---

## 3. What the captured provenance includes

Provenance here is **artifact-level lineage plus a validity status**: for every recorded result, which dataset version,
split, run, score or artifact it was derived from, and whether it is still valid under a given dataset version. It is
not tuple-level why- or how-provenance of SQL queries (§3.6). Everything below is read from VCP's evidence files by the
replay (`src/vcp/provenance/graph.py`) and stored row for row in PostgreSQL (§4.2).

### 3.1 Nodes: 12 entity types

Every node is stored as `(entity_id, entity_type, key_value, dataset_version_id, attributes, broken_reason)`.
`entity_id` is `<type>:<key>` (a fusion run keeps the `run:` id of the run it is); `dataset_version_id` names the
dataset version the node is pinned to.

| Entity type | One node per | Stored attributes | Read from |
|---|---|---|---|
| `dataset` | dataset version (name + sample-table hash) | name, samples hash, sample count, source-audit id | dataset card, sample table (re-hashed) |
| `split` | split plan on one dataset version | dataset, plan id, the full sample → subset assignment, subset roles | split plan files |
| `materialized_cache` | decoded image cache | dataset, mode, manifest hash, row count | cache manifest and every output file (hashed) |
| `run` | training or prediction run | dataset, samples hash, plan id, subsets trained on, subsets predicted, data-access records and receipts | run record (`run.yaml`), prediction files (hashed) |
| `fusion_run` | ensemble run | as `run`, plus recipe id and member runs | `fuse.json` |
| `export` | exported training set pinned by a run | export manifest hash | the run record's pin |
| `reading` | one score of one run on one subset | run, plan, subset, metric name (not the value) | score ledger `readings.jsonl` |
| `judgement` | one pre-registered decision | pre-registration id, time, verdict | `judgements.jsonl` |
| `artifact` | immutable artifact of any kind (diffs, receipts, audits, label sets, features, …) | kind, id, verified flag, dataset, params, inputs (name, path, sha256), supersedes | artifact `manifest.json` (+ files, verified) |
| `sample` | one changed sample row, before or after the change | sample id, row sha256 | only samples named in a verified `dataset_diff`; unchanged samples get no node |
| `submission` | staged competition submission | test dataset, kind | `stage.json` |
| `backup` | evidence backup manifest | the conclusion it backs up | backup manifest |

A node is **BROKEN** at build time when its evidence is missing or altered: an unreadable card, a sample table whose
hash differs from the card, a prediction or cache file whose hash changed, a run whose dataset or split is absent, an
artifact that fails verification. `broken_reason` holds the exact cause.

### 3.2 Edges: 10 dependency types (upstream → downstream)

| Edge type | From → to | Meaning |
|---|---|---|
| `DERIVED_FROM` | dataset → dataset | the new version was derived from the old by this verified diff (attributes: diff id, change count). Also projected into `dataset_edges` / `dataset_edge_changes` for the version-path queries |
| `CONTAINS_CHANGE` | diff artifact → sample | the diff records this changed row (attributes: change id, before/after side) |
| `USES_SPLIT` | dataset → split; split → run | the plan is defined on this version; the run used this plan |
| `CONSUMED_BY` | dataset, export, access receipt, evidence or other artifact → run | the run read this input |
| `PRODUCED_BY` | dataset → export / cache / artifact; run or artifact → artifact | the target was built from the source (attribute: input name) |
| `EVALUATED_BY` | run → reading; reading → judgement | the score measured this run; the decision used this score |
| `COMBINED_INTO` | member run → fusion run | the run is an ensemble member |
| `SUPERSEDES` | old artifact → new artifact | a verified correction replaces the old artifact (attribute: reason) |
| `BACKED_UP_AS` | run / artifact / submission → backup | the backup manifest lists its files |
| `SUBMITTED_AS` | eval or test run → submission | the submission was built from the run |

### 3.3 Sample changes

One row per changed sample: `change_id`, from/to dataset and sample-table hashes, `sample_id`, `change_type`,
`changed_fields`, `changed_domains`, `semantic_effects`, `before_row_hash`, `after_row_hash`. Field paths are kept, field
values are not.

- **Type**: `REMOVED` if the id is only in the old version, `ADDED` if only in the new one, `MODIFIED` if in both with a
  different row hash (SHA-256 of the sample's line in the sample table). Identical rows produce no event.
- **Domain** = first segment of each changed field path: `views` → VIEWS, `labels` → LABELS, `label_source` →
  LABEL_SOURCE, `group` → GROUP, `meta` → META, anything else → UNKNOWN. `ADDED`/`REMOVED` record the field `*` and
  the five named domains (all but UNKNOWN).
- **Effect**: a competition can register a classification policy; otherwise the built-in map applies: VIEWS →
  INPUT_AFFECTING; LABELS and LABEL_SOURCE → TRAINING_AFFECTING + EVALUATION_AFFECTING; GROUP → SPLIT_AFFECTING; META
  and UNKNOWN → UNKNOWN. `ADDED`/`REMOVED` are input-, training-, split- and evaluation-affecting.
- `change_id` is the SHA-256 of (from hash, to hash, sample id, type, before hash, after hash); it does not depend on the
  classification.

### 3.4 Statuses

For every head H and every entity: `(head_id, entity_id, status, reason, predecessor_id)`. An entity pinned to version A
is judged by the union of changes on the version path A → H; entities with no dataset version see no changes.

| Entity | Rule |
|---|---|
| any | BROKEN if its own evidence is broken (`broken_reason`); otherwise VALID unless a rule below applies |
| run, fusion run | STALE if any change on the path is split-affecting, or an input- or training-affecting change hits a sample in a subset the run trained on; REVIEW if an unknown-effect change hits a sample whose subset is unknown, trained on or predicted |
| reading | inherits its run's status if the run is not VALID; otherwise STALE if an input-, evaluation- or split-affecting change hits the reading's own subset; REVIEW for an unknown effect there |
| split | STALE on a split-affecting change, else REVIEW on an unknown effect |
| artifact, export, cache | STALE on any change with a real effect; REVIEW on an unknown effect |

Then a fixed-point pass along incoming edges: **BROKEN flows to every dependent** along any edge type; **STALE and REVIEW
flow along edges only into judgements, fusion runs, artifacts, submissions and backups** (runs, readings and splits use
the subset-aware rules above, so a change in a validation subset does not invalidate a run that never trained on it).
Severity is BROKEN > STALE > REVIEW > VALID; ties are broken deterministically by (reason, predecessor). `reason` is a
readable cause (for example `split-affecting change for sample 1234` or `upstream run:r12 is STALE`), and
`predecessor_id` is the one-hop cause: the dataset version for a change rule, the upstream node for an inherited status.

### 3.5 Two examples

- **Small, asserted by a unit test** (`tests/unit/provenance/test_graph_views.py`): version `tiny` → `tiny-v2` changes
  one box coordinate (`labels.boxes[0].x`, so LABELS, training- and evaluation-affecting) of a sample in subset `valA`.
  Under head `tiny-v2`, the run stays VALID (it did not train on `valA`), its `valA` score becomes STALE and its `valB`
  score stays VALID. A `group` change to a `train` sample instead makes the run and both scores STALE.
- **Real data (RSNA Knee, E4)**: r3 → r4 replaces the patient grouping of every sample (GROUP, split-affecting), so the
  split and the runs on it become STALE and their scores, ensembles and submissions inherit it. r4 → r5 changes only
  metadata fields (META, unknown effect), so the affected runs become REVIEW. r5 → r6 changes nothing: NO_OP, statuses
  copied.

### 3.6 What is not captured

- **No tuple-level provenance.** Nothing links a prediction row or a metric value to specific input rows; scores are
  recorded without their values, predictions are hash-checked per subset, and data access is recorded per subset.
- **Unchanged samples** have no node; changed fields are recorded by path, not by value.
- **Code and hyperparameters** are not nodes, so a code change produces no status (the run record keeps code hashes, but
  the graph does not read them). The submission ledger (uploads, scores, final picks) is checked for tampering but not
  graphed.
- Statuses react only to dataset changes and to missing or altered evidence.

---

## 4. How it is implemented in PostgreSQL

### 4.1 Boundary

PostgreSQL neither computes provenance nor runs ML. VCP computes the graph from files in Python; PostgreSQL stores a
normalized copy, answers status reads, finds the stored part of the dirty closure with `WITH RECURSIVE`, and gives us
transactions, a writer lock and MVCC snapshots. SQLite is the default backend; PostgreSQL is optional (`--backend postgresql`). VCP connects only
through a libpq *service name*; it takes no connection URI or credential option and never reads credential files.

### 4.2 Schema v1 (`vcp_provenance`): 12 tables, 13 secondary indexes

Every table other than `generations` and `active_generation` carries `generation_id` with a foreign key to
`generations` and `ON DELETE CASCADE`, so deleting a generation removes all of its rows. Hash columns are `char(64)` with a hex `CHECK`;
enumerations are `CHECK (... IN ...)`; foreign keys between rows are `DEFERRABLE INITIALLY DEFERRED` so a whole graph can
be inserted in any order inside one transaction.

| Table | Key columns | Role |
|---|---|---|
| `generations` | `generation_id` (uuid), `state` (`building`/`ready`), `schema_version`, `graph_hash`, `graph_record_count`, `graph_record_sum`, `canonical_snapshot_hash` | One row per index copy, with the hashes used to prove parity with the replay |
| `active_generation` | `singleton` (always TRUE), `generation_id` | The pointer readers follow |
| `metadata` | `(generation_id, key)`, `value` | Build string, graph gaps, canonical snapshot, which data/configs roots this index belongs to |
| `entities` | `(generation_id, entity_id)`, `entity_type`, `key_value`, `dataset_version_id`, `attributes` (JSONB), `broken_reason` | Graph nodes (§3.1) |
| `provenance_edges` | `(generation_id, edge_id)`, `source_id`, `target_id`, `edge_type` (10 values), `attributes` (JSONB) | Dependency edges (§3.2) |
| `dataset_edges` | `(generation_id, source_id, target_id)`, `artifact_id` | Version A → version B, one per ingested diff |
| `sample_changes` | `(generation_id, change_id)`, `sample_id`, `change_type`, `changed_domains[]`, `changed_fields[]`, `semantic_effects[]`, `before_row_hash`, `after_row_hash`, from/to dataset and sample-table hashes | One row per sample event (§3.3); a `CHECK` ties `ADDED`/`REMOVED`/`MODIFIED` to which row hash may be NULL |
| `dataset_edge_changes` | `(generation_id, source_id, target_id, change_id)` | Which changes belong to which version edge |
| `entity_status` | `(generation_id, head_id, entity_id)`, `status`, `reason`, `predecessor_id` | Materialized status of every entity under every head |
| `ingest_checkpoints` | `(generation_id, source_path)`, `consumed_bytes`, `prefix_sha256`, `last_event_id` | How far each append-only ledger has been read, and a hash of that prefix to detect rewritten history |
| `ingested_artifacts` | `(generation_id, artifact_id)`, `manifest_sha256` | Diffs already applied; makes a repeated ingest a no-op |
| `maintenance_decisions` | requested and selected strategy, `reason_code`, the features, both cost estimates, `policy_version`, `elapsed_ms` | One row per maintenance decision (telemetry; never affects statuses). Rows belong to a generation, so a FULL rebuild drops the earlier ones |

Besides the primary keys, 13 secondary indexes cover the access paths: entities by type and by dataset version; edges by source and by target (both
directions of every traversal); dataset edges by target; sample changes by sample, by transition, by target and by type;
version-edge changes by change; statuses by (head, status), by (entity, head) and by predecessor (partial index).

### 4.3 The recursive queries

Four `WITH RECURSIVE` queries do the graph walks inside PostgreSQL. The dirty closure (`src/vcp/provenance/postgres.py`):

```sql
WITH RECURSIVE ancestors(entity_id) AS (          -- the old head and every older version
    SELECT :source
    UNION
    SELECT e.source_id FROM vcp_provenance.dataset_edges e
    JOIN ancestors a ON e.target_id = a.entity_id
    WHERE e.generation_id = :gen
), roots(entity_id) AS (                          -- where the change can enter the graph
    SELECT entity_id FROM ancestors WHERE :semantic
    UNION
    SELECT unnest(:delta_ids)                     -- entities this ingest adds: the diff, changed samples
), dirty(entity_id) AS (                          -- everything downstream of a root
    SELECT entity_id FROM roots
    UNION
    SELECT e.target_id FROM vcp_provenance.provenance_edges e
    JOIN dirty d ON e.source_id = d.entity_id
    WHERE e.generation_id = :gen
)
SELECT entity_id FROM dirty ORDER BY entity_id;
```

`:semantic` is true when the diff has changes or the new version already has newer versions. `UNION` (not
`UNION ALL`) removes duplicate rows, so a walk does not repeat work on diamond-shaped paths and would terminate even on
a cycle (cycles between dataset versions are rejected before this point anyway). The query sees only the edges already
stored; Python then extends the set over the edges the new diff adds and over the scores, runs and ensembles whose
status depends on each other, and recomputes the statuses of that set with the same function the replay uses, so there
is one definition of status semantics. The other three queries are the dataset ancestors of a version, the dataset
descendants of a version, and the sample changes on the version path between two datasets (descendants of one ∩
ancestors of the other, joined to `dataset_edge_changes` and `sample_changes`).

### 4.4 One ingest, step by step

`vcp provenance ingest --artifact <diff> --backend postgresql --strategy incremental|full|auto [--policy ID]`
runs as **one transaction**:

1. `pg_advisory_xact_lock(<constant key>)`: one writer at a time; a second writer waits and then sees the diff as
   already applied.
2. Check the schema marker (a schema comment; an incompatible schema fails instead of migrating silently) and that the
   active generation was built for this checkout's data and configs roots (`root_mismatch:` otherwise).
3. Verify the evidence: every ledger prefix recorded in `ingest_checkpoints` must still hash the same. A rewritten,
   truncated or deleted record fails closed (`prefix_drift:`), and the user rebuilds after finding out who changed it.
4. If `ingested_artifacts` already has this diff with the same manifest hash, record a `duplicate_artifact_no_op`
   decision and stop; a different hash for the same id fails closed (`canonical_drift:`).
5. Load the stored topology (entities and edges, not the sample changes) and add the diff to it in memory: the diff
   node, its changed-sample nodes and the `DERIVED_FROM` edge. Both dataset versions must already be in the index (they
   are dataset nodes read from their cards); a second diff between the same two versions is `transition_conflict:`.
   Compute the dirty closure (§4.3) and the features, and let the policy choose (§5).
6. **FULL**: insert a new `generations` row in state `building`; write the whole replayed graph, every status, the
   checkpoints and the ingested-artifact rows into it; read it back and require exact equality with the replay (graph
   hash, record count and record sum); check the evidence files did not change meanwhile; set `state = 'ready'`;
   upsert `active_generation` to the new id; `DELETE` the old generation (the cascade removes its rows).
7. **NO_OP or INCREMENTAL**: insert the delta rows into the active generation (idempotent inserts), recompute statuses
   only for the dirty set under every head reachable from the new version, give the other heads rows for the new
   entities, insert the ingested-artifact row, update the generation's graph hash and canonical snapshot, and rewrite
   the checkpoints. For NO_OP the dirty set is empty, so statuses are only copied.
8. Insert the `maintenance_decisions` row, re-verify the evidence prefixes, check that the policy artifact did not
   change during the ingest, commit.

Every path, NO_OP included, starts by reading the stored entities and edges into Python. That is why INCREMENTAL's
cost has a term proportional to the graph's edges and why a NO_OP is not free (about 6.8 s at 100K in held-out v2);
INCREMENTAL avoids reloading the historical sample changes and rehashing the whole graph, not the topology read.

Because everything is one transaction, readers keep their MVCC snapshot of the old state until the commit, and a failure
at any step rolls back to the previous index. The cost of FULL is visible in storage: the old generation's rows stay as
dead tuples until `VACUUM`: at 100K, PostgreSQL storage after FULL was 2,467 MiB against 1,188 MiB after
INCREMENTAL (six-method v1).

### 4.5 Reading and checking

- `stale --head H`: load the graph from the index, then read H's rows from `entity_status`. (The benchmarks' "status"
  timing is that plain read of every status under one head.)
- `status`: report the table counts and verify the index against a replay.
- `impact --dataset D` and `explain --entity E`: load the graph and answer in Python (not yet rewritten as SQL; see §7).
- `verify-index`: replay the evidence and compare every entity, edge, change and status with the index.
- `rebuild`: FULL from the evidence. In PostgreSQL v1, `sync` is the same atomic full publication, and it refuses to
  replace a generation built by another checkout. (The SQLite `sync` appends new canonical rows and refuses edits.)

---

## 5. The policies

### 5.1 The decision rule (`src/vcp/provenance/strategy.py`)

In order:

1. No changed samples and an empty dirty set → **NO_OP** (`verified_zero_semantic_changes`).
2. `--strategy incremental` or `full` → do that (`requested_incremental` / `requested_full`).
3. `--strategy auto` without a policy → **FULL** with a WARN (`fallback_policy_absent_full`): the safe default.
4. `--strategy auto` with a compatible policy → **INCREMENTAL** only if
   `est_incremental + band_incremental < est_full − band_full`
   (`calibrated_incremental_lower_confident_cost`), otherwise **FULL**
   (`calibrated_full_lower_or_uncertain_cost`).

The features are the verified values of the diff, not guesses: changed samples, dirty entities, dirty ratio, total
entities, total edges, historical changes and head count. Only three of them carry weight in the fitted models (§5.2);
the size of the dirty set only decides NO_OP. A policy whose environment fingerprint, PostgreSQL major version or
schema version does not match fails when named explicitly. A policy can only choose an algorithm; it can
never change a status.

### 5.2 The cost models

Fitted once on the calibration benchmark (seeds 20260913/20260914: 1,224 timed repetitions, summarized as 92
per-scenario p50 observations of nonzero diffs) with non-negative linear regression. The fitted intercepts are 0, and so
is every coefficient not shown below (dirty entities, dirty ratio, head count, total entities). v1 and v2 share the
models exactly:

- `incremental_ms ≈ 1.162 · changed_samples + 0.118 · total_edges`
- `full_ms ≈ 0.704 · total_edges + 0.097 · historical_changes`

So `est_full − est_incremental ≈ 0.586 · E + 0.097 · H − 1.162 · C`. With few historical changes this is zero when
C / E ≈ 0.504: **FULL becomes cheaper above about one changed sample per two edges.** We call C / E the *density*.
The models were measured on the code before release 0.16.0, whose status fix makes FULL cheaper (§6.5), so 0.504 is
the break-even of that code.

### 5.3 The band: v1 versus v2

The band is the margin INCREMENTAL must win by, taken from the models' own errors, so no threshold is tuned by hand
(the decade grouping in v2 is a fixed design rule).

| Policy | Band (incremental / full, ms) | Total |
|---|---|---|
| v1 `postgres-adaptive-v1-9f4e58346529` | one RMSE pair over all calibration rows: 2,286.3 / 5,057.4 | 7,343.7 |
| v2 `postgres-adaptive-v2-9f4e58346529`, decade 3 (10³–10⁴ edges) | 90.9 / 465.3 | 556.2 |
| v2, decade 4 (10⁴–10⁵ edges) | 1,168.0 / 4,418.3 | 5,586.3 |
| v2, decade 5 (10⁵–10⁶ edges) | 3,695.5 / 7,336.4 | 11,031.9 |

v2's decade is ⌊log10 total_edges⌋; a graph outside the calibrated decades uses the nearest one (the larger on a tie).
Why v2: v1's absolute 7.3 s band is wider than the whole predicted gap on a 1K graph (about 0.84 s), so v1 picked FULL
on every nonzero 1K diff and ran 1.82–4.26× slower than INCREMENTAL there (held-out v1). v2 compares the gap with the error *at that graph
size*. The band design (stratified by decade, against a relative band) was chosen by a pre-registered cross-seed test
before any v2 measurement: stratified scored 1.011, relative 1.584 (lower is better).

---

## 6. The experiments E1–E5

E1–E5 are the labels our progress report gave the experiments. Each was measured on the code of its date
(September–October, before release 0.16.0); §3–§5 describe the code of release 0.17.0.

Common setup: synthetic graphs in VCP's real schema at nominal 1K, 10K and 100K entities (the generator's size
parameter; the graphs have about 1.33× as many entity rows, for example 133,329 entities and up to 199,982 edges at
100K, while E4 and E5 report true counts), nine change ratios
(0, 0.1%, 1%, 5%, 10%, 25%, 50%, 90%, 100%), two topologies (chain, branched), two seeds: 108 scenarios per seed pair.
Every repetition uses a fresh database (7 repetitions at 1K and 10K, 3 at 100K). Methods: canonical replay, SQLite full
and incremental, PostgreSQL full, incremental and adaptive. Calibration seeds fit the models; evaluation seeds are never
used for fitting. Hypotheses, gates and predictions are committed before the measurement they judge. One PC: AMD Ryzen 7
9700X, 31.2 GiB RAM, NVMe, Windows 11, PostgreSQL 17.11, Python 3.12.

### 6.1 E1 — Correctness: is the index always exactly the replay?

- **How**: every row of every benchmark compares the PostgreSQL (and SQLite) index with the canonical replay: the
  normalized graph, the graph hash, every materialized status, the heads. Any difference fails the row. A live
  integration suite also tests rollback, concurrent writers and MVCC readers.
- **Result**: integration 51/51 (28 rollback, 1 concurrency, 1 MVCC, 3 parity tests). Parity held in every row:
  six-method v1 648/648, held-out v1 324/324, real v1 18/18, held-out v2 432/432, real v2 21/21, six-method v2 648/648,
  E5 112/112.

### 6.2 E2 — Crossover: is there a change size where FULL beats INCREMENTAL?

- **How**: the six-method matrix above, maintenance p50 per scenario.
- **Result**: no crossover on synthetic data. INCREMENTAL won every nonzero scenario (held-out v2: INCREMENTAL took
  24–57% of FULL's time at 1K, 16–50% at 10K, 12–43% at 100K). The reason is structural: in the synthetic graphs even a
  100% change is about one change per six edges, far below the break-even density 0.5. The real r3→r4 transition sits
  near it (4,407 changes, 9,437 edges, density ≈ 0.47) and is a tie: FULL 1.08× faster in September, INCREMENTAL 1.06×
  faster in October. E5 was added to test the region past the break-even.

### 6.3 E3 — Selector: does it pass the gate on unseen seeds, and does v2 err less than v1?

- **How**: held-out seeds 20261101/20261102 (never used), v1 and v2 measured side by side, 432 rows. Gate: pool all
  timed samples; adaptive p50 ≤ 1.05× and p95 ≤ 1.10× the better fixed method. **H1**: among the 92 nonzero scenarios,
  v2 exceeds 1.05× the better fixed method in fewer scenarios than v1. **H1a**: the same at 1K only. No tuning after
  seeing results.
- **Result**: both pass the gate (v2 p50 1.016, p95 0.958; v1 1.011, 0.997). H1 holds, 18 < 37 scenarios; H1a holds,
  13 < 28. Worst scenario: v2 1.25×, v1 4.22×. v2 chose INCREMENTAL in all 92, so on this data it at best ties fixed
  INCREMENTAL (per-scenario ratio at 1K: median 1.04, range 0.97–1.25). An earlier held-out run of v1 alone (seeds 20261001/20261002, 324 rows)
  also passed the pooled gate (1.018 / 1.017) while failing every scenario at 1K, which is what motivated v2.

### 6.4 E4 — Real data: parity and sensible decisions on real versions?

- **How**: a read-only copy of the metadata of a real competition dataset (RSNA Knee) with four versions r3 → r4 → r5 →
  r6; graph of 13,421 entities, 18,133 edges, 8,752 changes and 104 runs (October 18). Seven methods per transition.
- **Result**: parity 21/21. r3→r4 (4,407 changes, split-affecting): the split and the runs on it become STALE; both
  policies pick FULL (v2 1.066× INCREMENTAL's time). r4→r5 (4,345 metadata-only changes): affected runs become REVIEW;
  v2 picks INCREMENTAL (1.007×), v1 picked FULL (1.72×). r5→r6 (no changes): NO_OP. r4→r5 motivated v2, so this is a
  sanity check, not held-out evidence.

### 6.5 E5 — Break-even: past the break-even, does FULL win and does v2 switch to it?

- **How**: a dense family where density can be set: seven levels L1–L7 (0.17, 0.30, 0.40, 0.50 with modifications only;
  0.60, 0.75, 1.00 with additions), 1K and 10K entities, two seeds: 28 scenarios × 4 methods (FULL, INCREMENTAL, v2,
  v1), 112 rows. Each modification adds two edges and an addition one, so a modification-only diff stays below 0.5.
  Pre-registered: **H5a** a crossover exists (at some size and level, FULL ≤ 0.95× INCREMENTAL in every scenario; the
  crossover is the lowest level from which every higher level also qualifies); **H5b** v2 passes the same gate as E3
  across all 28 scenarios; **H5c** pooled over the scenarios at or above the crossover, v2's p50 is below fixed
  INCREMENTAL's.
  Every v1 and v2 choice was predicted and committed before measuring.
- **Result** (`WARN: H5a holds, H5b fails, H5c holds`): parity 112/112 and all 28 predicted choices matched.
  - H5a holds: the crossover is between 0.40 and 0.50 at both sizes, next to the model's 0.504; above it FULL is 9–23%
    faster.
  - H5b fails: v2 switches to FULL from 0.30, too early. At 10K, levels 0.30 and 0.40 are 1.66–3.11× slower than
    INCREMENTAL, and the pooled p95 ratio is 1.628 > 1.10 (p50 0.959 passes).
  - H5c holds: above the crossover v2 is faster than fixed INCREMENTAL (pooled p50 4,979 ms vs 5,856 ms, 0.85×).
  - After the measurement, profiling showed part of FULL's cost at 10K came from a VCP status computation that
    rescanned the graph for every score record; it was fixed in release 0.16.0 (statuses unchanged, 10K L1 FULL
    33.1 s → 8.3 s in a profile). E5's numbers describe the code as measured and stay as published.

---

## 7. Limits and concrete future work

- One machine, one writer, one PostgreSQL version; 1M entities not measured.
- `impact` and `explain` still run in Python after loading the graph; rewriting them as recursive SQL is the obvious next
  step (six-method v1 at 100K, after incremental maintenance: `impact` p50 7,249 ms on the PostgreSQL index versus
  6,134 ms for a replay, while the status read is 731 ms versus 6,127 ms; right after a FULL rebuild the status read is
  2,117 ms, because of the dead tuples).
- The v2 band is too wide just below the break-even (E5 L2–L4). A next policy would need calibration data on both sides
  of density 0.5; modification-only diffs reach just under 0.5, and going above it needs additions or removals.
- FULL leaves dead tuples until `VACUUM`; the index does not schedule vacuuming.
- Real version changes so far are either 0% or close to 100%; more real transitions with medium-sized changes would test
  the selector better than synthetic data.

---

## 8. Related work

Our work sits between two lines of database research and one kind of ML tool:

- **provenance capture in or over a DBMS** (ProvSQL, GProM): which input tuples explain an output tuple, computed for
  SQL queries and transactions;
- **incremental view maintenance** (counting and DRed, pg_ivm): keeping derived data correct when base data changes,
  without recomputing it from scratch;
- **ML pipeline lineage** (DVC): which files and parameters each pipeline step depended on, and whether it is up to date.

We take the *maintenance* question from the second line and apply it to the *artifact-level lineage* of the third, inside
PostgreSQL. What we add is a validity state per dataset version (VALID / STALE / REVIEW / BROKEN) and a per-change,
cost-based choice between incremental maintenance and a full rebuild.

| | Tracks | Granularity | Where it runs | Reaction to a change | Incremental vs full |
|---|---|---|---|---|---|
| **VCP (this project)** | artifact lineage + validity status per dataset version | dataset version, changed sample, artifact | derived index in PostgreSQL (or SQLite), computed from files | `dataset_diff` → dirty closure → statuses recomputed | chosen per diff by a calibrated cost model with an RMSE band; FULL published by pointer switch |
| ProvSQL [1, 2] | why/how provenance (semirings), where-provenance, probabilities | tuple (cell for where) | PostgreSQL extension rewriting queries via a planner hook | provenance computed when a query runs | not applicable |
| GProM [3, 4] | provenance of queries, updates and transactions (reenactment) | tuple, plus statement history | middleware that compiles provenance requests into SQL for the backend | on demand, also retroactively for past transactions | not applicable |
| Counting / DRed [5] | materialized views, including recursive ones | derived tuple | algorithm | counting (non-recursive); delete a superset, then re-derive (recursive) | incremental only |
| pg_ivm [6] | incrementally maintainable materialized views | view rows | PostgreSQL extension, AFTER triggers in the same transaction | delta per statement (counting) | the user disables it and refreshes by hand when a large part changes; no `WITH RECURSIVE` |
| DVC [7] | pipeline stages, dependency and output hashes, parameters | file / directory, parameter key | command-line tool, state in Git-tracked YAML | hashes compared with `dvc.lock`; changed stages and their downstream rerun | recomputes the check every time; no index |

**Closest to us, one system at a time.**

- **ProvSQL.** Similar: it keeps a provenance DAG for data inside PostgreSQL. Different: it answers "which input tuples
  produced this output tuple, and how" for SQL queries at query time; we store lineage between pipeline artifacts and
  maintain whether each is still valid when a new dataset version arrives. Its 2025 paper lists recursive queries as
  future work; our dirty closure is a `WITH RECURSIVE` walk.
- **GProM.** Similar: a provenance layer over a relational backend that deals with changing data (it reenacts past
  updates and transactions). Different: its provenance is computed on demand per query or transaction; ours is a
  persistent index whose statuses are maintained as data changes.
- **Counting and DRed.** Similar: DRed's "delete a superset of what might change, then re-derive what is still
  supported" has the same shape as our dirty closure followed by status recomputation. Different: DRed maintains view
  tuples exactly under set semantics and has no full-rebuild alternative; we maintain four lifecycle states on artifacts
  and choose between incremental and full per change. We do not claim formal equivalence with DRed.
- **pg_ivm.** Similar: the same incremental-versus-recompute trade-off inside PostgreSQL, and its documentation notes
  that incremental maintenance stops paying off when a large part of a table changes. Different: the user has to switch
  to a manual refresh in that case, while our policy makes the choice per change; and pg_ivm does not support
  `WITH RECURSIVE`, which our closure needs.
- **DVC.** Similar: file-level lineage and a staleness check driven by content hashes, close to our STALE. Different: no
  queryable index (staleness is recomputed from files on every check), no REVIEW or BROKEN states, and no cost-based
  choice.

If the slide has room for only three, ProvSQL, GProM and DRed cover the two database lines; DVC is the best
one-sentence contrast on the ML side.

**References** (checked against the publisher's DOI record, the paper's arXiv page, or the official repository or docs):

1. P. Senellart, L. Jachiet, S. Maniu, Y. Ramusat. ProvSQL: Provenance and Probability Management in PostgreSQL.
   PVLDB 11(12): 2034–2037, 2018. doi:10.14778/3229863.3236253.
2. A. Sen, S. Maniu, P. Senellart. ProvSQL: A General System for Keeping Track of the Provenance and Probability of Data.
   ICDE 2026; arXiv:2504.12058.
3. B. Arab, S. Feng, B. Glavic, S. Lee, X. Niu, Q. Zeng. GProM – A Swiss Army Knife for Your Provenance Needs. IEEE Data
   Engineering Bulletin 41(1): 51–62, 2018.
4. B. S. Arab, D. Gawlick, V. Krishnaswamy, V. Radhakrishnan, B. Glavic. Using Reenactment to Retroactively Capture
   Provenance for Transactions. IEEE TKDE 30(3): 599–612, 2018. doi:10.1109/TKDE.2017.2769056.
5. A. Gupta, I. S. Mumick, V. S. Subrahmanian. Maintaining Views Incrementally. SIGMOD 1993, pp. 157–166.
   doi:10.1145/170035.170066.
6. pg_ivm: Incremental View Maintenance for PostgreSQL. SRA OSS. https://github.com/sraoss/pg_ivm (README).
7. DVC documentation: Defining Pipelines; dvc.yaml and dvc.lock; dvc status. https://doc.dvc.org/ (accessed 2026-11-03).

---

## 9. Sources in this repository

| Claim | File |
|---|---|
| Schema, indexes, recursive SQL, ingest transaction | `src/vcp/provenance/postgres_schema.py`, `src/vcp/provenance/postgres.py` |
| Graph construction, entity and edge types | `src/vcp/provenance/graph.py`, `src/vcp/provenance/schema.py` |
| Status rules | `src/vcp/provenance/views.py` |
| Diff classification | `src/vcp/provenance/diff.py` |
| Decision rule, bands | `src/vcp/provenance/strategy.py`, `src/vcp/provenance/policy_bands.py` |
| E1–E4 numbers | `postgres-provenance-course-brief-v1.md`, `-course-brief-v2.md`, `-v1.md`, `-v2.md` and their JSON evidence files |
| E5 numbers | `postgres-provenance-break-even-v1.md`, `-course-brief-v3.md`, `postgres-provenance-break-even-v1.json` |
