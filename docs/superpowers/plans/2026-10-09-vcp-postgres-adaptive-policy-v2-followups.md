# PostgreSQL adaptive policy v2：後記

spec：`docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`；計畫：`docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2.md`。

## 1. 裁決

1. **候選帶比較**（Task 5，2026-10-10）：勝者 `stratified_edges`，分數 A 1.584 / B 1.011（spec §13）。比較腳本在 commit `1823807` 預先登記，之後才執行。
2. **v2 policy**：`postgres-adaptive-v2-9f4e58346529`，`policy.json` SHA-256 `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000`；成本模型與 v1 相同（相對誤差 ≤ 1e-9 的檢查通過）。
3. **telemetry 不加 reason code**（spec §4.6）：v1 與 v2 以 `policy_version` 分辨，DDL 與 `POSTGRES_SCHEMA_VERSION` 不變。
4. **runner 的相容性**：比較方法 `postgres_adaptive_v1` 不進 `METHODS`；所有新參數沒給時與 0.14.0 逐位元相同，既有的 mock 簽名與 checkpoint 契約不變。
5. **未知的 policy 版本**：`load_policy_artifact` 在模型驗證之前就檢查 `policy.json` 的 `policy_version`，認不得的版本報 `incompatible_policy: provenance policy policy version`；沒有這個鍵的 payload 照 v1 的舊路徑處理（spec §4.4；Task 3 審查）。
6. **量測前的配對檢查**：held-out v2 與 real 驗證在量測之前就檢查 policy 配對（v2 為主、凍結的 v1 為比較、同一份 calibration），錯的配對不會先量好幾個小時才失敗；held-out v1 的入口只收 v1 policy（Task 6、8、9 審查）。
7. **0.14.0 讀到 v2 的 `policy.json`**：報的是 `mismatch: provenance policy payload`，不是 spec §9 原本寫的 `incompatible_policy`——0.14.0 把每個 `policy.json` 都當 v1 嚴格驗證。CHANGELOG 照實寫。
8. **量測提前開跑、順序改變**（使用者 2026-10-15 決定；偏離 spec §6.1 與計畫附錄 A）：
   - spec §6.1 原訂 10/26 之後才開始，附錄 A 的順序是 six-method → held-out → real。為了讓 held-out v2 的結果趕上 10/22 凍結、10/25 繳交的課程進度報告，改為現在開跑，順序：
     1. real RSNA v2：先跑，當整條流程的冒煙測試（v1／v2 配對檢查、`postgres_adaptive_v1`、環境指紋）；
     2. held-out v2：正式驗收，最優先，目標 10/21 前完成；
     3. six-method v2：預覽，10/21 前跑不完照跑。
   - 原訂延後是為了避開 RSNA 的訓練；使用者確認從開跑到量測結束，RSNA 不在這台機器上訓練。
   - held-out v2 與 six-method v2 另給 `--work-dir C:/vcp-data/bench/<名稱>-work`：checkpoint 放在 worktree 之外，supervisor 才能丟棄進行中的場景，跟 v1 的做法相同。不給時 runner 會放在 `docs/benchmarks/<輸出名>-work`，量到的數字兩者相同。
   - 只改時程、順序與 checkpoint 位置。spec §7 的驗收條件、§6.3 的 seeds、附錄 A 的其他參數、spec §6.1 的看管規則（RAM 護欄、裁決 6-3 的自己人判定、裁決 6-2 的汙染處置）都不變。
9. **real RSNA v2 改由修過的 runner 量、排在 held-out v2 之後**（2026-10-17，使用者選擇）：
   - 2026-10-17 07:45 UTC 的冒煙測試在量測前就失敗：`canonical fixture contains broken evidence`。9/20 量 real v1 時，四個 sixslot dataset 上只有 7 個 run；之後 RSNA 又加了約 90 個 run，它們列著存取收據（`access`）與 label set／證據副本（`evidence`）。v0.15.0 的 `real_validation.py` 只複製 runs、measure 與 configs，不複製這些產物，所以這些 run 在副本裡都是證據遺失（92 個 broken）。這是 runner 的複製範圍問題，與 policy v2 無關。
   - 失敗的兩次嘗試保存在量測機上，沒有產出證據檔。held-out v2 不受影響（合成 workload），07:53 UTC 先開跑；新順序是 held-out v2 → real RSNA v2 → six-method v2。
   - 修正：`_copy_snapshot` 另外複製被選中的 run 所引用的收據與 evidence 產物；來源就缺的照樣缺，fixture 照樣被拒。只改 `tests/performance/provenance/`，`src/vcp` 與 v0.15.0 逐位元相同。
   - real RSNA v2 從這個修正合併後的 commit 的 detached worktree 量，證據記下該 commit；量的是當天的 RSNA root（run 比 v1 多），v1 與 v2 在同一次執行內比較。與已發布的 real v1 不是同一張圖，報告照實寫；r4→r5 仍只是 sanity check（spec §6.4）。
10. **held-out v2 在 103/108 停下；idx 99–102 依時段重量；supervisor 加靜默時段**（2026-10-18，使用者選擇）：
    - 經過：07:25 UTC 完成第 103 個場景（idx 102）之後，idx 103 連續三次在寫出第一筆紀錄前失敗，量測鏈 08:12 照設計停下。
    - 原因：當地 03:15（07:15 UTC）Windows 自動維護開始，07:41 Defender 重啟並更新，07:51 Windows Update 下載更新。這段時間 WMI 查詢常超過 10 秒：WMI-Activity 在 07:42–08:18 記了 61 筆 `0x80041032`（呼叫被取消）。
      - runner 的 `_cpu_details` 在每個程序裡用 PowerShell 查一次 `Win32_Processor`（每個場景都是新的子程序；結果以 `lru_cache` 快取）；逾時就不出聲地改用 `platform.processor()`，並把實體核心數當成邏輯核心數。這和契約不符，`_validate_record_environment` 就拒寫（`checkpoint record environment mismatch`）。
      - 拒寫是 fail closed，沒有錯的列進入 checkpoint。
      - 同一段時間 `pg_isready` 有幾次 3 秒沒有回應，但 PostgreSQL 的排程工作一直在執行，判斷是整台主機卡頓，不是伺服器當掉。
      - 原因是這樣確認的：在 scratch work dir 用同一份契約單獨重跑 idx 103 的 child，印出被 runner 隱藏的 traceback。這次重跑不寫正式 checkpoint。
    - 處置一（依時段重量）：量測時間和維護時段重疊、在 07:15 UTC 之後才完成的 idx 99–102，從 work dir 移出（封存在量測機的 `heldout-v2-discarded-ruling10/`），由 runner 以同一份契約重量。
      - 挑選只看時段，不看數值。
      - 裁決 6-2 的門檻（six-method v1 觀察到的最大比值）在 held-out v2 分不出汙染：103 個已完成場景有 30 個超過，分散在整段量測，所以不拿它來決定。
    - 處置二（靜默時段）：`supervise.py` 加 `--quiet HH:MM-HH:MM`（UTC），量測鏈用 `06:55-08:45`。這段時間視同機器忙：停下、丟棄進行中的場景，時段過後再續跑。這樣避開維護對 WMI 和計時的干擾，代價是 six-method v2 每天少量約 2 小時。
    - runner 不改：`src/vcp`、runner 原始檔與契約都不變，held-out v2 繼續用 v0.15.0。
11. **held-out v2 與 real RSNA v2 的證據進 repo；real 的版本改用相對路徑**（2026-10-18，使用者決定）：
    - held-out v2：`docs/benchmarks/postgres-provenance-heldout-v2.json`，量於 commit `660d015`（v0.15.0），2026-10-18 19:31 UTC 發布，SHA-256 `e4cca33d04ea2b981a031afbe4b6a8edf64acfbe12e837f4a621594e2cc85a48`，原樣進 repo。檔內唯一像路徑的值是每列的 `disk_path = C:\`（磁碟根目錄，屬於契約的環境欄位）。
    - real RSNA v2：`docs/benchmarks/postgres-provenance-real-rsna-v2.json`，量於 commit `cafeea2`（裁決 9），2026-10-18 20:10 UTC 發布。
      - runner 把 `source_data_root`、`source_configs_root` 記成量測機的絕對路徑，裡面有使用者名稱。進 repo 的版本只把這兩欄改成相對於 runner 工作目錄（real 的 detached worktree）的路徑：`../RSNA_Knee_Abnormality_Detection/vcp-data` 與 `../RSNA_Knee_Abnormality_Detection/configs`。
      - 改之前先用 runner 的寫法（`json.dumps(..., ensure_ascii=False, indent=2) + "\n"`）重新序列化原檔，確認位元完全相同；改完後 27,385 行裡只有這 2 行不同。
      - 原檔 SHA-256 `e9eac27f34c667f37596e99feefb8bc3ac8fc99d9f57ff79893e37508d529d4d`，保存在量測機；repo 版 SHA-256 `bb29448f60db052c1d51e180437c356b09824b7b923a3eb3ea01a97e8e1ad75f`。引用 real v2 時兩個都寫。
    - 已發布的 real v1 檔案裡仍有絕對路徑；v1 證據凍結，不改。
12. **six-method v2：兩個場景因單次重複失敗而重量；量測期間 supervisor 加 GPU 與 CPU 條件**（2026-10-20）：
    - 第一次發布（16:43 UTC）的 648 列裡有 2 列 `failed`，各只有一次重複回報 `benchmark_operation_failed`；runner 不保存細節，當下也沒有已知的干擾事件：
      - `postgres_adaptive`，1K、0.05、branched、seed 20260914，第 6 次重複，10/19 17:21 UTC；
      - `postgres_full`，10K、0.01、branched、seed 20260914，第 7 次重複，10/20 02:49 UTC。
    - 處置：依「失敗」這個狀態（不看數值）選出這兩個場景，把它們的紀錄移出 work dir，用同一份契約重量。17:17 UTC 發布，648 列全部 ok。第一次發布的檔案（SHA-256 `d5adce06877e27f2dd9336867f5cb58b02320c461927462668707a4b5b151682`）保存在量測機。
    - 證據：`docs/benchmarks/postgres-provenance-six-method-v2.json`，量於 commit `660d015`（v0.15.0），SHA-256 `5bbee9fad2d20b609ccd7e67a01951096cbedd37ef1e202f00d8d73d01ac5a63`，policy `postgres-adaptive-v2-9f4e58346529`。檔內唯一像路徑的值是 `disk_path = C:\`，原樣進 repo。
    - 量測期間的看管：`C:/vcp-data/bench/supervise.py` 不在 repo 裡，它加了兩個條件，達到就視同機器忙。
      - `--gpu 15`：外部程式使用 GPU 3D 引擎 ≥ 15%。遊戲在記憶體門檻以下就會搶資源。
      - `--cpu 1.5`：外部程式在 3 秒取樣內用 ≥ 1.5 核。
      - 10/17–10/20 的量測期間，這兩個條件、記憶體門檻與靜默時段都觸發過。每次停下都丟棄進行中的場景，停機的那段時間沒有寫入任何紀錄。
    - 10/19 20:06 起的 Defender 事件、10/20 00:03 起的外部 CPU 負載讓連線與 WMI 逾時，runner 照裁決 10 的機制拒寫（fail closed）；事件時段內沒有任何場景完成。

## 2. 已知限制

- calibration v2 量於 commit `b1512ae`，成本模型可能因之後的程式改動略有偏差；six-method v2 與 held-out v2 量的是 0.15.0 的程式碼（spec §12）。
- B 的外推（spec §13）：超過約 200K edges 的圖用 decade 5 的帶，偏窄、可能偏向 INCREMENTAL；小於約 1.3K edges 的圖用 decade 3 的帶，偏寬約 10 倍、可能偏向 FULL（v1 的老問題）。只影響速度。
- decade 5 的帶（3,695.5 + 7,336.4 = 11.0 s）比 v1 的 7.3 s 寬：100K 級的圖上 v2 比 v1 保守；calibration 裡 decade 5 最小的預估差距是 84.5 s，所以樣本內的選擇不變。
- held-out v2 與 real v2 的 workload 同時安裝 v1 與 v2 兩個 policy artifact，圖比 six-method v2 與所有 v1 證據多一個 entity，workload hash 也不同；同一次執行內的比較公平，但 real v2 的 `postgres_adaptive_v1` 列不會逐位元重現 real v1 已發布的預估值。

## 3. 開放待辦

1. ~~依裁決 9 的順序跑 held-out v2、real RSNA v2、six-method v2，並寫證據紀錄與 course brief v2~~：三段都已完成（2026-10-20，裁決 11、12）；證據紀錄 `docs/benchmarks/postgres-provenance-v2.md` 與 `docs/benchmarks/postgres-provenance-course-brief-v2.md` 已寫。
2. ~~runner 的 `_cpu_details`（裁決 10）~~：已修正（2026-10-20）。Windows 的查詢重試三次、每次 30 秒，仍失敗就丟出 `cpu identity unavailable`，不再改用 `platform.processor()` 與 physical = logical 的備用值；失敗不進快取。
3. ~~runner 的錯誤訊息（裁決 10）~~：已修正（2026-10-20）。`adaptive_benchmark`、`evaluate_adaptive`、`calibrate_adaptive` 失敗時印出例外類別，原因只在不可能帶路徑、憑證或自由文字時才印（vcp 錯誤的 reason 字彙，或純小寫片語）；失敗的場景子程序把自己的摘要經 `ScenarioChildFailed.detail` 交給父程序。
4. ~~（已完成 2026-10-20：指南與 skill 改成建議 v2，並寫明 v2 的外推限制）~~ 操作指南 `docs/guides/POSTGRESQL_PROVENANCE.md` 的 `auto` 建議依 policy 版本補上 v2（spec §8：「v2 在 held-out v2 的結果出來後補上」），skill `vcp-provenance`（與 `.agents/skills/` 的鏡射）同步：v2 的 id、與 v1 的差別、B 的外推限制；改 skill 照 writing-skills 的流程先做基準測試。
