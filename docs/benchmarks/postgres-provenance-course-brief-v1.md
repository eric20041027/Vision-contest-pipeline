# PostgreSQL Adaptive Provenance：課程簡報素材完整版（v1，2026-09-20）

> 給 Data_Base 專案裡負責做簡報的 agent。這份文件把 2026-09-13 → 2026-09-20 這條研究線的**全部**資訊集中在一處：
> 背景、系統設計、實驗方法、五份證據的完整數字、發現與解讀、執行紀錄與教訓、限制、建議的簡報大綱、術語表、
> 檔案清單。所有數字都從 `docs/benchmarks/` 的 machine-readable 結果回讀，來源檔在 §10；**沒有估計值**，沒量到的事
> 寫「未量」。做簡報時請只用這裡的數字，不要外推或美化；負面發現（§5-2）要和正面結果並列。

課程：CS 5151/6051 Database Theory 小組專案（Group 1）。時程：progress report 2026-10-13、slides 2026-11-17、
final report 2026-12-07。專案母體：`vcp`（vision contest pipeline，開源，MIT，GitHub `eric20041027/Vision-contest-pipeline`）。

---

## 0. 三十秒版本

- **問題**：機器學習比賽的資料集會一版一版演化；每次改版後，「哪些訓練／評測結果因此失效」要靠 provenance 圖判定。
  這張圖可以整個重算（full），也可以只更新受影響的部分（incremental）。**用 PostgreSQL 做 provenance 索引時，
  能不能依變更規模自動選 NO_OP / INCREMENTAL / FULL，讓維護延遲優於任何固定策略？**
- **做法**：在既有的 canonical provenance（檔案為真相來源）之上加一個 optional 的 PostgreSQL backend（正規化 schema、
  FK/CHECK、13 個索引、`WITH RECURSIVE` 求 dirty closure、advisory lock 序列化、generation pointer 原子發布、
  MVCC 讀者），加一個以 calibration 資料 fit 出來、凍結成 immutable artifact 的 adaptive selector。
- **實驗**：六種方法（canonical replay、SQLite full/incremental、PostgreSQL full/incremental/adaptive）× 1K/10K/100K
  entities × 9 個變更比例 × 2 種拓樸 × 2 seeds；calibration 與 held-out 用不同 seeds、不同 process；每次量測 fresh
  database；每一列都對 canonical replay 做 exact parity；另加真實 RSNA 資料四個版本。
- **結果**：五份 live 證據齊全（integration 51/51；calibration 1,224 次；six-method 3,672 次；held-out 1,836 次；
  real RSNA 18 列），parity 全部一致。**沒有交會點**——incremental 在每個非零變更比例都比 full 快（100K 時 full 要
  75–140 s，incremental 是它的 13–52%）。**held-out 的正式 aggregate gate 通過**（adaptive / 較佳固定策略 = 1.018 / 1.017）。
- **但**：selector 的信心帶是絕對毫秒，導致 1K 場景全部選錯（慢 1.8–4.3 倍）、真實資料的一個 transition 也選錯
  （慢 1.79 倍）；PostgreSQL 全量重建後 dead tuples 使儲存變 2 倍；單機上 SQLite incremental 反而最快。這些都如實列入。

---

## 1. 背景與研究問題

### 1.1 vcp 的 provenance 是什麼

`vcp` 是一個給視覺比賽用的通用框架，強調「證據不可竄改」：資料集卡片、切分計畫、預登記、讀數台帳都是 append-only
或 write-once 的檔案。0.7.0 加入 **dataset evolution**：兩個資料集版本之間的差異寫成 immutable 的 `dataset_diff`
artifact（每個 sample 的 ADDED / REMOVED / MODIFIED、變更的 domain 與語意效果），provenance graph 從這些 artifact
與各台帳 **重放（canonical replay）** 得到；每個 head（資料集版本）下的每個實體有 materialized status：
`VALID / STALE / REVIEW / BROKEN`。SQLite 是預設的**衍生索引**（可刪、可重建），不是真相來源。

### 1.2 研究問題（spec §1 原文）

> 在 canonical VCP evidence、graph semantics 與 exact parity 不變的前提下，PostgreSQL provenance backend 能否根據
> dataset change size、dirty closure size 與 graph size，對每次 verified `dataset_diff` deterministic 地選擇 `NO_OP`、
> `INCREMENTAL` 或 `FULL`，使 held-out workloads 的 maintenance latency 優於固定 full 與固定 incremental 策略？

比較三種 PostgreSQL 維護方法（full / incremental / adaptive），SQLite 保留為 embedded baseline，canonical replay 為
oracle。研究**不**重新發明 diff、graph 或 status 語意，也**不**以效能結果放寬正確性。

### 1.3 動機（歷史 SQLite 數字，只是動機不是結論）

| 歷史事件數 | SQLite incremental + query p95 | Full load + fingerprint p95 | 倍率 |
|---:|---:|---:|---:|
| 1,000 | 55 ms | 21 ms | 0.39× |
| 10,000 | 61 ms | 176 ms | 2.88× |
| 100,000 | 205 ms | 2,114 ms | 10.34× |
| 1,000,000 | 2,427 ms | 23,731 ms | 9.78× |

SQLite 在 1K 時 incremental 反而慢、10K–100K 之間有交會點——這是「adaptive 可能有價值」的起點，也是後來
PostgreSQL 結果要對照的預期。

---

## 2. 系統設計

### 2.1 邊界：canonical vs derived

- 真相永遠在檔案：JSONL / YAML 台帳、`dataset_diff` artifact（`changes.jsonl` + `summary.json`，write-once）。
- SQLite 與 PostgreSQL 都是 **noncanonical 衍生索引**：可以刪掉重建；任何 evidence 消失、改寫或 ledger prefix drift
  都 **fail closed**（拒絕服務、不做 silent repair）；full rebuild 後以 canonical snapshot hash 驗證（`canonical_drift`）。
- 未指定 `--backend` 永遠用 SQLite；PostgreSQL 是 optional extra（`uv sync --extra postgres`，lazy import psycopg 3）。

### 2.2 PostgreSQL schema v1（`vcp_provenance`）

12 張正規化表、13 個索引、大量 CHECK 約束（hash 欄位 `char(64)` + 十六進位檢查、enum 以 CHECK IN 列舉、陣列以 `<@` 限制）：

| 表 | 角色 |
|---|---|
| `generations` | 每次 full rebuild 一個 generation：state（building/ready）、schema_version、graph_hash、record count/sum、canonical_snapshot_hash |
| `active_generation` | singleton 指標：目前對外可見的 generation |
| `metadata` | schema marker |
| `entities` | 實體（dataset / run / sample …），索引 type、dataset |
| `provenance_edges` | 一般 provenance 邊（edge_type 以 CHECK 列舉），索引 source、target |
| `dataset_edges` | 資料集版本之間的 explicit edge（來自 dataset_diff），索引 target |
| `sample_changes` | 每個 sample 的變更事件：change_type、changed_domains[]、semantic_effects[]、before/after row hash；索引 sample、transition、target、type |
| `dataset_edge_changes` | edge ↔ change 對應 |
| `entity_status` | 每個 head 下每個實體的 materialized status（VALID/STALE/REVIEW/BROKEN）與 predecessor；索引 head+state、entity+head、predecessor |
| `ingest_checkpoints` | 增量讀取台帳的 consumed_bytes / prefix_sha256 / last_event_id（prefix drift 偵測） |
| `ingested_artifacts` | 已吃過的 dataset_diff（manifest sha）→ 重複 ingest 冪等 |
| `maintenance_decisions` | 每次 ingest 的決策遙測：requested/selected strategy、reason code、features、estimated costs、policy version |

四段 `WITH RECURSIVE`：dataset ancestors、**dirty closure**（從變更的 sample 沿邊往上找受影響實體）、dataset
descendants、affected path changes。

### 2.3 交易、鎖與發布

- 每個 writer 以 `pg_advisory_xact_lock` 序列化（整合測試：兩個 writer 同時 ingest 同一個 diff，第二個看到 no-op）。
- **Full rebuild**：在新 generation 寫入整張圖與所有 head 的 status → exact read-back parity → 更新 `active_generation`
  指標 → `DELETE` 舊 generation（cascade）。讀者在指標切換前一直看到一致的舊 generation（MVCC）。
- **Incremental**：驗證 diff 與輸入 → 寫 delta（entities / edges / changes / dataset edge）→ SQL recursive dirty
  closure → 只重算 dirty set 的 status，其他 head 只補新增實體；不載入歷史 payload、不重 hash 整張圖。
- **NO_OP**：verified diff 的 `total_changes == 0` 時仍寫 transition、artifact 紀錄、checkpoint、fingerprint 並複製
  status，只省略 dirty propagation——所以圖仍與 canonical replay 完全相同。
- 連線只透過 libpq **service**（`PGSERVICEFILE` / `PGPASSFILE` 以路徑指定）；VCP 不接受連線 URI 或憑證欄位、不讀
  credential file；錯誤訊息去敏（不含 service、host、port、role、conninfo、driver 例外文字）。

### 2.4 Adaptive selector 與 policy artifact

- 特徵（全部是 verified 的實際值，不是估計）：`changed_samples`、`dirty_entities`、`dirty_ratio`、`total_edges`、
  `head_count`（incremental 模型）；`total_entities`、`total_edges`、`historical_changes`、`head_count`（full 模型）。
- Calibration 用 scikit-learn `LinearRegression(positive=True)`（非負係數、截距截為 0）對 p50 latency fit 兩個線性成本模型，
  連同 RMSE、training row count、PostgreSQL major、schema version、environment fingerprint 一起寫進 **immutable policy
  artifact**（`artifacts/provenance_policy/<policy_id>/{spec,policy,calibration,manifest}.json`）。
- 決策規則（spec §10.2）：(1) verified change count = 0 → `NO_OP`；(2) 明示 full / incremental 照做；(3) `auto` 且 policy
  相容時，**`estimated_incremental + incremental_rmse < estimated_full − full_rmse` 才選 INCREMENTAL，否則 FULL**。
  RMSE 構成由資料推導的 noise band，沒有 hard-coded 的 dirty-ratio 門檻。
- Fail-safe：明示 `--policy` 但 artifact 缺失／被改／不相容 → FAIL 不做維護；`auto` 未給 policy → 零變更 NO_OP、其餘
  FULL 並回 WARN；policy 只能選演算法，不能改 status 語意。
- 每次決策寫入 `maintenance_decisions`（derived telemetry），CLI 的 VERDICT 也回報 requested/selected/reason/estimated costs。

### 2.5 CLI

`vcp provenance rebuild|sync|ingest|impact|stale|explain|status|verify-index [--backend sqlite|postgresql] [--pg-service S]`；
`ingest` 另接受 `--strategy incremental|full|auto` 與 `--policy <ID>`。每個命令以
`VERDICT cmd=… status=OK|WARN|FAIL|ABORT …` 收尾，exit 0/1/2，永不互動。

---

## 3. 實驗方法

### 3.1 六種方法

| 方法 | 角色 |
|---|---|
| `canonical_full` | oracle：從檔案重放整張圖（正確性基準，也是速度對照） |
| `sqlite_full` | SQLite 衍生索引整個重建 |
| `sqlite_incremental` | SQLite 增量維護（既有 0.7.0 路徑） |
| `postgres_full` | PostgreSQL 新 generation 全量重建 + 原子發布 |
| `postgres_incremental` | PostgreSQL 增量維護（recursive dirty closure） |
| `postgres_adaptive` | `--strategy auto` + frozen policy，每次量測時由 selector 決定 NO_OP / INCREMENTAL / FULL |

### 3.2 合成矩陣（normative）

- 規模 1K / 10K / 100K entities（**1M 於 2026-09-15 裁決移出**，見 §7）；變更比例 0、0.1%、1%、5%、10%、25%、50%、
  90%、100%；拓樸 chain / branched；每個 split 2 個 seeds → **108 場景**。
- 重複次數：1K、10K 各 7 次，100K 各 3 次 → 每方法 612 個 timed samples。**每次重複用全新的 database**（`vcp_bench_<hash>`，
  用完即棄）；fixture 生成、初始發布、parity、storage 與 instrumentation **不計入** maintenance 時間。
- 16 個 NO_OP 格（所有 ratio 0 + 1K 的 0.1%——四捨五入後 0 個變更）在 fit 與 gate 中排除。
- Calibration split seeds `20260913 / 20260914`；held-out split seeds `20261001 / 20261002`（另一個 process、另一個
  worktree；evaluator 以 scenario_id、scenario_hash、workload_hash 三種身分比對，要求 overlap 0；不 refit）。
- 每個場景的每次重複都做 **exact parity**：graph（normalized）、graph hash、每個 materialized status、leaf heads、
  stored heads，對 canonical replay 逐項比對；任何一項不同整列 fail。
- 每個 PostgreSQL 列另在額外 fresh state、rollback-only 交易中對每種 SQL 形狀的第一個 DML 列做
  `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`（不計入 timing；保存原始與 allowlisted/sanitized 兩份）。
- 查詢面另量 `status`（public API）、`impact` 與 `explain`（含 graph load，同 CLI 行為）的 p50/p95；storage 為
  PostgreSQL `pg_total_relation_size` 總和、SQLite database bytes（口徑不同，不互相比率）。

### 3.3 Normative gate（held-out）

pool 全部 matched valid（非 NO_OP）場景的 raw samples：**adaptive aggregate p50 ≤ 較佳固定策略 p50 × 1.05**，
**p95 ≤ × 1.10** → `performance_pass`。另報每場景比值與更嚴的 every-scenario diagnostic；兩者都要報、不得互相取代。

### 3.4 Real track

四個真實資料集 `rsna-knee-sixslot` r3 → r4 → r5 → r6（RSNA Knee，各約 4,400 個 sample，7 個 run card）**唯讀複製**
（metadata-only）到暫存目錄，三個 transition 各以六方法量測（frozen policy），另驗 SQLite 對 canonical 的
graph / status parity 與 `verify-index`。

### 3.5 執行基礎設施（都在 repo 外的 `C:/vcp-data/bench/`）

- Runner 自帶 **per-repetition checkpoint 與續跑**：run 合約釘住 168 個原始檔的雜湊、Python / PostgreSQL / 硬體身分；
  任一檔改動即拒絕續跑（避免「量到一半改程式」）。
- `bench_launch.py`：以路徑設定 PostgreSQL env；RAM 護欄（可用 < 2 GiB 持續 30 秒即中止，寫 launcher 摘要）。
- `supervise.py`：只在機器空閒（可用 RAM ≥ 6–8 GiB、無 ≥ 4 GiB 的外來程式）時啟動；偵測到遊戲／訓練／低記憶體就
  停下、刪除進行中場景的 checkpoint、drop 用完即棄的 database，空出來再自動續跑。以 WMI 獨立於編輯器 session 啟動。

### 3.6 硬體與軟體

AMD Ryzen 7 9700X（8C/16T）、31.2 GiB RAM、NVMe 999 GB；Windows 11 10.0.26200；Python 3.12.14；psycopg 3.3.5；
SQLite 3.53.1；PostgreSQL 17.11（170011，原生 Windows 服務 `native_portable`，只聽 127.0.0.1:55432）。單一 writer、
桌面程式在背景。

---

## 4. 結果（完整數字）

### 4.1 Live integration（2026-09-14，commit `d8cc334`）

`uv run pytest tests/integration/provenance`：**51 passed / 0 failed / 0 errors / 0 skipped**，58.4 s。類別：
transaction rollback 28/28、concurrency 1/1（advisory lock 序列化並發 writer）、MVCC 1/1（讀者在 rebuild commit 前看舊
generation）、canonical / SQLite / PostgreSQL parity 3/3（incremental / full / auto）。前兩次嘗試保留為證據：v1 FAIL
（39 passed、4 behavior failures）、v2 ERROR（51 setup errors，PostgreSQL 程序被回收）。GitHub CI 的 `postgres` job
（ubuntu、Docker Compose、17.11）每個 PR 都通過，但沒有保存成 write-once record。

### 4.2 Calibration v2（2026-09-16，14.5 小時，commit `b1512ae`）

108 場景 × postgres_full / postgres_incremental × 7/7/3 次 = **1,224 次量測全 ok**，parity 1224/1224；RAM 護欄未觸發
（最低可用 2.63 GiB）。92 個 fit 觀測 → policy **`postgres-adaptive-v1-9f4e58346529`**，`policy.json` SHA-256
`a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`，`calibration_sha256` `9f4e5834…`。

成本模型：`incremental_ms ≈ 1.16·changed_samples + 0.118·total_edges`；`full_ms ≈ 0.704·total_edges + 0.097·historical_changes`
（`dirty_entities`、`dirty_ratio`、`head_count`、`total_entities` 係數被非負約束壓到 0）；RMSE incremental **2,286 ms**、
full **5,057 ms**（量級由 100K 主導）。12/12 切片 crossover `not_observed`。

（發布時撞到生產者／消費者 schema 漂移：`runtime_environment()` 新增兩個 OS 欄位，evaluator 的 strict 模型沒宣告，
216 列全被拒；修模型後**不重跑**，以外部腳本逐檔證明 168 個生產端檔案與 run 合約完全一致後從 checkpoint 發布，
記在 `.provenance.json`。）

### 4.3 Six-method v1（2026-09-17/18，calibration split，commit `6ab2b7a`）

108 × 6 = **648 列全 ok**、3,672 次量測、graph / hash / head parity 648/648；crossover 12/12 `not_observed`；
adaptive 決策 1K FULL ×28、10K/100K INCREMENTAL ×64、NO_OP ×16。324 個 PostgreSQL 列含 4,236 個 EXPLAIN 計畫。

**1K**（maintenance p50，ms；每格 = chain/branched × 2 seeds 四個場景的中位數）

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 111 | 265 | 65.5 | 150 | 148 | 163 | NO_OP |
| 0.001 | 113 | 272 | 66.2 | 155 | 158 | 166 | NO_OP |
| 0.01 | 115 | 272 | 69.9 | 663 | 181 | 696 | FULL |
| 0.05 | 117 | 284 | 78.4 | 673 | 198 | 692 | FULL |
| 0.1 | 121 | 288 | 86.3 | 701 | 215 | 686 | FULL |
| 0.25 | 128 | 301 | 98.0 | 725 | 265 | 751 | FULL |
| 0.5 | 130 | 321 | 114 | 766 | 322 | 784 | FULL |
| 0.9 | 136 | 340 | 134 | 856 | 417 | 887 | FULL |
| 1 | 140 | 347 | 154 | 876 | 448 | 896 | FULL |

**10K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 792 | 2,015 | 606 | 702 | 710 | 721 | NO_OP |
| 0.001 | 781 | 1,995 | 634 | 6,368 | 1,055 | 1,070 | INCREMENTAL |
| 0.01 | 810 | 2,043 | 650 | 6,433 | 1,040 | 1,070 | INCREMENTAL |
| 0.05 | 854 | 2,141 | 726 | 6,801 | 1,258 | 1,258 | INCREMENTAL |
| 0.1 | 854 | 2,175 | 763 | 6,787 | 1,419 | 1,427 | INCREMENTAL |
| 0.25 | 918 | 2,245 | 890 | 7,118 | 1,899 | 1,961 | INCREMENTAL |
| 0.5 | 1,051 | 2,602 | 1,193 | 7,738 | 2,924 | 2,920 | INCREMENTAL |
| 0.9 | 1,253 | 2,955 | 1,545 | 9,169 | 4,131 | 4,145 | INCREMENTAL |
| 1 | 1,278 | 3,077 | 1,648 | 9,343 | 4,246 | 4,253 | INCREMENTAL |

**100K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 9,631 | 23,768 | 7,630 | 7,171 | 7,010 | 6,671 | NO_OP |
| 0.001 | 9,855 | 22,853 | 7,760 | 89,170 | 12,050 | 11,776 | INCREMENTAL |
| 0.01 | 9,921 | 23,966 | 7,874 | 85,416 | 12,666 | 12,485 | INCREMENTAL |
| 0.05 | 10,073 | 24,096 | 8,652 | 87,697 | 14,481 | 14,799 | INCREMENTAL |
| 0.1 | 10,394 | 25,006 | 9,154 | 88,469 | 17,603 | 17,912 | INCREMENTAL |
| 0.25 | 10,493 | 26,474 | 10,937 | 96,426 | 25,523 | 24,700 | INCREMENTAL |
| 0.5 | 11,867 | 29,050 | 14,189 | 112,101 | 35,872 | 35,198 | INCREMENTAL |
| 0.9 | 13,994 | 34,351 | 18,729 | 118,614 | 54,344 | 51,148 | INCREMENTAL |
| 1 | 14,679 | 34,471 | 19,497 | 120,891 | 50,511 | 55,681 | INCREMENTAL |

ratio 0 那列的三個 PostgreSQL 方法是 NO_OP（只驗證零變更），不是重建成本；100K 的 pg_full 在 ratio 0（7.2 s）與
0.001（89 s）之差就是一次全量重建。

**maintenance p95（ms，四個場景的中位數）**

| scale | ratio | sqlite_incr | pg_full | pg_incr | pg_adaptive |
|---|---|---|---|---|---|
| 1K | 0.01 | 83.6 | 698 | 295 | 764 |
| 1K | 0.5 | 128 | 835 | 363 | 900 |
| 1K | 1 | 161 | 949 | 557 | 971 |
| 10K | 0.01 | 676 | 6,892 | 1,113 | 1,124 |
| 10K | 0.5 | 1,250 | 8,775 | 3,110 | 2,989 |
| 10K | 1 | 1,685 | 10,187 | 4,322 | 4,440 |
| 100K | 0.01 | 8,437 | 92,367 | 13,198 | 12,847 |
| 100K | 0.5 | 14,366 | 114,923 | 42,716 | 36,802 |
| 100K | 1 | 19,887 | 127,547 | 54,855 | 55,777 |

**比值摘要（排除 NO_OP，逐場景）**

| scale | pg_incr / pg_full | sqlite_incr / sqlite_full | pg_incr / sqlite_incr | pg_full / canonical | adaptive / pg_incr |
|---|---|---|---|---|---|
| 1K | 27–56%（中位 37%） | 25–46%（33%） | 2.40–3.23×（2.69×） | 4.9–6.7×（5.9×） | 1.82–3.97×（2.78×） |
| 10K | 16–50%（24%） | 31–54%（37%） | 1.57–2.76×（2.07×） | 6.9–8.7×（7.7×） | 0.92–1.06×（1.01×） |
| 100K | 13–52%（23%） | 30–58%（40%） | 1.52–3.54×（2.10×） | 7.4–10.1×（8.8×） | 0.90–1.14×（1.00×） |

**查詢延遲、儲存與吞吐（非零 ratio 場景的中位數）**

| scale | method | impact p50 / p95 ms | status p50 / p95 ms | explain p50 / p95 ms | index MiB | storage MiB | throughput samples/s |
|---|---|---|---|---|---|---|---|
| 1K | canonical_full | 56.0 / 74.1 | 55.9 / 71.6 | 51.6 / 67.1 | — | — | 418 |
| 1K | sqlite_full | 22.5 / 35.2 | 4.6 / 5.7 | 17.0 / 31.1 | — | 6 | 190 |
| 1K | sqlite_incr | 22.5 / 36.7 | 3.6 / 4.9 | 17.2 / 28.6 | — | 6 | 569 |
| 1K | pg_full | 64.6 / 82.7 | 27.5 / 37.1 | 60.8 / 94.0 | 18 | 25 | 80 |
| 1K | pg_incr | 66.2 / 88.3 | 27.8 / 47.9 | 59.7 / 78.5 | 8 | 12 | 227 |
| 1K | pg_adaptive | 65.8 / 87.0 | 27.7 / 39.5 | 60.9 / 89.2 | 18 | 25 | 77 |
| 10K | canonical_full | 521 / 573 | 530 / 587 | 404 / 524 | — | — | 609 |
| 10K | sqlite_full | 374 / 414 | 27.9 / 69.1 | 273 / 299 | — | 59 | 246 |
| 10K | sqlite_incr | 373 / 403 | 25.4 / 66.7 | 268 / 307 | — | 60 | 657 |
| 10K | pg_full | 538 / 621 | 99.3 / 166 | 441 / 512 | 181 | 247 | 81 |
| 10K | pg_incr | 545 / 623 | 82.0 / 153 | 454 / 543 | 81 | 119 | 329 |
| 10K | pg_adaptive | 557 / 621 | 69.3 / 128 | 442 / 535 | 81 | 119 | 331 |
| 100K | canonical_full | 6,134 / 6,276 | 6,127 / 6,692 | 4,750 / 5,229 | — | — | 537 |
| 100K | sqlite_full | 4,560 / 4,833 | 257 / 579 | 3,211 / 3,383 | — | 590 | 221 |
| 100K | sqlite_incr | 4,591 / 4,645 | 263 / 602 | 3,179 / 3,389 | — | 596 | 546 |
| 100K | pg_full | 6,523 / 6,764 | 2,117 / 2,360 | 4,720 / 5,052 | 1,826 | 2,467 | 61 |
| 100K | pg_incr | 7,249 / 7,610 | 731 / 784 | 4,780 / 5,047 | 812 | 1,188 | 232 |
| 100K | pg_adaptive | 7,175 / 7,423 | 718 / 805 | 4,818 / 5,207 | 812 | 1,188 | 247 |

Storage 逐場景範圍（100K）：pg_full relation 2.05–2.90 GiB、index 1.44–2.16 GiB；pg_incr 1.07–1.57 / 0.73–1.08 GiB；
SQLite 552–767 MiB。

### 4.4 Held-out v1（2026-09-19/20，seeds 20261001/20261002，commit `2644e83`）

108 場景 × 3 個 PostgreSQL 方法 = **324 列全 ok**、1,836 次量測；graph / hash / head / status parity 324/324；
**`overlap_count` 0**；crossover 12/12 `not_observed`；決策模式與 calibration split 相同；`VERDICT status=OK`。

**Normative gate（pool 92 個非 NO_OP 場景）**

| 指標 | pg_full | pg_incr | pg_adaptive | adaptive / 較佳 fixed | 門檻 | 結果 |
|---|---|---|---|---|---|---|
| aggregate p50 ms | 6,029 | 998 | 1,017 | **1.018** | ≤ 1.05 | **PASS** |
| aggregate p95 ms | 106,290 | 34,070 | 34,664 | **1.017** | ≤ 1.10 | **PASS** |

`performance_pass: true`；`every_scenario_pass: false`：36/92 場景 > 1.05——1K 全部 28 個（1.82–4.26；p95 比 1.42–4.60）、
10K 2 個（≤ 1.06）、100K 6 個（≤ 1.28，3 次重複的雜訊）。Held-out 逐場景比值：1K 22–54%（pg_incr/pg_full，中位 34%）、
10K 15–49%（23%）、100K 13–47%（22%）；adaptive/incr 1K 1.82–4.26、10K 0.97–1.06、100K 0.93–1.28。

**Held-out 對 calibration split（maintenance p50，ms；括號 = held-out / six-method 的比值）**

| scale | ratio | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|
| 1K | 0.01 | 604（0.91） | 151（0.83） | 612（0.88） | FULL |
| 1K | 0.05 | 606（0.90） | 168（0.85） | 638（0.92） | FULL |
| 1K | 0.1 | 598（0.85） | 175（0.82） | 619（0.90） | FULL |
| 1K | 0.25 | 676（0.93） | 233（0.88） | 666（0.89） | FULL |
| 1K | 0.5 | 693（0.91） | 268（0.83） | 705（0.90） | FULL |
| 1K | 0.9 | 773（0.90） | 385（0.93） | 768（0.87） | FULL |
| 1K | 1 | 793（0.91） | 424（0.95） | 817（0.91） | FULL |
| 10K | 0.001 | 6,098（0.96） | 980（0.93） | 985（0.92） | INCREMENTAL |
| 10K | 0.01 | 6,427（1.00） | 1,014（0.97） | 1,036（0.97） | INCREMENTAL |
| 10K | 0.05 | 6,428（0.95） | 1,144（0.91） | 1,167（0.93） | INCREMENTAL |
| 10K | 0.1 | 6,640（0.98） | 1,327（0.94） | 1,355（0.95） | INCREMENTAL |
| 10K | 0.25 | 6,778（0.95） | 1,847（0.97） | 1,853（0.94） | INCREMENTAL |
| 10K | 0.5 | 7,567（0.98） | 2,721（0.93） | 2,734（0.94） | INCREMENTAL |
| 10K | 0.9 | 9,007（0.98） | 4,069（0.98） | 4,095（0.99） | INCREMENTAL |
| 10K | 1 | 9,061（0.97） | 4,094（0.96） | 4,128（0.97） | INCREMENTAL |
| 100K | 0.001 | 86,268（0.97） | 11,460（0.95） | 11,482（0.98） | INCREMENTAL |
| 100K | 0.01 | 90,939（1.06） | 12,128（0.96） | 12,054（0.97） | INCREMENTAL |
| 100K | 0.05 | 84,528（0.96） | 14,222（0.98） | 14,298（0.97） | INCREMENTAL |
| 100K | 0.1 | 88,470（1.00） | 16,814（0.96） | 17,060（0.95） | INCREMENTAL |
| 100K | 0.25 | 104,999（1.09） | 26,012（1.02） | 25,661（1.04） | INCREMENTAL |
| 100K | 0.5 | 101,849（0.91） | 33,545（0.94） | 34,486（0.98） | INCREMENTAL |
| 100K | 0.9 | 128,333（1.08） | 51,159（0.94） | 51,763（1.01） | INCREMENTAL |
| 100K | 1 | 132,862（1.10） | 55,093（1.09） | 56,089（1.01） | INCREMENTAL |

（ratio 0 與 1K 的 0.001 為 NO_OP，略。）Held-out 查詢／儲存（100K 非 NO_OP 中位數）：`status` p50 pg_full 2,133 /
pg_incr 726 / adaptive 686 ms；`impact` 7.0–7.6 s；relation 2,445 / 1,188 / 1,188 MiB；throughput 52 / 233 / 216 samples/s。

### 4.5 Real RSNA v1（2026-09-20，commit `32f0f65`，19 分鐘）

canonical 圖 13,196 entities / 17,569 edges / 8,752 changes / 0 gaps；SQLite 對 canonical `graph_parity` /
`status_parity` true、`status_differences` 空、`verify_index ok`；18 列全 ok、parity 18/18；9 個 PostgreSQL 列含 115 個
EXPLAIN 計畫。真實 diff：r3→r4 4,407 changes（4,403 MODIFIED 全在 GROUP domain——split 群組換掉、4 REMOVED）；
r4→r5 4,345（全是 META domain、effect UNKNOWN → REVIEW）；r5→r6 0（samples hash 相同）。

| transition | 前圖 entities → changed（比例） | method | selected | maint p50 / p95 ms | status p50 | impact p50 | storage MiB | samples/s |
|---|---|---|---|---|---|---|---|---|
| r3→r4 | 38 → 4,407（100%） | canonical_full | FULL | 782 / 854 | 279 | 470 | — | 5,635 |
| | | sqlite_full | FULL | 2,250 / 2,366 | 26 | 200 | 59 | 1,959 |
| | | sqlite_incremental | INCREMENTAL | 1,688 / 1,735 | 25 | 353 | 59 | 2,611 |
| | | postgres_full | FULL | 5,625 / 6,500 | 81 | 566 | 113 | 784 |
| | | postgres_incremental | INCREMENTAL | 6,056 / 6,476 | 97 | 470 | 142 | 728 |
| | | postgres_adaptive | **FULL** | 5,768 / 5,908 | 99 | 449 | 136 | 764 |
| r4→r5 | 8,849 → 4,345（98.7%） | canonical_full | FULL | 1,337 / 1,541 | 784 | 827 | — | 3,251 |
| | | sqlite_full | FULL | 3,683 / 3,767 | 41 | 576 | 97 | 1,180 |
| | | sqlite_incremental | INCREMENTAL | 2,107 / 2,167 | 34 | 576 | 96 | 2,062 |
| | | postgres_full | FULL | 10,320 / 11,164 | 118 | 735 | 290 | 421 |
| | | postgres_incremental | INCREMENTAL | 5,865 / 7,097 | 117 | 878 | 208 | 741 |
| | | postgres_adaptive | **FULL（選錯，1.79×）** | 10,502 / 10,707 | 102 | 732 | 290 | 414 |
| r5→r6 | 13,195 → 0（0%） | canonical_full | FULL | 1,353 / 1,545 | 770 | 494 | — | — |
| | | sqlite_full | FULL | 3,365 / 3,465 | 37 | 535 | 97 | — |
| | | sqlite_incremental | NO_OP | 913 / 926 | 36 | 594 | 97 | — |
| | | postgres_full | NO_OP | 1,318 / 1,359 | 71 | 799 | 179 | — |
| | | postgres_incremental | NO_OP | 1,421 / 1,454 | 68 | 715 | 179 | — |
| | | postgres_adaptive | NO_OP | 1,363 / 1,406 | 66 | 698 | 179 | — |

adaptive 的估計：r3→r4 incremental 6,172 vs full 6,244 ms（幾乎打平 → 不確定 → FULL，實測 FULL 反而略快 1.08×）；
r4→r5 7,127 vs 12,788 ms（差 5.7 s < 7.3 s 的絕對信心帶 → FULL，實測選錯）；r5→r6 `verified_zero_semantic_changes`。

---

## 5. 發現與解讀（每條都附「簡報上可以怎麼講」）

### 5-1 沒有交會點：incremental 在每個非零變更比例都贏

三份合成證據 36/36 切片 `not_observed`。原因可以用成本模型講清楚：`full ≈ 0.704·edges + 0.097·hist`、
`incremental ≈ 1.16·changed + 0.118·edges`，而這個 workload 的 `changed ≤ 0.25·edges`，所以 full 永遠多付約 `0.586·edges`
的固定成本。**與 SQLite 的歷史動機（10K–100K 之間有交會點）相反**——PostgreSQL 的全量重建成本結構不同。
簡報講法：「adaptive 的答案是『幾乎永遠 incremental』；full 存在的理由是損毀修復、索引漂移與 schema 不相容，不是效能。」

### 5-2 Selector 選錯：信心帶是絕對毫秒（誠實的負面發現）

規則 `inc + 2,286 < full − 5,057` 要求預估差距 > 7.3 s 才敢選 INCREMENTAL。1K 的差距只有 ≈ 843 ms → 28/28 選 FULL，
慢 1.82–4.26 倍；真實 RSNA r4→r5（8.8K entities、98.7% 變更）差距 5.7 s → 也選錯，慢 1.79 倍；10K/100K 差距 ≥ 8.4 s
→ 全部正確。**這不是「小圖」問題，是「預估差距」問題**。Aggregate gate 仍通過（1.018 / 1.017），因為 pool 的
p50 落在 10K 區間、p95 落在 100K 區間，1K 的 196 個 sample（38%）拉不動它——這正是為什麼 spec 要求同時報
every-scenario diagnostic（36/92 FAIL）。處置：policy v1 凍結不改（改了就是拿 held-out 回頭調 selector = leakage）；
規模相對的信心帶列為 policy v2；操作指南建議小圖與近全量變更的 transition 直接 `--strategy incremental`。
簡報講法：兩句話並列——「v1 policy 通過正式 gate」＋「v1 policy 在 1K 每個場景都選錯」。

### 5-3 PostgreSQL 全量重建很慢，而且留下 dead tuples

100K 一次 75–140 s，是 canonical replay 的 7.4–10.1 倍、SQLite full 的 3.0–4.1 倍（整張圖以 `INSERT … ON CONFLICT`
寫進正規化表、維護 13 個索引、再切換 generation）。重建後舊 generation 的列已 `DELETE` 但在 VACUUM 前仍佔空間：
storage 約 2 倍（2.05–2.90 GiB 對 1.07–1.57 GiB）、`status` 查詢 2.1 s 對 0.73 s。這是 MVCC 原子切換的教科書代價；
待裁決是否在 rebuild 收尾加 `VACUUM`。簡報講法：用 relation size 對比圖講 MVCC bloat。

### 5-4 單機上 SQLite incremental 最快

100K：SQLite incremental 7.5–19.8 s，PostgreSQL incremental 是它的 1.5–3.5 倍；查詢面 `status` 在 1K 慢 6–10 倍
（連線與 round-trip），`impact` / `explain` 1.3–2.3 倍。PostgreSQL 的價值不是單 writer 速度，而是 advisory lock
序列化、MVCC 讀者、原子 generation 發布與可驗證的 rollback / concurrency 行為。簡報講法：「選 PostgreSQL 是為了
多人／多程序的一致性，不是為了快。」

### 5-5 三種 backend 對 canonical replay 完全一致

648/648、324/324、18/18、1224/1224 逐列 exact parity；整合測試 parity 3/3；真實資料 `status_differences` 空。
這是研究問題裡「correctness 不變」的前提被實際證明。

### 5-6 Policy 在未見過的 seeds 上決策一致、絕對時間受主機狀態影響

held-out 的決策模式與 calibration split 完全相同；絕對毫秒比 six-method 低 5–20%（held-out 在重開機後、驅動洩漏清除的
主機上跑）。gate 用比值所以不受影響；跨檔案比較絕對數字時要註明。

---

## 6. 執行紀錄與教訓（可做「lessons learned」一頁）

| 日期（UTC） | 事件 | 處置／教訓 |
|---|---|---|
| 09-13 | Codex 完成 Task 1–12 實作（0.8.0），三次終審 clean | 每任務一次 spec + 品質審查 |
| 09-14 | 本機原生 PostgreSQL 17.11 服務建立；integration v1 FAIL → v2 ERROR → v3 PASS 51/51 | 失敗的嘗試也保存成證據 |
| 09-14 | Codex 的 calibration v1 與 1M memory gate v2–v4 被 RAM 護欄中止（runner 16.3 GB） | 1M 移出正式矩陣（裁決 §1） |
| 09-15 | 探路矩陣（非正式，1 次重複）8.5 h：已看出無交會點 | 探路數字不填正式表 |
| 09-16 | 正式 calibration v2 14.5 h 完成；發布時撞到 schema 漂移 | 不重跑，逐檔驗證後從 checkpoint 發布；加契約測試 |
| 09-17 | six-method 第一次啟動時遊戲在跑 → 17 分鐘後護欄中止，資料作廢 | 寫看管程式：機器空閒才跑 |
| 09-17 17:50 | 看管程式把 NVIDIA Overlay（4 GB）誤判為外來大程式，停了 3 小時 | 加忽略清單 |
| 09-18 01:09 | 遊戲啟動 → 30 秒內停、丟棄進行中場景；02:39 自動續跑 | 設計如預期 |
| 09-18 12:26 / 13:41 | 兩次 RAM 護欄中止；診斷出主機核心 nonpaged pool 洩漏 7.6 GiB（8.6 天未重開） | 重開機；最後 5 個場景 2 h 跑完 |
| 09-18 | six-method 發布；發現 1K 全部 FULL（§5-2） | policy 凍結，記錄裁決 |
| 09-19/20 | held-out 18 h 後被使用者誤啟動的訓練（16 GB python）觸發護欄；看管程式當時把所有 python 當自己人 | 最後一個完成場景有汙染跡象（adaptive/incr 1.15 > 六方法最大 1.06）→ 丟棄重量；看管程式改為只認命令列帶 bench 標記的 python |
| 09-20 | held-out 發布：gate PASS；real RSNA 19 分鐘完成；十項報告 | 五份證據齊全 |
| 09-20 | CI 抓到 access receipt id 的 16-bit nonce 在同一秒內撞號（flaky） | nonce 加寬到 64 bit + 碰撞測試 |

通用教訓：(1) 長時間量測要有 checkpoint／續跑與「原始樹不變」守門；(2) 護欄要保護 timing 的乾淨，不只保護機器不當機；
(3) 產生者／消費者共用 schema 的離線測試必須用真的產生者輸出；(4) 沒有 machine-readable 結果回讀前不填任何表格；
(5) 負面發現照報，並用 aggregate + diagnostic 兩種口徑避免互相掩蓋。

---

## 7. 限制與誠實聲明

1. **1M 未量**：31 GiB 主機跑不完（需 ≥128 GB），結論適用 1K–100K；補 1M 必須換機器並重跑 calibration → held-out。
2. **Selector v1 的絕對信心帶**（§5-2）：小圖與近全量變更會選 FULL。
3. **單一主機、單一 writer**：concurrency / MVCC 只有整合測試的行為證據，沒有多 writer 吞吐量測。
4. **主機狀態**：六方法期間有驅動記憶體洩漏與桌面程式；held-out 在重開機後；絕對毫秒相差 5–20%，比值一致。
5. **真實資料只有變更比例的兩端**（100% / 98.7% / 0%）；中段靠合成矩陣。
6. **EXPLAIN 只涵蓋 DML**，不含 `status` / `impact` 查詢自身的計畫。
7. **Linux CI 只有容器內的 integration**，沒有 Linux host 的 live record。

未來工作：policy v2（差距／預估值的相對信心帶或分層 RMSE，重跑三段）；full rebuild 後 `VACUUM`；查詢計畫的 EXPLAIN；
多 writer 併發吞吐；1M。

---

## 8. 建議的簡報大綱（14 頁）

1. 題目與一句話結論（§0）
2. 背景：資料集演化與 provenance 圖；VALID / STALE / REVIEW / BROKEN（§1.1）
3. 研究問題與動機表（§1.2–1.3）
4. 架構：canonical 檔案 vs 衍生索引；fail-closed 原則（§2.1）
5. PostgreSQL schema：12 表、13 索引、CHECK、recursive CTE（§2.2；可畫 ER 圖）
6. 交易與發布：advisory lock、generation pointer、MVCC 讀者（§2.3；時序圖）
7. Adaptive selector：兩個線性成本模型、RMSE 信心帶、immutable policy artifact（§2.4）
8. 實驗設計：六方法、108 場景、fresh DB、exact parity、calibration/held-out 分離（§3）
9. 結果一：maintenance p50 對變更比例（三個規模各一張折線圖，§4.3 表）
10. 結果二：沒有交會點 + 成本模型解釋（§5-1）
11. 結果三：held-out gate PASS 表 + every-scenario FAIL（§4.4、§5-2）——兩句話並列
12. 結果四：real RSNA 表（§4.5）與 selector 選錯的真實案例
13. 結果五：儲存與 MVCC bloat；SQLite vs PostgreSQL 的定位（§5-3、§5-4）
14. 限制、教訓、未來工作（§6、§7）

圖表建議：折線圖用 §4.3 的三張表（x = ratio，y = p50，log 刻度）；長條圖用 §4.4 gate 表；relation size 對比用
§4.3 storage 欄（pg_full 2,467 vs pg_incr 1,188 MiB）；真實資料用 §4.5 的三組長條。

---

## 9. 術語表

| 詞 | 意思 |
|---|---|
| canonical replay | 從檔案（台帳 + dataset_diff artifact）重放整張 provenance 圖；正確性的唯一基準 |
| derived / noncanonical index | SQLite 或 PostgreSQL 裡的圖副本；可刪可重建；不一致就 fail closed |
| dataset_diff | 兩個資料集版本之間的 immutable 差異 artifact（`changes.jsonl` + `summary.json`） |
| head | 一個資料集版本（圖上的葉節點），每個 head 下每個實體有一個 status |
| dirty closure | 從變更的 sample 沿邊往上找出所有受影響實體（`WITH RECURSIVE`） |
| NO_OP / INCREMENTAL / FULL | 三種維護路徑：零變更只驗證、只更新 dirty set、整張圖重建成新 generation |
| generation / active_generation | 一次 full rebuild 的結果 / 目前對外可見的那一份（原子指標切換） |
| policy artifact | 凍結的成本模型 + RMSE + 環境指紋，內容定址、不可改；selector 只能讀它 |
| calibration / held-out split | 用來 fit policy 的場景 / 用來評分的場景，seeds 不同、身分比對 overlap 0 |
| aggregate gate / every-scenario diagnostic | 正式驗收（pool 所有 sample 的 p50/p95 比值）/ 逐場景比值（只報不裁決） |
| parity | graph、hash、status、heads 對 canonical replay 逐項完全相同 |
| crossover point | full 開始比 incremental 快的變更比例；本研究 36/36 切片 `not_observed` |
| dead tuples | PostgreSQL MVCC 下被 DELETE / UPDATE 後、VACUUM 前仍佔空間的舊版本列 |

---

## 10. 檔案清單（repo `docs/benchmarks/`，完整 SHA-256）

| 檔案 | 內容 | SHA-256 |
|---|---|---|
| `postgres-provenance-integration-v3.json` / `.xml` | live integration 51/51 | `b55f34fd…` / `b50ada0f459c5ddce82bc4da23cf6850a702b5a5416cdf6972b626e766b61823` |
| `postgres-provenance-calibration-v2.json` + `-artifacts/` + `.provenance.json` | calibration 1,224 次、policy artifact、發布註記 | `43d213e4…`；policy.json `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78` |
| `postgres-provenance-six-method-v1.json` | 648 列、EXPLAIN 4,236 | `47bc10ab19f20882e1afbb128290329d75bbb1565ec7ebef6dff03689be7395c` |
| `postgres-provenance-heldout-v1.json` | 324 列、evaluation、EXPLAIN 4,236 | `6d7efe24a0e30ba2bdafa710e2ee5c8728aaa795d61e033c5166e46589597bbb` |
| `postgres-provenance-real-rsna-v1.json` | 18 列、diff summaries、verify_index | `78487fc51db2847dd4afe8d79f4cd61ff7ab99d5de8b3f5b2971238f02830560` |
| `postgres-provenance-exploratory-v1.{json,-explain.json,md}` | 探路矩陣（非正式，1 次重複） | `98247c28…` / `451550ee…` |
| `postgres-provenance-v1.md` | evidence record：方法、逐格表、acceptance 表 | — |
| `postgres-provenance-report-v1.md` | 十項證據總結（短版） | — |
| `../superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md` | 設計 spec（§1–§20） | — |
| `../superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md` | 裁決 §1–§7 與開放待辦 | — |
| `../guides/POSTGRESQL_PROVENANCE.md` | 操作指南與安全邊界 | — |

量測 commit：integration `d8cc334`、calibration `b1512ae`、six-method `6ab2b7a`、held-out `2644e83`、real RSNA `32f0f65`。
PR：#4/#5（0.7.0/0.8.0）、#7 串流 replay、#8 探路、#9 1M 裁決、#11 calibration、#12 six-method、#13 held-out、#14 real RSNA + 報告。

---

## 11. 機器可讀摘要

```json
{
  "postgresql": {"server_version": "17.11", "server_version_num": 170011, "deployment": "native_portable", "environment_fingerprint": "f29fc1bbf397de87030c90ead9614d5093572bbf8ff5208bf1ffb25b19f7217f", "schema_tables": 12, "schema_indexes": 13},
  "host": {"cpu": "AMD Ryzen 7 9700X", "cores": 8, "threads": 16, "ram_gib": 31.2, "os": "Windows 11 10.0.26200", "python": "3.12.14", "sqlite": "3.53.1", "psycopg": "3.3.5"},
  "integration_v3": {"tests": 51, "passed": 51, "failed": 0, "rollback": 28, "concurrency": 1, "mvcc": 1, "parity": 3, "commit": "d8cc334"},
  "matrix": {"scales": [1000, 10000, 100000], "one_million": "excluded_by_ruling_2026-09-15", "ratios": [0, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 0.9, 1.0], "topologies": ["chain", "branched"], "repetitions": {"1000": 7, "10000": 7, "100000": 3}, "scenarios_per_split": 108, "no_op_scenarios": 16},
  "calibration_v2": {"seeds": [20260913, 20260914], "rows": 216, "samples": 1224, "fit_observations": 92, "policy_id": "postgres-adaptive-v1-9f4e58346529", "policy_sha256": "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78", "incremental_model": {"changed_samples": 1.162, "total_edges": 0.118}, "full_model": {"total_edges": 0.704, "historical_changes": 0.097}, "incremental_rmse_ms": 2286, "full_rmse_ms": 5057},
  "six_method_v1": {"rows": 648, "samples": 3672, "parity": "648/648", "crossover_slices_not_observed": "12/12", "adaptive_decisions": {"FULL_1K": 28, "INCREMENTAL_10K_100K": 64, "NO_OP": 16}, "pg_incr_over_pg_full_p50": {"1K": "27-56%", "10K": "16-50%", "100K": "13-52%"}, "pg_full_100K_seconds": "75-140", "sha256": "47bc10ab19f20882e1afbb128290329d75bbb1565ec7ebef6dff03689be7395c", "commit": "6ab2b7a"},
  "heldout_v1": {"seeds": [20261001, 20261002], "rows": 324, "samples": 1836, "parity": "324/324", "overlap_count": 0, "aggregate_ms": {"postgres_full": {"p50": 6029, "p95": 106290}, "postgres_incremental": {"p50": 998, "p95": 34070}, "postgres_adaptive": {"p50": 1017, "p95": 34664}}, "aggregate_p50_ratio": 1.018, "aggregate_p95_ratio": 1.017, "gate": "PASS", "every_scenario_pass": false, "every_scenario_over_1_05": "36/92", "sha256": "6d7efe24a0e30ba2bdafa710e2ee5c8728aaa795d61e033c5166e46589597bbb", "commit": "2644e83"},
  "real_rsna_v1": {"transitions": 3, "rows": 18, "parity": "18/18", "graph": {"entities": 13196, "edges": 17569, "changes": 8752}, "change_ratios": [1.0, 0.9868, 0.0], "adaptive_decisions": ["FULL", "FULL", "NO_OP"], "adaptive_wrong_transition": {"name": "r4->r5", "adaptive_ms": 10502, "incremental_ms": 5865, "ratio": 1.79}, "sha256": "78487fc51db2847dd4afe8d79f4cd61ff7ab99d5de8b3f5b2971238f02830560", "commit": "32f0f65"},
  "explain_plans": {"six_method": 4236, "heldout": 4236, "real_rsna": 115},
  "open_items": ["policy_v2_relative_confidence_band", "vacuum_after_full_rebuild", "explain_for_query_plans", "linux_host_live_record", "one_million_scale"]
}
```
