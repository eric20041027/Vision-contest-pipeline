# vcp 0.16.0：最終選取、Kaggle CLI 警告、訓練磁碟空間 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 台帳記下平台上實際的最終勾選並與 `final` 核對；`sync`／`upload` 浮出 Kaggle CLI 的警告；`train run` 在第一次寫入前與失敗後看磁碟空間；`train status` 列殘檔；vcp 的 yaml 換寫改成原子替換。

**Architecture:** 提交端新增 `vcp.submit.select`（事件 `selected`、共用判斷 `selection_state`、命令 `select`），`status`／`report`／`final` 只讀它；平台契約回傳 `PlatformList`，警告帶到 VERDICT、不進台帳。訓練端新增 `vcp.train.disk`（可替換的探針 `usage`／`device`、量測、診斷、`own_writes`），`train_run` 在 `_close_running` 之前量、attempt 失敗後再量。`vcp.core.atomic.replace_text` 讓 `dump_yaml_model` 換寫時讀者只看得到完整的舊檔或新檔。

**Tech Stack:** Python 3.12、pydantic v2、typer、pytest、ruff、uv。

**Spec:** `docs/superpowers/specs/2026-10-30-vcp-0160-selection-and-disk-design.md`（3fc5e2b；§13 的裁決照辦）。分支 `feat/v0160-selection-disk`，worktree `.claude/worktrees/v0160`。

## Global Constraints

- 取時只用 `vcp.core.time.utc_now()` / `stamp()`；ruff TID251 擋 `datetime.now` / `utcnow` / `today` / `time.time`（`time.sleep`、`time.monotonic` 可用）。
- 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾，exit 0（OK、WARN）/ 1（FAIL）/ 2（ABORT）；`--json` 時結果到 stdout、VERDICT 到 stderr；失敗時 `run_command(context=)` 保留 `dataset=`、`run=` 等識別欄位。
- 命令永不互動提問。
- 不用 `git add -A`（明列檔案）、不用 `git stash`、不對 markdown 跑 `ruff format`。
- 一律 LF；改寫台帳或卡的測試用 `newline="\n"` 或 `write_bytes`；收尾時 `git diff --check` 無輸出。
- 測試不碰真的 data root：用 `roots` fixture、`tests/helpers.py`、`tests/submit_fixtures.py`（`tests/conftest.py` 的 `_isolated_roots` 已是 autouse）。
- commit 格式 `type(scope): 說明`，不加 Co-Authored-By trailer。
- 每個行為變更先寫會失敗的測試、跑紅，再實作；只鎖現狀的 golden 測試例外（標明）。
- 每個 commit 前：`uv run ruff check . && uv run ruff format --check .`。
- Task 9 之後跑全套 `uv run pytest --cov=vcp`，coverage ≥ 80%。
- 不動 `CHANGELOG.md`、`__version__`、`.claude/.claude-plugin/plugin.json`：發版是使用者同意後另一步。
- `selection_warn_hours` 預設 `48`（`float | None`，有限、`>= 0`，等於預設時不寫出）；`expected = min(名額, 可選條目數)`，至少 1；名額取最新 `final` 列的 `slots`，沒有才用 `final_slots`。
- `--min-free-gib` 預設 `10.0`，GiB = 1024³ bytes；`0` 照量照記、永不 ABORT，也關掉結束後的空間比較。
- 新 `reason=` 字彙只有 `low_disk:`、`disk_full:`，都只在 `train run`。
- console 字樣（依此順序、不分大小寫）：`No space left on device`、`[Errno 28]`、`[WinError 112]`、`[WinError 39]`、`There is not enough space on the disk`、`Disk quota exceeded`；只讀 `console.<n>.log` 最後 `DISK_FULL_TAIL` = 64 KiB（65536 bytes）。
- vcp 自己寫不下 = `errno` 是 ENOSPC 或 EDQUOT，或 `winerror` 是 112、39；只有 `train run` 在第一次寫入之後轉成 ABORT `disk_full:`（`disk_evidence=vcp`），其他命令的 `OSError` 照舊。
- Windows 替換重試：只在 `os.name == "nt"`，`PermissionError` 最多重試 5 次，每次之前 `time.sleep` `REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)`（共約 1.55 秒），仍失敗刪暫存檔、原樣丟出；POSIX 不重試。
- 欄位名照 spec：VERDICT `selected=` `written=` `selection=` `cli_warnings=` `free_gib=` `min_free_gib=` `disk_path=` `disk_evidence=` `disk_check=` `tmp=`；`--json` 的 `selection` 是 `{chosen, recorded_at, words, expected, final, due_at}`；`DiskCheck(min_free_gib, checked_at, volumes)`，volume 是 `path` `roles` `free_bytes` `total_bytes`；`note` 是 `key` `marker` `free_bytes`。
- 照抄的文字：`the platform CLI says it is outdated; upgrade the CLI that kaggle_command runs`；`next: select <chosen> on the platform, then: vcp submit select --dataset T --record <chosen>`；`disk_full: the command exited <n> and its console says "<字樣>"`；`low_disk: the command exited <n> with <x> GiB free (below --min-free-gib <G>); the disk may have filled`；人看的行尾 `; free space, then --resume`。

## Review Focus

1. 絕對路徑或含 `**` 的 `--checkpoints` / `--final`（`D:/ckpt/**/*.pt`）：量的是它指的磁碟區，不被接到 `--cwd` 底下 → Task 6 `test_an_absolute_or_recursive_glob_is_measured_where_it_points`。
2. console 最後 64 KiB 的起點切在多位元組 UTF-8 字元中間（中文 log）：不丟例外、照樣找到 ASCII 字樣 → Task 7 `test_a_tail_that_starts_inside_a_multibyte_character_still_reads`。
3. `--record " S1 , S2 ,"`（空白、尾逗號）：記成 `["S1","S2"]`，冪等看去空白後的集合 → Task 3 `test_select_cli_verdict_json_and_blanks`。
4. `upload --no-sync`：沒有上傳前同步也印 `cli_warnings=`（只算回讀）；同步之前就 FAIL 的（`not_staged:`）不帶 → Task 5 `test_no_sync_counts_read_back_warnings_and_early_failures_carry_none`。
5. 登記在 data root 以外（絕對路徑）的 checkpoint，與 `stat` 前就被換走的 `.tmp`：`train status` 用絕對路徑列、不報錯 → Task 8 `test_a_checkpoint_outside_the_data_root_has_its_tmp_listed_by_absolute_path`。

兩個新模組：`src/vcp/submit/select.py`（T2、T3；不 import `report`、`final`，它們 import 它）與 `src/vcp/train/disk.py`（T6、T7）。其餘都是改既有檔，見各 task 的 Files。

---

### Task 1: `replace_text` 與 `dump_yaml_model` 的原子替換（§5.6，R3、C2）

**Files:**
- Modify: `src/vcp/core/atomic.py`（加常數與 `replace_text`；docstring 加一句「允許覆寫的同胞」）、`src/vcp/core/config.py:141-144`
- Test: `tests/unit/core/test_atomic.py`、`tests/unit/core/test_config.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Produces: `replace_text(path: Path, text: str) -> None`（UTF-8，換行照給；任何例外刪暫存檔、原樣丟出，`OSError` 不轉換）；`REPLACE_RETRY_DELAYS: tuple[float, ...]`；`IS_WINDOWS: bool = os.name == "nt"`。測試 patch `atomic.IS_WINDOWS`，**不 patch `os.name`**：POSIX 上改成 `"nt"` 會讓 `Path()` 建出 `WindowsPath` 而丟例外。`dump_yaml_model` 簽名不變。

- [ ] **Step 1: 寫會失敗的測試**

`test_atomic.py` 加兩個 helper：`_full(*_)` 丟 `OSError(errno.ENOSPC, "No space left on device")`；`_flaky(monkeypatch, fail_times, *, windows) -> (calls, sleeps)` 把 `atomic.os.replace` 換成「前 `fail_times` 次丟 `PermissionError(13, "used by another process")`、之後呼叫真的 `os.replace` 並記下 `src`」，設 `atomic.IS_WINDOWS = windows`，`atomic.time.sleep` 換成 `sleeps.append`。以下 `target` 先寫 `b"old\n"`。

```python
def test_replace_text_overwrites_and_leaves_no_temp(tmp_path):  # target = tmp_path/"a"/"t.yaml"，寫兩次
    assert target.read_bytes() == "中文\ntwo\n".encode()
    assert [p.name for p in target.parent.iterdir()] == ["t.yaml"]
@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_a_failed_replace_keeps_the_old_file_whole_and_no_temp(tmp_path, monkeypatch, step):
    monkeypatch.setattr(atomic.os, step, _full)  # pytest.raises(OSError) as ei
    assert ei.value.errno == errno.ENOSPC and not isinstance(ei.value, VcpError)  # C2
    assert target.read_bytes() == b"old\n" and [p.name for p in tmp_path.iterdir()] == ["t.yaml"]
def test_windows_retries_a_shared_target_then_replaces(tmp_path, monkeypatch):  # _flaky(…, 1, windows=True)
    assert target.read_bytes() == b"new\n" and len(calls) == 2 and sleeps == [0.05]
def test_windows_gives_up_after_five_retries_and_keeps_the_old_file(tmp_path, monkeypatch):  # 99 次、raises PermissionError
    assert sleeps == list(atomic.REPLACE_RETRY_DELAYS) == [0.05, 0.1, 0.2, 0.4, 0.8]
    assert len(calls) == 6 and target.read_bytes() == b"old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["t.yaml"]
def test_posix_does_not_retry_a_permission_error(tmp_path, monkeypatch):  # 99 次、windows=False
    assert len(calls) == 1 and sleeps == []
def test_the_replace_temp_name_is_one_is_tmp_name_knows(tmp_path, monkeypatch):  # _flaky(…, 0, windows=False)
    assert atomic.is_tmp_name(Path(calls[0]).name) and Path(calls[0]).name.startswith(".t.yaml.")
```

```python
# test_config.py
def test_dump_yaml_model_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    # dump M(name="old")；patch atomic.os.replace 為 ENOSPC；dump M(name="new") → raises OSError
    assert p.read_bytes() == before and [x.name for x in tmp_path.iterdir()] == ["m.yaml"]
@pytest.mark.parametrize(("model", "golden"), GOLDEN)  # golden：鎖現狀，改程式前後都綠
def test_dump_yaml_model_bytes_are_the_0_15_1_bytes(tmp_path, model, golden):
    dump_yaml_model(model, tmp_path / "x.yaml")
    assert (tmp_path / "x.yaml").read_bytes() == golden
```

`GOLDEN` 三項：`TrainRecord(run_id="r1", dataset="tiny", plan_id="fixed-v1", trained_on=["train"], config_hash="ab" * 32, cwd="work", command=["python", "train.py"])`；`RunCard(run_id="r1", dataset="tiny", samples_hash="cd" * 32, plan_id="fixed-v1", trained_on=["train"], source=RunSource(framework="fake"), created_at=STAMP)`；`PlatformProfile(dataset="t", eval_dataset="d", plan_id="fixed-v1", sealed_subset="holdout", platform="manual", board_rule="best", metric="accuracy", writer="scores_csv", created_at=STAMP)`。期望值在改 `config.py` 之前用 `dump_yaml_text(model).encode("utf-8")` 印出、貼成字面 bytes；profile 那一項也證明 Task 2 的預設 `selection_warn_hours` 不寫出。

```python
# test_cli_eval.py（C2：其他命令的契約不變）
def test_ingest_that_cannot_write_run_yaml_aborts_with_the_oserror_text(roots, tmp_path, monkeypatch):
    # seed_det；patch atomic.os.replace：dst 名為 run.yaml 時丟 ENOSPC，其餘呼叫真的；ingest_perfect(… "m1", "valA")
    assert r.exit_code == 2 and "status=ABORT" in v
    assert "OSError: [Errno 28] No space left on device" in v
    assert "disk_evidence" not in v and "disk_full" not in v
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/core tests/unit/test_cli_eval.py -q`；Expected: FAIL（`no attribute 'replace_text'`、`DID NOT RAISE`、ingest `exit_code == 0`）；golden 綠。
- [ ] **Step 3: 實作 `replace_text(path: Path, text: str) -> None` 與 `_replace_retrying(src: Path, dst: Path) -> None`**

mkdir 父目錄 → `_tmp_path(target)` → `"wb"` 寫 `text.encode("utf-8")` → flush、`os.fsync` → `_replace_retrying`（`IS_WINDOWS` 且 `PermissionError` 才照 `REPLACE_RETRY_DELAYS` 睡了再試）→ 外層 `except BaseException` 刪暫存檔、`raise` → 成功後 `_fsync_dir`。`dump_yaml_model` 改成 `replace_text(path, dump_yaml_text(model))`。

- [ ] **Step 4: 跑綠** — 同 Step 2 的命令；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/core/atomic.py src/vcp/core/config.py tests/unit/core/test_atomic.py tests/unit/core/test_config.py tests/unit/test_cli_eval.py`；`git commit -m "fix(core): rewrite yaml files through an atomic replace"`。

### Task 2: 事件 `selected`、`selection_warn_hours`、`selection_state`（§3.1、§3.3、§3.4，C1、C4）

**Files:**
- Modify: `src/vcp/submit/schema.py:32-57`（`Event`、`_REQUIRED`）、`:98-164`（`PlatformProfile`）、`:311-322`（`_shape`）；`src/vcp/submit/ledger.py`（`latest_final` 之後）
- Create: `src/vcp/submit/select.py`
- Modify: `tests/submit_fixtures.py`
- Test: `tests/unit/submit/test_schema.py`；Create `tests/unit/submit/test_select.py`

**Interfaces:**
- Produces:
  - `Event` 多 `"selected"`；`_REQUIRED["selected"] = ("chosen", "source")`；`_shape` 另驗 `selected needs at least one entry`、`selected has duplicate entries`。
  - `DEFAULT_SELECTION_WARN_HOURS: float = 48.0`；`PlatformProfile.selection_warn_hours: float | None = DEFAULT_SELECTION_WARN_HOURS`（`field_validator`：`None` 放行，否則有限且 `>= 0`，訊息 `selection_warn_hours must be a finite number >= 0, got <v>`）；序列化器 `_omit_the_default_ledger` 改名 `_omit_what_older_vcp_cannot_read`，另略過等於預設的值。
  - `SubmissionLedger.latest_selected() -> LedgerRow | None`（台帳順序的最後一列）。
  - `label(row: LedgerRow) -> str`（id，或 `foreign:<platform_ref>`）。
  - `@dataclass(frozen=True) SelectionState`：`chosen: list[str] | None`、`recorded_at: str | None`、`words: str`、`expected: int`、`final: list[str] | None`、`due_at: str | None`、`slots: int`、`deadline: str | None`；property `warns -> bool`（`words not in ("none", "ok")`）；`to_json() -> dict[str, Any]`（只有前六個鍵）。
  - `selection_state(ledger: SubmissionLedger, profile: PlatformProfile, now: datetime) -> SelectionState`
  - `selection_lines(state: SelectionState) -> list[str]`（`none`／`ok` → `[]`；依 `missing,slots,differs` 各一行）；`differs_line(final: list[str], recorded: list[str]) -> str`（空的一邊印 `-`）。
  - `tests/submit_fixtures.py`：`staged_row(sid)`（`kind="candidate"`、`eval_run="good"`、`gate=Gate(admission="PASS")`、`profile_sha256="p"*64`）、`uploaded_row(sid, at)`（`source="manual"`、`confirmed=True`）、`foreign_row(ref, at)`（`file_name="x.csv"`）、`final_row(chosen, slots)`（`rule="best_sealed"`、`table=[]`、`metric="accuracy"`、`params={}`、`holdout_unseals=0`）、`selected_row(chosen, *, ts=None)`（`source="manual"`）、`ledger_of(directory: Path, *rows) -> SubmissionLedger`、`seed_ledger(roots, profile, *rows) -> DatasetPaths`（`dump_yaml_model` 寫 `submit.yaml`，列 append 進 configs 台帳）；`ts` 預設 `stamp()`。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_schema.py
SELECTED = dict(event="selected", ts=STAMP, chosen=["S5", "foreign:k9"], source="manual")
def test_a_selected_row_carries_chosen_and_source():
    row = LedgerRow(**SELECTED, message="ticked on the web")
    assert "selected" in EVENTS and row.chosen == ["S5", "foreign:k9"] and row.source == "manual"
@pytest.mark.parametrize(("over", "words"), [
    ({"chosen": None}, "selected needs"), ({"source": None}, "selected needs"),
    ({"chosen": []}, "selected needs at least one entry"),
    ({"chosen": ["S1", "S1"]}, "selected has duplicate entries")])
def test_a_selected_row_without_a_proper_choice_fails(over, words):  # raises ValidationError, match=re.escape(words)
def test_selection_warn_hours_defaults_to_48_and_is_written_only_when_changed(tmp_path):
    assert _profile().selection_warn_hours == 48 and "selection_warn_hours" not in _profile().model_dump(mode="json")
    assert _profile(selection_warn_hours=72).model_dump(mode="json")["selection_warn_hours"] == 72
    assert _profile(selection_warn_hours=None).model_dump(mode="json")["selection_warn_hours"] is None
    # dump/load 來回：0 讀回 0；沒有這個鍵的舊檔讀回 48
@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_selection_warn_hours_must_be_finite_and_not_negative(tmp_path, bad):
    # match 這句（不是 extra_forbidden）：_profile(selection_warn_hours=bad) → ValidationError；
    # submit.yaml 末尾 append "selection_warn_hours: -1\n" 後 load_yaml_model → ValidationFailed
    words = "selection_warn_hours must be a finite number >= 0"
```

`test_select.py`：`NOW = parse_stamp("2026-10-20T00:00:00.000Z")`；`_at(h) = stamp(NOW + timedelta(hours=h))`；`_profile(**over)`（`dataset=TEST`、`platform="manual"`、`board_rule="best"`、`final_slots=2`、`deadline=None`）；`up(*sids)` = 依序 `uploaded_row(sid, _at(-5 + i))`。

```python
@pytest.mark.parametrize(("hours_left", "warn", "words"), [
    (72, 48, "none"), (47, 48, "missing"), (-1, 48, "missing"),
    (71, 72, "missing"), (1, None, "none"), (-1, None, "none")])
def test_missing_starts_selection_warn_hours_before_the_deadline(tmp_path, hours_left, warn, words):
    st = selection_state(ledger_of(tmp_path, *up("S1", "S2")),
                         _profile(deadline=_at(hours_left), selection_warn_hours=warn), NOW)
    assert st.words == words and st.chosen is None
    assert st.due_at == (None if warn is None else _at(hours_left - warn))
def test_without_a_deadline_the_final_row_is_the_moment_to_select(tmp_path):
    # 沒有 final → "none"；append fin = final_row(["S1"], 1) 之後：
    assert st.words == "missing" and st.due_at == fin.ts
def test_a_selection_equal_to_final_with_the_right_count_is_ok(tmp_path):
    # up S1 S2、final ["S1","S2"] slots 2、selected ["S2","S1"]
    assert st.to_json() == {"chosen": ["S2", "S1"], "recorded_at": sel.ts, "words": "ok",
                            "expected": 2, "final": ["S1", "S2"], "due_at": fin.ts} and not st.warns
def test_expected_is_never_more_than_the_entries_there_are(tmp_path):  # up S1；selected ["S1"]
    assert st.words == "ok" and st.expected == 1
def test_the_slots_of_the_newest_final_row_win_over_final_slots(tmp_path):  # up S1 S2；final ["S1"] slots 1；sel ["S1"]
    assert st.words == "ok" and st.expected == 1 and st.slots == 1
def test_count_and_set_both_wrong_say_slots_then_differs(tmp_path):  # up S1 S2 S3；final ["S1"] slots 1；sel ["S2","S3"]
    assert st.words == "slots,differs" and st.warns
def test_without_a_final_row_nothing_is_compared_with_it(tmp_path):  # final_slots=1；up S1 S2；sel ["S2"]
    assert st.words == "ok"
def test_the_newest_selected_row_is_the_selection(tmp_path):  # final ["S1"] slots 1；sel ["S2"]，再 sel ["S1"]
    assert st.words == "ok" and st.recorded_at == second.ts
def test_foreign_entries_count_and_are_labelled_foreign_ref(tmp_path):  # up S1 + foreign_row("k9")；sel ["S1","foreign:k9"]
    assert st.words == "ok" and st.expected == 2
def test_board_rule_last_is_never_checked(tmp_path):  # board_rule="last"、deadline=_at(-1)
    # 沒選取 → "none"；final ["S1"] + sel ["S2"] →
    assert st.words == "none" and st.chosen == ["S2"] and not st.warns
def test_selection_lines_and_differs_line(tmp_path):
    missing = selection_state(ledger_of(tmp_path), _profile(deadline=_at(24)), NOW)
    assert selection_lines(missing) == ["selection: missing (deadline 2026-10-21T00:00:00.000Z, "
                                        "warning since 2026-10-19T00:00:00.000Z)"]
    # 上面 slots,differs 的狀態：
    assert selection_lines(both) == [
        "selection: 2 recorded, 1 expected (slots=1)",
        "selection: final chose S1; recorded S2,S3 (not selected: S1; selected, not final: S2,S3)"]
    assert differs_line(["S1", "S2"], ["S1"]) == (
        "selection: final chose S1,S2; recorded S1 (not selected: S2; selected, not final: -)")
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit/test_schema.py tests/unit/submit/test_select.py -q`；Expected: FAIL（`selected` 不在 `Event`、`extra_forbidden`、`ModuleNotFoundError: vcp.submit.select`）。
- [ ] **Step 3: 實作上列 Interfaces**

`selection_state` 逐條照 §3.4：`board_rule != "best"` → `words="none"`、其餘照算；`due_at` = `stamp(parse_stamp(deadline) - timedelta(hours=h))`／沒有截止時最新 `final` 列的 `ts`／`None`；條目數 = `len({label(r) for r in ledger.arrivals()})`。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit -q`；Expected: PASS（含 `test_event_requirements_cover_exactly_the_event_type`）。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/schema.py src/vcp/submit/ledger.py src/vcp/submit/select.py tests/submit_fixtures.py tests/unit/submit/test_schema.py tests/unit/submit/test_select.py`；`git commit -m "feat(submit): selected ledger event and the shared selection state"`。

### Task 3: `vcp submit select --record`（§3.2、§7）

**Files:**
- Modify: `src/vcp/submit/select.py`、`src/vcp/cli_submit.py`（`final_cmd` 之後加 `select_cmd`）
- Test: `tests/unit/submit/test_select.py`、`test_transactions.py`、`test_adopt.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Consumes: Task 2 全部。
- Produces: `@dataclass(frozen=True) SelectOutcome(row: LedgerRow, written: bool, state: SelectionState)`；`select(dataset: str, entries: list[str], *, message: str | None = None, data_root: Path | None = None, configs_root: Path | None = None) -> SelectOutcome`；CLI `vcp submit select --dataset T --record S5,S3 [--message M]`（`--record` 經 `parse_csv`）。

- [ ] **Step 1: 寫會失敗的測試**

`test_select.py` 的 `_seeded(roots, **over)` = `seed_ledger(roots, _profile(**over), staged_row("S1"), staged_row("S2"), staged_row("S3"), uploaded_row("S1", stamp()), uploaded_row("S2", stamp()), foreign_row("k9", stamp()))`；`KW = {"data_root": roots.data, "configs_root": roots.configs}`。

```python
def test_select_records_the_ticked_ids_once(roots):  # _seeded(roots, final_slots=3)
    out = select(TEST, ["S1", "S2", "foreign:k9"], message="web", **KW)
    assert out.written and (out.row.event, out.row.source, out.row.message) == ("selected", "manual", "web")
    assert out.row.chosen == ["S1", "S2", "foreign:k9"] and out.state.words == "ok"
    again = select(TEST, ["foreign:k9", "S2", "S1"], **KW)
    assert not again.written and again.row == out.row
    assert [r.event for r in SubmissionLedger(paths.submissions_log).rows].count("selected") == 1
@pytest.mark.parametrize(("entries", "word"), [([], "invalid:"), (["S1", "S1"], "invalid:"),
    (["S9"], "not_staged:"), (["S3"], "not_uploaded:"), (["foreign:zz"], "not_found:")])
def test_select_refuses_and_writes_nothing(roots, entries, word):  # raises ValidationFailed, match=f"^{word}"
    assert paths.submissions_log.read_bytes() == before
    if word != "invalid:":
        assert ei.value.fields["id"] == entries[0]
def test_select_records_after_final_locked_the_ledger(roots):  # + final_row(["S1"],1)、lock 列 reason="final"；manual
    assert select(TEST, ["S1"], **KW).written
def test_select_records_a_wrong_count_or_a_choice_unlike_final_and_warns(roots):  # + final_row(["S1"], 1)
    assert select(TEST, ["S1", "S2"], **KW).state.words == "slots"
    assert select(TEST, ["S2"], **KW).state.words == "differs"  # 兩次都 written
def test_board_rule_last_records_and_never_checks(roots):  # board_rule="last" + final_row(["S1"], 1)
    out = select(TEST, ["S2"], **KW)
    assert out.written and out.state.words == "none"
```

```python
# test_transactions.py（沿用 _paths、_profile、_hold、_release、_verdict、lockmod）
def test_select_aborts_while_another_process_holds_the_ledger(roots, monkeypatch):
    # dump _profile()；WAIT_SECONDS 0.3、RETRY_SECONDS 0.05；_hold 台帳鎖；invoke submit select --record S1；_release
    assert r.exit_code == 2 and "status=ABORT" in v and "locked:" in v and f"dataset={TEST}" in v
    assert not paths.submissions_log.exists()
# test_adopt.py（§7：兩個 checkout 各記一次，兩列都留、ts 新的為準；合併邏輯既有，可能直接綠）
def test_adopt_keeps_both_selections_and_the_newer_one_wins(tmp_path):
    older = selected_row(["S1"], ts="2026-10-20T00:00:00.000Z")
    newer = selected_row(["S2"], ts="2026-10-20T01:00:00.000Z")
    rows, duplicates = merge_ledgers([[newer], [older]])
    assert rows == [older, newer] and duplicates == 0
    assert ledger_of(tmp_path, *rows).latest_selected().chosen == ["S2"]
# test_cli_submit.py
def test_select_cli_verdict_json_and_blanks(roots):  # seed_ledger：best、final_slots=2、S1 S2 staged 且上傳
    # --record " S1 , S2 ,"
    assert r.exit_code == 0 and "status=OK" in v and "selected=2" in v and "written=true" in v
    assert "selection=ok" in v and "ledger=configs" in v and "dataset=beach-test" in v
    # --record S2,S1 --json
    assert doc["fields"]["written"] is False and doc["result"]["chosen"] == ["S1", "S2"]
    assert doc["result"]["event"] == "selected" and doc["result"]["selection"]["words"] == "ok"
    # --record S1
    assert "status=WARN" in v and "selection=slots" in v
    assert "selection: 1 recorded, 2 expected (slots=2)" in r.output
    # submit.yaml 改 ledger: shared、沒 adopt
    assert r.exit_code == 1 and "not_adopted:" in v
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -k "select" -q`；Expected: FAIL（`cannot import name 'select'`、`No such command 'select'`）。
- [ ] **Step 3: 實作 `select(...)` 與 `select_cmd`**

`load_profile` → `transaction(paths, profile, command="submit.select")` 內依序：空 → `invalid: --record needs at least one id or foreign:<ref>`；重複 → `invalid: --record names <x> twice`；每項依輸入順序：`foreign:<ref>` 的 ref 不在 `ledger.foreign_refs()` → `not_found: foreign:<ref> is not a foreign upload in the ledger`；id 沒有 staged 列 → `not_staged: '<id>' has no staged row`；沒有 uploaded 列 → `not_uploaded: '<id>' has no uploaded row; the platform cannot select it`（三者 `fields={"id": entry}`）。最新 `selected` 的集合相同 → 不寫；否則 append（`ts=stamp()`、`source="manual"`、`chosen` 照輸入順序、`message`）。最後 `selection_state(ledger, profile, utc_now())`。不呼叫 `assert_unlocked`、不看截止、不呼叫平台。CLI：`fields = {dataset, selected=len(row.chosen), written, selection=state.words, ledger}`，`state.warns` → WARN，human = `selection_lines(state)`，result = `{**row.model_dump(mode="json", exclude_none=True), "selection": state.to_json()}`，`context={"dataset": dataset}`。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/select.py src/vcp/cli_submit.py tests/unit/submit/test_select.py tests/unit/submit/test_transactions.py tests/unit/submit/test_adopt.py tests/unit/test_cli_submit.py`；`git commit -m "feat(submit): submit select records the platform's final selection"`。

### Task 4: `status`、`report`、`final` 讀選取（§3.5，C1、C4）

**Files:**
- Modify: `src/vcp/submit/report.py:30-174`（`_label` 改用 `select.label`）、`src/vcp/submit/final.py:258-266,435-451`、`src/vcp/cli_submit.py`（`final_cmd`、`status_cmd`、`report_cmd`）
- Test: `tests/unit/submit/test_report.py`、`test_final.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Consumes: `label`、`selection_state`、`selection_lines`、`differs_line`、`latest_selected()`。
- Produces: `StatusView.selection: SelectionState`（最後一欄）；`ReportRow.selected: bool = False`（最後、有預設）；`FinalResult.selection: str | None = None`（只會是 `"differs"`）、`recorded: list[str] | None = None`（只在 differs 時）、`next_step: str | None = None`；`final.next_step_hint(profile: PlatformProfile, chosen: list[str]) -> str` = `f"select {c} on the platform, then: vcp submit select --dataset {profile.dataset} --record {c}"`（`c = ",".join(chosen)`）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_report.py
def test_status_carries_the_selection_state(pair):
    _seed(pair, _profile(board_rule="best", deadline=stamp(utc_now() + timedelta(hours=10))))
    assert status(TEST, **_kw(pair)).selection.words == "missing"
    # append selected_row(["S1"])
    assert st.selection.words == "ok" and st.selection.chosen == ["S1"]
def test_report_marks_every_upload_of_a_selected_id_and_foreign_too(pair):  # _seed(pair, _profile())
    assert not any(r.selected for r in report(TEST, **_kw(pair)))
    # 再 record S1 一次；append foreign_row("f1", <更晚>)、selected_row(["S1", "foreign:f1"])
    assert all(r.selected == (r.submission_id in {"S1", "foreign:f1"}) for r in rows)
    assert sum(r.selected for r in rows) == 3
# test_final.py；_best(uploaded) = dump_yaml_model(_profile(board_rule="best"), submit.yaml) + _measure_holdout good、bad
def test_final_on_best_prints_next_and_warns_only_when_the_recorded_selection_differs(uploaded):
    dry = final(TEST, dry_run=True, **_kw(uploaded))
    assert dry.selection is None and dry.recorded is None
    assert dry.next_step == "select S1 on the platform, then: vcp submit select --dataset beach-test --record S1"
    # append selected_row(["S2"])；res = final(dry_run=True)
    assert res.selection == "differs" and res.recorded == ["S2"]
    assert res.row.model_dump_json(exclude={"ts"}) == dry.row.model_dump_json(exclude={"ts"})  # golden
    # append selected_row(["S1"])；done = final(TEST)
    assert done.selection is None and done.next_step is not None and done.chosen == ["S1"]
def test_final_on_last_has_no_next_step_and_no_selection(uploaded):  # 量 holdout；plain = dry；append sel ["S2"]；res = dry
    assert res.selection is None and res.next_step is None and res.recorded is None
    assert res.row.model_dump_json(exclude={"ts"}) == plain.row.model_dump_json(exclude={"ts"})
# test_cli_submit.py
def test_status_cli_prints_selected_and_selection_and_warns(roots):
    # seed_ledger：best、final_slots=1、quota=Quota(per_day=5)、deadline=now+30h、selection_warn_hours=100；
    # staged_row("S1")、uploaded_row("S1", stamp())、scored 列（source="manual"、public=0.5）
    assert "status=WARN" in v and "selected=none" in v and "selection=missing" in v
    assert "selection: missing (deadline " in r.output and "cli_warnings" not in v  # R1
    assert set(doc["result"]["selection"]) == {"chosen", "recorded_at", "words", "expected", "final", "due_at"}
    # append selected_row(["S1"])
    assert "status=OK" in v and "selected=1" in v and "selection=ok" in v
def test_final_and_report_cli_render_the_selection(roots, monkeypatch):
    # cli_submit.final → FinalResult(final_row(["S1"], 1), ["S1"], [], None, [], True, selection="differs",
    #   recorded=["S2"], next_step=next_step_hint(<best profile>, ["S1"]))；cli_submit.report →
    #   [ReportRow("S1", STAMP, "candidate", 0.8, None, None, None, None, errored=True, selected=True)]；ledger_mode → "configs"
    assert "status=WARN" in v_final and "selection=differs" in v_final
    assert "selection: final chose S1; recorded S2 (not selected: S1; selected, not final: S2)" in r_final.output
    assert "next: select S1 on the platform, then: vcp submit select --dataset beach-test --record S1" in r_final.output
    assert next(x for x in r_report.output.splitlines() if " S1 " in x).endswith(" errored selected")
    assert doc_report["result"]["rows"][0]["selected"] is True
```

既有 `test_final_status_report_cli`（manual、`last`）的 `final --dry-run` 段加：`assert "next:" not in r.output and "selection=" not in v`。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit/test_report.py tests/unit/submit/test_final.py tests/unit/test_cli_submit.py -q`；Expected: FAIL（`selection`、`selected`、`next_step` 不存在）。
- [ ] **Step 3: 實作**

`status()` 用已有的 `now` 呼叫 `selection_state`；`report()` 先取 `latest_selected()` 的集合，每列 `selected=label(r) in chosen`。`_final_locked`：只在 `board_rule == "best"`，寫 `final` 列之前讀 `latest_selected()`，集合跟這次 `chosen` 不同 → `selection="differs"`、`recorded=那列的 chosen`；`next_step = next_step_hint(profile, chosen)`（`--dry-run` 也給）；`row` 一個位元組都不動。CLI：`status_cmd` 一律 `selected=<n>|none`、`selection=`，`warn |= st.selection.warns`，human 加 `selection_lines`，payload 加 `"selection": to_json()`；`report_cmd` 行尾在 ` errored` 之後加 ` selected`；`final_cmd` 在 `res.selection` 時加欄位、印 `differs_line(res.chosen, res.recorded)`、併入 WARN，`res.next_step` 時印 `next: <next_step>`。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_e2e_shared_ledger.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/submit/report.py src/vcp/submit/final.py src/vcp/cli_submit.py tests/unit/submit/test_report.py tests/unit/submit/test_final.py tests/unit/test_cli_submit.py`；`git commit -m "feat(submit): status, report and final read the recorded selection"`。

### Task 5: `PlatformList` 與 `cli_warnings=`（§4，R1）

**Files:**
- Modify: `src/vcp/core/errors.py:69-72`；`src/vcp/submit/platforms/base.py`、`__init__.py`、`kaggle.py:136-346`、`manual.py:30-33`；`src/vcp/submit/sync.py:51-66,266-282`、`actions.py:89-164`；`src/vcp/cli_submit.py`（`upload_cmd`、`sync_cmd`）；`tests/submit_fixtures.py`
- Test: `tests/unit/submit/test_platforms.py`、`test_sync.py`、`test_actions.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Produces:
  - `base.PlatformList(submissions: list[PlatformSubmission], warnings: tuple[str, ...] = ())`（frozen，從 `vcp.submit.platforms` 匯出）；`Platform.list_submissions(...) -> PlatformList`。
  - `as_platform_list(value: PlatformList | list[PlatformSubmission]) -> PlatformList`；`merge_warnings(*groups: Iterable[str]) -> tuple[str, ...]`（不重複、首見順序）。
  - `UploadResult.cli_warnings: tuple[str, ...] = ()`（最後一欄）；`PlatformError(message, *, location=None, fields=None, cli_warnings: tuple[str, ...] = ())`，屬性 `.cli_warnings`（回讀後的平台 FAIL 用它帶回讀看到的行，`fields` 不變）。
  - `kaggle._past_warnings(text) -> tuple[str, tuple[str, ...]]`；`parse_submissions(text) -> tuple[list[PlatformSubmission], str | None, tuple[str, ...]]`；`KagglePlatform._read_back(...) -> tuple[str | None, Readback, tuple[str, ...]]`。
  - `SyncResult.cli_warnings`、`UploadOutcome.cli_warnings`：`tuple[str, ...] = ()`。
  - `cli_submit.OUTDATED_HINT = "the platform CLI says it is outdated; upgrade the CLI that kaggle_command runs"`；`cli_warning_lines(lines: tuple[str, ...]) -> list[str]`（每行 `cli_warning: <line>`，任一行含 `outdated`（不分大小寫）再加 `OUTDATED_HINT`）。
  - `submit_fixtures.OUTDATED_WARNING`：`test_platforms.py` 的 `OUTDATED` 原文搬來，那邊改成 `OUTDATED = OUTDATED_WARNING`。

- [ ] **Step 1: 寫會失敗的測試，並改既有斷言**

```python
# test_platforms.py
def test_parse_returns_the_redacted_warning_lines_it_skipped():
    subs, token, warnings = parse_submissions(f"{OUTDATED}\n\n  Warning: {SECRET}\n{json.dumps(rows)}")
    assert len(subs) == 1 and token is None and warnings == (OUTDATED, "Warning: <redacted>")
    assert parse_submissions(json.dumps(rows))[2] == ()
def test_a_warning_printed_on_every_page_is_one_line():  # page1（nextPageToken）、page2，各自前面印 OUTDATED
    assert isinstance(listed, PlatformList) and listed.warnings == (OUTDATED,)
    assert [s.file_name for s in listed.submissions] == ["a.csv", "b.csv"]
def test_read_back_warnings_ride_the_upload_result(no_wait):
    # FakeRunner([(0, KERNEL_REPLY, ""), _listing((51234, "S1", 0)) 的 stdout 前面加 OUTDATED])
    assert res.confirmed and res.cli_warnings == (OUTDATED,)
def test_a_failed_upload_carries_its_read_back_warnings(no_wait):
    # FakeRunner([CLI_DOWN, *[(0, f"{OUTDATED}\nNo submissions found", "")] * LOOKS]) → raises PlatformError "upload_failed:"
    assert ei.value.cli_warnings == (OUTDATED,) and ei.value.fields == {"exit_code": 1, "readback": "not_listed"}
```

既有斷言改型別：`parse_submissions` 兩元拆包改三元（`== ([], None)` 改 `== ([], None, ())`；有警告那行改 `(OUTDATED,)`）；`list_submissions(...)` 的迭代與 `== []` 改用 `.submissions`。

```python
# test_sync.py
def test_sync_verdict_counts_cli_warnings_and_warns_every_time(pair, monkeypatch):
    # _only_s1(pair, kaggle_command=[sys.executable])；kaggle.timed_runner → 一律回
    # CompletedProcess([], 0, f"{OUTDATED_WARNING}\n[]", "")；before = 台帳 bytes；first = sync；again = sync --json
    for r in (first, again):  # 沒升級就每一次都 WARN
        assert r.exit_code == 0 and "status=WARN" in v_of(r) and "cli_warnings=1" in v_of(r)
    assert json.loads(again.stdout)["result"]["cli_warnings"] == [OUTDATED_WARNING]
    assert f"cli_warning: {OUTDATED_WARNING}" in first.output and OUTDATED_HINT in first.output
    assert _ledger(pair).path.read_bytes() == before  # 警告不寫台帳、沒有 note
    # 換成 "Warning: maintenance tonight\n[]"：cli_warnings=1、WARN、沒有 OUTDATED_HINT
def test_a_list_returning_platform_still_syncs(pair, monkeypatch):
    # _only_s1(pair)；monkeypatch.setitem(PLATFORMS, "kaggle", OldStyle())，其 list_submissions 回 []
    assert res.cli_warnings == () and res.platform_rows == 0
# test_actions.py；WARNED_EMPTY = (0, f"{OUTDATED_WARNING}\nNo submissions found", "")
def test_upload_counts_the_warnings_of_the_pre_sync_and_the_read_back_once(pair, no_wait):
    # kaggle profile（per_day=1）；FakeRunner([WARNED_EMPTY, (0, "queued", ""), 前面加警告的列表：
    #   {"ref": 7, "fileName": "submission.csv", "date": stamp(utc_now()), "description": "S1"}])
    assert upload(TEST, "S1", runner=runner, **_kw(pair)).cli_warnings == (OUTDATED_WARNING,)
    # upload S2 with FakeRunner([WARNED_EMPTY]) → raises "quota_exhausted"
    assert ei.value.fields["cli_warnings"] == 1
def test_no_sync_counts_read_back_warnings_and_early_failures_carry_none(pair, no_wait):
    # no_sync=True，CLI 回 "Successfully submitted to c1"
    assert out.cli_warnings == () and out.sync == "skipped"
    # upload "S9" → raises "not_staged"
    assert "cli_warnings" not in ei.value.fields
```

既有 `test_upload_failures_write_no_row` 的 `fields == {...}` 加 `"cli_warnings": 0`。

```python
# test_cli_submit.py（同 test_upload_verdict_carries_... 的 monkeypatch 寫法）
def test_upload_cli_prints_cli_warnings_and_the_upgrade_hint(roots, monkeypatch):
    # fake upload → UploadOutcome(row, UploadResult(True, "7", "ok"), None, cli_warnings=(OUTDATED_WARNING,))
    assert r.exit_code == 0 and "status=WARN" in v and "cli_warnings=1" in v and OUTDATED_HINT in r.output
    # cli_warnings=() 的版本："status=OK" in v and "cli_warnings=0" in v
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -q`；Expected: FAIL（`PlatformList` 不存在、三元拆包 `ValueError`、`cli_warnings` 不存在）。
- [ ] **Step 3: 實作上列 Interfaces**

Kaggle：`list_submissions` 逐頁 `merge_warnings`；`_read_back` 每次取 `.submissions` 並累積 `.warnings`，例外分支回傳到那時為止的行；`upload`、`_after_failure` 把它們放進 `UploadResult(cli_warnings=...)` 或 `PlatformError(..., cli_warnings=...)`。`sync()` 與 `actions._pre_sync` 用 `as_platform_list(...)`，`reconcile` 簽名不變、吃 `.submissions`，再 `dataclasses.replace(res, cli_warnings=listed.warnings)`。`upload()` 的 `except VcpError` 加 `e.fields.setdefault("cli_warnings", len(merge_warnings(pre, e.cli_warnings if isinstance(e, PlatformError) else ())))`；成功時 `merge_warnings(pre, result.cli_warnings)`。CLI：`sync_cmd`、`upload_cmd` 一律 `cli_warnings=<n>`，`> 0` → WARN，human `+= cli_warning_lines(...)`；`sync` payload 加 `"cli_warnings": list(...)`。`status` 不加欄位。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_e2e_shared_ledger.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add` 上列 Files 的每一個；`git commit -m "feat(submit): surface the platform CLI's warnings in sync and upload"`。

### Task 6: 啟動前的磁碟檢查與環境快照（§5.1、§5.2，C5）

**Files:**
- Create: `src/vcp/train/disk.py`
- Modify: `src/vcp/train/schema.py:128-143`、`env.py:102-115`、`run.py:68-118,470-675`、`src/vcp/cli_train.py:35-161`、`tests/helpers.py`（`ROOMY_BYTES = 1 << 40`）、`tests/conftest.py`
- Test: Create `tests/unit/train/test_disk.py`；`tests/unit/train/test_run.py`、`tests/unit/test_cli_train.py`

**Interfaces:**
- Produces:
  - `schema.py`：`DiskRole = Literal["run", "cwd", "checkpoints"]`；`DiskVolume(_Strict)`：`path: str`、`roles: list[DiskRole]`、`free_bytes: int | None`、`total_bytes: int | None`；`DiskCheck(_Strict)`：`min_free_gib: float`、`checked_at: str`、`volumes: list[DiskVolume]`；`EnvSnapshot.disk: DiskCheck | None = None`（最後一欄）。`env.snapshot(python, cwd, *, patch=None, disk: DiskCheck | None = None) -> EnvSnapshot`。
  - `disk.py`：`GIB = 1024**3`；`DEFAULT_MIN_FREE_GIB = 10.0`；`disk_free_total(path: Path) -> tuple[int, int]`（`shutil.disk_usage` 的 `(free, total)`）；`usage: Callable[[Path], tuple[int, int]] = disk_free_total`（可替換的探針，模組內一律經這個名字呼叫）；`device(path: Path) -> int`（`os.stat(path).st_dev`）；`glob_prefix(pattern: str) -> str`；`nearest_dir(path: Path) -> Path`；`targets(run_root: Path, cwd: Path, patterns: Iterable[str]) -> list[tuple[DiskRole, Path]]`（`cwd / glob_prefix(p)`，絕對 pattern 自然勝出）；`measure(targets, *, min_free_gib: float, data_root: Path) -> DiskCheck`（依 `device(nearest_dir(p))` 分區、首見順序、每區 `usage` 一次、`OSError` → 兩欄 `None`；`path = store_path(那區第一個目錄, data_root)`；`checked_at=stamp()`）；`lowest(check) -> DiskVolume | None`；`free_gib(check) -> float | str`（`round(最小/GIB, 1)`，全部量不到 `"unknown"`）；`unavailable(check) -> bool`；`check_min_free_gib(value: float) -> None`；`assert_room(check: DiskCheck) -> None`。
  - `RunSpec.min_free_gib: float = DEFAULT_MIN_FREE_GIB`；`RunResult.disk: DiskCheck | None = None`；`run._patterns(spec) -> list[str]`（`_finish_checkpoints` 也改用）。
  - conftest autouse `_roomy_disk(monkeypatch)`：`monkeypatch.setattr(disk, "usage", lambda path: (ROOMY_BYTES, ROOMY_BYTES))`。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_disk.py
def test_glob_prefix_stops_at_the_first_wildcard():
    assert glob_prefix("weights/*.pt") == "weights/" and glob_prefix("runs/**/best.pt") == "runs/"
    assert glob_prefix("ck[0-9]/x") == "ck" and glob_prefix("w?/x") == "w"
    assert glob_prefix("weights/best.pt") == "weights/best.pt" and glob_prefix("*.pt") == ""
def test_nearest_dir_climbs_to_an_existing_directory(tmp_path):  # tmp_path/"a"/"f.pt" 存在
    assert nearest_dir(tmp_path / "a" / "b" / "c.pt") == tmp_path / "a"
    assert nearest_dir(tmp_path / "a" / "f.pt") == tmp_path / "a" and nearest_dir(tmp_path) == tmp_path
def test_an_absolute_or_recursive_glob_is_measured_where_it_points(tmp_path):  # Review Focus 1
    got = targets(tmp_path / "runs" / "r1", cwd, [f"{other.as_posix()}/**/*.pt", "sub/**/x.pt"])
    assert [role for role, _ in got] == ["run", "cwd", "checkpoints", "checkpoints"]
    assert nearest_dir(got[2][1]) == other and nearest_dir(got[3][1]) == cwd
def test_measure_groups_by_device_and_takes_one_reading_per_volume(tmp_path, monkeypatch):
    # work/"ckpt" 存在；device：路徑含 "ckpt" → 2，否則 1；usage 記呼叫，ckpt → (5*GIB, 100*GIB)，其他 (50*GIB, 100*GIB)
    # measure([("run", tmp_path/"runs"/"r1"), ("cwd", work), ("checkpoints", work/"ckpt")], min_free_gib=10.0, data_root=tmp_path)
    assert [v.roles for v in check.volumes] == [["run", "cwd"], ["checkpoints"]] and len(calls) == 2
    assert lowest(check).free_bytes == 5 * GIB and free_gib(check) == 5.0 and not unavailable(check)
def test_an_unreadable_volume_is_null_and_all_unreadable_is_unknown(tmp_path, monkeypatch):  # usage 丟 OSError
    assert check.volumes[0].free_bytes is None and check.volumes[0].total_bytes is None
    assert unavailable(check) and free_gib(check) == "unknown" and lowest(check) is None
def test_assert_room_aborts_below_the_bar_and_never_at_zero():
    # vol = DiskVolume(path="runs", roles=["run"], free_bytes=2*GIB, total_bytes=100*GIB)；G=10 → raises "^low_disk:"
    assert ei.value.status == "ABORT" and "--min-free-gib" in str(ei.value)
    assert ei.value.fields == {"free_gib": 2.0, "min_free_gib": 10.0, "disk_path": "runs"}
    # G=0 不丟；free_bytes=None 不丟
@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_min_free_gib_must_be_finite_and_not_negative(bad):  # raises ValidationFailed, match="^invalid: --min-free-gib"
def test_the_real_probe_reads_this_disk(tmp_path):
    free, total = disk_free_total(tmp_path)
    assert total > 0 and 0 <= free <= total
```

```python
# test_run.py；_files(roots) = run_dir 下 run.yaml、train.yaml、train.log.jsonl 的 bytes
def test_train_run_aborts_before_any_write_when_a_run_disk_is_low(roots, work, monkeypatch):
    # usage → (2*GIB, 100*GIB)；raises VcpError "^low_disk:"
    assert ei.value.status == "ABORT" and set(ei.value.fields) == {"free_gib", "min_free_gib", "disk_path"}
    assert ei.value.fields["free_gib"] == 2.0 and ei.value.fields["min_free_gib"] == 10.0
    assert not run_dir(roots.data, "r1").exists()
def test_resume_on_a_low_disk_aborts_and_leaves_the_record_as_it_was(roots, work, monkeypatch):
    # 先跑 r1；before = _files；usage 2 GiB；--resume → raises "^low_disk:"
    assert _files(roots) == before
def test_input_errors_come_before_low_disk(roots, work, tmp_path, monkeypatch):
    # 先跑 r1、usage 2 GiB：同 id 不 --resume → match=RUN_EXISTS；r2 + cwd=tmp_path/"nowhere" → "not_found: --cwd"
@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_train_run_refuses_a_bad_min_free_gib(roots, work, bad):  # raises "^invalid:"
    assert not run_dir(roots.data, "r1").exists()
def test_min_free_gib_zero_never_aborts_and_still_records(roots, work, monkeypatch):  # usage 2 GiB；min_free_gib=0
    assert res.attempt.status == "finished" and env["disk"]["min_free_gib"] == 0.0
    assert env["disk"]["volumes"][0]["free_bytes"] == 2 * GIB
def test_the_env_snapshot_records_the_disk_check(roots, work):  # checkpoints=[]、final=None；env = train/env.1.json
    assert env["disk"]["min_free_gib"] == 10.0 and env["disk"]["checked_at"].endswith("Z")
    assert len(vols) == 1 and vols[0]["roles"] == ["run", "cwd"]
    assert vols[0]["free_bytes"] == ROOMY_BYTES == vols[0]["total_bytes"]
    assert res.disk.model_dump(mode="json") == env["disk"]
def test_a_glob_prefix_on_another_volume_is_its_own_entry_and_the_lowest_counts(roots, work, monkeypatch):
    # work/"ckpt"；device/usage 同 test_disk；checkpoints=["ckpt/*.pt"]、final=None、min_free_gib=1.0
    assert len(res.disk.volumes) == 2 and free_gib(res.disk) == 5.0
def test_an_unreadable_disk_warns_and_does_not_abort(roots, work, monkeypatch):  # usage 丟 OSError
    assert res.attempt.status == "finished" and "disk_check=unavailable" in res.warnings
    assert free_gib(res.disk) == "unknown"
# test_cli_train.py
def test_train_run_cli_free_gib_and_min_free_gib(roots, tmp_path, monkeypatch):
    assert "free_gib=1024.0" in _verdict(_run(work).output)
    # usage 2 GiB；_run(work, run="r2")
    assert r.exit_code == 2 and "status=ABORT" in v and "low_disk:" in v and "run=r2" in v
    assert "free_gib=2.0" in v and "min_free_gib=10.0" in v and "disk_path=" in v
    # _run(work, "--min-free-gib", "0", run="r2") → exit 0、"free_gib=2.0"
    # _run(work, "--min-free-gib=-1", run="r3") → exit 1、"invalid:"
    # usage 丟 OSError、run r4 → "status=WARN"、"disk_check=unavailable"、"free_gib=unknown"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.train.disk`、`RunSpec` 的 `extra_forbidden: min_free_gib`）。
- [ ] **Step 3: 實作上列 Interfaces，接進 `train_run`**

`check_min_free_gib(spec.min_free_gib)` 放在 `--cwd` 檢查之後。`check_names(...)` 之後、`_close_running(...)` 之前：`disk_targets = targets(run_dir(data_root, spec.run_id), cwd, _patterns(spec))`、`disk_check = measure(...)`、`assert_room(disk_check)`。`snapshot(..., disk=disk_check)`；`unavailable` → `warnings.append("disk_check=unavailable")`。訊息：`low_disk: <x.x> GiB free on <disk_path>, below --min-free-gib <G:g>; free space or move the run to another disk, or lower the bar with --min-free-gib`；`invalid: --min-free-gib must be a finite number >= 0, got <v!r>`。CLI：`--min-free-gib`（`float`，預設 `DEFAULT_MIN_FREE_GIB`，help 說 0 = 只記錄）；`fields["free_gib"] = free_gib(res.disk) if res.disk else "unknown"`；`unavailable` 時 `fields["disk_check"] = "unavailable"`。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py -q`；Expected: PASS（conftest 的大數值讓既有測試不看 runner 的空間）。
- [ ] **Step 5: Commit** — `git add` 上列 Files 的每一個；`git commit -m "feat(train): check free disk space before the first write"`。

### Task 7: attempt 失敗後的 `disk_full:`／`low_disk:`，與 vcp 自己寫不下（§5.3、§5.4，R2、C3）

**Files:**
- Modify: `src/vcp/train/disk.py`、`src/vcp/train/run.py`、`src/vcp/cli_train.py`
- Test: `tests/unit/train/test_disk.py`、`test_run.py`、`tests/unit/test_cli_train.py`

**Interfaces:**
- Consumes: Task 6 的 `measure`、`lowest`、`GIB`、`disk_targets`。
- Produces:
  - `DISK_FULL_TAIL = 64 * 1024`；`DISK_FULL_MARKERS: tuple[str, ...]`（Global Constraints 的六個，依序）；`DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})`；`DISK_FULL_WINERRORS = frozenset({112, 39})`。
  - `@dataclass(frozen=True) DiskFailure`：`key: Literal["disk_full", "low_disk"]`、`reason: str`、`marker: str | None`、`free_bytes: int | None`、`evidence: Literal["console"] | None`。
  - `console_marker(console: Path) -> str | None`（讀最後 `DISK_FULL_TAIL` bytes，`decode("utf-8", errors="replace")`、`casefold` 後依序比對，回傳清單的原字樣；檔案不在 → `None`）。
  - `diagnose(exit_code: int, console: Path, after: DiskCheck) -> DiskFailure | None`（門檻 `after.min_free_gib`；字樣 → `disk_full`；否則門檻 > 0 且 `lowest(after)` 低於門檻 → `low_disk`；`free_bytes` = `lowest(after)` 的值或 `None`）。
  - `is_disk_full(error: OSError) -> bool`；`own_writes() -> AbstractContextManager[None]`（`is_disk_full` 的 `OSError` → `VcpError("disk_full: vcp could not write its own record (<e>); free space, then --resume", fields={"disk_evidence": "vcp"}) from e`，其他原樣）；`RunResult.disk_failure: DiskFailure | None = None`。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_disk.py；_after(free, min_free=10.0) = DiskCheck(min_free_gib=min_free, checked_at=STAMP,
#   volumes=[DiskVolume(path=".", roles=["run"], free_bytes=free, total_bytes=100*GIB)])
@pytest.mark.parametrize(("line", "marker"), [
    ("OSError: [Errno 28] No space left on device", "No space left on device"),
    ("RuntimeError: [errno 28] write failed", "[Errno 28]"),
    ("OSError: [WinError 112] 磁碟空間不足。", "[WinError 112]"),
    ("OSError: [WinError 39] handle disk full", "[WinError 39]"),
    ("there is not enough space on the disk", "There is not enough space on the disk"),
    ("DISK QUOTA EXCEEDED", "Disk quota exceeded")])
def test_console_marker_finds_each_word_in_any_case(tmp_path, line, marker):
    assert console_marker(log_with(f"epoch 3\n{line}\n")) == marker
    assert console_marker(tmp_path / "missing.log") is None
def test_console_marker_reads_only_the_last_64_kib(tmp_path):
    # b"No space left on device\n" + b"x" * DISK_FULL_TAIL → None；後面再接一行字樣 → 找到
    assert DISK_FULL_TAIL == 65536
def test_a_tail_that_starts_inside_a_multibyte_character_still_reads(tmp_path):  # Review Focus 2
    body = ("中" * 30000).encode() + b"\n[Errno 28] write\n"
    assert (len(body) - DISK_FULL_TAIL) % 3 != 0 and console_marker(log_bytes(body)) == "[Errno 28]"
def test_diagnose_prefers_the_console_then_low_disk_then_nothing(tmp_path):  # full.log 有字樣；badbit.log 只有 ios_base::badbit
    assert diagnose(1, full, _after(2 * GIB)) == DiskFailure(
        key="disk_full", marker="No space left on device", free_bytes=2 * GIB, evidence="console",
        reason='disk_full: the command exited 1 and its console says "No space left on device"')
    low = diagnose(1, badbit, _after(2 * GIB))
    assert (low.key, low.marker, low.evidence) == ("low_disk", None, None)
    assert low.reason == ("low_disk: the command exited 1 with 2.0 GiB free "
                          "(below --min-free-gib 10); the disk may have filled")
    assert diagnose(1, badbit, _after(50 * GIB)) is None
    assert diagnose(1, badbit, _after(2 * GIB, min_free=0.0)) is None and diagnose(1, badbit, _after(None)) is None
def test_is_disk_full_knows_enospc_edquot_and_the_two_winerrors():
    # class Win112(OSError): winerror = 112；Win39 同理（子類別屬性在 Windows 與 POSIX 都讀得到）
    assert is_disk_full(OSError(errno.ENOSPC, "x")) and is_disk_full(OSError(errno.EDQUOT, "x"))
    assert is_disk_full(Win112(errno.EINVAL, "x")) and is_disk_full(Win39(errno.EINVAL, "x"))
    assert not is_disk_full(OSError(errno.EACCES, "x"))
def test_own_writes_turns_disk_full_into_an_abort_and_lets_others_through():
    # with own_writes(): raise OSError(errno.ENOSPC, ...) → raises VcpError "^disk_full: vcp could not write"
    assert ei.value.status == "ABORT" and ei.value.fields == {"disk_evidence": "vcp"}
    # with own_writes(): raise PermissionError(errno.EACCES, "denied") → raises PermissionError
```

```python
# test_run.py：假 child 是 [sys.executable, "-c", 腳本]
NO_SPACE = 'import sys; print("OSError: [Errno 28] No space left on device", flush=True); sys.exit(1)'
WIN_FULL = 'import sys; print("OSError: [WinError 112] The disk is full", flush=True); sys.exit(1)'
BADBIT = 'import sys; print("terminate called: ios_base::badbit", flush=True); sys.exit(1)'
UNRELATED = 'import sys; print("KeyError: fold", flush=True); sys.exit(1)'
EARLY = 'import sys; print("No space left on device"); print("x" * 70000); sys.exit(1)'
SAYS_BUT_OK = 'print("No space left on device")'
# _space_after_child(monkeypatch, gib)：child 印出第一行之前 usage 回 ROOMY_BYTES，之後回 gib*GIB；回傳要傳給 on_line 的函式
# _disk_notes(roots)：train.log.jsonl 裡 event == "note" 且 key 是 disk_full / low_disk 的列
@pytest.mark.parametrize(("script", "marker"), [(NO_SPACE, "No space left on device"), (WIN_FULL, "[WinError 112]")])
def test_a_child_that_ran_out_of_disk_fails_with_disk_full(roots, work, script, marker):
    assert res.attempt.status == "failed" and res.disk_failure.key == "disk_full"
    assert res.disk_failure.evidence == "console" and res.disk_failure.marker == marker
    assert res.disk_failure.reason.startswith("disk_full: the command exited 1 and its console says")
    [note] = _disk_notes(roots)
    assert (note["key"], note["marker"], note["free_bytes"]) == ("disk_full", marker, ROOMY_BYTES)
def test_a_badbit_child_on_a_low_disk_fails_with_low_disk(roots, work, monkeypatch):  # BADBIT + _space_after_child(…, 2)
    assert res.disk_failure.key == "low_disk" and "may have filled" in res.disk_failure.reason
    assert res.disk_failure.evidence is None
    assert [(n["key"], n["marker"], n["free_bytes"]) for n in _disk_notes(roots)] == [("low_disk", None, 2 * GIB)]
@pytest.mark.parametrize(("gib", "min_free", "script"), [
    (50, 10.0, BADBIT), (2, 0.0, BADBIT), (1024, 10.0, UNRELATED), (1024, 10.0, EARLY)])
def test_no_disk_reason_when_the_evidence_is_not_there(roots, work, monkeypatch, gib, min_free, script):
    assert res.attempt.status == "failed" and res.disk_failure is None and _disk_notes(roots) == []
def test_the_console_wins_over_low_disk(roots, work, monkeypatch):  # NO_SPACE + _space_after_child(…, 2)
    assert res.disk_failure.key == "disk_full" and res.disk_failure.free_bytes == 2 * GIB
def test_success_and_interruption_are_never_diagnosed(roots, work):
    # SAYS_BUT_OK → finished、disk_failure None；r2：NO_SPACE + on_line 丟 KeyboardInterrupt → interrupted、None
def test_vcp_that_cannot_write_its_console_stops_the_child_and_aborts(roots, work):
    # work/"sleeper.py" = SLEEPER；on_line 丟 OSError(errno.ENOSPC, ...)；raises VcpError "^disk_full:"
    assert ei.value.fields == {"disk_evidence": "vcp"} and time.monotonic() - started < 25
    assert load_record(roots.data, "r1").attempts[-1].status == "running"
def test_other_os_errors_pass_through_unchanged(roots, work):  # on_line 丟 PermissionError → raises PermissionError
def test_a_train_yaml_that_cannot_be_replaced_aborts_with_disk_full_and_stays_whole(roots, work, monkeypatch):
    # 先跑 r1；before = train.yaml bytes；patch atomic.os.replace：dst 名為 train.yaml 時丟 ENOSPC；--resume
    assert ei.value.fields == {"disk_evidence": "vcp"} and path.read_bytes() == before
    assert not [p for p in path.parent.iterdir() if atomic.is_tmp_name(p.name)]
# test_cli_train.py
def test_train_run_cli_names_disk_full(roots, tmp_path):
    # fake_train.py = 'import sys\nprint("OSError: [Errno 28] No space left on device")\nsys.exit(int(sys.argv[1]))\n'
    assert r.exit_code == 1 and "status=FAIL" in v and "disk_evidence=console" in v and "free_gib=" in v
    assert 'reason="disk_full: the command exited 1 and its console says' in v
    assert "; free space, then --resume" in r.output
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py -q`；Expected: FAIL（`cannot import name 'console_marker'`、`RunResult` 沒有 `disk_failure`、ENOSPC 以 `OSError` 丟出）。
- [ ] **Step 3: 實作上列 Interfaces，接進 `train_run`**

`with own_writes():` 從 `_close_running(...)` 那一行起，包到 `_finish_checkpoints(...)` 回傳為止（console、`save_record`／`save_run` 的替換、`append_event`、快照都在裡面）；`_upload_all` 不在裡面（目的地不是 vcp 的紀錄，那裡的磁碟滿照舊是原樣的 `OSError`；寫進後記的實作裁決）。`finished` 事件之後：`status == "failed"` 才 `after = measure(disk_targets, ...)`、`failure = diagnose(code, run_root / attempt.console, after)`，有就 `append_event(data_root, spec.run_id, "note", n, key=failure.key, marker=failure.marker, free_bytes=failure.free_bytes)`。CLI：有 `res.disk_failure` 時 `fields["reason"] = reason`、有 `evidence` 才 `fields["disk_evidence"]`，human 加 `f"{reason}; free space, then --resume"`，狀態仍是 FAIL。

- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/disk.py src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_disk.py tests/unit/train/test_run.py tests/unit/test_cli_train.py`；`git commit -m "feat(train): name disk_full and low_disk when an attempt fails"`。

### Task 8: `train status` 列殘留的 `.tmp`（§5.5）

**Files:**
- Modify: `src/vcp/train/status.py:16-54`、`src/vcp/cli_train.py:207-258`
- Test: `tests/unit/train/test_status.py`、`tests/unit/test_cli_train.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) TmpFile(path: str, bytes: int)`；`tmp_files(data_root: Path, record: TrainRecord) -> list[TmpFile]`（`run_dir` 遞迴 + 每個已登記 checkpoint 的 `resolve_stored_path(...).parent` 不遞迴；`name.lower().endswith(".tmp")`；依解析後路徑去重、依 `path` 排序；`path = store_path(p, data_root)`；目錄不在略過；`stat` 丟 `FileNotFoundError` 略過）；`StatusResult.tmp: list[TmpFile]`（最後一欄）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_status.py（沿用 _seeded：checkpoint 在 roots.data/"work"/"weights"）
def test_status_lists_leftover_tmp_files_in_the_run_and_beside_checkpoints(roots):
    # runs/r1/train/.train.yaml.0a1b2c3d.tmp（5 bytes）；w/"resume.pth.TMP"（266）；
    # w/"sub"/"deep.tmp"（不遞迴，不列）；roots.data/"elsewhere"/"junk.tmp"（不列）
    assert status(roots.data, "r1").tmp == [TmpFile("runs/r1/train/.train.yaml.0a1b2c3d.tmp", 5),
                                            TmpFile("work/weights/resume.pth.TMP", 266)]
def test_status_without_tmp_files_and_with_a_vanished_checkpoint_dir(roots):
    assert status(roots.data, "r1").tmp == []  # 再 shutil.rmtree(w) 一次，仍 == []
def test_a_checkpoint_outside_the_data_root_has_its_tmp_listed_by_absolute_path(roots, tmp_path):  # Review Focus 5
    # register(tmp_path/"outside"/"m.pt")、save_record；outside/"m.pt.tmp" 寫 b"tt"
    assert status(roots.data, "r1").tmp == [TmpFile((outside / "m.pt.tmp").resolve().as_posix(), 2)]
# test_cli_train.py
def test_train_status_cli_prints_tmp(roots, tmp_path):  # _run(work) 建 r1；work/"weights"/"resume.pth.tmp" 寫 10 bytes
    assert "tmp=1" in v and "status=WARN" in v and "tmp: " in r.output and "(10 bytes)" in r.output
    assert doc["fields"]["tmp"] == 1 and doc["result"]["tmp"][0]["bytes"] == 10
    assert doc["result"]["tmp"][0]["path"].endswith("weights/resume.pth.tmp")
    # 刪掉之後 "tmp=0" in v
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_status.py tests/unit/test_cli_train.py -k tmp -q`；Expected: FAIL（`cannot import name 'TmpFile'`、VERDICT 沒有 `tmp=`）。
- [ ] **Step 3: 實作** — `status()` 加 `tmp=tmp_files(data_root, record)`；CLI 一律 `fields["tmp"] = len(st.tmp)`、`warn |= bool(st.tmp)`、human `f"tmp: {t.path} ({t.bytes} bytes)"`、payload `"tmp": [{"path": t.path, "bytes": t.bytes} for t in st.tmp]`。唯讀、不刪。
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/status.py src/vcp/cli_train.py tests/unit/train/test_status.py tests/unit/test_cli_train.py`；`git commit -m "feat(train): train status lists leftover tmp files"`。

### Task 9: 回歸門檻與文件（§10.7、§11）

**Files:**
- Modify: `tests/unit/test_regression_gate.py`（`GATE` 最後加一列）
- Modify: `CLAUDE.md`（再整份複製成 `AGENTS.md`）、`docs/reference/cli.md`
- Modify: `.claude/skills/vcp-train-submit-backup/{SKILL.md,reference.md}`、`.claude/skills/vcp-running-contests/{workflow.md,operator-guide.md}`、`.claude/skills/vcp-orientation/{SKILL.md,map.md}`，鏡射到 `.agents/skills/`
- Modify: `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`（第 33 條）、`docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`（第 20 條）、`docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md`
- Create: `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md`

**Interfaces:**
- Consumes: Task 1–8 的測試名。

- [ ] **Step 1: 加 gate 列** — 標籤 `"selection and disk (0.16.0)"`；說明 `"submit: select --record writes one selected row per distinct set and status says missing / slots / differs; sync and upload count the platform CLI's warning lines; train: low_disk: before the first write and after a failed attempt, disk_full: from the console and from vcp's own writes, tmp= in train status; a yaml rewrite keeps the old file whole and retries a shared target on Windows"`（照既有列拆成多段字串）；證明：
  - `tests/unit/submit/test_select.py`：`test_select_records_the_ticked_ids_once`、`test_missing_starts_selection_warn_hours_before_the_deadline`、`test_count_and_set_both_wrong_say_slots_then_differs`
  - `tests/unit/submit/test_sync.py`：`test_sync_verdict_counts_cli_warnings_and_warns_every_time`；`tests/unit/submit/test_actions.py`：`test_upload_counts_the_warnings_of_the_pre_sync_and_the_read_back_once`
  - `tests/unit/train/test_run.py`：`test_train_run_aborts_before_any_write_when_a_run_disk_is_low`、`test_a_badbit_child_on_a_low_disk_fails_with_low_disk`、`test_a_child_that_ran_out_of_disk_fails_with_disk_full`、`test_vcp_that_cannot_write_its_console_stops_the_child_and_aborts`
  - `tests/unit/train/test_status.py`：`test_status_lists_leftover_tmp_files_in_the_run_and_beside_checkpoints`；`tests/unit/core/test_atomic.py`：`test_a_failed_replace_keeps_the_old_file_whole_and_no_temp`、`test_windows_retries_a_shared_target_then_replaces`

Run: `uv run pytest tests/unit/test_regression_gate.py -q` → PASS。Commit：`git add tests/unit/test_regression_gate.py`；`git commit -m "test(gate): guard the 0.16.0 selection, CLI-warning and disk fixes"`。

- [ ] **Step 2: 改文件（內容照 spec §11）**

- `CLAUDE.md`：「提交治理」的事件清單加 `selected`（0.16.0 起才有，舊 vcp 讀到會 FAIL，讀寫同一份台帳的都先升級）、一句 `select --record`（核對只在 `board_rule: best`）、`selection_warn_hours`；「路徑」的 `runs/<id>/` 段：`train.yaml`、`run.yaml` 的整份重寫是「暫存檔 → fsync → replace」、`env.<n>.json` 的 `disk`、`train status` 列 `.tmp`；「常用命令」加 `vcp submit select`、`train run` 加 `--min-free-gib`。然後 `cp CLAUDE.md AGENTS.md`。
- `docs/reference/cli.md`：新列 `vcp submit select`；改 `status` / `report`、`final`（核對與 `next:` 只在 `board_rule: best`；名額取最新 `final` 列的 `slots`）、`score` / `sync`（`cli_warnings=`）、`upload`、`init`（profile 新鍵）、`train run`（`low_disk:` 的兩個時點、`disk_full:` 只由 `train run` 轉換、`free` 不反映 Windows 配額）、`train status`（`tmp=`）；「一次提交」在 `final` 之後加 `uv run vcp submit select --dataset D-test --record SUB34`。
- skills：`vcp-train-submit-backup/SKILL.md` 的流程（`final` → 平台勾選 → `select --record` → `status` 沒有 `selection=` 的 WARN）、常見錯誤加「勾錯」、「磁碟滿」、「`cli_warnings=` 一直 WARN 就是 CLI 該升級」，`reference.md` 命令表；`vcp-running-contests/workflow.md` 第 10 步「決選」加記錄與核對、`operator-guide.md` 對應段落；`vcp-orientation/SKILL.md` 命令表加 `select`、`map.md` 版本沿革加 0.16.0。然後 `cp -r .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .agents/skills/`。
- 治理 spec 第 33 條、訓練層 spec 第 20 條：各一段摘要，結尾「細節見 `2026-10-30-vcp-0160-selection-and-disk-design.md`」。
- 新後記：照 `2026-10-09-vcp-048-errored-submissions-followups.md` 的樣子（標頭寫 spec、分支、base、commit 清單；§1 已知限制指回 spec §5.4、§12；§2.1 實作裁決；§2.2 開放待辦）。§2.1 至少寫：`PlatformError(cli_warnings=)` 讓回讀後的平台 FAIL 帶回讀的行；`_upload_all` 不在 C3 轉換範圍；測試 patch `atomic.IS_WINDOWS` 而不 patch `os.name`；`final` 列的 golden 是「有沒有選取，除了 `ts` 都相同」；`select` 的 `invalid:` 在交易內檢查；`select --json` 的 result 是那一列加 `selection`。§2.2：glob 寫進 `started` 事件（§12）、`--none`、`<id>@<ref>`、`gc`。
- `2026-10-23-rsna-wrapup-followups.md`：§1 加「1b 的處置（2026-10-30，0.16.0）」：`sync` / `upload` 的 `cli_warnings=`，`status` 不提示 CLI 過舊，理由照裁決 R1；§2、§3 各加一條處置，指向新 spec 與後記。

- [ ] **Step 3: 驗證文件** — `diff CLAUDE.md AGENTS.md; diff -r .claude/skills .agents/skills; git diff --check; uv run pytest tests/unit/test_skills_plugin.py -q`；Expected: 三者無輸出、PASS。
- [ ] **Step 4: Commit docs** — `git add CLAUDE.md AGENTS.md docs/reference/cli.md .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .agents/skills/vcp-train-submit-backup .agents/skills/vcp-running-contests .agents/skills/vcp-orientation docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md`；`git commit -m "docs: 0.16.0 selection record, CLI warnings and training disk space"`。
- [ ] **Step 5: 全套驗證** — `uv run ruff check . && uv run ruff format --check . && uv run pytest --cov=vcp`；Expected: 全綠，`TOTAL` coverage ≥ 80%。

---

## 自我檢查（對 spec 逐節核對的結果）

- **覆蓋：** §3.1、§3.3、§3.4 → T2；§3.2、§7 → T3；§3.5 → T4；§4 → T5；§5.1、§5.2 → T6；§5.3、§5.4 → T7；§5.5 → T8；§5.6 → T1（`train run` 的轉換在 T7）；§6 每一列都有 VERDICT 斷言；§8 由 T1 golden、T2 profile 測試、T6 conftest 鎖住；§10.1–§10.7 每條對到一個測試名；§11 → T9。§9 的版本與 `CHANGELOG.md` 照指示不在本計畫。
- **型別一致：** `SelectionState`、`selection_state`、`selection_lines(state)`、`differs_line(final, recorded)`、`label` 在 T2 定義、T3 T4 使用；`PlatformList`、`merge_warnings`、`as_platform_list` 在 T5 定義與使用；`disk_targets`、`measure`、`lowest`、`GIB` 在 T6 定義、T7 使用；`ROOMY_BYTES` 在 `tests/helpers.py`，conftest 與測試都從那裡 import。
- **既有測試：** 只有 T5 列出的兩處要改斷言；其餘既有 VERDICT 斷言用 `in`，有 `board_rule: best` 的 e2e 沒有截止、`final` 之後本來就因封槍 WARN，不受新的 WARN 影響。
