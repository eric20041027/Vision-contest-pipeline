# Plan 9 後記：角色範圍存取與收據（稽核 Wave 1b-1，v0.5.0）

計畫：`2026-09-12-vcp-plan9-access-receipts.md`；spec：`../specs/2026-09-12-vcp-access-receipts-design.md`（§16 記 22 條實作期決定）。分支 `wave1b-receipts`，base `main` 49216a8（v0.4.0）。12 個任務各經一次任務審查，7 個任務進了修正輪；最終整支審查（fable）給 6 個 Important，一輪修正後乾淨。全套 `tests/unit` 1128 passed / 2 skipped（torch 專用），覆蓋率 96.63%。

## 1. 執行期裁決（spec §16 之外）

spec §16 第 13–22 條是設計層面的裁決；下面是計畫或 brief 有缺漏時的處置：

1. `assert_run_matches` 改收 card 時計畫列了 6 個呼叫點，實為 8（`fuse/build.py` 與新加的 `load_card_context`）；全數改完。
2. 計畫的 `_open` 測試 helper 無條件塞 `subsets=`，壞掉 `roles=` 案例——只改 helper，產物程式碼照 brief。
3. `EVENTS` 加 `access` 後，`test_schema_records.py` 的 tuple 字面值一併更新。
4. judge 對沒有 `run.yaml` 的 run 以 `declared` 計（`_run_provenance`），讓「宣稱一個尚未建立的 run」仍是缺讀數 FAIL 而不是 crash；融合成員遺失維持 fail-fast。
5. `vcp eval status` 對算不出 provenance 的 run 記 `provenance_failed=`（WARN）；`vcp submit status` 對缺 `stage.json` 的提交印 `-`——兩個唯讀命令不依賴資料根完整。
6. 一個實作者把 commit 尾行寫成別的模型名，由控制端 amend；一個實作者留下 stash 條目，由控制端 drop；stray `test-output.txt` 移除。

## 2. 最終審查修正輪（已落地，commit e162659..4e9407a）

- close 後任何讀取（含先前取得的 generator）→ `closed:`。
- `provenance()` 多驗 `receipt.dataset` 與 `receipt.run_id`（spec §4.3 補列）。
- 失效收據只降等級、不抹掉觀測：`observed` = 所有 ref 的 `subsets` ∪ 有效收據的 `accessed`（spec §4.3 / §16.22）。
- `MaterializedReader` 在 run 下核對 `name` / `plan_id` 與 `train.yaml`（`mismatch:`）。
- `train run --resume` 以 `artifact_id` 合併 `run.yaml` 與 `train.yaml` 的 refs，不再覆蓋。
- README 自寫 loop 範例改用 `with`；`_build_receipt` 併入 writer context；空基底訊息列 `observed=`；final 的 `observed_sealed` 分支補測試。

## 3. 已知限制

- 一次 access 只能開一個 sealed 子集（§16.13）；plan 有兩個 sealed 子集時 `measure --unseal` 要分兩次 `--subsets`。
- 真資料整合測試 `tests/integration/test_access_receipts.py` 在開發機上 skip（`C:/vcp-data/datasets` 不在、`VCP_REALDATA_ROOT` 未設），尚未在真資料上跑過。
- 收據證明的是經存取器的讀取；process 自己 `open()` 檔案不在證明範圍（spec §2 的信任邊界；I/O 稽核 hook 留待 Wave 1b-2）。
- judge / sigma / anchor 仍用 `Dataset.load` 讀 eval 列；judge `--unseal` 的留痕沒有對應收據。

## 4. 開放待辦（依優先順序）

給 Wave 1b-2（SourceAudit、選取列存取器）或後續 PATCH：

1. **`train run` 的空 train 收據與半途收據**：train 收據 `accessed={}` 仍評 `receipt` → 加 WARN `receipt_empty=`；子程序被 SIGKILL 時收據不會 commit → 父程序掃 `artifacts/access_receipt/<run>-a<n>-*` 半途目錄加 WARN `receipt_partial=`。
2. judge / sigma / anchor 改走存取器（`purpose=measure`），讓每筆 unseal 留痕都對到一份收據；`unseal_event_sha256` 開始對 `<plan>.unseal.jsonl` 驗證。
3. 存取器 `_read` 對授權的 eval / sealed 列檢查 `label_source == "gold"`，補回 `assert_plan_invariants` 裡對量測有意義的那一半（measure / stage 路徑現在不跑它）。
4. 存取器：`subsets=set()` 應與 `roles=` 一樣拒絕；`iter()` 快取所有 Sample（記憶體）→ 1b-2 的選取列存取器處理；`by_id` 快取命中仍開檔；standalone id 只有 1 秒戳 + 16 位元 nonce 且不重試；`claim_receipt` 以訊息文字判斷 `exists:`（改用 `e.fields`）；`closed:` 訊息在 commit 失敗後仍寫「is committed」；`access.plan` 公開 sealed id 列表；`"samples file not found"` 缺 `not_found:` 前綴；`assert entry is not None` 在 `-O` 下失效。
5. `attach_receipts` 應拒絕 `measure` / `submit` 用途的收據（掛上自己的 measure 收據會清空乾淨基底）；`contaminated:` 對融合 run 印 `(receipt ?)`。
6. `export_subset` 的輸出目錄檢查移到 open 之前（現在非空目錄會留一份 failed 收據與 unseal 留痕）。
7. 提交層：`no_sealed_readings` 在 provenance 門檻擋掉所有候選時的建議文字誤導；`_render` 與 `verify()` 的 `WriteContext` 不同（排序子集 vs 檔案序 / 全集）→ 在 `WriteContext` docstring 寫明 writer 必須順序無關且不讀 `ctx.dataset.samples`；`submit status` 每筆都印 provenance 行（可只印低於門檻者）；讀過 sealed 子集的 baseline 仍可入選（考慮 WARN）。
8. 效能：`status()` 每個 run 重算 plan / card sha（可 hoist）；存取器每次呼叫重掃 `plan.assignment`（可快取每子集 id）。
9. 多程序訓練：`SessionBinding.on_commit` 整檔重寫 `train.yaml`，多 worker 各開存取器可能掉 ref（產物仍在；由 1. 的半途掃描或 Wave 2 lineage 補回）——先在文件標明。
10. 測試缺口：`receipt_invalid=` WARN、非零 `denied` 到 `RunResult`、崩潰子程序仍抄收據、`MAX_DENIED_FIRST` 上限、`claim_receipt` 重試耗盡、`index_samples` 重複 id、resume 的 `next_seq` 斷言（先種一筆 a1）、reader `__exit__` 帶例外、`provenance()` 的 dataset 不符分支、`samples_hash` 不符分支、candidate==baseline 的 contaminated 重複字串、`report` 的子字串斷言、`export_runs=`、`trained_on ∩ observed` 優先順序、RSNA `reader.sample()` / categories 路徑（訓練 venv）。
11. 小修：`Dataset.load` 兩次 `DatasetPaths.resolve`；`roles: dict[str, str]` 可收 `Role`；`ProvenanceInfo` 位置參數；`invalid` 只在融合分支排序；`_Context.provenance` 型別 `str`；`Session.access` 讀三次 `train.yaml`；RSNA categories 不符時 reader 以 completed 關閉（可放進 `with reader:` 內）；備份 verify 不對 `AccessRef.receipt_sha256` 交叉驗（provenance 已擋）；整合測試的「不變」守衛是路徑+大小不是位元組。
