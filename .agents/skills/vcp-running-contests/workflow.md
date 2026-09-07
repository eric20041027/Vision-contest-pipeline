# vcp 全流程與證據地圖

## 開始前需要的資訊

| 資訊 | 用途 | 缺少時怎麼做 |
|---|---|---|
| 比賽規則、授權、來源 URL、實際下載日 | dataset provenance | 查官方來源；無法確認就不匯入，但可先盤點格式 |
| 任務、類別、官方 metric、submission schema | importer、metric、writer | 先找既有登記項；比賽轉換放 `projects/<contest>/` |
| train/test 路徑與同源單位 | audit 與 group split | 查 patient/study/video/source id，不能以影像列隨機切分代替 |
| 截止時間、時區、每日配額、final slots/board rule | submit profile | 可先完成本地流程，不能猜平台規則 |
| GPU、時間、費用與儲存預算 | 實驗範圍 | 先做最小 baseline；超限前提出具體選項 |
| 已授權外部目的地 | upload 與異機備份 | 可完成 stage/verify/manifest；停在外傳前 |

## 階段與閘門

| 階段 | 主要命令或動作 | 完成證據 / 進下一階段的條件 |
|---|---|---|
| 0. 現況與規則 | 讀文件、盤點 roots/configs/projects | 規則、metric、分組單位、預算與授權範圍寫進 RUNBOOK |
| 1. 資料 | `data import`, `validate`, `audit --against` | card、`samples.jsonl`、manifest 自洽；重複/壞列已有裁決 |
| 2. 固定切分 | `data split`, `lineage` | 不可變 plan；通常 train + 至少兩個互斥 eval + sealed，group 不跨子集 |
| 3. 訓練輸入 | `data materialize` 或 `data export` | cache manifest 或 export manifest 可由訓練程式讀取 |
| 4. baseline | `train run`, `train status`, `eval ingest`, `measure`, `anchor` | checkpoint 有 sha；valA/valB baseline 讀數與 anchors 存在 |
| 5. 候選 | `eval sigma`（tuning）、`preregister`, `measure`, `judge --strict` | 預登記早於候選讀數；`verdict=PASS` 才準入 |
| 6. 融合 | `fuse recipe`, `ablate --preregister`, `eval measure/judge`, `fuse build` | 每位成員的「有它 vs 沒它」判決都 PASS；配方與成員 sha 固定 |
| 7a. file test 推論 | 專案推論、`eval ingest` | eval/test run 的權重或融合身分匹配；test run 可重產 |
| 7b. kernel 實跑 | 上傳已授權的 dataset/notebook、等實際成功版本與輸出 | 綁定成功 notebook id/version、輸出與 `--weights RUN[:sha]`；不捏造版本或 test run |
| 8. 提交準備 | `submit init`, `stage`, `verify` | file 走 eval/test 配對；kernel 走 notebook/version/weights；四道門通過且 `stage.json` 可驗證 |
| 9. 平台 | `upload` 或 `record`, `sync` 或 `score` | 台帳有真實 uploaded/scored，Kaggle 結果由 CLI 回讀 |
| 10. 決選 | sealed `measure --unseal --reason`, `final --dry-run`, `final` | 至少一個 candidate/baseline 已真實上傳或記錄；候選固定；解封後不調參；final 記錄並封槍 |
| 11. 提交證據備份 | `backup manifest`, `push --tier 1/2/3`, `verify`, `status` | 從真實 submission 重建完整證據圖；異機目的地逐檔 sha 通過 |

小資料集若撐不起四個子集，可以使用較少子集，但要在 RUNBOOK 記錄決定、依據與失去的保證。先檢查每個 group 數及 metric 所需支持：例如每個 multilabel AUC cell 都需要正例與負例。少於兩個 eval 時，`min_bases=2` 不會自動成立；沒有 sealed 就不能宣稱完成正常 sealed final。門檻在看到分數後不得放寬，空集合不能假裝多重驗證。

## submission kind 與平台形態

- `candidate`：必須有新鮮的 PASS judgement；融合 candidate 每位成員都要 PASS。
- `baseline`：需 `--reason`，admission 記為 waived；可以有 sealed 讀數並參與 final，但不能描述成經候選準入。
- `probe`：需 `--reason`，admission 記為 waived，永不進 final。
- `file`：本地 test 預測先 ingest 成 test run，以權重/融合身分和 eval run 配對，再 stage/verify。
- `kernel`：profile 不使用本地 test run；先在既有外部授權內取得真正成功的 notebook version/output，再以 kernel/version/weights stage。上傳 notebook/dataset、執行與 competition submission 是不同平台事實。

不要自行創造 legacy/emergency kind。舊 artifact 只能按真實身分記錄；若要用正式 `baseline` waiver，先以目前 `--help` 與 gate 核對必要證據及理由。

## 六組命令的角色

- `vcp data import|validate|audit|split|lineage|export|materialize`：建立可追蹤且防洩漏的資料輸入。
- `vcp train run|upload|status`：包住任意框架，記命令、環境、checkpoint 與副本；vcp 不替你定義模型。
- `vcp eval ingest|measure|anchor|sigma|preregister|judge|status|report`：把預測正規化，保護量測條件並留下可稽核判決。
- `vcp fuse recipe|ablate|build`：固定配方並證明每位成員的增益。
- `vcp submit init|stage|verify|upload|record|score|sync|final|lock|unlock|status|report`：治理候選身分、配額、平台事實與決選。
- `vcp backup manifest|push|verify|pull|status`：從結論反向收集證據並驗證恢復能力。

選項與例子以根目錄 `README.md` 和 `--help` 為準。不要從這張地圖拼出未驗證命令。

## 主要持久產物

| 根 | 內容 | 更新規則 |
|---|---|---|
| `$VCP_DATA_ROOT/raw/<name>/` | 主辦方原始資料 | 永不修改，不進 git |
| `$VCP_DATA_ROOT/datasets/<name>/` | samples、raw manifest、cache | samples/card 一致；cache 可重建 |
| `$VCP_DATA_ROOT/runs/<run>/` | run card、預測、train/fuse 證據 | 指定 replace/resume 才換寫，必須留 history/log |
| `$VCP_DATA_ROOT/measure/<name>/` | readings、judgements、sigma、anchors | 台帳只增；anchor 換寫前先留 log |
| `$VCP_DATA_ROOT/submit/<test>/<id>/` | submission、stage card | 寫一次不改 |
| `configs/datasets/<name>/` | dataset card、splits、prereg、fuse、submit、backup | 進 git；不可變物換 id |
| `projects/<contest>/` | 模型、轉換器、metric、writer、notebook、RUNBOOK | 比賽專屬且可重跑 |

## 備份是里程碑工作

- 訓練完成：先用 `train upload` 或 `train run --upload` 驗證 checkpoint 副本；必要時以 `run:<id>` 建 manifest。
- 重要判決完成：以 `judgement:<prereg>` 建新 manifest，保存決策與重現層。
- 真實提交完成：以 `submission:<id>` 建新 manifest，依 tier 1 → 2 → 3 推送並 verify。

manifest 是當下快照，之後的台帳成長不會自動進舊清單。`raw/`、`cache/` 永不進清單；另保存原始資料重新取得方法、Git commit/source bundle、專案程式、環境鎖定檔、必要 notebook bundle。`remote_copy` 只證明那個目的地的副本，C 槽副本不是異機備份。恢復演練要包含 Git/config 取回、`backup pull/verify`，以及 `submit verify` 重產提交物。

## 重新接手時的判定法

1. 從 Git commit 與 RUNBOOK 找宣稱的最後階段。
2. 用 `eval/status/report`、`train status`、`submit status/report`、`backup status` 找實際最後證據。
3. 對照必要產物；例如有 staged 不代表 uploaded，有 RUNBOOK 命令不代表執行，有 local backup 不代表異機備份。
4. 將未完成項縮成下一個可驗證動作。若只有外部目的地或授權缺失，完成本地可審查準備後等待該項，不要重做前段。

只讀介紹或審查時不跑上述 status：直接讀現有卡與台帳，分開標示文件歷史、這次檔案核對與尚未現場驗證的 CLI/平台狀態。

## 每階段最小回報

```text
目前階段：<階段>
已驗證：<VERDICT / 領域判決 / 檔案或台帳>
此次變更：<新 id、commit、run 或無>
未完成：<真實缺口>
下一步：<一個可執行動作>
需要使用者：<只列會改變授權、資源或最終決策的事項；沒有就寫無>
```
