# VCP-048 後記：平台出錯、沒有分數的提交（待 0.14.0）

- spec：`../specs/2026-10-09-vcp-errored-submissions-design.md`（a7d220a）。沒有另寫計畫：spec §2 的範圍就是工作清單，照「平台 → 台帳事件 → sync → 讀取端 → 文件」的順序做，一件一個 commit。
- 分支 `feat/vcp-048-errored-submissions`，base `main` 02f787d（v0.13.0）。
- commit：
  - `478bd96` feat(submit): platform marks errored entries
  - `58599d0` feat(submit): errored ledger event
  - `c21a5d9` feat(submit): sync writes errored rows
  - `b91cb8a` feat(submit): status, report and final read errored outcomes
  - 文件一個 commit（本檔、`cli.md`、CLAUDE.md / AGENTS.md、skill 與鏡射、治理 spec 第 32 條、稽核 §18）。
- 每個行為變更都先寫測試、看它失敗再實作。例外：第一版 `errored_for_ref` 的單元測試是在 sync 的測試（先紅）帶出這個函式之後才補的；審查第 1 輪換成 `outcome_for_ref`，它的測試先紅。
- 版本、`CHANGELOG.md` 都沒動，發版時再做（§2 待辦第 1 條）。

## 1. 已知限制

- **沒有錯誤訊息的文字。** CLI 2.2.4 的 `--format json` 沒有 `error_description`；vcp 不碰憑證，也不改用 Python API。錯在哪要到平台的提交頁看。
- **沒有公開排行榜的比賽。** 這種比賽的 COMPLETE 本來就沒有 public 分數；比賽期間 private 也不公開，所以每一發都會被判成出錯。目前沒有這種比賽；遇到時要讓平台契約知道「這場沒有公開分數」。
- **foreign 列不變。** 它本來就存 `platform_status`；出錯的 foreign 一發只是多一筆快照，不寫 `errored` 列。
- **Manual 平台沒有列表。** 出錯的一發在 `status` 裡一直是 `unscored`；需要時用 `note` 事件手記。
- **出錯之後又回到 PENDING。** 平台若把出錯的一發重新排進佇列，pending 什麼都不寫，結果仍是出錯，直到它出分或再出錯。Kaggle 目前不會這樣。
- **`upload` 的 VERDICT 不報出錯。** 上傳前同步照樣寫 `errored` 列，但只有之後的 `status` / `report` 看得到（spec §2 的決定）。
- **`status` 只看每個 id 最新的一發。** 同一個 id 較早的一發出錯、最新的一發已出分時，`status` 不列它；`report` 那一行仍標 ` errored`。
- **長得像憑證的狀態字。** Kaggle 的狀態字在解析時就經過 redact；32 個以上連續英數字的狀態會變成 `<redacted>`，認不出 `error` / `complete`，當成不是出錯。實際的狀態字遠短於此。
- **舊 vcp 讀不動新台帳。** 0.13.0 以前的 vcp 讀到 `errored` 列會 FAIL `bad ledger row`。`configs` 台帳跟著 git 走，所有 checkout 都要先升級；`shared` 正本是同一個 data root 的所有 worktree。

## 2. 執行期裁決與開放待辦

### 2.1 執行期裁決

1. **本檔的結構。** CLAUDE.md 要裁決寫在後記的最後一節，也說開放待辦在最後一節；這份把兩者放進同一個最後一節（2.1、2.2），已知限制放前面。
2. **Kaggle 的判斷用 redact 之後的狀態字。** 也就是寫進台帳的那個字；不另外 `strip()`，照 spec 以 `.` 分割取最後一段再轉小寫。`PlatformSubmission.errored` 由各平台自己決定，Kaggle 的規則寫成 `kaggle.is_errored`。
3. **`SyncResult` 存 id 清單，`errored` 是它的長度。** spec 要 `errored: int` 給 VERDICT、id 清單給 `--json`；兩個欄位可能對不上，所以只存 `errored_ids`（每寫一列記一個 id，照寫入順序；同一次 sync 裡同一個 id 有兩發出錯就出現兩次），`errored` 是唯讀的 property。
4. **冪等跟同一個 ref 最新的結果列比（spec 作者 2026-10-09 裁決，修訂 spec §4.1）。**
   - 第一版照 spec 原文：`errored` 只跟同一個 ref 最新的 `errored` 列比，`scored` 只跟最新的 `scored` 列比。
   - 審查（Minor 1）指出，同一個 ref 在有分數與沒分數之間來回時，最後的狀態會漏記：0.8 → 空 → 0.8 停在出錯；空 → 0.8 → 空 停在已出分，`final` 可能選中平台上沒有結果的 id。
   - 裁決：兩種列一起看，跟這個 (id, ref) 最新的結果列（`scored` 或 `errored`，台帳順序）比；是同一種、而且分數與狀態都相同才不寫。狀態沒變時重跑 sync 照樣不寫。
   - 沒有 `errored` 列的台帳，最新的結果列就是最新的 `scored` 列，行為跟 0.13.0 相同。
   - 鍵仍是 (id, ref)，跟 `score_for_ref` 一樣。`SubmissionLedger.outcome_for_ref` 取代第一版的 `errored_for_ref`（沒發出過）；`score_for_ref` 是 0.12.0 起的公開方法，保留，但 src 已經不用它。sync 的兩個比較函式合成一個 `_outcome_changed`。
5. **寫第一列之前先建好每一列。** sync 原本在寫入前把每一發的 foreign 列建一次，值有問題就 `platform_response:`、台帳不動。現在出錯的一發也先建一次 `errored` 列（id 還不知道，用 `-` 代替），別的平台若把沒有狀態字的一發標成出錯，也在寫入前就停。
6. **人看的行。** spec 只規定 `status` 的 `errored: <id>`。`sync` 也每寫一列印一行 `errored: <id> (the platform finished it without a score)`，跟它的 `unconfirmed: <id>` 並排。
7. **VERDICT 欄位的位置。** `sync` 的 `errored=` 放在 `scored=` 後面，`status` 的 `errored=` 放在 `unscored=` 後面。
8. **結果列照台帳順序歸屬。** 一個 id 的 `scored` 與 `errored` 列一起、照台帳順序交給 `assign_scores`；只有 `scored` 列時跟以前完全一樣（`ts` 相同時仍是台帳裡先出現的那列為準）。
9. **出錯比分數新，就以出錯為準。** spec §4.2 說只看分數的地方照舊只看 `scored` 列，§4.4 說出錯那一列的 public、private 是空的。兩者只在「同一發先有分數、後來出錯」時不一致，這裡以 §4.4 為準：`report` 的 public／private 與 `status` 的榜面現任（`board_rule=best`）都取這一發的結果，結果是 `errored` 就沒有分數，榜面略過它。其他情況兩種讀法的結果相同。`latest_score`（`final` 表的 public 欄）照舊只看 `scored` 列。
10. **`assign_scores` 搬到 `vcp.submit.ledger`。** `final` 也要用它，而 `report` 會 import `final`，放在 `report` 會循環 import。`report` 的 `__all__` 照樣匯出它，舊的 import 不用改。`SubmissionLedger` 多了 `outcomes`、`assigned_outcomes`、`latest_outcome`，`status`、`report`、`final` 都經過它們。
11. **`final` 對出錯的一發不讀 provenance 與 sealed 讀數。** 跟 `probe`、`not_uploaded` 一樣，表裡的 `provenance`、`sealed_value` 是空的；`public` 欄照舊是 `latest_score`。
12. **資料類別的預設值。** `ReportRow.errored` 給了預設 `False`（放在最後，舊的位置參數建構照舊）；`StatusView.errored` 是必填欄位，只有 `status()` 建它。
13. **skill 的建議。** 平台的暫時問題才用同一個 id `--force` 重傳；程式錯了要修好、用新的 notebook 版本以新 id stage（`stage.json` 寫一次不改，同一個 id 只能送同一個版本）。
14. **CLAUDE.md / AGENTS.md。** spec 只要求事件清單加 `errored`；另加半句「舊 vcp 讀到會 FAIL，讀寫同一份台帳的都先升級」，因為這是升級時唯一會踩到的事。
15. **sync 的 CLI 測試走真的 Kaggle 平台。** 把 `kaggle.timed_runner` 換成假 runner（`test_actions` 已經這樣做），從 JSON 解析、配對到 VERDICT 都是真的程式碼。

### 2.2 開放待辦（依優先順序）

1. **發版 0.14.0。** `CHANGELOG.md` 要寫：
   - 台帳新事件 `errored` 與升級順序（spec §6）；
   - VERDICT 新欄位：`sync` 的 `errored=`（大於 0 時 WARN）、`status` 的 `errored=`；`final` 表的 `why=errored`；
   - `--json`：`sync` 與 `status` 的 `errored` 清單、`report` 每列的 `errored`；
   - Python API：`PlatformSubmission.errored`、`SyncResult.errored_ids` / `errored`、`StatusView.errored`、`ReportRow.errored`、`SubmissionLedger.outcomes` / `assigned_outcomes` / `latest_outcome` / `outcome_for_ref`，以及 `assign_scores` 改放在 `vcp.submit.ledger`；
   - `sync` 的冪等改跟同一個 ref 最新的結果列比（沒有 `errored` 列時行為不變）。
2. **比賽工作區升級。** 讀寫同一份台帳的每個 checkout 一起升到 0.14.0；升級後第一次 `sync` 會替已經出錯的那兩發補寫 `errored` 列（WARN 一次），之後 `status` 不再因為它們 WARN。
3. **交接文件。** `HANDOVER.md` 與 `CODEX_PROMPT.md` 在發版時提到新事件與升級順序。
4. **端到端測試。** `test_e2e_submit.py` 的假 Kaggle CLI 可以加一發「COMPLETE、分數空字串」，走完 sync → status → final；目前這條鏈由單元測試分段覆蓋。
5. **錯誤訊息。** Kaggle CLI 若在之後的版本把錯誤說明（Python API 的 `error_description`）放進 JSON，可以存進 `errored` 列（要經過 redact），再決定要不要加欄位。
