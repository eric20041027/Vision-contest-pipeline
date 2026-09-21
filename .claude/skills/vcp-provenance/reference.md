# Provenance 參考

## Status 語意（每個 head 下每個實體一個）
| status | 意思 | 誰決定 |
|---|---|---|
| `VALID` | 上游在這個 head 下沒有影響它的變更 | 圖 |
| `STALE` | 上游 sample 有 semantic 變更（labels / views / group 等），結果要重做 | diff 的 `semantic_effects` |
| `REVIEW` | 上游有未知或未分類的變更（預設 `meta.*`、policy 標 UNKNOWN），要人判 | policy plugin 或預設 |
| `BROKEN` | 上游實體被移除或證據消失 | 圖 |

`dataset_diff` 的每個 `SampleChange`：`change_type ADDED|REMOVED|MODIFIED`、`changed_domains[]`（LABELS、VIEWS、GROUP、META、LABEL_SOURCE…）、`semantic_effects[]`（TRAINING_AFFECTING、EVALUATION_AFFECTING、SPLIT_AFFECTING、INPUT_AFFECTING、UNKNOWN）；`grade=fallback` 表示沒有 policy plugin 介入。比賽專屬的分類以 `register_impact_policy` 登記、`data diff --plugin … --policy …` 使用。

## Strategy 與 reason code
| requested | selected | reason |
|---|---|---|
| 任何 | `NO_OP` | `verified_zero_semantic_changes`（verified 零變更仍寫 transition、checkpoint、fingerprint，複製 status）或 `duplicate_artifact_no_op` |
| `incremental` | `INCREMENTAL` | `requested_incremental` |
| `full` | `FULL` | `requested_full` |
| `auto` + policy | `INCREMENTAL` / `FULL` | `calibrated_incremental_lower_confident_cost`（`inc + inc_rmse < full − full_rmse`）/ `calibrated_full_lower_or_uncertain_cost` |
| `auto` 無 policy | `FULL` | `fallback_policy_absent_full`（WARN） |

policy v1 `postgres-adaptive-v1-9f4e58346529`：`incremental_ms ≈ 1.16·changed_samples + 0.118·total_edges`、`full_ms ≈ 0.704·total_edges + 0.097·historical_changes`；RMSE 2,286 / 5,057 ms → 帶寬 7,343 ms 絕對值。policy 不相容（環境指紋、PostgreSQL major、schema 版本）時明示 `--policy` FAIL、未明示則 FULL fallback。

## PostgreSQL 機制（給解釋用）
12 張正規化表、13 個索引、CHECK 約束；`WITH RECURSIVE` 求 dataset ancestors / dirty closure / descendants；writer 以 `pg_advisory_xact_lock` 序列化；full rebuild 寫新 generation → exact read-back parity → 切換 `active_generation` 指標 → `DELETE` 舊 generation；讀者在切換前看舊 generation（MVCC）。舊列在 VACUUM 前是 dead tuples（storage ≈ 2×，`status` 查詢變慢）。錯誤訊息去敏：不含 service、host、port、role、conninfo、driver 例外文字。

## 修復
| 症狀 | 處置 |
|---|---|
| `sync` 拒絕：既有 record 改寫 / 刪除 / prefix drift | 人工查明是誰動了台帳；確認後 `rebuild` |
| diff manifest 或 payload 不符 | 該 artifact 不可信；重做 `data diff` 到新 id |
| PostgreSQL schema marker 不相容 | 換乾淨 database / service，`rebuild`；不做 silent migration |
| `verify-index` 不一致 | 刪索引重 `rebuild`；仍不一致就是 canonical 資料本身的問題 |

## 證據檔（`docs/benchmarks/`）
| 檔 | 內容 |
|---|---|
| `postgres-provenance-integration-v3.json/.xml` | live integration 51/51（rollback 28、concurrency 1、MVCC 1、parity 3），PostgreSQL 17.11 |
| `postgres-provenance-calibration-v2.json` + `-artifacts/` | 1,224 次量測 → policy artifact |
| `postgres-provenance-six-method-v1.json` | 108 場景 × 6 方法，648 列，parity 648/648，無交會點 |
| `postgres-provenance-heldout-v1.json` | seeds 20261001/2，324 列，overlap 0，aggregate gate PASS 1.018 / 1.017，every-scenario 在 1K FAIL |
| `postgres-provenance-real-rsna-v1.json` | 3 個真實 transition，18 列，parity 18/18；一個 transition 選錯 FULL 慢 1.79× |
| `postgres-provenance-v1.md` / `-report-v1.md` / `-course-brief-v1.md` | acceptance record / 十項總結 / 簡報素材 |

`tests/unit/provenance/test_postgres_docs.py` 斷言 evidence record 與 handoff 的精確字串（含「沒有 Absent 格」）——改這兩份文件要同步改測試。

## 基準命令（opt-in preflight 後）
```bash
uv run python tests/performance/provenance/adaptive_benchmark.py --entities 1000 10000 100000 --ratios 0 0.001 0.01 0.05 0.10 0.25 0.50 0.90 1 --seeds 20260913 20260914 --policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --output <NEW> [--work-dir <CHECKPOINTS>]
uv run python tests/performance/provenance/calibrate_adaptive.py --output <NEW>
uv run python tests/performance/provenance/evaluate_adaptive.py --policy-from …calibration-v2.json --output <NEW>
uv run python tests/performance/provenance/real_validation.py --data-root <RO> --configs-root <RO> --six-method --policy-from …calibration-v2.json --output <NEW>
```
輸出路徑必須尚不存在。calibration 與 held-out 用不同 seeds、不同 process；held-out 不 fit。每方法 612 個 timed samples（1K/10K 各 7 次、100K 3 次）。發版或改原始檔前先讓量測跑完；量測期間別在該 worktree 改任何檔。
