# PostgreSQL adaptive policy v2：後記

spec：`docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`；計畫：`docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2.md`。

## 1. 裁決

1. **候選帶比較**（Task 5，2026-10-10）：勝者 `stratified_edges`，分數 A 1.584 / B 1.011（spec §13）。比較腳本在 commit `1823807` 預先登記，之後才執行。

## 2. 已知限制

- calibration v2 量於 commit `b1512ae`，成本模型可能因之後的程式改動略有偏差；six-method v2 與 held-out v2 量的是 0.15.0 的程式碼（spec §12）。
- B 往較大規模外推時偏窄（spec §13 的留一層外推）：超過約 200K edges 的圖，v2 會用 decade 5 的帶，可能偏向 INCREMENTAL。只影響速度。

## 3. 開放待辦

1. 發 0.15.0 之後依計畫附錄 A 跑 six-method v2、held-out v2、real RSNA v2。
