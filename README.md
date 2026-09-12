# vcp — vision contest pipeline

可重複使用的影像競賽框架：標準資料格式、多重驗證集切分、lineage、進場稽核、materialize 快取；量測層接標準預測格式、指標、護欄、σ_p、預登記與判決，並提供融合、訓練紀錄、提交治理與備份審計。設計文件見 `docs/superpowers/specs/`，操作慣例見 `AGENTS.md`。

```bash
uv sync                      # 核心 venv；DICOM 支援：uv sync --extra dicom
uv run vcp --help
uv run pytest --cov=vcp
```

第一次使用或要讓 agent 接手完整比賽時，使用專案 skill `vcp-running-contests`（`.agents/skills/` 與 `.claude/skills/` 皆有鏡像）。它先判定現況，再串起資料、訓練、量測、融合、提交與備份；新比賽 Day 1、資料層細節與新增通用登記項分別交給既有三個專用 skills。

## 資料層命令

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp data import` | 原始資料 → `dataset.yaml` + `samples.jsonl` | `--importer`、`--src`、`--name`、`--license`、`--url`、`--downloaded-at`、`--opt k=v`、`--raw-manifest full\|sizes` |
| `vcp data validate` | 重驗 card、samples 與 hash | `--name` |
| `vcp data audit` | 座標 sanity、近重複與 test 重疊、來源檢查 | `--against`、`--max-bad-boxes`、`--min-box-px`、`--max-aspect`、`--max-cover`、`--hamming`、`--corr` |
| `vcp data split` | 固定多子集 plan（進 git、不可改） | `--plan-id`、`--subsets`、`--stratify-key`、`--group-key`、`--group-from-audit`、`--strategy` |
| `vcp data lineage` | 某訓練用了哪些子集 → 哪些驗證集還乾淨 | `--plan`、`--trained-on` |
| `vcp data export` | 子集 → COCO / YOLO 目錄 + manifest | `--plan`、`--subset`、`--format`、`--out`、`--opt view=`、`--opt copy=true`、`--unseal --reason` |
| `vcp data materialize` | 每個 view 解碼一次成 npy / png 快取 + manifest | `--mode`、`--resize`、`--stack-seq`、`--window`、`--workers`、`--force`、`--decoder` |

每個命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；`--json` 時結果到 stdout、VERDICT 到 stderr。eval / fuse / train / submit / backup 失敗時仍帶命令已知的 dataset / run / recipe / id 等識別欄位；深層錯誤可提供更精確的身分。

## 量測層命令 `vcp eval`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp eval ingest` | 框架輸出 → run 的標準預測檔（記 sha、建或更新 `run.yaml`） | `--run`、`--dataset`、`--plan`、`--subset`、`--format jsonl\|coco_results\|yolo_txt\|scores_csv`、`--src`、`--export-manifest`、`--trained-on`、`--framework`、`--notes`、`--weights PATH`、`--config PATH`、`--keep-input`、`--replace`、`--opt allow_unknown=true` |
| `vcp eval measure` | 護欄 → 每個乾淨 eval 子集 × 適用指標一列讀數 | `--run`、`--metrics`、`--subsets`、`--params k=v`、`--unseal --reason` |
| `vcp eval anchor` | 把既有讀數設成該 plan/子集/指標的護欄 | `--run`、`--subset`、`--metric`、`--params`、`--tolerance`（須有限且 ≥ 0）、`--replace` |
| `vcp eval sigma` | 估 σ_p 並 append | `--dataset`、`--plan`、`--metric`、`--method`（已登記的 σ_p 估法；內建 `splithalf` / `bootstrap` / `prior`，其餘以 `--plugin` 登記）、`--params`、`--subsets`、`--run`（bootstrap 預設取該 cell 的錨點 run）、`--prior --note`、`--resamples`、`--seed` |
| `vcp eval preregister` | 量候選之前先把主張寫死（進 git） | `--dataset`、`--id`、`--claim`、`--component`、`--class model\|tuning`、`--baseline-run`、`--candidate-run`、`--metric`、`--params`、`--subsets`、`--t-min`、`--min-bases`、`--sigma-method`、`--sigma-ratio` |
| `vcp eval judge` | 配對 bootstrap → Δ、se、t、基底數、σ_p 條件 → 判決 | `--dataset`、`--prereg`、`--resamples`、`--seed`、`--strict`、`--unseal --reason` |
| `vcp eval status` | 孤兒預登記、run / 預登記 / 判決 / 錨點數、最新 σ_p | `--dataset`、`--max-age-hours` |
| `vcp eval report` | 全部 run × subset 讀數（全精度）+ 每個判決的 last-vs-last | `--dataset`、`--metric`、`--plan` |

共用選項：`--json`、`--data-root`、`--configs-root`；前六個命令另有 `--plugin <module>`（可重複，import 該模組讓它登記指標、轉換器或 σ_p 估法），`status` / `report` 不碰登記表所以沒有。狀態與 exit code：未知 sample_id、缺讀數、選項不合法 → FAIL(1)；沒有錨點、σ_p 為 0、有孤兒預登記 → WARN(0)；護欄對不上 → ABORT(2) 且一列讀數都不寫，VERDICT 帶 `guardrail=FAIL anchor=<reading_id> got=<值>`。判決本身不是工具錯誤：`status=OK verdict=PASS|FAIL|INVALID`，要讓 FAIL 擋 CI 就加 `--strict`。

### 標準預測格式

`runs/<run_id>/predictions/<subset>.jsonl`，一列一個 sample，依 `sample_id` 排序、LF、UTF-8。payload 欄位由 task 決定，其餘為 null：

```json
{"sample_id": "s0001", "boxes": [{"x": 12.0, "y": 8.0, "w": 40.0, "h": 25.0, "category_id": 3, "score": 0.91, "view": 0}]}
{"sample_id": "s0002", "masks": [{"category_id": 1, "score": 0.88, "rle": "<COCO compressed RLE>", "meta": {"size": [512, 512]}}]}
{"sample_id": "s0003", "scores": {"cat": 0.7, "dog": 0.2, "bird": 0.1}}
{"sample_id": "s0004", "targets": {"age": 41.5}}
```

det → `boxes`、seg → `masks`（`rle` 與 `polygon` 恰一）、cls / multilabel → `scores`（鍵 = card 的類別名）、regression → `targets`。cls / multilabel / regression 每個 sample 都要有一列，缺列即 FAIL；det / seg 缺列視為零偵測，計入 `empty`。四種轉換器負責把框架輸出轉成這個格式，`yolo_txt` 與 `coco_results` 需要 `--export-manifest` 指向對應子集的 `vcp data export` 目錄。

### 一次判決的流程

```bash
uv run vcp eval ingest --run base --dataset D --plan fixed-v1 --subset valA --format yolo_txt \
  --src runs/base/valA --export-manifest exports/valA --trained-on train   # valB 同樣再跑一次
uv run vcp eval measure --run base
uv run vcp eval anchor --run base --subset valA --metric coco_map          # 之後每次 measure 都重驗
uv run vcp eval preregister --dataset D --id p1 --claim "新 backbone 更好" --component backbone-v2 \
  --class model --baseline-run base --candidate-run cand --metric coco_map # 先寫死，才准量候選
uv run vcp eval measure --run cand
uv run vcp eval judge --dataset D --prereg p1 --strict
```

`--class tuning` 的主張還要先有 σ_p（`vcp eval sigma --method splithalf|bootstrap|prior`），否則判決 `FAIL reason=no_sigma`。最後用 `vcp eval status --dataset D` 看有沒有寫了卻沒判的主張，`vcp eval report --dataset D` 看全部讀數與 last-vs-last。

比賽官方計分器、比賽專屬格式或自訂 σ_p 估法放在 `projects/<contest>/`，以 `--plugin projects.<contest>.metrics` 匯入，模組自己呼叫 `register_metric` / `register_converter` / `register_sigma_method` 登記，`src/vcp` 不出現比賽名稱。

## 融合層命令 `vcp fuse`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp fuse recipe` | 驗成員後把配方寫進 git（`configs/datasets/<name>/fuse/<id>.yaml`，寫了不改） | `--dataset`、`--id`、`--plan`、`--method wbf\|mean\|rank_mean`、`--params k=v`、`--member RUN[:WEIGHT]`（可重複，順序有意義）、`--notes` |
| `vcp fuse build` | 成員預測檔 → 融合 run（`runs/fuse-<id>/`，`fuse.json` 記每個成員的 sha 與輸出 sha） | `--dataset`、`--recipe`、`--run`、`--subsets`、`--replace` |
| `vcp fuse ablate` | 每位成員一份「少了它」的變體配方與 run；`--preregister` 時每位成員一份準入預登記，交給 `vcp eval judge` | `--dataset`、`--recipe`、`--subsets`、`--no-build`、`--replace`（透傳給它觸發的每個 build）、`--preregister --metric M --metric-params k=v --bases valA,valB --t-min --min-bases` |

共用選項：`--json`、`--data-root`、`--configs-root`、`--plugin <module>`（自訂融合器以 `register_fuser` 登記）。融合結果就是普通 run：`trained_on` 取成員聯集、`source.framework=vcp.fuse`、`source.config_hash` = 配方檔 sha，量測與判決全用 `vcp eval`。同配方再 build 是 `cached=`；成員檔動一個位元是 FAIL `IntegrityError`；同一 run id 只綁一份配方。

### 一輪準入

```bash
uv run vcp fuse recipe --dataset D --id r1 --plan fixed-v1 --method wbf --params iou=0.6 --params min_score=0.02 --member a --member b:0.5
uv run vcp fuse ablate --dataset D --recipe r1 --preregister --metric coco_map   # 寫 r1-minus-*、建 fuse-r1 與 fuse-r1-minus-*、寫 r1-admit-*
uv run vcp eval measure --run fuse-r1 && uv run vcp eval measure --run fuse-r1-minus-a && uv run vcp eval measure --run fuse-r1-minus-b
uv run vcp eval judge --dataset D --prereg r1-admit-a    # PASS = a 證明了自己的位置；FAIL = 降權或移除，換新配方 id 再來
```

準入 = 「有它 vs 沒它」：候選是完整配方、基準是少了該成員的變體，主張 class 固定為 model。完整配方量測過就不能再寫準入預登記（`candidate_measured`），所以先 ablate 再 measure。成員重新 ingest 後直接 `vcp fuse ablate --replace`：這面旗子透傳給完整配方與每個變體的 build，沒變的子集照樣是 cached，舊 sha 進各 run 的 `history.jsonl`。

若 run 卡仍在但 `fuse.json` 遺失，快取命中或省略已宣告子集會 FAIL `not_found`；用 `vcp fuse build --replace` 恢復。此恢復會重建卡上全部子集（含 `--subsets` 未列者及位元相同者），先驗完所有成員才寫入，舊 sha 仍進 history。

`wbf`（boxes）：`iou`、`skip`（輸入框門檻）、`min_score`（融合後門檻）、`max_per_image`、`conf_type=avg|max`，語意同 ensemble-boxes 的 `weighted_boxes_fusion(allows_overflow=False)` 但在像素座標運算、依 (view, category) 分群、有尺寸才裁邊。`mean`（scores / targets）加權平均；`rank_mean`（scores）以子集為母體的名次平均，AUC 型指標用、不是機率。

## 訓練層命令 `vcp train`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp train run` | 包在任何訓練命令外面：開始就寫 `run.yaml`（`trained_on` 由 export manifest 推導）、複製 config、環境快照、console 落檔、結束後登記 checkpoint 的 sha、上傳並驗證 | `--run`、`--dataset`、`--plan`、`--export DIR`（可重複）或 `--trained-on a,b`、`--venv DIR`、`--config`、`--seed`、`--framework`、`--cwd`、`--checkpoints GLOB`（可重複）、`--final GLOB`、`--upload DEST`（可重複）、`--resume`、`--notes`；`--` 之後是訓練命令 |
| `vcp train upload` | 事後或換目的地上傳已登記的 checkpoint，冪等 | `--run`、`--dest`、`--only final` |
| `vcp train status` | attempts / checkpoints / 副本（唯讀；人類行印最後一個 attempt 的命令）；`backed=` / `unbacked=`（目前的 bytes 沒副本，會 WARN）/ `superseded=`（同路徑已被後來的登記取代、又從沒上傳過的舊 bytes，只報不 WARN——它們的副本再也不會出現） | `--run`、`--verify`（重算 sha） |

共用選項：`--json`、`--data-root`；`--configs-root` 只有 `vcp train run` 有（另外兩個命令只讀資料根目錄下的 run）。`--upload` 的目的地：`remote:path` 走 rclone，經 `vcp.backup.dest.RcloneDest`（`copyto --checksum` + `hashsum sha256` 逐檔比對，指令前綴 `vcp.backup.dest.RCLONE`；rclone 不在 PATH 又沒注入 runner 是 `VcpError("rclone_not_found: ...")` ABORT，指令有跑但失敗是 `PlatformError`〔FAIL，最後一行已去敏〕；`hashsum` exit 3/4 才算「還沒東西」，其餘非 0 或雜湊欄不是 sha256 都是錯誤），其餘是本機 / 掛載目錄（複製後讀回驗 sha）。訓練命令 exit ≠ 0 → `status=FAIL exit_code=N`，checkpoint 仍登記但不上傳；沒給 `--seed`、`--venv`、`--final` 各 WARN 一項。`run.yaml` 就是量測層的 run：之後 `vcp eval ingest --run R ...` 直接接上，不必再給 `--trained-on` / `--framework`。`--resume` 每次都新增一個 attempt，各記自己的 `command` / `seed` / `venv`；`train.yaml` 頂層那三欄刻意留著**第一個** attempt 的值（它描述 run 是怎麼開始的），要看最新的看 `attempts[-1]`。上一輪若還停在 `running`（vcp 自己崩過），`--resume` 會把它標成 `interrupted` 並在 `train.log.jsonl` 記一列 `note`。

### 一次訓練到量測

```bash
uv run vcp data export --name D --plan fixed-v1 --subset train --format yolo --out exports/D-train
uv run vcp train run --run y12x_r2 --dataset D --plan fixed-v1 --export exports/D-train \
  --venv C:/venvs/ultra --seed 42 --framework "ultralytics 8.3.0" --cwd projects/D \
  --checkpoints "runs-ultra/y12x_r2/weights/*.pt" --final "runs-ultra/y12x_r2/weights/best.pt" \
  --upload gdrive:vcp/weights -- yolo train model=yolo12x.pt data=exports/D-train/data.yaml epochs=60
uv run vcp train status --run y12x_r2                    # unbacked=0 才算有副本
uv run vcp eval ingest --run y12x_r2 --dataset D --plan fixed-v1 --subset valA --format yolo_txt --src ... --export-manifest exports/D-valA
uv run vcp eval measure --run y12x_r2
```

自寫 PyTorch loop 只需要兩個名字：

```python
# torch 與 NAMES（你的類別名清單）是讀者自己的東西，vcp 不提供也不要求
from vcp.train import MaterializedReader, Session

reader = MaterializedReader("rsna-knee", "png-r256", plan_id="fixed-v1", subset="train")


class Knee(torch.utils.data.Dataset):
    def __len__(self):
        return len(reader)

    def __getitem__(self, i):
        rec = reader[reader.ids[i]]  # rec.arrays: {"0": HxW(xC)} 或 {seq_id: SxHxW}
        x = torch.from_numpy(next(iter(rec.arrays.values())))
        y = torch.tensor([rec.labels.targets[n] for n in NAMES])
        return x, y


s = Session.current()  # 在 vcp train run 底下才有
s.register_checkpoint("ckpt/best.pt", final=True)
s.note("val_auc", 0.91)
```

## 提交治理命令 `vcp submit`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp submit init` | 寫 `configs/datasets/<test>/submit.yaml`（平台、配額與時區、截止、決選指標、輸出格式），並替 test dataset 建單子集 plan `all-v1` | `--dataset`（test dataset）、`--eval-dataset`、`--plan`、`--sealed`、`--platform manual\|kaggle`、`--competition`、`--kind file\|kernel`、`--board-rule last\|best`、`--quota N --day-tz TZ`、`--display-tz`、`--deadline`、`--metric`、`--writer`、`--writer-opt k=v`、`--kaggle-command` |
| `vcp submit stage` | 四道門（封槍 / 截止、eval-test 配對核對、準入判決、產檔）全過才寫 `submit/<test>/<id>/` 與台帳 `staged` 列 | `--id`、`--eval-run`、`--test-run`、`--kind candidate\|baseline\|probe`、`--reason`、kernel 類 `--kernel --version --weights RUN[:sha]`、`--writer-opt`、`--plugin` |
| `vcp submit upload` | Kaggle：再驗 sha → 配額 → `kaggle competitions submit` → `uploaded` 列 | `--id`、`--message` |
| `vcp submit record` | 手動平台：你在網頁上傳後回填，平台顯示時間換成 UTC | `--id`、`--at "YYYY-MM-DD HH:MM"`、`--tz platform\|utc`、`--platform-ref` |
| `vcp submit score` / `sync` | 回填 public / private；Kaggle 以 `competitions submissions` 回讀、配對、把別人的發記成 `foreign`（照數配額）。同一個 foreign ref 的狀態或分數變了（PENDING → COMPLETE 等）就多記一筆快照，VERDICT `refreshed=`；配額與到達仍每個 ref 算一次 | `--public`、`--private` |
| `vcp submit final` | 已準入且已上傳的候選依 sealed 讀數（同分看 public、再看 staged 時間）選出 `final_slots` 個，寫決選表並封槍 | `--slots`、`--dry-run` |
| `vcp submit lock` / `unlock` | 封槍 / 解封（留理由） | `--reason` |
| `vcp submit status` / `report` | 配額剩餘與重置時間、截止倒數、榜面現任、未回填；每發 last-vs-last、sealed 讀數、public→private 位移（唯讀） | |
| `vcp submit verify` | 三驗第三驗：重讀檔 sha、從 test run 重產比對（位元級重現） | `--id` |

候選 = (eval 側 run, test 側 run) 一對：單模比 `weights_hash`（用 `vcp eval ingest --weights PATH` 給 run 身分），融合比 method / params / 成員權重並逐成員遞迴。`candidate` 需要 `vcp eval judge` 的 PASS 判決（融合 run 每個成員各一份）；`baseline` / `probe` 要 `--reason`，probe 永不進決選。沒有 `--override`。vcp 不碰 Kaggle 憑證：只呼叫 kaggle CLI，輸出經 redact 才落地。

### 一次提交

```bash
uv run vcp eval ingest --run good.test --dataset D-test --plan all-v1 --subset test --format scores_csv --src preds.csv --weights runs-ultra/good/weights/best.pt
uv run vcp submit stage --dataset D-test --id SUB34 --eval-run good --test-run good.test
uv run vcp submit upload --dataset D-test --id SUB34            # 手動平台改 record --at "2026-08-31 21:28"
uv run vcp submit sync --dataset D-test                          # 手動平台改 score --public 0.7916
uv run vcp eval measure --run good --subsets holdout --unseal --reason "final pick"
uv run vcp submit final --dataset D-test                        # 自動封槍；board_rule=last 時照 needs_reupload 重傳
```

## 備份審計命令 `vcp backup`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp backup manifest` | 從結論反向走證據圖，寫 `configs/datasets/<name>/backup/<id>.json`（進 git、寫一次不改）：每個檔的角色、tier、sha、大小、服務的結論 | `--dataset`、`--conclusion submission:<id>\|judgement:<prereg>\|run:<id>\|all`、`--id` |
| `vcp backup push` | 先小後大推到 rclone 遠端或本機目錄（`<dest>/data\|configs\|external/…`），只推目的地沒有或不同的檔，推完逐檔比對 sha；`train upload` 驗過的權重副本（`remote_copy`）不重推 | `--manifest`、`--dest`、`--tier 1\|2\|3`（累積到 N；預設 1）、`--forget-remote`（整份清單的每個檔都在目的地驗過（含更高 tier）才 `rclone config delete <remote>`） |
| `vcp backup verify` | 三層稽核：副本（給 `--dest` 才做；`absent=` 是清單時已缺、目的地也沒有，不算失敗）、本機一致性（卡 ↔ 預測檔、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔、台帳 ↔ `stage.json`、清單 ↔ 現在的檔）、時戳（台帳逐列 `ts` 可解析且單調、卡的 `*_at` 可解析） | `--manifest`、`--dest`、`--tier`（副本層只查 tier 1..N，預設 3 = 全部） |
| `vcp backup pull` | 從目的地把清單裡的檔拉回原相對路徑、讀回驗 sha；本機已有且不同 → `conflict`，`--overwrite` 才蓋（舊檔留 `.bak-<時戳>`，新檔驗過才留）；`external_skipped=` 是清單外的絕對路徑，只在目錄已存在時還原 | `--manifest`、`--dest`、`--tier`（預設 3）、`--overwrite` |
| `vcp backup status` | 每份清單最新的 push / verify、從未推過的 tier、`rclone_conf=present\|absent\|unknown`（唯讀）；`verified` 要有一次 verify 連 tier 3 副本層都過，只驗本機兩層記 `local_ok` | |

tier 1 決策層（台帳、卡、判決、預登記、配方、快照、候選檔；KB 級）、tier 2 重現層（預測檔、樣本、`train/`、logs；MB 級）、tier 3 權重層（GB 級，只在要求時）。`cache/`、`raw/` 永不進清單。清單只含路徑、sha、大小、時間與 dest 字串；vcp 不讀 rclone 設定檔內容，rclone 的輸出經 redact 才落地，台帳 `backup.log.jsonl` 只增。台帳在清單之後長大：verify 不算漂移，push 推的是清單那一刻的快照（前 N 位元組），pull 視為已有；被改或截短才算漂移。

### 機器回收前的撤離順序

```bash
uv run vcp backup manifest --dataset D-test --conclusion submission:SUB34 --id sub34   # 最終發需要的一切
uv run vcp backup push --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 1   # 先救決策層
uv run vcp backup push --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 2   # 再救重現層
uv run vcp backup manifest --dataset D --conclusion all --id all-final                        # 有空再救全部
uv run vcp backup push --dataset D --manifest all-final --dest gdrive:vcp/backup --tier 3 --forget-remote
uv run vcp backup status --dataset D-test                                                    # rclone_conf=absent 才走
```

### 賽後重建

```bash
git pull                                                                # 清單與台帳跟著 configs/ 回來
uv run vcp backup pull --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 2
uv run vcp backup verify --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 2
uv run vcp submit verify --dataset D-test --id SUB34                    # 位元級重現候選檔
```

## 不可變產物命令 `vcp artifact`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp artifact create` | 把既有檔案封成一個產物：`<data_root>/artifacts/<kind>/<id>/`，`mkdir` 獨佔搶 id，逐檔 `.tmp → fsync → replace`，最後寫 `manifest.json`（有它才是產物）；`--input` 在 open 與 commit 各雜湊一次（變了 → `drift:`）；`--id-pattern` 的具名群組必須等於同名欄位（`seed` / `dataset` / `plan_id` / `--param` 的鍵） | `--kind`、`--id`、`--file PATH\|NAME=PATH`…、`--dataset`、`--plan`、`--seed`、`--param k=v`…、`--input name=PATH`…、`--supersedes OLD --reason R`、`--id-pattern RE`、`--notes` |
| `vcp artifact show` | 印 manifest：欄位、input、檔案表、`supersedes=` 與 `superseded_by=`（唯讀） | `--kind`、`--id` |
| `vcp artifact verify` | 逐檔重算 sha（`mismatch=` / `missing=`）、manifest 沒列的檔（`extra=`，含殘留 `.tmp`）、supersession 台帳（缺列 `unlinked=1` → WARN；列與 manifest 不符 → `mismatch`） | `--kind`、`--id` |
| `vcp artifact lineage` | 根 → id → 接替者；`heads=` 是沒被接替的末端，`forks=` > 0 → WARN（唯讀） | `--kind`、`--id` |
| `vcp artifact status` | 每個 kind 的 `complete=` / `partial=`（有 `spec.json` 沒 manifest；附 `opened_at` 與 `failure.json` 的例外類別）/ `unlinked=` / `forks=` / `foreign=`（沒有 `spec.json` 的目錄，不碰）（唯讀） | `--kind` |
| `vcp artifact relink` | manifest 有 `supersedes` 而台帳缺列（manifest 之後、台帳之前崩潰）→ 從 manifest 補一列；冪等 `appended=0\|1` | `--kind`、`--id` |
| `vcp artifact clean` | 列出（`--apply` 才移除）`opened_at` 早於 `--older-than` 的半途目錄與同樣老的 `.<name>.<nonce>.tmp`；有 manifest 的產物、台帳、外來目錄永不碰；讀不到 `spec.json` 的目錄不列 | `--kind`、`--older-than N{m\|h\|d}`（預設 `24h`，`0` 可）、`--apply` |

程式產生的產物走 Python API：

```python
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.paths import DatasetPaths

paths = DatasetPaths.resolve("knee")
spec = ArtifactSpec(
    kind="selection",
    id="six-slot-v2-s42",
    seed=42,
    id_pattern=r"six-slot-v2-s(?P<seed>\d+)",
    inputs=[
        InputRef(name="plan", path=str(paths.plan_json("fixed-v1")))
    ],  # 絕對路徑；相對路徑以 data root 為基準
)
with ArtifactWriter.create(spec, data_root=root) as art:
    art.write_json("receipt.json", payload)  # write_text / write_bytes 也有
    art.add_file("weights.pt", src_path)  # 串流複製並雜湊
    out = art.reserve("features.npy")  # 回傳最終路徑，自己用 numpy 寫；commit 時雜湊
    manifest = art.commit()  # 寫 manifest.json = commit；之後任何寫入 → closed:
```

`vcp.artifact.store.reuse(spec, root)` 只在完整 spec 逐欄相等（含每個 input 現算的 sha，`notes` 除外）才回傳既有 manifest，否則 `spec_mismatch:`——目錄存在不等於可重用。例外離開 `with` 會留下半途目錄與經 redact 的 `failure.json`；同一 id 不能再開，修正一律新 id + `supersedes`（open 查舊產物存在且已 commit，commit 驗逐檔 sha 並記其 manifest sha，之後 append `artifacts/<kind>/supersession.jsonl`）。manifest / `spec.json` / 台帳只含路徑、sha、大小、時戳、build string 與結構化參數——不放憑證。

## 匯入器與 `rows_read` 的語意

| 匯入器 | 來源 | `rows_read` 數的是 |
|---|---|---|
| `jsonl` | 已是標準格式的 `samples.jsonl` | sample 列 |
| `csv_boxes` | 一列一框的 CSV + 影像目錄 | CSV 資料列 |
| `coco` | instances JSON + 影像目錄 | annotations |
| `yolo` | `images/` + `labels/*.txt` + 類別表 | 影像 |
| `imagefolder` | `root/<class>/*` | 影像 |
| `image_csv` | CSV 路徑欄 + 標籤 / 目標欄 | CSV 資料列 |
| `dicom` | DICOM 目錄樹（header 分組） | 掃到的檔案 |

所有影像匯入器接受 `--opt exif=stored|oriented`（預設 `stored`），Orientation ≠ 1 的 view 會記進 `view.meta.exif_orientation` 並讓 VERDICT 帶 `exif_rotated=<n>` WARN；`materialize`（npy 與 png 皆然）是唯一會把方向烙進像素的步驟；`dicom` 匯入器不讀 EXIF。

## DICOM 形態的用法（以 RSNA Knee 為例）

200-study 影像基準的實際訓練、量測、融合、離線 notebook 打包、提交與備份命令見 [RSNA Knee RUNBOOK](projects/rsna-knee/RUNBOOK.md)。已實跑到本機 bundle；外部上傳與備份的待續條件也記在手冊。PyTorch 使用 `projects/rsna-knee/.venv`，Windows wrapper 後的訓練命令使用該 venv 的絕對 Python 路徑。

```bash
uv run vcp data import --importer dicom --src C:/vcp-data/raw/rsna-knee/train_series --name rsna-knee \
  --license "Competition rules" --url https://www.kaggle.com/competitions/rsna-knee-abnormality-detection \
  --downloaded-at 2026-09-03 \
  --opt labels_csv=../train.csv --opt "target_cols=ACL,MCL,Medial Meniscus,Lateral Meniscus,Medial OA,Lateral OA,PF OA,Effusion,Synovitis,Baker's,Contusion,Fracture" \
  --opt meta_cols=Report --opt seq_csv=../train_series.csv --opt "seq_cols=Fluid_Sensitive,Fat_Suppression,Anatomical_Plane" \
  --opt role_from=Anatomical_Plane --opt workers=8
uv run vcp data materialize --name rsna-knee --mode png --resize 256 --workers 4
uv run vcp data materialize --name rsna-knee --mode npy --stack-seq --workers 4
```

`sample_level=study|series`、`view_level=slice|series`、`group_from=<tag>`（預設 PatientID）、`glob=`、`tags=` 為其餘選項。快取在 `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/`，`manifest.jsonl` 內路徑皆為相對路徑。

### 在 Kaggle notebook 上跑

```bash
pip install git+https://github.com/eric20041027/Vision-contest-pipeline
export VCP_DATA_ROOT=/kaggle/working/vcp-data VCP_CONFIGS_ROOT=/kaggle/working/vcp-configs
vcp data import --importer dicom --src /kaggle/input/<comp>/train_series --name <name> ... --raw-manifest sizes
vcp data materialize --name <name> --mode png --resize 256 --workers 4
```

把 `vcp-data/datasets/<name>/`（含 `cache/materialize/`）與 `vcp-configs/datasets/<name>/` 打包成 Kaggle Dataset 下載到本機同樣的相對位置即可沿用。`--raw-manifest sizes` 只記檔名與大小，card 的 `raw_manifest_mode` 會如實記錄。
