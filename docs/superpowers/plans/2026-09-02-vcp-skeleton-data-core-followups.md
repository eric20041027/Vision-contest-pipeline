# Plan 1 執行後記：裁決紀錄與 Plan 2 待辦

- 對應計畫：`2026-09-02-vcp-skeleton-data-core.md`（14 個任務，全部完成並逐任務審查）
- 分支：`feat/plan1-skeleton-data-core`，26 個 commit，137 個測試，覆蓋率 96.5%，ruff check / format 乾淨
- 執行方式：subagent-driven development；每個任務一個新實作者 + 一次任務審查，最後整支分支由 opus 審查一次、一次修正波、一次範圍限定再審

## 執行中做的裁決（全部）

每條都是 controller 代替使用者做的決定，附上做錯時的代價，方便事後推翻。

1. **在原地開 feature branch 而非 worktree**。使用者未指示 worktree（`EnterWorktree` 工具要求明示），repo 無 `origin`，分支已足以保護 `main`。代價：checkout 停在 feature branch 直到合併。
2. **`.superpowers/` 加進 `.gitignore` 與 `.git/info/exclude`**。SDD 工作區不得提交。代價：一行 ignore。
3. **Task 4：接受 `config.py` 的 `UP047` per-file-ignore**（保留計畫原文的 TypeVar 寫法）。後於最終修正波改成 PEP 695 泛型並移除 ignore。
4. **Task 5：計畫原文的 `_BARE` 正規式有真 bug**（`$` 會放過結尾單個換行），規格意圖優先：改 `fullmatch` 並加回歸測試。代價：無。
5. **Task 7：三行超過 100 字元的計畫原文測試行，純換行整理**，獨立 `style` commit。代價：無。
6. **Task 11：計畫原文的向量分層抽樣依賴 iterstrat，但它不保證回傳剛好 n 個**（62 樣本 + 預設切分 → valA 拿到 8 而非 6），且不變量沒檢查子集大小。新增 `_balance_to_size()` 確定性修整到剛好 n，並在產生器加大小 `InvariantError`。代價：被修整的少數列標籤平衡略差；換得子集大小精確。
7. **Task 13：`vcp version` 也必須以 VERDICT 收尾**（鐵則不設例外）。代價：多一行輸出。
8. **Task 14：端到端走查改用 `--data-root` / `--configs-root` 指向暫存目錄**，不碰真實 `C:/vcp-data` 與 repo `configs/`。
9. **Task 14：`ruff format` 連 `docs/*.md` 的 Python 區塊也重排了**，已從 `ea47a3d` 還原 docs 並把 `docs` 加進 ruff `extend-exclude`。
10. **Task 14：測試曾寫 log 到真實 `C:/vcp-data/logs`**，加 autouse fixture 讓所有測試都用暫存根目錄。
11. **最終修正波納入 5 個 Important + 6 個小項**（分布表用 plan 的分層鍵、零寬向量退回隨機、空子集回報、策略登記表接線、groups.json 驗證、`_logger` 全例外保護、plan 最後寫入、`Dataset` 不可變外露、刪除未用夾具、PEP 695、JSON 分流測試）。
12. **策略登記表現在就接線**（`--strategy`，預設 `fixed`），因為「加一種形態不改 CLI」是通用性原則的核心；K-fold 本身仍不在範圍。
13. **空的 eval / sealed 子集以 `WARN` + `empty_subsets=` 呈現**，不做 FAIL；plan 仍是使用者要的比例，可見性才是機械保證。若日後被忽略而出事，改成 FAIL。
14. **`normalize_keys` 的 docstring 對 width==0 退回情況已過時**，只是註解，留給 Plan 2 第一次動到 `split.py` 時修。

## Plan 2 的 spec / plan 必須納入的事（來自整支審查）

1. **`image_root` 與 `raw_path` 目前寫的是絕對本機路徑，而 `dataset.yaml` 進 git**。匯出器與 materialize 都要解析它；跨機器（本機 Windows ↔ 租用 Linux）會斷。建議規則：能相對於 `data_root` 就存相對路徑，讀取時由 `DatasetPaths` 解回絕對。
2. **`import` 覆寫既有資料集時應 WARN**：`finalize_import` 會直接蓋掉 `samples.jsonl` 與 `dataset.yaml`；若 `splits/*.json` 已存在且新 hash 不同，早點警告勝過訓練後才被 `subset()` 的 hash 檢查擋下。
3. **`finalize_import` 先寫 `raw_manifest.txt`（md5 全部檔案，大資料集很慢）才驗證**；加大型匯入器時把驗證提前，或讓 manifest 可快取。
4. **`rows_skipped > 0 → WARN` 路徑零覆蓋**（`jsonl` 永不跳過）；`csv_boxes` 進來時連 `import_skipped.jsonl` 格式一起測。
5. **`tests/integration/` 尚不存在**，`realdata` marker 已註冊；Plan 2 第一個任務建立它。
6. **`resolve_configs_root` 的套件相對 fallback**在非 editable 安裝下會指向 site-packages 而不報錯；該分支也沒測試。改成明確報錯。
7. **`Dataset.load(verify_hash=False)`** 只有測試在用；加 docstring 警語或標為僅供測試，避免 Plan 2 為了速度濫用而斷開 hash 鏈。
8. **`load_plan` 不重驗不變量**：plan 檔被手動編輯時 `subset()` 會靜默少回傳；載入時跑一次 `assert_plan_invariants`。
9. **比例超額訂閱時的錯誤訊息**（多個 eval 子集四捨五入後總和超過可用池）應說明原因，而非 `stratified_take returned 1 units ... expected 2`。
10. **規格 §5.2 允許 regression 的 `targets` 為空集合**，會讓樣本通過驗證卻無法分層；建議規格改成「非空且 ⊆」。
11. **`generate_fixed` 約 80 行**，抽出「建立單位 + 統計衝突」成 `_build_units` 即可。
12. **`seed + step` 讓相鄰 seed 的子集共用亂數**，改 `seed * 1000 + step` 之類。
13. 小項：`save_plan` 的 exists-then-open 可改 `open("x")`；`stamp()` 截斷 vs 四捨五入、`parse_stamp` 寬鬆度、`worst()/exit_code()` 裸 KeyError、`record.vcp` 可覆蓋保留鍵、`(None, seq_index)` 去重邊界、`stratify_key="none"` 端到端、非 sealed 子集傳 unseal 參數、`audit_group_conflicts → WARN` 分支、`finalize_import` 的 `src.is_dir` 守衛，皆缺測試。
14. **計畫檔 Task 14 的 §13 驗收編號與規格不一致**（計畫文字錯誤；實際對應 1、5、8、9、10 後半、11）。

## 流程觀察

逐任務審查抓到的都是單檔內的問題；整支審查抓到的 5 個 Important 全部是跨模組接縫（分布表 ↔ plan params、tasks ↔ split ↔ sklearn、CLI ↔ 檔案邊界、CLI ↔ 登記表）。Plan 2 保留「整支分支最終審查」這一關。
