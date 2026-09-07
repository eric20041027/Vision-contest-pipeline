# Plan 3 後記：裁決、審查發現與待辦（量測層）

- 日期：2026-09-05
- 計畫：`docs/superpowers/plans/2026-09-04-vcp-plan3-measurement-layer.md`（12 任務）
- spec：`docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md`（v1 + §15 補充決定）
- 分支：`feat/plan3-measurement-layer`（自 main 的 `29d401a` 分出）
- 執行方式：Subagent-Driven Development。預檢以 Workflow 並行 12 個讀者 + 3 個掃描面向；Task 1–3 的審查以 Workflow 多面向；Task 4 起改回單一審查者；每個任務一位實作者、一位審查者、修正輪後範圍限定再審；最終全分支審查一位（opus），唯一修正輪，再審後合併。

## 1. 結果摘要

| 項目 | 值 |
|---|---|
| commit | 41 個（含預檢後 12 任務、各任務修正輪、最終修正輪與殘餘） |
| 測試 | 505 passed、3 skipped（海廢資料集未匯入） |
| 覆蓋率 | 96.45%（門檻 80%） |
| ruff | `check` 與 `format --check` 皆乾淨；整套測試在 RuntimeWarning / UserWarning / DeprecationWarning 皆為錯誤下無警告 |
| 真資料 | `VCP_REALDATA_ROOT=C:/vcp-data uv run pytest tests/integration -m realdata` → 3 passed（RSNA）；最終審查另在 rsna-knee 的副本上以 CLI 跑完八個命令，全流程無需改碼 |
| 檔案 | `src/vcp/cli_eval.py` 約 550 行，`measure/` 各模組皆 < 300 行 |

交付：`src/vcp/measure/`（schema、predictions、runs、ingest、converters ×4、metrics ×9、masks、ledger、anchors、measure、stats、sigma、prereg、judge、report、plugins）；`vcp eval` 八個命令；`cli_common.py` 拆分；`TaskSpec.pred_payload`、`DatasetPaths` 量測路徑、`vcp.core.paths.runs_root / run_path`、YOLO manifest `images`、`VcpError.fields`；端到端測試、RSNA 真資料測試、README / CLAUDE.md / spec §15。

## 2. 預檢裁決（執行前）

12 個讀者回報 187 個簡報內部問題（52 個為 lint 類），3 個掃描面向找出 12 個 blocking。通則：A lint 由實作者自行處理；B 測試與程式碼矛盾時程式碼勝（除非斷言承載 spec 要求）；F 跨測試模組 import 一律經 `helpers`；G 使用者可達的失敗一律是帶位置的 `ValidationFailed`。四個會直接讓執行失敗的：`clean_eval_subsets` 回傳 `holdout(sealed)`（Task 9 需去標記）；共用夾具的 import 路徑在 pytest 規則下不存在（改放 `tests/helpers.py`）；Task 7 沒更新 Task 6 的斷言；Task 1 的「修改 `cli.py` 第 1–122 行」會砍掉 Typer app。駁回的誤報：pycocotools 在 extra 與 dev 群組各寫一次是 Plan 2c 守門測試強制的。

範圍裁決：`PAYLOAD_FOR_TASK` 改為 `TaskSpec.pred_payload`（新增任務只改一處）；`PredMask` 加 `view`，`path` 型遮罩與 `view != 0` 都是定位錯誤而非靜默錯算。

## 3. 各任務審查的重要發現與處置

| 任務 | 發現 | 處置 |
|---|---|---|
| 1 | 匯出 manifest `images` 的斷言恆真（夾具路徑本來就扁平），鍵改成 view.path 也能過 | 補巢狀路徑測試 |
| 2 | `CLS_LIKE` 是第二份寫死的任務清單；三個夾具零覆蓋；history 時戳未釘 | 改由 payload 形態推導；補 `test_helpers_fixtures.py` |
| 3 | 截斷 CSV 列以裸 `TypeError` 逃出；pydantic 包裝可達卻未測；轉換器又寫死任務清單 | 全修；補 Task 4 裁決 0 |
| 4 | `_mask` 漏裸例外；seg 分支零覆蓋；匯出清單讀取漏裸 `KeyError`；`_almost` 不比 y/h；yolo_txt 對多 view 錯算 | 修 + 多 view 改為拒絕 |
| 5 | `--export-manifest` 被靜默丟棄；`--plugin` 接線未測 | 修；後於 Task 12 改為每子集記 sha |
| 6 | 空子集讓 rmse/mae 回靜默 nan（寫入後整份台帳不可讀）；gold 缺欄位裸 `KeyError`；指標無方向宣告 | `require_nonempty`；`higher_is_better` |
| 7 | `iscrowd` 寫死 0；多 view 框摺進 view 0；全空預測時無 gold 類別回 0.0；決定性測試無法失敗 | 全修；輪二排除 crowd |
| 8 | Pillow 與 pycocotools 兩種柵格化慣例不相容，同區域完美預測 dice 0.71 | 多邊形改走 pycocotools；定向過濾其過時警告 |
| 9 | `--tolerance nan` 讓護欄形同虛設並蓋章 ok；壞 `anchors.json` 漏裸 pydantic 錯誤 | 修；log 先寫再原子替換 |
| 10 | 負 seed 裸 numpy 錯誤；`--subsets valA,valA` 寫入 σ_p=0；spec 的錨點預設被計畫漏掉 | 全修 |
| 11 | 判決台帳無有限值守門（外掛 nan → ±T_CAP 偽造信心）；sealed 子集無法判決 | 守門；`judge --unseal --reason` |
| 12 | 簡報的端到端在 Task 5 守門下跑不起來 | `export_manifest_sha` 改記每子集 |

## 4. 最終全分支審查

六個面向通讀、spec §13 逐項對證、真資料 CLI 全流程。無 Critical。七個 Important 全部在唯一修正輪解決：`report` 每個主張只留最新判決且篩選到兩半；`status`/`report` 拒收不存在的資料集；壞 run 卡改計數（`runs=N unreadable=M`）；三處與程式碼不符的文件；`macro_f1` 對子集缺席類別改記 `None`（與其他四個巨集指標一致）；σ_p 方法軸補公開登記 API；§13.1 / 13.2 的證據與判決列 `delta == sign × (candidate − baseline)` 的接縫斷言。附帶：ingest VERDICT `samples=` 改 `in_subset=`；判決列記 `metric_version`。再審全數通過。

裁決紀錄：`macro_f1` 改語意但維持版本 1（合併前無任何已寫入讀數；spec §15-11）；`report.py` 不呼叫 `judge_prereg`（唯讀）。

## 5. 待辦（依優先序）

1. **`build_plan` 應拒絕零樣本的 eval 子集**（資料層）：指標層的空子集守門讓它變成大聲的失敗，但切分時就是使用者錯誤。
2. **YOLO 匯出 manifest 記錄匯出的 view 索引**（資料層，一行：`select_view` 已回傳 `vi`）：可同時解除 `yolo_txt` 對多 view 的拒絕，與 det / seg 指標「只算 view 0」限制的一半。
3. **bootstrap 在負樣本多的子集不可用**（Task 10 裁決 0 的刻意結果）：任一重抽命中指標守門即整個估計中止，使用者只能換 seed 或子集；小型 eval/holdout 子集（n=60 時 (3/6)^6 ≈ 1.6%/次）會撞到。可考慮讓 `coco_map` 對零 gold 的重抽回 `None` 並由 bootstrap 記錄跳過數。
4. **效能**：seg 每樣本配置 2×類別數個全圖陣列，兩個指標各跑一遍，200 次重抽在 512² / 10 類為 15 s、1024² 為 72 s；護欄對每個 (子集, 指標) 重新載入錨點 run、重算 sha、重算指標；`judge` 載入資料集兩次。
5. **VERDICT 欄位命名**：`sigma` 的 `cached=` 是 bool 而 `measure` 是 int（改 `existing=`）；`report` 的 `judgements=` 計判決×子集列而 `status` 的 `judged=` 計主張（改 `deltas=`）；`anchor` 缺 `dataset=`；`sigma[<metric>/<method>]=` 是唯一帶括號的欄位名且 `Verdict.line()` 不轉義欄位名（外掛指標名含空白或 `=` 會產生無法解析的行——在 `register_metric` 驗證名稱）。
6. **常數重複**：`"readings.jsonl"` 在五處宣告、兩處裸字串；`_TRUE` 四處；component-class 集合三處。
7. **`Converter.version` 無處記錄**（`PredictionFile.format_in` 只記名稱）：加 `format_version`。
8. **σ_p bootstrap 估計未記預測 sha**：之後的 `ingest --replace` 會讓估計不可驗證。
9. **`create_prereg` 先寫 yaml 再寫 log**：中間失敗留下無法判決也無法重登記的 id。
10. **負門檻未擋**：`min_bases 0`、`sigma_ratio -1` 讓門檻形同虛設（可見於已提交的 yaml，比 nan 輕）。
11. **`report --plan` 會隱藏 `missing_readings` 的判決**（無讀數即無 plan）。
12. **跨程序**：兩個程序可 append 同一 `reading_id`；`anchors.json.tmp` 固定檔名；`set_anchor` 是未上鎖的讀改寫。
13. **coords 的 view 索引未做範圍檢查**（Plan 2c 留下，`tasks.py`）。
14. **測試**：`test_cls_metrics_match_sklearn_directly` 未斷言「三類都在 gold 裡」的前提；`ingest_perfect` 的 `preds[:len-drop]` 在 drop 過大時繞回而非清空；`test_png_resize_then_skip_then_force` 在 `-W error` 下因未關閉檔案失敗（Plan 2c，另開）。
15. **`_metric_version` 的 `--method` 說明字串仍寫死三個內建名**（外掛方法不會出現在 help）。

## 6. 方法上的紀錄

- 預檢並行讀者 + 三面向掃描找出 12 個 blocking，其中四個會讓執行整個失敗；掃描也駁回了讀者的誤報。這一步的成本（15 個代理）換掉的是至少三次任務失敗與重派。
- 「寫死的任務清單」在三個任務連續出現（`PAYLOAD_FOR_TASK`、`CLS_LIKE`、scores_csv 的 tuple），第三次才補成明文裁決；同類模式第二次出現就該寫進裁決檔。
- 每個任務的審查者用真實執行對抗探測（突變、scratch data root、pycocotools 自己的 encode 當外部 oracle），找到的多是單看 diff 看不出的問題：nan 寫入台帳、兩種柵格化慣例、`--tolerance nan`。
- 兩個修正輪各自引入新問題（Task 7 的 crowd、Task 8 的 546 個警告），都是再審抓到的；**修正後的再審不是形式**。
- 控制者自己下錯的裁決有三個（Task 5 的 `samples == len(val)`、Task 5 的 F1b、Task 12 的 `report.py` 該呼叫 judge），都由實作者或審查者以證據推翻；裁決要能被下游推翻，且推翻要記進台帳。

## 7. Hygiene A：§5 待辦的處置（2026-09-07，分支 `worktree-hygiene-a-data-measure`，3 個 commit + 1 個控制者微修）

| §5 項 | 處置 |
|---|---|
| 1 | 做了：`build_plan` 拒絕零樣本的 eval / sealed 子集（`empty_subset:`）；空 train 仍合法、`vcp data split` 只對它 WARN（提交層的 test plan 直接建 `SplitPlan`，不受影響）。 |
| 2 | 做了 manifest 的部分：YOLO 匯出 manifest 的 `images` 列改為 `{"sample_id", "view"}`，`yolo_txt` 兩種形狀都讀；多 view 的拒絕與 det 指標只認 view 0 維持（spec §15-7 加註）。 |
| 3 | 做了：`bootstrap_sd` 對指標守門拒答的重抽記 `skipped` 並繼續；`skipped > used` 或可用重抽少於 2 → `too_many_skipped:`（微修補後者，否則單一可用值會回 nan）；估計的 `inputs` 記 `resamples / used / skipped`；`vcp eval sigma` 印 `skipped=` 並 WARN；`paired_bootstrap`（judge 用）不變。 |
| 4 | 不做（效能，另開回合）。 |
| 5 | 做了：`sigma` 的 `cached=` → `existing=`（整數）、`report` 的 `judgements=` → `deltas=`、`anchor` 加 `dataset=`；`register_metric` / `register_sigma_method` 拒絕不合 `^[A-Za-z0-9][A-Za-z0-9._-]*$` 的名字；`sigma[<metric>/<method>]=` 保留（名字驗過就可解析）。 |
| 6 | 做了：三個台帳檔名只在 `measure/ledger.py` 宣告；`TRUE_VALUES` / `is_true` 在 `core/config.py`（提交層的兩參數版改名 `option_is_true`）；component-class 常數收進 `measure/schema.py`。 |
| 7 | 做了：`PredictionFile.format_version`，ingest 記轉換器版本；融合寫的條目為 None。 |
| 8 | 做了：bootstrap 估計的 `inputs` 記 `prediction_sha`；`estimate_id` 由 inputs 導出，`ingest --replace` 後自然重算（舊列留著）。 |
| 9 | 做了：`create_prereg` 的 log 寫入失敗就刪掉剛寫的 yaml。 |
| 10 | 做了：`t_min >= 0`、`min_bases >= 1`、`sigma_ratio > 0`。 |
| 11 | 做了：`report --plan` 對沒有讀數的判決改由其 baseline / candidate run 的卡推 plan；`last_vs_last` 對沒有任何子集結果的判決多出一列空值列（否則怎麼過濾都看不見）；`deltas=` 的語意寫進 spec §16。 |
| 12 | 做了暫存檔名（`NamedTemporaryFile(prefix="anchors.")` + `os.replace`）；鎖不做。 |
| 13 | 做了：`tasks.py` 對 boxes 與 masks 兩個欄位都檢查 view 索引，越界是定位的 `ValidationFailed`（同 Plan 2c §5-1）。 |
| 14 | 做了三項測試修補（sklearn 前提、`ingest_perfect` 的 drop 夾住、PNG 測試關檔——漏在測試不在程式）。 |
| 15 | 做了：`--method` 說明改「registered sigma_p method」。 |

審查（opus）：SPEC ✅、APPROVED；1 MEDIUM（可用重抽只剩 1 次回 nan → 微修）、3 LOW（spec §15-7 與兩則註解過時 → 一併改；`BootstrapSd.resamples` 只有測試讀）。全套 881 passed / 4 skipped、覆蓋率 96.40%、真資料整合 8 passed。

## 8. Hygiene C 接縫處置（2026-09-07）

ingest 權重衝突訊息補 omit --weights 提示；由提交層 C2 帶入並補紅→綠測試，身分驗證規則與既有卡相容性不變。決定 — 提示只加在 weights_hash 衝突；依據 — config_hash 並非 weights 選項；代價 — 不替使用者改寫既有 run 身分。

## 9. CLI 識別欄位與剩餘待辦處置（2026-09-07）

八個 eval 命令均傳 run_command(context=)，保留可得的 dataset / run / plan / subset / prereg / candidate / baseline / metric。可選值未給時省略（FieldValue 不接受 None），錯誤本身的 fields 優先。新 CLI 測試檢查早期失敗、exit 1、文字末行與 JSON stdout / VERDICT stderr。

§5-4 效能明確延後；§5-12 跨程序 append 與 anchors 交易鎖延後，需一併設計讀改寫與崩潰恢复，不能以一把局部鎖冒充完整交易。現有唯一暫存檔保留，使用者流程依序執行。其餘 §7 已完成項不重開。
