# PostgreSQL Adaptive Provenance Benchmark v2（policy v2）

設計 spec：`docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`；實作計畫：同名 plan；量測期間的裁決：
`docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md` 裁決 8–12。v1 的證據紀錄
`postgres-provenance-v1.md` 不動。本檔每個數字都從 `docs/benchmarks/` 的 machine-readable 輸出回讀。

## 結論與證據邊界

- **正式驗收（held-out v2）通過**：policy v2 的 pooled p50 比值 1.0157（門檻 1.05）、p95 比值 0.9576（門檻 1.10）；
  432 列 parity 全部 true，216 個 adaptive 決策重算全部一致，`overlap_count` 0。
- **改善假設成立**：超過 1.05 倍的非 NO_OP 場景，v2 18/92、凍結的 v1 37/92（H1）；只看 1K 層，v2 13/28、v1 28/28（H1a）。
  v2 在 1K 的 28 個場景全部選 INCREMENTAL；v1 全部選 FULL，慢 1.82–4.22 倍。
- **真實資料（sanity check）**：RSNA r4→r5 v2 選 INCREMENTAL（實測為 incremental 的 1.007 倍），v1 選 FULL（1.72 倍）；
  r3→r4 兩者都選 FULL（1.066 倍）。21 列 parity 全部 true。
- **預覽（six-method v2，calibration seeds）**：648 列全部 ok；超過 1.05 倍的場景 18/92（同一算法下 six-method v1 為 31/92）。
- 證據邊界：同一台主機、同一個 workload 產生器、一組新 seeds；真實資料只有變更比例的兩端；v2 的分層帶在
  calibration 範圍外（約 1.3K edges 以下、約 200K edges 以上）只能用最近層外推（spec §13）。

## 命名對照（spec §3）

| 步驟 | 名稱 | 狀態 |
|---|---|---|
| 量測，擬合用 | calibration v2（`postgres-provenance-calibration-v2.json`，量測 commit `b1512ae`） | 沿用、不改 |
| 擬合 | policy v1 `postgres-adaptive-v1-9f4e58346529` | 凍結 |
| 候選帶比較 | band comparison v1（`postgres-provenance-band-comparison-v1.json`，腳本 commit `1823807`） | 完成 2026-10-10 |
| 擬合 | policy v2 `postgres-adaptive-v2-9f4e58346529`（`postgres-provenance-policy-v2.json`） | 隨 0.15.0 發布 2026-10-10 |
| 量測，正式驗收 | held-out v2（seeds 20261101、20261102，v1 與 v2 同場） | 完成 2026-10-18 |
| 量測，真實資料 | real RSNA v2（sanity check） | 完成 2026-10-18 |
| 量測，預覽 | six-method v2（calibration seeds） | 完成 2026-10-20 |

## 候選帶比較（spec §5，預先登記）

比較腳本與它的單元測試先 commit（`1823807`），之後才對 calibration v2 的 92 個觀測執行。判準：以 seed 交叉驗證
（一個 seed 擬合、另一個檢驗，兩個方向），在檢驗資料上算每個（方向、模型、decade）組合的標準化殘差均方根 `s`；
分數 = 所有組合中最大的 `|ln s|`，越小越好；兩個分數相差小於 0.1 時選 A。

| 候選 | 分數 | 決定分數的那一格 |
|---|---|---|
| A `relative`（帶寬 = 預估值 × 相對 RMSE） | 1.584 | 100K 的 full，`s` = 0.205（帶寬約為實際誤差的 5 倍，太寬） |
| **B `stratified_edges`**（每個 ⌊log10 total_edges⌋ 層各自的 RMSE） | **1.011** | 100K 的 incremental，`s` = 2.749（帶寬約為實際誤差的 0.36 倍，太窄） |

相差 0.573，不在平手範圍內，勝者 B。全資料的帶寬（毫秒；incremental / full）：

| | decade 3（1K，28 觀測） | decade 4（10K，32） | decade 5（100K，32） |
|---|---|---|---|
| v1（不分層） | 2,286.3 / 5,057.4 | 同左 | 同左 |
| B（policy v2） | 90.9 / 465.3 | 1,168.0 / 4,418.3 | 3,695.5 / 7,336.4 |
| A（落選） | 相對 RMSE `r_inc` 0.227、`r_full` 0.307 | 同左 | 同左 |

只報告、不拿來決定的數字：

- 在 calibration 的 92 個觀測上，A 與 B 都全部選 INCREMENTAL（1K 層 28/28），選了較慢一方 0 次。
- 留一層外推：B 往較大規模外推嚴重偏窄（不含 decade 5 擬合、檢驗 decade 5：incremental `s` 83.3、full 141.8），
  往較小規模外推偏寬（不含 decade 3：0.077 / 0.105）；A 分別是 7.6 / 12.8 與 0.68 / 1.42。

## Policy v2

- id `postgres-adaptive-v2-9f4e58346529`；`policy.json` SHA-256 `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000`。
- 成本模型與 v1 逐項相同（相對誤差 ≤ 1e-9）：`incremental_ms ≈ 1.162·changed_samples + 0.118·total_edges`、
  `full_ms ≈ 0.704·total_edges + 0.097·historical_changes`。只有信心帶不同。
- 選擇：`est_inc + rmse_inc[層] < est_full − rmse_full[層]` 時選 INCREMENTAL，否則 FULL；層 = ⌊log10 total_edges⌋，
  不在 strata 內就取最近層（距離相同取較大的 decade）。v1 的條件是固定的 `est_inc + 2,286 < est_full − 5,057`。
- 帶寬總和（rmse_inc + rmse_full）對 held-out v2 的預估差距（full − incremental）：

| 規模 | total_edges | 預估差距 | v1 帶寬 | v1 選擇 | v2 層 / 帶寬 | v2 選擇 |
|---|---|---|---|---|---|---|
| 1K | 1,336–1,982 | 842–845 ms | 7,343.7 ms | FULL | decade 3 / 556.2 ms | INCREMENTAL |
| 10K | 13,336–19,982 | 8,448–8,477 ms | 7,343.7 ms | INCREMENTAL | decade 4 / 5,586.3 ms | INCREMENTAL |
| 100K | 133,396–199,982 | 84,515–84,800 ms | 7,343.7 ms | INCREMENTAL | decade 5 / 11,031.9 ms | INCREMENTAL |

## 量測執行

- 主機與軟體與 v1 相同：AMD Ryzen 7 9700X（8C/16T）、31.2 GiB RAM、Windows 11 10.0.26200、Python 3.12.14、
  SQLite 3.53.1、PostgreSQL 17.11（`native_portable`，127.0.0.1:55432）；環境指紋 `f29fc1bb…` 與兩個 policy 相同。
- held-out v2 與 six-method v2 從 `v0.15.0`（`660d015`）的 detached worktree 量；real RSNA v2 從 `cafeea2`
  （裁決 9：real runner 另外複製 run 引用的收據與 evidence；`src/vcp` 與 v0.15.0 逐位元相同）。
- 由 repo 外的 `C:/vcp-data/bench/supervise.py` 看管：RAM 護欄、外來大程式、GPU 與 CPU 條件（裁決 12）、
  每日 06:55–08:45 UTC 靜默時段（裁決 10）；每次停下都丟棄進行中的場景與它的暫存資料庫，事後從逐列 checkpoint 續跑。
- 偏離原排程（裁決 8–12）：提前在 10/15 開跑；順序改為 held-out v2 → real RSNA v2 → six-method v2；Windows 自動維護
  期間完成的 4 個 held-out 場景依時段重量；six-method v2 兩個因單次重複失敗的場景重量。

| 量測 | 期間（UTC） | 列 | 計時樣本 | EXPLAIN 計畫 | parity | 輸出 SHA-256 |
|---|---|---|---|---|---|---|
| held-out v2 | 10/17 07:53 → 10/18 19:31 | 432 | 2,448 | 5,608 | 432/432 | `e4cca33d…5a48` |
| real RSNA v2 | 10/18 19:37 → 20:10 | 21 | — | 153 | 21/21 | `bb29448f…d75f`（repo 版） |
| six-method v2 | 10/18 20:13 → 10/20 17:17 | 648 | 3,672 | 4,208 | 648/648 | `5bbee9fa…5a63` |

## Held-out v2（正式驗收，spec §6.3、§7）

108 個場景 × 4 個方法（`postgres_full`、`postgres_incremental`、`postgres_adaptive` 用 v2、`postgres_adaptive_v1`
用凍結的 v1）× 7/7/3 次重複；`VERDICT cmd=provenance.evaluate status=OK h1=true h1a=true`。

**§7.2 正式效能 gate**（每種方法把全部場景的原始樣本 pool 起來；ms）

| | pg_full | pg_incr | adaptive v2 | adaptive v1 |
|---|---|---|---|---|
| pooled p50 | 5,969 | 1,001 | 1,016 | 1,012 |
| pooled p95 | 115,148 | 36,140 | 34,609 | 36,027 |
| p50 比值（≤ 1.05） | | | **1.0157 PASS** | 1.0115 |
| p95 比值（≤ 1.10） | | | **0.9576 PASS** | 0.9969 |

**§7.3 診斷**（非 NO_OP 場景；比值 = adaptive p50 / 較佳固定方法 p50）

| 層 | v2 超過 1.05 | v2 最差 | v1 超過 1.05 | v1 最差 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| decade 3（1K） | 13/28 | 1.245 | 28/28 | 4.219 | INCREMENTAL 28、NO_OP 8 | FULL 28、NO_OP 8 |
| decade 4（10K） | 5/32 | 1.086 | 8/32 | 1.119 | INCREMENTAL 32、NO_OP 4 | INCREMENTAL 32、NO_OP 4 |
| decade 5（100K） | 0/32 | 1.032 | 1/32 | 1.489 | INCREMENTAL 32、NO_OP 4 | INCREMENTAL 32、NO_OP 4 |
| 合計 | **18/92** | 1.245 | **37/92** | 4.219 | | |

1K 的 28 個非零場景，實測都是 incremental 較快；v2 那 13 個超過 1.05 的場景與較佳固定方法用的是同一個策略，比值
0.97–1.25（中位 1.04），是兩次量測之間的重複變異。v1 的 28 個是 FULL，1.82–4.22 倍（中位 2.96）。

**§7.4 改善假設**：H1 成立（18 < 37）；H1a 成立（13 < 28）。

**Maintenance p50（ms；每格 = chain/branched × 2 seeds 四個場景的中位數）**

**1K**

| ratio | pg_full | pg_incr | adaptive v2 | adaptive v1 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| 0 | 117 | 123 | 130 | 132 | NO_OP | NO_OP |
| 0.001 | 135 | 132 | 153 | 149 | NO_OP | NO_OP |
| 0.01 | 597 | 155 | 162 | 635 | INCREMENTAL | FULL |
| 0.05 | 605 | 175 | 178 | 628 | INCREMENTAL | FULL |
| 0.1 | 652 | 188 | 195 | 641 | INCREMENTAL | FULL |
| 0.25 | 676 | 239 | 253 | 705 | INCREMENTAL | FULL |
| 0.5 | 687 | 278 | 292 | 713 | INCREMENTAL | FULL |
| 0.9 | 774 | 388 | 402 | 806 | INCREMENTAL | FULL |
| 1 | 842 | 428 | 455 | 809 | INCREMENTAL | FULL |

**10K**

| ratio | pg_full | pg_incr | adaptive v2 | adaptive v1 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| 0 | 663 | 672 | 685 | 693 | NO_OP | NO_OP |
| 0.001 | 5,945 | 997 | 1,020 | 1,005 | INCREMENTAL | INCREMENTAL |
| 0.01 | 6,067 | 1,015 | 1,018 | 1,025 | INCREMENTAL | INCREMENTAL |
| 0.05 | 6,387 | 1,162 | 1,164 | 1,183 | INCREMENTAL | INCREMENTAL |
| 0.1 | 6,583 | 1,366 | 1,372 | 1,389 | INCREMENTAL | INCREMENTAL |
| 0.25 | 7,193 | 1,909 | 2,061 | 2,001 | INCREMENTAL | INCREMENTAL |
| 0.5 | 7,681 | 2,941 | 2,922 | 2,902 | INCREMENTAL | INCREMENTAL |
| 0.9 | 8,858 | 4,239 | 4,249 | 4,208 | INCREMENTAL | INCREMENTAL |
| 1 | 9,292 | 4,137 | 4,179 | 4,207 | INCREMENTAL | INCREMENTAL |

**100K**

| ratio | pg_full | pg_incr | adaptive v2 | adaptive v1 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| 0 | 6,838 | 6,870 | 6,835 | 6,910 | NO_OP | NO_OP |
| 0.001 | 88,210 | 11,650 | 11,772 | 11,634 | INCREMENTAL | INCREMENTAL |
| 0.01 | 93,221 | 12,489 | 12,233 | 12,171 | INCREMENTAL | INCREMENTAL |
| 0.05 | 90,050 | 14,262 | 14,443 | 14,719 | INCREMENTAL | INCREMENTAL |
| 0.1 | 102,175 | 18,532 | 18,581 | 18,018 | INCREMENTAL | INCREMENTAL |
| 0.25 | 91,531 | 24,058 | 24,013 | 24,041 | INCREMENTAL | INCREMENTAL |
| 0.5 | 109,639 | 36,173 | 36,204 | 36,096 | INCREMENTAL | INCREMENTAL |
| 0.9 | 133,694 | 53,134 | 53,424 | 52,624 | INCREMENTAL | INCREMENTAL |
| 1 | 143,381 | 54,272 | 52,197 | 53,984 | INCREMENTAL | INCREMENTAL |

逐場景比值（非 NO_OP；最小–最大，括號為中位數）：

| 規模 | adaptive v2 / 較佳固定 | adaptive v1 / 較佳固定 | pg_incr / pg_full |
|---|---|---|---|
| 1K | 0.97–1.25（1.04） | 1.82–4.22（2.96） | 24%–57%（35%） |
| 10K | 0.96–1.09（1.01） | 0.96–1.12（1.01） | 16%–50%（24%） |
| 100K | 0.86–1.03（1.00） | 0.85–1.49（0.99） | 12%–43%（22%） |

在 held-out v2 自己的資料上，12 個（topology、entities、seed）切片都沒有出現 full 比 incremental 快的變更比例。

## Real RSNA v2（sanity check，spec §6.4）

四個 `rsna-knee-sixslot` 資料集（r3 → r4 → r5 → r6）唯讀複製（metadata-only，含被選中 run 引用的收據與 evidence）；
canonical 圖 13,421 entities / 18,133 edges / 8,752 changes / 0 gaps；104 個 run（real v1 為 7 個 run、13,196 entities，
不是同一張圖）。SQLite 對 canonical 的 `graph_parity`、`status_parity` 為 true，`status_differences` 空，`verify_index` ok。
真實 diff：r3→r4 4,407 個變更、r4→r5 4,345、r5→r6 0。

| transition | 前圖 entities → changed（比例） | method | selected | maint p50 / p95 ms | status p50 | impact p50 | storage MiB | samples/s |
|---|---|---|---|---|---|---|---|---|
| r3→r4 | 263 → 4,407（100.0%） | canonical_full | FULL | 1,446 / 1,595 | 800 | 788 | — | 3,047 |
| | | sqlite_full | FULL | 3,369 / 3,467 | 23.2 | 351 | 60 | 1,308 |
| | | sqlite_incremental | INCREMENTAL | 1,527 / 1,560 | 22.0 | 211 | 60 | 2,886 |
| | | postgres_full | FULL | 7,221 / 8,589 | 102 | 584 | 117 | 610 |
| | | postgres_incremental | INCREMENTAL | 6,788 / 8,414 | 107 | 475 | 143 | 649 |
| | | postgres_adaptive（v2） | FULL | 7,234 / 7,399 | 167 | 474 | 117 | 609 |
| | | postgres_adaptive_v1 | FULL | 7,110 / 7,917 | 102 | 505 | 149 | 620 |
| r4→r5 | 9,074 → 4,345（98.7%） | canonical_full | FULL | 2,623 / 3,108 | 1,401 | 1,449 | — | 1,656 |
| | | sqlite_full | FULL | 5,658 / 5,919 | 36.1 | 825 | 98 | 768 |
| | | sqlite_incremental | INCREMENTAL | 2,277 / 2,325 | 35.2 | 798 | 97 | 1,908 |
| | | postgres_full | FULL | 12,109 / 12,474 | 123 | 1,140 | 294 | 359 |
| | | postgres_incremental | INCREMENTAL | 6,888 / 7,304 | 133 | 1,166 | 210 | 631 |
| | | postgres_adaptive（v2） | **INCREMENTAL** | 6,936 / 7,162 | 119 | 1,137 | 210 | 626 |
| | | postgres_adaptive_v1 | FULL | 11,824 / 12,900 | 112 | 976 | 294 | 367 |
| r5→r6 | 13,420 → 0（0.0%） | canonical_full | FULL | 2,928 / 3,016 | 1,349 | 1,418 | — | — |
| | | sqlite_full | FULL | 5,445 / 5,792 | 37.1 | 839 | 98 | — |
| | | sqlite_incremental | NO_OP | 885 / 905 | 35.9 | 800 | 98 | — |
| | | postgres_full | NO_OP | 1,943 / 2,077 | 70.5 | 958 | 181 | — |
| | | postgres_incremental | NO_OP | 2,042 / 2,147 | 71.1 | 983 | 181 | — |
| | | postgres_adaptive（v2） | NO_OP | 1,943 / 1,958 | 71.8 | 1,041 | 181 | — |
| | | postgres_adaptive_v1 | NO_OP | 1,884 / 2,081 | 183 | 978 | 181 | — |

預估與選擇：

- r3→r4：incremental 6,238 對 full 6,641 ms，差 403 ms；edges 9,437 → decade 3 帶寬 556 ms > 403 ms → v2 FULL；v1 也 FULL。
  實測 FULL 是 incremental 的 1.066 倍（full 的預估 6,641 低於實測 7,221）。
- r4→r5：7,194 對 13,185 ms，差 5,991 ms；edges 18,130 → decade 4 帶寬 5,586 ms < 5,991 ms → v2 INCREMENTAL；
  v1 帶寬 7,343.7 ms > 5,991 ms → FULL。實測 v2 是 incremental 的 1.007 倍、v1 是 1.72 倍。
- r5→r6：`verified_zero_semantic_changes` → 兩者 NO_OP。
- r4→r5 是 v2 的設計動機，只能證明「v2 在這個已知案例上的選擇」，不是 held-out 證據（spec §6.4）。

## Six-method v2（預覽，spec §6.2）

calibration seeds（20260913、20260914），108 個場景 × 6 個方法 × 7/7/3 次重複，`postgres_adaptive` 用 policy v2；
648 列全部 ok、parity 1.0；PostgreSQL 列通過 `evaluate_adaptive.validate_rows`，每個 adaptive 決策都以 policy v2 重算一致；
12 個切片都沒有交會點。這組 seeds 也是 calibration 的 seeds，所以只是預覽，不是驗收。

預覽數字用 held-out v2 的算法（全部場景 pool；比值 = adaptive / 較佳固定方法）；v1 欄是同一算法對 six-method v1
（2026-09-18，policy v1）重算的結果（v1 證據紀錄裡的 1.010 / 0.932 是只 pool 非 NO_OP 場景、另一種 p95 算法）：

| | six-method v2（policy v2） | six-method v1（policy v1） |
|---|---|---|
| pooled p50 比值 | 1.0277 | 1.0180 |
| pooled p95 比值 | 1.0452 | 0.9581 |
| 超過 1.05 / 最差 | 18/92，1.163 | 31/92，3.973 |
| 1K | 13/28，最差 1.163，INCREMENTAL 28 | 28/28，最差 3.973，FULL 28 |
| 10K | 2/32，最差 1.065 | 1/32，最差 1.065 |
| 100K | 3/32，最差 1.118 | 2/32，最差 1.140 |

100K 兩次預覽的選擇完全相同（INCREMENTAL），p95 比值的差（1.045 對 0.958）來自相隔一個月的兩次量測之間的變異，不是 policy。

**Maintenance p50（ms；每格四個場景的中位數）**

**1K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 105 | 224 | 59.2 | 129 | 131 | 142 | NO_OP |
| 0.001 | 101 | 217 | 58.9 | 125 | 127 | 136 | NO_OP |
| 0.01 | 105 | 222 | 65.2 | 633 | 155 | 172 | INCREMENTAL |
| 0.05 | 105 | 233 | 68.0 | 614 | 163 | 179 | INCREMENTAL |
| 0.1 | 106 | 231 | 73.9 | 616 | 184 | 188 | INCREMENTAL |
| 0.25 | 110 | 238 | 84.9 | 658 | 228 | 239 | INCREMENTAL |
| 0.5 | 115 | 262 | 104 | 710 | 289 | 306 | INCREMENTAL |
| 0.9 | 125 | 287 | 126 | 815 | 395 | 413 | INCREMENTAL |
| 1 | 136 | 295 | 146 | 810 | 452 | 445 | INCREMENTAL |

**10K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 817 | 2,036 | 618 | 690 | 695 | 715 | NO_OP |
| 0.001 | 793 | 1,970 | 615 | 6,346 | 1,001 | 1,031 | INCREMENTAL |
| 0.01 | 789 | 1,967 | 631 | 6,256 | 1,018 | 1,053 | INCREMENTAL |
| 0.05 | 774 | 1,987 | 650 | 6,019 | 1,148 | 1,150 | INCREMENTAL |
| 0.1 | 794 | 2,012 | 714 | 6,529 | 1,318 | 1,340 | INCREMENTAL |
| 0.25 | 871 | 2,173 | 875 | 6,947 | 1,896 | 1,902 | INCREMENTAL |
| 0.5 | 1,000 | 2,459 | 1,121 | 7,665 | 2,799 | 2,782 | INCREMENTAL |
| 0.9 | 1,229 | 2,959 | 1,533 | 8,863 | 4,271 | 4,232 | INCREMENTAL |
| 1 | 1,211 | 2,896 | 1,591 | 9,312 | 4,201 | 4,105 | INCREMENTAL |

**100K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 9,545 | 23,595 | 7,189 | 6,802 | 6,751 | 6,895 | NO_OP |
| 0.001 | 9,518 | 22,676 | 7,301 | 87,479 | 11,443 | 11,465 | INCREMENTAL |
| 0.01 | 10,225 | 24,652 | 7,971 | 100,114 | 13,215 | 13,354 | INCREMENTAL |
| 0.05 | 10,050 | 24,130 | 8,249 | 86,170 | 14,483 | 14,446 | INCREMENTAL |
| 0.1 | 10,050 | 24,489 | 9,027 | 89,076 | 17,306 | 17,444 | INCREMENTAL |
| 0.25 | 10,477 | 26,739 | 11,113 | 99,509 | 23,809 | 24,154 | INCREMENTAL |
| 0.5 | 11,828 | 29,303 | 14,434 | 111,343 | 34,145 | 35,287 | INCREMENTAL |
| 0.9 | 13,913 | 33,777 | 18,405 | 124,317 | 48,189 | 47,901 | INCREMENTAL |
| 1 | 14,300 | 34,912 | 19,881 | 124,724 | 52,239 | 52,157 | INCREMENTAL |

逐場景比值（非 NO_OP；最小–最大，括號為中位數）：adaptive v2 / 較佳固定 1K 0.93–1.16（1.05）、10K 0.94–1.07（1.01）、
100K 0.95–1.12（1.00）；pg_incr / pg_full 1K 22%–60%（35%）、10K 15%–50%（23%）、100K 12%–45%（22%）。

## 可重跑命令

都在 detached worktree 裡執行，PostgreSQL 只用 service name（`VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE` 以路徑指定）：

```bash
git worktree add --detach ../Vision-contest-pipeline-v0150-bench v0.15.0
uv sync --frozen --extra postgres
uv run python tests/performance/provenance/evaluate_adaptive.py --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --comparison-policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --output docs/benchmarks/postgres-provenance-heldout-v2.json --work-dir <work>/heldout-v2-work
uv run python tests/performance/provenance/adaptive_benchmark.py --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --output docs/benchmarks/postgres-provenance-six-method-v2.json --work-dir <work>/six-method-v2-work
```

real RSNA v2 從 `cafeea2` 的 detached worktree：

```bash
uv run python tests/performance/provenance/real_validation.py --data-root <RSNA data root> --configs-root <RSNA configs root> --six-method --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --comparison-policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --output docs/benchmarks/postgres-provenance-real-rsna-v2.json
```

## Acceptance record（spec §7）

| 項目 | 條件 | 結果 |
|---|---|---|
| §7.1 parity | 每一列 true | 432/432 |
| §7.1 決策重算 | 每個 adaptive 列與它的 policy 一致 | 216/216（v2 108、v1 108） |
| §7.1 overlap | `overlap_count` 0 | 0 |
| §7.2 p50（v2） | ≤ 1.05 | **1.0157 PASS** |
| §7.2 p95（v2） | ≤ 1.10 | **0.9576 PASS** |
| §7.2 並列（v1） | 不影響 gate | 1.0115 / 0.9969 |
| §7.3 診斷 | 照報 | v2 18/92（1K 13/28）、v1 37/92（1K 28/28） |
| §7.4 H1 | v2 超標數 < v1 | **成立**（18 < 37） |
| §7.4 H1a | 只看 decade 3 | **成立**（13 < 28） |
| §6.4 real | sanity check | 21/21 parity；r4→r5 v2 INCREMENTAL、v1 FULL |
| §6.2 six-method | 預覽 | 648/648 ok；18/92（v1 同算法 31/92） |

## 檔案與雜湊

| 檔案 | SHA-256 | 量測 commit |
|---|---|---|
| `postgres-provenance-band-comparison-v1.json` | `ade06945cc2411f950d9a4f25346b934fd1dd86af151ed934f4b0ce3a610b16c` | 腳本 `1823807` |
| `postgres-provenance-policy-v2.json` | `81ec2eba938765209ae1cbf461f0ddad8a07e4debee34db627ee036327f0f770` | — |
| `…-policy-v2-artifacts/…/postgres-adaptive-v2-9f4e58346529/policy.json` | `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000` | — |
| `…-calibration-v2-artifacts/…/postgres-adaptive-v1-9f4e58346529/policy.json` | `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78` | — |
| `postgres-provenance-heldout-v2.json` | `e4cca33d04ea2b981a031afbe4b6a8edf64acfbe12e837f4a621594e2cc85a48` | `660d015` |
| `postgres-provenance-real-rsna-v2.json`（repo 版，路徑相對化，裁決 11） | `bb29448f60db052c1d51e180437c356b09824b7b923a3eb3ea01a97e8e1ad75f` | `cafeea2` |
| 同上，量測機上的原檔 | `e9eac27f34c667f37596e99feefb8bc3ac8fc99d9f57ff79893e37508d529d4d` | `cafeea2` |
| `postgres-provenance-six-method-v2.json` | `5bbee9fad2d20b609ccd7e67a01951096cbedd37ef1e202f00d8d73d01ac5a63` | `660d015` |
| 同上，第一次發布（2 列失敗，裁決 12；量測機） | `d5adce06877e27f2dd9336867f5cb58b02320c461927462668707a4b5b151682` | `660d015` |
