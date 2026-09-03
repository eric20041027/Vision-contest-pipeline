# 整合測試（真實資料）

這裡的測試以 `realdata` 標記，資料不在就 skip。它們讀 `VCP_REALDATA_ROOT`（預設
Windows `C:/vcp-data`、Linux `~/vcp-data`）與 `VCP_REALDATA_CONFIGS`（預設 repo `configs/`）。

## 準備海廢資料（一次）

1. 從 Google Drive vault 下載官方 train 影像與標註 CSV 到 `<root>/raw/marine-debris/`，
   `val_gt_282.json` 與對應的 val 影像到 `<root>/raw/marine-val282/`。
2. 匯入（依實際檔名調整 `--opt csv=`、`--opt images=`）：

```bash
uv run vcp data import --importer csv_boxes --src C:/vcp-data/raw/marine-debris --name marine-debris \
  --license "AIdea competition terms" --url "https://aidea-web.tw" --downloaded-at 2026-08-23 \
  --opt csv=train_label.csv --opt images=train --opt on_bad_row=skip
uv run vcp data import --importer coco --src C:/vcp-data/raw/marine-val282 --name marine-val282 \
  --license "AIdea competition terms" --url "https://aidea-web.tw" --downloaded-at 2026-08-23 \
  --opt json=val_gt_282.json --opt images=images
```

3. 跑：`uv run pytest tests/integration -m realdata`。

期望值來自賽後報告 §1.3 / §5.1：train 15,163 張、34 類；val282 282 張、1,093 框、33 類出現。
`on_bad_row=skip` 會把方向錯誤的標註列寫進 `cache/import_skipped.jsonl`，稽核 `coords` 再看一次。

## 準備 RSNA Knee 子集（一次）

1. `uvx --from kaggle python projects/rsna-knee/list_files.py`（列出 82 萬筆檔案清單，可中斷續跑）。
2. `uvx --from kaggle --with requests python projects/rsna-knee/download_subset_zip.py --workers 4`（58 個 gold study + 142 個隨機 study，透過整包 zip 的 HTTP Range 讀取抓約 11 GiB 壓縮資料、解壓成約 24 GiB，保留 `train_series/<study>/<series>/*.dcm` 佈局，可中斷續跑；逐檔的 `download_subset.py` 會撞 Kaggle 配額，只留作備援）。
3. 以 README 的 `vcp data import --importer dicom ...` 匯入為 `rsna-knee`。
4. `uv run pytest tests/integration -m realdata`。

期望值由 CSV 推算（子集內的 study 數、十二欄皆有值的 gold 數、每 study 的 series 數），不寫死。
