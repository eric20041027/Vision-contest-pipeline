# vcp 提交治理層設計（子專案 5）

- 日期：2026-09-05
- 狀態：v1（brainstorming 逐節核可後寫成）
- 前置：子專案 0/1 資料層（`2026-09-02-vcp-skeleton-and-data-layer-design.md`）、子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md` §11：「上傳決策讀 `judgements.jsonl` 與 report，本 spec 不定上傳格式」）、子專案 4 融合層（`2026-09-05-vcp-fusion-layer-design.md` §10：「test 側的配方另寫；eval 側與 test 側 method / params / weights 的核對規則由 5 定」）、子專案 3 訓練層（`2026-09-05-vcp-training-layer-design.md` §10：「提交層讀 `run.yaml` 的 `weights_hash` 與 `train.yaml` 的 `uploads`」）
- 來源：賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md` §9 藍圖第 7 點（md5 三驗；台帳寫入是上傳流程的原子步驟；榜面回讀；最後一發由規則決定；封槍協議；去留判決須過凍結 holdout；配額狀態機、雙時區程式化）、§6 錯誤目錄 #1（時區混亂）、#6（max-of-N 偽增益）、#7（台帳失同步）、§8 結構性缺陷 2（準入標準不一致）與 5（量測與上傳的耦合）、§5.3 驗屍 3（被 public 確認過的路徑集合）
- 後續：子專案 6 備份審計

## 1. 目的

讓「上傳」變成一個有紀錄、有門檻、有身分的動作：每一發都對得回 eval 側量過的那個模型（不是名字相同，是權重 hash 相同）、都過了同一套機械準入、都在配額與封槍狀態機底下、台帳列是命令的副產品而不是人補的。最後一發（或 Kaggle 的最終選擇）由寫死在設定檔的規則從凍結 holdout 的讀數決定，不由 public 榜面決定。

框架不綁平台：AIdea 這類沒有 API 的平台由人上傳、`vcp submit record` 回填；Kaggle 走它自己的 CLI。vcp 對憑證零接觸。

不在本 spec：無 API 平台的自動上傳與瀏覽器自動化、非 Kaggle 的榜面抓取、隊友通知與回執、σ_p 自動回寫、AWS 重現包與 README、Kaggle dataset（權重）上傳、notebook 產生、輸出檔壓縮（§16）。

## 2. 核心架構原則

- **候選 = 一對 run。** 一個候選綁一個 eval 側 run（量測、判決、sealed 讀數都在它身上）與一個 test 側 run（上傳檔由它產生）。兩側是不是同一個模型由 hash 決定（§7）：單模比 `weights_hash`，融合比 method / params / 成員權重並逐成員遞迴。名字不算數。
- **門在寫入之前。** `stage` 把配對核對、準入門、封槍、截止全做完才產檔、才寫台帳；任何一道不過就零副作用。準入門沒有 `--override`：門沒過還要傳的唯一合法路徑是誠實標成 `probe`（永不進決選）。對應報告 §8 缺陷 2、§9 第 5 點「判決違規在系統層阻斷」。
- **台帳是動作的副產品。** `upload` 在 CLI 回 0 的同一個命令裡寫列；`record` 把人做的上傳連同平台時間一起寫列；`sync` 把不是 vcp 上傳的發也寫進來（`foreign`）並照數配額。對應報告 §6 #7。
- **時間只有 UTC 與程式換算。** 配額視窗與平台顯示時間全走 `zoneinfo`；`record --at` 必填、不接受「現在」。對應報告 §6 #1。
- **決選看 holdout，不看 public。** `final` 只在已準入且已上傳的候選中，依 sealed 子集讀數排序；public 只做同分的次序鍵。對應報告 §5.3 驗屍 3、§9 第 2 點。
- **報 last-vs-last。** `report` 只給每一發對前一發的 Δ，從不對「最好那發」。對應報告 §6 #6。
- **憑證零接觸。** vcp 不讀、不寫、不驗、不記錄任何 API 憑證（§11）。

### 2.1 擴充點

| 軸 | 位置 | v1 內容 | 加一項的成本 |
|---|---|---|---|
| 輸出格式 | `submit/writers/` | `scores_csv`、`coco_results`、`csv_boxes` | 一個實作 `Writer` 的模組，`register_writer` 登記；比賽專屬放 `projects/<contest>/`，`--plugin` 載入 |
| 平台 | `submit/platforms/` | `manual`、`kaggle` | 一個實作 `Platform` 的模組 |

決選規則 `final_rule` 是設定檔裡的 Literal（v1 只有 `best_sealed`），不是登記表；再加規則時升格。

## 3. 已定案的決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 平台互動 | staging + 台帳為核心；Kaggle 經其 CLI 上傳（含 code 賽的 kernel 提交）與回讀；其餘平台人上傳、`record` 回填 | 沒 API 的平台只能手動；Kaggle CLI 已裝（2.2.4） |
| 最終挑選 | `best_sealed`：sealed 讀數 → public → staged 時間；寫死在設定檔 | 驗屍 3 的直接對策；規則進 git，每個事件記 `profile_sha256` |
| test 側 | test 集是自己的 dataset（無標籤）；候選 = (eval run, test run)；身分靠 hash | 融合層留下的規則；不動 `RunCard` 與 `samples_hash` 的不變量 |
| 台帳位置 | `submissions.jsonl` 與 `submit.yaml` 進 git（configs）；輸出檔與 `stage.json` 進 data root | 跟預登記一樣是決策紀錄：團隊看得見、diff 得到；大檔不進 git |
| 準入 | candidate 須 PASS 判決（融合 run 每個直接成員各一份）；baseline / probe 須 `--reason`；無 override | 準入統一且機械（報告 §8 缺陷 2） |
| 配額 | 視窗 = `[day_start @ day_tz, +24h)`；`uploaded` + `foreign` 計數；`tzdata` 進依賴 | Windows 沒系統 tz 資料庫；報告 §6 #1 |
| 上傳原子性 | CLI 回 0 即寫 `uploaded`；回應解析不了只降 WARN `confirmed=false` | 動作已發生，台帳必須記 |
| 憑證 | 零接觸；CLI 原文全部 redact 後才可落地 | 使用者要求 |

## 4. 資料模型（`src/vcp/submit/schema.py`，pydantic，`extra="forbid"`）

### 4.1 平台設定檔（`configs/datasets/<test>/submit.yaml`，`PlatformProfile`，進 git）

```yaml
dataset: beach-trash-test          # test dataset（無標籤）；等於目錄名
eval_dataset: beach-trash          # eval 側 run 所屬 dataset
plan_id: fixed-v1                  # eval 側 plan；sealed 子集在這裡
sealed_subset: holdout             # 決選讀數來源，plan 裡 role=sealed
test_plan: all-v1                  # test dataset 的單子集 plan（init 會建）
test_subset: test
platform: kaggle                   # manual | kaggle
competition: rsna-2026-knee        # kaggle 必填；manual 可省
submission_kind: file              # file | kernel（Kaggle code 賽）
board_rule: best                   # last | best：榜面掛哪一發（init 預設 manual→last、kaggle→best）
final_rule: best_sealed
final_slots: 1                     # Kaggle 可 2
quota: {per_day: 5, day_tz: UTC, day_start: "00:00"}   # 省略 = 不查配額
display_tz: America/New_York       # record --at 的預設解讀時區；省略 = day_tz，再省略 = UTC
deadline: 2026-10-22T23:59:00Z     # UTC stamp；省略 = 不查
metric: macro_auc                  # 決選看的 sealed 讀數
metric_params: {}
writer: scores_csv                 # submission_kind=file 必填
writer_opts: {id_field: view_stem, id_col: id}
kaggle_command: ["uv", "tool", "run", "kaggle"]   # 預設 ["kaggle"]
created_at: <UTC stamp>
```

驗證：`day_tz` / `display_tz` 必須是 `zoneinfo` 認得的名稱；`day_start` 為 `HH:MM`；`deadline` 走 `parse_stamp`；`per_day ≥ 1`；`final_slots ≥ 1`；`platform=kaggle` 時 `competition` 必填；`submission_kind=file` 時 `writer` 必填。**沒有任何憑證欄位**，`extra="forbid"` 讓 `kaggle_key` 之類的鍵直接 FAIL。設定檔寫後由 git 管：`init` 拒絕覆寫；台帳的 `staged` / `uploaded` / `final` 列記當下的 `profile_sha256`，規則被改看得見。

### 4.2 候選快照（`submit/<test>/<submission_id>/stage.json`，`Staged`，寫一次不改）

```json
{
  "submission_id": "SUB34-m1skip02",
  "dataset": "beach-trash-test",
  "kind": "candidate",
  "eval_run": "fuse-golden12",
  "test_run": "fuse-golden12.test",
  "pairing": {"mode": "fusion", "members": [{"eval": "codino_ft", "test": "codino_ft.test", "mode": "single"}], "checks": ["method=wbf", "params=…", "members=12"]},
  "artifact": {"kind": "file", "path": "submission.csv", "bytes": 1234567, "sha256": "…", "md5": "…",
               "writer": "csv_boxes", "writer_version": "1", "writer_opts": {"id_field": "view_stem"},
               "rows": 18342, "samples": 2092, "missing": 0},
  "gate": {"admission": "PASS", "judgements": ["golden12-admit-codino_ft", "…"], "reason": ""},
  "profile_sha256": "…",
  "staged_at": "<UTC stamp>",
  "vcp_version": "<vcp version 印的同一字串>"
}
```

kernel 類候選：`test_run` 為 `null`；`artifact` 為 `{"kind": "kernel", "kernel": "user/notebook", "version": 7, "output": "submission.csv", "weights": [{"run": "yolo11m-s0", "sha256": "…"}]}`；`pairing.mode = "kernel"`。vcp 只核對它知道的東西（宣告的權重 sha 對得上 `run.yaml.weights_hash`），notebook 內容不驗，`checks` 明寫 `notebook=declared`。

### 4.3 台帳（`configs/datasets/<test>/submissions.jsonl`，只 append）

每列 `{"event": …, "ts": <UTC stamp>, …}`，`ts` 一律由寫入時的 `stamp()` 產生。事件與各自的欄位：

| event | 欄位 |
|---|---|
| `staged` | `submission_id`、`kind`、`eval_run`、`test_run`、`sha256`、`md5`、`gate`（同 stage.json）、`profile_sha256` |
| `uploaded` | `submission_id`、`at`（上傳的 UTC 時間）、`source`（`vcp` \| `manual`）、`platform_ref`（平台給的 id，可 null）、`message`、`confirmed`（bool）、`sha256`（上傳前重算）、`profile_sha256` |
| `scored` | `submission_id`、`public`、`private`（各可 null）、`source`（`manual` \| `platform`）、`platform_status`（可 null） |
| `foreign` | `platform_ref`、`file_name`、`at`、`public`、`private`、`platform_status`、`submitted_by`（平台有給才記） |
| `final` | `rule`、`slots`、`chosen`（submission_id 清單）、`table`（每位候選：`submission_id`、`eligible`、`why`、`sealed_value`、`sealed_reading_id`、`public`、`staged_at`）、`metric`、`params`、`holdout_unseals`、`profile_sha256` |
| `lock` / `unlock` | `reason` |
| `note` | `submission_id`（可 null）、`text` |

分數與讀數非有限值一律拒寫（同量測層）。`uploaded` 可對同一個 `submission_id` 出現多次（同檔重傳當最終發就是第二列）。

## 5. 目錄佈局

```
<VCP_CONFIGS_ROOT>/datasets/<test>/submit.yaml
<VCP_CONFIGS_ROOT>/datasets/<test>/submissions.jsonl
<VCP_CONFIGS_ROOT>/datasets/<test>/splits/all-v1.json           # init 建的單子集 plan
<VCP_DATA_ROOT>/submit/<test>/<submission_id>/stage.json
<VCP_DATA_ROOT>/submit/<test>/<submission_id>/<writer.file_name>   # submission.csv / results.json
```

`DatasetPaths` 新增 `submit_yaml`、`submissions_log`、`submit_dir`、`submission_dir(submission_id)`；`submission_id` 走 `validate_name`。

## 6. CLI 總表（`vcp submit`）

`--dataset` 一律是 test dataset。共用 `--json`、`--data-root`、`--configs-root`；`stage` 與 `verify` 另有 `--plugin`（載入 writer）。所有命令以 VERDICT 收尾，exit 0 / 0 / 1 / 2。

| 命令 | 作用 | 狀態規則 |
|---|---|---|
| `init --dataset T --eval-dataset D --plan P --sealed S --platform manual\|kaggle [--competition C] [--kind file\|kernel] [--board-rule last\|best] [--quota N --day-tz TZ --day-start HH:MM] [--display-tz TZ] [--deadline UTC] [--metric M --params k=v] [--writer W --writer-opt k=v] [--kaggle-command "uv tool run kaggle"]` | 寫 `submit.yaml`（`--kaggle-command` 以空白切成清單）；test dataset 沒有 `all-v1` 就直接組 `SplitPlan` 寫檔（全體樣本 → `test`，role `eval`，`params.eval_gold_only=false`，不經產生器） | 設定檔已存在 → FAIL `reason=exists`；eval 側 plan 沒有 role=sealed 的 `--sealed` → FAIL；時區名未知 → FAIL |
| `stage --dataset T --id S --eval-run E [--test-run R] [--kind candidate\|baseline\|probe] [--reason …] [--kernel user/nb --version N --output submission.csv] [--weights RUN[:sha]]… [--writer-opt k=v]…` | §6.1 的順序：檢查全做完才產檔、才寫 `staged` | 任一門不過 → FAIL 零副作用；id 已存在（目錄或台帳）→ FAIL；`kind` 非 candidate 而無 `--reason` → FAIL `reason=reason_required`。stage 不吃配額 |
| `upload --dataset T --id S [--message TEXT]` | Kaggle：再驗 sha → 封槍 / 截止 / 配額 → 平台適配器上傳 → `uploaded` 列 `source=vcp` | `platform=manual` → FAIL `reason=manual_platform`；sha 不符 → `IntegrityError`；配額用盡 → FAIL；CLI 非 0 → FAIL 不寫列；CLI 回 0 但無法確認 → WARN `confirmed=false` 仍寫列 |
| `record --dataset T --id S --at "2026-08-31 21:28" [--tz platform\|utc] [--platform-ref ID] [--message TEXT]` | 手動平台（或 Kaggle 在網頁上傳的發）：再驗 sha → 封槍 / 截止 → `--at` 換 UTC → `uploaded` 列 `source=manual` | `--at` 在未來或早於 `staged_at` → FAIL；該視窗已滿 → WARN `quota_overflow=`（動作已發生，照記）；sha 不符 → `IntegrityError` |
| `score --dataset T --id S [--public X] [--private Y]` | `scored` 列 `source=manual` | 沒有 `uploaded` 列 → FAIL；兩個都沒給 → FAIL；非有限 → FAIL |
| `sync --dataset T` | Kaggle：讀全部平台 submissions（翻頁到底）→ §6.3 配對 → 補 `scored`（`source=platform`）、不認得的發寫 `foreign`（去重） | `platform=manual` → FAIL；新 `foreign` > 0 → WARN `foreign=`；台帳有 `uploaded` 但平台查無 → WARN `unconfirmed=`；平台回應缺必要鍵 → FAIL `reason=platform_response key=` |
| `final --dataset T [--slots N] [--dry-run]` | §6.4：可決選者依 sealed 讀數排序取 N，寫 `final` 列再寫 `lock` 列 | 全無 sealed 讀數 → FAIL `reason=no_sealed_readings`；部分缺 → WARN `unranked=`；`board_rule=last` 且選中者不是最後一發 → WARN `needs_reupload=`；已 lock → FAIL；過截止 → WARN；`--dry-run` 只印決選表不寫 |
| `lock --dataset T --reason …` / `unlock --dataset T --reason …` | 封槍 / 解封各一列 | 已 lock 再 lock、未 lock 就 unlock → FAIL |
| `status --dataset T` | 唯讀：配額 `used/per_day`、`resets_at`（UTC）與 `local=`（平台時區）、`deadline_in=`、`locked=`、榜面現任（`board_rule=last` → 最後一發；`best` → public 最高）、未回填分數的發 | 剩 0 / 已 lock / 截止 < 24h / 有未回填 → WARN；`quota` 省略 → WARN `quota=none` |
| `report --dataset T` | 唯讀：每一發（依 `at` 排序）的 public、對前一發的 Δ（last-vs-last）、sealed 讀數、private、public→private 位移 | 永遠 OK，`rows=` |
| `verify --dataset T --id S [--plugin …]` | 三驗第三驗：重讀檔 sha 對 `stage.json`；writer 從 test run 重產到暫存比 sha；融合候選再核 `fuse.json` 的 `output_sha256` = test run 預測 sha | 任一不符 → `IntegrityError` FAIL；kernel 類只核權重 sha |

### 6.1 stage 的順序（檢查全做完才寫）

1. 載入設定檔（記 `profile_sha256`）、test dataset、eval dataset、eval 側 plan；`sealed_subset` 必須存在且 role 為 `sealed`。
2. 封槍 → FAIL `reason=locked since=<ts> why=<reason>`；過截止 → FAIL `reason=past_deadline`。
3. `--id` 走 `validate_name`；目錄或台帳已有 → FAIL `reason=exists`。
4. 載入 `eval_run`：`assert_run_matches`（eval dataset）且 `plan_id` = 設定的 `plan_id`，否則 `PlanMismatchError` `side=eval`。file 類載入 `test_run`：`assert_run_matches`（test dataset）且 `plan_id` = `test_plan`，否則 `PlanMismatchError` `side=test`；`verify_prediction(test_subset)` 必須通過。
5. 配對核對（§7）。
6. 準入門（§8）。
7. file 類：讀 test run 的預測 → writer 寫到 `submit/<T>/.tmp-<S>/<file_name>` → sha256 + md5 → 完整性檢查（writer 回的 `missing`；`stage.json` 的 `artifact.missing` 記其個數）→ 整個目錄改名為 `submit/<T>/<S>/`。kernel 類：只建目錄。
8. 寫 `stage.json` → append `staged`。append 失敗即刪掉目錄（全有或全無）。

### 6.2 upload / record 的原子性

順序：設定檔 → 封槍 → 截止 → 重算輸出檔 sha 對 `stage.json`（不符 → `IntegrityError`，不動台帳）→ 配額（`upload` 用 `utc_now()` 所在視窗，滿 → FAIL；`record` 用 `--at` 所在視窗，滿 → WARN `quota_overflow=` 照記）→ 動作 → 寫列。`upload` 的 `at` = CLI 回 0 的那一刻的 `utc_now()`；`message` 預設為 `<submission_id>`（sync 靠它配對），使用者給的 `--message` 會在前面加上 `<submission_id> ` 前綴。CLI 非 0 → FAIL，訊息是 redact 後的 stderr 最後一行，不寫列。

### 6.3 sync 的配對

對平台的每一發 P（`platform_ref`、`file_name`、`at`、`description`、`public`、`private`、`status`）依序：

1. `description` 含某個台帳裡的 `submission_id` → 配給它。
2. 否則 `file_name` = 該候選的輸出檔名，且台帳有該候選的 `uploaded` 列，`|P.at − at| ≤ 10 分鐘` → 配給它。
3. 否則 → `foreign`（以 `platform_ref` 去重，重跑 sync 不重複寫）。

配到的：P 有分數而台帳最新 `scored` 不同（或沒有）→ append `scored`（`source=platform`）。台帳有 `uploaded` 但沒有任何 P 配到它 → `unconfirmed`。平台時間一律由適配器轉成 UTC stamp 後再進 vcp。

### 6.4 final 的演算法

1. 已 lock → FAIL；過截止 → WARN 照做（Kaggle 的最終選擇在截止前做，AIdea 類的「最後一發」也可能需要在截止前重傳）。
2. 可決選者 = `staged.kind ∈ {candidate, baseline}` 且至少一列 `uploaded`。
3. 每位候選的 sealed 讀數 = `measure/<eval_dataset>/readings.jsonl` 中 `run_id = eval_run`、`plan_id`、`subset = sealed_subset`、`metric`、`params_key(metric_params)` 的最新一筆；再驗兩件事：`prediction_sha` = `eval_run` 現在 `predictions[sealed_subset].sha256`（否則當缺，`why=stale_reading`）、`n_samples` = sealed 子集大小（否則當缺，`why=partial_reading`）。
4. 有讀數者依（`higher_is_better` 取向的讀數 ↓，public ↓（缺 = 最低），`staged_at` ↑）排序，取 `slots` 位。
5. 排序名單為空 → FAIL `reason=no_sealed_readings`；有候選缺讀數 → WARN `unranked=` 並在 `table` 記 `eligible=false why=…`。
6. `board_rule=last` 且 `chosen[0]` ≠ 最後一列 `uploaded` 的候選 → WARN `needs_reupload=<id>`（決定已做，重傳是另一個動作：`record` / `upload` 同 id 再一列）。
7. `--dry-run` 只印決選表；否則 append `final`（含整張表與 `holdout_unseals` = `splits/<plan>.unseal.jsonl` 裡 `subset = sealed_subset` 的列數）再 append `lock`（`reason=final`）。

### 6.5 時間與配額的計算

- 視窗：把時間點換到 `day_tz` 的牆鐘時間，取當天 `day_start` 為起點（牆鐘時間早於 `day_start` 則取前一天），終點 = 起點加一天（牆鐘加法，DST 交界由 `zoneinfo` 處理），兩端再換回 UTC。
- 已用 = 視窗內 `uploaded`（以 `at`）與 `foreign`（以 `at`）的列數；`remaining = per_day − used`。
- `resets_at` = 視窗終點的 UTC stamp；VERDICT 另給 `local=<終點在 day_tz 的牆鐘>`。
- `record --at` 的字串格式 `YYYY-MM-DD HH:MM[:SS]`，`--tz platform`（預設，`display_tz`）或 `--tz utc`；列上同時記 `at`（換算後 UTC）與 `ts`（寫入時刻）。
- 存取時鐘只經 `vcp.core.time`；測試以 monkeypatch `utc_now` 固定時間。

## 7. 配對核對規則（融合層留給本層的規則）

輸入 `eval_run` 與 `test_run` 的 `RunCard`（§6.1 第 4 步已驗 dataset 與 plan）。

1. **模式**：兩側都有 `fuse.json` → `fusion`；都沒有 → `single`；只有一側有 → FAIL `reason=identity field=fuse.json`。
2. **single**：`source.weights_hash` 兩側非空且相等，否則 FAIL `reason=identity field=weights_hash side=<缺的那側|both>`；`source.config_hash` 兩側都非空時必須相等（不等 → FAIL `field=config_hash`），一側空 → WARN `config_hash=unchecked`。
3. **fusion**：`method`、`method_version`、`params` 相等；成員數相等；依位置逐對：`weight` 相等、對每對 (eval 成員, test 成員) 載入兩張 run 卡並遞迴套本節（成員本身是融合 run 就再進 `fusion`）；不等 → FAIL `reason=identity field=<method|params|members|weight> index=<i>`。test 側再驗 `fuse.json.subsets[test_subset].output_sha256` = `test_run.predictions[test_subset].sha256`，否則 `IntegrityError`。
4. **kernel**：`--weights RUN[:sha]`（可重複，預設 `[eval_run]`）：每個 RUN 是 eval 側 run，`source.weights_hash` 必須非空（否則 FAIL `field=weights_hash run=`）；給了 `:sha` 必須相等。記成 `artifact.weights`。
5. **不得訓練在 holdout 上**：`kind=candidate` 且 `sealed_subset ∈ eval_run.trained_on` → FAIL `reason=trained_on_sealed`（永遠拿不到乾淨 sealed 讀數，不可能成為最終）。融合 run 的 `trained_on` 已是聯集。
6. `pairing.checks` 記每條通過的檢查（人可讀），`pairing.members` 記配對表。

## 8. 準入門

讀 `measure/<eval_dataset>/judgements.jsonl`（每個 `prereg_id` 取最新一列）與 `readings.jsonl`。

- **candidate**：存在判決 `candidate_run = eval_run` 且 `verdict = PASS`，否則 FAIL `reason=not_admitted`。`eval_run` 是融合 run 時再要求：對 `fuse.json.members` 的每個直接成員 M，存在判決其預登記的 `component = M.run`、`candidate_run = eval_run`、最新 `verdict = PASS`（就是 `vcp fuse ablate --preregister` 產的 `<recipe>-admit-<M>` 經 `judge` 的結果），缺 → FAIL `reason=member_not_admitted member=M`。巢狀融合的成員視為一個組件，其內部成員不再往下查。
- **判決不能過期**：所採用的每份判決，其 `reading_ids` 指到的每筆讀數，`prediction_sha` 必須等於該讀數 `run_id` 現在 `predictions[subset].sha256`；不等 → FAIL `reason=stale_judgement prereg=<id>`。`ingest --replace` 過的 run 舊判決一律作廢。
- **baseline**：不需判決；`--reason` 必填；可進決選；`gate.admission = "waived"`。
- **probe**：`--reason` 必填；永不進決選；`gate.admission = "waived"`。
- `gate.judgements` 記採用的預登記 id（candidate 至少一個；融合 run 為每個成員一個）。

## 9. 輸出格式登記表（`src/vcp/submit/writers/`）

### 9.1 介面（`writers/base.py`）

```python
@dataclass(frozen=True)
class WriteContext:
    dataset: Dataset  # test dataset
    samples: list[Sample]  # test_subset 的樣本
    options: dict[str, str]  # writer_opts 與 --writer-opt 合併（命令列優先）
    out: Path  # 要寫的檔案路徑


@dataclass(frozen=True)
class WriteResult:
    rows: int  # 寫出的列 / 條目數
    samples: int  # 有輸出的樣本數
    missing: list[str]  # 沒有輸出的樣本 id（mapping 類且未 allow_missing 時 stage 據此 FAIL）
    fields: dict[str, FieldValue]


class Writer(Protocol):
    name: str
    version: str
    payloads: frozenset[str]  # ⊆ {"boxes", "masks", "scores", "targets"}
    file_name: str  # 輸出檔名（submission.csv / results.json）

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult: ...
```

`WRITERS` 登記表：`register_writer(writer)`（重複 → `RegistryError`）、`get_writer(name)`（未知 → `RegistryError`，訊息列出已登記者）。任務適用性由 `payload_field(task) ∈ writer.payloads` 推，不寫死任務名。

### 9.2 通用規則

- **決定性**：同輸入同位元組。列依 `sample_id` 排序（同樣本內依預測檔順序）、浮點以 `repr` 寫、整數以整數寫、LF、utf-8 無 BOM、不寫時間或版本。`verify` 的位元級重現建立在這條上。
- **id 對映** `id_field`：`sample_id`（預設）| `meta.<key>`（`Sample.meta[key]`，缺 → FAIL）| `view_path`（`views[0].path`）| `view_stem`（`views[0].path` 的 stem）。對映後 id 重複 → FAIL `reason=duplicate_id`。
- **完整性**：mapping 類（scores / targets）每個樣本必須一列，缺 → `missing`；`allow_missing=true` 才放行（WARN `missing=`）。det / seg 沒框的樣本本來就沒列，不算缺。
- 未知選項鍵 → FAIL `option=`。

### 9.3 `scores_csv`（payloads `{scores, targets}`，version `1`，`submission.csv`）

`id_col`（預設 `id`）、`columns`（`名稱=欄名,…` 改欄名）。值欄 = card 的類別名依 card 順序；card 無類別（regression）時取所有預測的 `targets` 鍵聯集排序。缺鍵 → FAIL `reason=missing_key sample=`。量測層 `scores_csv` 轉換器讀回本 writer 的輸出必須得到同一組預測（往返測試）。

### 9.4 `coco_results`（payloads `{boxes, masks}`，version `1`，`results.json`）

`[{"image_id", "category_id", "bbox": [x, y, w, h], "score"}]`；masks 為 `{"image_id", "category_id", "segmentation", "score"}`，RLE → `{"size": [h, w], "counts": rle}`（需 view 的 width / height，缺 → FAIL）、polygon → `[[…]]`。`image_id` = 對映後的 id，純十進位字串轉 int，否則保留字串。`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` + LF。

### 9.5 `csv_boxes`（payloads `{boxes}`，version `1`，`submission.csv`）

一框一列。`columns` 預設 `image=image_filename,label=label_id,x=x,y=y,w=w,h=h,score=score`（與匯入器 `csv_boxes` 的 `DEFAULT_COLUMNS` 對稱，多一個 `score`）；`box_format` `xywh`（預設）| `xyxy` | `cxcywh`；`coords` `abs`（預設）| `norm`（需 view width / height）；`label` `category_id`（預設）| `category_name`。表頭為 `columns` 的值依 image, label, x, y, w, h, score 順序。

## 10. 平台登記表與 Kaggle 適配器（`src/vcp/submit/platforms/`）

### 10.1 介面

```python
@dataclass(frozen=True)
class UploadResult:
    confirmed: bool
    platform_ref: str | None
    detail: str  # 已 redact 的一行摘要


@dataclass(frozen=True)
class PlatformSubmission:
    platform_ref: str
    file_name: str
    at: str  # UTC stamp
    description: str
    public: float | None
    private: float | None
    status: str
    submitted_by: str | None


class Platform(Protocol):
    name: str

    def upload(
        self, staged: Staged, message: str, profile: PlatformProfile, runner: Runner
    ) -> UploadResult: ...
    def list_submissions(
        self, profile: PlatformProfile, runner: Runner
    ) -> list[PlatformSubmission]: ...
```

`Runner` 與訓練層 rclone 的相同形狀：`run(args: list[str]) -> (code, stdout, stderr)`，預設 `subprocess.run`（繼承環境、不記錄環境、捕捉輸出）；測試注入假 runner，永不碰真 CLI。`manual` 的兩個方法都拋 `ValidationFailed reason=manual_platform`。

### 10.2 `kaggle`

- 執行檔：`profile.kaggle_command` 的第一個元素經 `shutil.which` 找不到 → `VcpError` ABORT `reason=kaggle_not_found`。
- `upload`：file 類 `… competitions submit -f <輸出檔絕對路徑> -m "<message>" -q <competition>`；kernel 類 `… competitions submit -k <kernel> -v <version> -f <output> -m "<message>" -q <competition>`。stdout 含 `Successfully submitted` → `confirmed=True`。
- `list_submissions`：`… competitions submissions --format json --page-size 200 <competition>`，回應含下一頁 token 就帶 `--page-token` 續讀到底。讀 `fileName`、`date`、`description`、`status`、`publicScore`、`privateScore`，`ref` 有就用、沒有以 `sha256_text(fileName|date)` 當 `platform_ref`；缺必要鍵 → FAIL `reason=platform_response key=`；`date` 依 ISO 8601 解析，無時區即視為 UTC；分數字串空白或 `null` → `None`。
- 平台回應的原文只在記憶體裡解析；任何要進 VERDICT、`logs/`、`--json`、台帳的字串都先過 §11 的 redact。

## 11. API 隱私硬規則

1. vcp **不讀、不寫、不驗、不記錄**任何憑證：不碰 `~/.kaggle/`、沒有 `--token` / `--key` 類選項、設定檔沒有憑證欄位（`extra="forbid"`）。憑證只存在於 kaggle CLI 自己的設定與使用者的環境變數。
2. 子程序**繼承環境但不記錄環境**：本層沒有環境快照；`Runner` 不接受 `env` 參數。
3. **redact 後才落地**：`redact(text)` 對 `(?i)(key|token|secret|password|authorization)\s*[=:]\s*\S+` → `\1=<redacted>`、`(?i)bearer\s+\S+` → `bearer <redacted>`、長度 ≥ 32 的 `[A-Za-z0-9+/_-]` 連續串 → `<redacted>`。適用對象是第三方 CLI 的 stdout / stderr 與其衍生訊息；vcp 自己的欄位（sha、id）不經此函式。
4. 台帳、`stage.json`、`--json` 輸出只含：submission id、檔名、sha、時間、分數、平台 ref、平台狀態、`submitted_by`；不含 URL 帶 token、不含帳號 key。
5. **測試**：假 runner 的環境放 `KAGGLE_KEY=fakesecretfakesecretfakesecret1234`，並讓假 CLI 在 stdout / stderr 回吐它；跑完 `upload`（成功、非 0、無法確認）與 `sync` 後掃描 data root、configs root、`logs/`、捕捉的 stdout / stderr，該字串零出現。

## 12. 錯誤處理與 VERDICT 字彙

| 情境 | 類型 / `reason=` | 狀態 |
|---|---|---|
| 設定檔缺、壞、含未知鍵；時區名未知；`--at` 格式錯 | `ValidationFailed` | FAIL |
| 配對核對不過 | `ValidationFailed` `identity` + `field=` `side=` / `index=` | FAIL |
| run 的 dataset / plan 與設定不符 | `PlanMismatchError` + `side=` | ABORT |
| 準入門 | `not_admitted` / `member_not_admitted member=` / `stale_judgement prereg=` / `trained_on_sealed` / `reason_required` | FAIL |
| 配額 / 封槍 / 截止 / 平台 | `quota_exhausted resets_at= local=` / `locked since= why=` / `past_deadline` / `manual_platform` | FAIL |
| id 已存在；writer 選項未知；對映 id 重複；缺樣本 | `exists` / `option=` / `duplicate_id` / `missing=` | FAIL |
| 輸出檔 sha 不符（upload / record / verify）；融合 output sha 不符 | `IntegrityError` | FAIL |
| kaggle 找不到 | `VcpError` `kaggle_not_found` | ABORT |
| kaggle CLI 非 0 | `VcpError`（redact 後的最後一行） | FAIL |
| 平台回應缺鍵 | `ValidationFailed` `platform_response key=` | FAIL |
| 台帳列壞 | `ValidationFailed` 帶 `file:line` | FAIL |
| 未知 writer / platform | `RegistryError` | ABORT |

共用欄位：`dataset=` `id=` `sha256=<前 12>` `quota=used/per_day` `resets_at=` `local=` `locked=` `deadline_in=` `foreign=` `unconfirmed=` `unranked=` `final=` `confirmed=` `missing=`。

## 13. 與其他子專案的介面

- **資料層**：`Dataset.load`、`SplitPlan` / `save_plan` / `load_plan`（init 直接組單子集 plan）、`DatasetPaths.unseal_jsonl`（數 holdout 使用次數）、`Sample.views[].width/height`（`coords=norm` 與 RLE）；新增 `DatasetPaths` 的四個路徑屬性。
- **量測層**：`load_run` / `assert_run_matches` / `verify_prediction`、`read_predictions`、`ReadingsLedger` / `read_rows(Judgement)`、`params_key`、`get_metric(...).higher_is_better`、`load_plugins`；零改動。
- **融合層**：`fuse.build.load_record`（`fuse.json`）；零改動。決選規則、配對核對即融合層 §10 留給本層的規則。
- **訓練層**：`RunCard.source.weights_hash`（kernel 類的權重身分）；`train.yaml.uploads` 不在本層讀（副本稽核屬子專案 6）。
- **備份審計（6）**：`submissions.jsonl`、`submit.yaml`（git）與 `submit/<test>/`（data root）是它的稽核標的。
- **文件**：README 新增「提交治理命令 `vcp submit`」一節（命令表、一次提交的順序、決選）；CLAUDE.md 路徑加 `configs/datasets/<name>/submit.yaml` + `submissions.jsonl` + `submit/<name>/`，常用命令加 `stage` / `upload` / `final`，變異軸清單補上輸出格式與平台（十二個）。
- **依賴**：`tzdata>=2024.1` 進 `dependencies`。

## 14. 測試策略

- **單元**：schema（`extra="forbid"`、憑證鍵拒收、時區驗證、`deadline` 格式、事件欄位）；配對核對每條規則（single 缺 hash / 不等 / config 一側空；fusion method / params / 成員數 / 權重 / 巢狀遞迴；只有一側有 `fuse.json`；kernel 權重 sha）；準入門（四種 kind、`member_not_admitted`、`stale_judgement`、`trained_on_sealed`、`reason_required`）；配額視窗（假時鐘：視窗邊界、`day_start` 非午夜、`America/New_York` 的 DST 交界、`record --at` 的 overflow WARN）；lock / unlock / deadline；三個 writer 的決定性（兩次同 bytes）、完整性、四種 `id_field`、`scores_csv` 與量測層轉換器的往返、`csv_boxes` 三種 `box_format` × 兩種 `coords`；Kaggle 假 runner（upload 成功 / 非 0 / 無法確認、kernel 類命令列、sync 三種配對與 foreign 去重、翻頁、缺鍵）；`redact`；§11 第 5 條的隱私掃描；`final` 的排序鍵與 `stale_reading` / `partial_reading`；`report` 的 last-vs-last。
- **CLI**：每個命令的 VERDICT 欄位與 exit code；`--json` 的 stdout / stderr 分離；失敗路徑零副作用（目錄與台帳都沒動）。
- **端到端** `tests/unit/test_e2e_submit.py`：合成 eval dataset（含 sealed）+ test dataset → 兩個模型各 ingest eval 與 test 預測 → prereg + judge PASS → `stage` candidate / probe / baseline → `record` → `score` → `report` last-vs-last → `measure --unseal` → `final` 選出並 lock → lock 後 `stage` FAIL；同一劇本用 Kaggle 假 runner 走 `upload` / `sync`。
- **真資料** `tests/integration/test_rsna_knee_submit.py`：以 RSNA 的 example test study 為 test dataset 產 `scores_csv`（`id_field=view_stem`），無資料即 skip。

## 15. 驗收條件

1. 端到端劇本 exit 0，每個命令都有 VERDICT。
2. j7cas 情境：judge FAIL 的候選以 `candidate` stage → FAIL；同一候選標 `probe --reason` 可 stage 與上傳，`final` 的 `table` 記 `eligible=false why=probe`。
3. 配額：同視窗第 `per_day+1` 發 `upload` FAIL `quota_exhausted`；跨 `day_start` 後 OK；`foreign` 計入。
4. 台帳：寫入命令失敗 → 零新列與零新目錄；成功 → 恰一列（`upload` 在 CLI 回 0 後一定有列）。
5. 隱私掃描零命中。
6. 決定性：同一對 run 以不同 id stage 兩次 → 同 sha；`verify` OK；改動輸出檔一個位元組 → `verify` FAIL。
7. `final` 選出 sealed 讀數最高者而非 public 最高者（測試裡兩者刻意不同）。
8. 覆蓋率 ≥ 80%、ruff `check` 與 `format --check` 乾淨。

## 16. 不在範圍

無 API 平台的自動上傳與瀏覽器自動化；非 Kaggle 的榜面抓取；隊友通知與回執（人做，台帳有 `lock` 列可 grep）；public→private 位移自動回寫 σ_p 台帳（`report` 給位移，人用 `vcp eval sigma --method prior` 記）；AWS 重現包與 README（子專案 6 / `projects/`）；Kaggle dataset（權重）上傳與 notebook 產生；輸出檔壓縮；多個決選規則的登記表（v1 只有 `best_sealed`）；同一個 test dataset 對多個平台。以上皆為已預留的擴充點，不是設計缺口。

## 17. v2 補充決定（Plan 6 實作與審查的定案，2026-09-06）

以下為實作期間由計畫或審查裁決、原 spec 未明說或已被推翻的規則，與前文衝突時以本節為準。

1. **量測層改一處**（§13「零改動」作廢）：`vcp eval ingest --weights PATH [--config PATH]` 把檔案 sha256 寫進 `run.yaml` 的 `source.weights_hash` / `config_hash`；既有 run 的空值可補、非空值不同即 FAIL；缺檔在任何寫入前 FAIL。沒有這條，只經 ingest 建立的 test 側 run 沒有權重身分，§7 的配對無從做起。
2. **`scores_csv` 要求 card 有類別**（§9.3「card 無類別時取 targets 鍵聯集」作廢）：沒有類別即 FAIL `fields={"dataset"}`——聯集寫出的欄位量測層轉換器會當 `extra` 拒收，同節的往返承諾優先。
3. **`coco_results` 只把標準十進位 id 轉成 int**（`value.isdigit() and str(int(value)) == value`）；"07" 保持字串，不會與 7 撞號。
4. **`local=` 的時區**（§6.5 修正）：配額 VERDICT 的 `local=` 以 `display_tz` → `quota.day_tz` → UTC 的順序取（`PlatformProfile.effective_display_tz()`），`QuotaState` 自帶該時區；`resets_at` 仍是 UTC。
5. **probe 跳過身分核對**：`kind=probe` 的 file 類候選記 `pairing.checks=["identity=skipped"]`（mode 依 test 側有無 `fuse.json`）；kernel 類一律核對宣告的權重 sha。probe 永不進決選。
6. **封槍後的最終發**：lock 後 `stage` 一律 FAIL；`upload` / `record` 只放行最新 `final` 列 `chosen` 裡的 id（`board_rule=last` 時選中者要重傳成最後一發）；`score` / `sync` / `status` / `report` 不受 lock 影響。
7. **`record --at` 的容差**：不得早於 `staged_at` 截到秒、不得晚於 `utc_now() + 60 s`；配額用 `--at` 所在視窗，已滿只 WARN `quota_overflow`（動作已發生）。
8. **平台錯誤**：CLI 非 0 是 `PlatformError`（`VcpError` 子類，status FAIL），訊息為 redact 後的最後一行非空白（stderr 以 `.strip()` 判空才退到 stdout）；`kaggle_not_found` 仍是 `VcpError`（ABORT），且只在沒有注入 runner 時檢查執行檔。`parse_score` / `parse_date` 的錯誤訊息也經 redact；非有限的平台分數在 `sync` 是 `platform_response` FAIL，不是 ABORT。
9. **test plan 帶一個空的 `train` 子集**：`SplitPlan` 要求恰一個 role=train，`init` 建的 `all-v1` 是 `train:train:0.0` + `<test_subset>:eval:1.0`、`params.eval_gold_only=false`，直接組 `SplitPlan` 不經產生器。
10. **sync 的配對與順序**：規則 0 = 平台列的 `platform_ref` 等於某 `uploaded` 列的 `platform_ref`（`record --platform-ref`）；規則 1 = description 含 id（字界比對，長 id 優先，不受本次已配對的 id 限制——同一 id 重傳本來就會提及兩次）；規則 2 = 檔名相同且與某 `uploaded` 的 `at` 相差 ≤ 10 分（跳過本次已配走的 id）。平台列依 `at` 升冪處理，`scored` 列因此依時間追加、最新分數是 latest。缺 `stage.json` 的 id 只是少了規則 2，不讓整個 sync 失敗。
11. **台帳列以 `exclude_none` 寫出**：一列只帶自己事件的欄位；因此持久化模型的可空欄位一律預設 `None`（`FinalEntry` 補上）；`stage.json` 整份 dump（含 null）。
12. **`final --slots`**：在函式層檢查 `slots ≥ 1`（FAIL），不用 Click 的 `min=`——每個命令都要以 VERDICT 收尾（鐵則 2）。`--slots 0` 不再等於預設。
13. **決選次序鍵**：sealed 讀數依指標 `higher_is_better` 取向；public 恆為降冪（§6.4 原意）；再以 `staged_at` 升冪。
14. **`submission_id` 至多 31 字元**：redact 會把 ≥ 32 字元的英數串遮掉，sync 靠 description 裡的 id 配對。
15. **報告輸出的私密性**：`upload` 的 `detail`、`sync` 的 `description` 經 redact；`fileName` / `status` / `submittedBy` 目前照原文進台帳（§10.2 與 §11.4 的矛盾留待辦，補 redact 為宜）。→ Plan 6c 已補：三個欄位進台帳前都 redact（32+ 字元的檔名會被遮、sync 配不到；本 repo 的候選檔名 `submission.csv` 不受影響）。
16. **重傳 id 的逐發分數（Plan 6c）**：`sync` 寫的 `scored` 列記平台時間 `at` 與 `platform_ref`；`report` / `status` 對每一發 `uploaded` 列配自己的分數——平台時間的分數配給 `at` 最近的那一發（同距離取 `at` 較晚者），手動 `score` 配給 `ts` 不晚於它的最近一發，每發取最新的一筆；`status` 的 `unscored` 看 `at` 最新的那一發；`final` 仍用最新的分數。
17. **stage 的 VERDICT（Plan 6c）**：file 類帶 `missing=`；配對有 `config_hash=` 檢查時帶 `config_hash=<12 hex 或 unchecked>`（§9.2 原意）。
18. **FAIL 而非 ABORT（Plan 6c）**：sealed 子集不在 plan 裡 → `sealed_subset:`；`--test-subset train` 撞到空 train 子集 → `test_subset …`；file 類 profile 給 kernel 選項 → `kernel_options:`；kernel 類給 `--test-run` → `test_run:`。
19. **決選次序鍵改寫（取代第 13 條，Plan 6c）**：sealed 讀數與 public 同分鍵都依指標的 `higher_is_better`（榜面分數是同一指標，lower-is-better 比賽越低越好）；`public` 缺席一律排最後；再以 `stage.json` 的 `staged_at` 升冪（原本用台帳列的 `ts`，差毫秒）。
20. **台帳列的時戳（Plan 6c）**：`ts` 與 `at` 必須是 `stamp()` 格式的 UTC 字串（讀入即驗）。
