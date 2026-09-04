# vcp Plan 2c：資料層 hygiene（Plan 2b 後記 §5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan 2b 最終審查延後的機械性小修一次做完：套套邏輯的測試、未測分支、materialize 的 manifest / 快取 / 孤兒檔 / window 語意、coords 稽核的兩個邊角、`summary.json` 記錄略過的檢查，並把計畫層決定寫進 spec v5。

**Architecture:** 不加新單元；每個任務只動既有檔案並帶回歸測試。materialize 的 manifest 只加可選欄位（舊 manifest 仍可讀）。

**Tech Stack:** 同 Plan 2b（Python 3.12 / uv、pydantic v2、numpy、Pillow、pydicom、pytest、ruff）。

**Spec:** `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md` v4 §15；待辦清單 `docs/superpowers/plans/2026-09-03-vcp-plan2b-followups.md` §4–§5。

## Global Constraints

- `uv run` 前綴；三條機械鐵則（`vcp.core.time`、VERDICT 收尾與 exit code、venv 隔離）；`src/vcp` 無比賽名稱。
- 文字檔 `encoding="utf-8", newline="\n"`；讀檔指定 `encoding="utf-8"`。
- 使用者資料或選項問題 → `ValidationFailed`（FAIL）；環境或程式問題 → `VcpError`（ABORT）。
- 測試用 `roots` fixture / tmp；helper 自 `tests/helpers.py`。ruff line-length 100、select `E F I UP B TID`（含 E741：不用 `l` 當變數名）、`ruff format --check`。
- 舊資料相容：既有 `manifest.jsonl`（無 `srcs` 欄）必須仍可讀。
- commit 訊息結尾空行 + `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`。

---

## File Structure

| 檔案 | 責任 | 任務 |
|---|---|---|
| `tests/unit/data/materialize/test_run.py`、`tests/unit/data/test_dicomio.py`、`tests/unit/data/materialize/test_decoders.py`、`tests/unit/test_package.py` | 測試補強與依賴漂移守門 | 1 |
| `src/vcp/data/materialize/{base,manifest,run,window}.py`、`decoders/{base,image,dicom}.py`、`src/vcp/cli.py` | manifest `srcs`、`decode_series(exif_policy)`、孤兒檔清理、`--window` 語意、uint8 MONOCHROME1 | 2 |
| `src/vcp/data/audit/{coords,base}.py`、`src/vcp/cli.py` | 共用尺寸快取、越界優先於重複、`summary.json["skipped"]` | 3 |
| `docs/superpowers/specs/...design.md`（§16）、`docs/superpowers/plans/2026-09-03-vcp-plan2b-followups.md` | spec v5 補充與待辦收尾 | 4 |

---

### Task 1: 測試補強與依賴漂移守門（後記 §4 M3、§5-4、§5-5）

**Files:**
- Modify: `tests/unit/data/materialize/test_run.py:78-81`、`tests/unit/data/test_dicomio.py`、`tests/unit/data/materialize/test_decoders.py`、`tests/unit/test_package.py`

**Interfaces:** 無新介面；只動測試。

- [ ] **Step 1: `--decoder` 測試去套套邏輯**

把 `test_bad_decoder_option_is_a_validation_failure` 改為：

```python
def test_bad_decoder_option_is_a_validation_failure(roots):
    """F7: an unknown --decoder is the caller's mistake (FAIL), not a registry ABORT."""
    _image_ds(roots, n=1)
    with pytest.raises(ValidationFailed, match="must be one of"):
        materialize(_spec(roots, mode="npy", decoder="nifti"))
```

Run: `uv run pytest tests/unit/data/materialize/test_run.py -k bad_decoder -q`。Expected: PASS。再暫時把 `src/vcp/data/materialize/run.py` `_validate` 的 decoder 兩行註解掉重跑一次，Expected: FAIL（RegistryError 不是 ValidationFailed）；然後還原。

- [ ] **Step 2: dicomio 與 rescale 的未測分支**

`tests/unit/data/test_dicomio.py` 加：

```python
def test_sort_by_position_uses_default_normal_without_orientation():
    def header(name: str, z: float) -> SliceHeader:
        return SliceHeader(Path(name), "s", "se", name, 4, 4, None, (0.0, 0.0, z), None, {})

    ordered = sort_slices([header("b.dcm", 9.0), header("a.dcm", 3.0), header("c.dcm", 6.0)])
    assert [h.path.name for h in ordered] == ["a.dcm", "c.dcm", "b.dcm"]
```

`tests/unit/data/materialize/test_decoders.py` 加：

```python
def test_rescale_dtype_ladder():
    from vcp.data.materialize.decoders.dicom import rescale

    arr = np.array([[5, 7]], dtype=np.uint16)
    assert rescale(arr, 1.0, 0.0, False).dtype == np.uint16
    assert rescale(arr, 1.0, 0.0, True).dtype == np.int16  # signed pixels stay signed
    assert rescale(arr, 1.0, -10.0, False).dtype == np.int16
    assert rescale(arr, 100000.0, 0.0, False).dtype == np.int32
    assert rescale(arr, 0.5, 0.0, False).dtype == np.float32
```

Run: `uv run pytest tests/unit/data/test_dicomio.py tests/unit/data/materialize/test_decoders.py -q`。Expected: PASS。

- [ ] **Step 3: 依賴漂移守門**

後記 §5-5 建議把 dev 群組改成依賴 `vcp[dicom]`；裁決：不改依賴結構（PEP 735 群組不能引用專案自己的 extra，uv 的自我引用語法尚不穩定），改以測試守門。`tests/unit/test_package.py` 加（`tomllib` 是標準庫）：

```python
def test_dev_group_pins_every_optional_extra():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = set(doc["dependency-groups"]["dev"])
    for extra, pins in doc["project"]["optional-dependencies"].items():
        missing = [p for p in pins if p not in dev]
        assert not missing, f"extra {extra!r} pins {missing} are missing from the dev group"
```

Run: `uv run pytest tests/unit/test_package.py -q`。Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add tests/unit/data/materialize/test_run.py tests/unit/data/test_dicomio.py tests/unit/data/materialize/test_decoders.py tests/unit/test_package.py
git commit -m "test: --decoder 測試去套套邏輯、dicomio 預設法線與 rescale dtype 階梯、dev 群組與 extra 同步守門

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: materialize 小修（後記 §5-2：M4、M5、M6、M7、M9、M10）

**Files:**
- Modify: `src/vcp/data/materialize/manifest.py:13-30`、`src/vcp/data/materialize/base.py`（`MaterializeSpec.window`、`MaterializeResult.orphans_removed`）、`src/vcp/data/materialize/run.py`、`src/vcp/data/materialize/window.py:27-37`、`src/vcp/data/materialize/decoders/{base,image,dicom}.py`、`src/vcp/cli.py`（materialize 命令的 `--window` 預設與 `orphans_removed` 欄位）
- Test: `tests/unit/data/materialize/test_run.py`、`tests/unit/data/materialize/test_decoders.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Produces: `ManifestRow.srcs: list[str] | None = None`（每列都寫，堆疊列為完整來源）；`Decoder.decode_series(paths, *, exif_policy="stored")`；`MaterializeSpec.window: str | None = None`（png 缺省 → `dicom`；npy 明確給 → `ValidationFailed`）；`MaterializeResult.orphans_removed: int = 0`；`to_uint8` 對 uint8 且 `photometric == "MONOCHROME1"` 反相；成功的堆疊列取代其 views 的逐 view 列。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/materialize/test_run.py` 加：

```python
def test_stack_row_records_all_sources_and_window_semantics(roots):
    _dicom_ds(roots, "dcm")
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    row = read_manifest(res.manifest_path)[row_key("1.2.1", None, "1.2.1.1")]
    assert row.srcs is not None and len(row.srcs) == 3 and row.src == row.srcs[0]
    with pytest.raises(ValidationFailed, match="window"):
        materialize(_spec(roots, name="dcm", mode="npy", window="minmax"))
    png = materialize(_spec(roots, name="dcm", mode="png", resize=8))
    assert next(iter(read_manifest(png.manifest_path).values())).window == "dicom"
    again = materialize(_spec(roots, name="dcm", mode="png", resize=8, window="minmax"))
    assert again.materialized == 6 and again.skipped == 0  # window change invalidates the cache


def test_orphan_outputs_are_removed_when_plan_changes(roots):
    _dicom_ds(roots, "dcm")
    stacked = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (stacked.out_dir / "1.2.1" / "1.2.1.1.npy").is_file()
    plain = materialize(_spec(roots, name="dcm", mode="npy"))
    assert plain.materialized == 6 and plain.orphans_removed == 2
    assert not (plain.out_dir / "1.2.1" / "1.2.1.1.npy").exists()
    assert (plain.out_dir / "manifest.jsonl").is_file()
    back = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert back.materialized == 2 and back.orphans_removed == 6
    assert not (back.out_dir / "1.2.1" / "0.npy").exists()
    assert len(read_manifest(back.manifest_path)) == 2  # the stack rows supersede per-view rows


def test_old_manifest_without_srcs_still_loads(roots):
    _image_ds(roots, n=1)
    res = materialize(_spec(roots, mode="npy"))
    lines = res.manifest_path.read_text(encoding="utf-8").splitlines()
    assert all('"srcs"' in line for line in lines)
    stripped = [{k: v for k, v in json.loads(line).items() if k != "srcs"} for line in lines]
    with res.manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(row) + "\n" for row in stripped)
    assert len(read_manifest(res.manifest_path)) == 1
    assert materialize(_spec(roots, mode="npy")).skipped == 1
```

`tests/unit/data/materialize/test_decoders.py` 加：

```python
def test_to_uint8_inverts_monochrome1_even_for_uint8():
    arr = np.array([[0, 255]], dtype=np.uint8)
    assert to_uint8(Decoded(arr, {"photometric": "MONOCHROME1"}), "minmax").tolist() == [[255, 0]]
    assert to_uint8(Decoded(arr, {}), "minmax") is arr


def test_image_decode_series_honours_exif_policy(tmp_path):
    write_exif_image(tmp_path / "o.jpg", size=(8, 4), orientation=6)
    dec = get_decoder("image")
    assert dec.decode_series([tmp_path / "o.jpg"]).array.shape == (1, 4, 8, 3)
    oriented = dec.decode_series([tmp_path / "o.jpg"], exif_policy="oriented")
    assert oriented.array.shape == (1, 8, 4, 3)
```

`tests/unit/test_cli.py` 的 `test_materialize_cli` 末尾加：

```python
    r = runner.invoke(
        app, ["data", "materialize", "--name", "tiny", "--mode", "npy", "--window", "minmax"]
    )
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)
```

Run: `uv run pytest tests/unit/data/materialize tests/unit/test_cli.py -k "materialize or uint8 or decode_series" -q`。Expected: FAIL（`srcs` 不存在、npy + window 不 FAIL、`orphans_removed` 不存在、uint8 未反相、`exif_policy` 關鍵字不存在）。

- [ ] **Step 2: 實作**

`manifest.py`：`ManifestRow` 在 `src` 之後加 `srcs: list[str] | None = None`。

`base.py`：`MaterializeSpec.window: str | None = None`；`MaterializeResult` 加 `orphans_removed: int = 0`。

`decoders/base.py` 的 protocol：`def decode_series(self, paths: list[Path], *, exif_policy: str = "stored") -> Decoded: ...`。`image.py`：`decode_series(self, paths, *, exif_policy="stored")` 內改 `self.decode(p, exif_policy=exif_policy)`。`dicom.py`：簽名同樣加 `*, exif_policy: str = "stored"`（忽略：DICOM 無 EXIF）。

`window.py` 的 `to_uint8`（docstring 改為「uint8 input is returned untouched except for the MONOCHROME1 inversion」）：

```python
    arr = decoded.array
    inverted = decoded.info.get("photometric") == "MONOCHROME1"
    if arr.dtype == np.uint8:
        return (255 - arr) if inverted else arr
    lo, hi = _bounds(decoded, mode)
    scaled = np.clip((arr.astype(np.float64) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    out = np.round(scaled * 255).astype(np.uint8)
    return (255 - out) if inverted else out
```

`run.py`：

1. `_validate`：把 window 檢查改成兩條：
   ```python
       if spec.window is not None and spec.window not in WINDOW_MODES:
           raise ValidationFailed(f"--window must be one of {WINDOW_MODES}, got {spec.window!r}")
       if spec.window is not None and spec.mode != "png":
           raise ValidationFailed("--window applies to png mode only (npy keeps raw values)")
   ```
2. `materialize` 建 `Settings(..., spec.window or "dicom", ...)`。
3. `_row` 的參數 `src: str` 改成 `srcs: tuple[str, ...]`，寫 `src=srcs[0], srcs=list(srcs)`；`run_job` 四處呼叫：series 目錄、單 view、堆疊都傳 `job.srcs`，shape 不一致的逐 view fallback 傳 `(src,)`。
4. `run_job` 的 series 目錄分支改 `dec.decode_series(files, exif_policy=cfg.exif_policy)`。
5. 成功的堆疊列取代逐 view 列（否則 `_planned_keys` 會把舊的逐 view 列留在 manifest，孤兒清理永遠清不到）：在 `materialize` 合併 outcomes 之前加
   ```python
       for job, o in zip(todo, outcomes, strict=True):
           if job.view is None and any(r.view is None for r in o.rows):  # stack succeeded
               for i in job.views:
                   rows.pop(row_key(job.sample_id, i, job.seq_id), None)
   ```
6. 孤兒清理 helper（模組層級，`from collections.abc import Iterable`）：
   ```python
   def _remove_orphans(out_root: Path, rows: Iterable[ManifestRow]) -> int:
       """Delete outputs under ``out_root`` no manifest row references (superseded stacks, files
       of failed re-attempts); manifest.jsonl / failed.jsonl stay. Empty sample dirs go too."""
       keep = {r.out for r in rows} | {"manifest.jsonl", "failed.jsonl"}
       removed = 0
       for p in sorted(out_root.rglob("*")):
           if p.is_file() and p.relative_to(out_root).as_posix() not in keep:
               p.unlink()
               removed += 1
       for d in sorted((p for p in out_root.rglob("*") if p.is_dir()), reverse=True):
           if not any(d.iterdir()):
               d.rmdir()
       return removed
   ```
   在 `failed.jsonl` 寫入 / 刪除之後呼叫 `orphans = _remove_orphans(out_root, rows.values())`，`MaterializeResult(..., orphans_removed=orphans)`。

`cli.py` materialize 命令：`window` 參數改 `Annotated[str | None, typer.Option("--window", help="png only: dicom | minmax | percentile (default dicom)")] = None`；`fields` 在 `res.orphans_removed` 非零時加 `fields["orphans_removed"] = res.orphans_removed`。

Run: `uv run pytest tests/unit/data/materialize tests/unit/test_cli.py tests/unit/test_e2e_flow.py -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add src/vcp/data/materialize src/vcp/cli.py tests/unit/data/materialize tests/unit/test_cli.py
git commit -m "fix(materialize): 每列記 srcs、decode_series 帶 EXIF 政策、孤兒輸出清理、堆疊列取代逐 view 列、--window 只對 png、uint8 MONOCHROME1 反相

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: coords 稽核邊角與略過的檢查（後記 §4 dedup、§5-3）

**Files:**
- Modify: `src/vcp/data/audit/coords.py`（`CoordsCheck.run`、`_check_boxes`、`_check_polygons`）、`src/vcp/data/audit/base.py:84-101`（`run_audit`）、`src/vcp/cli.py`（audit 命令 `skipped=` 欄位）
- Test: `tests/unit/data/audit/test_checks.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Produces: `run_audit` 回傳不變，`summary.json` 多 `"skipped": [<不適用的檢查名, 登記順序>]`；`audit` VERDICT 在有略過時帶 `skipped=<a,b>`；`_check_boxes(s, image_root, exif_policy, opts, rows, counts, sizes)` 與 `_check_polygons(s, image_root, exif_policy, rows, counts, sizes)` 共用同一個 `sizes` 快取；越界檢查先於重複檢查。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/audit/test_checks.py` 加（所有 import 該檔已有）：

```python
def test_duplicate_of_out_of_bounds_box_counts_as_out_of_bounds(roots):
    box = Box(x=0, y=0, w=100, h=100, category_id=0)
    ds, paths = _dataset(roots, "dupoob", [("u.png", _gradient(3), Labels(boxes=[box, box]), {})])
    res = get_check("coords").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert res.fields["out_of_bounds"] == 2 and res.fields["suspicious"] == 0


def test_size_cache_is_shared_between_boxes_and_polygons(roots, monkeypatch):
    import vcp.data.audit.coords as coords

    calls = []
    real = coords.image_header

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(coords, "image_header", counting)
    labels = Labels(
        boxes=[Box(x=1, y=1, w=4, h=4, category_id=0)],
        masks=[Mask(category_id=1, polygon=[[0, 0, 4, 0, 4, 4]])],
    )
    ds, paths = _dataset(roots, "shared", [("both.png", _gradient(4), labels, {})])
    get_check("coords").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert len(calls) == 1


def test_run_audit_records_skipped_checks(roots):
    src = roots.data / "raw" / "dcm"
    write_dicom_study(src, series=1, slices=1)
    res = get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name="dcm",
            license="CC0",
            url="u",
            downloaded_at="2026-09-04",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    paths = DatasetPaths.resolve("dcm", data_root=roots.data, configs_root=roots.configs)
    status, results = run_audit(AuditContext(dataset=res.dataset, paths=paths, opts=AuditOptions()))
    summary = json.loads((paths.cache_dir / "audit" / "summary.json").read_text(encoding="utf-8"))
    assert summary["skipped"] == ["coords", "dedup"] and list(results) == ["provenance"]
```

`tests/unit/test_cli.py` 加（補 import：`from helpers import write_dicom_study`（併入既有的 helpers import 行）、`from vcp.data.importers import get_importer`、`from vcp.data.importers.base import ImportSpec`）：

```python
def test_audit_cli_reports_skipped_checks(roots):
    src = roots.data / "raw" / "dcm"
    write_dicom_study(src, series=1, slices=1)
    get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name="dcm",
            license="CC0",
            url="u",
            downloaded_at="2026-09-04",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    r = runner.invoke(app, ["data", "audit", "--name", "dcm"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "skipped=coords,dedup" in v and "provenance=OK" in v
```

Run: `uv run pytest tests/unit/data/audit tests/unit/test_cli.py -k "audit" -q`。Expected: FAIL（`out_of_bounds == 1`、`calls == 2`、`KeyError: 'skipped'`、VERDICT 無 `skipped=`）。

- [ ] **Step 2: 實作**

`coords.py`：
- `CoordsCheck.run` 每個 sample 建一個 `sizes: dict[int, tuple[int, int] | None] = {}`，傳給 `_check_boxes(s, image_root, exif_policy, ctx.opts, rows, counts, sizes)` 與 `_check_polygons(s, image_root, exif_policy, rows, counts, sizes)`；兩個函式加最後一個參數 `sizes`，並刪掉各自的 `sizes = {}`。
- `_check_boxes` 迴圈改順序：(1) `size = _view_size(...)`，None → `unsized` 並 continue；(2) view 無 width/height 時 `box_problems` → `out_of_bounds` 並 continue；(3) 才做 duplicate 檢查（`key in seen` → suspicious `duplicate of box N` 並 continue；否則 `seen[key] = i`）；(4) `suspicious_problems`。docstring 註明「bounds before duplicates: a duplicated out-of-bounds box is two bad boxes, not one」。

`base.py` `run_audit`：

```python
    applicable = {name: check for name, check in AUDITS.items() if check.applies(ctx.dataset)}
    skipped = [name for name in AUDITS if name not in applicable]
    results = {name: check.run(ctx) for name, check in applicable.items()}
```

summary 加 `"skipped": skipped`（放在 `"checks"` 之前）。回傳型別不變。

`cli.py` audit 命令：`from vcp.data.audit import AUDITS, AuditContext, AuditOptions, run_audit`；`fn()` 內 `run_audit` 之後加
```python
        skipped = [n for n in AUDITS if n not in results]
```
`fields["summary"]` 之前加 `if skipped: fields["skipped"] = ",".join(skipped)`；`payload` 加 `"skipped": skipped`。

Run: `uv run pytest tests/unit/data/audit tests/unit/test_cli.py -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add src/vcp/data/audit/coords.py src/vcp/data/audit/base.py src/vcp/cli.py tests/unit/data/audit/test_checks.py tests/unit/test_cli.py
git commit -m "fix(audit): 越界優先於重複、boxes 與 polygons 共用尺寸快取、summary 與 VERDICT 記錄略過的檢查

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: spec v5 補充與待辦收尾（後記 §5-1）

**Files:**
- Modify: `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`（第 4–7 行的版本 / 狀態 / 後續，新增 §16）、`docs/superpowers/plans/2026-09-03-vcp-plan2b-followups.md`（§4、§5 標記完成）

- [ ] **Step 1: 寫 §16**

版本行末尾加「；v5（2026-09-04）依 Plan 2b 後記與 Plan 2c hygiene，新增 §16 收錄計畫層決定」；狀態行末尾加「；v5 §16 為 Plan 2b / 2c 實作結果的紀錄（裁決已記在後記，不另行核可）」；後續行末尾加「→ Plan 2c（hygiene）→ 子專案 2 量測層（`2026-09-04-vcp-measurement-layer-design.md`）」。檔尾加：

```markdown
## 16. v5 補充決定（Plan 2b 實作與 Plan 2c hygiene 的定案，2026-09-04）

以下為實作期間由計畫或審查裁決、原 spec 未明說的規則，與前文衝突時以本節為準。

1. **materialize**：`--resize` 只對 `png`（npy 保留原解析度，給了即 FAIL）；`--window` 只對 `png`（npy 明確給了即 FAIL，未給時 png 預設 `dicom`）；png 模式遇到體積（series 層級 view 或堆疊）記入 `failed.jsonl` 而非另開目錄；`--workers` 預設 1；manifest 列多 `bytes`（skip 判斷用）與 `srcs`（每列都寫，堆疊列為完整來源，`src` 恆等於 `srcs[0]`；舊 manifest 無此欄仍可讀）；堆疊工作只認自己的列，尺寸不一致的序列每次重試並 WARN；成功的堆疊列取代其 views 的逐 view 列；每輪結束後 manifest 只保留本輪規劃且未失敗的列，未被任何列引用的輸出檔會被刪除（VERDICT `orphans_removed=`）；快取比對含解碼器名與版本、resize、window、exif_policy、檔案大小；`to_uint8` 對 uint8 輸入不重新映射，但 MONOCHROME1 仍反相。
2. **dicom 匯入器**：沒有 `labels_csv` 時 task 預設 `multilabel`（可 `--opt task=regression`）；series 層級 view 的 `seq_index` 為 None；目錄 view 一律由 dicom 解碼器讀取（`Path.suffix` 對 UID 目錄名不可靠）；所有選項在 header 掃描之前驗證。
3. **COCO 匯入 + EXIF**：COCO 的 `width`/`height` 視為儲存像素尺寸；`--opt exif=oriented` 下遇到會對調軸的方向即 `ValidationFailed`，提示改用 `stored` 或拿掉 JSON 尺寸。
4. **檢查適用性**：dedup 只在所有 view 副檔名屬 `IMAGE_EXTS` 時執行；不適用的檢查（登記順序）記在 `summary.json["skipped"]` 與 audit VERDICT 的 `skipped=`，不改狀態。
5. **coords**：越界檢查先於重複檢查（重複的越界框計入 out_of_bounds）；boxes 與 polygons 共用每個 sample 的尺寸快取。
6. **import 的 `old_card=unreadable`**：只在 `plans_invalidated > 0` 時輸出。
7. **依賴**：選用 extra 的每個 pin 都同時列在 dev 群組，由 `tests/unit/test_package.py` 守門；不改成 dev 依賴 `vcp[dicom]`。
```

- [ ] **Step 2: 後記收尾**

`2026-09-03-vcp-plan2b-followups.md`：§4 兩點各加「→ Plan 2c Task 1 / Task 3 完成」；§5-1 →「Task 4 §16」；§5-2 → M4、M5、M6、M7、M9、M10「Task 2 完成」、M11「留待（效能，量測層之後）」；§5-3 →「Task 3 完成」；§5-4 → M3 與 T4#51「Task 1 完成」，`test_rsna_knee` 一項「已於 2026-09-04 以真資料驗證通過」；§5-5 →「Task 1 以守門測試取代，不改依賴結構」；§5-7 →「skill 已完成（48184ef），量測層 spec 已寫」。

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md docs/superpowers/plans/2026-09-03-vcp-plan2b-followups.md
git commit -m "docs: spec v5 §16 收錄 Plan 2b/2c 的計畫層決定；Plan 2b 後記標記完成項目

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Run（最終）: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`。Expected: 全綠。

---

## 自我審查紀錄

- 後記對照：§4 M3 → T1；§4 dedup skipped → T3；§5-1 → T4；§5-2（M4、M5、M6、M7、M9、M10）→ T2，M11 留待；§5-3 → T3；§5-4 → T1；§5-5 → T1 守門測試（裁決：不改依賴結構）；§5-6 維持；§5-7 已完成。
- 相容性：`ManifestRow.srcs` 可選，舊 manifest 可讀（T2 有測試）；`MaterializeSpec.window` 由 `str` 改 `str | None`，既有測試傳 `window="dicom"` / `"gamma"` 仍走原本的驗證訊息；CLI `--window` 預設改 None。
- 語意新增：成功堆疊取代逐 view 列（T2 第 5 點）是 M6 的必要前提，否則 `_planned_keys` 的 fallback 保留會讓孤兒清理無效；記入 §16-1。
- 型別一致性：`_row(job, view, srcs, out, shape, dtype, cfg, version)` 的四處呼叫皆更新；`decode_series` 的關鍵字在 protocol、兩個解碼器、`run_job` 一致；`_check_boxes` / `_check_polygons` 的 `sizes` 參數與 `run` 的呼叫一致。
