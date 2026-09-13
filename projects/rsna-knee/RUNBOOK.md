# RSNA Knee：本機基準到 notebook 提交

更新：2026-09-07。這是一條已實跑到離線 notebook bundle 的影像基準流程；Kaggle 上傳尚待指定目的地核准，遠端備份目的地尚未提供。未執行的步驟在 §7–9 明確標示，沒有虛構 submitted / scored / final 紀錄。設計與邊界見 [DESIGN.md](DESIGN.md)。

## 1. 本次執行結果與固定身分

| 項目 | 實際結果 |
|---|---|
| 資料 | `rsna-knee`：200 studies / 36,683 views；58 gold、142 none |
| 固定切分 | `fixed-v1`、seed 42、`group-key=auto`（PatientID）；train 164（22 gold、142 none），valA / valB / sealed holdout 各 12 gold |
| 像素 | PNG、長邊 256；train 36,683 views、test 557 views，解碼失敗 0 |
| test | `rsna-knee-test`，3 studies、12 標籤欄，全部 none；`all-v1/test` |
| 訓練 | 小型九通道 CNN，22 gold studies，固定 20 epochs；seed 42 / 43 各一個 run |
| 推論一致性 | 3 個 test study：同裝置 raw DICOM / PNG CSV SHA 相同；從 bundle 解壓後 CPU 推論成功 |
| CPU / CUDA | test 最大機率差 0.00006539；valA / valB 最大差 0.00006983 / 0.00006670，每個標籤的排序完全相同、AUC 相同 |
| 治理 | seed 43 主張與兩個融合準入皆 FAIL；首次提交只使用預先固定的 seed 42 baseline |
| 封存 | holdout 未解封；只在最終候選確定且要 final 時讀取 |
| 外部狀態 | Kaggle CLI 可讀帳號 `pongpong1027`；尚未建立本次 dataset / notebook 或提交 |
| 憑證狀態 | 官方 rclone 1.75.1 可用時，`backup status` 實測 `rclone_conf=absent` |
| 本機備份 | `knee-local-v1`：51 項證據全數驗過，`unverified=0`；兩份權重、notebook bundle、來源 Git bundle 已存 `C:/vcp-backup/rsna-knee`，仍是同機副本 |

| Run | valA macro AUC | valB macro AUC | 處置 |
|---|---:|---:|---|
| `knee-cnn-s42-v1` | 0.6432532167 | 0.4187500000 | 第一個固定基準 |
| `knee-cnn-s43-v1` | 0.6370801267 | 0.4028549383 | `knee-s43-vs-s42-v1` FAIL |
| `fuse-knee-mean-v1` | 0.6478993807 | 0.4097800926 | 兩位成員的準入都 FAIL，不提交融合 |

只有 22 個有標籤訓練 study、每個 eval 僅 12 個 study，這些讀數用於驗證流程，不足以宣稱競賽效能。不要依這次結果調低 `t_min=2` / `min_bases=2`。

seed 42 checkpoint SHA256：
`001256fb97d24c1ea6a6d87c83ba9417e5b7df866e2c414cf22ad3ef3281d82c`

seed 43 checkpoint SHA256：
`3675435cfde249c19bcd771ac0a42f272b44539bfa9419a26ad5bef9695feb38`

## 2. PowerShell 路徑與重跑規則

以下命令從 repo 根目錄執行。這些 ID 已存在；重做實驗必須換 run、plan、prereg、recipe、bundle 版本及輸出路徑，不能修改既有不可變檔。只讀命令與明示快取的命令可以重跑。`vcp` 與專案 CLI 成功為 exit 0，FAIL 為 1、ABORT 為 2；`--json` 的 JSON 在 stdout，最後的 VERDICT 在 stderr。`uv` / `kaggle` / `pip` 本身是第三方工具，不使用 vcp 的 VERDICT。

```powershell
$repo = 'C:/Users/smallfire123123/Desktop/Vision-contest-pipeline'
$data = 'C:/vcp-data'
$project = "$repo/projects/rsna-knee"
$trainEnv = "$project/.venv"
$trainPython = "$trainEnv/Scripts/python.exe"
$env:VCP_DATA_ROOT = $data
$env:VCP_CONFIGS_ROOT = "$repo/configs"
```

核心環境仍為 repo 的 `.venv`，沒有安裝 torch。訓練環境為專案自己的 `.venv`；實測 Python 3.12.14、torch 2.11.0+cu128、RTX 5070 Ti 16 GB。量測所用環境凍結後不要再 install。

本次建立訓練環境的命令（既有環境不用再跑）：

```powershell
uv venv --python 3.12 $trainEnv
uv pip install --python $trainPython torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python $trainPython -e '.[dicom]' pytest
uv pip freeze --python $trainPython
```

實際凍結版本在 [requirements-training.txt](requirements-training.txt)，不含 editable 的本機路徑。恢復新環境時先依官方 cu128 索引安裝 torch，再按凍結檔裝其餘套件並 editable 裝同一版本 vcp；不要把 Windows 的 wheel 放進 Linux notebook。

## 3. validate → split → materialize → export

```powershell
uv run vcp data validate --name rsna-knee
# v0.6.0 起 validate 同時寫 artifacts/source_audit/<id>（VERDICT source_audit=… source_audit_state=…）；之後訓練 / 量測不再整檔 hash samples.jsonl
uv run vcp data split --name rsna-knee --plan-id fixed-v1 --subsets train:train:0.4,valA:eval:0.2,valB:eval:0.2,holdout:sealed:0.2 --seed 42 --group-key auto
uv run vcp data materialize --name rsna-knee --mode png --resize 256 --workers 4
uv run python projects/rsna-knee/prepare.py export --out C:/vcp-data/exports/rsna-knee/fixed-v1/train
```

期待最後一行分別是 `VERDICT cmd=data.validate status=OK`、`data.split status=OK`、`data.materialize status=OK`、`rsna.export status=OK ... gold=22 unlabeled=142`，後面還有識別與計數欄位。第二次 materialize 使用已驗證快取。

**PNG 不加 `--stack-seq`**：資料 spec §16-1 明定 PNG 只能逐 slice；九通道組裝放在專案的 `study_tensor`。每個方位選 fluid-sensitive / fat-suppressed 序列，取 25% / 50% / 75% 三張；方位缺席補零。訓練與 notebook 共用相同實作。

test 的 `test.csv` 沒有 gold targets。專案匯入器在暫存目錄產生空 target 欄，保留官方十二類而不捏造負標籤；不寫 raw：

```powershell
uv run python projects/rsna-knee/prepare.py import-test --raw C:/vcp-data/raw/rsna-knee --downloaded-at 2026-09-04
uv run vcp data validate --name rsna-knee-test
uv run vcp data materialize --name rsna-knee-test --mode png --resize 256 --workers 4
```

期待 `rsna.import-test status=OK ... samples=3 unlabeled=3 skipped=0`、validate / materialize OK。test 的 all-v1 由 §7 的 submit init 建立。

## 4. 固定兩個種子訓練

`baseline-s42.json` / `baseline-s43.json` 在讀取候選 eval 分數前固定，不用 eval 做 epoch 選擇。訓練只載入 train 的 gold samples；`MaterializedReader(verify=True)` 檢查快取，`Session` 核對 dataset / plan / trained_on / config SHA / attempt seed，登記每個 epoch loss 與已關檔的 final checkpoint。

```powershell
uv run vcp train run --run knee-cnn-s42-v1 --dataset rsna-knee --plan fixed-v1 --export C:/vcp-data/exports/rsna-knee/fixed-v1/train --venv $trainEnv --seed 42 --config projects/rsna-knee/configs/baseline-s42.json --framework pytorch-2.11 --checkpoints 'C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s42-v1/*.pt' --final C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt --resume -- $trainPython projects/rsna-knee/train.py --config projects/rsna-knee/configs/baseline-s42.json --out C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt
uv run vcp train run --run knee-cnn-s43-v1 --dataset rsna-knee --plan fixed-v1 --export C:/vcp-data/exports/rsna-knee/fixed-v1/train --venv $trainEnv --seed 43 --config projects/rsna-knee/configs/baseline-s43.json --framework pytorch-2.11 --checkpoints 'C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s43-v1/*.pt' --final C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s43-v1/model.pt -- $trainPython projects/rsna-knee/train.py --config projects/rsna-knee/configs/baseline-s43.json --out C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s43-v1/model.pt
uv run vcp train status --run knee-cnn-s42-v1
uv run vcp train status --run knee-cnn-s43-v1
```

成功的外層尾行為 `VERDICT cmd=train.run status=OK`，子程序自己的 `rsna.train status=OK` 保存在 console。最後 epoch loss 約 0.701744 / 0.667820；seed 42 成功 attempt 2、seed 43 attempt 1。尚未備份權重時 status 的 unbacked WARN 是真實狀態。

seed 42 的第一個 attempt 用裸 `python`，Windows 實際啟動到沒有 typer 的基底 Python，已留下失敗紀錄。第二次以**絕對路徑** `$trainPython`、相同 config 加 `--resume` 完成；新 run 首次執行不要加 `--resume`。`--venv` 的環境探針成功不代表 Windows `Popen` 一定用該 venv 的裸命令，詳見 Plan 5 後記 §10。

## 5. 預測、預登記與量測

先產生兩個 run 的 valA / valB 預測並 ingest；不接觸 holdout。已有 training RunCard 的 ingest 不再指定另一個 weights / framework / trained_on：

```powershell
foreach ($seed in 42,43) {
    $run = "knee-cnn-s$seed-v1"
    foreach ($subset in 'valA','valB') {
        $csv = "$data/predictions/rsna-knee/$run/$subset.csv"
        & $trainPython projects/rsna-knee/predict.py --weights "$data/checkpoints/rsna-knee/$run/model.pt" --out $csv --dataset rsna-knee --plan fixed-v1 --subset $subset
        if ($LASTEXITCODE -ne 0) { throw 'prediction failed' }
        uv run vcp eval ingest --run $run --dataset rsna-knee --plan fixed-v1 --subset $subset --format scores_csv --src $csv --keep-input
        if ($LASTEXITCODE -ne 0) { throw 'ingest failed' }
    }
}
uv run vcp eval preregister --dataset rsna-knee --id knee-s43-vs-s42-v1 --claim 'Fixed seed 43 improves over fixed seed 42 on both untouched eval subsets' --component seed43 --class model --baseline-run knee-cnn-s42-v1 --candidate-run knee-cnn-s43-v1 --metric macro_auc --subsets valA,valB
uv run vcp eval measure --run knee-cnn-s42-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval anchor --run knee-cnn-s42-v1 --subset valA --metric macro_auc
uv run vcp eval anchor --run knee-cnn-s42-v1 --subset valB --metric macro_auc
uv run vcp eval measure --run knee-cnn-s42-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval measure --run knee-cnn-s43-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval judge --dataset rsna-knee --prereg knee-s43-vs-s42-v1
```

預測各 12 rows，`rsna.predict` / `eval.ingest` OK。首次參考 measure 因沒有 anchor 為 WARN / exit 0；anchor 後參考讀數命中快取，候選 measure OK。judge 的**主張判決是 FAIL**，一般命令仍 `VERDICT cmd=eval.judge status=OK ... verdict=FAIL` / exit 0；加 `--strict` 才以 exit 1 表示未準入。不要將命令 OK 誤認為模型通過。

## 6. 平均融合與逐成員準入

```powershell
uv run vcp fuse recipe --dataset rsna-knee --id knee-mean-v1 --plan fixed-v1 --method mean --member knee-cnn-s42-v1 --member knee-cnn-s43-v1
uv run vcp fuse ablate --dataset rsna-knee --recipe knee-mean-v1 --preregister --metric macro_auc
uv run vcp eval measure --run fuse-knee-mean-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval measure --run fuse-knee-mean-v1-minus-knee-cnn-s42-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval measure --run fuse-knee-mean-v1-minus-knee-cnn-s43-v1 --metrics macro_auc --subsets valA,valB
uv run vcp eval judge --dataset rsna-knee --prereg knee-mean-v1-admit-knee-cnn-s42-v1
uv run vcp eval judge --dataset rsna-knee --prereg knee-mean-v1-admit-knee-cnn-s43-v1
uv run vcp fuse build --dataset rsna-knee --recipe knee-mean-v1
```

所有操作正常完成；兩個 judge 的判決均 FAIL，`bases_positive=0`；最後 build `status=OK built=0 cached=2`。ablate 在 measure 前建立預登記。完整配方、兩個刪除成員的配方及準入 claims 都已進 configs；不修改既有配方來偷換成員。

## 7. test profile、離線 bundle 與私有 Kaggle notebook

**已完成** profile 與 bundle；**尚未執行** dataset create / kernels push / stage / upload。自動核准審查要求明確同意將程式、依賴及權重送到下列私有目的地，核准前不重試。

```powershell
uv run vcp submit init --dataset rsna-knee-test --eval-dataset rsna-knee --plan fixed-v1 --sealed holdout --platform kaggle --kind kernel --competition rsna-knee-abnormality-detection --metric macro_auc --board-rule best --slots 2 --quota 1 --kaggle-command 'uvx --from kaggle==2.2.4 kaggle'
```

已得到 `submit.init status=OK`、test `all-v1`。每日 1 次是保守的**本機治理配額**，不是已查證的 Kaggle 上限；deadline 未填，沒有猜測 2026-10-22 的具體時區 / 秒數。正式提交前核對比賽規則，必要時修改 git 管理的 `submit.yaml`。

Kaggle 憑證只由 Kaggle CLI 管理；本機 `uv tool list` 實際沒有已裝工具，使用 `uvx --from kaggle==2.2.4 kaggle` 已成功讀到自己的 notebooks。推論 notebook 不讀憑證、不印環境、網路關閉，CPU 推論；只上傳以下列出檔案，不打包 data / configs / logs。

本次建構 Linux / Python 3.12 離線 wheel 與 bundle 的實際命令：

```powershell
uv build --wheel --out-dir C:/vcp-data/artifacts/rsna-knee/wheels
uvx --from pip pip download --only-binary=:all: --platform manylinux_2_17_x86_64 --platform manylinux_2_27_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.12 --implementation cp --abi cp312 --dest C:/vcp-data/artifacts/rsna-knee/wheels --constraint projects/rsna-knee/requirements-training.txt C:/vcp-data/artifacts/rsna-knee/wheels/vcp-0.1.0-py3-none-any.whl pydicom pylibjpeg pylibjpeg-libjpeg pylibjpeg-openjpeg
uv run python projects/rsna-knee/bundle.py --out C:/vcp-data/artifacts/rsna-knee/kaggle-v1 --wheels C:/vcp-data/artifacts/rsna-knee/wheels --weights C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt --kernel pongpong1027/vcp-rsna-knee-baseline-v1 --dataset pongpong1027/vcp-rsna-knee-weights-v1
```

`rsna.bundle status=OK files=34 weights=1`。輸出：

- `C:/vcp-data/artifacts/rsna-knee/kaggle-v1/dataset/bundle.zip`：predict、共用 transform / model、vcp 與依賴 wheels、checkpoint、逐檔 SHA manifest。
- `.../dataset/dataset-metadata.json`：私有 dataset 的描述；原作者授權仍適用，無競賽影像或患者 metadata。
- `.../kernel/inference.ipynb` 與 `kernel-metadata.json`：指定 competition input 及私有 bundle，網路關閉。
- `.../bundle-manifest.json`：可檢閱的 34 檔 SHA 清單。

notebook 需要 Python 3.12 及 Kaggle 預裝 PyTorch；不同 Python 版本直接 ABORT，不在線下載。它先驗每個檔案 SHA，再從附掛 wheels 以 `--no-index --no-deps --target` 安裝到自己的 deps 目錄，最後用子程序跑 `predict.py`。zip 原檔或 Kaggle 自動解壓的目錄皆可讀。**Kaggle 上的實際 runtime 相容性仍待首次執行驗證。**

已完成本機驗證：

```powershell
& $trainPython projects/rsna-knee/predict.py --weights "$data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt" --out "$data/predictions/rsna-knee/knee-cnn-s42-v1/test-cached.csv" --dataset rsna-knee-test --plan all-v1 --subset test
& $trainPython projects/rsna-knee/predict.py --weights "$data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt" --out "$data/predictions/rsna-knee/knee-cnn-s42-v1/test-raw.csv" --raw "$data/raw/rsna-knee"
& $trainPython C:/vcp-data/artifacts/rsna-knee/bundle-check-v1/predict.py --weights C:/vcp-data/artifacts/rsna-knee/bundle-check-v1/weights/0.pt --raw C:/vcp-data/raw/rsna-knee --device cpu --out C:/vcp-data/predictions/rsna-knee/knee-cnn-s42-v1/test-bundle-cpu.csv
```

前兩個 CSV SHA 前綴都是 `02461e2f590c`；CPU 為 `1a1eca1cc02d`。`bundle-check-v1` 是逐檔驗 SHA 後的解壓目錄。不同裝置有微小浮點差，不能要求逐 byte 相同；同裝置 raw / cache 一致性及驗證集逐標籤排序已另外驗過。

**核准後才執行**（不覆寫帳號中其他 notebook；初建預設私有，沒有 `--public`）：

```powershell
uvx --from kaggle==2.2.4 kaggle datasets create -p C:/vcp-data/artifacts/rsna-knee/kaggle-v1/dataset
uvx --from kaggle==2.2.4 kaggle kernels push -p C:/vcp-data/artifacts/rsna-knee/kaggle-v1/kernel
uvx --from kaggle==2.2.4 kaggle kernels status pongpong1027/vcp-rsna-knee-baseline-v1
uvx --from kaggle==2.2.4 kaggle kernels output pongpong1027/vcp-rsna-knee-baseline-v1 -p C:/vcp-data/artifacts/rsna-knee/kaggle-output-v1
```

等 status complete，確認 notebook 尾行 `VERDICT cmd=rsna.notebook status=OK output=submission.csv`、CSV 的 UID 與官方 test.csv 一致、12 個機率欄有限且在 [0,1]。記錄實際成功版本後 stage；以下 `$kernelVersion` 必須是查到的數字，不先猜 1：

```powershell
uv run vcp submit stage --dataset rsna-knee-test --id knee-baseline-v1 --eval-run knee-cnn-s42-v1 --kind baseline --reason 'First image-only pipeline baseline; not an admitted ensemble' --kernel pongpong1027/vcp-rsna-knee-baseline-v1 --version $kernelVersion --weights knee-cnn-s42-v1:001256fb97d24c1ea6a6d87c83ba9417e5b7df866e2c414cf22ad3ef3281d82c
uv run vcp submit verify --dataset rsna-knee-test --id knee-baseline-v1
uv run vcp submit upload --dataset rsna-knee-test --id knee-baseline-v1
uv run vcp submit sync --dataset rsna-knee-test
uv run vcp submit status --dataset rsna-knee-test
```

stage 的 baseline waiver 留有理由，不能把未準入融合標成 candidate。kernel submission 用 notebook ID / 版本 / output / 權重綁定；沒有虛構 test-run。upload 預期 OK / uploaded，sync 以平台真實回傳追加 scored，評分未完成就稍後再 sync，不手動編造分數。kernels upload 不等於 competition submission。

## 8. 最後一次 sealed 評估與 final（尚未執行）

只有候選已固定、成功上傳且即將決選，才執行這個評估窗口；不得再依 sealed 分數調參。Reader 及量測各自的解封讀取都會留 audit row，理由一致：

```powershell
$finalReason = 'Final evaluation of the fixed first pipeline baseline; no tuning'
& $trainPython projects/rsna-knee/predict.py --weights C:/vcp-data/checkpoints/rsna-knee/knee-cnn-s42-v1/model.pt --out C:/vcp-data/predictions/rsna-knee/knee-cnn-s42-v1/holdout.csv --dataset rsna-knee --plan fixed-v1 --subset holdout --device cpu --unseal --reason $finalReason
uv run vcp eval ingest --run knee-cnn-s42-v1 --dataset rsna-knee --plan fixed-v1 --subset holdout --format scores_csv --src C:/vcp-data/predictions/rsna-knee/knee-cnn-s42-v1/holdout.csv --keep-input
uv run vcp eval measure --run knee-cnn-s42-v1 --metrics macro_auc --subsets holdout --unseal --reason $finalReason
uv run vcp submit final --dataset rsna-knee-test --dry-run
uv run vcp submit final --dataset rsna-knee-test
```

首次 holdout 沒有 anchor 時 measure 可 WARN，但必須有真實 sealed reading；final 只看 sealed 讀數。dry-run 先檢閱表，正式 final 追加 final / lock，之後不能再提交。未上傳的候選不進 final。

## 9. 備份與撤離（遠端步驟尚未執行）

本機基準程式已以 `8b1f77764defd823168dfe2c4b1e200c68372b10` 合併並 push。兩個 checkpoint 的 data.py / model.py SHA 重新驗過後，各在 `train.log.jsonl` 追加 `source_commit` 與 `checkpoint_code_sha_verified=true`；seed 42 另記 CPU / CUDA 排序一致。沒有改寫當初 `dirty=true` 的環境快照或失敗 attempt。

**已完成的同機備份**（這批尚無提交，因此結論用 `all`）：

```powershell
uv run vcp train upload --run knee-cnn-s42-v1 --dest C:/vcp-backup/rsna-knee/weights
uv run vcp train upload --run knee-cnn-s43-v1 --dest C:/vcp-backup/rsna-knee/weights
uv run vcp backup manifest --dataset rsna-knee --conclusion all --id knee-local-v1
uv run vcp backup push --dataset rsna-knee --manifest knee-local-v1 --dest C:/vcp-backup/rsna-knee/evidence --tier 1
uv run vcp backup push --dataset rsna-knee --manifest knee-local-v1 --dest C:/vcp-backup/rsna-knee/evidence --tier 2
uv run vcp backup push --dataset rsna-knee --manifest knee-local-v1 --dest C:/vcp-backup/rsna-knee/evidence --tier 3
uv run vcp backup verify --dataset rsna-knee --manifest knee-local-v1 --dest C:/vcp-backup/rsna-knee/evidence
```

實際結果：兩次 train upload 均 `uploaded=1 verified=1`；manifest 51 項、`remote_copies=2 missing=0`。tier 1 推 25 項、tier 2 新推 24 項、tier 3 跳過既有 49 項，另在原副本位置驗兩份權重。verify `status=OK ok=51 missing=0 mismatch=0 drift=0 bad_stamps=0`；status `OK manifests=1 unverified=0 rclone_conf=absent`。清單內的 `remote_copy` 是框架欄位名，本次兩份實際都在同機的 `weights/<run>/model.pt`，不代表異機安全。

vcp 證據圖以外的恢復材料也已複製並驗 SHA：

- `C:/vcp-backup/rsna-knee/artifacts/kaggle-v1/`：bundle.zip、dataset metadata、notebook、kernel metadata、bundle SHA manifest 五個檔案。
- `C:/vcp-backup/rsna-knee/knee-local-v1.json`：清單本身的副本。
- `C:/vcp-backup/rsna-knee/backup.log.snapshot.jsonl`：完成三個 tier 與 verify 後的備份台帳快照。
- `C:/vcp-backup/rsna-knee/source-8b1f777.bundle`：`git bundle create ... main` 產生，`git bundle verify` 通過、包含完整 main 歷史，可在無網路時恢復程式。

bundle.zip SHA256：`f45e4ba0a5d50b7ca252b6e34ae45510960b242ad313e2a119c86effb33beab2`。
Git bundle SHA256：`55e1057bd67f257a7903670ac1ba0d915108fdee469bd19e2030391c563160b5`。

本機已安裝官方 rclone 1.75.1 的 portable binary：下載自 `https://downloads.rclone.org/v1.75.1/`，安裝前比對官方 `SHA256SUMS`；未建立任何 remote 或憑證。每個新 PowerShell session 使用：

```powershell
$env:PATH = 'C:/vcp-data/tools/rclone-v1.75.1/bin/rclone-v1.75.1-windows-amd64;' + $env:PATH
uv run vcp backup status --dataset rsna-knee
```

已實測 `rclone_conf=absent`；沒有 rclone binary 時真實結果是 `unknown`，不能寫成 absent。尚未推送清單前 status 是 WARN，目前本機清單已驗證為 OK。

實際提交完成後，`$backupDest` 必須填使用者提供的本機路徑或 `remote:path`，不能把示意文字當目的地。rclone 的設定與認證在 rclone CLI 處理，vcp 不接收憑證：

```powershell
uv run vcp train upload --run knee-cnn-s42-v1 --dest $backupDest
uv run vcp backup manifest --dataset rsna-knee-test --conclusion submission:knee-baseline-v1 --id knee-submission-v1
uv run vcp backup push --dataset rsna-knee-test --manifest knee-submission-v1 --dest $backupDest --tier 1
uv run vcp backup push --dataset rsna-knee-test --manifest knee-submission-v1 --dest $backupDest --tier 2
uv run vcp backup push --dataset rsna-knee-test --manifest knee-submission-v1 --dest $backupDest --tier 3
uv run vcp backup verify --dataset rsna-knee-test --manifest knee-submission-v1 --dest $backupDest
```

manifest / push / verify 都應 OK；verify 的 copies `missing=0 mismatch=0`，本機漂移與壞時戳皆 0。先將權重 upload 到真正遠端、再產新清單，才能讓 `remote_copy` 指向遠端；不能拿仍指著 C 槽副本的 `knee-local-v1` 當異機撤離證據。run / checkpoint / 預測 / 台帳及 configs 依證據圖納入，raw / cache 不進清單。notebook bundle 位於 artifacts，**不自動成為 vcp 證據圖的一部分**；需另保存 bundle 及逐檔 SHA manifest，不能僅因 vcp verify OK 就宣稱 notebook 重建材料已全部備妥。

只有真實 rclone remote、且**整份清單**（包括高 tier）都驗證成功，才執行：

```powershell
uv run vcp backup push --dataset rsna-knee-test --manifest knee-submission-v1 --dest $backupDest --tier 3 --forget-remote
uv run vcp backup status --dataset rsna-knee-test
```

期待 `backup.push status=OK` 及 `backup.status ... rclone_conf=absent`。本機目的地不能傳 `--forget-remote`，會 `forget_refused`；本機副本不等於異機容災。沒有真實提交時不得建立假的 `submission:` 證據。

## 10. 測試、裁決與未完成事項

```powershell
uv run pytest --cov=vcp
uv run ruff check .
uv run ruff format --check .
uv run ruff check projects/rsna-knee/rsna_knee projects/rsna-knee/train.py projects/rsna-knee/predict.py projects/rsna-knee/prepare.py projects/rsna-knee/bundle.py tests/unit/test_rsna_knee_glue.py tests/integration/test_rsna_knee_project.py
uv run ruff format --check projects/rsna-knee/rsna_knee projects/rsna-knee/train.py projects/rsna-knee/predict.py projects/rsna-knee/prepare.py projects/rsna-knee/bundle.py tests/unit/test_rsna_knee_glue.py tests/integration/test_rsna_knee_project.py
& $trainPython -m pytest tests/unit/test_rsna_knee_glue.py -o addopts=''
uv run pytest tests/integration -o addopts='' -q -m realdata
```

全套實測 **949 passed / 5 skipped，coverage 96.67%**（221.37 秒）；16 個 project unit tests 在獨立 torch venv 全過。核心環境只 skip torch checkpoint roundtrip，沒有因測試安裝 torch。真資料煙霧測試只讀來源 DICOM，全部衍生檔寫 tmp_path；缺資料即 skip。單元檔採 `test_rsna_knee_glue.py`，避免 pytest 與 integration 同 basename 的 import mismatch。全庫 ruff check / format 及上述新增檔的明列檢查皆綠，因 repo 預設排除 projects，歷史下載腳本未納入這次推論 runtime。

單獨真資料 gate：**9 passed / 3 skipped**（26.39 秒）；三個 skip 是尚無資料的 marine-debris 測試。

| 裁決 / 處置 | 依據 | 代價 / 待續 |
|---|---|---|
| PNG slice + 專案九通道組裝 | data spec 禁止 PNG volume | 不執行 PNG `--stack-seq`；未修改核心 schema |
| 只訓練 22 gold，保留 142 none 的 lineage | none 不是全零標籤 | 此批資料不足以訓練高品質模型 |
| 固定 epochs、兩種子，FAIL 判決照留 | 防止看過 eval 再改主張 | 融合未准入，第一次僅用 baseline waiver |
| Windows 訓練用絕對 interpreter | 裸 python 實際落到基底環境 | 一個失敗 attempt 保留；通用解析修復另列 Plan 5 待辦 |
| checkpoint 綁 data.py / model.py bytes | 防止前處理或架構靜默漂移 | 已訓練後連格式化也需新 run；bundle 保留原 bytes |
| CPU / CUDA 比較逐標籤排序 | 浮點差不等於 AUC 差 | 不承諾跨硬體逐 byte 一致；Kaggle runtime 還需實測 |
| 不做 Report 特徵、無 raw / metadata 上傳 | 推論只使用像素與序列方位 | bundle 與資料必須分開附掛 |
| train role 負面測試比對 unseal ledger 前後 bytes | 既有 det fixture 自己已留下解封事件 | 改正「檔案應不存在」的錯誤期待，仍嚴格證明新操作未寫入 |
| Kaggle 上傳等待明確目的地核准 | 自動核准審查拒絕這次外傳 | notebook / competition submission / scored / final 尚未完成 |
| 遠端目的地未提供，先完成同機副本與 SHA 驗證 | 保全已完成的模型、證據與程式 | 51 項證據已驗證；待指定 `$backupDest`，遠端重建新清單並另保存 notebook bundle |

參考官方文件：[PyTorch 安裝版本](https://pytorch.org/get-started/previous-versions/)、[Kaggle kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md)、[Kaggle dataset metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)、[rclone 下載](https://rclone.org/downloads/)、[比賽頁](https://www.kaggle.com/competitions/rsna-knee-abnormality-detection)。
