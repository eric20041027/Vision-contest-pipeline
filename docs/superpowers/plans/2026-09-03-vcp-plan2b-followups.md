# Plan 2b 執行後記：裁決紀錄與 Plan 3 待辦

日期：2026-09-04。分支 `feat/plan2b-dicom-materialize`（base `main` 821d8d8）。
計畫：`docs/superpowers/plans/2026-09-03-vcp-plan2b-dicom-materialize.md`；spec：`docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md` §15。

## 1. 結果摘要

- 8 個任務全部完成。T1、T2、T3、T8 一次通過；T4、T5、T6、T7 各經一輪修正（T4 兩個 Important、T5 一個 Important 加三個 Minor 併修、T6 三個 Important、T7 一個測試覆蓋缺口）。
- 最終全分支審查（opus）判定 **With fixes**：4 個 Important、1 個 spec 層級 Important（I5）、11 個 Minor。一次修正波（3 commits）→ 一次範圍限定再審 → 殘餘 1 個測試品質項目（park，見 §4）。
- 交付：card 欄位 `exif_policy` / `raw_manifest_mode` 與 `--raw-manifest sizes`；影像匯入器記錄 EXIF 方向並依政策決定尺寸、`exif_rotated` WARN、匯出 manifest 記錄政策、`ExportOutput` 契約；coords 稽核改報可疑框 / 無尺寸 view 越界 / 匯入被擋列；`data/dicomio.py`；解碼器登記表（image、dicom）與 window / resize；`dicom` 匯入器；`vcp data materialize` 與可搬移 manifest；hygiene（YOLO manifest categories、`images=` 欄位、`_place_image` 收斂、`old_card=unreadable`）；RSNA 真資料整合測試（資料缺席即 skip）、dicom 端到端 CLI 測試、README / CLAUDE.md。
- 測試：259 passed、5 skipped（realdata），覆蓋率 95.59%；ruff check / format 乾淨。

## 2. 預檢與任務期間的裁決

| 時點 | 發現 | 裁決 |
|---|---|---|
| 預檢 | T5 測試的 `rows_read == 7` 與夾具不符（9 片 + 1 個垃圾檔） | 改 10，計畫檔修正 commit |
| 預檢 | 模型選擇 | implementer 一律 sonnet（每任務都跨 ≥3 檔），任務審查 sonnet，最終審查 opus |
| T2 → T3 | coords 對無尺寸 view 用 `image_size`，忽略 card 的 EXIF 政策 | T3 改用 `image_header` 並在 `oriented` 下對調 5–8 方向的寬高，與 `make_view` 一致 |
| T2 → T7 | `export_subset` 的 `**output.manifest` 展開在基底鍵之後，可覆寫 | T7 改為先展開附加鍵、基底鍵最後（合成匯出器測試把關） |
| T4 | `_floats` 對純量 pydicom 值 `len()` 拋 TypeError；`RescaleSlope=0` 被 `or 1.0` 當缺省 | 兩者皆為計畫程式碼缺陷，依 spec §15.3-11 / §15.4-18 修正並加回歸測試 |
| T5 | labels/seq CSV 驗證在 82 萬檔掃描之後；`workers=abc` ABORT；seq_csv 重複 id 靜默覆寫；`meta_cols=series` 覆寫摘要 | 四項併入同一修正回合（其中兩項違反「使用者資料問題 → ValidationFailed」的全域約束） |
| T6 | `Path("1.2.1.1").suffix == ".1"` 讓 UID 目錄被判成 image 解碼器 | 接受 implementer 的 `_decoder_name()`：目錄 view 一律 dicom |
| T6 | skip 規則不比解碼器名；逐 view 列可充當堆疊工作；失敗後 manifest 留下指向不存在檔案的列 | 堆疊工作只認自己的列（不一致序列每次重試並 WARN）；manifest = 本輪規劃 ∧（跳過 ∨ 產出）− 失敗；png 體積判定改用 `info["slices"]` |
| T7 | `old_card=unreadable` 只在 `plans_invalidated` 區塊內輸出 | 接受：舊 card 不可讀且零個 plan 檔時本來無事可報；spec 措辭留待 v5 補 |
| T7 | manifest 鍵優先序的斷言沒有真的碰撞 | 修正回合加合成匯出器測試 |

## 3. 最終全分支審查裁決

| # | 發現 | 裁決 | 處置 |
|---|---|---|---|
| I1 | `dedup.applies` 無條件為真，`vcp data audit` 對 dicom 資料集 ABORT / FAIL | 修 | `365d166`：只在所有 view 副檔名屬 `IMAGE_EXTS` 時套用 |
| I2 | `exif_policy` 改變不使 materialize 快取失效 | 修 | `e0d703d`：skip 規則比對列的 `exif_policy` |
| I3 | 乾淨重匯不清 `import_skipped.jsonl`，coords 永遠 FAIL | 修 | `365d166`：無跳過列即刪檔 |
| I4 | series 目錄含非 DICOM 檔即整條失敗 | 修 | `e0d703d`：依解碼器家族過濾檔案 |
| I5 | COCO + `oriented` + JSON 尺寸：宣告尺寸與解碼陣列不一致 | spec 裁決 | COCO 的 width/height 來自儲存像素；`oriented` 下遇到會對調的方向即 `ValidationFailed`，提示改用 `exif=stored` 或拿掉 JSON 尺寸（`365d166`）。需寫進 spec v5 |
| M1 / T8#89 | README「png 是唯一烙進方向的步驟」不成立、dicom 不讀 EXIF | 修 | `58e92ca` |
| M2 | CLI `fields.update(extra_fields / fields)` 可覆寫基底 VERDICT 欄位 | 修 | `e0d703d`：`setdefault` |
| M3 | `--decoder` 打錯字 ABORT 而非 FAIL | 修 | `e0d703d`：`_validate` 先查登記表 |
| M8 | CLAUDE.md 快取路徑漏 `[-r<長邊>]` | 修 | `58e92ca` |
| T5#62 | 無 labels_csv 時 `--opt task=` 仍在掃描後才驗 | 修 | `365d166` |
| M4–M7, M9–M11 與其餘 deferred | 見 §5 | 延後 | Plan 3 |

## 4. 範圍限定再審的殘餘

- **M3 的回歸測試為套套邏輯**（`tests/unit/data/materialize/test_run.py` 的 `--decoder` 測試沒先建立資料集，`match="decoder"` 命中的是 tmp_path 裡的測試函式名）。生產修正正確；測試在 Plan 3 hygiene 補強：先建資料集，`match="must be one of"`。裁決：park，不再開第二波修正。→ Plan 2c Task 1 完成（`45bce05`）。
- **dedup 的副檔名白名單是語法代理**：路徑無常見副檔名的 jsonl / image_csv 資料集會讓 dedup 靜默不跑（`run_audit` 只是略過，無 WARN）。與 `iter_images` 的慣例一致，接受；Plan 3 可考慮在 summary 記 `skipped_checks`。→ Plan 2c Task 3 完成（`c186052`：`summary.json["skipped"]` 與 audit VERDICT 的 `skipped=`）。

## 5. Plan 3 待辦（spec v5 補充 + hygiene）

1. **spec v5 收錄的計畫層決定**：`--resize` 只對 png；png 遇體積記 failed；`--workers` 預設 1；dicom 無 labels_csv 時 task 預設 `multilabel`；series 層級 view 的 `seq_index` 為 None；`ManifestRow.bytes`；I5 的 COCO 規則；`old_card=unreadable` 的措辭。→ Plan 2c Task 4 完成：spec v5 §16。
2. **materialize**：堆疊列只記 `srcs[0]`（M4，manifest 應能還原完整來源）；`decode_series` 缺 `exif_policy`（M5）；`--stack-seq` 來回切換留下孤兒檔（M6）；npy 模式靜默忽略 `--window`（M7，與 `--resize` 不對稱）；`--window` 變動使快取失效沒測、`resize` 比對是死碼（M9）；`to_uint8` 對 uint8 輸入不做 MONOCHROME1 反相（M10）；RSNA 全量時 `is_dir()` + `stat()` 兩百萬次（M11）。→ M4、M5、M6、M7、M9、M10 由 Plan 2c Task 2 完成（`92570e4`）；M11 留待（效能，量測層之後）。
3. **稽核**：boxes / polygons 各自的尺寸快取（T3#43）；duplicate 短路早於越界檢查（T3#44）；dedup 不適用時在 summary 記錄。→ Plan 2c Task 3 完成（`c186052`）。
4. **測試**：M3 測試補強；`rescale` signed→int16 與 `sort_slices` 預設法線的分支（T4#51）；`test_rsna_knee` 假設每個 study 的 Report 非空，資料到位後驗證。→ M3 與 T4#51 由 Plan 2c Task 1 完成（`45bce05`）；`test_rsna_knee` 已於 2026-09-04 以真資料驗證通過。
5. **依賴**：dicom 四個套件同時列在 extra 與 dev 群組（T1#30），改 dev 依賴 `vcp[dicom]`。→ Plan 2c Task 1 以守門測試取代（`tests/unit/test_package.py`），不改依賴結構。
6. **README 命令表**省略次要旗標（T8#90）為設計取捨，維持。
7. **下一步**：Plan 2b 合併後先寫三個專案 skill（`vcp-data-pipeline`、`vcp-extend-registry`、`vcp-contest-onboarding`），再進子專案 2（量測）的 spec。→ skill 已完成（`48184ef`），量測層 spec 已寫（`docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md`）。
