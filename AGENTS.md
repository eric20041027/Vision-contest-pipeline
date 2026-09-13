# vcp 操作慣例（給 agent 與人看）

## 三條機械鐵則
1. 取時只能用 `vcp.core.time.utc_now()` / `stamp()`。ruff TID251 會擋 `datetime.now` / `utcnow` / `today` / `time.time`；唯一例外是 `src/vcp/core/time.py`。
2. 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 1（FAIL）/ 2（ABORT）。命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。eval / fuse / train / submit / backup 命令以 `run_command(context=)` 保留失敗時已知的 dataset / run / recipe / id 等識別欄位；可選值未給時省略。
3. venv 隔離：核心 `vcp` 一個 venv（`uv sync`）；訓練框架各自 venv，以 editable 裝 `vcp`；量測 venv 凍結後禁 install。

## 通用性原則
以資料形態與任務類型為軸，不以特定比賽為軸。比賽專屬程式碼放 `projects/<contest>/`，不進 `src/vcp`。十二個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核、轉換器、指標、σ_p 方法、融合器、輸出格式、平台）都是登記表；加一種形態 = 加一個登記項，不改 schema、不改 CLI。

## 路徑
- 資料根目錄：`VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）。`raw/<name>/` 永不修改；`datasets/<name>/` 放 `samples.jsonl`、`raw_manifest.txt`、`cache/`。
- 設定根目錄：`VCP_CONFIGS_ROOT`（預設 repo 的 `configs/`）。`configs/datasets/<name>/dataset.yaml` 與 `splits/*.json` 進 git；plan 檔一旦寫入不可修改，要改就換 plan-id。
- 整合測試讀 `VCP_REALDATA_ROOT` / `VCP_REALDATA_CONFIGS`，資料缺席即 skip；`raw/<name>/` 永不修改。
- `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/` 是可搬移的解碼快取，`manifest.jsonl` 為權威對照。
- `measure/<name>/` 只增不改：`readings.jsonl`、`judgements.jsonl`、`sigma.jsonl` 是 append-only 台帳，錨點 `anchors.json` 整份換寫但每次都先寫 `anchors.log.jsonl`。`runs/<run_id>/`（`run.yaml`、`predictions/<subset>.jsonl`）不是只增不改，而是「換寫留痕」：`ingest --replace` 會重寫 `run.yaml` 與該子集的預測檔，舊 sha 進 `history.jsonl`（`history.jsonl` 本身只增不改）。預登記 `configs/datasets/<name>/prereg/<id>.yaml` 與 `prereg.log.jsonl` 進 git，寫下就不改，要改就換 id。融合配方 `configs/datasets/<name>/fuse/<id>.yaml` 進 git、寫了不改；融合 run 是普通 run，另有 `runs/<id>/fuse.json`（每次 build 整份換寫，記每個成員預測檔的 sha 與輸出 sha）。
- `runs/<id>/train.yaml` 是訓練紀錄的快照（每次事件整份重寫）、`train.log.jsonl` 只增；`train/` 放 console、config 副本、環境快照。checkpoint 不搬動，只記路徑與 sha；`--upload` 的副本另記 sha 與驗證結果。`vcp train run` 開始就寫 `run.yaml`，之後 `eval ingest` 直接接上。
- 提交治理：`configs/datasets/<test>/submit.yaml`（平台設定，改就改 git）與 `submissions.jsonl`（只 append 的台帳：staged / uploaded / scored / foreign / final / lock / unlock）進 git；輸出檔與 `stage.json` 在 `submit/<test>/<id>/`（寫一次不改）。候選 = (eval run, test run) 配對，身分靠 `weights_hash`；`final` 只看 sealed 讀數。vcp 不碰平台憑證。
- `configs/datasets/<name>/backup/<manifest_id>.json` 是證據清單（從結論反向生成，寫一次不改，進 git），`backup.log.jsonl` 只增（manifest / push / verify / pull / remote_forgotten）。目的地佈局 `<dest>/data|configs|external/<相對路徑>`；`train upload` 驗過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。`cache/`、`raw/` 永不進清單；push 推台帳的快照，`--forget-remote` 要整份清單驗證通過。
- `artifacts/<kind>/<id>/` 是不可變產物：`spec.json`（open 時寫）、檔案們、`manifest.json`（commit 點；**有它才是產物**）、`failure.json`（例外離開時）；`artifacts/<kind>/supersession.jsonl` 只增。同 id 不能重開；修正用新 id + `supersedes`。`vcp artifact clean` 只移除超過寬限期的半途目錄與 `.tmp`。vcp 自己的四個寫一次檔（split plan、預登記 yaml、融合配方、backup manifest）與產物的每個檔都經 `vcp.core.atomic.write_once`。
- `artifacts/access_receipt/<id>/receipt.json` 是存取器留下的收據（train 下 `<run>-a<attempt>-<n>`，其他 `<purpose>-<dataset>-<plan>-<stamp>-<nonce>`）：只有授權子集的列會被解析，未授權 → `denied:` 並計數；`run.yaml` / `train.yaml` 的 `access` 列出掛上的收據，等級 `receipt > export > declared` 讀取時算出。measure 的乾淨基底 = `trained_on ∪ 觀測`；judge 讀過主張子集 → `INVALID contaminated`；`submit.yaml` 的 `require_provenance` 決定 gate。
- `artifacts/source_audit/src-<dataset>-<hash16>/`（`audit.json` + `index.jsonl`）是 `vcp data import` / `validate` 留的逐列 sha 索引（內容定址、同內容重用、不做 supersedes）：存取器有它就不掃 `samples.jsonl`、只驗讀到的列；壞掉 → `mismatch:` FAIL；缺席 → 退回整檔 hash 並 WARN `source_audit=missing`；收據 `identity` 記用了哪條路。壞掉的稽核沒有 supersedes：搬走目錄再 `validate` 重建。

## 常用命令
- `uv sync` / `uv run vcp --help` / `uv run pytest --cov=vcp` / `uv run ruff check .`
- `uv run vcp data export --name X --plan P --subset S --format coco|yolo --out DIR` / `uv run vcp data audit --name X [--against TEST]`
- `uv run vcp data materialize --name X --mode npy|png [--resize L] [--stack-seq]`
- `uv run vcp eval measure --run R`（護欄 → 讀數；`--unseal --reason` 才動 sealed 子集）/ `uv run vcp eval judge --dataset D --prereg ID [--strict]`
- `uv run vcp eval status --dataset D` / `uv run vcp eval report --dataset D`（兩者唯讀）；比賽自己的指標或格式以 `--plugin projects.<contest>.metrics` 登記
- `uv run vcp fuse recipe --dataset D --id R --plan P --method wbf|mean|rank_mean --member RUN[:W]…` / `uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M [--replace]`（每位成員一份準入預登記，交給 `vcp eval judge`；先 ablate 再 measure）
- `uv run vcp train run --run R --dataset D --plan P --export DIR --venv ENV --seed N --checkpoints "…" --final "…" [--upload DEST] -- <訓練命令>` / `uv run vcp train status --run R`（唯讀）/ `uv run vcp train upload --run R --dest DEST`（冪等）
- `uv run vcp submit stage --dataset T --id S --eval-run E --test-run R` / `uv run vcp submit upload --dataset T --id S`（Kaggle）或 `record --at "…"`（手動）/ `uv run vcp submit final --dataset T`（決選 + 封槍）/ `uv run vcp submit status --dataset T`（唯讀）
- `uv run vcp backup manifest --dataset D --conclusion submission:ID|judgement:P|run:R|all [--id M]` / `uv run vcp backup push --dataset D --manifest M --dest DEST [--tier 1|2|3] [--forget-remote]`（先小後大、逐檔驗、冪等）/ `uv run vcp backup verify --dataset D --manifest M [--dest DEST [--tier N]]`（副本 / 一致性 / 時戳三層；`--tier` 只限副本層）/ `uv run vcp backup pull --dataset D --manifest M --dest DEST [--tier N] [--overwrite]` / `uv run vcp backup status --dataset D`（唯讀）
- `uv run vcp artifact create --kind K --id I --file PATH… [--input name=PATH…] [--supersedes OLD --reason R] [--id-pattern RE]` / `uv run vcp artifact verify --kind K --id I` / `uv run vcp artifact lineage --kind K --id I` / `uv run vcp artifact status [--kind K]`（唯讀）/ `uv run vcp artifact clean [--older-than 24h] [--apply]`；程式內用 `vcp.artifact.writer.ArtifactWriter`、重用用 `vcp.artifact.store.reuse`
- `uv run vcp eval ingest --run R … --receipt ID` 掛外部收據；訓練迴圈 `with MaterializedReader(…, plan_id=P, subset="train") as reader:` 或 `Session.current().access(subsets={"train"})` 才會有收據；`vcp eval status --dataset D` 看每個 run 的 provenance
- `uv run vcp data validate --name X` 也負責產 / 重用 `source_audit`（VERDICT `source_audit=` `source_audit_state=`）；`uv run vcp artifact status --kind source_audit` 看現有稽核

## 版本
- SemVer，停在 `0.x`。MINOR = 寫進產物 / 台帳的內容或 CLI 契約（命令、VERDICT 欄位、exit code、`reason=` 字彙、登記項）改變；PATCH = 其餘修正；`1.0.0` 留給稽核 Wave 1 落地。規則、build string 格式與發版四步在 `CHANGELOG.md` 表頭。
- `src/vcp/__init__.py` 的 `__version__` 是唯一來源（`pyproject.toml` 動態讀它）。產物的 `vcp_version` 是 `vcp.core.build.build_string()`——版本 + commit + dirty（`0.2.0+g<sha>.dirty`），`vcp version` 印同一字串；每個 release 一個 `vX.Y.Z` annotated tag。`0.1.0` 是未發版的開發期，該時期的產物回推不到單一 commit（CHANGELOG 有說明）。

## 文件
- RSNA Knee 實際基準流程：`projects/rsna-knee/RUNBOOK.md`（真實命令、讀數、外部待續條件）；設計與裁決：同目錄 `DESIGN.md`。Windows 訓練命令使用獨立 venv 的絕對 interpreter；checkpoint 綁定的前處理 / 模型檔不可在訓練後靜默改動。
- 設計 spec：`docs/superpowers/specs/`；實作計畫：`docs/superpowers/plans/`；賽後報告：`docs/postmortems/`
- 交接：`docs/handover/HANDOVER.md`（現況、程式碼地圖、待辦、流程、陷阱）與 `docs/handover/CODEX_PROMPT.md`（接續開發的完整指示）；開放待辦在各後記（`docs/superpowers/plans/*-followups.md`）的最後一節。

## 給 Codex / 其他代理
- 先讀本檔與 `docs/handover/HANDOVER.md`，再讀要改的那一層的 spec 與後記；spec 的「補充決定」以程式碼為準。
- 開始、接續、操作、交接或向使用者介紹完整比賽時，以 `vcp-running-contests` skill 為入口；它會再指向 Day 1、資料層與登記表的專用 skills。
- 一件事一個分支一個 commit（`type(scope): 說明`），不用 `git add -A`；每個行為變更先寫失敗的測試；commit 前 `uv run ruff check . && uv run ruff format --check .`，合併前全套 `uv run pytest --cov=vcp`。
- 裁決（spec 沒說的決定）與處置寫進該層後記的最後一節；不對 markdown 跑 `ruff format`；測試重寫台帳 / 卡一律 `newline="\n"`。
