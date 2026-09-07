# RSNA Knee 基準流程（2026-09-07）

目標是以本機 200-study 子集跑通可追溯流程。此版是影像基準模型，不宣稱具競賽級效能。

- 固定 fixed-v1 / seed 42：train 164（22 gold、142 none），valA / valB / sealed holdout 各 12 gold。group-key=auto 使用 PatientID 分組，不用無標籤樣本當負例。
- PNG 256 逐 slice 快取。現行 data spec §16-1 禁止 PNG volume，因此不傳 --stack-seq；比賽端依 seq_id / seq_index 組合三方位各三張（25%、50%、75%）成九通道。每方位先選 Fluid_Sensitive=1、再 Fat_Suppression=1，同分依 UID；缺方位補零，三方位全缺即 FAIL。
- 同一套選片、DICOM decode / window=dicom、resize 與補邊在本機及 notebook 使用。輸入只含像素與序列方位，不讀 Report、患者身分或標籤作預測特徵。
- 小型三層 CNN、BCEWithLogitsLoss、AdamW；固定 seed 42 / 43、20 epochs，不使用 eval / sealed 決定 epoch。最終 checkpoint 關檔後由 Session 登記，訓練參數及 train samples_hash 綁入 checkpoint。
- 專案 exporter 產生 vcp manifest 及 gold 訓練 ID；vcp train run 的 --export 保留原 train 子集 lineage。訓練檢查 Session 的 dataset / plan / seed / config_hash / trained_on 與 CLI 一致；評估或 sealed 子集不能傳給訓練。
- eval ingest 採內建 scores_csv；首次參考 measure 會 WARN 無 anchor，然後 anchor，再 measure。模型主張及融合準入皆在候選讀數產生前預登記，門檻維持 t_min=2、min_bases=2；FAIL 判決照實保留。
- notebook 只依 test.csv 列出的 study 推論，保持原 UID 與 12 欄順序，無網路下載；bundle 只含必要程式、權重及 vcp wheel，不含原始資料、Report 或憑證。先本機驗證 raw 與 PNG 預測相同，再提供 Kaggle 執行。
- kernel 提交與遠端備份的 notebook id / version / quota / destination 需真實設定；不產生虛構上傳或榜面分數。sealed 僅在模型選定且要 final 時進行最後評估窗口，Reader 與 measure 的每次解封各自留痕，不為測試流程預先開封。

測試：合成資料驗選片排序、空方位、標籤篩選、CSV ID/有限機率、train lineage 守門；真資料在暫存根驗 PNG 與 raw 前處理相同，缺資料 skip；實際 PyTorch 訓練及推論在獨立 venv 執行。核心 venv 不安裝 torch；所有新 Python 檔另以 ruff 明確指定檢查（repo 預設排除 projects）。

實作與現場裁決、命令證據記於 RUNBOOK.md 的執行紀錄。

## 補充決定（2026-09-07）

1. Windows 實際訓練命令使用獨立 venv 的絕對 interpreter 路徑。裸 `python` 的失敗 attempt 保留；不改訓練身分或清除歷史。核心解析行為另記 Plan 5 後記 §10。
2. checkpoint 保存 data.py / model.py 的 SHA，推論先驗版本、類別順序、程式 SHA，再以 `weights_only=True` 載入。已登記的 checkpoint 不隨格式化或新模型程式自動相容。
3. seed 43 與融合的判決皆 FAIL；不調低準入門檻，首次 notebook 使用第一個固定 seed 42 baseline 並留下 baseline 理由。
4. 本機測得 CPU / CUDA 存在小於 0.00007 的機率差，但 valA / valB 的逐標籤排序完全相同。raw / PNG 在同裝置的前處理與 CSV 一致；跨硬體不要求逐 byte 相同。
5. notebook bundle 明列必要檔案、驗 SHA、禁止網路、使用 CPU；離線依賴是 Linux Python 3.12 wheels，torch 由 Kaggle runtime 提供。實際 Kaggle 執行相容性要由成功執行證據確認。
6. 上傳到私有 Kaggle dataset 的動作遭自動核准審查要求明確目的地同意，核准前停在已驗證的 bundle。遠端備份目的地未提供，不能虛構 scored / final / verified 或 remote_forgotten。
