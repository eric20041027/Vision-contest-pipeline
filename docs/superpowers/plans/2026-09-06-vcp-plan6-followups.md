# Plan 6 後記：裁決、審查發現與待辦（提交治理層）

- 日期：2026-09-06
- 計畫：`docs/superpowers/plans/2026-09-06-vcp-plan6-submission-governance.md`（12 任務）
- spec：`docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`（v1 + §17 補充決定）
- 分支：`worktree-plan6-submit`（自 main 的 `50badc5` 分出；`.claude/worktrees/plan6-submit`）
- 執行方式：Subagent-Driven Development。預檢由控制者人工掃描（24 列接縫表，1 個發現 → R0）；每個任務一位實作者（轉錄型 haiku：T1、T2、T5、T6、T8；整合型 sonnet：T3、T4、T7、T9、T10、T11、T12）、一位審查者（sonnet）；六個任務各一輪修正（T2、T5、T6、T8、T10、T12）；最終整支審查一位（opus，四個探針腳本）、唯一修正輪、範圍限定再審抓到自己引入的回歸後一個微修（R12）再審。T10 的第一位實作者被用量上限中斷，從同一 BASE 重派。

## 1. 結果摘要

| 項目 | 值 |
|---|---|
| commit | 21 個（12 任務 + 6 任務修正 + 1 計畫片段修正 + 最終修正輪 + R12 微修）+ 收尾文件 |
| 測試 | 755 passed、4 skipped（海廢資料集未匯入 ×3、ensemble-boxes 未安裝 ×1） |
| 覆蓋率 | 96.37%（門檻 80%） |
| ruff | `check` 與 `format --check` 皆乾淨 |
| 真資料 | `tests/integration/test_rsna_knee_submit.py` 1 passed（三個 RSNA study 複製到暫存 dataset，`scores_csv` 逐位元決定性） |
| 端到端 | 手動平台：init → stage（candidate / 被擋的 j7cas / probe / baseline / 身分不符）→ verify → record ×3（配額 3/3）→ score → report → final 失敗（無 sealed 讀數）→ `eval measure --unseal` ×2 → final 選 sealed 較高者而非 public 較高者 → 封槍後 stage FAIL、選中者可重傳、其他 FAIL。假 Kaggle CLI（真子程序）：失敗上傳不寫列 → 兩發成功 → 第三發 `quota_exhausted` → sync 補分數 + 一列 foreign → 再 sync 零新列 → final 選兩位 → 隱私掃描（data root 含 `logs/`、configs root、所有輸出）零命中 |

交付：`src/vcp/submit/`（schema、ledger、timewin、profile、guards、pairing、gate、`writers/` scores_csv / coco_results / csv_boxes、`platforms/` manual / kaggle、stage、actions、sync、final、report）；`vcp submit init|stage|verify|upload|record|score|sync|final|lock|unlock|status|report`（`cli_submit.py`）；`vcp eval ingest --weights / --config`；`PlatformError`；`DatasetPaths` 四個成員；`tzdata` 依賴；README「提交治理命令」一節；CLAUDE.md 變異軸、路徑、常用命令各一處。

## 2. 預檢裁決（執行前）

24 列接縫表（每對共用檔案 / 介面的任務一列、每個任務自洽一列），一個發現：計畫的 T10 / T11 CLI 把 `QuotaState.fields("UTC")` 寫死，`local=` 就不會是 spec §6 說的平台時區 → **R0** `QuotaState` 自帶 `tz_name`（`quota_state` 從 `profile.effective_display_tz()` 填）、`fields()` 無參數、`assert_quota(state)` 一參數。

## 3. 執行期裁決

- **R1（Task 3）** 對計畫 markdown 跑 `ruff format` 把獨立片段的 `= None,` 改成 tuple `(None,)` → 以 `= None` 為準，計畫與 brief 同步修正（commit fd6439b）。
- **R2（Task 5）** spec §9.3「card 無類別時取 targets 鍵聯集」寫出的欄位量測層轉換器會當 `extra` 拒收，與同節的往返承諾衝突 → 移除後備，`scores_csv` 要求 card 有類別否則 FAIL。
- **R3（Task 6）** `_image_id` 把所有數字字串轉 int，"07" 與 "7" 撞號 → 只轉標準十進位（`str(int(v)) == v`）。
- **R4（Task 7）** brief 的 stale 測試用「反轉再少一個樣本」改寫預測檔——預測檔寫入前排序、cls 子集缺樣本 ingest 直接失敗，sha 根本不會變 → 改用 `noisy_predictions(flip=1.0)`；T11 同型測試同修。
- **R5（Task 8）** `parse_score` / `parse_date` 的錯誤訊息帶原始平台值未 redact → 包 `redact(str(value))` + 回歸測試。
- **R6（Task 10）** brief 測試要求「S10 is not S1」不得配到 S1，實作者為此加了「旁邊有更長的相似 token 就不信」守衛 → 測試錯：句子裡的獨立 `S1` 就是提及；守衛還原，測試改為 `"S10 only"` → None。
- **R7（Task 10）** 平台列依平台順序處理，同一 id 重傳時舊分數可能最後寫入成為 latest → 依 `at` 升冪處理；提及規則不受 `taken` 限制（重傳本來就會提及同一 id 兩次）。
- **R8（Task 11）** `FinalEntry` 的可空欄位沒有 `= None` 預設，`exclude_none` 寫出的 final 列讀不回來 → 加預設（`stage.json` 不受影響：它不用 exclude_none）。
- **R9（Task 12）** brief 的身分失敗案例 `--eval-run good --test-run bad.mismatch` 兩側都是 good 的權重、身分會過 → 改 `--eval-run bad`。
- **R10（Task 12）** 端到端的 `_scan` 沒有斷言至少掃到一個檔案 → 計數並 `assert scanned > 0`。
- **R11（最終審查）** 一個修正輪 = Important 1、2 + minor 1、9、10 + 兩個 `\r` 斷言；Important 3（spec §17）由控制者在收尾文件寫；其餘 minor 留待辦。
- **R12（再審）** 修正輪自己引入：typer 的 `min=1` 讓 `--slots 0` 走 Click 的 usage error（exit 2、無 VERDICT）違反鐵則 2 → 破例再派一個微修（拿掉 `min=1`，函式層檢查已給 VERDICT FAIL）+ 一次再審。

## 4. 各任務審查發現與處置

| 任務 | 發現 | 處置 |
|---|---|---|
| 1 | Event / EVENTS / _REQUIRED 三處列舉；`last_uploaded()` 回 foreign 列未測；`ts` / `at` 無格式驗證 | 留待辦 |
| 2 | 沒作用的 `# noqa: DTZ007`（Important，計畫規定的清理步驟被跳過）；25 h DST 未測 | 修正輪；後者留待辦 |
| 3 | config 只與 weights 一起測；衝突訊息缺「omit --weights」提示 | 留待辦 |
| 4 | plan 重用失敗分支未測；`--test-subset train` 是 ABORT 非 FAIL | 留待辦 |
| 5 | 無類別後備不能往返（Important，計畫規定） | R2 |
| 6 | `_image_id` 撞號（Important，計畫規定）；view 越界未測；`columns` 重名表頭未擋 | R3；其餘留待辦 |
| 7 | 缺 run 目錄顯示 run not found 而非 stale_judgement；同 prereg 判兩次未測；巢狀融合未測 | 留待辦 |
| 8 | parse 錯誤訊息未 redact（Important，計畫規定）；`_failed` 以整串真值選串流；`ref=0` 未測 | R5；`_failed` 在最終修正輪處理 |
| 9 | `Staged(...)` 在清理守衛外；allow_missing WARN 與融合 verify 未測；`warnings` 就地修改 | 前者在最終修正輪處理；其餘留待辦 |
| 10 | `sync` 依平台順序處理（Important，計畫規定）；`upload` 先驗 sha 再查平台 / 封槍 / 截止；sync 的列缺 ValidationError 守衛；上傳成功後台帳寫入失敗 | R7；守衛在最終修正輪處理；其餘留待辦 |
| 11 | `slots or final_slots` 吞掉 0；lower-is-better 未測；同分未測；表格尾空白 | 前者在最終修正輪處理；其餘留待辦 |
| 12 | `_scan` 可能空轉通過（Important，計畫規定）；部分呼叫只斷 exit_code；file-level noqa | R10；其餘留待辦 |

## 5. 最終整支審查（opus）

四個探針對暫存 dataset 驅動 CLI：連 stage 兩個 id 位元組相同；失敗的 stage 零副作用；final 後只有選中者能重傳而 score / sync 照開；unlock → final 再寫一份決定。

**Important**（唯一修正輪，再審通過）：(1) `final --slots` 沒驗——負數在 `board_rule=best` 下寫出 `chosen=[]` 的 final 列 + lock，`last` 下 IndexError；0 悄悄變預設 → 函式層 `slots < 1` FAIL、`n = slots if slots is not None else …`。(2) `sync` 開頭對每個 id `load_staged`，`stage.json`（data root）缺一個整個命令 FAIL `not_staged`，而台帳在 git → 缺快照的 id 略過檔名規則。(3) spec 沒有補充決定節（§13 仍寫量測層零改動、§9.3 仍有後備、§6.5 的 `local=` 時區）→ §17。

**Minor 1–12**：非有限平台分數 ABORT 而非 FAIL（修）；kaggle 的 fileName / status / submittedBy 進台帳未 redact（待辦）；stage VERDICT 缺 `missing=` 與 `config_hash=unchecked`（待辦）；重傳的 id 在 report 顯示同一分數兩次（待辦）；`FinalEntry.staged_at` 取台帳列 `ts`（待辦）；Runner / default_runner 與 train/upload.py 重複（待辦）；錯誤模式的選項被靜默忽略（待辦）；sealed 子集查不到是 ABORT（待辦）；`Staged(...)` 在守衛外（修）；`_failed` 串流真值（修）；public 次序鍵不隨指標方向翻（spec 原意，待辦加註）；RSNA 測試沒用 `id_field=view_stem`（待辦）。

再審：F1–F6 全部 ADDRESSED；新 Important = R12（見 §3）。

## 6. 待辦（依優先序）

1. **report / status 對重傳的 id**：兩列 `uploaded` 都顯示 `latest_score`，last-vs-last 的 Δ 變 0、早一發的真分數不見（minor 4）；記 arrival 當下的分數或以時間最近的 `scored` 列對應。
2. **kaggle 列的 `fileName` / `status` / `submittedBy` 進台帳未 redact**（minor 2）：spec §10.2 說平台字串進台帳前都 redact、§11.4 又允許這三個欄位——補 `redact()` 零成本。
3. **stage 的 VERDICT 欄位**（minor 3）：`missing=` 與 `config_hash=unchecked` 只在 human 行與 JSON，spec §9.2 / §7.2 / §12 要它們在 VERDICT。
4. **`Runner` / `default_runner` 與 `train/upload.py` 重複**（minor 6）：抽到 `vcp/core/proc.py`，順便讓 rclone 的錯誤訊息也過 `redact`（最終審查建議 3，子專案 6 會碰）。
5. **plan 子集查不到是 ABORT 非 FAIL**（minor 8 + T4）：`stage` 的 `eval_plan.subset(sealed)`、init 的 `--test-subset train`。
6. **錯誤模式的選項靜默忽略**（minor 7）：kernel 類給了 `--test-run`、file 類給了 `--kernel` 應 FAIL。
7. **`FinalEntry.staged_at` 用 `stage.json` 的 `staged_at`**（minor 5）而非台帳列 `ts`（差毫秒，但它是排序鍵）。
8. **決選次序鍵註解**（minor 11）：sealed 依 `higher_is_better`、public 恆降冪是 spec §6.4 原意；lower-is-better 比賽可能想翻——加註解與一個 lower-is-better 測試。
9. **測試覆蓋**：25 h DST；view 越界；`columns` 重名表頭；巢狀融合配對；同 prereg 判兩次；allow_missing WARN；融合 test run 的 `verify`；同分決選；foreign 為第一發的 report；RSNA 測試改用 `id_field=view_stem`（minor 12）；`ref=0`；`ts` / `at` 格式驗證。
10. **小項**：Event / EVENTS / _REQUIRED 三處列舉；`_failed(proc: Any)` 型別；`samples == rows` 註解；ingest 衝突訊息補「omit --weights」；`upload` 的檢查順序與 spec 不同（只影響複合失敗的 `reason=`）；上傳成功後台帳寫入失敗留下無紀錄的上傳（先寫遠端後記錄的固有風險，`sync` 會把它撿回成 foreign）；e2e 部分呼叫只斷 exit_code；file-level `noqa: E501`。

## 7. 方法上的紀錄

- **不要對計畫 markdown 跑 `ruff format`**：它會把獨立的程式片段（`x: T = None,`）當模組層程式改寫（R1）。片段保持縮排（不可解析）或不格式化。
- **brief 裡的測試輸入也要「這個輸入真的會造成那個條件嗎」**：三個 brief 測試是錯的——排序後重寫不改 sha（R4）、句子裡的 token 就是提及（R6）、兩側同權重的配對不會失敗（R9）。實作者三次都正確地判斷是測試錯而非程式錯，並在報告裡說清楚。
- **控制者自己寫的修正規格也會回歸鐵則**（R12）：碰 CLI 選項的修正要明說「不用 Click 層驗證」。
- **接縫表有用**：唯一的預檢發現（`local=` 的時區）是靠「誰產、誰吃」逐列對出來的；最終審查的兩個 Important 則都是「一個命令在別的檔案不在時怎麼辦」（`stage.json` 在 data root、台帳在 git）與「一個 CLI 數字未經驗證就到台帳」——下次預檢加兩問：「每個寫入命令的每個輸入在到達台帳前經過誰驗證？」「這個命令讀的檔案哪些是 git 的、哪些是 data root 的，缺了會怎樣？」

## 8. Plan 6c：§6 待辦的處置（2026-09-06，分支 `worktree-plan6c-hygiene`，4 個 commit + 1 個控制者微修）

| §6 項 | 處置 |
|---|---|
| 1 | 做了：`sync` 的 `scored` 列記 `at` 與 `platform_ref`；`report.assign_scores`——平台時間的分數配給時間最近的那一發（同距離取 `at` 較晚者，不依賴列的順序），手動分數配給 `ts` 不晚於它的最近一發，每發取最新；`report` / `status` 逐發用它，`status.unscored` 看 `at` 最新的那一發；`final` 仍用 `latest_score`。實作者第一版用「互相搶佔」的配法，會把被修正的舊分數推到前一發——裁決 R1 改成現在的規則；審查再抓到同距離時依列序而非 `at`——R2 微修。 |
| 2 | 做了：kaggle 的 `fileName` / `status` / `submittedBy` 進台帳前 `redact`（32+ 字元的檔名會被遮、sync 配不到——本 repo 的候選檔名不會）。 |
| 3 | 做了：stage 的 VERDICT 帶 `missing=`（file 類）與 `config_hash=`（`unchecked` 或 12 hex）。 |
| 4 | Plan 7 已做（`vcp/core/proc.py`）。 |
| 5 | 做了：sealed 子集查不到 → `ValidationFailed("sealed_subset: …")`；`--test-subset train` 撞到必有的空 train 子集 → `ValidationFailed("test_subset …")`（原本是未包的 pydantic 錯 → ABORT）。 |
| 6 | 做了：file 類 profile 給 `--kernel` / `--version` / `--weights` → `kernel_options:`；kernel 類給 `--test-run` → `test_run:`；在 `load_profile` 之後、載入任何 run 之前檢查。 |
| 7 | 做了：`FinalEntry.staged_at` 取 `stage.json` 的 `staged_at`（原本是台帳列的 `ts`，差毫秒）。 |
| 8 | 做了：`rank_key(sign)`——public 的同分鍵也依 `higher_is_better`（lower-is-better 比賽的榜面分數同樣越低越好）；`public=None` 兩個方向都排最後；spec §17 第 13 條改寫。 |
| 9 | 做了一部分：`LedgerRow` 的 `ts` / `at` 必須是 UTC stamp；25 小時 DST、foreign 開頭的 report、`ref=0` 測試。未做：view 越界、`columns` 重名表頭、巢狀融合配對、同 prereg 判兩次、allow_missing WARN、融合 test run 的 `verify`、RSNA 測試改 `id_field=view_stem`。 |
| 10 | 做了：kaggle.py 改用 `core.proc.last_line`、`_failed` 型別。未做：三處事件列舉、`samples == rows` 註解、ingest 訊息、`upload` 檢查順序、上傳成功後台帳寫入失敗的固有風險、e2e 只斷 exit_code 的呼叫、file-level `noqa: E501`。 |

審查（sonnet）：SPEC ✅；QUALITY 1 MEDIUM（同距離時依列序 → R2 修）、1 LOW（`_assigned_score` 每列重算一次 `assign_scores`，量小不改）。全套 850 passed / 4 skipped、覆蓋率 96.40%。
