# vcp 0.17.0：選取的兩個缺口、失敗時的 CLI 警告、磁碟盤點 `vcp gc scan` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `select --none` 與 `--record <id>@<ref>`，讀取時把已被認成自己上傳的 `foreign:<ref>` 解析成 id；失敗的 `sync` 與上傳前同步也帶 `cli_warnings=`；新命令 `vcp gc scan` 只列大項目、類別與引用者，寫一份不可變的 `gc_scan` 產物，從不刪任何東西。

**Architecture:** 選取以「條目 → 標籤（比對單位）+ ref（哪一發）」解析（`vcp.submit.select.resolve`），台帳記人說的字面、讀取時才解析；`selection_state`、`report`、`final` 都改用標籤。CLI 警告從 `PlatformError` 搬到 `VcpError`，只有 `sync` 與 `actions` 以 `with_cli_warnings` 把個數寫進 VERDICT 欄位。gc 是新套件 `vcp.gc`：`references`（以 backup 的 `Collector` 走正本，換上不雜湊、以走法單位為交易的記錄端）、`layout`（類別與單位的純函式）、`scan`（走根、合併成項目、只對大小相符的候選雜湊）、`record`（掃完才開 `ArtifactWriter`），加 `vcp/cli_gc.py`。

**Tech Stack:** Python 3.12、pydantic v2、typer、pytest、ruff、uv。

**Spec:** `docs/superpowers/specs/2026-11-01-vcp-0170-selection-gaps-and-gc-design.md`（1dffac7；§13.1 的裁決優先於正文其他處）。分支 `feat/v0170-selection-gc`，worktree `.claude/worktrees/v0170`。

**Base：** 從 `main` 開，前提是兩個 PATCH 分支都已合併：`fix/append-whole-line`（9180269：`vcp.core.atomic.append_whole_line`、`core/log.py` 的 `WholeLineFileHandler`、`tests/helpers.py` 的 `full_disk_on_append`、禁止其他 append 模式 open 的守衛測試）與 `refactor/v0160-review-leftovers`（cd0466f：`train/status.py` 的 `TmpScan` / `_glob_dirs` / 讀不了的目錄略過、`cli_submit` 的 `_status_fields` / `_status_warns` / `_status_lines` / `_status_payload`、`final.py` 的 `_board_selection` / `_final_row`、`train_run` 拆分）。Task 5 依賴後者的 `_board_selection` 與 `_status_*`；Task 7 依賴後者的 `TmpScan` 與 `_glob_dirs`。開工前 `git log --oneline main` 要看得到這兩個合併；看不到就停下來回報，不要自己合。

## Global Constraints

- 取時只用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`；ruff TID251 擋 `datetime.now` / `utcnow` / `today` / `time.time`（`datetime.fromtimestamp(x, tz=UTC)` 可用）。
- 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾，exit 0（OK、WARN）/ 1（FAIL）/ 2（ABORT）；`--json` 時結果到 stdout、VERDICT 到 stderr；失敗時 `run_command(context=)` 保留識別欄位；命令永不互動提問。
- 不用 `git add -A`（明列檔案）、不用 `git stash`、不對 markdown 跑 `ruff format`；一律 LF，改寫台帳或卡的測試用 `newline="\n"` 或 `write_bytes`；收尾時 `git diff --check` 無輸出。
- append-only 檔一律經 `vcp.core.atomic.append_whole_line`；不新增任何 append 模式的 `open`（守衛測試 `tests/unit/test_append_only_ledgers.py` 會紅）。gc 寫的檔只經 `ArtifactWriter`（`write_once`）。
- 測試不碰真的 data root：`roots` fixture、`tests/helpers.py`、`tests/submit_fixtures.py`、`tests/backup_fixtures.py`（`_isolated_roots` 是 autouse）。
- **不建 `tests/unit/gc/` 套件**：`tests/unit` 沒有 `__init__.py`，pytest 會把它放上 `sys.path`，`gc/__init__.py` 會蓋掉標準庫的 `gc`。gc 的測試是 `tests/unit/test_gc_references.py`、`test_gc_layout.py`、`test_gc_scan.py`、`test_cli_gc.py`。原始碼套件 `src/vcp/gc/` 沒有這個問題。
- commit 格式 `type(scope): 說明`，不加 Co-Authored-By trailer；每個 commit 前 `uv run ruff check . && uv run ruff format --check .`。
- 每個行為變更先寫會失敗的測試、跑紅，再實作；只鎖現狀的 golden 測試例外（標明）。
- 不動 `CHANGELOG.md`、`src/vcp/__init__.py` 的 `__version__`、`.claude/.claude-plugin/plugin.json`、`docs/handover/`：發版是使用者同意後另一步。Task 11 最後跑全套 `uv run pytest --cov=vcp`，coverage ≥ 80%。
- **沒有新的 `reason=` 字彙。** 失敗一律用既有的 `invalid:` `not_found:` `not_staged:` `not_uploaded:` `sync_failed:` `platform_response:` `kaggle_not_found:`。
- 條目：`<id>`、`<id>@<ref>`（以第一個 `@` 切開、兩側去空白後寫成 `<id>@<ref>`）、`foreign:<ref>`（先判斷前綴，ref 可含 `@`）。
- 選取的字：`none` `ok` `missing` `unticked` `slots` `differs`；`NO_WARNING = ("none", "ok", "unticked")`；`board_rule` 不是 `best` 一律 `none`。
- gc：`--min-mib` 預設 `100.0`（MiB = 1024² bytes，`min_bytes = math.floor(M * 1024**2)`），`0` 列出每個項目，非有限數或 `< 0` → `invalid:`；重複檔候選 = 類別 `record` 或 `unreferenced`、大小 `>= max(1 MiB, min_bytes)`；人看的行最多印 3 個引用者；大小 `>= 1 GiB` 印 GiB、否則 MiB，都無條件捨去到一位小數（`math.floor(x * 10) / 10`，印 `:.1f`），VERDICT 的 `*_gib=` 用同一個捨去；產物 kind `gc_scan`、id `gc-<YYYYMMDDTHHMMSSZ>-<8 位十六進位>`（`os.urandom(4).hex()`，時戳取掃描開始）、檔案 `scan.json` 與 `items.jsonl`；`spec.params` 的值是字串 `{"min_bytes": "<n>", "root_options": "<n>"}`、沒有 `inputs`。
- 類別順序（判斷順序也是輸出順序）：判斷 `protected` → `debris` → `referenced` → `rebuildable` → `record` → `unreferenced`；VERDICT 與總量行的順序是 `CLASSES = ("referenced", "record", "rebuildable", "debris", "unreferenced", "protected")`。
- gc 從不刪、不改既有檔、不上鎖、不讀 `indexes/`、不 import `vcp.provenance`、不呼叫平台。
- `gc scan` VERDICT 依序：`scan=<id>|none roots= root_options= items= min_mib= referenced_gib= record_gib= rebuildable_gib= debris_gib= unreferenced_gib= protected_gib= duplicate_gib= missing= skipped= unreadable= running=`；`skipped` `unreadable` `running` 任一 `> 0` → WARN；`cmd=gc.scan`。
- 照抄的文字：
  - `note: foreign:<ref> is <id>'s upload; it is read as <id>`
  - `selection: final chose <a,b>; nothing ticked (recorded <ts>): the platform picks by its own rule`
  - `selection: <n> recorded (<k> distinct), <expected> expected (slots=<s>)`（`(<k> distinct)` 只在 `k != n` 時出現）
  - `gc scan <id>: listed only; vcp deletes nothing. unreferenced means no vcp record names it, not that nothing else needs it`（`--no-record` 時 `<id>` 是 `none`）
  - 項目行：`<class> <size> <path>[/ (files=<n>)][ by <r1>; <r2>; <r3>[; +<k> more]][ copy <dest>][ duplicate of <path>]`

## Review Focus

1. 同一個檔，紀錄裡的拼法跟掃到的不一樣（Windows 磁碟機代號大小寫、路徑裡有 `..`）：仍是 `referenced`，不是 `unreferenced`（錯的方向是把要用的權重說成沒人要）→ Task 9 `test_a_reference_spelled_another_way_still_matches`。
2. RSNA 那種專案目錄：`--root proj`，`proj/weights/` 同時是檢查點目錄、`proj/bundle/` 是上傳包：檢查點目錄的檔逐檔一項、每個檔只算一次，`proj/weights` 不會被說成一個目錄項目 → Task 9 `test_a_checkpoint_dir_inside_a_root_is_counted_once_file_by_file`。
3. `--root` 指向本機 backup 目的地底下（引用落在根的上層目錄）：根底下每個檔都帶 `backup:` 引用者 → Task 9 `test_a_root_inside_a_referenced_directory_inherits_its_referrers`。
4. `--record " S1 @ 11 "`（`@` 兩側有空白）：記成 `S1@11`；再記 `S1@11` 時 `written=false` → Task 4 `test_spaces_around_at_are_dropped_before_anything_is_compared`。
5. `--root` 裡的中文檔名：`items.jsonl`、`--json` 與人看的行都照常輸出，不因 cp950 主控台而失去 VERDICT → Task 10 `test_gc_scan_lists_a_non_ascii_name`。

新模組：`src/vcp/gc/{__init__,references,layout,scan,record}.py`、`src/vcp/cli_gc.py`。`vcp.gc.scan` import `references` 與 `layout`；`record` import `scan`；`cli_gc` import 全部。其餘都是改既有檔，見各 task 的 Files。

---

### Task 1: 失敗的列表也帶 `cli_warnings=`（§4）

**Files:**
- Modify: `src/vcp/core/errors.py`（`VcpError.__init__` 收 `cli_warnings`；刪掉 `PlatformError.__init__`，docstring 移到 `VcpError`）
- Modify: `src/vcp/submit/platforms/base.py`（`with_cli_warnings`，進 `__all__`）、`src/vcp/submit/platforms/__init__.py`（再匯出）
- Modify: `src/vcp/submit/platforms/kaggle.py`（`parse_submissions`、`list_submissions`）
- Modify: `src/vcp/submit/sync.py`（`sync`）、`src/vcp/submit/actions.py`（`_pre_sync`、`upload`）
- Test: `tests/unit/submit/test_platforms.py`、`test_sync.py`、`test_actions.py`、`test_transactions.py`

**Interfaces:**
- Produces:
  - `VcpError(message, *, location=None, fields=None, cli_warnings: tuple[str, ...] = ())`，屬性 `.cli_warnings`；`PlatformError` 繼承它，原本的關鍵字建構照用。
  - `with_cli_warnings(e: VcpError, *groups: Iterable[str]) -> VcpError`：`e.cli_warnings = merge_warnings(e.cli_warnings, *groups)`；`e.fields.setdefault("cli_warnings", len(e.cli_warnings))`；回傳 `e`。**只有 `sync()` 與 `actions` 呼叫它**；kaggle 只合併 `e.cli_warnings`、從不寫 `fields["cli_warnings"]`（否則剖析失敗那一頁的個數會先被 `setdefault` 鎖住，前面頁併進來也不再變）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_platforms.py
def test_every_vcp_error_can_carry_cli_warnings_and_with_cli_warnings_never_overrides():
    assert VcpError("x").cli_warnings == () and ValidationFailed("x", cli_warnings=("a",)).cli_warnings == ("a",)
    e = PlatformError("y", fields={"exit_code": 1}, cli_warnings=("b",))
    assert with_cli_warnings(e, ["c", "b"]) is e and e.cli_warnings == ("b", "c")
    assert e.fields == {"exit_code": 1, "cli_warnings": 2}
    f = VcpError("z", fields={"cli_warnings": 7})
    assert with_cli_warnings(f, ["d"]).fields["cli_warnings"] == 7 and f.cli_warnings == ("d",)
@pytest.mark.parametrize("body", [
    "not json",
    '[{"date": "2026-09-05T00:00:00Z"}]',  # fileName missing
    '[{"fileName": "a.csv", "date": "2026-09-05T00:00:00Z", "publicScore": "abc"}]'])
def test_a_page_that_fails_to_parse_carries_its_warning_lines(body):
    # pytest.raises(ValidationFailed, match="^platform_response:") as ei: parse_submissions(f"{OUTDATED}\n{body}")
    assert ei.value.cli_warnings == (OUTDATED,) and "cli_warnings" not in ei.value.fields
def test_a_bad_later_page_carries_the_warnings_of_every_page():
    # FakeRunner([(0, f"{OUTDATED}\n" + json.dumps({"submissions": [], "nextPageToken": "t2"}), ""),
    #             (0, f"{OUTDATED}\nWarning: maintenance tonight\nnot json", "")])
    # raises ValidationFailed "not JSON" from KagglePlatform().list_submissions(_profile(), runner)
    assert ei.value.cli_warnings == (OUTDATED, "Warning: maintenance tonight")
```

`test_sync.py` 加 helper `_answers(monkeypatch, *answers)`：把 `kaggle.timed_runner` 換成依序回答的假 runner（一個答案是 `(code, stdout)`，或一個要丟出的例外）。`PAGE1 = f"{OUTDATED_WARNING}\n" + json.dumps({"submissions": [], "nextPageToken": "t2"})`；`INF = json.dumps([{"ref": 5, "fileName": "submission.csv", "date": stamp(utc_now()), "description": "S1", "publicScore": "inf"}])`。

```python
@pytest.mark.parametrize(("answers", "words", "count"), [
    ([PAGE1, subprocess.TimeoutExpired([], 120)], ["sync_failed: kaggle CLI timed out"], 1),
    ([PAGE1, (1, "boom")], ["PlatformError: kaggle CLI failed (exit 1)", "exit_code=1"], 1),
    ([(0, f"{OUTDATED_WARNING}\nnot json")], ["platform_response: not JSON"], 1),
    ([PAGE1, (0, "Warning: maintenance tonight\nnot json")], ["platform_response:"], 2),
    ([(0, f"{OUTDATED_WARNING}\n{INF}")], ["platform_response:"], 1)])  # reconcile's _row
def test_a_failed_sync_still_counts_the_cli_warnings(pair, monkeypatch, answers, words, count):
    # _only_s1(pair, kaggle_command=[sys.executable])；before = _ledger(pair) bytes；CliRunner submit sync
    assert r.exit_code == 1 and "status=FAIL" in v and f"cli_warnings={count}" in v
    assert all(w in v for w in words) and _ledger(pair).path.read_bytes() == before
def test_failures_around_the_list_carry_cli_warnings_only_inside_its_scope(pair):
    # _only_s1(pair, kaggle_command=["vcp-no-such-kaggle"])，不換 runner
    assert r.exit_code == 2 and "kaggle_not_found:" in v and "cli_warnings=0" in v
    # 另一個 dataset 目錄、platform="manual" 的 profile：exit 1、"manual_platform:" in v
    assert "cli_warnings" not in v_manual
```

```python
# test_transactions.py（沿用 _hold、_release、_verdict、lockmod）
def test_a_sync_that_cannot_take_the_lock_carries_no_cli_warnings(roots, monkeypatch):
    # kaggle profile；WAIT_SECONDS 0.3、RETRY_SECONDS 0.05；_hold；invoke submit sync；_release
    assert r.exit_code == 2 and "locked:" in v and "cli_warnings" not in v
# test_actions.py
def test_a_pre_sync_that_cannot_parse_counts_its_warnings_and_reports_no_sync(pair):
    # kaggle profile；FakeRunner([(0, f"{OUTDATED_WARNING}\nnot json", "")])
    # raises ValidationFailed, match="^sync_failed: platform_response:"
    assert ei.value.fields == {"cli_warnings": 1}  # no sync=, no bound=
    assert pair.test_paths.submissions_log.read_bytes() == before
def test_a_pre_sync_whose_reconcile_fails_counts_the_listed_warnings(pair):  # 同上，答案是警告 + INF 列表
    assert ei.value.fields == {"cli_warnings": 1}
def test_a_read_back_look_that_cannot_parse_still_counts_the_looks_before_it(pair, no_wait):
    # FakeRunner([EMPTY, (0, "queued", ""), WARNED_EMPTY, (0, "Warning: maintenance tonight\nnot json", "")])
    out = upload(TEST, "S1", runner=runner, **_kw(pair))
    assert out.result.readback == "failed" and not out.row.confirmed
    assert out.cli_warnings == (OUTDATED_WARNING, "Warning: maintenance tonight")
```

既有 `test_an_unreadable_list_stops_the_upload` 加 `assert ei.value.fields == {"cli_warnings": 0}`。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit -q`；Expected: FAIL（`unexpected keyword argument 'cli_warnings'`、`cannot import name 'with_cli_warnings'`、VERDICT 沒有 `cli_warnings=`）。
- [ ] **Step 3: 實作上列 Interfaces，接進四個呼叫點**

`parse_submissions`：`_past_warnings` 之後的整段包一層 `except VcpError as e: e.cli_warnings = merge_warnings(warnings, e.cli_warnings); raise`。`list_submissions`：合併前面頁警告的 `except PlatformError` 放寬成 `except VcpError`（`_command` 丟的 `kaggle_not_found:` 在 `try` 之前，不動）。`sync()`：在交易裡先 `listed: PlatformList | None = None`，`try` 從 `get_platform(...).list_submissions(...)` 包到 `reconcile(...)`；`except PlatformTimeout as e: raise with_cli_warnings(ValidationFailed(f"sync_failed: {e}"), e.cli_warnings) from e`；`except VcpError as e: with_cli_warnings(e, listed.warnings if listed else ()); raise`。`OSError` 不接（ABORT 不帶欄位）。`_pre_sync`：同樣先 `listed = None`，`except (VcpError, OSError) as e: raise with_cli_warnings(ValidationFailed(f"sync_failed: {e}"), getattr(e, "cli_warnings", ()), listed.warnings if listed else ()) from e`。`upload()` 的 `seen = e.cli_warnings`（不再判斷 `isinstance`）。`_read_back` 不改。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/core tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/core/errors.py src/vcp/submit/platforms/base.py src/vcp/submit/platforms/__init__.py src/vcp/submit/platforms/kaggle.py src/vcp/submit/sync.py src/vcp/submit/actions.py tests/unit/submit/test_platforms.py tests/unit/submit/test_sync.py tests/unit/submit/test_actions.py tests/unit/submit/test_transactions.py`；`git commit -m "feat(submit): failed lists and syncs still count the platform CLI's warnings"`。

### Task 2: 台帳的 `refs_of` / `owners` 與條目解析（§3.1）

**Files:**
- Modify: `src/vcp/submit/ledger.py`（`_ours` 改回傳 `dict[str, str]`；`SubmissionLedger.refs_of`、`owners`；`arrivals` 改用 `owners()` 的鍵）
- Modify: `src/vcp/submit/select.py`（`FOREIGN_PREFIX` 移到模組上方；`parse_entry`、`Resolved`、`resolve`）
- Modify: `tests/submit_fixtures.py`
- Test: `tests/unit/submit/test_ledger.py`、`tests/unit/submit/test_select.py`

**Interfaces:**
- Produces:
  - `SubmissionLedger.refs_of(submission_id: str) -> set[str]`：這個 id 的 `uploaded`、`scored`、`errored` 列帶的 `platform_ref`。
  - `SubmissionLedger.owners() -> dict[str, str]`：被認成自己上傳的 foreign ref → 那一發的 id（`_ours` 的兩條路：上傳列直接帶 ref；結果列把 ref 綁給沒有 ref 的上傳）。`arrivals()` 的行為不變。
  - `parse_entry(entry: str) -> tuple[str | None, str | None]`：`<id>` → `(id, None)`；`<id>@<ref>` → 以第一個 `@` 切開、兩側 `strip()`；`foreign:<ref>` → `(None, ref)`（先判斷前綴）。
  - `@dataclass(frozen=True) Resolved(label: str, submission_id: str | None, ref: str | None)`；`resolve(ledger: SubmissionLedger, entry: str) -> Resolved`（§3.1 的三條；不寫台帳）。
  - `tests/submit_fixtures.py`：`uploaded_row(sid, at, *, ref: str | None = None)`；新 `scored_row(sid, *, at: str | None = None, ref: str | None = None, public: float = 0.5) -> LedgerRow`（`source="platform"`）；新 `errored_row(sid, *, at: str, ref: str) -> LedgerRow`（`source="platform"`、`platform_status="SubmissionStatus.ERROR"`）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_ledger.py；T0..T3 = 相隔一小時的 stamp
def test_refs_of_reads_uploaded_scored_and_errored_rows(tmp_path):
    led = ledger_of(tmp_path, staged_row("S1"), uploaded_row("S1", T0, ref="11"), uploaded_row("S1", T1),
                    scored_row("S1", at=T1, ref="12"), errored_row("S1", at=T2, ref="13"),
                    uploaded_row("S2", T0, ref="21"), foreign_row("99", T0))
    assert led.refs_of("S1") == {"11", "12", "13"} and led.refs_of("S2") == {"21"}
    assert led.refs_of("S9") == set()
def test_owners_maps_each_foreign_ref_we_uploaded_to_its_id(tmp_path):
    # S1 uploaded ref "k1" + foreign_row("k1", T0)；S2 uploaded 無 ref at T1 + foreign_row("k9", T1+1min)
    #   + scored_row("S2", at=T1+1min, ref="k9")；foreign_row("x7", T2)（隊友）
    assert led.owners() == {"k1": "S1", "k9": "S2"}
    assert sorted(label(r) for r in led.arrivals()) == ["S1", "S2", "foreign:x7"]
# test_select.py
@pytest.mark.parametrize(("entry", "parsed"), [
    ("S3", ("S3", None)), ("S3@123", ("S3", "123")), (" S3 @ 123 ", ("S3", "123")),
    ("S3@1@2", ("S3", "1@2")), ("foreign:a@b", (None, "a@b"))])
def test_parse_entry_splits_at_the_first_at_after_the_prefix(entry, parsed):
    assert parse_entry(entry) == parsed
def test_resolve_names_the_label_and_the_shot(tmp_path):  # 上面 owners 的台帳
    assert resolve(led, "S1") == Resolved("S1", "S1", None)
    assert resolve(led, "S1@k1") == Resolved("S1", "S1", "k1")
    assert resolve(led, "foreign:k9") == Resolved("S2", "S2", "k9")  # read as S2
    assert resolve(led, "foreign:x7") == Resolved("foreign:x7", None, "x7")
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit/test_ledger.py tests/unit/submit/test_select.py -q`；Expected: FAIL（`no attribute 'refs_of'`、`cannot import name 'parse_entry'`、`unexpected keyword argument 'ref'`）。
- [ ] **Step 3: 實作上列 Interfaces** — `_ours` 兩條路各記 `ref → submission_id`（第一條取帶 ref 的上傳列的 id，第二條取被吸收的上傳列的 id）；`owners()` 用跟 `arrivals()` 相同的 `uploads` / `latest_foreign` / outcomes 輸入（抽一個私有 `_arrival_inputs()` 讓兩者共用）。
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit -q`；Expected: PASS（含既有 `test_a_foreign_row_for_our_own_upload_is_one_arrival`）。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/ledger.py src/vcp/submit/select.py tests/submit_fixtures.py tests/unit/submit/test_ledger.py tests/unit/submit/test_select.py`；`git commit -m "feat(submit): ledger refs_of and owners, and selection entries resolved to labels"`。

### Task 3: 空的選取、`resolved`、`unticked` 與人看的行（§3.3 schema、§3.4）

**Files:**
- Modify: `src/vcp/submit/schema.py`（`LedgerRow._shape`：`selected` 的空 `chosen` 放行）
- Modify: `src/vcp/submit/select.py`（`SelectionState`、`NO_WARNING`、`_words`、`selection_state`、`selection_lines`、`differs_line`；新 `unticked_line`）
- Test: `tests/unit/submit/test_schema.py`、`tests/unit/submit/test_select.py`

**Interfaces:**
- Consumes: Task 2 的 `resolve`。
- Produces:
  - `selected` 列：`chosen=[]` 可以載入；缺 `chosen`、重複項照舊 `ValueError`。
  - `SelectionState.resolved: list[str] | None = None`（**最後一欄、有預設**；`chosen` 每項的標籤，順序同 `chosen`）；`to_json()` 多 `"resolved"` 鍵（共七鍵）；`NO_WARNING = ("none", "ok", "unticked")`。
  - `_words`：`board_rule != "best"` → `none`；`chosen is None` → `missing` / `none`（不變）；`chosen == []` → `unticked`，有 `final` 列時 `unticked,differs`；其他：`len(set(resolved)) != expected` → `slots`，有 `final` 且 `set(resolved) != set(final)` → `differs`。
  - `differs_line(final: list[str], recorded: list[str], resolved: list[str] | None = None) -> str`：`resolved` 預設 `recorded`；兩個差集以標籤算、各自去重保序，`recorded` 照台帳字面印。
  - `unticked_line(final: list[str], recorded_at: str) -> str`（Global Constraints 的句子）。
  - `selection_lines`：`unticked,differs` 只出 `unticked_line` 一行；`slots` 行照 Global Constraints（`n = len(chosen)`、`k = len(set(resolved))`）；其餘不變。

- [ ] **Step 1: 寫會失敗的測試**

`test_schema.py`：`test_a_selected_row_without_a_proper_choice_fails` 的參數拿掉 `({"chosen": []}, …)`；加：

```python
def test_an_empty_selected_row_loads_and_round_trips():
    row = LedgerRow(**{**SELECTED, "chosen": []})
    assert row.chosen == [] and LedgerRow.model_validate_json(row.model_dump_json(exclude_none=True)) == row
```

```python
# test_select.py；ref 版的 up：up_refs(("S1", "1"), ("S1", "2"), ("S3", "3")) 依序 uploaded_row(sid, _at(-5 + i), ref=ref)
def test_nothing_ticked_is_unticked_and_warns_only_against_a_final(tmp_path):
    rows = [*up("S1", "S2"), sel := selected_row([])]
    st = selection_state(ledger_of(tmp_path, *rows), _profile(deadline=_at(-1)), NOW)
    assert (st.words, st.warns, st.chosen, st.resolved) == ("unticked", False, [], [])
    # 另一個台帳再加 final_row(["S1", "S2"], 2)：
    assert st2.words == "unticked,differs" and st2.warns
    assert selection_lines(st2) == [f"selection: final chose S1,S2; nothing ticked (recorded {sel.ts}): "
                                    "the platform picks by its own rule"]
    # board_rule="last" → "none"
def test_ticking_two_shots_of_one_id_counts_one_submission(tmp_path):
    # up_refs(("S1","1"),("S1","2"),("S3","3"))、final_row(["S1","S3"], 2)
    # selected ["S3@3", "S1@1"] → words "ok"、resolved ["S3", "S1"]
    # selected ["S1@1", "S1@2"] →
    assert st.words == "slots,differs" and selection_lines(st) == [
        "selection: 2 recorded (1 distinct), 2 expected (slots=2)",
        "selection: final chose S1,S3; recorded S1@1,S1@2 (not selected: S3; selected, not final: -)"]
def test_a_foreign_ref_later_bound_to_our_upload_is_read_as_that_id(tmp_path):
    # uploaded_row("S1", T) 無 ref、foreign_row("k9", T+1min)、scored_row("S1", at=T+1min, ref="k9")、
    # final_row(["S1"], 1)、selected_row(["foreign:k9"])
    assert st.words == "ok" and st.resolved == ["S1"] and st.to_json()["resolved"] == ["S1"]  # 0.16.0: differs
```

既有 `test_selection_lines_and_differs_line`、`test_a_selection_equal_to_final_with_the_right_count_is_ok` 改 `to_json()` 的期望（多 `"resolved"`），其餘斷言一字不改：`k == n` 時 `slots` 行與 0.16.0 逐位元相同（golden）。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit/test_schema.py tests/unit/submit/test_select.py -q`；Expected: FAIL（`selected needs at least one entry`、`no attribute 'resolved'`、`cannot import name 'unticked_line'`）。
- [ ] **Step 3: 實作上列 Interfaces** — `selection_state` 在有 `selected` 列時 `resolved = [resolve(ledger, e).label for e in chosen]`；`expected` 不變。
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/schema.py src/vcp/submit/select.py tests/unit/submit/test_schema.py tests/unit/submit/test_select.py`；`git commit -m "feat(submit): an empty selection reads as unticked and labels decide slots and differs"`。

### Task 4: `select --none` 與 `--record <id>@<ref>`（§3.2、§3.3）

**Files:**
- Modify: `src/vcp/submit/select.py`（`select`、`_check_entries`、`SelectOutcome`；新 `owned_note`）、`src/vcp/cli_submit.py`（`select_cmd`）
- Test: `tests/unit/submit/test_select.py`、`test_transactions.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Consumes: Task 2 的 `parse_entry`、`resolve`、`refs_of`、`owners`；Task 3 的 `selection_state`。
- Produces:
  - `select(dataset: str, entries: list[str] | None, *, none: bool = False, message: str | None = None, data_root: Path | None = None, configs_root: Path | None = None) -> SelectOutcome`（`entries is None` = 沒給 `--record`）。
  - `SelectOutcome.notes: tuple[str, ...] = ()`（最後一欄）；`owned_note(entry: str, submission_id: str) -> str` = `f"note: {entry} is {submission_id}'s upload; it is read as {submission_id}"`。
  - CLI：`--record` 改成 `str | None = None`（help 提 `<id>@<ref>`）、新 `--none`；`human = [*out.notes, *selection_lines(out.state)]`；`selected=` 是 `len(out.row.chosen)`（`--none` 時 `0`）。

- [ ] **Step 1: 寫會失敗的測試**

`test_select.py` 的 `_shots(roots, *extra, **over)`：`seed_ledger(roots, _profile(**over), staged_row("S1"), staged_row("S2"), staged_row("S3"), uploaded_row("S1", T0, ref="11"), uploaded_row("S1", T1, ref="12"), uploaded_row("S2", T2), foreign_row("k9", T2 + 1min), scored_row("S2", at=T2 + 1min, ref="k9"), foreign_row("77", T3), *extra)`（S2 的 ref 只在結果列上；`k9` 被綁給 S2；`77` 是隊友）。

```python
def test_select_none_writes_an_empty_selection_once_even_after_the_lock(roots):
    _shots(roots, final_row(["S1"], 1), LedgerRow(event="lock", ts=stamp(), reason="final"))
    out = select(TEST, None, none=True, message="let the platform pick", **KW)
    assert out.written and out.row.chosen == [] and out.row.message == "let the platform pick"
    assert out.state.words == "unticked,differs"
    assert not select(TEST, None, none=True, **KW).written
    later = select(TEST, ["S1@11"], **KW)  # the newest row is the selection again
    assert later.written and later.state.chosen == ["S1@11"] and later.state.words == "ok"
@pytest.mark.parametrize(("entries", "none"), [(["S1"], True), (None, False)])
def test_record_and_none_are_exactly_one(roots, entries, none):  # raises "^invalid: give exactly one"
@pytest.mark.parametrize("entries", [
    ["S1@"], ["@11"], ["S1", "S1@11"], ["S1@11", "foreign:11"], ["S1@11", "S2@11"]])
def test_select_refuses_a_malformed_ref_entry(roots, entries):  # "^invalid:"、台帳 bytes 不變
def test_a_ref_entry_names_one_shot_of_its_id(roots):
    assert select(TEST, ["S1@12", "S2@k9"], **KW).row.chosen == ["S1@12", "S2@k9"]  # k9 only on a scored row
    assert select(TEST, ["S1"], **KW).written and select(TEST, ["S1@11"], **KW).written  # more precise: written
@pytest.mark.parametrize(("entry", "word", "says"), [
    ("S1@k9", "not_found:", "S2's upload"), ("S1@zz", "not_found:", None),
    ("S3@1", "not_uploaded:", None), ("S9@1", "not_staged:", None),
    ("foreign:11", "not_found:", "record it as S1@11")])
def test_a_ref_entry_that_is_not_that_ids_upload_fails(roots, entry, word, says):
    assert ei.value.fields["id"] == entry and (says is None or says in str(ei.value))
def test_spaces_around_at_are_dropped_before_anything_is_compared(roots):  # Review Focus 4
    assert select(TEST, ["S1 @ 11"], **KW).row.chosen == ["S1@11"]
    assert not select(TEST, ["S1@11"], **KW).written
def test_a_foreign_ref_that_is_our_upload_is_recorded_with_a_note(roots):
    out = select(TEST, ["foreign:k9"], **KW)
    assert out.written and out.row.chosen == ["foreign:k9"] and out.state.resolved == ["S2"]
    assert out.notes == ("note: foreign:k9 is S2's upload; it is read as S2",)
    assert select(TEST, ["foreign:k9"], **KW).notes == out.notes  # not written, still said
```

```python
# test_transactions.py
def test_select_with_bad_input_still_waits_for_the_lock(roots, monkeypatch):
    # _hold；invoke submit select --record S1 --none；_release
    assert r.exit_code == 2 and "locked:" in v
# test_cli_submit.py
def test_select_cli_none_and_ref_entries(roots):
    # _best_board(roots, staged_row("S1"), uploaded_row("S1", stamp(), ref="11"), final_slots=1)
    # --none → exit 0、"status=OK"、"selected=0"、"selection=unticked"、"written=true"
    # --record "S1 @ 11" --json →
    assert doc["result"]["chosen"] == ["S1@11"] and doc["result"]["selection"]["resolved"] == ["S1"]
    assert "selected=1" in v and "selection=ok" in v
    # --record S1 --none、以及兩者都沒有 → exit 1、"invalid:"、"dataset=beach-test"
def test_select_cli_prints_the_note_for_an_owned_foreign_ref(roots):
    # S1 uploaded 無 ref at T、foreign_row("k9", T+1min)、scored_row("S1", at=T+1min, ref="k9")；--record foreign:k9
    assert "note: foreign:k9 is S1's upload; it is read as S1" in r.output and "selection=ok" in v
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -k "select" -q`；Expected: FAIL（`unexpected keyword argument 'none'`、`No such option: --none`、`@` 項被當成未 stage 的 id）。
- [ ] **Step 3: 實作**

全部檢查在 `transaction(paths, profile, command="submit.select")` 裡、讀完台帳之後，任一項不過就 FAIL、一列都不寫。先把 `<id>@<ref>` 項寫成 `f"{sid}@{ref}"`（`parse_entry` 去空白後；邊是空的留到第 3 條報），再依序：
1. `(entries is None) == (not none)` → `invalid: give exactly one of --record and --none`；
2. `entries == []` → `invalid: --record needs at least one id or foreign:<ref>`（不變）；
3. 依輸入順序，含 `@` 的非 foreign 項兩側有空的 → `invalid: --record entry '<entry>' needs an id before @ and a platform ref after it`；
4. 重複項 → `invalid: --record names <entry> twice`（不變）；
5. 同一個 id 同時以裸 id 與 `@` 出現 → `invalid: --record names <id> both alone and as <id>@<ref>: record the id, or each of its uploads`；
6. 同一個 ref 被兩項指到（`@` 項與 `foreign:` 項都算）→ `invalid: --record names the platform ref <ref> twice`；
7. 依輸入順序逐項（`fields={"id": entry}`）：`foreign:<ref>` 不在 `foreign_refs()` → `not_found: foreign:<ref> is not a foreign upload in the ledger`，ref 屬於某個 id 的 `refs_of` 時加 `; it is <id>'s upload: record it as <id>@<ref>`；`<id>` 與 `<id>@<ref>` 照舊 `not_staged:` / `not_uploaded:`；`<id>@<ref>` 的 ref 不在 `refs_of(id)` → `not_found: <entry>: no upload of <id> carries the platform ref <ref>`，屬於別的 id（取排序第一個）時加 `; <ref> is <other>'s upload`。

冪等照舊比 `set(latest.chosen) == set(entries)`（`--none` 時 `entries = []`）。`notes` 取回傳那一列的 `foreign:` 項中 ref 在 `owners()` 的，依列內順序。不呼叫 `assert_unlocked`、不看截止、不呼叫平台。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -q`；Expected: PASS（含既有 `test_select_refuses_and_writes_nothing` 的 `[]` → `invalid:`）。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/select.py src/vcp/cli_submit.py tests/unit/submit/test_select.py tests/unit/submit/test_transactions.py tests/unit/test_cli_submit.py`；`git commit -m "feat(submit): select --none and --record <id>@<ref>"`。

### Task 5: `status`、`report`、`final` 依條目讀選取（§3.5）

**Files:**
- Modify: `src/vcp/submit/report.py`（`report` 的 `selected`）、`src/vcp/submit/final.py`（`FinalResult`、`_differing_selection`、`_board_selection`）、`src/vcp/cli_submit.py`（`final_cmd` 的選取行）
- Test: `tests/unit/submit/test_report.py`、`test_final.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Consumes: `resolve`、`Resolved`、`differs_line(final, recorded, resolved)`、`unticked_line(final, recorded_at)`；leftovers 分支的 `_board_selection`、`_status_fields`。
- Produces: `FinalResult.resolved: list[str] | None = None`、`FinalResult.recorded_at: str | None = None`（兩欄放最後，只在 `selection == "differs"` 時有值；`recorded` 照舊是台帳字面，空選取時是 `[]`）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_report.py
def test_status_says_unticked_and_carries_resolved(pair):
    # _seed(pair, _profile(board_rule="best"))；append selected_row([])
    assert st.selection.words == "unticked" and st.selection.resolved == [] and not st.selection.warns
def test_report_marks_by_entry(pair):
    # _seed(pair, _profile())：S1、S2 各 record 一次（無 ref），S1 有手動分數。
    # S1 再 record 一次 platform_ref="r2"（at 晚 30 分鐘）；foreign_row("k9", <S1 第一發 at + 1 分>)、
    # scored_row("S1", at=<同上>, ref="k9")（k9 綁給 S1 第一發）
    def marks(*chosen):  # append selected_row(list(chosen))；回傳 S1 兩列依時間的 selected
        ...
    assert marks("S1@r2") == [False, True]
    assert marks("foreign:k9") == [True, False]  # bound: marks the S1 shot whose outcome carries k9
    assert marks("S1") == [True, True]
    assert marks() == [False, False] and not any(r.selected for r in report(TEST, **_kw(pair)))
# test_final.py（_best(uploaded) 既有）
def test_final_compares_labels_and_an_empty_selection_differs(uploaded):
    plain = final(TEST, dry_run=True, **_kw(uploaded))
    # append selected_row(["S1@7"])；res = final(dry_run=True)
    assert res.selection is None and res.recorded is None
    # append sel = selected_row([])；res = final(dry_run=True)
    assert (res.selection, res.recorded, res.resolved, res.recorded_at) == ("differs", [], [], sel.ts)
    assert res.row.model_dump_json(exclude={"ts"}) == plain.row.model_dump_json(exclude={"ts"})  # golden
# test_cli_submit.py
def test_final_cli_says_nothing_ticked(roots, monkeypatch):
    # FinalResult(final_row(["S1"], 1), ["S1"], [], None, [], True, selection="differs", recorded=[],
    #   resolved=[], recorded_at=STAMP, next_step=next_step_hint(profile, ["S1"]))；ledger_mode → "configs"
    assert "status=WARN" in v and "selection=differs" in v
    assert f"selection: final chose S1; nothing ticked (recorded {STAMP}): the platform picks by its own rule" in r.output
def test_status_cli_after_none_prints_selected_0_and_unticked(roots):
    # _best_board(roots, staged_row("S1"), uploaded_row("S1", stamp()), _scored_row("S1", 0.5),
    #   final_slots=1, quota=Quota(per_day=5), selected_row([]))
    assert "status=OK" in v and "selected=0" in v and "selection=unticked" in v
    assert doc["result"]["selection"]["resolved"] == []
```

既有 `test_final_and_report_cli_render_the_selection`（沒給 `resolved`）照過：`differs_line` 的 `resolved` 預設 `recorded`。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit/test_report.py tests/unit/submit/test_final.py tests/unit/test_cli_submit.py -q`；Expected: FAIL（`S1@r2` 沒標任何列、`resolved` 不是 `FinalResult` 的欄位、空選取時 `selection is None`）。
- [ ] **Step 3: 實作**

`report()`：`marks = [resolve(ledger, e) for e in latest_selected.chosen]`（沒有列 → `[]`）；我們的一發，`ref = r.platform_ref or (outcome.platform_ref if outcome else None)`（`outcome` 就是已算的 `_outcome(ledger, r)`）；foreign 列 `ref = r.platform_ref`；`selected = any(m.label == label(r) and (m.ref is None or m.ref == ref) for m in marks)`。`_differing_selection`：最新 `selected` 列的 `chosen` 為空，或 `{resolve(...).label}` 不等於 `set(chosen)` 時回傳那一列，否則 `None`；`_board_selection` 據此給 `FinalResult` 的 `selection="differs"`、`recorded`、`resolved`、`recorded_at`。`final` 列一個位元組都不動。`final_cmd`：`res.selection` 時，`res.recorded == []` 印 `unticked_line(res.chosen, res.recorded_at)`，否則 `differs_line(res.chosen, res.recorded or [], res.resolved)`。`status` 不用改程式（`selected=` 已是 `len(chosen)`，`to_json` 已帶 `resolved`）。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_e2e_shared_ledger.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/report.py src/vcp/submit/final.py src/vcp/cli_submit.py tests/unit/submit/test_report.py tests/unit/submit/test_final.py tests/unit/test_cli_submit.py`；`git commit -m "feat(submit): status, report and final read selection entries by label and shot"`。

### Task 6: `Collector` 的記錄端與 `walk_all(split=)`（§5.3，先鎖 backup 的 golden）

**Files:**
- Modify: `src/vcp/backup/evidence.py`（`Recorder`、`Collector.recorder`、`_try` 改名 `attempt`、`walk_all(split=)`、`_walk_submissions`）
- Test: Create `tests/unit/backup/test_evidence_recorder.py`

**Interfaces:**
- Produces:
  - `class Recorder(Protocol)`：`tolerates: tuple[type[Exception], ...]`；`add(self, path: Path, role: str, conclusion: str, *, sha256: str | None = None, size: int | None = None, remote: RemoteCopy | None = None) -> None`；`mark(self) -> int`；`rollback(self, mark: int) -> None`。
  - `Collector(data_root, configs_root, *, recorder: Recorder | None = None)`（`field(default=None, kw_only=True)`）。`recorder is None`：`add` 與失敗處理跟今天逐位元相同（雜湊、`FileEntry`、只接 `ValidationFailed`、截斷回滾）——backup 的 `col.entries` / `missing` / `unlisted` / `files_of()` 不變。有 `recorder`：`add` 直接轉給它、不雜湊、不碰 `entries`。
  - `Collector.attempt(label: str, walk: Callable[..., None], *args: Any) -> None`：原 `_try` 改成公開名（gc 從別的模組呼叫它），三個呼叫點跟著改；有 `recorder` 時接 `recorder.tolerates`、以 `mark()` / `rollback()` 回滾；兩種都還原 `_seen`、`skipped.append(f"{label}: {e}")`。
  - `walk_all(dpaths: DatasetPaths, *, split: bool = False) -> None`：

| 走法單位 | `split=False`（不變） | `split=True` 的 label = 結論 |
|---|---|---|
| 這個 dataset 的 run | label `run:<id>`、結論 `all` | `run:<id>` |
| 預登記 | `judgement:<pid>`、`all` | `judgement:<name>/<pid>` |
| 台帳整體 | `submissions` | label `submissions:<name>` |
| 每個 submission | `submission:<sid>`、`all` | `submission:<name>/<sid>` |
| dataset 自己的檔 | `all` | 結論 `dataset:<name>` |
| `logs/vcp-*.jsonl` | `all` | 不走 |

- [ ] **Step 1: 先寫 golden（鎖現狀，改程式之前就綠）**

```python
# test_evidence_recorder.py；world fixture 來自 tests/unit/backup/conftest.py
def _shape(f):  # 不含 sha、bytes、source 的絕對路徑：跨次執行穩定
    return (f.key, f.role, f.tier, f.kind, f.present, tuple(f.for_), f.remote is not None)
GOLDEN = {("all", EVAL): [...], ("run:good", EVAL): [...], ("submission:S1", TEST): [...]}
@pytest.mark.parametrize(("conclusion", "dataset"), list(GOLDEN))
def test_backup_manifests_keep_their_files(world, conclusion, dataset):
    res = build_manifest(dataset, conclusion, manifest_id="golden", **_kw(world))
    assert [_shape(f) for f in res.manifest.files if f.role != "logs"] == GOLDEN[(conclusion, dataset)]
    assert all(f.sha256 == sha256_file(local_path(f, world.roots.data, world.roots.configs))
               for f in res.manifest.files if f.present and f.role != "submissions_log")
```

三份清單在改 `evidence.py` 之前印出、貼成字面；貼之前同一指令跑兩次確認一樣。Run: `uv run pytest tests/unit/backup/test_evidence_recorder.py -q` → PASS。

- [ ] **Step 2: 寫會失敗的測試**

```python
class _Book:  # a recorder double: every add is a row; a unit is taken back by truncation
    tolerates = (ValidationFailed, OSError)
    def __init__(self): self.rows = []
    def add(self, path, role, conclusion, *, sha256=None, size=None, remote=None):
        self.rows.append((Path(path), role, conclusion))
    def mark(self): return len(self.rows)
    def rollback(self, mark): del self.rows[mark:]
def test_a_recorder_gets_every_file_and_nothing_is_hashed(world, monkeypatch):
    # evidence.sha256_file、evidence.sha256_prefix 換成丟 AssertionError 的函式
    col = Collector(world.roots.data, world.roots.configs, recorder=(book := _Book()))
    col.walk_all(_paths(world, TEST), split=True)
    conclusions = {c for _, _, c in book.rows}
    assert {"dataset:beach-test", "submission:beach-test/S1"} <= conclusions and "all" not in conclusions
    assert all(c.startswith(("dataset:", "run:", "submission:beach-test/")) for c in conclusions)
    assert "logs" not in {r for _, r, _ in book.rows} and col.entries == {} and col.files_of() == []
    col.walk_all(_paths(world, EVAL), split=True)
    assert "judgement:beach/p-good" in {c for _, _, c in book.rows}
def test_a_unit_that_fails_takes_back_only_its_own_rows(world, monkeypatch):
    # Collector.walk_run 換成：結論以 "submission:" 開頭就 raise OSError("disk went away")，否則呼叫原本的
    col.walk_all(_paths(world, TEST), split=True)  # _Book recorder
    assert not any(c.startswith("submission:") for _, _, c in book.rows)
    assert col.skipped == ["submission:beach-test/S1: disk went away"]
def test_without_a_recorder_backup_still_stops_on_what_it_never_tolerated(world, monkeypatch):
    # 同上的換法、沒有 recorder → pytest.raises(OSError): col.walk_all(_paths(world, TEST))
```

- [ ] **Step 3: 跑紅** — `uv run pytest tests/unit/backup/test_evidence_recorder.py -q`；Expected: golden PASS、其餘 FAIL（`unexpected keyword argument 'recorder'`、`'split'`）。
- [ ] **Step 4: 實作上列 Interfaces** — `_walk_submissions(dpaths, conclusion: str | None)`：`None` 時每個 submission 用 `submission:<name>/<sid>` 當 label 與結論；`walk_all` 依 `split` 選表上的 label、結論，`split` 時不加 `logs/`。
- [ ] **Step 5: 跑綠** — `uv run pytest tests/unit/backup tests/unit/test_e2e_backup.py tests/unit/test_cli_backup.py -q`；Expected: PASS（golden 不變，既有 backup 測試全過）。
- [ ] **Step 6: Commit** — `git add src/vcp/backup/evidence.py tests/unit/backup/test_evidence_recorder.py`；`git commit -m "refactor(backup): a replaceable recording end for the evidence walk and split conclusions"`。

### Task 7: `checkpoint_dirs` 與 gc 的引用收集（§5.2 第 2 點、§5.3）

**Files:**
- Modify: `src/vcp/train/status.py`（抽出 `checkpoint_dirs`，`tmp_files` 改用它）
- Create: `src/vcp/gc/__init__.py`（只有 docstring）、`src/vcp/gc/references.py`
- Test: `tests/unit/train/test_status.py`；Create `tests/unit/test_gc_references.py`（本地 fixture `world = make_world(roots, tmp_path)`，`from backup_fixtures import make_world`）

**Interfaces:**
- Consumes: Task 6 的 `Collector(recorder=)`、`attempt`、`walk_all(split=True)`、`Recorder`。
- Produces:
  - `vcp.train.status.checkpoint_dirs(data_root: Path, record: TrainRecord) -> set[Path]` = 每個已登記 checkpoint 的 `resolve_stored_path(c.path, data_root).parent` ∪ `_glob_dirs(data_root, record)`。
  - `path_key(path: Path) -> str` = `os.path.normcase(str(Path(path).resolve()))`。
  - `@dataclass(frozen=True) Reference(path: Path, referrer: str, is_dir: bool, sha256: str | None, size: int | None, copy: str | None)`；`referrer` 是 `f"{conclusion}#{role}"`；`copy` 是 `remote.dest`。
  - `class ReferenceRecorder`（實作 `Recorder`）：`tolerates = (VcpError, OSError, ValueError)`；`refs: list[Reference]`；`add(...)`（檔）；`add_dir(path: Path, role: str, conclusion: str) -> None`；`mark()` / `rollback()` 以 `refs` 的長度截斷。
  - `@dataclass(frozen=True) FileRef(path: Path, referrers: frozenset[str], sha256: str | None, size: int | None, copy: str | None)`（同一個鍵的 `sha256` / `size` / `copy` 取第一個非 `None`）。
  - `@dataclass(frozen=True) References`：`files: dict[str, FileRef]`、`dirs: dict[str, frozenset[str]]`（鍵都是 `path_key`）、`skipped: list[str]`（`"<label>: <error>"`，同一個 label 只留第一筆）、`skipped_runs: frozenset[str]`（label 是 `run:<id>` 的 id）、`running: list[str]`、`checkpoint_dirs: list[Path]`（已 `resolve()`、依字串排序）、`counts: dict[str, int]`（`runs` `datasets` `backup_manifests` `artifacts`：成功的單位數）、`missing: int`（檔與目錄引用中 `os.path.lexists` 為假的不同鍵數）。
  - `collect_references(data_root: Path, configs_root: Path) -> References`。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_status.py
def test_checkpoint_dirs_are_the_registered_parents_and_the_started_globs(roots):
    # 沿用 _seeded（checkpoint 在 roots.data/"work"/"weights"）；train.log.jsonl 加一筆 started 事件
    #   checkpoints=["ckpt/*.pt"]、cwd=<roots.data/"work"> 的 store_path，並建 work/ckpt
    assert checkpoint_dirs(roots.data, load_record(roots.data, "r1")) == {w, roots.data / "work" / "ckpt"}
# test_gc_references.py
def test_a_checkpoint_a_submission_uses_has_both_referrers_and_its_copy(world):
    refs = collect_references(world.roots.data, world.roots.configs)
    best = refs.files[path_key(world.weights / "best.pt")]
    assert {"run:good#checkpoint_final", "submission:beach-test/S1#checkpoint_final"} <= best.referrers
    assert best.copy == str(world.vault) and best.sha256 is not None
    assert "run:good#upload_copy" in refs.files[path_key(world.vault / "good" / "best.pt")].referrers
    assert refs.counts["runs"] >= 2 and refs.counts["datasets"] == 2
def test_train_record_dirs_are_referenced(world, tmp_path):
    # good 的 train.yaml 加 exports=[ExportRef(dir=<tmp/export>…)]、venv=<tmp/venv>、
    #   config=ConfigRef(path=<tmp/cfg.yaml>, sha256="1"*64, copied_to="train/config.yaml")；save_record
    assert "run:good#export" in refs.dirs[path_key(tmp_path / "export")]
    assert "run:good#venv" in refs.dirs[path_key(tmp_path / "venv")]
    assert "run:good#config" in refs.files[path_key(tmp_path / "cfg.yaml")].referrers
def test_image_roots_backups_and_artifact_inputs_are_referenced(world, tmp_path):
    # beach-test 的 card 改 image_root=<tmp/images> 絕對路徑；build_manifest(EVAL, "run:good", manifest_id="m1")；
    # push(EVAL, "m1", str(tmp/"usb"))；ArtifactWriter：kind "note"、id "n1"、inputs=[InputRef(name="src", path=<tmp/src.bin>)]
    assert "dataset:beach-test#image_root" in refs.dirs[path_key(tmp_path / "images")]
    assert "backup:beach/m1#backup_dest" in refs.dirs[path_key(tmp_path / "usb")]
    assert "backup:beach/m1#checkpoint_final" in refs.files[path_key(world.weights / "best.pt")].referrers
    assert "artifact:note/n1#artifact_input" in refs.files[path_key(tmp_path / "src.bin")].referrers
    assert refs.counts["backup_manifests"] == 1 and refs.counts["artifacts"] >= 1
def test_a_run_of_another_checkout_is_still_walked(world, tmp_path):
    # save_run 一張 dataset="elsewhere" 的 RunCard "far"（configs 沒有這個 dataset）+ train.yaml 登記 <tmp/ext/m.pt>
    assert "run:far#checkpoint" in refs.files[path_key(tmp_path / "ext" / "m.pt")].referrers
def test_a_failed_submission_unit_leaves_no_referrer_behind(world, monkeypatch):
    # Collector.walk_run：結論以 "submission:" 開頭就 raise OSError("boom")
    assert not any(r.startswith("submission:") for f in refs.files.values() for r in f.referrers)
    assert path_key(stage_json(world.pair.test_paths, "S1")) not in refs.files
    assert refs.skipped == ["submission:beach-test/S1: boom"]
def test_unreadable_run_cards_are_skipped_once(world, monkeypatch):
    # runs/x/run.yaml 寫 b"\xff\xfe\x00bad"；evidence.load_run 對 "bad" 丟 PermissionError(13, "denied")
    labels = [s.split(": ", 1)[0] for s in refs.skipped]
    assert labels.count("run:x") == 1 and labels.count("run:bad") == 1 and {"x", "bad"} <= refs.skipped_runs
def test_a_programming_error_is_not_tolerated(world, monkeypatch):  # walk_run 丟 KeyError → pytest.raises(KeyError)
def test_collecting_references_hashes_nothing(world, monkeypatch):
    # evidence.sha256_file、evidence.sha256_prefix 換成丟 AssertionError 的函式；collect_references 照常回傳
def test_running_checkpoint_dirs_and_missing(world):
    # good 的 attempts 最後加一個 status="running" 的 Attempt；before = collect(...).missing；刪 last.pt
    assert refs.running == ["good"] and refs.checkpoint_dirs == [world.weights.resolve()]
    assert refs.missing == before + 1
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_status.py tests/unit/test_gc_references.py -q`；Expected: FAIL（`cannot import name 'checkpoint_dirs'`、`ModuleNotFoundError: vcp.gc`）。
- [ ] **Step 3: 抽出 `checkpoint_dirs`**；`uv run pytest tests/unit/train tests/unit/test_cli_train.py -q` → PASS；Commit：`git add src/vcp/train/status.py tests/unit/train/test_status.py`；`git commit -m "refactor(train): checkpoint_dirs out of tmp_files"`。
- [ ] **Step 4: 實作 `collect_references`**

一個 `Collector(data_root, configs_root, recorder=ReferenceRecorder())`，每個走法單位都經 `col.attempt(label, fn, …)`，依序：
1. 不在交易裡的一趟：每個有 `train.yaml` 的 run，`try` 讀 `TrainRecord`（`VcpError`、`OSError`、`ValueError` 就跳過——它的 run 單位會報 skipped），收 `checkpoint_dirs` 與「最後一個 attempt 是 `running`」的 id。
2. `data_root/runs/*/run.yaml`（依 id 排序），label `run:<id>`：`col.walk_run(id, f"run:{id}")`；有 `train.yaml` 時加 `exports[].dir`（`add_dir`，`export`）、`kind == "local"` 的 `uploads` 的 `Path(u.dest) / run_id / u.name`（`upload_copy`，帶 `sha256`）、`venv` 與每個 attempt 的 `venv`（`add_dir`，`venv`）、`config.path`（`config`，帶 `sha256`），路徑都經 `resolve_stored_path(…, data_root)`。
3. `configs_root/datasets/*/dataset.yaml`（依名稱）：label `dataset:<name>` → `col.walk_all(dpaths, split=True)`；label `image_root:<name>` → `Dataset.load_card` + `dpaths.resolve_image_root(card)`（`add_dir`，role `image_root`，結論 `dataset:<name>`；自成一個單位，壞掉的 card 不會連帶收回 `walk_all` 的引用）。
4. 同一個 dataset 的 `backup/*.json`：label `backup:<name>/<mid>` → `load_manifest(dpaths, mid)`，每個 `FileEntry` 以 `local_path(...)` 加（role、`sha256`、`bytes`、`remote` 照清單）；`backup.log.jsonl` 存在時 label `backup:<name>` → 每個 `push` 列中 `dest_kind(row.dest) == "local"` 的 `add_dir(Path(row.dest), "backup_dest", f"backup:{name}/{row.manifest_id}")`。
5. `data_root/artifacts/*/*/manifest.json`（依路徑）：label `artifact:<kind>/<id>` → `store.load_manifest`，每個有 `path` 的 `spec.inputs` 以 `resolve_stored_path` 加（`artifact_input`，帶 `sha256`）。

最後把 `refs` 依 `is_dir` 與 `path_key` 收成 `files` / `dirs`、算 `missing`、`skipped` 依 label 去重、`counts` 數成功的單位（`run:` / `dataset:` / `backup:<name>/` / `artifact:` 開頭、不在 skipped 的 label）。不讀 `indexes/`。

- [ ] **Step 5: 跑綠** — `uv run pytest tests/unit/test_gc_references.py tests/unit/backup -q`；Expected: PASS。
- [ ] **Step 6: Commit** — `git add src/vcp/gc/__init__.py src/vcp/gc/references.py tests/unit/test_gc_references.py`；`git commit -m "feat(gc): collect every reference to a path from the canonical records"`。

### Task 8: gc 的類別與單位（§5.4、§5.5 的單位表）

**Files:**
- Create: `src/vcp/gc/layout.py`
- Test: Create `tests/unit/test_gc_layout.py`

**Interfaces:**
- Produces（純函式，`parts` 是相對於所在根的路徑段，最後一段是檔名）：
  - `Where = Literal["data", "checkpoint", "option"]`；`CLASSES: tuple[str, ...] = ("referenced", "record", "rebuildable", "debris", "unreferenced", "protected")`。
  - `classify(parts: tuple[str, ...], where: Where, *, referenced: bool, partial_artifact: bool = False, committed_artifact: bool = False, skipped_run: bool = False, in_configs: bool = False) -> str`：依序
    1. `protected`：`where == "data"` 且 `len(parts) >= 2` 且 `parts[0] in ("raw", "locks")`；或 `".git" in parts[:-1]`（任何根）；
    2. `debris`：`parts[-1].lower().endswith(".tmp")`，或 `partial_artifact`；
    3. `referenced`：`referenced`；
    4. `rebuildable`（只 `data`）：`datasets/<n>/cache/materialize/…`、`datasets/<n>/cache/audit/…`、`datasets/<n>/cache/dhash.jsonl`、`indexes/…`；
    5. `record`：`in_configs`（任何根）；`data` 的 `committed_artifact`、`artifacts/<k>/supersession.jsonl`、`measure/…` `submit/…` `logs/…`、`runs/<id>/…` 且 `skipped_run`、`runs/<id>/inputs/…`、`datasets/<n>/samples.jsonl`、`datasets/<n>/raw_manifest.txt`、`datasets/<n>/cache/` 底下其他的檔；
    6. 其他 `unreferenced`。
  - `unit_of(parts: tuple[str, ...], where: Where) -> tuple[str, ...]`：`checkpoint` → `parts`；`option` → `parts[:1]`；`data` 照下表（`n = len(parts)`，不符合條件時單位就是檔本身 `parts`）：`raw` 且 `n >= 3` → `parts[:2]`；`datasets` 且 `n >= 4` 且 `parts[2] == "cache"`：`parts[3] == "materialize"` 且 `n >= 6` → `parts[:5]`，`parts[3] != "materialize"` → `parts[:4]`；`datasets` 且 `n >= 3`（其他）→ `parts[:2]`；`runs` / `measure` 且 `n >= 3` → `parts[:2]`；`submit` / `artifacts` 且 `n >= 4` → `parts[:3]`；其他頂層名稱且 `n >= 2` → `parts[:1]`。

- [ ] **Step 1: 寫會失敗的測試**

```python
@pytest.mark.parametrize(("rel", "where", "flags", "cls"), [
    ("raw/beach/a.png", "data", {}, "protected"), ("raw/beach/x.tmp", "data", {}, "protected"),
    ("raw/beach/a.png", "data", {"referenced": True}, "protected"),
    ("locks/submissions-ab.lock", "data", {}, "protected"), ("repo/.git/objects/ab", "option", {}, "protected"),
    ("runs/r1/train/.train.yaml.0a1b2c3d.tmp", "data", {"referenced": True}, "debris"),
    ("w/resume.PTH.TMP", "checkpoint", {}, "debris"),
    ("artifacts/k/i1/a.bin", "data", {"partial_artifact": True}, "debris"),
    ("work/w/best.pt", "data", {"referenced": True}, "referenced"),
    ("datasets/d/cache/materialize/png/x.png", "data", {}, "rebuildable"),
    ("datasets/d/cache/audit/groups.json", "data", {}, "rebuildable"),
    ("datasets/d/cache/dhash.jsonl", "data", {}, "rebuildable"),
    ("indexes/provenance-0123.sqlite3", "data", {}, "rebuildable"),
    ("datasets/d/cache/import_skipped.jsonl", "data", {}, "record"),
    ("datasets/d/samples.jsonl", "data", {}, "record"), ("datasets/d/raw_manifest.txt", "data", {}, "record"),
    ("runs/r1/inputs/preds.csv", "data", {}, "record"), ("runs/r1/big.bin", "data", {"skipped_run": True}, "record"),
    ("runs/r1/big.bin", "data", {}, "unreferenced"), ("runs/r9/a.bin", "data", {}, "unreferenced"),
    ("artifacts/gc_scan/gc-1/scan.json", "data", {"committed_artifact": True}, "record"),
    ("artifacts/k/supersession.jsonl", "data", {}, "record"), ("artifacts/k/foreign/a.bin", "data", {}, "unreferenced"),
    ("measure/d/readings.jsonl", "data", {}, "record"), ("submit/t/S1/stage.json", "data", {}, "record"),
    ("logs/vcp-2026-11-01.jsonl", "data", {}, "record"),
    ("configs/datasets/d/prereg.log.jsonl", "option", {"in_configs": True}, "record"),
    ("junk.bin", "data", {}, "unreferenced"), ("bundle/model.zip", "option", {}, "unreferenced")])
def test_the_class_of_a_file_is_the_first_rule_that_holds(rel, where, flags, cls):
    assert classify(tuple(rel.split("/")), where, **{"referenced": False, **flags}) == cls
@pytest.mark.parametrize(("rel", "where", "unit"), [
    ("raw/beach/a/b.png", "data", "raw/beach"), ("raw/readme.txt", "data", "raw/readme.txt"),
    ("datasets/d/cache/materialize/png/x.png", "data", "datasets/d/cache/materialize/png"),
    ("datasets/d/cache/materialize/npy-r256/x.npy", "data", "datasets/d/cache/materialize/npy-r256"),
    ("datasets/d/cache/audit/groups.json", "data", "datasets/d/cache/audit"),
    ("datasets/d/cache/dhash.jsonl", "data", "datasets/d/cache/dhash.jsonl"),
    ("datasets/d/samples.jsonl", "data", "datasets/d"), ("runs/r1/train/console.1.log", "data", "runs/r1"),
    ("measure/d/readings.jsonl", "data", "measure/d"), ("submit/t/S1/submission.csv", "data", "submit/t/S1"),
    ("submit/t/submissions.jsonl", "data", "submit/t/submissions.jsonl"),
    ("artifacts/k/i1/a.bin", "data", "artifacts/k/i1"),
    ("artifacts/k/supersession.jsonl", "data", "artifacts/k/supersession.jsonl"),
    ("indexes/provenance-0123.sqlite3", "data", "indexes"), ("logs/vcp-2026-11-01.jsonl", "data", "logs"),
    ("work/w/best.pt", "data", "work"), ("junk.bin", "data", "junk.bin"),
    ("bundle/a/b.zip", "option", "bundle"), ("top.zip", "option", "top.zip"), ("best.pt", "checkpoint", "best.pt")])
def test_units(rel, where, unit):
    assert "/".join(unit_of(tuple(rel.split("/")), where)) == unit
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/test_gc_layout.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.gc.layout`）。
- [ ] **Step 3: 實作上列 Interfaces**（只有路徑段的比對，不碰檔案系統）。
- [ ] **Step 4: 跑綠** — 同 Step 2；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/gc/layout.py tests/unit/test_gc_layout.py`；`git commit -m "feat(gc): classes and units of the disk inventory"`。

### Task 9: gc 掃描：根、走訪、合併、重複檔（§5.2、§5.5）

**Files:**
- Create: `src/vcp/gc/scan.py`
- Test: Create `tests/unit/test_gc_scan.py`（helper：`_put(base, rel, size=1, data=None) -> Path` 建檔；`_count_files(root)` = `os.walk` 底下的一般檔數、不含連結；`_without_indexes(res)` = 路徑不以 `indexes` 開頭的 `[i.to_json() …]`；`world` fixture 同 Task 7）

**Interfaces:**
- Consumes: `collect_references`、`References`、`path_key`（Task 7）；`classify`、`unit_of`、`CLASSES`（Task 8）。
- Produces:
  - `MIB = 1024**2`；`DEFAULT_MIN_MIB = 100.0`。
  - `@dataclass(frozen=True) Root(path: Path, recursive: bool, source: Literal["data_root", "checkpoint_dir", "option"])`。
  - `@dataclass(frozen=True) Item(path: str, kind: Literal["file", "dir"], bytes: int, files: int, cls: str, referrers: tuple[str, ...], copy: str | None, duplicate_of: str | None, duplicate_bytes: int, mtime: str)`；`to_json() -> dict[str, Any]`，鍵依序 `path kind bytes files class referrers copy duplicate_of duplicate_bytes mtime`（`referrers` 是 list）。`path` 是 `store_path`，目錄不帶結尾 `/`；檔的 `duplicate_bytes` = 是重複檔時的 `bytes`，否則 0；`mtime` 是最新的 mtime 以 `stamp(datetime.fromtimestamp(m, tz=UTC))`。
  - `@dataclass(frozen=True) Unreadable(path: str, error: str)`（`error` 是例外類別名）。
  - `@dataclass(frozen=True) ScanResult`：`scanned_at: str`、`finished_at: str`、`data_root: Path`、`configs_root: Path`、`roots: list[Root]`、`min_bytes: int`、`totals: dict[str, tuple[int, int]]`（每個類別都在，`(bytes, files)`）、`duplicates: tuple[int, int]`、`items: list[Item]`（依 `bytes` 由大到小、同大小依 `path`）、`references: dict[str, int]`、`missing: int`、`skipped: list[str]`、`unreadable: list[Unreadable]`、`running: list[str]`。
  - `check_min_mib(value: float) -> int`：非有限或 `< 0` → `ValidationFailed(f"invalid: --min-mib must be a finite number >= 0, got {value!r}", fields={"min_mib": value})`；否則 `math.floor(value * MIB)`。
  - `check_roots(paths: Sequence[Path]) -> list[Path]`：`expanduser()` 後不是目錄 → `ValidationFailed(f"not_found: --root {p} is not a directory", fields={"root": str(p)})`；回傳 `resolve()` 後的路徑。
  - `scan(data_root: Path, configs_root: Path, *, options: Sequence[Path] = (), min_bytes: int) -> ScanResult`（`options` 已經過 `check_roots`）。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_items_merge_up_to_their_unit_and_never_across_it(roots):
    # raw/a/x.png、raw/b/y.png；datasets/d/samples.jsonl、datasets/d/cache/materialize/png/1.png、
    # datasets/d/cache/materialize/npy-r256/1.npy、datasets/d/cache/import_skipped.jsonl；
    # runs/r9/a.bin、runs/r9/sub/b.bin（沒有 run.yaml）；junk.bin
    by = {i.path: i for i in scan(roots.data, roots.configs, min_bytes=0).items}
    assert (by["raw/a"].kind, by["raw/a"].cls) == ("dir", "protected") and "raw" not in by
    assert {"datasets/d/cache/materialize/png", "datasets/d/cache/materialize/npy-r256"} <= set(by)
    assert by["datasets/d/cache/materialize/png"].cls == "rebuildable" and "datasets/d" not in by
    assert by["datasets/d/samples.jsonl"].cls == by["datasets/d/cache/import_skipped.jsonl"].cls == "record"
    assert (by["runs/r9"].kind, by["runs/r9"].files, by["runs/r9"].cls) == ("dir", 2, "unreferenced")
    assert by["junk.bin"].kind == "file"
def test_a_run_dir_with_mixed_classes_is_split(world):
    # _put(world.roots.data, "runs/good/big.bin", 50)；_put(…, "runs/good/inputs/p.csv", 5)
    assert by["runs/good/big.bin"].cls == "unreferenced" and by["runs/good/inputs"].cls == "record"
    assert "run:good#run_card" in by["runs/good/run.yaml"].referrers and "runs/good" not in by
def test_a_skipped_run_is_a_record(world):  # runs/x/run.yaml 寫非 UTF-8 bytes 與 runs/x/big.bin
    assert res.skipped and by["runs/x"].cls == "record"
def test_the_threshold_lists_big_items_but_totals_count_everything(roots):
    # junk-a.bin 200 bytes、junk-b.bin 300、junk-c.bin 300、junk-d.bin 50；min_bytes=100
    assert [i.path for i in res.items] == ["junk-b.bin", "junk-c.bin", "junk-a.bin"]  # bytes desc, then path
    assert res.totals["unreferenced"] == (850, 4)
def test_a_root_above_the_data_root_keeps_the_data_root_rules(roots, tmp_path):
    # raw/beach/a.png、raw/beach/b.png；tmp_path/"loose.zip"；options=[tmp_path]（tmp_path 含 data/ 與 configs/）
    assert by["raw/beach"].files == 2 and by["raw/beach"].cls == "protected"
    assert sum(f for _, f in res.totals.values()) == _count_files(tmp_path)  # 每個檔只算一次
    assert [r.source for r in res.roots] == ["data_root", "option"]
def test_a_checkpoint_dir_inside_a_root_is_counted_once_file_by_file(world, tmp_path):  # Review Focus 2
    # good 的 train.yaml 登記 tmp/"proj"/"weights"/"m.pt"；proj/weights/notes.txt；proj/weights/sub/x.bin；
    # proj/bundle/up.zip；options=[tmp/"proj"]
    assert by[(proj / "weights" / "m.pt").resolve().as_posix()].cls == "referenced"
    assert by[(proj / "weights" / "notes.txt").resolve().as_posix()].kind == "file"
    assert (proj / "weights").resolve().as_posix() not in by
    assert by[(proj / "bundle").resolve().as_posix()].kind == "dir"
    assert [r.source for r in res.roots].count("checkpoint_dir") >= 1
    assert sum(f for _, f in res.totals.values()) == _count_files(world.roots.data) + 4
def test_a_root_inside_a_referenced_directory_inherits_its_referrers(world, tmp_path):  # Review Focus 3
    # build_manifest(EVAL, "run:good", manifest_id="m1")；push(EVAL, "m1", str(usb))；options=[usb/"data"]
    under = [i for i in res.items if i.path.startswith((usb / "data").resolve().as_posix())]
    assert under and all(i.cls == "referenced" and "backup:beach/m1#backup_dest" in i.referrers for i in under)
def test_a_reference_spelled_another_way_still_matches(world, tmp_path):  # Review Focus 1
    # 建 proj/x/ 與 proj/cfg.yaml；good 的 config.path 存成 (proj/"x"/".."/"cfg.yaml").as_posix()，
    # Windows 上再 .upper()；options=[proj]
    assert by[(proj / "cfg.yaml").resolve().as_posix()].cls == "referenced"
def test_a_copy_of_a_referenced_checkpoint_is_a_duplicate_and_only_size_matches_are_hashed(world, tmp_path, monkeypatch):
    # good 的 train.yaml 再登記 world.weights/"big.pt"（MIB + 7 bytes 的 b"a"）；proj/copy.pt = 同 bytes；
    # proj/other.pt = MIB + 7 bytes 的 b"b"；proj/odd.pt = MIB + 8 bytes；scan.sha256_file 換成記錄呼叫再呼叫原本的
    assert by[copy].duplicate_of == "work/good/weights/big.pt" and by[copy].duplicate_bytes == MIB + 7
    assert by[other].duplicate_of is None and res.duplicates == (MIB + 7, 1)
    assert sorted(Path(p).name for p in hashed) == ["copy.pt", "other.pt"]
def test_the_index_is_never_read(world):
    # 三次掃描：indexes/provenance-x.sqlite3 是 b"junk"、換成 b"other junk"、刪掉
    assert _without_indexes(a) == _without_indexes(b) == _without_indexes(c)  # [i.to_json() for i in items if not i.path.startswith("indexes")]
def test_an_unreadable_directory_is_listed_and_the_scan_goes_on(roots, monkeypatch):
    # runs/r9/a.bin、junk.bin；scan_mod.os.scandir 對 roots.data/"runs" 丟 PermissionError(13, "denied")
    assert [(u.path, u.error) for u in res.unreadable] == [("runs", "PermissionError")]
    assert "junk.bin" in {i.path for i in res.items}
def test_links_and_junctions_are_not_followed(roots, tmp_path):
    # tmp/"outside"/"big.bin"；os.symlink(tmp/"outside", roots.data/"link", target_is_directory=True)，OSError → pytest.skip
    assert not any(i.path.startswith("link") for i in res.items) and res.totals["unreferenced"][1] == 0
def test_input_checks(tmp_path):
    for bad in (-1.0, float("nan"), float("inf")):  # raises ValidationFailed "^invalid: --min-mib"
    assert check_min_mib(0) == 0 and check_min_mib(1.5) == MIB + MIB // 2
    # check_roots([tmp_path/"nope"]) → "^not_found: --root"；檔案也是 not_found
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/test_gc_scan.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.gc.scan`）。
- [ ] **Step 3: 實作上列 Interfaces**

根：`data_root.resolve()`（`data_root`、遞迴）；`refs.checkpoint_dirs`（`checkpoint_dir`、不遞迴）；`options`（`option`、遞迴）。`roots` 每個不同的 `path_key` 一項，先出現的來源勝（順序 data root、檢查點目錄、`--root`）。實際走訪：data root 走全部；檢查點目錄只走不在 data root 底下的，只計直接底下的檔；`--root` 只走不在 data root 底下、也不在另一個 `--root` 底下的。遞迴走訪時：不進 data root（它是自己的根）、不計任何被走的檢查點目錄直接底下的檔；以 `os.scandir` 依名稱排序讀目錄，`entry.is_symlink() or entry.is_junction()` 的整個略過；`FileNotFoundError` 靜默略過，其他 `OSError` 記 `Unreadable` 後繼續（雜湊候選時的 `OSError` 也一樣，記了就不算重複）。檔的路徑以「已 resolve 的根 / 各段名稱」組成，不逐檔 `resolve()`。

每個目錄往下帶：繼承的引用者（根的每個祖先與自己的 `refs.dirs` 引用者的聯集，從根開始算一次）、`in_configs`（鍵在 configs root 底下）、data root 的 `artifacts/<k>/<id>` 是半途（有 `spec.json`、沒有 `manifest.json`）還是已提交、`runs/<id>` 是否在 `refs.skipped_runs`。檔：`classify(...)`（`referenced` = 繼承的引用者或 `refs.files` 有它）、引用者 = 兩者聯集排序、`copy` = `refs.files` 的 `copy`、總量每檔都加。重複檔：類別是 `record` / `unreferenced`、大小 `>= max(MIB, min_bytes)`、且大小在「有記錄 sha 的被引用檔」的大小表裡（大小取記錄的，沒有就 `os.stat`，不存在就不收）才 `sha256_file`（模組層 `from vcp.core.hashing import sha256_file`）；sha 相同 → `duplicate_of` 是那個被引用檔的 `store_path`（多個時取排序第一個）。

合併（後序走訪，記憶體只留目前路徑上的摘要與已列出的項目）：

```text
summary(node) = EMPTY | MIXED | Uniform(sig=(cls, referrers, copy), unit, path, kind, bytes, files, mtime, dup_of, dup_bytes)
file  -> Uniform(kind="file", unit=unit_of(parts, where))
dir D -> 子項摘要 S（略過 EMPTY）
         D 可以合併 ⇔ D 底下沒有被略過的部分（data root、被走的檢查點目錄的直接檔）
                      且每個 S 都是 Uniform、sig 與 unit 都相同
                      且 D 不在 unit 的上層（unit 是 D 的路徑段的前綴，或兩者相同）
         可以 -> Uniform(kind="dir", path=D, 加總 bytes / files / dup_bytes，mtime 取最新，dup_of=None)
         不行 -> 每個 Uniform 的 S 列成項目（bytes >= min_bytes 才留）；回傳 MIXED
根本身永遠在 unit 的上層，所以它的 Uniform 子項一定列成項目、根從不是項目。
```

`references` = `refs.counts`；`missing`、`skipped`、`running` 照抄 `refs`；`scanned_at` 在收引用之前 `stamp()`，`finished_at` 在走完之後。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/test_gc_scan.py tests/unit/test_gc_layout.py tests/unit/test_gc_references.py -q`；Expected: PASS（沒有權限建符號連結的 Windows 上 skip 一項）。
- [ ] **Step 5: Commit** — `git add src/vcp/gc/scan.py tests/unit/test_gc_scan.py`；`git commit -m "feat(gc): scan the data root, checkpoint directories and --root"`。

### Task 10: `gc_scan` 產物與 `vcp gc scan`（§5.1、§5.6–§5.8、§7）

**Files:**
- Create: `src/vcp/gc/record.py`、`src/vcp/cli_gc.py`
- Modify: `src/vcp/cli.py`（`app.add_typer(gc_app, name="gc")`）
- Test: Create `tests/unit/test_cli_gc.py`（helper：`_field(verdict, key) -> str`；`_snap(root)` = `{相對 posix 路徑: (st_size, st_mtime_ns)}`；`world` fixture 同 Task 7）

**Interfaces:**
- Consumes: Task 9 的 `scan`、`ScanResult`、`Item`、`check_min_mib`、`check_roots`、`DEFAULT_MIN_MIB`、`MIB`；`CLASSES`。
- Produces:
  - `record.py`：`KIND = "gc_scan"`；`new_scan_id(scanned_at: str) -> str`；`scan_json(result: ScanResult) -> dict[str, Any]`（§5.7 的鍵依序：`scanned_at finished_at data_root configs_root roots min_bytes totals duplicates items references missing skipped unreadable running`；`data_root` / `configs_root` 是絕對 posix；`roots` 每項 `{"path": store_path, "recursive", "source"}`，data root 自己是 `"."`；`totals` 每類 `{"bytes", "files"}`；`duplicates` `{"bytes", "files"}`；`items` 是**個數**；`unreadable` 每項 `{"path", "error"}`）；`result_json(result) -> dict[str, Any]`（同上，但 `items` 換成 `[i.to_json() ...]`——`--json` 的 result）；`write_record(result: ScanResult, *, root_options: int) -> str`（掃完才 `ArtifactWriter.create(ArtifactSpec(kind=KIND, id=new_scan_id(result.scanned_at), params={"min_bytes": str(result.min_bytes), "root_options": str(root_options)}), data_root=result.data_root)`；`write_json("scan.json", scan_json(result))`、`write_bytes("items.jsonl", …)`（每項 `json.dumps(i.to_json(), ensure_ascii=False) + "\n"` 的 UTF-8）；`commit()`；回傳 id）。
  - `cli_gc.py`：`gc_app = typer.Typer(no_args_is_help=True, help="disk inventory: lists what is on disk and who names it; never deletes")`；`scan_cmd(root: list[Path] | None = --root, min_mib: float | None = --min-mib, no_record: bool = --no-record, json_mode, data_root, configs_root)`；`floor_gib(n: int) -> float`；`size_text(n: int) -> str`；`item_line(item: Item) -> str`；`LAST_LINE = "gc scan {id}: listed only; vcp deletes nothing. unreferenced means no vcp record names it, not that nothing else needs it"`。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_item_line_and_sizes():
    d = Item(path="runs/r7", kind="dir", bytes=3 * GIB + 1, files=4, cls="referenced",
             referrers=("a#x", "b#y", "c#z", "d#w"), copy="D:/vault", duplicate_of=None, duplicate_bytes=0, mtime=STAMP)
    assert item_line(d) == "referenced 3.0 GiB runs/r7/ (files=4) by a#x; b#y; c#z; +1 more copy D:/vault"
    f = Item(path="C:/proj/copy.pt", kind="file", bytes=150 * MIB + 5, files=1, cls="unreferenced", referrers=(),
             copy=None, duplicate_of="work/w/best.pt", duplicate_bytes=150 * MIB + 5, mtime=STAMP)
    assert item_line(f) == "unreferenced 150.0 MiB C:/proj/copy.pt duplicate of work/w/best.pt"
    assert size_text(int(99.96 * MIB)) == "99.9 MiB" and floor_gib(int(9.96 * GIB)) == 9.9
def test_gc_scan_writes_an_artifact_that_verifies(world):
    # invoke ["gc", "scan", "--min-mib", "0", "--json"]
    assert r.exit_code == 0 and "status=OK" in v and v.split()[1] == "cmd=gc.scan"
    assert [k.split("=")[0] for k in v.split()[3:]] == ["scan", "roots", "root_options", "items", "min_mib",
        "referenced_gib", "record_gib", "rebuildable_gib", "debris_gib", "unreferenced_gib", "protected_gib",
        "duplicate_gib", "missing", "skipped", "unreadable", "running"]
    sid = _field(v, "scan"); adir = world.roots.data / "artifacts" / "gc_scan" / sid
    assert re.fullmatch(r"gc-\d{8}T\d{6}Z-[0-9a-f]{8}", sid)
    assert {p.name for p in adir.iterdir()} == {"spec.json", "manifest.json", "scan.json", "items.jsonl"}
    lines = adir.joinpath("items.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == doc["result"]["items"] and len(lines) == int(_field(v, "items"))
    assert json.loads(adir.joinpath("scan.json").read_text(encoding="utf-8"))["items"] == len(lines)
    # vcp artifact verify --kind gc_scan --id <sid> → exit 0
def test_gc_scan_deletes_and_changes_nothing(world, tmp_path):
    # _snap(root) = {相對路徑: (st_size, st_mtime_ns)}；proj 有 bundle/up.zip
    # before = _snap(data)、_snap(proj)；gc scan --root proj --min-mib 0
    assert _snap(proj) == proj_before
    after = _snap(data)
    assert {k for k in after if k not in before and not k.startswith("logs/")} == {
        f"artifacts/gc_scan/{sid}/{n}" for n in ("spec.json", "manifest.json", "scan.json", "items.jsonl")}
    assert all(after[k] == v for k, v in before.items() if not k.startswith("logs/"))
    # --no-record：exit 0、"scan=none"、新增的只有 logs/ 的當日 log；最後一行是 LAST_LINE.format(id="none")
def test_gc_scan_human_output_ends_with_the_listed_only_line(world):
    lines = r.output.splitlines()
    assert lines[-2] == LAST_LINE.format(id=_field(lines[-1], "scan")) and lines[-1].startswith("VERDICT ")
    assert any(x.startswith("referenced: ") for x in lines) and any(x.startswith("duplicates: ") for x in lines)
def test_gc_scan_warns_when_it_could_not_see_everything(world, monkeypatch):
    # runs/x/run.yaml 非 UTF-8 → "status=WARN"、"skipped=1"、輸出有 "skipped: run:x: "
    # good 的最後 attempt 改 running → "running=1"
    # scan_mod.os.scandir 對 roots.data/"measure" 丟 PermissionError → "unreadable=1"、exit 0
def test_gc_scan_input_failures(roots, tmp_path):
    # --min-mib=-1 → exit 1、"invalid:"、"min_mib=-1.0"；--min-mib nan → exit 1、"invalid:"
    # --root <tmp/nope> → exit 1、"not_found:"、"root_options=1"
    # --data-root <tmp/new> → exit 0、"status=OK"、"items=0"（只有 run_command 建的 logs/）
def test_a_record_that_cannot_be_written_aborts_and_leaves_no_partial(world, monkeypatch):
    # record.ArtifactWriter.create 換成丟 OSError(errno.ENOSPC, "No space left on device")
    assert r.exit_code == 2 and "status=ABORT" in v and "OSError" in v
    assert not (world.roots.data / "artifacts" / "gc_scan").exists()
def test_gc_scan_lists_a_non_ascii_name(roots, tmp_path):  # Review Focus 5
    # tmp/"proj"/"上傳包.zip" 200 bytes；--root proj --min-mib 0 --json
    assert any(i["path"].endswith("上傳包.zip") for i in doc["result"]["items"])
    # 不加 --json 再跑一次：exit 0、輸出最後一行以 "VERDICT " 開頭
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/test_cli_gc.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.cli_gc`、`No such command 'gc'`）。
- [ ] **Step 3: 實作**

`scan_cmd` 的 `fn` 依序：`check_min_mib(min_mib if min_mib is not None else DEFAULT_MIN_MIB)`（先）→ `check_roots(root or [])` → `resolve_data_root(data_root)`、`resolve_configs_root(configs_root)` → `scan(...)` → 沒有 `--no-record` 時 `write_record(...)`。`context`：有給 `--min-mib` 時 `min_mib=`，有給 `--root` 時 `root_options=`。欄位照 Global Constraints 的順序：`scan` = id 或 `"none"`、`roots = len(res.roots)`、`root_options = len(root or [])`、`items = len(res.items)`、`min_mib` = 生效的 float、各類 `floor_gib(res.totals[c][0])`、`duplicate_gib`、`missing`、`skipped = len(...)`、`unreadable = len(...)`、`running = len(...)`；三者任一 `> 0` → WARN。人看的行：每個項目 `item_line`，每類 `f"{c}: {size_text(b)} in {n} files"`（`CLASSES` 順序），`f"duplicates: {size_text(b)} in {n} files"`，每個 `f"skipped: {s}"`、`f"unreadable: {u.path} ({u.error})"`、`f"running: {run_id}"`，最後 `LAST_LINE`。payload = `result_json(res)`。寫產物時的例外不接（`OSError` → ABORT 原例外）。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/test_cli_gc.py tests/unit/test_gc_scan.py tests/unit/test_cli.py tests/unit/test_cli_artifact.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/gc/record.py src/vcp/cli_gc.py src/vcp/cli.py tests/unit/test_cli_gc.py`；`git commit -m "feat(gc): vcp gc scan writes a gc_scan artifact and never deletes"`。

### Task 11: 端到端、回歸門檻與文件（§10.5、§11）

**Files:**
- Modify: `tests/unit/test_e2e_submit.py`（新測試）、`tests/unit/test_regression_gate.py`（`GATE` 最後加一列）
- Modify: `CLAUDE.md`（再整份複製成 `AGENTS.md`）、`docs/reference/cli.md`、`docs/guides/AGENT_SKILLS.md`
- Modify: `.claude/skills/vcp-train-submit-backup/{SKILL.md,reference.md}`、`.claude/skills/vcp-running-contests/{workflow.md,operator-guide.md}`、`.claude/skills/vcp-orientation/{SKILL.md,map.md}`，鏡射到 `.agents/skills/`
- Modify: `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`（第 34 條）、`docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`（§14 補充決定加一條）、`docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md`（§2.2 第 6、7、12 條的處置）、`docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md`（§4 的處置）
- Create: `docs/superpowers/plans/2026-11-01-vcp-0170-selection-gaps-and-gc-followups.md`

**Interfaces:**
- Consumes: Task 1–10 的測試名與 CLI。

- [ ] **Step 1: 端到端（先紅：Task 1–10 都在時才會綠，所以本步只確認它跑得過）**

```python
# test_e2e_submit.py
def test_kaggle_selection_by_ref_then_gc_scan(pair, tmp_path, monkeypatch):
    # 同 test_kaggle_platform_story 的假 CLI 與 profile（final_slots=1）；stage S1（good / good.test）；
    # upload S1 → confirmed=true；sync → S1 的 scored 列帶 platform_ref "1"；_measure_holdout("good")；
    # final → "chosen=S1"、"next: select S1 on the platform"
    # select --record S1@1 → "selected=1"、"selection=ok"、"written=true"
    # status → "selection=ok"；report → S1 那一行以 " selected" 結尾
    r = runner.invoke(app, ["gc", "scan", "--min-mib", "0", "--json"])
    assert r.exit_code == 0 and "status=OK" in v and "skipped=0" in v and "running=0" in v
    item = by["submit/beach-test/S1/submission.csv"]
    assert item["class"] == "referenced" and "submission:beach-test/S1#artifact" in item["referrers"]
    # vcp artifact verify --kind gc_scan --id <scan> → exit 0
```

Run: `uv run pytest tests/unit/test_e2e_submit.py -q` → PASS。真資料的整合測試不加 `gc scan`（它會寫產物）。

- [ ] **Step 2: 加 gate 列** — 標籤 `"selection gaps, failed-list CLI warnings and gc scan (0.17.0)"`；說明 `"submit: select --none records an empty selection that reads as unticked; --record <id>@<ref> names one upload and is checked against refs_of; a foreign:<ref> bound to our upload is read as that id; failed syncs and the pre-upload sync carry cli_warnings=; gc scan classes every file from the canonical records without the provenance index, writes a gc_scan artifact and changes nothing; the backup walk's manifests are unchanged by its new recording end"`（照既有列拆成多段字串）；證明：
  - `tests/unit/submit/test_select.py`：`test_select_none_writes_an_empty_selection_once_even_after_the_lock`、`test_a_ref_entry_that_is_not_that_ids_upload_fails`、`test_a_foreign_ref_later_bound_to_our_upload_is_read_as_that_id`、`test_nothing_ticked_is_unticked_and_warns_only_against_a_final`
  - `tests/unit/submit/test_report.py`：`test_report_marks_by_entry`；`tests/unit/submit/test_sync.py`：`test_a_failed_sync_still_counts_the_cli_warnings`；`tests/unit/submit/test_actions.py`：`test_a_pre_sync_that_cannot_parse_counts_its_warnings_and_reports_no_sync`
  - `tests/unit/backup/test_evidence_recorder.py`：`test_backup_manifests_keep_their_files`；`tests/unit/test_gc_layout.py`：`test_the_class_of_a_file_is_the_first_rule_that_holds`；`tests/unit/test_gc_scan.py`：`test_the_index_is_never_read`；`tests/unit/test_cli_gc.py`：`test_gc_scan_deletes_and_changes_nothing`
  - `tests/unit/test_e2e_submit.py`：`test_kaggle_selection_by_ref_then_gc_scan`

Run: `uv run pytest tests/unit/test_regression_gate.py -q` → PASS。Commit：`git add tests/unit/test_e2e_submit.py tests/unit/test_regression_gate.py`；`git commit -m "test(gate): guard the 0.17.0 selection, CLI-warning and gc scan behaviour"`。

- [ ] **Step 3: 改文件（內容照 spec §11）**

- `CLAUDE.md`：「提交治理」：`selected` 的 `chosen` 可以是空的（`select --none`，0.17.0 起，舊 vcp 讀到會 FAIL，第一次 `--none` 之前讀寫同一份台帳的都先升級）、條目 `<id>@<ref>`（0.16.0 照常載入但可能多報 `differs` / `slots`）、`foreign:<ref>` 讀取時解析、失敗的 `sync` 與 `sync_failed:` 也帶 `cli_warnings=`；「路徑」：`artifacts/gc_scan/<id>/`（`scan.json` + `items.jsonl`）是 `vcp gc scan` 的稽核紀錄，gc 只列、不刪、不讀 provenance 索引、不上鎖；「常用命令」加 `uv run vcp gc scan [--root DIR]… [--min-mib M] [--no-record]`、`select` 那行加 `--none` 與 `<id>@<ref>`。然後 `cp CLAUDE.md AGENTS.md`。
- `docs/reference/cli.md`：`submit select`（`--none`、`@ref`、§3.2 的檢查順序與訊息、`note:`）、`status` / `report` / `final`（`unticked`、以標籤比對、`report` 依條目標記）、`sync` / `upload`（FAIL 的 `cli_warnings=` 與它的範圍）；新節「磁碟盤點 `vcp gc`」（命令列、掃描範圍與根的規則、§5.3 的引用來源表、類別表、單位表、重複檔、產物、VERDICT、§12 的限制，並說明為什麼是 `gc scan` 而不是後記寫的 `gc --dry-run`）；「一次提交」的 `select` 範例後加一行 `# 刻意不勾：uv run vcp submit select --dataset D-test --none`。
- skills：`vcp-train-submit-backup/SKILL.md` 的選取步驟（`@ref` 用在重傳過的 id、`--none`）與新段「清磁碟」（`gc scan` → 讀 `unreferenced` / `debris` / `duplicate of` → 人自己刪，`record` 先問自己是否放棄那個 dataset 或 run → 掃描的 id 就是紀錄；`--root` 加專案目錄），`reference.md` 命令表；`vcp-running-contests/workflow.md` 第 10 步「決選」與收尾的清磁碟、`operator-guide.md` 對應段落；`vcp-orientation/SKILL.md` 命令表加 `gc scan`、路由（gc → `vcp-train-submit-backup`），`map.md` 版本沿革加 0.17.0。`docs/guides/AGENT_SKILLS.md` 的路由表加 gc。然後 `cp -r .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .agents/skills/`。
- 治理 spec 第 34 條、backup 審計 spec §14 補充決定一條（`Collector` 的記錄端、`attempt`、`walk_all(split=)`，backup 清單位元組不變）：各一段摘要，結尾「細節見 `2026-11-01-vcp-0170-selection-gaps-and-gc-design.md`」。
- 新後記：照 `2026-10-30-vcp-0160-selection-and-disk-followups.md` 的樣子（標頭寫 spec、分支、base 的兩個 PATCH 合併、commit 清單；§1 已知限制指回 spec §12；§2.1 實作裁決；§2.2 開放待辦）。§2.1 至少寫：kaggle 只合併 `cli_warnings`、個數只由 `sync` / `actions` 以 `with_cli_warnings` 寫進欄位；`Collector._try` 改名公開的 `attempt`、沒有記錄端時行為逐位元不變；gc 的 dataset 單位與 `image_root:<name>`、`backup:<name>` 兩種單位分開，`skipped` 依 label 去重；有記錄 sha 的被引用檔也包含 backup 清單與產物 input；檢查點目錄在 data root 底下時套 data root 的單位；被包住的 `--root` 列在 `roots`、由外層走；含有被略過部分的目錄不合併成項目；`--json` 的 `items` 是清單、`scan.json` 的是個數；`FinalResult` 多 `resolved`、`recorded_at`；測試目錄不叫 `gc`。§2.2：自動讀回平台的勾選（0.16.0 後記第 9 條照舊）、`gc` 的進度輸出、硬連結重複計算。
- 0.16.0 後記 §2.2 第 6、7、12 條各補一句處置（2026-11-01，0.17.0，指向本計畫與新後記）；RSNA 收尾後記 §4 補處置：命令是 `vcp gc scan`、只列不刪、寫 `gc_scan` 產物，不從 provenance 圖取引用（索引是衍生的、可能過期，記的是 entity 不是檔案路徑；過期時錯的方向是把要用的檔說成沒被引用）。
- `CHANGELOG.md`、`docs/handover/HANDOVER.md`、`CODEX_PROMPT.md`、`__version__`、`plugin.json`：不在本分支，發版 commit 再改。

- [ ] **Step 4: 驗證文件** — `diff CLAUDE.md AGENTS.md; diff -r .claude/skills .agents/skills; git diff --check; uv run pytest tests/unit/test_skills_plugin.py -q`；Expected: 三個 diff 無輸出、PASS。
- [ ] **Step 5: Commit docs** — `git add CLAUDE.md AGENTS.md docs/reference/cli.md docs/guides/AGENT_SKILLS.md .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .agents/skills/vcp-train-submit-backup .agents/skills/vcp-running-contests .agents/skills/vcp-orientation docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md docs/superpowers/plans/2026-11-01-vcp-0170-selection-gaps-and-gc-followups.md docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md`；`git commit -m "docs: 0.17.0 selection gaps, CLI warnings on failure and gc scan"`。
- [ ] **Step 6: 全套驗證** — `uv run ruff check . && uv run ruff format --check . && uv run pytest --cov=vcp`；Expected: 全綠，`TOTAL` coverage ≥ 80%。

---

## 自我檢查（對 spec 逐節核對的結果）

- **覆蓋：** §3.1 → T2（`refs_of`、`owners`、`resolve`）；§3.2 → T4；§3.3 → T3（schema）+ T4（命令）；§3.4 → T3；§3.5 → T5；§4.1、§4.2 → T1；§5.1 → T10；§5.2 → T7（`checkpoint_dirs`）+ T9（根、連結、讀不了的目錄、依目錄彙總）；§5.3 → T6（記錄端、`split`）+ T7；§5.4 → T8；§5.5 → T8（單位表）+ T9（合併、門檻、`copy`、重複檔）；§5.6 → T10；§5.7 → T10；§5.8 → T7（`running`、`skipped`）+ T10（WARN）；§6 的兩項 PATCH 是 Base；§7 每一列都有 VERDICT 斷言（`select` T4、`status` T5、`report` T5、`final` T5、`sync` T1、`upload` T1、`gc scan` T10）；§8 由 T3 的 schema 測試、T6 的 golden、T5 的 `final` 列 golden 鎖住；§10.1–§10.5 每條對到一個測試名（§10.4 的「`checkpoint_dirs` 抽出之後 `tmp=` 全過」是 T7 Step 3 的驗證）；§11 → T11。§9 的版本與 `CHANGELOG.md` 照指示不在本計畫。
- **型別一致：** `parse_entry`、`Resolved`、`resolve` 在 T2 定義，T3 T4 T5 使用；`SelectionState.resolved`、`unticked_line`、`differs_line(final, recorded, resolved)` 在 T3 定義，T4 T5 使用；`with_cli_warnings` 只在 T1；`Recorder`、`attempt`、`walk_all(split=)` 在 T6 定義、T7 使用；`path_key`、`References`、`collect_references` 在 T7 定義、T9 使用；`classify`、`unit_of`、`CLASSES` 在 T8 定義、T9 T10 使用；`Item`、`ScanResult`、`check_min_mib`、`check_roots`、`MIB`、`DEFAULT_MIN_MIB` 在 T9 定義、T10 使用。
- **既有測試要改的地方：** T3：`test_schema.py` 拿掉 `chosen: []` 的失敗參數，`test_select.py` 的 `to_json()` 期望多 `resolved`；T1：`test_an_unreadable_list_stops_the_upload` 多一個斷言；`submit_fixtures.uploaded_row` 多一個有預設的關鍵字參數，既有呼叫不受影響。其餘既有斷言不動：`differs_line` 的 `resolved` 預設 `recorded`、`FinalResult` 的新欄位有預設、`Collector` 沒有記錄端時行為不變。
