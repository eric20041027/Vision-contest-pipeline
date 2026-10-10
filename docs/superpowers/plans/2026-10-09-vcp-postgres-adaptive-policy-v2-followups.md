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

## 2. 已知限制

- calibration v2 量於 commit `b1512ae`，成本模型可能因之後的程式改動略有偏差；six-method v2 與 held-out v2 量的是 0.15.0 的程式碼（spec §12）。
- B 的外推（spec §13）：超過約 200K edges 的圖用 decade 5 的帶，偏窄、可能偏向 INCREMENTAL；小於約 1.3K edges 的圖用 decade 3 的帶，偏寬約 10 倍、可能偏向 FULL（v1 的老問題）。只影響速度。
- decade 5 的帶（3,695.5 + 7,336.4 = 11.0 s）比 v1 的 7.3 s 寬：100K 級的圖上 v2 比 v1 保守；calibration 裡 decade 5 最小的預估差距是 84.5 s，所以樣本內的選擇不變。
- held-out v2 與 real v2 的 workload 同時安裝 v1 與 v2 兩個 policy artifact，圖比 six-method v2 與所有 v1 證據多一個 entity，workload hash 也不同；同一次執行內的比較公平，但 real v2 的 `postgres_adaptive_v1` 列不會逐位元重現 real v1 已發布的預估值。

## 3. 開放待辦

1. 發 0.15.0 之後依計畫附錄 A 跑 six-method v2、held-out v2、real RSNA v2。
