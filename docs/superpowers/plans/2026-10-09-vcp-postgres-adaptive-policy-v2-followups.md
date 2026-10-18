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
      - runner 的 `_cpu_details` 每寫一筆紀錄都用 PowerShell 查一次 `Win32_Processor`；逾時就不出聲地改用 `platform.processor()`，並把實體核心數當成邏輯核心數。這和契約不符，`_validate_record_environment` 就拒寫（`checkpoint record environment mismatch`）。
      - 拒寫是 fail closed，沒有錯的列進入 checkpoint。
      - 同一段時間 `pg_isready` 有幾次 3 秒沒有回應，但 PostgreSQL 的排程工作一直在執行，判斷是整台主機卡頓，不是伺服器當掉。
      - 原因是這樣確認的：在 scratch work dir 用同一份契約單獨重跑 idx 103 的 child，印出被 runner 隱藏的 traceback。這次重跑不寫正式 checkpoint。
    - 處置一（依時段重量）：量測時間和維護時段重疊、在 07:15 UTC 之後才完成的 idx 99–102，從 work dir 移出（封存在量測機的 `heldout-v2-discarded-ruling10/`），由 runner 以同一份契約重量。
      - 挑選只看時段，不看數值。
      - 裁決 6-2 的門檻（six-method v1 觀察到的最大比值）在 held-out v2 分不出汙染：103 個已完成場景有 30 個超過，分散在整段量測，所以不拿它來決定。
    - 處置二（靜默時段）：`supervise.py` 加 `--quiet HH:MM-HH:MM`（UTC），量測鏈用 `06:55-08:45`。這段時間視同機器忙：停下、丟棄進行中的場景，時段過後再續跑。這樣避開維護對 WMI 和計時的干擾，代價是 six-method v2 每天少量約 2 小時。
    - runner 不改：`src/vcp`、runner 原始檔與契約都不變，held-out v2 繼續用 v0.15.0。

## 2. 已知限制

- calibration v2 量於 commit `b1512ae`，成本模型可能因之後的程式改動略有偏差；six-method v2 與 held-out v2 量的是 0.15.0 的程式碼（spec §12）。
- B 的外推（spec §13）：超過約 200K edges 的圖用 decade 5 的帶，偏窄、可能偏向 INCREMENTAL；小於約 1.3K edges 的圖用 decade 3 的帶，偏寬約 10 倍、可能偏向 FULL（v1 的老問題）。只影響速度。
- decade 5 的帶（3,695.5 + 7,336.4 = 11.0 s）比 v1 的 7.3 s 寬：100K 級的圖上 v2 比 v1 保守；calibration 裡 decade 5 最小的預估差距是 84.5 s，所以樣本內的選擇不變。
- held-out v2 與 real v2 的 workload 同時安裝 v1 與 v2 兩個 policy artifact，圖比 six-method v2 與所有 v1 證據多一個 entity，workload hash 也不同；同一次執行內的比較公平，但 real v2 的 `postgres_adaptive_v1` 列不會逐位元重現 real v1 已發布的預估值。

## 3. 開放待辦

1. 依裁決 9 的順序跑 held-out v2、real RSNA v2、six-method v2（held-out 與 six-method 用 `v0.15.0` 的 detached worktree，real 用裁決 9 的修正 commit；都由 `supervise.py` 看管，裁決 10 的靜默時段 06:55–08:45 UTC 不量）。
2. runner 的 `_cpu_details`（裁決 10）：每寫一筆紀錄都查一次 WMI，逾時就不出聲地改值。改成每個程序只查一次並快取，查不到就明確失敗。量測全部結束後再改，否則續跑會被原始檔雜湊擋下。
3. runner 的錯誤訊息（裁決 10）：`evaluate_adaptive` 與 child 只印「details redacted」，連例外類別都看不到。在不洩漏路徑與連線資訊的前提下，至少印出例外類別與 `reason=` 字彙。
