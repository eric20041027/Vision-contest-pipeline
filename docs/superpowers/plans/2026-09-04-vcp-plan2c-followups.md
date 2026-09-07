# Plan 2c 後記：裁決、審查發現與待辦

- 日期：2026-09-04
- 計畫：`docs/superpowers/plans/2026-09-04-vcp-plan2c-hygiene.md`
- 分支：`feat/plan2c-hygiene`（自 main 的 `8a3f0a8` 分出）
- 執行方式：Subagent-Driven Development，四個任務各自審查；最終全分支審查改用 Workflow 並行六個面向 + 對抗式驗證

## 1. 結果摘要

| 項目 | 值 |
|---|---|
| commit | `45bce05`、`92570e4`、`b83d02f`、`c186052`、`9ae5879`、`b12c9ef`、`afd4cb5` |
| 測試 | 280 passed、3 skipped（海廢資料集未匯入） |
| 覆蓋率 | 96.03% |
| ruff | `check` 與 `format --check` 皆乾淨 |
| 真資料 | `VCP_REALDATA_ROOT=C:/vcp-data uv run pytest tests/integration -m realdata` → 2 passed（RSNA）、3 skipped |

交付：`--decoder` 測試去套套邏輯、`rescale` dtype 階梯與 `sort_slices` 預設法線的分支測試、pyproject 依賴漂移守門；`ManifestRow.srcs`、`decode_series(..., exif_policy=)`、孤兒輸出清理與 `orphans_removed`、堆疊列取代逐 view 列、`--window` 只對 png、uint8 MONOCHROME1 反相；coords 越界優先於重複與共用尺寸快取、`summary.json["skipped"]` 與 audit VERDICT 的 `skipped=`；spec v5 §16。

## 2. 計畫期間的裁決

1. **依賴結構不動**（後記 2b §5-5 原建議 dev 依賴 `vcp[dicom]`）：PEP 735 群組不能引用專案自己的 extra，改以 `tests/unit/test_package.py` 的守門測試確保每個 extra pin 都同時列在 dev 群組。
2. **解碼器版本不跳號**：uint8 MONOCHROME1 反相與 series 目錄的 `exif_policy` 是 `92570e4` 才修的。跳號會讓所有 npy 快取失效，代價遠大於受影響的兩種窄情況；改為在 spec §16-1 註明那之前的快取需要跑一次 `--force`。
3. **`skipped` 在 `run_audit` 與 CLI 各自推導**：保持 `run_audit` 回傳型別穩定，兩處推導可證等價（`results` 的鍵恆為 `applicable` 的子集）。
4. **CLI 測試必須有承載力**：Task 2 的 `--window` 斷言原本放在刪檔之後，任何 npy 執行都會 FAIL，斷言等於沒斷。移到刪檔之前並檢查 VERDICT 的 `reason=` 文字。

## 3. 最終全分支審查

六個面向（materialize 核心、解碼器與 8-bit 映射、稽核層、CLI 契約、測試品質、跨檔案文件與鐵則）加一個完整性批判，32 個原始發現，每個由三個對抗式代理嘗試反駁，8 個存活。

| # | 等級 | 位置 | 內容 | 處置 |
|---|---|---|---|---|
| 1 | critical | `run.py` 單張序列 | `--stack-seq` 下只有一張切片的序列以 `None.<ext>` 命名，同一 study 的多個單張序列互相覆蓋，manifest 兩列指向同一檔 | 修（`b12c9ef`）：`_job_stem` 一律由工作身分命名；`_is_current` 加記錄輸出路徑比對，讓舊命名的快取自己重做 |
| 2 | important | `_is_current` | 不比對來源，重新匯入換了來源檔仍沿用舊解碼結果 | 修（`b12c9ef`）：比對 `srcs`，`None` 者維持舊 manifest 相容 |
| 3 | important | 堆疊取代逐 view | 只作用於本輪 todo，修不了已經混有兩種列的舊 manifest | 修（`b12c9ef`）：改由最終列集合對所有工作推導 |
| 4 | minor | `coords._view_size` | 標註帶超範圍 view 索引時拋裸 `IndexError`（ABORT）而非有定位的 `ValidationFailed`（FAIL） | park，見 §5 |
| 5 | minor | audit 測試 | 沒有任何測試釘住「全部適用時不輸出 `skipped=`」（突變測試證實） | 修（`b12c9ef`） |
| 6 | minor | audit `--json` | payload 的 `skipped` 鍵無人斷言，改名不會被發現 | 修（`b12c9ef`） |
| 7 | minor | skill reference | `--window` 少了 png-only 的限制，而它現在是硬 FAIL | 修（`b12c9ef`） |
| 8 | minor | `orphans_removed` | VERDICT 欄位全套測試都沒執行到 | 修（`b12c9ef`） |

## 4. 範圍限定再審與殘餘修正

三個面向（逐發現裁決、破壞獵捕、經驗回歸探測）確認七項皆已處理，但抓到修正輪自身引入的問題：

1. **重做的工作沿用舊列**（important，兩個面向各自端到端重現）：過期的堆疊列活過一次退回逐 view 的執行，接著取代該輪剛寫出的逐 view 列，孤兒清理再刪掉檔案，且每輪重複。修（`afd4cb5`）：carry-over 排除本輪 todo 的鍵。
2. **單張序列寫出 2-D**（important）：與 spec 和 skill 允諾的 S×H×W 不符。修（`afd4cb5`）：堆疊工作一律走堆疊分支，S=1。附帶結果是 png + `--stack-seq` 的單張序列改記入 `failed.jsonl`，與 §16-1 對體積的規定一致。
3. **檔名空間撞名**（minor，靜默毀損）：堆疊輸出以 `seq_id` 命名、逐 view 以索引命名，兩者共用一個 sample 的檔名空間。修（`afd4cb5`）：`plan_jobs` 撞名即 `ValidationFailed`，與既有的樣本目錄撞名一致。

三項修正各自通過突變測試（撤掉任一項，恰好有一個新測試失敗）。最終驗證代理另跑九組對抗情境（含 `--workers 2` 的重複退回、剝掉 `srcs` 的舊 manifest、真 UID 的多序列 study、`view_level=series`）皆無新發現。

**裁決**：加上 carry-over 的排除之後，「取代動作在剔除失敗列之後」已不可觀測（把兩段對調重跑，34 個 materialize 測試全過，結果逐位元相同）。保留該順序為防禦性寫法，註解陳述不變量而不宣稱有測試釘住。

## 5. 待辦

1. **coords 的 view 索引未做範圍檢查**（審查發現 4）：既有問題，只有 jsonl 匯入器這條逃生門到得了；真正的修正在 `tasks.py` 的驗證器補範圍檢查，讓兩種標註欄位都被檢查而不只是任務的 `label_field`。已記在 Plan 2b 後記 §5-8。
2. **M11 效能**：`_decoder_name` 每個 view 一次 `is_dir()`、`_is_current` 每列一次 `stat()`，加上本次新增的孤兒掃描兩次 `rglob`。RSNA 全量（約兩百萬 view）時才會痛，留到量測層之後的效能回合。
3. **退回逐 view 的序列每輪重做**：形狀不一致的序列沒有自己的列，所以永遠不算 current，每輪重新解碼。既有且刻意（`_is_current` 的 docstring 已說明），確認不會造成孤兒或資料遺失，只是重算成本。
4. **`view_level=series` 的來源比對無效**：該形態的 view 是一個目錄，目錄內增減 slice 不改 `srcs`，要靠 `--force`。已寫入 spec §16-1 與 skill reference。
5. **並行執行**：同一個 `out_dir` 不要同時跑兩種規劃，先完成的一輪會刪掉另一輪的輸出。已寫入 spec §16-1。

## 6. 方法上的紀錄

- 六面向並行審查加三重對抗式反駁，把 32 個原始發現收斂到 8 個，其中一個是端到端重現的資料毀損；單一審查者的前四個任務審查都沒看到它。並行加驗證的組合值得在後續計畫沿用。
- 修正輪自己引入了一個比原問題更糟的回歸（刪掉剛寫出的檔案），由兩個獨立面向各自重現才抓到。**修正之後的範圍限定再審不是形式**。
- 突變測試（撤掉修正、確認恰好一個測試失敗）是判斷測試有沒有承載力最省事的方法，本輪用了六次。

## 7. Hygiene A：§5 待辦的處置（2026-09-07）

1. 做了：`tasks.py` 的驗證器對 boxes 與 masks 都檢查 view 索引，越界是帶 `location` 的 `ValidationFailed`（jsonl 匯入的逃生門現在 FAIL 不 ABORT）。詳見 Plan 3 後記 §7。
2. 不做（效能回合）。
3–5. 已是文件化的既定行為，無動作。

## 8. 接續待辦核對（2026-09-07）

§5-2 依本次範圍延後效能回合。§5-3 的異形序列重算保證失敗可重試，不另設可能掩蓋修復的快取；§5-4 series 目錄來源用 --force 保持既定契約；§5-5 同 out_dir 的多程序鎖牽涉整轮清理交易，延後設計回合。比賽流程採逐 slice PNG，單一程序規劃快取。
