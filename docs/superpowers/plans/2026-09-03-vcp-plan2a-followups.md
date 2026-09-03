# Plan 2a 執行後記：裁決紀錄與 Plan 2b 待辦

日期：2026-09-03。分支 `feat/plan2a-importers-exporters-audit`（base `ca51d24`，即 Plan 2a 計畫檔 commit）。
計畫：`docs/superpowers/plans/2026-09-03-vcp-plan2a-importers-exporters-audit.md`；spec：`docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`（§6.1、§6.2、§8、§11、§14）。

## 1. 結果摘要

- 11 個任務全部完成，每個任務一位全新 implementer + 一次任務審查；六個任務各經一輪修正（T2、T3、T5、T6、T7、T8），其餘一次通過。
- 最終全分支審查（opus）判定 **With fixes**：1 Critical、2 Important（程式）、2 Important（spec 層級）、8 Minor。
- 修正波（3 commits）→ 一次範圍限定再審 → 殘餘 3 個 Minor 以一個 commit 補修（`67d65e2`，見 §4）。
- 交付：5 個新匯入器（csv_boxes、coco、yolo、imagefolder、image_csv）、2 個匯出器（coco、yolo）與 `vcp data export`、稽核登記表（coords、dedup、provenance）與 `vcp data audit`、`--group-from-audit`、整合測試骨架（`VCP_REALDATA_ROOT`）、端到端流程測試。
- 測試：200 passed、3 skipped（realdata 缺席即 skip），覆蓋率 96.13%；ruff check / format 乾淨。

## 2. 任務審查期間的主要裁決

| 任務 | 發現 | 裁決 |
|---|---|---|
| T2 jsonl | `src` 為相對路徑時 `image_root` 重複拼接 | 修：以 `store_path` 正規化一次 |
| T3 common | 測試檔被貼進字面 U+FEFF（非 `\ufeff` 逸出） | 修：byte-level 置換；同樣問題曾出現在計畫檔 |
| T5 coco | 重複 image id 靜默覆蓋；數值錯誤無位置 | 修：重複即 `ValidationFailed`，錯誤附 annotation/image 位置 |
| T6 yolo | `load_names` 對非 mapping YAML 呼叫 `.get` | 修：`ValidationFailed` |
| T7 imagefolder / image_csv | `_cls_categories` 對 "01"/"1" 排序不確定；imagefolder 空資料集 OK | 修：固定排序；空資料集即失敗 |
| T8 exporters | `select_view` 未指定多視角回 FAIL（spec 要 ABORT）；COCO mask `area` 恆 0；YOLO 影像扁平化碰撞 | 修：依 spec ABORT；area 依 meta/shoelace；碰撞即失敗 |
| T8 測試 | 計畫測試假設 `sample_id == view.path`，但 helper 用 `s0010`/`s0010.jpg` | 測試修：改比對 `ds.by_id[i].views[0].path`（實作正確） |
| T9 audit | fixture `poly.png` 在 det 資料集缺 `boxes=[]` | 測試修 |
| T9/T11 fixtures | 漸層／純色影像會讓 dHash 假重複 | 測試修：隨機 8×8 區塊 `np.kron` 放大；`write_images` 每視角隨機像素 |

## 3. 最終全分支審查裁決

| # | 發現 | 裁決 | 處置 |
|---|---|---|---|
| Critical 1 | YOLO 匯出 `x.jpg` / `x.png` 產生同一 `labels/x.txt`，第二個靜默覆蓋 | 修 | `9d7db06`：影像名與標籤名都進碰撞檢查；標籤名以原始副檔名切 stem（不用 `with_suffix`，避免帶點目錄被截） |
| Important 2 | `Dataset.subset` 以 `Sample.group` 重驗不變量，`--group-key meta.*` 的合法 plan 匯出時 ABORT | 修 | `b55996f`：以 `plan.params["group_key"]` 解析分組函式 |
| Important 3 | yolo 匯入器 labels 目錄不存在／打錯 → 全負樣本仍 OK | 修 | `9d7db06`：目錄不存在即 `ValidationFailed`；`ImportResult.unlabeled` 與 VERDICT `unlabeled=` |
| Important 4 | EXIF 方向未處理（`common.image_size` 回儲存尺寸） | 延後 | Plan 2b spec 決定（§5-1） |
| Important 5 | 有尺寸的 det 資料集越界框在載入即被擋，coords 稽核觸及不到；海廢整合測試 `max_bad_boxes=10**9` 為空洞斷言 | 延後 | Plan 2b spec 決定（§5-2） |
| Minor | `audit --json` 掉各檢查的 VERDICT 行 | 修 | `b55996f`：JSON 模式下以 `VERDICT ` 開頭的 human 行輸出到 stderr |
| Minor | 空子集匯出回 OK | 修 | `b55996f`：warning `subset is empty` → WARN |
| Minor | 舊 `dataset.yaml` 不可讀時 `count_invalidated_plans` 讓重匯入 ABORT | 修 | `9d7db06`（半修）+ §4 補修：無法比對 hash 時保守計入全部 plans |
| Minor | `image_csv` 只有表頭 → 空資料集 OK | 修 | `9d7db06`：`ValidationFailed` |
| Minor | 五處測試 `read_text()` 未指定編碼 | 修 | `9133cd8` |
| Minor | VERDICT `images=copied` 欄位未實作（warnings 已承載） | 延後 | §5-6 |
| Minor | `dedup._views` 以 view path 為鍵（多樣本共享檔案） | 延後 | §5-3 |
| Minor | `_place_image` 以廣泛 `OSError` 退回複製 | 延後 | §5-7 |
| Minor | YOLO manifest 建議加 `categories: [{index,id,name}]` | 延後 | §5-4 |
| Minor | `rows_read` 語意各匯入器不同 | 延後 | §5-5 |

## 4. 範圍限定再審的殘餘裁決

再審（opus，`e36f099..9133cd8`）：F1–F5、F7、F8 resolved；F6 partial；新增 3 個 Minor。全部一次補修（`67d65e2`）：

- **F6 補完**：`load_yaml_model` 的 `yaml.safe_load` 在 try 之外，真正壞掉的 YAML 仍以 `yaml.parser.ParserError` 逃出 → ABORT。裁決：在 YAML 邊界統一轉 `ValidationFailed`（FAIL），而非在呼叫端擴大 except；F6 測試同時涵蓋「語法壞」與「schema 壞」兩種 card。
- **匯出器命名空間**：影像名與標籤名共用一個 dict，`x.txt` 這種（jsonl 可達的）路徑會與自己碰撞，`x.jpg`+`x.txt` 被誤報為影像碰撞。裁決：`images/` 與 `labels/` 分開兩個 dict。
- **subset 對無效 group_key**：`except ValidationFailed: group_of = None` 讓手改的 plan 靜默退回 `Sample.group` 檢查，違反 spec §14.4 「阻擋手改 plan」的意圖。裁決：讓 `ValidationFailed` 傳播（FAIL）。
- 未補的測試缺口（延後，§5-8）：CLI 層沒有斷言 `unlabeled=` 欄位與空子集 `status=WARN`。

## 5. Plan 2b 待辦（spec 補充 + 計畫）

1. **EXIF 方向政策**：決定 (a) 記錄 `exif_orientation` 進 `view.meta` 並 WARN，或 (b) 直接採旋轉後尺寸。影響 coords 驗證、匯出器、materialize/解碼器；解碼器登記表要與此一致。
2. **coords 稽核 vs 任務驗證的職責切分**：選項 (a) `on_bad_row=keep` + 放寬 `_check_bounds` 交由稽核；(b) 重新定義稽核範圍為「載入後仍可能出錯的形態（無尺寸資料集、多視角）」，並讓整合測試斷言 `import_skipped.jsonl` 計數而非 `max_bad_boxes=10**9`。
3. **多視角／共享檔案語意**：同一檔案被多個樣本引用時 dedup 的判定單位（view path vs sample）；RSNA 形態的多視角 fixture；`select_view` 的預設策略是否可在 `dataset.yaml` 指定。
4. **YOLO manifest** 加 `categories: [{index, id, name}]`，讓 class index → category id 可逆。
5. **`rows_read` 語意**逐匯入器記錄於 README（csv 列 / coco annotations / yolo 影像 / imagefolder 影像）。
6. **VERDICT `images=copied|symlinked`** 欄位（目前 warnings 字串承載）；需要匯出器回傳額外欄位的通道。
7. **`_place_image`** 的 `OSError` 範圍收斂（symlink 權限不足才退回複製，其他 I/O 錯誤應失敗）。
8. **CLI 層測試**：`unlabeled=` 欄位、空子集 `status=WARN`、`plans_invalidated` 的 WARN 行。
9. **dicom 匯入器、materialize / 解碼器登記表、cache 命名**（Plan 2b 主體，spec §14.5）。
10. **`count_invalidated_plans`** 在 card 不可讀時計入全部 plans：是否在 VERDICT 標示原因（`old_card=unreadable`）。
