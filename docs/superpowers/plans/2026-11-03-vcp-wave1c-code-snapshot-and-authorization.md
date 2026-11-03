# vcp 稽核 Wave 1c（0.18.0）：程式碼快照與產物授權 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `train run` 在第一次寫入前掃描每個程式碼根目錄、寫或重用內容定址的 `code_snapshot`；用 `Session` 的子程序結束時記下實際 import 的模組（`code_trace`）；讀取時算出程式碼等級 `traced > snapshot > commit > declared`；`authorization` 產物把 benchmark 的設定綁到正式的產物 job 與訓練；`vcp code snapshot | verify | diff | export`；備份、gc、provenance 圖、`eval ingest`、`submit stage` / `final` 認得新產物與 `require_code`。

**Architecture:** 新套件 `vcp.code`：`rules`（規則 v1 的常數與檔名判斷）、`scan`（一個 root 的記憶體內掃描與身分）、`generated`（生成程式碼登記表）、`roots`（root 規劃、checkpoint 與生成目錄的排除、distribution 紀錄）、`snapshot`（產物、鎖、重用、完好檢查）、`trace`（模組分類與 `atexit`）、`grade`（等級）、`process`（同程序的明要快照與 import 檢查）、`bundle` / `diff`（`export`、`verify --dir`、`diff`）、`schema`（pydantic 模型與 `CodeGrade`）。訓練層加 `vcp.train.coderun`（`train run` 的整合，`run.py` 只呼叫它）與 `vcp.train.authorize`（run 依據與 `train run` 的 claims）；產物層加 `vcp.artifact.authorization`（產物、claims、`consume`），writer 以延遲 import 檢查兩個保留輸入名。CLI 新檔 `vcp/cli_code.py`。

**Tech Stack:** Python 3.12、pydantic v2、typer、pytest、ruff、uv。

**Spec:** `docs/superpowers/specs/2026-10-31-vcp-wave1c-code-snapshot-and-authorization-design.md`（1e94798）。§14 作者裁決、§15 controller 裁決 C1–C4、§16 使用者 2026-11-03 批准的十二項預設都已定案；本計畫只在最後一節「Open points for the controller」列出 spec 沒說、或程式碼讓 spec 的字面做不到的地方，各附提議的裁決。分支 `feat/wave1c-code-snapshot`（spec 的三個 commit 之上）。

**Base:** 1e94798。開工前 `git log --oneline -1` 要是它或它的後代；不是就停下來回報。

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()`。ruff TID251 會擋 `datetime.now` / `utcnow` / `today` / `time.time`；唯一例外是 `src/vcp/core/time.py`。（`datetime.fromtimestamp(x, tz=UTC)` 可用。）
- 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 1（FAIL）/ 2（ABORT）。命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。命令以 `run_command(context=)` 保留失敗時已知的識別欄位；可選值未給時省略。`code *` 帶 `id` / `root` / `run`；`authorize` 帶 `id`、`basis`。
- 新 `reason=` 字彙：`unauthorized:`、`superseded:`、`unbindable:`、`code_outside:`、`code_required:`、`too_large:`、`vcp_mismatch:` 是 FAIL（exit 1）；`code_unavailable:` 是 ABORT（exit 2）。其餘沿用既有的 `drift:`、`mismatch:`、`not_found:`、`exists:`、`invalid:`、`role_reserved:`、`partial:`、`locked:`、`disk_full:`。不再加別的字。
- 寫一次的檔一律經 `vcp.core.atomic.write_once`（產物都經 `ArtifactWriter`）；事件只經 `append_event`（`append_whole_line`）；不新增 append 模式的 `open`（`tests/unit/test_append_only_ledgers.py` 會紅）。
- 上限照抄：單檔保留 ≤ 1 MiB（`1024**2`）、每個 root 保留累計 ≤ 64 MiB；納入 ≤ 20,000 檔、雜湊總量 ≤ 512 MiB；一般目錄走訪 ≤ 200,000 個項目；清單（`untracked_code_paths`、`excluded_paths`、trace 的 `outside` / `unlisted` / `changed`）≤ 50；結束比對的 `paths` ≤ 5；開跑前那一行列前 5 個路徑。鎖 `<data_root>/locks/code_snapshot-<id>.lock`，等 60 秒（`vcp.core.lock.WAIT_SECONDS`，測試調小）。身分前綴 `"vcp-code/1\n"`；id `code-<身分前 16 碼>`、trace `<run>-a<n>-t<seq>`、ref `code.<label>` / `code_trace.a<n>.<seq>` / `authorization`。
- 安全：機密檔名不開檔、不雜湊；任何 `.` 開頭的段、被 git 忽略的檔、未追蹤的 `CONFIG` 檔、root 以外的檔都不讀；環境變數不記錄；`direct_url.url` 去掉 userinfo、query、fragment 再過 `redact`。追蹤永不改變子程序的 exit code。
- 測試全用 `tmp_path`，不碰真的 data root（`roots` fixture、`_isolated_roots` autouse、`tests/helpers.py`、`tests/submit_fixtures.py`、`tests/backup_fixtures.py`）；git 用 `isolated_git` fixture 加 `helpers.git_repo` / `helpers.git`，找不到 git 就 skip。Windows 與 Linux 的 CI 都要過（只在 Windows 跑過會漏掉 POSIX 的失敗）。
- **`tests/unit/code/` 不放 `__init__.py`，檔名一律 `test_code_*.py`**：`tests/unit` 沒有 `__init__.py`，pytest 的 prepend 模式會把它放上 `sys.path`，`code/__init__.py` 會蓋掉標準庫的 `code`（`pdb` 用它），跟 0.17 的 `gc` 同一個陷阱；沒有 `__init__.py` 的目錄只是 namespace 片段，標準庫照樣優先。
- 從 Task 7 起 `tests/conftest.py` 有 autouse `_code_tracking_off`（`VCP_CODE_TRACE=0`、`vcp.code.roots.editables_of` → `[]`）；要追蹤或要 editable root 的測試呼叫 `helpers.trace_on(monkeypatch)`。
- 位元組不變：沒有新 ref 的 `TrainRecord` / `RunCard`、`require_code: declared` 的 profile、沒有 `code` 的 `stage.json` 都跟 0.17 逐位元組相同（Task 5、Task 17 的 golden）。
- 不用 `git add -A`（明列檔案）、不用 `git stash`、不對 markdown 跑 `ruff format`；一律 LF，測試改寫台帳或卡用 `newline="\n"` 或 `write_bytes`；收尾 `git diff --check` 無輸出。
- commit 格式 `type(scope): 說明`，不加 Co-Authored-By trailer（CLAUDE.md）；每個 commit 前 `uv run ruff check . && uv run ruff format --check .`。
- 每個行為變更先寫會失敗的測試、跑紅，再實作；只鎖現狀的 golden 例外（標明）。
- 不動 `CHANGELOG.md`、`src/vcp/__init__.py` 的 `__version__`、`.claude/.claude-plugin/plugin.json`、tag：發版是使用者同意後另一個 PR。文中「舊 vcp」指 0.17 以前。
- `src/vcp/train/run.py` 已經 923 行：新邏輯放 `vcp/train/coderun.py`，`run.py` 只加呼叫與欄位。

## Review Focus

1. 訓練 venv 放在專案 repo 裡（`<repo>/.venv/Lib/site-packages/numpy/...`，cwd root 就是 `<repo>`）：這些模組是 `distribution`，不是 `unlisted`；否則這種常見佈局的 run 永遠到不了 `traced` → Task 9 `test_a_module_in_a_venv_inside_a_root_is_a_distribution`。
2. Windows 的標準庫擴充模組在 `<base_prefix>/DLLs`（`_socket.pyd`、`select.pyd`），`sysconfig` 的 `stdlib` / `platstdlib` 不含它：它們是 `stdlib`，不是 `outside` → Task 9 `test_windows_dlls_are_stdlib`。
3. 命令寫的是相對路徑（`python tools/train.py`、`python ../other/train.py`）：`script` root 以 `--cwd` 為基準找，不是 wrapper 自己的工作目錄 → Task 3 `test_a_relative_script_is_found_from_cwd`。
4. editable 套件的路徑很長（Windows 的 `C:/Users/<name>/Desktop/...`）：`redact` 的長字串規則（`[A-Za-z0-9+/_-]{32,}`，含 `/`）會把記錄用的 `direct_url.url` 變成 `<redacted>`；root 的路徑必須從原始 url 算，不是從清過的那份 → Task 3 `test_editable_roots_come_from_the_raw_url`。
5. 掃描之後、複製之前，程式檔被刪掉（不只是被改）：那個 root 是 `drift`，不是 `FileNotFoundError` 讓整個 `train run` ABORT → Task 4 `test_a_file_deleted_after_the_scan_is_drift`。

新模組：`src/vcp/code/{__init__,rules,scan,schema,generated,roots,snapshot,trace,grade,process,bundle,diff}.py`、`src/vcp/train/{coderun,authorize}.py`、`src/vcp/artifact/authorization.py`、`src/vcp/cli_code.py`。依賴方向：`rules` ← `scan` ← `roots` ← `snapshot` ← `trace` / `process` / `grade` ← `bundle` / `diff` ← `cli_code`；`vcp.code` 不在模組頂層 import `vcp.train`（`trace` 例外：它 import `vcp.train.records`，而 `Session.current` 延遲 import `trace`）；`vcp.artifact.writer` 延遲 import `vcp.code.process` 與 `vcp.artifact.authorization`。

---

### Task 1: `vcp.core.build` 用 vcp 自己的 repo（§2 順手；dirty 工作樹後記 §3 第 3 條）

**Files:**
- Modify: `src/vcp/core/proc.py`（新 `GIT_LOCAL_ENV`、`git_environment`）
- Modify: `src/vcp/train/gitstate.py:62-123`（`_LOCAL_ENV = GIT_LOCAL_ENV`；`_environment()` 回傳 `git_environment()`，名字保留）
- Modify: `src/vcp/core/build.py:41-45`（`_git` 帶 `env=git_environment()`）
- Test: `tests/unit/core/test_proc.py`、`tests/unit/core/test_build.py`

**Interfaces:**
- Produces:
  - `GIT_LOCAL_ENV: frozenset[str]`：gitstate 現在 `_LOCAL_ENV` 的十三個名字，原樣搬過來。
  - `git_environment() -> dict[str, str]`：`os.environ` 去掉 `GIT_LOCAL_ENV`，加 `GIT_OPTIONAL_LOCKS="0"`（`git status --porcelain` 從此不改寫 index）。`vcp.code` 的每個 git 呼叫都用它。
  - 放在 core 而不是讓 build import gitstate：`vcp.core.build` → `vcp.train.gitstate` 會經 `vcp/train/__init__.py` → `session` → `vcp.artifact.writer` → `vcp.core.build` 繞回來（Open point 2）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_proc.py
def test_git_environment_drops_repository_local_variables(monkeypatch):
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'safe.directory=*'")
    env = git_environment()
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"} & set(env)
    assert env["GIT_CONFIG_PARAMETERS"] == "'safe.directory=*'" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert GIT_LOCAL_ENV == gitstate._LOCAL_ENV
# test_build.py（既有的 repo fixture）
def test_vcp_version_ignores_a_git_dir_pointing_elsewhere(repo, tmp_path, monkeypatch):
    # other = 另一個有一個 commit 的 repo；兩邊的 HEAD 在 setenv 之前先讀
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert probe_build(pkg).commit == own_head != other_head
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/core/test_proc.py tests/unit/core/test_build.py -q`；Expected: FAIL（`cannot import name 'git_environment'`、commit 是 other 的）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/core tests/unit/train/test_gitstate.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/core/proc.py src/vcp/core/build.py src/vcp/train/gitstate.py tests/unit/core/test_proc.py tests/unit/core/test_build.py`；`git commit -m "fix(core): vcp_version reads vcp's own repository whatever GIT_DIR says"`。

### Task 2: 規則 v1 與一個 root 的掃描（§4.2、§4.1 身分與保留）

**Files:**
- Create: `src/vcp/code/__init__.py`（只有 docstring）、`src/vcp/code/rules.py`、`src/vcp/code/scan.py`
- Test: Create `tests/unit/code/test_code_rules.py`、`tests/unit/code/test_code_scan.py`（不建 `__init__.py`）

**Interfaces:**
- Consumes: Task 1 的 `git_environment`。
- Produces:
  - `rules.py`：`RULES_VERSION = 1`、`IDENTITY_PREFIX = "vcp-code/1\n"`；`CODE_SUFFIXES = frozenset({".py", ".pyi", ".pyx", ".pxd", ".c", ".cc", ".cpp", ".cu", ".cuh", ".h", ".hpp"})`；`CONFIG_SUFFIXES = frozenset({".pyd", ".so", ".ipynb", ".sh", ".ps1", ".bat", ".cmd", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json"})`；`CONFIG_GLOBS = ("requirements*.txt", "constraints*.txt", "*.lock", "Makefile", "Dockerfile")`；`SECRET_GLOBS = ("*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx", "id_rsa*", "id_ed25519*", "id_ecdsa*", "kaggle.json", "rclone.conf", "pg_service.conf", "pgpass.conf", "_netrc", "credentials*", "*secret*", "*service-account*", "*service_account*")`；`KEEP_FILE_LIMIT = 1024**2`、`KEEP_ROOT_LIMIT = 64 * 1024**2`、`MAX_FILES = 20_000`、`MAX_HASH_BYTES = 512 * 1024**2`、`MAX_WALK_ENTRIES = 200_000`、`LISTED_PATHS = 50`。
  - `file_class(name: str) -> Literal["code", "config"] | None`：`name.casefold()`；副檔名比兩組，`CONFIG_GLOBS` 以 `fnmatch.fnmatchcase(folded, pattern.casefold())`。
  - `is_secret(name: str) -> bool`（同樣的比法）；`skipped_segment(segment: str) -> bool`：以 `.` 開頭，或 casefold 後是 `__pycache__` / `node_modules` / `site-packages`，或以 `.egg-info` 結尾。
  - `scan.py`：`@dataclass(frozen=True) IndexEntry(path: str, sha256: str, bytes: int, kept: bool = False)`；`RootKind = Literal["git", "dir"]`；`ScanReason = Literal["too_large", "unreadable"]`；`@dataclass(frozen=True) RootScan(root: Path, kind: RootKind, entries: tuple[IndexEntry, ...], untracked_code: tuple[str, ...], excluded_paths: tuple[str, ...], reason: ScanReason | None)`，屬性 `identity_sha256: str | None`、`snapshot_id: str | None`（`f"code-{identity[:16]}"`）、`files`、`bytes`、`kept`、`not_kept`（有 reason 時 identity 與 id 都是 None、entries 是空的）。
  - `path_key(path: Path) -> str`（`os.path.normcase(str(Path(path).resolve()))`）；`is_under(path: Path, directory: Path) -> bool`（同一個或在它底下，以 `path_key` 比）。
  - `root_kind(path: Path) -> RootKind`：`git -C <path> rev-parse --show-toplevel` 在 `git_environment()` 下成功就是 `git`；沒有 git 或失敗是 `dir`。
  - `scan_root(root: Path, *, kind: RootKind, skip_dirs: Sequence[Path] = (), keep: Literal["capped", "all"] = "capped") -> RootScan`。
  - `identity_sha256(entries: Iterable[IndexEntry]) -> str`：`sha256(IDENTITY_PREFIX + "".join(f"{e.sha256} {e.bytes} {e.path}\n"))`，依 path 排序，`kept` 不進。
  - `select_kept(entries: Sequence[IndexEntry], keep: Literal["capped", "all"]) -> tuple[IndexEntry, ...]`：`all` 全保留；`capped` 依 path 順序貪婪：`bytes <= KEEP_FILE_LIMIT and total + bytes <= KEEP_ROOT_LIMIT` 才保留，保留了才 `total += bytes`（Open point 12）。
  - `snapshot_json_bytes(entries: Sequence[IndexEntry]) -> bytes`：`(json.dumps({"schema_version": 1, "rules": 1, "identity_sha256": ..., "files": len(entries), "bytes": sum}, ensure_ascii=False, indent=1) + "\n").encode("utf-8")`——跟 `ArtifactWriter.write_json` 寫出的位元組相同，鍵序固定。
  - `index_jsonl_bytes(entries) -> bytes`：每行 `json.dumps({"path", "sha256", "bytes", "kept"}, ensure_ascii=False) + "\n"`。

`scan_root` 的順序（測試依它）：

```text
1. 候選：git → git -C root ls-files -z --cached（追蹤）與 ls-files -z --others --exclude-standard（未追蹤），
   UTF-8 解碼（解不了 → unreadable）；dir → os.scandir 迭代，項目數 > MAX_WALK_ENTRIES → too_large，
   symlink / junction 記 excluded，跳過的段與含 pyvenv.cfg 的目錄不進入。
2. 每個相對 posix 路徑：任一段 skipped_segment、某個上層目錄（root 之內）含 pyvenv.cfg、絕對路徑 is_under 任一 skip_dirs → 丟掉（不列）；
   is_secret(檔名) → excluded_paths（不 stat、不開）；os.path.islink 或 junction → excluded_paths；
   分類：追蹤的收 code 與 config，未追蹤與 dir root 只收 code；其他丟掉；不是一般檔（submodule、已刪除）丟掉。
3. lstat 大小；檔數 > MAX_FILES 或總和 > MAX_HASH_BYTES → too_large（一個都不雜湊）。
4. sha256_file 逐檔；FileNotFoundError → 丟掉；其他 OSError → unreadable。
5. 依 path 排序、select_kept；untracked_code = 收進來的未追蹤 code 檔。上限以 rules.X 在呼叫時讀（測試會 monkeypatch）。
```

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_code_rules.py
def test_limits_and_prefix_are_the_specs():
    assert (rules.KEEP_FILE_LIMIT, rules.KEEP_ROOT_LIMIT) == (1024**2, 64 * 1024**2)
    assert (rules.MAX_FILES, rules.MAX_HASH_BYTES, rules.MAX_WALK_ENTRIES) == (20_000, 512 * 1024**2, 200_000)
    assert (rules.LISTED_PATHS, rules.IDENTITY_PREFIX, rules.RULES_VERSION) == (50, "vcp-code/1\n", 1)
@pytest.mark.parametrize(("name", "cls"), [("a.PY", "code"), ("k.cu", "code"), ("m.HPP", "code"), ("x.Json", "config"),
    ("nb.ipynb", "config"), ("ext.pyd", "config"), ("requirements-dev.txt", "config"), ("constraints.txt", "config"),
    ("uv.lock", "config"), ("Makefile", "config"), ("Dockerfile", "config"), ("README.md", None), ("notes.txt", None),
    ("d.csv", None), ("i.png", None)])
def test_two_groups_ignoring_case(name, cls): ...
@pytest.mark.parametrize("name", ["server.pem", "a.KEY", "id_rsa.pub", "id_ed25519", "Kaggle.JSON", "rclone.conf",
    "pg_service.conf", "pgpass.conf", "_netrc", "credentials.yaml", "my_secret_cfg.py", "gcp-service-account.json",
    "x_service_account.py"])
def test_secret_names(name): assert is_secret(name)
def test_ordinary_names_are_not_secret(): assert not any(map(is_secret, ["train.py", "config.yaml", "keys.py"]))
```

```python
# test_code_scan.py；_never_open(monkeypatch, names)：Path.open 與 builtins.open 碰到這些檔名就丟 AssertionError
def test_skipped_paths_are_neither_read_nor_listed(tmp_path):
    # r/: ok.py、.venv/x.py、.git/h.py、.claude/c.py、__pycache__/m.py、node_modules/n.py、pkg.egg-info/p.py、
    #     env/pyvenv.cfg + env/lib/q.py、data/d.py、configs/c.py
    s = scan_root(r, kind="dir", skip_dirs=[r / "data", r / "configs"])
    assert [e.path for e in s.entries] == ["ok.py"] and s.excluded_paths == ()
def test_secret_files_are_never_opened(tmp_path, monkeypatch):  # train.py、deploy.pem、kaggle.json、my_secret.py
    assert [e.path for e in s.entries] == ["train.py"]
    assert sorted(s.excluded_paths) == ["deploy.pem", "kaggle.json", "my_secret.py"]
def test_symlinks_are_not_followed(tmp_path):  # os.symlink 失敗（Windows 沒權限）→ pytest.skip
    assert "link.py" not in [e.path for e in s.entries] and "link.py" in s.excluded_paths
def test_a_plain_directory_takes_code_only(tmp_path):  # a.py b.cu c.yaml d.json e.ipynb → ["a.py", "b.cu"]
# 以下 usefixtures("isolated_git")
def test_tracked_code_and_config_come_from_the_worktree(tmp_path):  # commit train.py + cfg.yaml，再改 train.py 不 commit
    assert s.kind == "git" and {e.path: e.sha256 for e in s.entries} == {
        "cfg.yaml": sha256_file(r / "cfg.yaml"), "train.py": sha256_file(r / "train.py")}
def test_untracked_code_is_read_and_untracked_config_never_opened(tmp_path, monkeypatch):
    # 未追蹤：helper.py k.cu + 不得開啟的 args.yaml hparams.yaml config.json metrics.json notes.txt x.csv run.ipynb ext.so run.sh
    assert [e.path for e in s.entries] == ["helper.py", "k.cu", "train.py"]
    assert s.untracked_code == ("helper.py", "k.cu")
def test_ignored_files_are_never_opened(tmp_path, monkeypatch):  # .gitignore: gen.py、out/；_never_open({"gen.py", "o.py"})
def test_a_subdirectory_root_lists_only_its_subtree(tmp_path):  # repo/a/x.py、repo/b/y.py；scan_root(repo / "a") → ["x.py"]
def test_nested_roots_split_innermost(tmp_path):  # skip_dirs=[repo / "vendor"] 時沒有 vendor/*；scan_root(repo / "vendor") 有
def test_tracked_but_deleted_and_submodules_are_not_listed(tmp_path):
def test_a_non_ascii_tracked_name_is_listed_by_its_real_name(tmp_path):  # "資料處理.py" → entries[0].path == "資料處理.py"
# 上限
def test_capped_keep_is_greedy_in_path_order(tmp_path, monkeypatch):
    # big.py 1 MiB + 1 byte：capped → kept False，all → True
    # rules.KEEP_ROOT_LIMIT = 10：a.py 4、b.py 4、c.py 4、d.py 2 → kept [True, True, False, True]；再掃一次結果相同
def test_too_many_files_or_bytes_is_too_large_and_hashes_nothing(tmp_path, monkeypatch):
    # rules.MAX_FILES = 2、三個檔；scan.sha256_file 換成丟例外 → reason "too_large"、entries ()、snapshot_id None
    # rules.MAX_HASH_BYTES = 5、一個 6 bytes 的檔 → "too_large"
def test_a_walk_past_the_entry_limit_is_too_large(tmp_path, monkeypatch):  # MAX_WALK_ENTRIES = 3、五個項目
def test_an_unreadable_file_marks_the_root_unreadable(tmp_path, monkeypatch):  # sha256_file 對 a.py 丟 PermissionError
# 身分
def test_identity_is_the_documented_sha():
    es = [IndexEntry("b.py", SB, 2, True), IndexEntry("a.py", SA, 1, False)]
    assert identity_sha256(es) == hashlib.sha256(f"vcp-code/1\n{SA} 1 a.py\n{SB} 2 b.py\n".encode()).hexdigest()
    assert identity_sha256([replace(e, kept=not e.kept) for e in es]) == identity_sha256(es)
def test_one_content_in_two_places_has_one_snapshot_json(tmp_path):  # r1、r2 同內容（含 >1 MiB 的檔），capped 與 all 交叉
    assert len({snapshot_json_bytes(s.entries) for s in four_scans}) == 1 and len({s.snapshot_id for s in four_scans}) == 1
    doc = json.loads(b); assert list(doc) == ["schema_version", "rules", "identity_sha256", "files", "bytes"]
def test_a_code_edit_moves_the_identity_and_a_readme_does_not(tmp_path):
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code`）。
- [ ] **Step 3: 實作上列 Interfaces 與掃描順序。**
- [ ] **Step 4: 跑綠** — 同一指令；Expected: PASS（Windows 上 symlink 那條可能 skip）。
- [ ] **Step 5: Commit** — `git add src/vcp/code/__init__.py src/vcp/code/rules.py src/vcp/code/scan.py tests/unit/code/test_code_rules.py tests/unit/code/test_code_scan.py`；`git commit -m "feat(code): rules v1 and the in-memory scan of one code root"`。

### Task 3: 生成程式碼登記表與 root 規劃（§4.2 排除、§5.1 第 4 步、§5.2 登記表、§4.3 label）

**Files:**
- Create: `src/vcp/code/schema.py`（`DirectUrl`、`Distribution`）、`src/vcp/code/generated.py`、`src/vcp/code/roots.py`
- Test: Create `tests/unit/code/test_code_generated.py`、`tests/unit/code/test_code_roots.py`

**Interfaces:**
- Consumes: Task 2 的 `root_kind`、`path_key`、`is_under`；`vcp.core.proc.redact`。
- Produces:
  - `schema.py`（`_Strict`，extra forbid）：`DirectUrl(url: str, editable: bool = False, commit_id: str | None = None)`、`Distribution(name: str, version: str, record_sha256: str | None, direct_url: DirectUrl | None = None)`、`RootSource = Literal["cwd", "script", "editable", "extra"]`（定義在這裡、`roots` 從這裡 import；`schema` 只 import `scan` 的 `RootKind`，不 import `roots`，免得循環）。
  - `generated.py`：`@dataclass(frozen=True) GeneratedEntry(name: str, resolver: Callable[[], Path | str | None], env: str | None = None)`；`register_generated(name: str, resolver: Callable[[], Path | str | None], *, env: str | None = None) -> None`（同名取代）；`generated_locations(*, env_only: bool = False) -> dict[str, Path]`（解析失敗或 None 的略過；`env_only` 只回有 `env` 且該變數有值的項目，值取自變數）。內建三項：`torch_inductor`（`TORCHINDUCTOR_CACHE_DIR`；否則 `sys.modules` 裡的 `torch._inductor.runtime.cache_dir_utils.cache_dir()`；否則 `Path(tempfile.gettempdir()) / f"torchinductor_{getpass.getuser()}"`）、`torch_extensions`（`TORCH_EXTENSIONS_DIR`；否則 `sys.modules` 裡的 `torch.utils.cpp_extension.get_default_build_root()`；否則 None）、`triton`（`TRITON_CACHE_DIR`；否則 `Path.home() / ".triton" / "cache"`）。解析時不 import 任何東西。
  - `roots.py`：`@dataclass(frozen=True) PlannedRoot(label: str, source: RootSource, path: Path, kind: RootKind, commit: str | None)`；`@dataclass(frozen=True) Editable(name: str, path: Path)`。
  - `canonical_name(name: str) -> str`（PEP 503：`re.sub(r"[-_.]+", "-", name).lower()`）；`clean_url(url: str) -> str`（`urlsplit`、netloc 去掉 `user:pass@`、query 與 fragment 清空、`urlunsplit`、`redact`）；`editable_dir(url: str) -> Path | None`（`file` scheme → `Path(url2pathname(unquote(path)))`，有非 localhost 的 netloc 時是 UNC；其他 scheme → None）。
  - `distribution_record(raw: Mapping[str, Any]) -> Distribution`：raw 是 PROBE 的一列（`name`、`version`、`record_sha256`、`direct_url` 原始 dict 或 None）；`DirectUrl(clean_url(url), dir_info.editable is True, vcs_info.commit_id)`。
  - `editables_of(raw: Sequence[Mapping[str, Any]]) -> list[Editable]`：`dir_info.editable` 為真、`editable_dir(原始 url)` 不是 None 的，依序。**從原始 url 算**（Review Focus 4）。這是測試的替換點：`probe()`（Task 6）與 `snapshot_inputs`（Task 11）一律經模組屬性 `roots.editables_of(...)` 呼叫。
  - `repo_top(path: Path) -> tuple[Path, str | None] | None`：`rev-parse --show-toplevel` 與 `rev-parse HEAD`（沒有 commit 時第二項 None）；不在 git 裡 None。
  - `script_of(command: Sequence[str], cwd: Path) -> Path | None`：第一個 casefold 後以 `.py` 結尾、`(cwd / token)` 是檔案的 token，`resolve()` 後回傳（絕對 token 照樣成立）。
  - `plan_roots(*, cwd: Path, command: Sequence[str], editables: Sequence[Editable], extra: Sequence[Path]) -> list[PlannedRoot]`：`cwd`（`repo_top(cwd)` 的頂層，否則 cwd 本身）→ `dist-<canonical_name>`（每個 editable）→ `root-<i>`（`i` 是在 `extra` 裡從 1 起算的位置，前面的被去重也不重編）；同一個 `path_key` 只留第一個；最後看 `script_of`：不在任何已規劃的 root 底下才加 `script`（它所在 repo 的頂層，否則它的目錄），排在 `cwd` 之後。每個 root 以 `root_kind` 定 kind、git 的帶 commit。
  - `skip_dirs_for(roots: Sequence[PlannedRoot], i: int, *, fixed: Sequence[Path]) -> list[Path]`：`fixed`（data root、configs root、排除目錄）加上嚴格位於 `roots[i].path` 之內的其他 root。
  - `checkpoint_exclusions(patterns: Sequence[str], cwd: Path, roots: Sequence[PlannedRoot]) -> list[Path]`：每個 pattern 以 `/`（`\` 先換成 `/`）切段，在第一個含 `*`、`?`、`[` 的段之前截斷；沒有萬用字元時去掉最後一段；相對的接在 `cwd` 後面；只留嚴格位於某個 root 之內的（空的、等於某個 root、或在 root 上層的都不排除）；去重、以 `path_key` 排序。
  - `generated_exclusions(roots: Sequence[PlannedRoot]) -> list[Path]`：`generated_locations(env_only=True)` 中嚴格位於某個 root 之內的。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_code_generated.py
def test_builtin_locations_from_env(monkeypatch, tmp_path):  # 三個變數各指一處 → 三個鍵；env_only=True 相同
def test_defaults_without_env(monkeypatch):  # 三個 delenv、sys.modules 沒有 torch
    assert locs["torch_inductor"] == Path(tempfile.gettempdir()) / f"torchinductor_{getpass.getuser()}"
    assert locs["triton"] == Path.home() / ".triton" / "cache" and "torch_extensions" not in locs
    assert generated_locations(env_only=True) == {}
def test_torch_extensions_asks_the_loaded_module(monkeypatch, tmp_path):  # sys.modules 塞假的 torch.utils.cpp_extension
def test_a_registered_entry_applies_but_is_not_env_only(tmp_path):
def test_resolving_imports_nothing():  # set(sys.modules) 前後相同
```

```python
# test_code_roots.py（git 的用 isolated_git）
def test_cwd_root_is_the_repository_top(tmp_path):  # cwd = repo / "sub"
    assert plan_roots(cwd=repo / "sub", command=[], editables=[], extra=[]) == [
        PlannedRoot("cwd", "cwd", repo.resolve(), "git", head)]
def test_cwd_outside_git_is_itself(tmp_path):  # kind "dir"、commit None
def test_a_script_in_another_repository_is_its_own_root(tmp_path):  # ["python", str(other / "t.py")] → ["cwd", "script"]、path == other
def test_a_relative_script_is_found_from_cwd(tmp_path, monkeypatch):  # Review Focus 3；monkeypatch.chdir(tmp_path / "elsewhere")
    # cwd=work、["python", "tools/run.py"]（work/tools/run.py）→ 只有 cwd
    # cwd=tmp/a、["python", "../b/train.py"]（tmp/b 不在 git）→ script 的 path == (tmp / "b").resolve()、kind "dir"
def test_editable_roots_come_from_the_raw_url(tmp_path):  # Review Focus 4
    deep = tmp_path / ("x" * 40) / "pkg"
    raw = {"name": "My_Pkg", "version": "1.0", "record_sha256": None,
           "direct_url": {"url": deep.as_uri(), "dir_info": {"editable": True}}}
    assert editables_of([raw]) == [Editable("My_Pkg", deep)]
    assert "<redacted>" in distribution_record(raw).direct_url.url
    assert [r.label for r in plan_roots(cwd=work, command=[], editables=editables_of([raw]), extra=[])] == ["cwd", "dist-my-pkg"]
def test_clean_url_drops_userinfo_query_and_fragment():
    assert clean_url("https://user:tok@host.example/x.git?token=abc#frag") == "https://host.example/x.git"
def test_non_editable_or_remote_urls_are_no_roots():
def test_two_spellings_of_one_root_are_one_root(tmp_path):  # extra=[work / ".", work / "sub" / ".."] → 只有 cwd；[x, y] 去掉 x 後 y 仍是 "root-2"
def test_skip_dirs_hold_nested_roots_only(tmp_path):
@pytest.mark.parametrize(("pattern", "excluded"), [("runs/exp/*.pt", "runs/exp"), ("runs/exp*/x.pt", "runs"),
    ("weights/best.pt", "weights"), ("*.pt", None), ("best.pt", None), ("../*.pt", None), ("**/*.pt", None)])
def test_checkpoint_globs_exclude_their_base_directory(tmp_path, pattern, excluded):
def test_an_absolute_glob_outside_every_root_excludes_nothing(tmp_path):
def test_generated_env_directories_inside_a_root_are_excluded(tmp_path, monkeypatch):  # TORCHINDUCTOR_CACHE_DIR=work/"ic" → [work/"ic"]
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code.roots`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — 同一指令；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/schema.py src/vcp/code/generated.py src/vcp/code/roots.py tests/unit/code/test_code_generated.py tests/unit/code/test_code_roots.py`；`git commit -m "feat(code): generated-code registry and code-root planning"`。

### Task 4: `code_snapshot` 產物、鎖與重用（§4.1、C3）

**Files:**
- Create: `src/vcp/code/snapshot.py`
- Test: Create `tests/unit/code/test_code_snapshot.py`

**Interfaces:**
- Consumes: Task 2 的 `RootScan`、`IndexEntry`、`identity_sha256`、`snapshot_json_bytes`、`index_jsonl_bytes`；`ArtifactWriter`、`store`、`vcp.core.lock.file_lock` / `LOCKS_DIR`。
- Produces:
  - 常數：`KIND = "code_snapshot"`、`SNAPSHOT_FILE = "snapshot.json"`、`INDEX_FILE = "index.jsonl"`、`FILES_DIR = "files"`、`SNAPSHOT_PATH = re.compile(r"^artifacts/code_snapshot/(code-[0-9a-f]{16})/snapshot\.json$")`、`MAX_PATH = 259`、`_WINDOWS = os.name == "nt"`。
  - `WriteReason = Literal["locked", "partial", "unwritable", "drift"]`；`@dataclass(frozen=True) SnapshotOutcome(snapshot_id: str, state: Literal["created", "reused"] | None, reason: WriteReason | None, kept: int, not_kept: int)`（有 reason 時 state 是 None）。
  - `lock_file(data_root: Path, snapshot_id: str) -> Path` = `data_root / LOCKS_DIR / f"code_snapshot-{snapshot_id}.lock"`（不用 `lock_path`：它雜湊的是路徑）。
  - `snapshot_spec(scan: RootScan) -> ArtifactSpec`：kind、id、`params={"rules": "1", "identity_sha256": ...}`，沒有 inputs。
  - `write_snapshot(data_root: Path, scan: RootScan, *, command: str) -> SnapshotOutcome`（`scan.reason` 不是 None 是呼叫端的 bug → `ValueError`）。
  - `snapshot_json_path(data_root: Path, snapshot_id: str) -> Path`；`load_index(data_root: Path, snapshot_id: str) -> list[IndexEntry]`（未排序、重複、壞列 → `IntegrityError("mismatch: ...")`）。
  - `@dataclass(frozen=True) SnapshotCheck(ok: bool, mismatch: list[str], missing: list[str], files: int, kept: int)`；`check_snapshot(data_root: Path, snapshot_id: str) -> SnapshotCheck`：`store.verify` 的結果 + 每個 `kept` 項在 manifest 有 `files/<path>` 且 sha 相同（缺 → missing、多出的 `files/*` 或 sha 不同 → mismatch）+ `snapshot_json_bytes(index)` 等於 `snapshot.json` 的位元組、身分前 16 碼等於 id（否則 mismatch `snapshot.json`）。`not_found:` / `partial:` 照 `load_manifest` 丟出。

`write_snapshot` 的步驟：

```text
1. with file_lock(lock_file(...), command=command, label=f"code_snapshot {id}")；VcpError 以 "locked:" 開頭 → reason "locked"。
2. 鎖內：store.is_partial → "partial"。已 commit → 重用：manifest 的 params.identity_sha256 不同，或 snapshot.json 的 sha
   不等於 sha256(snapshot_json_bytes(scan.entries)) → IntegrityError("mismatch: code_snapshot/<id> ...; move the directory
   aside and snapshot again")；否則 state "reused"，kept / not_kept 數 load_index 的（不是 scan 的）。
3. 宣告 id 之前的預檢（什麼都不寫）：_WINDOWS 且（某個 kept 項的 artifact_dir/files/<path> 長度 > MAX_PATH，或兩個 kept
   路徑 casefold 後相同）→ "unwritable"（Open point 15）。
4. ArtifactWriter.create(snapshot_spec(scan))：write_bytes(snapshot.json)、write_bytes(index.jsonl)、kept 項逐一
   add_file(f"files/{path}", root / path)；sha 不等於掃描時的、或來源不見（add_file 的 not_found: 或 FileNotFoundError）
   → 內部的 _Drift，在 with 之外接住 → "drift"（目錄照一般中斷的產物留成半途，附 failure.json）；
   OSError 且 is_disk_full（延遲 import vcp.train.disk）→ 原樣丟出，讓 own_writes 變 disk_full:；其他 OSError → "unwritable"。
   commit → "created"。
```

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_write_then_reuse_writes_nothing_new(roots, tmp_path):
    assert (first.state, again.state) == ("created", "reused") and again.snapshot_id == first.snapshot_id
    assert _tree(adir) == before  # 名稱與 mtime_ns
    assert sorted(p.name for p in adir.iterdir()) == ["files", "index.jsonl", "manifest.json", "snapshot.json", "spec.json"]
def test_one_content_from_two_paths_is_one_artifact(roots, tmp_path):
def test_a_full_scan_reuses_a_capped_copy_and_counts_not_kept(roots, tmp_path):  # capped 先寫 → all 的 scan 再寫：reused、not_kept 1
def test_same_id_other_identity_is_mismatch(roots, tmp_path):  # 換掉已 commit 的 snapshot.json → pytest.raises(IntegrityError, match="^mismatch:")
def test_two_processes_snapshot_one_content_once(roots, tmp_path):  # 兩個 sys.executable -c 子程序同時 scan_root + write_snapshot
    assert sorted(states) == ["created", "reused"] and len(list((roots.data / "artifacts/code_snapshot").iterdir())) == 1
def test_a_held_lock_times_out_as_locked(roots, tmp_path, monkeypatch):  # lock.WAIT_SECONDS 0.3、RETRY_SECONDS 0.05；子程序持鎖印 "held" 後睡 5 秒
    assert out.reason == "locked" and not artifact_dir(roots.data, "code_snapshot", out.snapshot_id).exists()
def test_a_partial_directory_blocks_until_clean(roots, tmp_path):  # ArtifactWriter.create(snapshot_spec(s)) 不 commit
    assert write_snapshot(...).reason == "partial"
    clean(roots.data, kind="code_snapshot", older_than=timedelta(0), apply=True)
    assert write_snapshot(...).state == "created"
def test_a_change_after_the_scan_is_drift(roots, tmp_path):  # reason "drift"、沒有 manifest.json
def test_a_file_deleted_after_the_scan_is_drift(roots, tmp_path):  # Review Focus 5：不丟例外
def test_unwritable_paths_claim_nothing(roots, tmp_path, monkeypatch):  # snapshot._WINDOWS True、MAX_PATH 40 → "unwritable"、沒有目錄；
    # A.py 與 a.py 都 kept（大小寫敏感的檔案系統才建得出來，否則 skip 這段）→ "unwritable"
def test_a_full_disk_is_raised_not_degraded(roots, tmp_path, monkeypatch):  # writer.write_once_stream 丟 OSError(ENOSPC) → pytest.raises(OSError)
def test_check_snapshot(roots, tmp_path):  # 剛寫的 ok；改 files/a.py → mismatch 有 "files/a.py"；改 snapshot.json → mismatch 有 "snapshot.json"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code/test_code_snapshot.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code.snapshot`）。
- [ ] **Step 3: 實作上列 Interfaces 與步驟。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/code tests/unit/artifact -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/snapshot.py tests/unit/code/test_code_snapshot.py`；`git commit -m "feat(code): content-addressed code_snapshot artifacts under a per-id lock"`。

### Task 5: run 端的紀錄：EnvSnapshot 三段、EvidenceRef 新 kind、保留名（§4.3、§8）

**Files:**
- Modify: `src/vcp/code/schema.py`（`EnvIdentity`、`RootReason`、`CodeRoot`、`CodeSection`、`CodeGrade`、`CODE_RANK`）
- Modify: `src/vcp/train/schema.py:151-167`（`EnvSnapshot` 多三欄）
- Modify: `src/vcp/train/env.py`（`EnvSections`、`read_sections`）
- Modify: `src/vcp/data/evidence_ref.py`、`src/vcp/data/evidence.py:62-93`（`check_evidence_file`）
- Modify: `src/vcp/train/run.py:900`、`src/vcp/cli_train.py:250`、`src/vcp/cli_eval.py:184`（改用 `evidence_count`）
- Test: Create `tests/unit/test_compat_v0170.py`；Modify `tests/unit/data/test_evidence_ref.py`、`tests/unit/train/test_session.py`、`tests/unit/train/test_env.py`、`tests/unit/train/test_run.py`

**Interfaces:**
- Produces:
  - `EnvIdentity(python: str, packages_sha256: str, gpus: str)`；`RootReason = Literal["too_large", "unreadable", "unwritable", "not_found", "locked", "partial", "drift"]`。
  - `CodeRoot(label: str, source: RootSource, kind: RootKind, path: str, commit: str | None, snapshot: str | None, reason: RootReason | None, files: int, untracked_code: int, untracked_code_paths: list[str], excluded: int, excluded_paths: list[str])`；`CodeSection(rules: int, excluded_dirs: list[str], roots: list[CodeRoot])`。
  - `CodeGrade = Literal["declared", "commit", "snapshot", "traced"]`；`CODE_RANK = {"declared": 0, "commit": 1, "snapshot": 2, "traced": 3}`（放這裡，`submit.schema` import 它不會拉進訓練層）。
  - `EnvSnapshot.code: CodeSection | None = None`、`distributions: list[Distribution] | None = None`、`identity: EnvIdentity | None = None`。
  - `@dataclass(frozen=True) EnvSections(git: GitInfo | None, code: CodeSection | None, identity: EnvIdentity | None)`；`read_sections(path: Path) -> EnvSections`：永不丟例外——檔不在、不是 JSON、不是物件 → 三個 None；三段各自 `model_validate`，`ValueError` 只讓那一段是 None。（§5.4 讀 `git` 與 `code`；run 依據的 claims 另讀 `identity`，Task 13。）
  - `evidence_ref.py`：`EvidenceKind = Literal["evidence", "label_set", "code_snapshot", "code_trace", "authorization"]`；`CODE_ROLE = "code"`、`CODE_TRACE_ROLE = "code_trace"`、`AUTHORIZATION_ROLE = "authorization"`、`RESERVED_ROLES = (LABELS_ROLE, CODE_ROLE, CODE_TRACE_ROLE, AUTHORIZATION_ROLE)`、`AUTHORIZATION_NAME = "authorization"`、`RESERVED_NAME_PREFIXES = ("code.", "code_trace.")`、`ORIGINAL_KINDS = ("evidence", "label_set")`；`is_reserved_name(name: str) -> bool`；`evidence_count(refs: list[EvidenceRef]) -> int`（`current` 之中 kind 在 `ORIGINAL_KINDS` 的）。
  - `check_evidence_file`：role 在 `RESERVED_ROLES`、或 `is_reserved_name(name)` → `ValidationFailed("role_reserved: ...", fields={"evidence_name": name})`；`labels` 那句訊息不變。`--evidence`（`preflight`、`ingest._attach`）與 `Session.attach_evidence` 都經過它。

- [ ] **Step 1: 先寫 golden（鎖現狀，改程式之前就綠）**

```python
# test_compat_v0170.py —— 字面在 0.17 的程式上產生、同一指令跑兩次確認一樣後照貼
TRAIN_YAML_0170 = """..."""  # 兩個 attempt、一個 checkpoint、evidence（evidence + label_set）的 TrainRecord，dump_yaml_model 寫出
RUN_YAML_0170 = """..."""
ENV_JSON_0170 = """..."""    # 0.17 的 env.1.json：git 有 status_sha256、disk 一個 volume
ENV_JSON_0100 = """..."""    # 0.10 的形狀：git 只有 commit、dirty
def test_a_0170_train_yaml_and_run_yaml_keep_their_bytes(tmp_path):  # load_yaml_model → dump_yaml_model → bytes 相等
def test_old_env_json_still_loads():  # 兩份都 EnvSnapshot.model_validate_json（Step 3 之後 code / distributions / identity 是 None）
```

Run: `uv run pytest tests/unit/test_compat_v0170.py -q` → PASS（改程式之前）。

- [ ] **Step 2: 寫會失敗的測試**

```python
def test_new_kinds_load(): EvidenceRef(name="code.cwd", role="code", kind="code_snapshot", ...)  # code_trace、authorization 同
@pytest.mark.parametrize(("name", "role"), [("code.cwd", "code.cwd"), ("code_trace.a1.1", "x"), ("authorization", "authorization"),
    ("x", "code"), ("x", "code_trace"), ("x", "authorization")])
def test_reserved_names_and_roles_are_refused(tmp_path, name, role):  # check_evidence_file → "^role_reserved:"
def test_session_attach_evidence_refuses_a_reserved_name(...):  # name "code.cwd" → role_reserved:、train.yaml 位元組不變
def test_train_run_evidence_refuses_a_reserved_name(roots, work, tmp_path):  # evidence=["authorization=f.txt"] → role_reserved:、沒有 run 目錄
def test_evidence_and_labels_count_only_the_original_kinds():  # 五種 kind 各一 → evidence_count 2、labels_field 只有 label_set 的 id
def test_env_sections_are_read_tolerantly(tmp_path):  # 非 JSON → (None, None, None)；git 壞 code 好 → (None, CodeSection, None)；ENV_JSON_0170 → git 有、code None
def test_an_env_snapshot_with_code_round_trips():
```

- [ ] **Step 3: 跑紅** — `uv run pytest tests/unit/test_compat_v0170.py tests/unit/data tests/unit/train -q`；Expected: FAIL（`literal_error` on kind、`cannot import name 'read_sections'`、沒有 `role_reserved:`）。
- [ ] **Step 4: 實作上列 Interfaces。**
- [ ] **Step 5: 跑綠** — `uv run pytest tests/unit/test_compat_v0170.py tests/unit/data tests/unit/train tests/unit/test_cli_train.py tests/unit/test_cli_eval.py -q`；Expected: PASS（golden 照綠）。
- [ ] **Step 6: Commit** — `git add src/vcp/code/schema.py src/vcp/train/schema.py src/vcp/train/env.py src/vcp/data/evidence_ref.py src/vcp/data/evidence.py src/vcp/train/run.py src/vcp/cli_train.py src/vcp/cli_eval.py tests/unit/test_compat_v0170.py tests/unit/data/test_evidence_ref.py tests/unit/train/test_session.py tests/unit/train/test_env.py tests/unit/train/test_run.py`；`git commit -m "feat(train): env snapshot code sections, new evidence kinds and reserved names"`。

### Task 6: PROBE 回報 distribution 與 identity（§4.3；§5.1 第 2 步用的資料）

**Files:**
- Modify: `src/vcp/train/env.py:22-121`（`PROBE`、`ProbeReport`、`probe`、`snapshot`）
- Modify: `src/vcp/code/roots.py`（`local_distributions`、`identity_of`）
- Test: `tests/unit/train/test_env.py`、`tests/unit/code/test_code_roots.py`

**Interfaces:**
- Consumes: Task 3 的 `distribution_record`、`editables_of`、`canonical_name`；Task 5 的 `EnvIdentity`。
- Produces:
  - `PROBE` 多輸出 `"distributions": [{"name", "version", "record_sha256", "direct_url"}]` 與 `"identity_python"`：`record_sha256` = `Path(d._path) / "RECORD"` 的位元組 sha（沒有該屬性或檔就 null）；`direct_url` = `json.loads(d.read_text("direct_url.json"))` 是 dict 時，否則 null；`identity_python = f"{sys.implementation.name}-{platform.python_version()}-{sys.implementation.cache_tag}-{sysconfig.get_platform()}"`。仍然只用標準庫、不印任何環境變數。
  - `local_distributions() -> list[dict[str, Any]]`：本程序的同一份清單（同一套讀法；測試讓兩者保持一致）。
  - `identity_of(raw: Sequence[Mapping[str, Any]], python: str, gpus: Sequence[str]) -> EnvIdentity`：非 editable 的每列 `f"{canonical_name(name)}=={version} {record_sha256 or '-'}"`，有 `vcs_info.commit_id` 時加 `f" {commit_id}"`；`packages_sha256 = sha256_text("".join(f"{line}\n" for line in sorted(lines)))`；`gpus = ",".join(gpus)`（Open point 12）。
  - `@dataclass(frozen=True) ProbeReport(data: dict[str, Any], distributions: list[Distribution], editables: list[Editable], identity: EnvIdentity, gpus: list[str], driver: str | None)`，屬性 `child_vcp -> str | None`（`canonical_name == "vcp"` 那個 distribution 的版本）。
  - `probe(python: Path | None) -> ProbeReport`：`_probe(python or sys.executable)`、`gpus()`、`roots.editables_of(raw)`（經模組屬性）。失敗照舊是 `VcpError`（ABORT）。
  - `snapshot(python, cwd, *, patch=None, disk=None, report: ProbeReport | None = None, code: CodeSection | None = None) -> EnvSnapshot`：`report` 是 None 時才跑 `probe(python)`；填 `distributions`、`identity`、`code`。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_probe_reports_distributions_and_identity():
    r = probe(None)
    assert r.identity.python == f"{sys.implementation.name}-{platform.python_version()}-{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
    pyd = next(d for d in r.distributions if canonical_name(d.name) == "pydantic")
    assert re.fullmatch(r"[0-9a-f]{64}", pyd.record_sha256) and r.child_vcp == vcp.__version__
def test_probe_and_local_distributions_agree():  # 依 name 排序後 [distribution_record(x) for x in local_distributions()] == probe(None).distributions
def test_a_fake_dist_info_is_recorded_cleaned(tmp_path, monkeypatch):
    # tmp/site/fake_pkg-1.0.dist-info/{METADATA, RECORD, direct_url.json: url "https://u:p@h.example/r.git?x=1#f", vcs_info.commit_id "c0ffee"}
    # monkeypatch.setenv("PYTHONPATH", str(tmp / "site"))
    assert d.direct_url == DirectUrl(url="https://h.example/r.git", editable=False, commit_id="c0ffee")
    assert d.record_sha256 == sha256_file(record)
def test_packages_sha_skips_editables_and_is_the_documented_sha():  # 兩列 raw、一列 editable → 只有另一列進 sha（測試裡照公式算）
def test_snapshot_uses_the_given_report(monkeypatch, tmp_path):  # envmod._probe 換成丟例外；snapshot(None, tmp_path, report=r) 照樣產出
```

既有的 `test_probe_prints_json_for_this_interpreter` 改期望的鍵（多 `distributions`、`identity_python`）。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_env.py tests/unit/code/test_code_roots.py -q`；Expected: FAIL（`cannot import name 'probe'`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/code -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/env.py src/vcp/code/roots.py tests/unit/train/test_env.py tests/unit/code/test_code_roots.py`；`git commit -m "feat(train): the environment probe reports distributions and an identity"`。

### Task 7: `train run` 寫快照（§5.1 第 2–5、7–9 步；沒有授權的那一半）

**Files:**
- Create: `src/vcp/train/coderun.py`
- Modify: `src/vcp/train/run.py`（`RunSpec.code_roots`；`_Prepared.report`、`.code`；`_prepare` 的順序；`train_run`；`_snapshot_env`；`_run_attempt`；`RunResult` 欄位；`_run_warnings`）
- Modify: `src/vcp/data/evidence.py`（`_ref` 改名公開的 `artifact_ref`，呼叫點跟著改）
- Modify: `src/vcp/cli_train.py`（`--code-root`；欄位）
- Modify: `tests/conftest.py`（autouse `_code_tracking_off`）、`tests/helpers.py`（`VCP_PACKAGE`、`trace_on`）
- Test: Create `tests/unit/train/test_run_code.py`；Modify `tests/unit/test_cli_train.py`

**Interfaces:**
- Consumes: Task 2 的 `scan_root`；Task 3 的 `plan_roots`、`skip_dirs_for`、`checkpoint_exclusions`、`generated_exclusions`；Task 4 的 `write_snapshot`；Task 5 的 `CodeSection` / `CodeRoot`；Task 6 的 `probe` / `ProbeReport`。
- Produces（`coderun.py`）：
  - `@dataclass(frozen=True) CodePlan(roots: list[PlannedRoot], scans: list[RootScan], excluded_dirs: list[Path])`。
  - `check_child_vcp(report: ProbeReport, venv: Path | None) -> None`：給了 venv、`child_vcp` 不是 None、`MAJOR.MINOR` 跟 `vcp.__version__` 不同 → `ValidationFailed(f"vcp_mismatch: the training venv has vcp {child}, this vcp is {ours}; install the same MINOR in {venv}", fields={"child_vcp": child})`。
  - `plan_code(*, cwd: Path, command: list[str], report: ProbeReport, code_roots: list[Path], patterns: list[str], data_root: Path, configs_root: Path) -> CodePlan`：`--code-root` 不是目錄 → `ValidationFailed("not_found: --code-root <p> is not a directory")`（Open point 7）；排除 = `checkpoint_exclusions` + `generated_exclusions`；每個 root `scan_root(..., skip_dirs=skip_dirs_for(roots, i, fixed=[data_root, configs_root, *exclusions]), keep="capped")`。
  - `@dataclass(frozen=True) RootResult(root: PlannedRoot, scan: RootScan, outcome: SnapshotOutcome | None)`，屬性 `snapshot: str | None`、`reason: RootReason | None`（scan 的，否則 outcome 的）。
  - `write_all(data_root: Path, plan: CodePlan, *, strict: bool) -> list[RootResult]`：沒有 reason 的 scan 才 `write_snapshot(..., command="train.run")`；`strict=False` 時 reason 只記在 root 上（`strict=True` 在 Task 14）。
  - `code_section(plan: CodePlan, results: list[RootResult], data_root: Path) -> CodeSection`（`store_path`；`rules=1`；清單截到 50；`files`、`excluded`、`untracked_code` 是完整數量）。
  - `attach_code_refs(data_root: Path, record: TrainRecord, results: list[RootResult], attempt: int) -> TrainRecord`：有快照的每個 root `artifact_ref(data_root, "code_snapshot", id, name=f"code.{label}", role="code", attempt=attempt, binding="cli")` → `bind_ref`（同名同 manifest 不加列，事件照既有的 `evidence` 記）。
  - `snapshot_state(results) -> tuple[Literal["ok", "partial", "none"], list[str]]`（第二項是沒有快照的 labels）；`untracked_total(results) -> int | None`（沒有 git root 時 None）；`untracked_notice(results) -> str | None` = `f"untracked_code={n}: {', '.join(前五個 '<label>:<path>')} (in the snapshot; git add or ignore them)"`。
- Produces（`run.py` / CLI）：
  - `RunSpec.code_roots: list[Path] = Field(default_factory=list)`；`RunResult.code_snapshot: str = "none"`、`code_missing: list[str] = []`、`untracked_code: int | None = None`。
  - `_prepare` 在 `check_names` 之後依序：`report = probe(python)`（失敗 ABORT、一個檔都沒寫）→ `check_child_vcp(report, spec.venv)` → `plan_code(...)` →（Task 14 的授權）→ `_check_disk`。
  - `train_run`：`with own_writes(): results = write_all(p.data_root, p.code, strict=False); a = _run_attempt(spec, p, results)`；`_snapshot_env` 傳 `report=p.report, code=code_section(...)`；`attach_cli_evidence` 之後 `attach_code_refs`；`untracked_notice` 有值時在 `_execute_attempt` 之前 `print(line, file=sys.stderr, flush=True)`。
  - warnings：`f"code_snapshot={state} (code_missing={','.join(labels)})"`（partial / none）、`f"untracked_code={n}"`（> 0）。
  - CLI：`--code-root DIR`（可重複，help `another directory whose code the run uses (repeatable)`）；`free_gib` 之後的一律欄位依序 `code`、`code_snapshot`、`code_trace`（本 task 只有 `code_snapshot`，Task 9、10 照這個順序插入）；既有的條件欄位之後依序 `untracked_code`（有 git root）、`code_missing`、`code_gaps`、`code_changed`、`authorization`（各 task 補）。
  - `tests/conftest.py`：

```python
@pytest.fixture(autouse=True)
def _code_tracking_off(monkeypatch):
    """No editable install is a code root and no child traces its imports unless the test calls
    helpers.trace_on: the test venv's editable vcp is this whole checkout, which every train run
    would scan and copy."""
    from vcp.code import roots
    monkeypatch.setenv("VCP_CODE_TRACE", "0")
    monkeypatch.setattr(roots, "editables_of", lambda raw: [])
```

  - `tests/helpers.py`：`VCP_PACKAGE = Path(vcp.__file__).resolve().parent`；`trace_on(monkeypatch) -> None`（`delenv("VCP_CODE_TRACE")`、`editables_of` → `[Editable("vcp", VCP_PACKAGE)]`）。Task 3、6 的測試在收集時就以名字 import `editables_of`，測到的是真的函式。
  - 先 `rg -n "train_run\(|\"train\", \"run\"" tests` 找沒給 `cwd` / `--cwd` 的 train run，各給一個 tmp 目錄（否則 cwd root 是這個 checkout）。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_run_code.py
def test_a_clean_repository_run_snapshots_its_code(roots, tmp_path, isolated_git):
    # git_repo(work, {"train.py": FAKE, ".gitignore": b"weights/\n"})；--checkpoints "weights/*.pt"
    assert res.code_snapshot == "ok" and res.untracked_code == 0 and res.code_missing == []
    root = env1["code"]["roots"][0]
    assert (root["label"], root["source"], root["kind"], root["reason"]) == ("cwd", "cwd", "git", None)
    assert re.fullmatch(r"code-[0-9a-f]{16}", root["snapshot"]) and env1["identity"]["python"] and env1["distributions"]
    refs = [r for r in load_record(roots.data, "r").evidence if r.kind == "code_snapshot"]
    assert [(r.name, r.role, r.binding, r.attempt) for r in refs] == [("code.cwd", "code", "cli", 1)]
    assert any(e["event"] == "evidence" and e["kind"] == "code_snapshot" for e in read_events(roots.data, "r"))
    assert "code.cwd" in [r.name for r in load_run(roots.data, "r").evidence]
    # --resume：env2 的 snapshot 相同、code_snapshot 的 ref 仍一列、artifacts/code_snapshot 只有一個目錄
def test_an_untracked_helper_is_kept_and_warned(roots, tmp_path, isolated_git, capsys):  # commit train.py（import helper），helper.py 不 add
    assert ("helper.py", True) in [(e.path, e.kept) for e in load_index(roots.data, sid)]
    assert (adir / "files" / "helper.py").read_bytes() == (work / "helper.py").read_bytes()
    assert res.untracked_code == 1 and "untracked_code=1" in res.warnings
    assert "untracked_code=1: cwd:helper.py (in the snapshot; git add or ignore them)" in capsys.readouterr().err
def test_a_too_large_root_is_partial_and_training_runs(roots, tmp_path, monkeypatch):  # cwd 三個 .py、--code-root extra 一個；rules.MAX_FILES = 2
    assert res.attempt.status == "finished" and (res.code_snapshot, res.code_missing) == ("partial", ["cwd"])
    assert "code_snapshot=partial (code_missing=cwd)" in res.warnings and env1 cwd root reason == "too_large"
@pytest.mark.parametrize("reason", ["drift", "locked", "partial", "unwritable"])
def test_a_snapshot_that_cannot_be_written_degrades_without_authorization(roots, work, monkeypatch, reason):
    # coderun.write_snapshot 換成回 SnapshotOutcome(id, None, reason, 0, 0)
    assert res.attempt.status == "finished" and env1 cwd root reason == reason and res.code_snapshot == "none"
    assert not [r for r in load_record(roots.data, "r").evidence if r.kind == "code_snapshot"]
def test_the_child_vcp_must_share_the_minor(roots, work, tmp_path, monkeypatch):
    # 假 venv（Scripts/python.exe 與 bin/python 空檔）；run_module.probe 換成回 child_vcp "0.17.0" 的 ProbeReport
    assert ei.value.fields["child_vcp"] == "0.17.0" and str(ei.value).startswith("vcp_mismatch:") and not run_dir(roots.data, "r").exists()
def test_a_failing_probe_aborts_before_any_write(roots, work, monkeypatch):  # envmod.PROBE = "import sys; sys.exit(3)"
    assert ei.value.status == "ABORT" and not run_dir(roots.data, "r").exists()
def test_an_unignored_secret_is_not_read(roots, tmp_path, isolated_git, monkeypatch):  # commit deploy.pem、未追蹤 kaggle.json、_never_open
    assert set(root["excluded_paths"]) == {"deploy.pem", "kaggle.json"} and root["excluded"] == 2
def test_script_and_extra_roots_are_recorded(roots, tmp_path, isolated_git):  # labels ["cwd", "script", "root-1"]、sources 對
def test_a_missing_code_root_fails_before_any_write(roots, work, tmp_path):  # "^not_found:"
def test_checkpoint_directories_are_excluded_and_recorded(roots, work):  # --checkpoints "runs/exp/*.pt"，work/runs/exp/gen.py
    assert "runs/exp/gen.py" not in index_paths and env1["code"]["excluded_dirs"] == [store_path(work / "runs/exp", roots.data)]
def test_framework_outputs_do_not_change_the_identity(roots, tmp_path, isolated_git):  # C1：child 寫 args.yaml hparams.yaml config.json metrics.json；--resume 後 snapshot 相同
# test_cli_train.py
def test_train_run_cli_code_fields(roots, tmp_path, isolated_git):
    # git cwd → "code_snapshot=ok" 與 "untracked_code=0"；plain cwd → 有 "code_snapshot=ok"、沒有 "untracked_code="
    # --code-root <不存在> → exit 1、"not_found:"、"run=r"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_run_code.py tests/unit/test_cli_train.py -q`；Expected: FAIL（`no attribute 'code_snapshot'`、`No such option: --code-root`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py tests/unit/test_e2e_evidence.py -q`；Expected: PASS（既有測試的 warnings 不變：plain cwd、沒有 git root、沒有 editable root）。
- [ ] **Step 5: Commit** — `git add src/vcp/train/coderun.py src/vcp/train/run.py src/vcp/data/evidence.py src/vcp/cli_train.py tests/conftest.py tests/helpers.py tests/unit/train/test_run_code.py tests/unit/test_cli_train.py`（加上 Step 3 補了 cwd 的測試檔）；`git commit -m "feat(train): train run snapshots every code root before the first write"`。

### Task 8: 結束時的比對（§5.3 前兩點）

**Files:**
- Modify: `src/vcp/train/coderun.py`、`src/vcp/train/run.py`（`_note_changes`、`RunResult`、`_run_warnings`）、`src/vcp/cli_train.py`
- Test: `tests/unit/train/test_run_code.py`

**Interfaces:**
- Consumes: Task 4 的 `load_index`；Task 7 的 `RootResult`。
- Produces:
  - `@dataclass(frozen=True) CodeChange(count: int, paths: list[str], roots: list[str])`；`changed_since_snapshot(data_root: Path, results: list[RootResult]) -> CodeChange`：每個有快照的 root，`load_index` 列過的檔逐一重算（不見或 sha 不同算改了；不找新檔）；`paths` 前 5 個 `<label>:<path>`；`roots` 是有改動的 labels（root 順序）。
  - `_note_changes` 在 `git_changed` 之後：`count > 0` → `append_event(p.data_root, spec.run_id, "note", n, key="code_changed", value=count, paths=paths, roots=roots)`；`RunResult.code_changed: int = 0`；warning `f"code_changed={count}"`；VERDICT `code_changed=<n>` 只在 > 0。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_a_listed_file_the_child_changes_is_noted(roots, work):  # child 結束前 append 到 helper.py
    note = next(e for e in read_events(roots.data, "r") if e.get("key") == "code_changed")
    assert (note["value"], note["paths"], note["roots"], note["attempt"]) == (1, ["cwd:helper.py"], ["cwd"], 1)
    assert res.code_changed == 1 and "code_changed=1" in res.warnings
def test_a_new_file_or_nothing_is_no_change(roots, work):  # child 寫 new.py → 沒有 code_changed note、code_changed 0
def test_a_deleted_listed_file_is_a_change(roots, work):
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_run_code.py -k change -q`；Expected: FAIL（沒有 note）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/coderun.py src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_run_code.py`；`git commit -m "feat(train): a listed code file changed during the run becomes a code_changed note"`。

### Task 9: 子程序的 import 追蹤（§4.4、§5.2、§5.3 第三點、C2）

**Files:**
- Create: `src/vcp/code/trace.py`
- Modify: `src/vcp/code/schema.py`（`TraceDoc`）
- Modify: `src/vcp/train/session.py:42-50`（`Session.current` 最後延遲 `from vcp.code import trace` 並 `trace.arm(session)`）
- Modify: `src/vcp/train/coderun.py`（`TraceSummary`、`trace_summary`）、`src/vcp/train/run.py`（`RunResult.code_trace`、`code_gaps`、warning）、`src/vcp/cli_train.py`
- Test: Create `tests/unit/code/test_code_trace.py`；Modify `tests/unit/train/test_run_code.py`

**Interfaces:**
- Consumes: Task 3 的 `generated_locations`、`rules.skipped_segment`；Task 4 的 `load_index`、`snapshot_json_path`；Task 5 的 `read_sections`；`bind_ref`、`artifact_ref`。
- Produces:
  - `TraceDoc(_Strict)`：`schema_version: int = 1`、`run_id`、`attempt: int`、`executable`、`python`、`roots: list[dict[str, str]]`（`{label, snapshot}`）、`counts: dict[str, int]`（七個鍵一律在：`root generated stdlib distribution outside unlisted changed`）、`generated_by: dict[str, int]`、`outside: list[str]`、`unlisted: list[str]`、`changed: list[str]`、`complete: bool`、`taken_at`、`vcp_version`。
  - `trace.py`：`KIND = "code_trace"`、`TRACE_FILE = "trace.json"`；`ModuleClass = Literal["generated", "root", "changed", "unlisted", "stdlib", "distribution", "outside"]`；`@dataclass(frozen=True) TracedRoot(label: str, path: Path, snapshot: str, index: Mapping[str, str])`；`@dataclass(frozen=True) Classified(cls: ModuleClass, shown: str, generated_by: str | None)`；`@dataclass(frozen=True) TraceResult(counts: dict[str, int], generated_by: dict[str, int], outside: list[str], unlisted: list[str], changed: list[str], complete: bool)`。
  - `classify_file(path: Path, *, roots: Sequence[TracedRoot], generated: Mapping[str, Path], stdlib: Sequence[Path], site: Sequence[Path]) -> Classified`：先對到的為準——生成位置 → 最內層的 root，但相對路徑含 `skipped_segment` 的段或有含 `pyvenv.cfg` 的上層時不算「在 root 裡」、往下走（Review Focus 1；Open point 4）：在 index 且 sha 相同 `root`、在 index 但 sha 不同或檔不見 `changed`、不在 index `unlisted` → stdlib → distribution（site）→ `outside`。任一上層是檔（zip 裡的模組）→ `outside`。只雜湊 root 底下的檔。
  - `classify_modules(modules: Mapping[str, ModuleType], *, roots, generated, stdlib, site) -> TraceResult`：每個 `__file__` 是 str 的模組（含 `__main__`），同一個檔只算一次；`outside` 是絕對 posix、`unlisted` / `changed` 是 `<label>:<path>`，各排序後截 50；`complete = outside + unlisted + changed == 0`。不 import 任何東西。
  - `stdlib_dirs() -> list[Path]`：`sysconfig` 的 `stdlib`、`platstdlib`；`os.name == "nt"` 時再加存在的 `Path(sys.base_prefix) / "DLLs"`、`Path(sys.prefix) / "DLLs"`（Review Focus 2；Open point 5）。`site_dirs() -> list[Path]`：`purelib`、`platlib`、`site.getsitepackages()`、`site.getusersitepackages()`（各自防例外）。
  - `arm(session: Session) -> None`：同一程序一次（模組旗標）；不登記：`VCP_CODE_TRACE == "0"`、`multiprocessing.parent_process() is not None`、`os.environ.get("RANK", "")` 不是 `""` 也不是 `"0"`；`atexit.register(_at_exit, session.run_id, session.data_root, session.attempt, os.getpid())`。
  - `_at_exit(run_id: str, data_root: Path, attempt: int, pid: int) -> None`：pid 不同就 return；`try: write_trace(...)`（呼叫時才查模組全域名）`except BaseException as e: sys.stderr.write(f"[vcp] code trace skipped: {type(e).__name__}: {e}\n")`。
  - `write_trace(run_id: str, data_root: Path, attempt: int, *, modules: Mapping[str, ModuleType]) -> str`：train.yaml → 這個 attempt 的 `env` → `read_sections` → 有快照的 roots 組 `TracedRoot`；分類（`generated_locations()`、`stdlib_dirs()`、`site_dirs()`）；`ArtifactSpec(kind="code_trace", id=f"{run_id}-a{attempt}-t{seq}", dataset=record.dataset, plan_id=record.plan_id, params={"run": run_id, "attempt": str(attempt)}, inputs=[InputRef(name=f"snapshot.{label}", path=str(snapshot_json_path(...)))])`（Open point 9）；`seq` 從「這個 attempt 已有的 `code_trace` ref 數 + 1」起，`exists:` 就 +1，最多 100 次；`write_json(TRACE_FILE, doc)`、commit；`bind_ref(artifact_ref(..., "code_trace", id, name=f"code_trace.a{attempt}.{seq}", role="code_trace", attempt=attempt, binding="session"))`。回傳 id。
  - `coderun`：`@dataclass(frozen=True) TraceSummary(state: Literal["complete", "incomplete", "none"], gaps: int)`；`trace_summary(data_root: Path, record: TrainRecord, attempt: int) -> TraceSummary`（這個 attempt 的 `code_trace` ref；讀不動的 trace 算 incomplete；gaps = 各份 `outside + unlisted + changed` 的和）。`RunResult.code_trace: str = "none"`、`code_gaps: int = 0`；warning `f"code_trace=incomplete (code_gaps={gaps})"`；VERDICT `code_trace=` 一律、`code_gaps=` 只在 incomplete。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_code_trace.py：假的 generated / root / stdlib / site 目錄，modules 是帶 __file__ 的 types.ModuleType
def test_each_module_class(tmp_path):
    assert res.counts == {"root": 1, "generated": 1, "stdlib": 1, "distribution": 1, "outside": 2, "unlisted": 1, "changed": 2}
    assert res.generated_by == {"torch_inductor": 1} and res.changed == ["cwd:gone.py", "cwd:moved.py"] and not res.complete
def test_a_module_in_a_venv_inside_a_root_is_a_distribution(tmp_path):  # Review Focus 1
    # root/.venv/Lib/site-packages/np/__init__.py（site=[那個目錄]）→ distribution；root/env/pyvenv.cfg 底下的也是
def test_windows_dlls_are_stdlib(tmp_path, monkeypatch):  # Review Focus 2：trace 模組的 os.name → "nt"、sys.base_prefix → tmp/base（有 DLLs/）
    assert classify_file(base / "DLLs" / "_socket.pyd", roots=[], generated={}, stdlib=stdlib_dirs(), site=[]).cls == "stdlib"
def test_generated_modules_are_not_gaps(tmp_path, monkeypatch):  # 三個環境變數各一個模組 → generated 3、generated_by 各 1、complete True
def test_registered_entries_apply_and_hf_remote_code_is_outside(tmp_path, monkeypatch):
    # register_generated("myjit", lambda: d)；HOME 指 tmp，~/.cache/huggingface/modules/transformers_modules/x.py → outside
def test_classifying_imports_nothing(tmp_path):  # set(sys.modules) 前後相同
@pytest.mark.parametrize("env", [{"VCP_CODE_TRACE": "0"}, {"RANK": "1"}])
def test_arm_skips_the_off_switch_and_other_ranks(monkeypatch, env):  # trace.atexit.register 換成記錄器；RANK=0 登記一次，再 arm 不再登記
def test_arm_skips_a_worker(monkeypatch):  # trace.multiprocessing.parent_process → lambda: object()
def test_at_exit_in_another_pid_does_nothing(monkeypatch):  # _at_exit(..., os.getpid() + 1) → write_trace 沒被呼叫
# test_run_code.py（helpers.trace_on）
def test_a_session_child_importing_only_root_code_is_traced(roots, work, monkeypatch):
    # child：import helper（work/helper.py）；from vcp.train import Session；Session.current()
    assert (res.code_trace, res.code_gaps) == ("complete", 0)
    assert doc["complete"] and doc["counts"]["outside"] == 0 and doc["roots"][0]["label"] == "cwd"
    assert [r.name for r in load_record(roots.data, "r").evidence if r.kind == "code_trace"] == ["code_trace.a1.1"]
def test_a_module_outside_every_root_makes_the_trace_incomplete(roots, work, tmp_path, monkeypatch):
    # child 把 tmp/elsewhere 放進 sys.path、import stray
    assert (res.code_trace, res.code_gaps) == ("incomplete", 1) and "code_trace=incomplete (code_gaps=1)" in res.warnings
def test_a_failing_trace_keeps_the_exit_code(roots, work, monkeypatch):
    # child：import vcp.code.trace as t; t.write_trace = lambda *a, **k: 1 / 0；Session.current()；sys.exit(3)
    assert res.attempt.exit_code == 3 and res.code_trace == "none"
    assert "[vcp] code trace skipped: ZeroDivisionError" in console_text
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code/test_code_trace.py tests/unit/train/test_run_code.py -k "trace or classify or arm" -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code.trace`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/code tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_access.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/trace.py src/vcp/code/schema.py src/vcp/train/session.py src/vcp/train/coderun.py src/vcp/train/run.py src/vcp/cli_train.py tests/unit/code/test_code_trace.py tests/unit/train/test_run_code.py`；`git commit -m "feat(code): a Session child traces its imports into a code_trace artifact at exit"`。

### Task 10: 程式碼等級與 `train status`（§5.4、§5.8 第一點）

**Files:**
- Create: `src/vcp/code/grade.py`
- Modify: `src/vcp/train/run.py`（`RunResult.code`）、`src/vcp/train/status.py`（`StatusResult.code`）、`src/vcp/cli_train.py`
- Test: Create `tests/unit/code/test_code_grade.py`；Modify `tests/unit/train/test_run_code.py`、`tests/unit/test_cli_train.py`

**Interfaces:**
- Consumes: Task 5 的 `CodeGrade`、`CODE_RANK`、`read_sections`；`ref_intact`；Task 9 的 `TraceDoc`。
- Produces:
  - `lowest(grades: Iterable[CodeGrade]) -> CodeGrade`（空的是 `declared`）。
  - `@dataclass(frozen=True) RootCode(label: str, snapshot: str | None, reason: str | None)`；`AttemptCode(n: int, grade: CodeGrade, roots: list[RootCode])`；`CodeInfo(grade: CodeGrade, attempts: list[AttemptCode])`，`to_json() -> dict` = `{"grade", "attempts": [{"n", "grade", "roots": [{"label", "snapshot", "reason"}]}]}`。
  - `notes_by_attempt(data_root: Path, run_id: str) -> dict[int, set[str]]`：`note` 事件的 `key`，容錯讀（壞列略過，同 `train/status._started_globs`）。
  - `code_info(data_root: Path, run_id: str) -> CodeInfo`：讀不動的一律 `declared`，不丟 `VcpError` / `OSError` / `ValueError`。規則照 §5.4 的表：「快照齊全」= 這個 attempt 的 `code.roots` 非空（Open point 12）、每個 root 都有 snapshot id、train.yaml 的 evidence 裡有指向該 id 且 `ref_intact` 的 `code_snapshot` ref；`traced` = 齊全 + 至少一份 `attempt == n` 的 `code_trace` ref，每份完好且 trace.json `complete`；`snapshot` = 齊全且沒有這個 attempt 的 `code_changed` note；`commit` = env 的 `git` 有 commit、沒有這個 attempt 的 `git_changed` note，而且有 `status_sha256` 時 `modified == 0 or diff_sha256`、沒有時 `not dirty`；其他 `declared`。run = 各 attempt 的最低（沒有 attempt → declared）。沒有 train.yaml：至少一個 current 的 `code_snapshot` ref 且都完好 → `snapshot`，否則 `declared`。fused（`card.source.framework == FUSE_FRAMEWORK` 且 fuse record 在；延遲 import，同 `measure/provenance._fused`）→ 成員的最低。
  - `RunResult.code: CodeGrade = "declared"`（attempt 之後算）；`StatusResult.code: CodeInfo`；`train status` VERDICT 一律 `code=`、payload 多 `"code": info.to_json()`、人看的行每個 attempt 一行 `attempt <n>: code=<grade>`。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_code_grade.py：手寫 train.yaml、env.<n>.json、事件，快照與 trace 用真的產物
def test_traced_needs_complete_snapshots_and_complete_intact_traces(roots, tmp_path):
def test_snapshot_needs_complete_snapshots_and_no_code_changed(roots, tmp_path):
@pytest.mark.parametrize(("git", "notes", "grade"), [
    ({"commit": C, "dirty": True, "modified": 0, "untracked": 2, "status_sha256": S}, set(), "commit"),
    ({"commit": C, "dirty": True, "modified": 1, "status_sha256": S, "diff_sha256": D}, set(), "commit"),
    ({"commit": C, "dirty": True, "modified": 1, "status_sha256": S}, set(), "declared"),
    ({"commit": C, "dirty": False}, set(), "commit"),
    ({"commit": C, "dirty": True}, set(), "declared"),
    ({"commit": C, "dirty": False}, {"git_changed"}, "declared"),
    (None, set(), "declared")])
def test_commit_rules(roots, tmp_path, git, notes, grade):
def test_a_0170_env_json_grades_commit(roots, tmp_path):  # ENV_JSON_0170（Task 5 的 golden）
def test_code_changed_drops_snapshot_to_the_git_grade(roots, tmp_path):
def test_a_deleted_snapshot_drops_the_grade(roots, tmp_path):  # rmtree artifacts/code_snapshot/<id> → commit
def test_the_run_takes_its_lowest_attempt(roots, tmp_path):
def test_an_unreadable_env_is_declared(roots, tmp_path):
def test_an_ingest_run_with_intact_code_refs_is_snapshot(roots, tmp_path):  # run.yaml 手掛 code.cwd → snapshot；快照壞了 → declared
def test_a_fused_run_takes_its_lowest_member(roots, tmp_path):  # backup_fixtures.make_fusion
# test_run_code.py
def test_train_run_reports_its_code_grade(roots, work):  # 乾淨 → res.code == "snapshot"、code_trace "none"；child 改 helper.py 的另一個 run → "declared"
def test_a_traced_run_is_traced(roots, work, monkeypatch):  # trace_on → "traced"
# test_cli_train.py
def test_train_status_always_reports_code(roots, work):  # "code=snapshot"；--json result["code"]["attempts"][0]["grade"] == "snapshot"
def test_train_status_verify_names_a_broken_snapshot(roots, work):  # 改 files/<x> → "status=WARN"、"drift=1"、輸出有 "drift: evidence:code.cwd"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code/test_code_grade.py tests/unit/train/test_run_code.py tests/unit/test_cli_train.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code.grade`、沒有 `code=`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/code tests/unit/train tests/unit/test_cli_train.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/grade.py src/vcp/train/run.py src/vcp/train/status.py src/vcp/cli_train.py tests/unit/code/test_code_grade.py tests/unit/train/test_run_code.py tests/unit/test_cli_train.py`；`git commit -m "feat(code): code grade traced > snapshot > commit > declared, computed on read"`。

### Task 11: `ArtifactWriter` 的 `code_snapshot.<label>` 與 `snapshot_inputs`（§5.5、VCP-004 驗收 1–2）

**Files:**
- Create: `src/vcp/code/process.py`
- Modify: `src/vcp/artifact/schema.py`（`CODE_INPUT_PREFIX = "code_snapshot."`、`AUTHORIZATION_INPUT = "authorization"`）
- Modify: `src/vcp/artifact/writer.py:59-79`（`create`：`resolve_inputs` 之後、`mkdir` 之前 `_check_reserved_inputs`）、`:228-251`（`commit`：`_check_drift` 之後再一次）
- Test: Create `tests/unit/artifact/test_writer_code.py`、`tests/unit/code/test_code_process.py`；Modify `tests/unit/test_cli_artifact.py`

**Interfaces:**
- Consumes: Task 3 的 `plan_roots`、`local_distributions`；Task 4 的 `write_snapshot`、`check_snapshot`、`SNAPSHOT_PATH`、`load_index`；Task 9 的 `classify_modules`、`stdlib_dirs`、`site_dirs`。
- Produces:
  - `writer._check_reserved_inputs(spec: ArtifactSpec, data_root: Path) -> None`：每個名稱以 `code_snapshot.` 開頭的 input：`SNAPSHOT_PATH.fullmatch(ref.path)` 不成立 → `ValidationFailed("not_found: input <name> is not artifacts/code_snapshot/<id>/snapshot.json")`；`check_snapshot` 不 ok → `IntegrityError("mismatch: ...")`；接著 `process.check_imports(spec, data_root)`（延遲 import：`vcp.code.snapshot` import 了 writer）。`authorization` 那一支在 Task 12。
  - `process.py`：模組屬性 `loaded_modules: Callable[[], Mapping[str, ModuleType]] = lambda: sys.modules`、`main_file: Callable[[], Path | None]`（`__main__.__file__` 是存在的檔才回）——測試替換它們；`@dataclass(frozen=True) Tracked(label: str, path: Path, snapshot: str, index: dict[str, str])`；`_TRACKED: dict[str, Tracked]`（以 snapshot id 為鍵）。
  - `strict(root: PlannedRoot, scan: RootScan, outcome: SnapshotOutcome | None) -> str`：明要快照的對照，回 id 或丟出：scan `too_large` → `ValidationFailed("too_large: ...")`；scan `unreadable` → `VcpError("code_unavailable: ...", fields={"code_root": label, "cause": "unreadable"})`（Open point 13）；`drift` → `IntegrityError("drift: ...", fields={"code_root": label})`；`locked` / `partial` / `unwritable` → `VcpError("code_unavailable: ...", fields={"code_root": label, "cause": reason})`。
  - `snapshot_inputs(data_root: Path, extra_roots: Sequence[Path] = ()) -> list[InputRef]`：`plan_roots(cwd=Path.cwd(), command=[str(main_file())] if main_file() else [], editables=roots.editables_of(local_distributions()), extra=extra_roots)`；跳過 data root 與（解析得到時的）configs root；沒有 checkpoint 排除；`keep="capped"`；`write_snapshot(..., command="snapshot_inputs")`；`strict`；登記 `_TRACKED`；依 root 順序回 `InputRef(name=f"code_snapshot.{label}", path=str(snapshot_json_path(...)), sha256=sha256_file(...))`。
  - `check_imports(spec: ArtifactSpec, data_root: Path) -> None`：這個 spec 的 `code_snapshot.*` input 中 id 在 `_TRACKED` 的 roots；沒有就 return；`classify_modules(loaded_modules(), roots=..., generated=generated_locations(), stdlib=stdlib_dirs(), site=site_dirs())`；`outside + unlisted > 0` → `ValidationFailed(f"code_outside: {n} imported module(s) lie outside the snapshot roots: {前五個}; pass their directories as extra_roots")`；`changed > 0` → `IntegrityError("drift: imported <label:path> changed since its snapshot")`；`generated` 不算。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_writer_code.py；proj fixture：monkeypatch.chdir(proj)、process.main_file → proj/job.py、
#   process.loaded_modules → {"__main__": job, "helper": proj/helper.py, "json": json}
@pytest.mark.parametrize("bad", ["not_a_snapshot", "tampered"])
def test_a_wrong_or_broken_snapshot_input_leaves_no_directory(roots, tmp_path, bad):
    # not_a_snapshot：code_snapshot.cwd=<artifacts/evidence/.../manifest.json> → "^not_found:"；tampered：改 files/a.py → "^mismatch:"
    assert not (roots.data / "artifacts" / "features" / "f1").exists()
def test_an_imported_file_changed_during_the_job_fails_commit(roots, proj):
    w = ArtifactWriter.create(ArtifactSpec(kind="features", id="f1", inputs=snapshot_inputs(roots.data)), data_root=roots.data)
    (proj / "helper.py").write_text("x = 2\n"); pytest.raises(IntegrityError, match="^drift:") on w.commit()
def test_a_file_nobody_imported_may_change(roots, proj):  # 改 proj/unused.py → commit 成功
def test_a_module_outside_the_roots_fails_at_create(roots, proj, tmp_path):  # 多一個 tmp/elsewhere/stray.py → "^code_outside:"、沒有目錄
def test_generated_modules_do_not_trigger_code_outside(roots, proj, monkeypatch):  # TORCHINDUCTOR_CACHE_DIR 底下的模組 → create 成功
def test_extra_roots_cover_a_module(roots, proj, tmp_path):  # extra_roots=[elsewhere] → 成功、input 名 "code_snapshot.root-1"
# test_code_process.py
@pytest.mark.parametrize(("reason", "word", "status"), [("locked", "code_unavailable:", "ABORT"),
    ("partial", "code_unavailable:", "ABORT"), ("unwritable", "code_unavailable:", "ABORT"), ("drift", "drift:", "FAIL")])
def test_snapshot_inputs_has_no_fallback(roots, proj, monkeypatch, reason, word, status):  # process.write_snapshot 回該 reason
    assert ei.value.status == status and str(ei.value).startswith(word) and (reason == "drift" or ei.value.fields["cause"] == reason)
def test_snapshot_inputs_refuses_a_too_large_root(roots, proj, monkeypatch):  # rules.MAX_FILES = 0 → "^too_large:"
def test_snapshot_inputs_names_and_pins_each_root(roots, proj):  # ["code_snapshot.cwd"]、sha == sha256_file(path)、SNAPSHOT_PATH 對得上
# test_cli_artifact.py
def test_cli_create_checks_a_snapshot_input_only_statically(roots, tmp_path):
    # artifact create --input code_snapshot.cwd=<snapshot.json> --file x → exit 0（CliRunner 的程序有 root 外的模組也沒關係）；壞快照 → exit 1 "mismatch:"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/artifact/test_writer_code.py tests/unit/code/test_code_process.py tests/unit/test_cli_artifact.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.code.process`、沒有被拒）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/artifact tests/unit/code tests/unit/test_cli_artifact.py tests/unit/data -q`；Expected: PASS（既有產物的 input 名都不以 `code_snapshot.` 開頭，行為不變）。
- [ ] **Step 5: Commit** — `git add src/vcp/code/process.py src/vcp/artifact/schema.py src/vcp/artifact/writer.py tests/unit/artifact/test_writer_code.py tests/unit/code/test_code_process.py tests/unit/test_cli_artifact.py`；`git commit -m "feat(artifact): code_snapshot inputs are checked and in-process imports traced"`。

### Task 12: 授權產物與消耗（§4.5、§4.6 左欄、§5.6、VCP-006）

**Files:**
- Create: `src/vcp/artifact/authorization.py`
- Modify: `src/vcp/artifact/writer.py`（`_check_reserved_inputs` 的 `authorization` 一支）
- Test: Create `tests/unit/artifact/test_authorization.py`；Modify `tests/unit/test_cli_artifact.py`

**Interfaces:**
- Consumes: `store`、`lineage.list_manifests` / `successors_of`、`ArtifactWriter`、`vcp.core.paths.run_path`。
- Produces:
  - `KIND = "authorization"`、`FILE = "authorization.json"`、`Consumer = Literal["artifact", "train"]`；`TRAIN_FIXED = frozenset({"dataset", "plan_id", "seed", "param:samples_hash", "param:trained_on", "param:config_sha256", "param:command_sha256", "input:plan", "env:python", "env:packages", "env:gpus"})`、`TRAIN_PREFIXES = ("input:code_snapshot.", "input:evidence.", "input:labels.")`。
  - `in_vocabulary(name: str, consumer: Consumer) -> bool`：artifact = `dataset`、`plan_id`、`seed`、`param:<非空>`、`input:<非空，不是 authorization>`；train = `TRAIN_FIXED` 或 `TRAIN_PREFIXES` 開頭且後面非空。
  - `AuthorizationDoc(_Strict)`：`schema_version: int = 1`、`consumer: Consumer`、`target_kind: str | None`、`basis: str`、`claims: dict[str, str]`、`claims_sha256: str`、`freed: list[str]`、`reason: str`；驗證：claims 非空且鍵已排序、`claims_sha256 == sha256_json(claims)`、`consumer == "artifact"` 恰在 `target_kind` 有值時。
  - `parse_basis(text: str) -> tuple[Literal["artifact", "run"], str, str]`：`artifact:<kind>/<id>` → `("artifact", kind, id)`；`run:<id>` → `("run", id, "")`；其他 `invalid:`。
  - `artifact_claims(spec: ArtifactSpec) -> dict[str, str]`：已 resolve 的 spec；`dataset` / `plan_id` / `seed`（字串）有值才有；`param:<k>`；`authorization` 以外每個 input 的 `input:<name>` = sha；排序。
  - `is_superseded(data_root: Path, kind: str, artifact_id: str) -> bool`（`successors_of(list_manifests(data_root, kind))`）。
  - `@dataclass(frozen=True) AuthorizeSpec(authorization_id: str, basis: str, consumer: Consumer, target_kind: str | None, free: list[str], set_: list[str], reason: str, supersedes: str | None = None)`；`AuthorizeResult(authorization_id: str, basis: str, consumer: Consumer, claims: dict[str, str], freed: list[str], manifest_sha256: str)`。
  - `authorize(spec: AuthorizeSpec, *, data_root: Path, run_claims: Callable[[str], dict[str, str]] | None = None) -> AuthorizeResult`。依序：`validate_name(id)`；consumer / target_kind 形狀不對、reason 空 → `invalid:`；依據：artifact → `load_manifest`（`not_found:` / `partial:`）、`store.verify` 失敗 → `IntegrityError("mismatch: ...")`（Open point 11）、被 supersede → `superseded:`，claims = `artifact_claims(manifest.spec)`；run → `run_path(...)/"run.yaml"` 不在 → `not_found:`，claims = `run_claims(id)`（None → `invalid:`）；`--free` 不在 claims 裡 → `invalid:`（Open point 12）；`--set NAME=VALUE`、`NAME=@PATH`（`sha256_file`，檔不在 → `not_found:`，形狀錯 → `invalid:`）；字彙以外的名稱 → `ValidationFailed(f"unbindable: {names} cannot be bound for --for {consumer}; free them with --free")`；空 → `invalid:`。寫：`ArtifactSpec(kind="authorization", id, dataset=claims.get("dataset"), plan_id=claims.get("plan_id"), params={"consumer": ..., "basis": ..., **({"target_kind": tk} if tk else {})}, inputs=[InputRef(name="basis", path=str(依據的 manifest.json))] if 產物依據 else [], supersedes=spec.supersedes, supersedes_reason=reason if supersedes else None)`；`write_json(FILE, doc)`；writer 的 `exists:` 原樣丟出。
  - `authorization_input(data_root: Path, authorization_id: str) -> InputRef` = `InputRef(name="authorization", path=str(store.manifest_path(...)))`；manifest 不在 → `not_found:`。`load_authorization(data_root, authorization_id) -> AuthorizationDoc`。
  - `consume(data_root: Path, authorization_id: str, *, consumer: Consumer, target_kind: str | None, claims: Mapping[str, str]) -> AuthorizationDoc`：(1) 不在 → `not_found:`；`verify` 失敗或被 supersede → `superseded:`（§5.6 第 1 步的字；訊息說是哪一種）；(2) 依據：產物依據完好、其 manifest sha 等於釘住的 `basis` input、沒被 supersede；run 依據的 run.yaml 在；不成立 → `superseded: the basis ... no longer holds`（Open point 11）；(3) consumer / target_kind 不同 → `unauthorized:`（`differs=consumer` / `differs=target_kind`）；(4) 授權的每個 claim，消費者缺或值不同 → 收集；有就 `ValidationFailed("unauthorized: authorization <id> binds " + "; ".join(f"{n}: authorized {a[:12]}, got {(b or 'none')[:12]}"), fields={"authorization": id, "differs": ",".join(names)})`；消費者多出來的 claim 不管。
  - writer 的 `authorization` input：路徑要 fullmatch `^artifacts/authorization/([A-Za-z0-9][A-Za-z0-9._-]*)/manifest\.json$`，否則 `not_found:`；`consume(data_root, id, consumer="artifact", target_kind=spec.kind, claims=artifact_claims(spec))`。`create` 與 `commit` 各一次。

- [ ] **Step 1: 寫會失敗的測試**

```python
# bench fixture：benchmark/b-50，params {batch: "8", limit: "50"}，dataset "demo"，inputs code_snapshot.cwd（靜態）、ids、source_audit、model
def test_an_artifact_basis_gives_these_claims(roots, bench):
    res = authorize(AuthorizeSpec("a1", "artifact:benchmark/b-50", "artifact", "features", ["param:limit"], [], "50-sample benchmark passed"), data_root=roots.data)
    assert res.claims == {"dataset": "demo", "input:code_snapshot.cwd": S, "input:ids": I, "input:model": M,
                          "input:source_audit": A, "param:batch": "8"} and res.freed == ["param:limit"]
    assert doc["claims_sha256"] == sha256_json(doc["claims"]) and doc["target_kind"] == "features"
    assert m.spec.params == {"consumer": "artifact", "basis": "artifact:benchmark/b-50", "target_kind": "features"}
    assert [(i.name, i.sha256) for i in m.spec.inputs] == [("basis", sha256_file(bench_manifest))]
def test_set_and_at_path_override_a_claim(roots, bench, tmp_path):  # --set param:batch=16；--set input:model=@new.bin
def test_unbindable_unknown_free_and_empty(roots, bench):  # --for train 時 param:batch → "^unbindable:"；--free param:nope → "^invalid:"；全 free → "^invalid:"
def test_a_superseded_or_broken_basis_is_refused(roots, bench):  # supersede b-50 → "^superseded:"；改 b-50 的檔 → "^mismatch:"
def test_a_benchmark_authorizes_the_full_job(roots, bench):  # VCP-006：features/f-all，batch 8、limit all、同樣四個 input + authorization_input → commit
@pytest.mark.parametrize("changed", ["param:batch", "input:code_snapshot.cwd", "input:ids", "input:source_audit", "input:model"])
def test_any_bound_input_that_changes_is_unauthorized(roots, bench, tmp_path, changed):
    assert ei.value.fields == {"authorization": "a1", "differs": changed} and "authorized " in str(ei.value)
    assert not (roots.data / "artifacts" / "features" / "f-all").exists()
def test_a_superseded_authorization_is_refused(roots, bench):  # a2 --supersedes a1 → 用 a1 → "^superseded:"
def test_one_authorization_serves_two_jobs(roots, bench):  # f-1、f-2 都成功，authorization input 的 sha 相同
def test_the_target_kind_must_match(roots, bench):  # kind "tiles" → unauthorized、differs "target_kind"
# test_cli_artifact.py
def test_cli_create_with_an_authorization_input(roots, bench):  # batch 改了 → exit 1、"unauthorized:"、"differs=param:batch"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/artifact/test_authorization.py tests/unit/test_cli_artifact.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.artifact.authorization`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/artifact tests/unit/test_cli_artifact.py tests/unit/test_e2e_artifact.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/artifact/authorization.py src/vcp/artifact/writer.py tests/unit/artifact/test_authorization.py tests/unit/test_cli_artifact.py`；`git commit -m "feat(artifact): authorization artifacts bind a job to what its benchmark proved"`。

### Task 13: `vcp artifact authorize` 與 run 依據的 claims（§4.6 右欄、§5.6 建立）

**Files:**
- Create: `src/vcp/train/authorize.py`
- Modify: `src/vcp/cli_artifact.py`（`authorize` 命令）
- Test: Create `tests/unit/train/test_authorize.py`；Modify `tests/unit/test_cli_artifact.py`

**Interfaces:**
- Consumes: Task 5 的 `read_sections`、`EnvIdentity`；Task 4 的 `snapshot_json_path`；Task 12 的 `authorize`、`AuthorizeSpec`。
- Produces:
  - `train_claims(*, dataset: str, plan_id: str, seed: int | None, samples_hash: str, trained_on: Sequence[str], config_sha256: str, command: Sequence[str], plan_sha256: str, code: Mapping[str, str], evidence: Mapping[str, str], labels: Mapping[str, str], identity: EnvIdentity | None) -> dict[str, str]`：`seed` 不是 None 才有；`param:trained_on = ",".join(sorted(trained_on))`；`param:command_sha256 = sha256_json(list(command))`；`input:plan`；`input:code_snapshot.<label>`（snapshot.json 的 sha）、`input:evidence.<name>`（來源 sha）、`input:labels.<id>`（manifest sha）；identity 有值才有 `env:python` / `env:packages` / `env:gpus`；排序。
  - `run_claims(data_root: Path, configs_root: Path, run_id: str) -> dict[str, str]`：最後一個 `finished` attempt，否則最後一個（沒有 attempt → `invalid:`）；command = `attempt.command or record.command`、seed = `attempt.seed`；`read_sections(run_dir / attempt.env)`；每個 code root 都要有快照，否則 `ValidationFailed("invalid: basis run <id> attempt <n> has no code snapshot for <labels>")`（Open point 8）；code 值 = `sha256_file(snapshot_json_path(...))`；evidence = current 的 `evidence` ref → `source_sha`；labels = current 的 `label_set` ref → `manifest_sha256`；plan sha = `sha256_file(DatasetPaths.resolve(...).plan_json(plan_id))`（Open point 14）。
  - CLI `authorize_cmd(authorization_id --id, basis --basis, consumer --for, target_kind --target-kind | None, free --free（可重複）, set_ --set（可重複）, reason --reason, supersedes --supersedes | None, json_mode, data_root, configs_root)`：`cmd=artifact.authorize`；`run_claims=functools.partial(run_claims, data, configs)`；VERDICT `id= basis= for= claims=<數量> freed=<數量>`；人看的行每個 claim 一行 `claim <name>=<value 前 12 碼>`；context `{"id": ..., "basis": ...}`。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_run_claims_from_the_last_finished_attempt(roots, work):  # attempt 1 finished，attempt 2（--resume --seed 2）failed
    assert set(c) == {"dataset", "plan_id", "seed", "param:samples_hash", "param:trained_on", "param:config_sha256",
                      "param:command_sha256", "input:plan", "input:code_snapshot.cwd", "env:python", "env:packages", "env:gpus"}
    assert c["seed"] == "1" and c["param:config_sha256"] == record.config_hash
    assert c["input:code_snapshot.cwd"] == sha256_file(snapshot_json) and c["env:python"] == env1["identity"]["python"]
def test_train_claims_agree_with_run_claims(roots, work):
def test_evidence_and_labels_become_claims(roots, work, tmp_path):  # --evidence cfg=f.yaml、--labels pseudo-v1
    assert c["input:evidence.cfg"] == sha256_file(f) and c["input:labels.pseudo-v1"] == label_manifest_sha
def test_a_basis_run_missing_a_snapshot_is_refused(roots, work, monkeypatch):  # coderun.write_snapshot 回 locked → "^invalid:"
# test_cli_artifact.py
def test_artifact_authorize_cli(roots, bench):
    assert r.exit_code == 0 and all(f in v for f in ["id=a1", "basis=artifact:benchmark/b-50", "for=artifact", "claims=6", "freed=1"])
    # --for artifact 沒有 --target-kind → exit 1、"invalid:"、"id=a1"；unbindable → exit 1、"unbindable:"；--basis run:R --for train → exit 0、"for=train"
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_authorize.py tests/unit/test_cli_artifact.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.train.authorize`、`No such command 'authorize'`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — 同一指令加 `tests/unit/artifact`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/authorize.py src/vcp/cli_artifact.py tests/unit/train/test_authorize.py tests/unit/test_cli_artifact.py`；`git commit -m "feat(artifact): vcp artifact authorize from an artifact or a training run"`。

### Task 14: `train run --authorization`（§5.1 第 6、8 步、C3、§16 第 5、6 項）

**Files:**
- Modify: `src/vcp/train/run.py`（`RunSpec.authorization`；`_prepare` 在掃描之後、磁碟檢查之前 `consume`；`write_all(strict=spec.authorization is not None)`；ref；`RunResult.authorization`）
- Modify: `src/vcp/train/coderun.py`（`claims_now`、`authorization_ref`、`write_all` 的 strict 一支）
- Modify: `src/vcp/cli_train.py`（`--authorization`、`authorization=`）
- Test: Create `tests/unit/train/test_run_authorization.py`

**Interfaces:**
- Consumes: Task 12 的 `consume`；Task 13 的 `train_claims`；Task 2 的 `snapshot_json_bytes`。
- Produces:
  - `claims_now(*, dataset: str, plan_id: str, seed: int | None, samples_hash: str, trained_on: list[str], chash: str, command: list[str], plan_sha256: str, evidence: list[tuple[str, Path]], labels: list[str], plan: CodePlan, report: ProbeReport, data_root: Path) -> dict[str, str]`：`train_claims`，code = 沒有 reason 的 scan 各自 `sha256(snapshot_json_bytes(scan.entries))`（位元組跟寫出來的 snapshot.json 相同，所以寫入前就算得出）；evidence = 每個 `--evidence` 檔的 `sha256_file`；labels = 每個 label set 的 manifest sha。原始型別參數，避免 `coderun` import `run`。
  - `_prepare`：`spec.authorization` 有值時 `consume(..., consumer="train", target_kind=None, claims=claims_now(...))`，失敗就是 FAIL、一個檔都沒寫；`_Prepared.plan_sha256`。
  - `write_all(strict=True)`：`drift` → `IntegrityError(f"drift: {label}: a file changed after the authorization was checked", fields={"code_root": label})`；`locked` / `partial` / `unwritable` → `VcpError(f"code_unavailable: the snapshot of {label} could not be written ({cause}); <每種 cause 的處置>", fields={"code_root": label, "cause": cause})`。在 `_run_attempt` 之前，run 的檔都還沒寫。
  - `authorization_ref(data_root: Path, authorization_id: str, attempt: int) -> EvidenceRef`（name / role / kind 都是 `authorization`、binding `cli`），跟 code refs 同一處 `bind_ref`。
  - `RunSpec.authorization: str | None = None`、`RunResult.authorization: str | None = None`；VERDICT `authorization=<id>`（有給時）；失敗帶 `consume` 的 `authorization=` `differs=`。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_a_benchmark_run_authorizes_the_full_run(roots, tmp_path, isolated_git):  # C1 的理由本身
    # git cwd；R1 的 child 寫 args.yaml hparams.yaml config.json metrics.json（未追蹤）
    # authorize a1 --basis run:R1 --for train --free param:command_sha256；R2 同 cwd、命令多 "--epochs" "50"、--authorization a1
    assert r2.attempt.status == "finished" and r2.authorization == "a1"
    assert [r.name for r in load_record(roots.data, "R2").evidence if r.kind == "authorization"] == ["authorization"]
@pytest.mark.parametrize("change", ["seed", "code", "evidence", "config"])
def test_any_bound_train_claim_that_changes_is_unauthorized(roots, tmp_path, isolated_git, change):
    want = {"seed": "seed", "code": "input:code_snapshot.cwd", "evidence": "input:evidence.cfg", "config": "param:config_sha256"}
    assert ei.value.fields["differs"] == want[change] and not run_dir(roots.data, "R2").exists()
def test_a_superseded_authorization_stops_the_run(...):  # "^superseded:"、沒有 run 目錄
def test_a_too_large_root_the_authorization_binds_is_unauthorized(...):  # rules.MAX_FILES 讓 cwd too_large → differs "input:code_snapshot.cwd"、沒有檔
@pytest.mark.parametrize(("reason", "word", "status"), [("drift", "drift:", "FAIL"), ("locked", "code_unavailable:", "ABORT"),
    ("partial", "code_unavailable:", "ABORT"), ("unwritable", "code_unavailable:", "ABORT")])
def test_with_an_authorization_a_snapshot_that_cannot_be_written_fails_closed(roots, tmp_path, monkeypatch, reason, word, status):  # C3
    assert ei.value.status == status and str(ei.value).startswith(word) and ei.value.fields["code_root"] == "cwd"
    assert reason == "drift" or ei.value.fields["cause"] == reason
    assert not run_dir(roots.data, "R2").exists()
def test_an_authorization_holds_for_one_call(...):  # R2 --resume 不給 --authorization → authorization ref 列數不變、res.authorization is None
def test_train_run_cli_authorization_fields(...):  # "authorization=a1"；不符 → exit 1、"unauthorized:"、"authorization=a1"、"differs=seed"；code_unavailable → exit 2
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/train/test_run_authorization.py -q`；Expected: FAIL（`extra_forbidden: authorization`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/train tests/unit/test_cli_train.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/train/run.py src/vcp/train/coderun.py src/vcp/cli_train.py tests/unit/train/test_run_authorization.py`；`git commit -m "feat(train): train run --authorization checks the run against its benchmark before any write"`。

### Task 15: `vcp code` 命令群（§5.7、C4、稽核 §12 第 10 條）

**Files:**
- Create: `src/vcp/code/bundle.py`、`src/vcp/code/diff.py`、`src/vcp/cli_code.py`
- Modify: `src/vcp/code/process.py`（`Written`、`snapshot_roots`）、`src/vcp/cli.py:11-63`（import 並 `app.add_typer(code_app, name="code")`）
- Test: Create `tests/unit/code/test_code_bundle.py`、`tests/unit/test_cli_code.py`

**Interfaces:**
- Consumes: Task 11 的 `strict`；Task 4 的 `check_snapshot`、`load_index`；Task 2 的 `scan_root`、`snapshot_json_bytes`。
- Produces:
  - `process`：`@dataclass(frozen=True) Written(root: PlannedRoot, scan: RootScan, outcome: SnapshotOutcome)`；`snapshot_roots(data_root: Path, configs_root: Path | None, roots: Sequence[Path]) -> list[Written]`：每個 `--root` 照給的（`root_kind`，label `root-<n>`），不是目錄 → `not_found:`；重複去掉、巢狀分最內層、跳過 data / configs root；`keep="all"`；`strict`（Open point 13 的 `unreadable`）。
  - `bundle.py`：`VCP_CODE_DIR = ".vcp-code"`；`@dataclass(frozen=True) ExportResult(files: int, exported: int, not_kept: int)`；`export(data_root: Path, snapshot_id: str, out: Path) -> ExportResult`：`check_snapshot` 不 ok → `mismatch:`；`out` 在 data root 裡 → `invalid:`；`out` 存在且非空 → `exists:`；保留的檔寫到 `out/<path>`，`snapshot.json` 與 `index.jsonl` 寫到 `out/.vcp-code/`。`@dataclass(frozen=True) VerifyDirResult(files: int, kept: int, mismatch: list[str], missing: list[str], not_kept: list[str], extra: list[str])`；`verify_dir(directory: Path) -> VerifyDirResult`：不呼叫 git；index 未排序或重複 → mismatch；`snapshot_json_bytes(index)` 不等於 `.vcp-code/snapshot.json` → mismatch `.vcp-code/snapshot.json`；在場的檔 sha 不同 → mismatch；`kept: true` 不在 → missing；`kept: false` 不在 → not_kept；extra = 目錄裡（跳過規則的段、`.vcp-code/`、機密檔名）`file_class` 不是 None、index 沒有的檔。
  - `diff.py`：`@dataclass(frozen=True) DiffResult(changed: list[str], added: list[str], removed: list[str])`；`diff_root(data_root: Path, snapshot_id: str, root: Path, *, kind: RootKind | None = None, skip_dirs: Sequence[Path] = ()) -> DiffResult`（同一套規則掃現在的檔；`too_large` → `too_large:`）；`diff_run(data_root: Path, run_id: str, attempt: int | None) -> list[tuple[str, DiffResult]]`（預設最後一個 attempt；有快照的 roots；用記錄的 `kind`、`excluded_dirs` 與同 attempt 其他 root 當 skip）。
  - `cli_code.py`：`code_app = typer.Typer(no_args_is_help=True, help="code snapshots: write, verify, compare, export")`：
    - `snapshot_cmd(root --root 必填可重複, json_mode, data_root, configs_root)`：`cmd=code.snapshot`；VERDICT `roots= ids= files= kept= not_kept= reused=`（ids 逗號串接，其餘加總）；`not_kept > 0` → WARN；人看的行 `<label> <id> <path> <created|reused> files=<n> kept=<k>`；context `root=`（給的路徑逗號串接）。
    - `verify_cmd(snapshot_id --id | None, directory --dir | None, …)`：恰好一個，否則 `invalid:`；`cmd=code.verify`；`id=` 或 `dir=`、`files=`、`kept=`；不符 → FAIL `mismatch:`，欄位 `mismatch=<n> missing=<n>`；`--dir` 時一律帶 `not_kept=` `extra=`，> 0 WARN。
    - `diff_cmd(snapshot_id --id, root --root, run --run, attempt --attempt, …)`：（`--id` 加 `--root`）與 `--run` 恰好一組，否則 `invalid:`；`changed= added= removed=`，任一 > 0 WARN。
    - `export_cmd(snapshot_id --id, out --out, …)`：`files= exported= not_kept=`，`not_kept > 0` WARN。

- [ ] **Step 1: 寫會失敗的測試**

```python
# test_cli_code.py
def test_code_snapshot_keeps_everything_then_reuses(roots, tmp_path):  # 1.5 MiB 的 big.py
    assert "kept=2" in v and "not_kept=0" in v and "reused=0" in v  # 再跑一次 → "reused=1"、"status=OK"
def test_code_snapshot_reusing_a_capped_copy_warns_not_kept(roots, tmp_path):  # 先 write_snapshot(capped scan) → "status=WARN"、"not_kept=1"
def test_code_snapshot_has_no_fallback(roots, tmp_path, monkeypatch):  # MAX_FILES=0 → exit 1 "too_large:"；process.write_snapshot 回 locked → exit 2 "code_unavailable:" "cause=locked"
def test_verify_ignores_the_worktree_and_diff_sees_it(roots, tmp_path):  # 稽核 §12 第 10 條：改 a.py、加 c.py、刪 b.py
    # verify --id → exit 0、"status=OK"；diff --id --root → "status=WARN"、"changed=1"、"added=1"、"removed=1"
def test_verify_catches_a_tampered_snapshot(roots, tmp_path):  # 改 files/a.py → exit 1、"mismatch:"
def test_export_refuses_the_data_root_and_a_non_empty_directory(roots, tmp_path):  # "invalid:"；"exists:"
def test_export_then_verify_dir_round_trip(roots, tmp_path):  # C4：capped 快照含 >1 MiB 的 big.py
    # export → "status=WARN" "not_kept=1"；verify --dir → exit 0 "status=WARN" "not_kept=1" "extra=0"
    # 放回 big.py → "status=OK"；改在場的 a.py → exit 1 "mismatch:"；改回、刪 kept 的 b.py → exit 1 "mismatch:" "missing=1"；加 extra.py → WARN "extra=1"
def test_verify_dir_needs_no_git(roots, tmp_path, monkeypatch):  # export 後 PATH="" → verify --dir exit 0
def test_diff_run_compares_the_attempts_roots(roots, work):  # train run 之後改 helper.py → diff --run r → "status=WARN" "changed=1"
def test_code_commands_json_and_context(roots, tmp_path):  # --json：stdout 一份 JSON（result["roots"][0]["id"]）、VERDICT 在 stderr；不存在的 --id → exit 1 "not_found:" "id=code-…"
```

`test_code_bundle.py` 以函式層級鎖 `export` 與 `verify_dir` 的每一支（同上的情境，斷言 `VerifyDirResult` 的四個清單）。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/code/test_code_bundle.py tests/unit/test_cli_code.py -q`；Expected: FAIL（`ModuleNotFoundError: vcp.cli_code`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/code tests/unit/test_cli_code.py tests/unit/test_cli.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/bundle.py src/vcp/code/diff.py src/vcp/code/process.py src/vcp/cli_code.py src/vcp/cli.py tests/unit/code/test_code_bundle.py tests/unit/test_cli_code.py`；`git commit -m "feat(code): vcp code snapshot, verify, diff and export"`。

### Task 16: `eval ingest --code` 與 `eval status` 的 code（§5.8 第二、三點）

**Files:**
- Modify: `src/vcp/code/snapshot.py`（`code_ref`）、`src/vcp/measure/ingest.py:41-260`（`IngestSpec.code`、`parse_code_args`、`IngestResult.code`）、`src/vcp/measure/report.py:36-145`（`StatusResult.code`）、`src/vcp/cli_eval.py`（ingest `--code`、`code=`；status 人看的行）
- Test: Create `tests/unit/measure/test_ingest_code.py`；Modify `tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 10 的 `code_info`；Task 4 的 `check_snapshot`。
- Produces:
  - `code_ref(data_root: Path, label: str, snapshot_id: str, *, attempt: int | None, binding: Binding) -> EvidenceRef`：快照不在 → `not_found:`、壞 → `mismatch:`；name `code.<label>`、role `code`、kind `code_snapshot`。
  - `parse_code_args(items: list[str]) -> list[tuple[str, str]]`：`LABEL=ID`，label `validate_name`、形狀錯或重複 → `invalid:`。所有檢查在任何寫入之前（跟既有的 evidence 檢查同一段）；掛上 binding `manual`、attempt None。
  - `IngestSpec.code: list[str] = []`；`IngestResult.code: CodeGrade = "declared"`（存檔後 `code_info`）；VERDICT `code=`。
  - `StatusResult.code: dict[str, str]`；status 人看的行多 ` code=<grade>`；`--json` 帶出；VERDICT 欄位不變。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_ingest_code_attaches_a_snapshot(roots, ...):  # vcp code snapshot 的 id；--code cwd=<id>
    assert ref.name == "code.cwd" and ref.binding == "manual" and ref.attempt is None and "code=snapshot" in v
def test_ingest_code_refuses_a_missing_or_broken_snapshot(...):  # exit 1 "not_found:"；exit 1 "mismatch:"；run.yaml 不存在或位元組不變
def test_ingest_code_label_must_be_a_name(...):  # --code "bad/label=x" → exit 1 "invalid"
def test_eval_status_lists_each_runs_code(...):  # 人看的行有 "code=snapshot" 與 "code=declared"；--json result["status"]["code"]；VERDICT 欄位名清單跟改之前一樣
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/measure/test_ingest_code.py tests/unit/test_cli_eval.py -q`；Expected: FAIL（`No such option: --code`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/measure tests/unit/test_cli_eval.py tests/unit/test_e2e_eval.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/code/snapshot.py src/vcp/measure/ingest.py src/vcp/measure/report.py src/vcp/cli_eval.py tests/unit/measure/test_ingest_code.py tests/unit/test_cli_eval.py`；`git commit -m "feat(eval): eval ingest --code attaches a snapshot and eval status shows each run's code"`。

### Task 17: 提交的程式碼要求（§4.7、§5.8 stage / final、§8）

**Files:**
- Modify: `src/vcp/submit/schema.py:111-192`（`PlatformProfile.require_code`、serializer）、`:213-255`（`Artifact.code_snapshot`、serializer）、`:263-277`（`Staged.code`、serializer）
- Modify: `src/vcp/submit/stage.py`（`StageSpec.code`；等級與檢查）、`src/vcp/submit/kernel.py`（weights run 的等級）、`src/vcp/submit/final.py:215-283`、`src/vcp/cli_submit.py`（stage `--code`、`code=`）
- Test: Modify `tests/unit/test_compat_v0170.py`（golden 先）；Create `tests/unit/submit/test_stage_code.py`；Modify `tests/unit/submit/test_final.py`

**Interfaces:**
- Consumes: Task 5 的 `CodeGrade`、`CODE_RANK`；Task 10 的 `code_info`、`lowest`；Task 4 的 `check_snapshot`。
- Produces:
  - `PlatformProfile.require_code: CodeGrade = "declared"`，等於預設時 serializer 拿掉（比照 `selection_warn_hours`）。
  - `Artifact.code_snapshot: str | None = None`（只給 kernel；file 帶它 → `_shape` 的 ValueError）；`Staged.code: CodeGrade | None = None`；兩者 None 時 serializer 拿掉。
  - `StageSpec.code: str | None = None`；`stage()` 與既有的 `kernel_options` 檢查同一處：file profile 給了 `--code` → `invalid:`。
  - `_stage_locked`（provenance 檢查之後、render 之前）：kernel `--code` → `check_snapshot` ok，否則 `not_found:` / `mismatch:`；runs = eval run、test run（file）、weights runs（kernel）；`grade = lowest(code_info(...).grade for runs)`；每個低於 `require_code` 的 run：candidate → `ValidationFailed(f"code_required: run {r!r} is {g}, profile requires {req}", fields={"run": r, "code": g})`；baseline / probe → pairing checks 加 `code:<run>=<grade>`（Open point 10）；kernel candidate、`require_code` 是 `snapshot` 以上、沒給 `--code` → `code_required: a kernel candidate needs --code <snapshot id> when the profile requires <req>`（`fields={"code": "none"}`）。
  - `Staged.code = grade`、`Artifact.code_snapshot = spec.code`；CLI stage VERDICT `code=`（接在 `provenance` 之後）。
  - `final`：`_run_code(paths, row, staged) -> CodeGrade`（同 stage 的 runs）；`_final_entries` 在 `provenance_required` 之後 `elif CODE_RANK[code] < CODE_RANK[profile.require_code]` 或（kernel、`require_code` ≥ snapshot、`code_snapshot` 不在或 `check_snapshot` 不 ok）→ `why = "code_required"`；`FinalEntry` 形狀不變。

- [ ] **Step 1: 先寫 golden（改 schema 之前就綠）**

```python
# test_compat_v0170.py 追加
SUBMIT_YAML_0170 = """..."""        # init_profile 寫出的（ledger configs、selection_warn_hours 預設）
STAGE_JSON_0170_FILE = """..."""    # 0.17 的 file stage.json
STAGE_JSON_0170_KERNEL = """..."""  # 0.17 的 kernel stage.json
def test_a_0170_submit_yaml_keeps_its_bytes(tmp_path):  # load → dump_yaml_model → 相等
def test_a_0170_stage_json_keeps_its_bytes():  # json.dumps(Staged.model_validate_json(t).model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n" == t（兩份）
```

Run: `uv run pytest tests/unit/test_compat_v0170.py -q` → PASS（改程式之前）。

- [ ] **Step 2: 寫會失敗的測試**

```python
# test_stage_code.py（submit_fixtures.make_pair、seed_eval_runs、seed_test_runs）
def test_require_code_default_is_not_written(pair):  # init → 沒有 "require_code"；require_code="snapshot" → 有 "require_code: snapshot"
def test_a_candidate_below_require_code_fails(pair):
    assert ei.value.fields == {"run": "good", "code": "declared"} and str(ei.value).startswith("code_required:")
    # 沒有 submit/<test>/S1、台帳沒有 staged 列
def test_a_probe_below_require_code_is_noted(pair):  # pairing.checks 有 "code:good=declared" 與 "code:good.test=declared"
def test_stage_records_the_lowest_grade(pair):  # good 用 ingest --code 變 snapshot、good.test declared → staged.code "declared"、CLI "code=declared"
def test_kernel_code_is_recorded_and_a_file_profile_refuses_it(pair):
    # kernel --code <id> → stage.json["artifact"]["code_snapshot"] == id；file + --code → "^invalid:"；require_code snapshot 的 kernel candidate 沒 --code → "^code_required:"
# test_final.py
def test_final_marks_code_required(...):  # stage 時 declared；submit.yaml 改寫 require_code: snapshot（newline="\n"）→ 那列 why "code_required"、FinalEntry 的鍵不變
```

- [ ] **Step 3: 跑紅** — `uv run pytest tests/unit/submit tests/unit/test_compat_v0170.py -q`；Expected: FAIL（`extra_forbidden: require_code`、沒有 `code_required:`）。
- [ ] **Step 4: 實作上列 Interfaces。**
- [ ] **Step 5: 跑綠** — `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_compat_v0170.py -q`；Expected: PASS（golden 照綠）。
- [ ] **Step 6: Commit** — `git add src/vcp/submit/schema.py src/vcp/submit/stage.py src/vcp/submit/kernel.py src/vcp/submit/final.py src/vcp/cli_submit.py tests/unit/test_compat_v0170.py tests/unit/submit/test_stage_code.py tests/unit/submit/test_final.py`；`git commit -m "feat(submit): require_code gates stage and final on the code grade"`。

### Task 18: 備份與 gc（§4.8 前兩點）

**Files:**
- Modify: `src/vcp/backup/schema.py:18-85`、`src/vcp/backup/evidence.py:196-337`
- Test: Create `tests/unit/backup/test_evidence_code.py`、`tests/unit/test_gc_code.py`（不建 `tests/unit/gc/`）

**Interfaces:**
- Consumes: Task 12 的 `load_authorization`、`parse_basis`；Task 17 的 `Artifact.code_snapshot`。
- Produces:
  - `ROLES` 在 `"evidence"` 之後插入 `"code_snapshot", "code_trace", "authorization", "authorization_basis"`；`_TIER2` 加 `code_snapshot`、`authorization_basis`（code_trace、authorization 是 tier 1）；`CARD_ROLES` 加 `authorization`、`code_trace`。
  - `_artifact` 拆出 `_artifact_files(kind: str, artifact_id: str, role: str, conclusion: str, pinned_sha: str | None = None) -> None`（現在的行為原樣）；`_artifact(ref, conclusion)` 在 `ref.kind == "authorization"` 時再 `_authorization_basis(ref, conclusion)`：讀不動就 return（上面已列成 missing）；產物依據 → `_artifact_files(kind, id, "authorization_basis", conclusion, pinned_sha=<basis input 的 sha>)`；run 依據 → `self.walk_run(run_id, conclusion)`。
  - `walk_submission`：`staged.artifact.code_snapshot` 有值 → `_artifact_files("code_snapshot", id, "code_snapshot", conclusion)`。
  - `completeness.py` 與 `vcp.gc` 不改。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_new_roles_and_tiers():
    assert [TIER_OF[r] for r in ("code_snapshot", "code_trace", "authorization", "authorization_basis")] == [2, 1, 1, 2]
    assert {"authorization", "code_trace"} <= CARD_ROLES
def test_walk_run_collects_code_records(roots, ...):  # trace_on、產物依據的授權 → build_manifest run:<id> 的 roles 有四種；依據的 manifest.json 是 authorization_basis
def test_a_run_basis_is_walked_as_a_run(...):  # 依據 run:R1 → 清單有 R1 的 run.yaml（run_card）
def test_manifest_gaps_know_the_new_refs(...):  # 新清單 → manifest_gaps == []；拿掉 code_snapshot 的 manifest 列重寫（write_old_manifest）→ 名稱有 "evidence.code_snapshot/<id>"
def test_backup_verify_with_code_records(...):  # push 到本機目的地 → verify exit 0（含 CARD 時戳層）
def test_walk_submission_collects_the_kernel_snapshot(...):
# test_gc_code.py
def test_gc_names_who_references_code_records(...):  # collect_references：快照每個檔、trace、授權與依據都在，referrer 有 "run:<id>#code_snapshot" 等
def test_a_snapshot_no_run_names_is_not_referenced(...):  # vcp code snapshot 寫的 → scan 的類別是 "record"
```

既有 `tests/unit/backup/test_evidence_recorder.py::test_backup_manifests_keep_their_files` 不改、照綠（沒有新 ref 的清單不變）。

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/backup/test_evidence_code.py tests/unit/test_gc_code.py -q`；Expected: FAIL（`unknown role 'code_snapshot'`）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/backup tests/unit/test_gc_*.py tests/unit/test_cli_gc.py tests/unit/test_e2e_backup.py -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/backup/schema.py src/vcp/backup/evidence.py tests/unit/backup/test_evidence_code.py tests/unit/test_gc_code.py`；`git commit -m "feat(backup): backup and gc follow code snapshots, traces and authorizations"`。

### Task 19: provenance 圖（§4.8 第三點）

**Files:**
- Modify: `src/vcp/provenance/graph.py:289-401`（`_run_entity(..., code_grade)`、`_scan_runs`）、`:707-722`（`_link_artifact_runs` 跳過 `code_trace`）、新 `_link_authorization_runs`、`_scan_submissions`（kernel 快照）、`build_graph`（在 `_scan_runs` 之後呼叫新函式）
- Test: Create `tests/unit/provenance/test_graph_code.py`

**Interfaces:**
- Consumes: Task 10 的 `code_info`；Task 17 的 `Artifact.code_snapshot`。
- Produces:
  - run entity 屬性 `"code_grade": <grade>`（fused 也有）。渲染不改。
  - `_link_authorization_runs(graph: ProvenanceGraph) -> None`：kind `authorization`、`params.basis` 是 `run:<id>` 且 run entity 在 → `add_edge(run, auth, "PRODUCED_BY", {"input": "basis"})`。產物依據走既有的 `_link_artifact_inputs`（input 名 `basis`）。
  - `_scan_submissions`：`staged.artifact.code_snapshot` 的 entity 在 → `add_edge(artifact:code_snapshot/<id>, submission, "CONSUMED_BY")`。
  - 既有 index 的 `sync` 會把多了 `code_grade` 的 run 當成改寫而拒絕：文件要求每個 checkout `vcp provenance rebuild` 一次（Task 21），程式不改。

- [ ] **Step 1: 寫會失敗的測試**

```python
def test_run_entities_carry_code_grade(roots, ...):  # traced 的 train run → "traced"；ingest run → "declared"
def test_code_artifacts_link_through_runs_and_inputs(...):
    # snapshot → run CONSUMED_BY；trace → run 恰一條 CONSUMED_BY；snapshot → features PRODUCED_BY {"input": "code_snapshot.cwd"}；
    # authorization → features PRODUCED_BY {"input": "authorization"}；benchmark → authorization PRODUCED_BY {"input": "basis"}；
    # R1 → run 依據的授權 PRODUCED_BY {"input": "basis"}；kernel 快照 → submission CONSUMED_BY
def test_rebuild_twice_gives_one_graph_hash(...):  # graph_hash(build_graph(d, c)) 兩次相同
```

- [ ] **Step 2: 跑紅** — `uv run pytest tests/unit/provenance/test_graph_code.py -q`；Expected: FAIL（沒有 `code_grade`、trace 有兩條邊）。
- [ ] **Step 3: 實作上列 Interfaces。**
- [ ] **Step 4: 跑綠** — `uv run pytest tests/unit/provenance -q`；Expected: PASS。
- [ ] **Step 5: Commit** — `git add src/vcp/provenance/graph.py tests/unit/provenance/test_graph_code.py`；`git commit -m "feat(provenance): code grade on runs and edges for code snapshots and authorizations"`。

### Task 20: 回歸門檻、端到端與 quickstart（§10.7；§13 第 4 條）

**Files:**
- Create: `tests/unit/test_e2e_code.py`、`tests/integration/test_code_grade_realdata.py`
- Modify: `tests/unit/test_regression_gate.py`（`GATE` 最後加一列）、`examples/quickstart.py`

**Interfaces:**
- Consumes: Task 1–19 的測試名與 CLI。
- Produces: `quickstart.run(step, *args, env_root, configs_root, tail: Sequence[str] = ()) -> str`（`tail` 接在 `--data-root` / `--configs-root` 之後，好讓 `-- <命令>` 留在最後；回傳 stdout）。

- [ ] **Step 1: 端到端（Task 1–19 都在時才綠，本步確認它跑得過）**

```python
# test_e2e_code.py
def test_benchmark_authorize_full_job_then_stage(pair, tmp_path, monkeypatch):
    # 產物：benchmark（snapshot_inputs）→ CliRunner "artifact authorize" → 正式 features job commit 成功
    # 訓練：train run R1 → authorize --basis run:R1 --for train → train run R2 --authorization → R2 與 R2.test 的預測 ingest（test run 帶 --code）
    # submit.yaml 寫 require_code: snapshot → submit stage --eval-run R2 --test-run R2.test → exit 0、"code=snapshot"
```

Run: `uv run pytest tests/unit/test_e2e_code.py -q` → PASS。

- [ ] **Step 2: 加 gate 列** — 標籤 `"code snapshots, traces and authorizations (0.18.0)"`；說明 `"code: train run snapshots every code root before its first write and the snapshot reuses one content-addressed artifact; an imported file changed during an artifact job is drift while an unimported one is not; exported snapshots verify without the repo and verify ignores the worktree while diff reports it; any bound claim that changes is unauthorized and a train run with an authorization fails closed when its snapshot cannot be written; secrets are never opened; framework outputs and generated modules do not change identity or completeness; a failing trace never changes the child's exit code"`（照既有列拆成多段字串）；證明：
  - `tests/unit/artifact/test_writer_code.py`：`test_an_imported_file_changed_during_the_job_fails_commit`、`test_a_file_nobody_imported_may_change`
  - `tests/unit/test_cli_code.py`：`test_verify_ignores_the_worktree_and_diff_sees_it`、`test_export_then_verify_dir_round_trip`、`test_verify_dir_needs_no_git`
  - `tests/unit/train/test_run.py`：`test_require_clean_fails_before_the_first_write`；`tests/unit/train/test_run_code.py`：`test_an_untracked_helper_is_kept_and_warned`、`test_framework_outputs_do_not_change_the_identity`、`test_a_failing_trace_keeps_the_exit_code`、`test_an_unignored_secret_is_not_read`
  - `tests/unit/artifact/test_authorization.py`：`test_any_bound_input_that_changes_is_unauthorized`；`tests/unit/train/test_run_authorization.py`：`test_any_bound_train_claim_that_changes_is_unauthorized`、`test_a_benchmark_run_authorizes_the_full_run`、`test_with_an_authorization_a_snapshot_that_cannot_be_written_fails_closed`
  - `tests/unit/code/test_code_scan.py`：`test_secret_files_are_never_opened`；`tests/unit/code/test_code_trace.py`：`test_generated_modules_are_not_gaps`
  - `tests/unit/test_e2e_code.py`：`test_benchmark_authorize_full_job_then_stage`

Run: `uv run pytest tests/unit/test_regression_gate.py -q` → PASS。Commit：`git add tests/unit/test_e2e_code.py tests/unit/test_regression_gate.py`；`git commit -m "test(gate): guard the 0.18.0 code snapshot, trace and authorization behaviour"`。

- [ ] **Step 3: quickstart 與選配的真資料測試**

`examples/quickstart.py`：`run` 加 `tail` 並回傳 stdout；第 12 步之後加：
- `13. train run: the code that ran is snapshotted before the first write` — 在 `root / "project"` 寫一個 `train.py`（印一行、寫 `weights/last.pt`），`run(..., "train", "run", "--run", "trained", "--dataset", "demo", "--plan", "fixed-v1", "--trained-on", "train", "--seed", "0", "--cwd", str(project), "--checkpoints", "weights/*.pt", tail=("--", sys.executable, "train.py"))`；VERDICT 行沒有 `code=snapshot` → `SystemExit("quickstart step failed: expected code=snapshot")`。
- `14. code verify: the snapshot verifies without the project` — `train status --run trained --json` 的 `result["evidence"]` 取 `name == "code.cwd"` 的 `artifact_id`，`run(..., "code", "verify", "--id", sid)`。
- 結尾的說明多一句：程式碼快照讓「跑的是哪些程式碼」可以驗。

`tests/integration/test_code_grade_realdata.py`：沿用 `tests/integration_helpers.py` 的 skip（`VCP_REALDATA_ROOT` 缺席就 skip）；對 `runs/*/run.yaml` 的每個 run 呼叫 `code_info(root, run_id)`，不得有例外，`grade in CODE_RANK`。唯讀。

Run: `uv run python examples/quickstart.py` → exit 0、輸出有 `code=snapshot`；`uv run pytest tests/integration/test_code_grade_realdata.py -q` → skipped（或 PASS）。Commit：`git add examples/quickstart.py tests/integration/test_code_grade_realdata.py`；`git commit -m "docs(examples): quickstart snapshots a training run and verifies its code"`。

### Task 21: 文件與 skills（§11）

**Files:**
- Modify: `CLAUDE.md`（再整份複製成 `AGENTS.md`）、`docs/reference/cli.md`、`README.md`、`README.zh-TW.md`
- Modify: `.claude/skills/vcp-train-submit-backup/{SKILL.md,reference.md}`、`.claude/skills/vcp-running-contests/{workflow.md,operator-guide.md}`、`.claude/skills/vcp-orientation/{SKILL.md,map.md}`、`.claude/skills/vcp-provenance-graph/{SKILL.md,reference.md}`、`.claude/skills/vcp-release-and-environments/SKILL.md`，鏡射到 `.agents/skills/`
- Modify: `docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`、`2026-09-11-vcp-immutable-artifacts-design.md`、`2026-09-05-vcp-submission-governance-design.md`、`2026-09-06-vcp-backup-audit-design.md`、`2026-09-27-vcp-dirty-tree-design.md`（各加一條指回本 spec）
- Modify: `docs/audits/2026-09-11-vcp-improvement-audit.md`（VCP-004、VCP-006 狀態行與第 1 節表格的 P0 列；VCP-007 狀態行的「等 Wave 1c」）、`docs/superpowers/plans/2026-09-27-vcp-dirty-tree-followups.md`（§3 第 3 條標為已處理）
- Modify: `docs/handover/HANDOVER.md`、`docs/handover/CODEX_PROMPT.md`
- Create: `docs/superpowers/plans/2026-11-03-vcp-wave1c-followups.md`

**Interfaces:**
- Consumes: Task 1–20 的命令、欄位、字彙與測試名。

- [ ] **Step 1: 改文件（內容照 spec §11）**

- `CLAUDE.md`「路徑」：`artifacts/code_snapshot/code-<16 hex>/`（`snapshot.json` + `index.jsonl` + `files/`；內容定址、同內容重用、不做 supersedes，壞了搬走目錄再快照）、`artifacts/code_trace/<run>-a<n>-t<seq>/trace.json`、`artifacts/authorization/<id>/authorization.json`（撤銷 = supersede）、`locks/code_snapshot-<id>.lock`（60 秒）；`env.<n>.json` 多 `code` / `distributions` / `identity`；`evidence` 多三種 kind、名稱 `authorization` 與 `code.` / `code_trace.` 開頭的保留（`role_reserved:`），0.17 以前讀到會 FAIL，整個 data root 與每個訓練 venv 先升 0.18（`vcp_mismatch:`）；`ArtifactWriter` 的保留輸入名 `code_snapshot.<label>` 與 `authorization`；**未追蹤但未被忽略的 `CODE` 檔會被讀、會保留，未追蹤的設定與資料、被忽略的檔、機密檔名、`.` 開頭的路徑不讀**；寫死在程式碼裡的機密會跟著進 data root 與備份；`submit.yaml` 的 `require_code`；provenance 索引要每個 checkout `rebuild` 一次。「常用命令」加 `uv run vcp code snapshot --root DIR… | verify --id ID|--dir DIR | diff --id ID --root DIR|--run R | export --id ID --out DIR`、`uv run vcp artifact authorize --id A --basis artifact:K/I|run:R --for artifact|train [--target-kind K] [--free N]… [--set N=V|N=@PATH]… --reason "…"`、`train run` 那行加 `[--code-root DIR]… [--authorization ID]`、`eval ingest` 加 `--code LABEL=ID`、`submit stage` 加 `--code ID`。然後 `cp CLAUDE.md AGENTS.md`。
- `docs/reference/cli.md`：新節「程式碼快照 `vcp code`」（四個命令、VERDICT、§4.2 規則摘要、保留上限、`verify` 與 `diff` 的分工、`export` 的來回）與「授權 `vcp artifact authorize`」（claims 字彙表 §4.6、`--free` / `--set`、消耗規則、撤銷）；`train run`（新選項、新欄位、新字彙與它們的 exit 類別）、`train status`（`code=`）、`eval ingest` / `status`、`submit stage` / `final`（`require_code`、`--code`、`why=code_required`）、`artifact create`（保留輸入的 FAIL）。
- `README.md`、`README.zh-TW.md` 的功能清單各一行：程式碼快照與授權（跑的是哪些程式碼、benchmark 綁正式 job）。
- skills：`vcp-train-submit-backup`（等級表、`code_snapshot=` / `code_trace=` / `untracked_code=` / `code_changed=` 的 WARN 與處置、`code_unavailable:` 三種 cause 的處置、`require_code`、kernel `--code`、`VCP_CODE_TRACE=0`）；`vcp-running-contests`（正式訓練前 benchmark → `vcp artifact authorize` → `train run --authorization`，`.gitignore` 掉 wandb 之類的輸出目錄）；`vcp-orientation`（地圖加 `vcp.code`、版本沿革加 0.18.0）；`vcp-provenance-graph`（三種新 kind、`code_grade`、新邊）；`vcp-release-and-environments`（訓練 venv 的 vcp 要同 MINOR、升級順序、`provenance rebuild`）。然後 `cp -r .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .claude/skills/vcp-provenance-graph .claude/skills/vcp-release-and-environments .agents/skills/`。
- 五份 spec 各加一段摘要，結尾「細節見 `2026-10-31-vcp-wave1c-code-snapshot-and-authorization-design.md`」：訓練層（PROBE 提前、root 與快照、追蹤）、不可變產物（兩個保留輸入名）、提交治理（`require_code`、`Staged.code`、`Artifact.code_snapshot`）、備份（四個 role 與 tier）、dirty 工作樹（決定 1 對未追蹤 `CODE` 檔的偏離）。
- 稽核文件：VCP-004、VCP-006 的狀態行寫「0.18.0 已實作（Wave 1c），見 spec 與本計畫」，第 1 節表格的 P0「自動 code/environment inventory」列更新；VCP-007 狀態行的「commit 時比對程式碼快照的部分等 Wave 1c」改成已處理。dirty 工作樹後記 §3 第 3 條補一句處置（2026-11-03，Task 1）。
- 新後記（照 `2026-11-01-vcp-0170-selection-gaps-and-gc-followups.md` 的樣子）：標頭寫 spec、分支、base、commit 清單；§1 已知限制指回 spec §12；§2.1 實作裁決——本計畫「Open points」每條的最終裁決、`ProbeReport` 與 `snapshot(report=)`、`coderun` 的位置、`_code_tracking_off` 與 `trace_on`、測試目錄不放 `__init__.py`、editable vcp checkout 的雜湊量；§2.2 開放待辦——精確到「被 import 的檔」的重用（spec §12）、標準庫寫的 Kaggle 內驗證腳本、其他框架的生成程式碼登記項、wandb 之類輸出目錄的提示。
- `HANDOVER.md`：現況（0.18.0 待發）、程式碼地圖加 `vcp.code`、`coderun`、`authorize`、`authorization`；陷阱加 `tests/unit/code` 與 autouse fixture。`CODEX_PROMPT.md`：狀態行與 1.0 門檻（spec §13）的進度。
- `CHANGELOG.md`、`__version__`、`plugin.json`、tag：不在本分支。

- [ ] **Step 2: 驗證文件** — `diff CLAUDE.md AGENTS.md; diff -r .claude/skills .agents/skills; git diff --check; uv run pytest tests/unit/test_skills_plugin.py -q`；Expected: 三個 diff 無輸出、PASS。
- [ ] **Step 3: Commit** — `git add CLAUDE.md AGENTS.md docs/reference/cli.md README.md README.zh-TW.md .claude/skills/vcp-train-submit-backup .claude/skills/vcp-running-contests .claude/skills/vcp-orientation .claude/skills/vcp-provenance-graph .claude/skills/vcp-release-and-environments .agents/skills/vcp-train-submit-backup .agents/skills/vcp-running-contests .agents/skills/vcp-orientation .agents/skills/vcp-provenance-graph .agents/skills/vcp-release-and-environments docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md docs/superpowers/specs/2026-09-27-vcp-dirty-tree-design.md docs/audits/2026-09-11-vcp-improvement-audit.md docs/superpowers/plans/2026-09-27-vcp-dirty-tree-followups.md docs/superpowers/plans/2026-11-03-vcp-wave1c-followups.md docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md`；`git commit -m "docs: 0.18.0 code snapshots, traces and authorizations"`。
- [ ] **Step 4: 全套驗證** — `uv run ruff check . && uv run ruff format --check . && uv run pytest --cov=vcp`；Expected: 全綠、`TOTAL` coverage ≥ 80%；推上去後 Windows 與 Linux 的 CI 都綠。

---

## 自我檢查（對 spec 逐節核對的結果）

- **覆蓋：** §4.1 → T2（身分、`snapshot.json`、保留）+ T4（產物、鎖、重用、半途）；§4.2 → T2（規則、上限）+ T3（checkpoint 與生成目錄的排除）；§4.3 → T5（三段、kind、保留名）+ T6（distributions、identity）+ T7（`CodeRoot` 寫入、label、ref、事件）；§4.4 → T9；§4.5 → T12；§4.6 → T12（左欄）+ T13（右欄、run 依據）+ T14（train 消費者）；§4.7 → T17；§4.8 → T18（備份、gc）+ T19（provenance）；§5.1 → T6（PROBE）+ T7（第 2–5、7–9 步、WARN 與欄位）+ T14（第 6、8 步）；§5.2 → T3（登記表）+ T9；§5.3 → T8 + T9；§5.4 → T10；§5.5 → T11（快照輸入、同程序追蹤）+ T12（授權輸入）；§5.6 → T12–T14；§5.7 → T15（`verify` 一覽的各層由 T4、T10、T18、T19 的測試鎖）；§5.8 → T10（`train status`）+ T16 + T17；§6 每一列都有 VERDICT 斷言（`train run` T7–T10、T14；`train status` T10；`eval ingest` T16；`submit stage` / `final` T17；`code *` T15；`artifact authorize` T13；`artifact create` T11、T12）；§7 → T2、T3、T7 的機密與未追蹤測試；§8 → T5、T17 的 golden + T19 的 rebuild 說明 + T21 的升級順序；§9 版本照指示不在本計畫；§10.1–§10.7 每條對到一個測試名（§10.1 → T2–T4；§10.2 → T7、T8、T10、T14；§10.3 → T9；§10.4 → T10；§10.5 → T11、T12、T14；§10.6 → T15–T19 與 T5、T17 的 golden、T1；§10.7 → T20）；§11 → T21。
- **型別一致：** `IndexEntry`、`RootScan`、`snapshot_json_bytes` 在 T2 定義，T4、T9、T14、T15 使用；`PlannedRoot`、`Editable`、`editables_of`、`plan_roots` 在 T3 定義，T6、T7、T11、T15 使用；`SnapshotOutcome`、`write_snapshot`、`check_snapshot`、`load_index`、`snapshot_json_path`、`SNAPSHOT_PATH` 在 T4 定義，之後各 task 使用；`CodeRoot`、`CodeSection`、`EnvIdentity`、`CodeGrade`、`CODE_RANK`、`read_sections`、`evidence_count` 在 T5 定義；`ProbeReport`、`probe` 在 T6 定義、T7 使用；`RootResult`、`CodePlan`、`write_all` 在 T7 定義、T8、T14 使用；`classify_modules`、`stdlib_dirs`、`site_dirs` 在 T9 定義、T11 使用；`code_info`、`lowest` 在 T10 定義、T16、T17、T19、T20 使用；`strict` 在 T11 定義、T15 使用；`consume`、`artifact_claims`、`AuthorizeSpec`、`authorization_input` 在 T12 定義、T13、T14、T18 使用；`train_claims`、`run_claims` 在 T13 定義、T14 使用。
- **既有測試要改的地方：** T5：三處 VERDICT 的 `evidence=` 改用 `evidence_count`（值不變）；T6：`test_probe_prints_json_for_this_interpreter` 的鍵；T7：沒給 cwd 的 train run 測試補 tmp cwd。其餘既有斷言不動：新 `EnvSnapshot` 欄位有預設、`snapshot()` 的舊呼叫照用、沒有新 ref 時 YAML 位元組不變、autouse fixture 讓既有 train run 只多一個 plain cwd root（沒有新 WARN）。

## Open points for the controller

**裁決（controller，2026-11-03）：十五項全部照提議**，對實作有約束力；第 3 項的替代（editable root 改成套件頂層）不採用，雜湊與儲存的實際成本記進後記。第 10 項已對照 `src/vcp/submit/stage.py`：`require_provenance` 也只擋 candidate。

1. **測試目錄**（spec §10.1 `tests/unit/code/`）：`tests/unit` 沒有 `__init__.py`，`tests/unit/code/__init__.py` 會在 prepend 模式下蓋掉標準庫 `code`（0.17 的 `gc` 同一問題）。提議：目錄照 spec，但不放 `__init__.py`、檔名一律 `test_code_*.py`（Global Constraints）。
2. **git 子程序環境的位置**（§2 末點、§14 第 33 條「改用 `gitstate` 的子程序環境」）：`vcp.core.build` import `vcp.train.gitstate` 會循環（`vcp.train/__init__` → `session` → `artifact.writer` → `core.build`）。提議：搬到 `vcp.core.proc.git_environment()`，gitstate 的 `_environment` / `_LOCAL_ENV` 指向它，同一個環境（Task 1）。
3. **editable vcp checkout 的成本**（§5.1 第 4 步、§14 第 8 條）：訓練 venv 的 editable vcp 是整個 checkout，追蹤的 `docs/benchmarks/*.json`（CONFIG，約 120 MB）每個 attempt 都雜湊兩次（開跑與結束比對），快照 id 也隨文件改動而變；在上限內（512 MiB），但慢且吵。提議：照 spec 不改（root = PEP 610 的目錄），把數字寫進後記；單元測試以 autouse fixture 把 editable roots 換成 `[]`，要追蹤的測試換成 `src/vcp`。替代：editable root 改成套件的頂層目錄（`vcp.__file__` 的上層）。
4. **在 root 裡、但被規則跳過的位置**（§5.2 分類順序）：照字面，`<root>/.venv/.../site-packages/numpy` 先對到 root、不在 index → `unlisted`，venv 放在專案裡的 run 永遠到不了 `traced`。提議：相對路徑有規則跳過的段或 venv 上層時不算「在 root 裡」，往下分到 stdlib / distribution / outside（Task 9、Review Focus 1）。
5. **Windows 的 `DLLs`**（§5.2「`sysconfig` 的 `stdlib` / `platstdlib`」）：Windows 的標準庫擴充模組在 `<base_prefix>/DLLs`，不在那兩個路徑裡，照字面會是 `outside`，Windows 上每個 trace 都 incomplete。提議：Windows 上把存在的 `sys.base_prefix/DLLs`、`sys.prefix/DLLs` 算 stdlib（Task 9、Review Focus 2）。
6. **重用時的 `mismatch:`**（§4.1「同 id、不同身分 → `mismatch:` FAIL」對上 §5.1 第 8 步只列四種 reason、§2 反目標「快照失敗不擋訓練」）：data root 裡同 id 的快照被竄改時，`train run` 沒有對應的 `reason`。提議：一律 FAIL `mismatch:`（同 source audit，訊息說搬走目錄再跑），不新增第九個 reason 字；替代是記成某個既有 reason 並降級。
7. **`--code-root` 指到不存在的目錄**（§4.3 `reason: not_found`）：spec 沒說是輸入錯誤還是降級。提議：`--code-root` 是使用者給的輸入，不是目錄就第一次寫入前 FAIL `not_found:`；`reason: not_found` 留給 PROBE 回報、但已不存在的 editable 目錄。
8. **run 依據缺快照**（§4.6「run 依據用右欄」）：依據 run 的那個 attempt 有 root 沒有快照時，那個 `input:code_snapshot.<label>` 算不出來。提議：`authorize` FAIL `invalid:`（不默默少綁一項）；替代是只綁有快照的 root 並在 VERDICT 多一個欄位。
9. **`code_trace` 的 input 名**（§4.4「`inputs` 是各 root 的 `snapshot.json`」）：沒說名字。用保留名 `code_snapshot.<label>` 會讓 writer 在子程序結束時再對每份快照做完整的 `verify`。提議：`snapshot.<label>`（provenance 的 input 邊與 gc 的引用都照樣連得到）。
10. **`require_code` 對 baseline、probe 與 weights run**（§5.8「baseline 與 probe 的待遇照 `require_provenance`」）：`require_provenance` 對 baseline 只在 weights 不屬於 candidate 時擋，程式碼沒有對應的那條。提議：只有 candidate 會 FAIL `code_required:`；baseline 與 probe 對每個低於要求的 run 寫 pairing check `code:<run>=<grade>`。
11. **授權與依據的完好性用哪個字**（§5.6 消耗第 1、2 步、建立第 1 步）：消耗時「完好、沒被 supersede，否則 `superseded:`」一字涵蓋兩種；依據的問題沒說字；建立時依據壞了沒說字（§6 的 `authorize` 沒有 `mismatch:`）。提議：消耗時授權或依據壞了、被取代、不見都用 `superseded:`（訊息說是哪一種）；建立時依據驗不過用 `mismatch:`、被取代用 `superseded:`。
12. **格式細節由本計畫釘死**（spec 沒寫到位元組）：`packages_sha256` 每列 `"<PEP 503 名>==<version> <record_sha256 或 -> [commit_id]"`、排序、各帶 `\n`；`capped` 保留是依 path 順序貪婪（之後較小的檔仍可放進 64 MiB）；`--free` 不在 claims 裡的名字 FAIL `invalid:`；授權的 `params` 在 `consumer: train` 時不寫 `target_kind`（`params` 只收字串）；`code.roots` 是空的不算「快照齊全」；開跑前那一行的路徑寫成 `<label>:<path>`、以 `, ` 串接；train run 的 warning 文字 `code_snapshot=<state> (code_missing=<labels>)` 與 `code_trace=incomplete (code_gaps=<n>)`。
13. **明要快照遇到讀不動的 root**（§5.5、§5.7 只列 `too_large` / `drift` / `locked` / `partial` / `unwritable`）：`unreadable` 沒有對應。提議：ABORT `code_unavailable:`、`cause=unreadable`（權限是環境問題，比照其他三種）。
14. **run 依據的 `input:plan`**（§4.6「取它最後一個 finished attempt 的紀錄」）：train.yaml 沒有記 plan 的 sha。提議：讀現在的 plan 檔（plan 寫了不改，現在的 sha 就是當時的）。
15. **`unwritable` 的預檢**（§5.1 第 8 步、§12「路徑超過 260 字元或只差大小寫的兩個檔，那個 root 記 `unwritable`」）：spec 沒說要不要留下半途目錄；寫到一半才失敗會留下擋住同內容的半途目錄直到 `artifact clean`。提議：Windows 上在宣告 id 之前先檢查路徑長度與大小寫重名，預檢不過就什麼都不寫；寫到一半的 `OSError` 仍是 `unwritable` 並照一般中斷留下半途目錄。
