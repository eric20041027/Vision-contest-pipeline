# vcp Plan 2b：dicom 匯入器、materialize、EXIF 與 coords 稽核落地 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 RSNA 形態的資料（DICOM 目錄樹、study 含多 series 多切片、只有部分 study 有人工標籤）進入 `vcp` 的標準格式，並以 `vcp data materialize` 把 DICOM 或大圖解碼成可搬移的 npy / png 快取；同時落實 spec §15 已核可的 EXIF 政策、coords 稽核職責切分、raw manifest 模式與 Plan 2a 遺留的 hygiene。

**Architecture:** 三個新單元加在既有登記表之上：`data/dicomio.py`（header 讀取與切片排序，匯入器與解碼器共用）、`data/importers/dicom.py`（第七個匯入器）、`data/materialize/`（解碑器登記表 `decoders/` + 執行器 + manifest）。EXIF 政策成為 card 欄位，由 `importers/common.make_view` 寫入、解碼器與匯出器讀取。coords 稽核不再重複載入驗證，改報可疑框、無尺寸 view 的越界、與匯入時被擋的列。

**Tech Stack:** Python 3.12（uv）、pydantic v2、typer、numpy、Pillow、pydicom 3 + pylibjpeg（`dicom` extra，dev 群組亦裝）、pytest / pytest-cov、ruff。

**Spec:** `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md` §15（並參照 §6.1 `dicom` 列、§6.3、§14.1）。Plan 2a 後記：`docs/superpowers/plans/2026-09-03-vcp-plan2a-followups.md`。

## Global Constraints

- 每個專案命令前綴 `uv run`（例：`uv run pytest`、`uv run ruff check .`）；改 `pyproject.toml` 後先 `uv sync`。
- 三條機械鐵則（`CLAUDE.md`）：取時只用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251 會擋 `datetime.now` 等）；每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT` 收尾、exit 0 / 0 / 1 / 2、永不互動、`--json` 時 JSON 到 stdout、VERDICT 到 stderr；venv 隔離。
- 通用性：`src/vcp` 不得出現比賽名稱或比賽專屬欄名；RSNA 的欄名只出現在測試、README 範例與 `projects/rsna-knee/`。六個變異軸都是登記表。
- 寫入會被 hash 或被 git 紀錄的文字檔一律 `encoding="utf-8", newline="\n"`；讀檔一律指定 `encoding="utf-8"`。
- 錯誤語意：使用者資料問題 → `ValidationFailed`（FAIL）；環境或程式問題 → `VcpError` / 其他例外（ABORT）。
- 測試：`tests/conftest.py` 的 autouse fixture 已把 `VCP_DATA_ROOT` / `VCP_CONFIGS_ROOT` 指到 tmp；需要真實路徑物件時用 `roots` fixture（`roots.data`、`roots.configs`）。夾具 helper 在 `tests/helpers.py`（`from helpers import ...`，`pythonpath = ["tests"]`）。真資料測試放 `tests/integration/`、標記 `realdata`、資料缺席即 skip。
- ruff：line-length 100、select `E F I UP B TID`；`uv run ruff format --check .` 也要過。覆蓋率門檻 80%（`fail_under`）。
- Windows 開發機：符號連結可能無權限；`Path.glob` 不分大小寫但 Linux 分，程式碼要自己處理副檔名大小寫。
- 每個 commit 訊息結尾加空行與 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`（`.superpowers/`、`projects/rsna-knee/` 的未追蹤檔不屬於本計畫）。

---

## File Structure

| 檔案 | 責任 | 任務 |
|---|---|---|
| `pyproject.toml` | dev 群組加 pydicom / pylibjpeg 三件 | 1 |
| `src/vcp/data/schema.py` | `ExifPolicy`、`RawManifestMode`、`DatasetCard.exif_policy`、`SourceInfo.raw_manifest_mode` | 1 |
| `src/vcp/core/hashing.py` | `dir_manifest(root, *, mode)` 的 `sizes` 模式 | 1 |
| `src/vcp/data/importers/base.py` | `ImportSpec.raw_manifest`、`ImportResult.exif_rotated`、`finalize_import(exif_policy, exif_rotated)`、`count_invalidated_plans` 回傳可讀性 | 1, 7 |
| `src/vcp/data/importers/common.py` | `image_header`、`make_view(exif_policy)`、`choice_option`、`exif_policy_option`、`count_exif_rotated` | 2 |
| `src/vcp/data/importers/{csv_boxes,coco,yolo,imagefolder,image_csv,jsonl}.py` | 傳遞 EXIF 政策與計數 | 2 |
| `src/vcp/data/exporters/base.py` | `ExportOutput`、`ExportResult.fields`、manifest 的 `exif_policy` / `exif_rotated` / 匯出器附加欄位 | 2, 7 |
| `src/vcp/data/exporters/{coco,yolo}.py` | 回傳 `ExportOutput`；YOLO 的 `images=` 欄位、manifest `categories`、`_place_image` 例外收斂 | 2, 7 |
| `src/vcp/data/audit/base.py` | `AuditOptions.min_box_px / max_aspect / max_cover` | 3 |
| `src/vcp/data/audit/coords.py` | 可疑框、無尺寸 view 越界、`import_skipped` 併入 | 3 |
| `src/vcp/data/dicomio.py` | `require_pydicom`、`SliceHeader`、`read_header`、`sort_slices`、`jsonable` | 4 |
| `src/vcp/data/materialize/decoders/{__init__,base,image,dicom}.py` | `Decoded`、`Decoder` 協定與登記表、image / dicom 解碼器 | 4 |
| `src/vcp/data/materialize/window.py` | `to_uint8(decoded, mode)`、`resize_long_side` | 4 |
| `src/vcp/data/importers/dicom.py` | 第七個匯入器 | 5 |
| `src/vcp/data/materialize/{__init__,base,manifest,run}.py` | `MaterializeSpec` / `MaterializeResult`、manifest 列、執行器 | 6 |
| `src/vcp/cli.py` | `import --raw-manifest`、`exif_rotated` 欄位、audit 三個門檻選項、`materialize` 命令、`old_card=unreadable`、匯出器附加欄位 | 1, 2, 3, 6, 7 |
| `tests/helpers.py` | `write_exif_image`、`write_dicom_study` | 2, 4 |
| `tests/unit/...`、`tests/integration/test_rsna_knee.py`、`tests/unit/test_e2e_flow.py` | 每任務的測試；真資料與端到端 | 各任務, 8 |
| `README.md`、`CLAUDE.md`、`tests/integration/README.md` | 命令總表、`rows_read` 語意、Kaggle 用法、RSNA 子集準備 | 8 |

任務順序：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8。Task 4 產出的 `dicomio` 與夾具被 5、6 使用；Task 2 產出的 `ExportOutput` 被 7 使用。

---

### Task 1: 依賴、card 新欄位、raw manifest 模式（spec §15.1-1、§15.5、§15.6-25）

**Files:**
- Modify: `pyproject.toml:28-30`（dev 群組）
- Modify: `src/vcp/data/schema.py:14`（型別別名）、`:96-104`（`SourceInfo`）、`:107-116`（`DatasetCard`）
- Modify: `src/vcp/core/hashing.py:42-45`（`dir_manifest`）
- Modify: `src/vcp/data/importers/base.py:20-50`（`ImportSpec`、`ImportResult`）、`:88-150`（`finalize_import`）
- Modify: `src/vcp/cli.py:122-174`（`import_cmd`）
- Test: `tests/unit/data/test_schema.py`、`tests/unit/core/test_hashing.py`、`tests/unit/data/importers/test_jsonl.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: 現有 `finalize_import(*, spec, importer, task, categories, image_root, samples, rows_read, skipped, unlabeled=0)`。
- Produces（後續任務依賴）：
  - `schema.ExifPolicy = Literal["stored", "oriented"]`、`schema.RawManifestMode = Literal["full", "sizes"]`
  - `DatasetCard.exif_policy: ExifPolicy = "stored"`、`SourceInfo.raw_manifest_mode: RawManifestMode = "full"`
  - `hashing.dir_manifest(root: Path, *, mode: str = "full") -> list[str]`
  - `ImportSpec.raw_manifest: RawManifestMode = "full"`；`ImportResult.exif_rotated: int = 0`
  - `finalize_import(..., exif_policy: str = "stored", exif_rotated: int = 0)`
  - CLI `vcp data import --raw-manifest full|sizes`；VERDICT 欄位 `exif_rotated=<n>`（>0 時 WARN）

- [ ] **Step 1: 加 dev 依賴並同步**

`pyproject.toml` 的 `[dependency-groups]` 改為：

```toml
[dependency-groups]
dev = [
  "pytest>=8.0",
  "pytest-cov>=5.0",
  "ruff>=0.5",
  "pydicom>=3.0",
  "pylibjpeg>=2.0",
  "pylibjpeg-libjpeg>=2.0",
  "pylibjpeg-openjpeg>=2.0",
]
```

Run: `uv sync && uv run python -c "import pydicom, pylibjpeg; print(pydicom.__version__)"`
Expected: 印出 `3.0.x`（Windows / py3.12 的 wheel 已在本機驗證可裝）。

- [ ] **Step 2: 寫 schema 的失敗測試**

在 `tests/unit/data/test_schema.py` 末尾加（若檔頭沒有，補 `import pytest`、`from pydantic import ValidationError`、`from helpers import make_card`、`from vcp.data.schema import DatasetCard`）：

```python
def test_card_exif_policy_and_manifest_mode_defaults_are_backward_compatible():
    card = make_card("det")
    assert card.exif_policy == "stored"
    assert card.source.raw_manifest_mode == "full"
    dumped = card.model_dump(mode="json")
    dumped.pop("exif_policy")
    dumped["source"].pop("raw_manifest_mode")
    assert DatasetCard.model_validate(dumped).exif_policy == "stored"  # v3 cards still load
    with pytest.raises(ValidationError):
        DatasetCard.model_validate({**card.model_dump(mode="json"), "exif_policy": "rotated"})
    with pytest.raises(ValidationError):
        DatasetCard.model_validate(
            {**dumped, "source": {**dumped["source"], "raw_manifest_mode": "md5"}}
        )
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_schema.py -k exif_policy -q`
Expected: FAIL（`AttributeError: 'DatasetCard' object has no attribute 'exif_policy'`）。

- [ ] **Step 4: 加 schema 欄位**

`src/vcp/data/schema.py`：在 `LabelSource = ...` 之後加

```python
ExifPolicy = Literal["stored", "oriented"]
RawManifestMode = Literal["full", "sizes"]
```

`SourceInfo` 末尾加 `raw_manifest_mode: RawManifestMode = "full"`；`DatasetCard` 在 `schema_version: int = 1` 之前加 `exif_policy: ExifPolicy = "stored"`。

Run: `uv run pytest tests/unit/data/test_schema.py -q`
Expected: PASS。

- [ ] **Step 5: 寫 `dir_manifest` 的失敗測試**

`tests/unit/core/test_hashing.py` 末尾加（補 `import pytest` 與 `from vcp.core.hashing import dir_manifest, manifest_hash` 若缺）：

```python
def test_dir_manifest_sizes_mode_skips_hashing(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00" * 3)
    full = dir_manifest(tmp_path)
    sizes = dir_manifest(tmp_path, mode="sizes")
    assert [ln.split("\t")[:2] for ln in full] == [ln.split("\t")[:2] for ln in sizes]
    assert all(ln.endswith("\t-") for ln in sizes)
    assert not any(ln.endswith("\t-") for ln in full)
    assert manifest_hash(full) != manifest_hash(sizes)
    with pytest.raises(ValueError, match="manifest mode"):
        dir_manifest(tmp_path, mode="md5")
```

Run: `uv run pytest tests/unit/core/test_hashing.py -k sizes_mode -q`
Expected: FAIL（`TypeError: dir_manifest() got an unexpected keyword argument 'mode'`）。

- [ ] **Step 6: 實作 `sizes` 模式**

`src/vcp/core/hashing.py` 把 `dir_manifest` 換成：

```python
MANIFEST_MODES = ("full", "sizes")


def dir_manifest(root: Path, *, mode: str = "full") -> list[str]:
    """One line per file under ``root``: ``relpath<TAB>size<TAB>md5`` in mode ``full``, or
    ``relpath<TAB>size<TAB>-`` in mode ``sizes`` (no hashing; for very large raw trees).
    Sorted by posix relpath."""
    if mode not in MANIFEST_MODES:
        raise ValueError(f"manifest mode must be one of {MANIFEST_MODES}, got {mode!r}")

    def digest(p: Path) -> str:
        return md5_file(p) if mode == "full" else "-"

    entries = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    return [f"{rel}\t{p.stat().st_size}\t{digest(p)}" for rel, p in entries]
```

Run: `uv run pytest tests/unit/core/test_hashing.py -q`
Expected: PASS。

- [ ] **Step 7: 寫 `finalize_import` 的失敗測試**

`tests/unit/data/importers/test_jsonl.py` 末尾加（該檔已有 `_src`、`_spec`、`CATS`、`det_samples`、`DatasetPaths`、`get_importer`、`json`）：

```python
def test_raw_manifest_sizes_mode_is_recorded(roots, tmp_path):
    src = _src(tmp_path, det_samples(3))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    spec = _spec(roots, src, task="det", categories="categories.json").model_copy(
        update={"raw_manifest": "sizes"}
    )
    res = get_importer("jsonl").run(spec)
    assert res.dataset.card.source.raw_manifest_mode == "sizes"
    assert res.dataset.card.exif_policy == "stored"
    assert res.exif_rotated == 0
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    lines = paths.raw_manifest.read_text(encoding="utf-8").splitlines()
    assert lines and all(ln.endswith("\t-") for ln in lines)
```

Run: `uv run pytest tests/unit/data/importers/test_jsonl.py -k sizes_mode -q`
Expected: FAIL（`AttributeError: ... raw_manifest_mode` 或 `exif_rotated`）。

- [ ] **Step 8: 實作 `ImportSpec` / `ImportResult` / `finalize_import`**

`src/vcp/data/importers/base.py`：

- import 行改為 `from vcp.data.schema import Category, DatasetCard, RawManifestMode, Sample, SourceInfo`。
- `ImportSpec` 在 `notes: str = ""` 之後加 `raw_manifest: RawManifestMode = "full"`。
- `ImportResult` 在 `unlabeled: int = 0` 之後加 `exif_rotated: int = 0`。
- `finalize_import` 簽名在 `unlabeled: int = 0,` 之後加 `exif_policy: str = "stored",` 與 `exif_rotated: int = 0,`；`SourceInfo(...)` 加 `raw_manifest_mode=spec.raw_manifest,`；`DatasetCard(...)` 加 `exif_policy=exif_policy,`（pydantic 會拒絕非法字串）；`dir_manifest(spec.src)` 改為 `dir_manifest(spec.src, mode=spec.raw_manifest)`；`ImportResult(...)` 加 `exif_rotated=exif_rotated,`。docstring 加一句：`exif_policy` / `exif_rotated` 由影像匯入器提供（見 `importers/common.py`）。

Run: `uv run pytest tests/unit/data/importers -q`
Expected: PASS。

- [ ] **Step 9: 寫 CLI 的失敗測試**

`tests/unit/test_cli.py` 末尾加（檔頭補 `import yaml` 若缺）：

```python
def _jsonl_import_args(src, name):
    return [
        "data", "import", "--importer", "jsonl", "--src", str(src), "--name", name,
        "--license", "CC0", "--url", "https://example.org", "--downloaded-at", "2026-09-02",
        "--opt", "task=det", "--opt", "categories=cats.json",
    ]


def test_import_raw_manifest_mode(roots, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_samples_jsonl(src / "samples.jsonl", det_samples(5, seed=0))
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    r = runner.invoke(app, [*_jsonl_import_args(src, "tiny"), "--raw-manifest", "sizes"])
    assert r.exit_code == 0, r.output
    card = yaml.safe_load(
        (roots.configs / "datasets" / "tiny" / "dataset.yaml").read_text(encoding="utf-8")
    )
    assert card["source"]["raw_manifest_mode"] == "sizes" and card["exif_policy"] == "stored"
    r = runner.invoke(app, [*_jsonl_import_args(src, "tiny2"), "--raw-manifest", "md5"])
    assert r.exit_code == 1
    assert "status=FAIL" in _last_verdict(r.output) and "raw-manifest" in r.output
```

Run: `uv run pytest tests/unit/test_cli.py -k raw_manifest_mode -q`
Expected: FAIL（typer：`No such option: --raw-manifest`，exit code 2）。

- [ ] **Step 10: 實作 CLI 選項與 `exif_rotated` 欄位**

`src/vcp/cli.py` 的 `import_cmd`：在 `notes` 參數之後加

```python
    raw_manifest: Annotated[
        str,
        typer.Option(
            "--raw-manifest",
            help="full: md5 of every raw file (default) | sizes: names and sizes only (huge trees)",
        ),
    ] = "full",
```

`fn()` 開頭加

```python
        if raw_manifest not in MANIFEST_MODES:
            raise ValidationFailed(
                f"--raw-manifest must be one of {MANIFEST_MODES}, got {raw_manifest!r}"
            )
```

（檔頭加 `from vcp.core.hashing import MANIFEST_MODES`）。`ImportSpec(...)` 加 `raw_manifest=raw_manifest,`（型別在 pydantic 端是 Literal，前面的檢查保證合法）。狀態行改為

```python
        status: Status = (
            "WARN" if res.rows_skipped or res.plans_invalidated or res.exif_rotated else "OK"
        )
```

並在 `if res.unlabeled:` 區塊之後加

```python
        if res.exif_rotated:
            fields["exif_rotated"] = res.exif_rotated
```

Run: `uv run pytest tests/unit/test_cli.py -q`
Expected: PASS。

- [ ] **Step 11: 全套與 ruff，然後 commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 全綠（之前 200 passed + 4 新測試）。

```bash
git add pyproject.toml uv.lock src/vcp/data/schema.py src/vcp/core/hashing.py src/vcp/data/importers/base.py src/vcp/cli.py tests/unit/data/test_schema.py tests/unit/core/test_hashing.py tests/unit/data/importers/test_jsonl.py tests/unit/test_cli.py
git commit -m "feat(data): card 新增 exif_policy 與 raw_manifest_mode、dir_manifest sizes 模式、import --raw-manifest；dev 群組加 pydicom/pylibjpeg

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: EXIF 方向政策落地：匯入器記錄與計數、匯出 manifest（spec §15.1-2 到 -4）

**Files:**
- Modify: `src/vcp/data/importers/common.py:10,31-44`（`image_header`、`make_view`、新 helper）
- Modify: `src/vcp/data/importers/csv_boxes.py:33-37,79-127`、`coco.py:39-142`、`yolo.py:70-102`、`imagefolder.py:15-46`、`image_csv.py:36-90`、`jsonl.py:23-50`
- Modify: `src/vcp/data/exporters/base.py:41-59,98-135`、`coco.py:46-53`、`yolo.py:37-113`
- Modify: `src/vcp/cli.py:346-360`（export 欄位）
- Modify: `tests/helpers.py`（`write_exif_image`）
- Test: `tests/unit/data/importers/test_common.py`、`tests/unit/data/importers/test_imagefolder_csv.py`、`tests/unit/data/exporters/test_exporters.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 1 的 `finalize_import(..., exif_policy, exif_rotated)`、`DatasetCard.exif_policy`。
- Produces:
  - `common.image_header(path) -> tuple[int, int, int | None]`（width, height, orientation；1 或缺 → None）
  - `common.make_view(root, rel, *, exif_policy: str = "stored") -> View`
  - `common.choice_option(opts, key, allowed, default) -> str`、`common.exif_policy_option(opts) -> str`、`common.count_exif_rotated(samples) -> int`、`common.EXIF_POLICIES`
  - `exporters.base.ExportOutput(files, warnings=[], fields={}, manifest={})`（dataclass）；`Exporter.run(...) -> ExportOutput`；`ExportResult.fields: dict[str, FieldValue]`
  - 匯出 `manifest.json` 多 `exif_policy`、`exif_rotated`；`ExportResult.fields["exif_rotated"]` 與 warning（>0 時）
  - `helpers.write_exif_image(path, *, size=(8, 4), orientation=6)`

- [ ] **Step 1: 夾具 helper**

`tests/helpers.py` 末尾加：

```python
def write_exif_image(path: Path, *, size: tuple[int, int] = (8, 4), orientation: int = 6) -> None:
    """A JPEG whose stored pixels are ``size`` and whose EXIF Orientation tag is ``orientation``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[0x0112] = orientation
    Image.new("RGB", size, (10, 20, 30)).save(path, format="JPEG", exif=exif.tobytes())
```

- [ ] **Step 2: 寫 `make_view` 的失敗測試**

`tests/unit/data/importers/test_common.py` 加（補 import：`pytest`、`from PIL import Image`、`from helpers import write_exif_image`、`from vcp.core.errors import ValidationFailed`、`from vcp.data.importers.common import choice_option, exif_policy_option, image_header, make_view`）：

```python
def test_make_view_records_exif_orientation_and_honours_policy(tmp_path):
    write_exif_image(tmp_path / "o.jpg", size=(8, 4), orientation=6)
    Image.new("RGB", (8, 4)).save(tmp_path / "plain.png")
    assert image_header(tmp_path / "o.jpg") == (8, 4, 6)
    assert image_header(tmp_path / "plain.png") == (8, 4, None)
    stored = make_view(tmp_path, "o.jpg")
    assert (stored.width, stored.height, stored.meta) == (8, 4, {"exif_orientation": 6})
    oriented = make_view(tmp_path, "o.jpg", exif_policy="oriented")
    assert (oriented.width, oriented.height, oriented.meta["exif_orientation"]) == (4, 8, 6)
    assert make_view(tmp_path, "plain.png", exif_policy="oriented").meta == {}


def test_option_helpers():
    assert exif_policy_option({}) == "stored"
    assert exif_policy_option({"exif": "oriented"}) == "oriented"
    with pytest.raises(ValidationFailed, match="exif="):
        exif_policy_option({"exif": "rotated"})
    assert choice_option({"m": "b"}, "m", ("a", "b"), "a") == "b"
    with pytest.raises(ValidationFailed, match="--opt m="):
        choice_option({"m": "z"}, "m", ("a", "b"), "a")
```

Run: `uv run pytest tests/unit/data/importers/test_common.py -q`
Expected: FAIL（ImportError：`image_header` 等不存在）。

- [ ] **Step 3: 實作 common helpers**

`src/vcp/data/importers/common.py`：import 改為 `from collections.abc import Iterable` 加 `from typing import Any`，schema import 改 `from vcp.data.schema import Category, Sample, View`。把 `image_size` / `make_view` 換成：

```python
EXIF_ORIENTATION_TAG = 0x0112
SWAPPED_ORIENTATIONS = frozenset({5, 6, 7, 8})
EXIF_POLICIES = ("stored", "oriented")


def choice_option(opts: dict[str, str], key: str, allowed: tuple[str, ...], default: str) -> str:
    """``--opt key=value`` restricted to ``allowed``; a bad value is the user's problem (FAIL)."""
    value = opts.get(key, default)
    if value not in allowed:
        raise ValidationFailed(f"--opt {key}= must be one of {allowed}, got {value!r}")
    return value


def exif_policy_option(opts: dict[str, str]) -> str:
    return choice_option(opts, "exif", EXIF_POLICIES, "stored")


def image_header(path: Path) -> tuple[int, int, int | None]:
    """(width, height, exif_orientation) from the file header; pixels are never decoded.
    Orientation is None when the tag is absent or 1 (normal)."""
    try:
        with Image.open(path) as im:
            width, height = im.size
            orientation = im.getexif().get(EXIF_ORIENTATION_TAG)
    except FileNotFoundError:
        raise ValidationFailed(f"image not found: {path}") from None
    except UnidentifiedImageError:
        raise ValidationFailed(f"not a readable image: {path}") from None
    if not isinstance(orientation, int) or orientation == 1:
        orientation = None
    return width, height, orientation


def image_size(path: Path) -> tuple[int, int]:
    width, height, _ = image_header(path)
    return width, height


def make_view(root: Path, rel: str, *, exif_policy: str = "stored") -> View:
    """View with header size. Under ``oriented`` the size is the EXIF-transposed one; the raw
    orientation tag is always recorded in ``meta`` so audits and exporters can warn."""
    width, height, orientation = image_header(root / rel)
    meta: dict[str, Any] = {}
    if orientation is not None:
        meta["exif_orientation"] = orientation
        if exif_policy == "oriented" and orientation in SWAPPED_ORIENTATIONS:
            width, height = height, width
    return View(path=rel, width=width, height=height, meta=meta)


def count_exif_rotated(samples: Iterable[Sample]) -> int:
    return sum(1 for s in samples for v in s.views if "exif_orientation" in v.meta)
```

`csv_boxes.py`：刪掉本地 `_choice`，改 `from vcp.data.importers.common import choice_option as _choice`（保留名稱，其餘呼叫不動）。

Run: `uv run pytest tests/unit/data/importers -q`
Expected: PASS。

- [ ] **Step 4: 寫匯入器端到端的失敗測試**

`tests/unit/data/importers/test_imagefolder_csv.py` 加（補 `from helpers import write_exif_image`）：

```python
def test_image_csv_exif_policy(roots, tmp_path):
    src = tmp_path / "src"
    write_exif_image(src / "images" / "a.jpg", size=(8, 4), orientation=6)
    _img(src / "images" / "b.jpg")
    (src / "labels.csv").write_text("path,label\na.jpg,0\nb.jpg,1\n", encoding="utf-8")
    res = get_importer("image_csv").run(_spec(roots, "image_csv", src, task="cls"))
    a = res.dataset.by_id["a.jpg"].views[0]
    assert (a.width, a.height, a.meta["exif_orientation"]) == (8, 4, 6)
    assert res.exif_rotated == 1 and res.dataset.card.exif_policy == "stored"
    res2 = get_importer("image_csv").run(
        _spec(roots, "image_csv", src, task="cls", exif="oriented")
    )
    a2 = res2.dataset.by_id["a.jpg"].views[0]
    assert (a2.width, a2.height) == (4, 8) and res2.dataset.card.exif_policy == "oriented"
    with pytest.raises(ValidationFailed, match="exif="):
        get_importer("image_csv").run(_spec(roots, "image_csv", src, task="cls", exif="x"))
```

`tests/unit/test_cli.py` 加：

```python
def test_import_warns_on_exif_rotated_views(roots, tmp_path):
    src = tmp_path / "src"
    write_exif_image(src / "images" / "a.jpg", orientation=6)
    (src / "labels.csv").write_text("path,label\na.jpg,0\n", encoding="utf-8")
    r = runner.invoke(
        app,
        [
            "data", "import", "--importer", "image_csv", "--src", str(src), "--name", "ex",
            "--license", "CC0", "--url", "u", "--downloaded-at", "2026-09-03", "--opt", "task=cls",
        ],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "exif_rotated=1" in v
```

（`from helpers import ...` 行加 `write_exif_image`。）

Run: `uv run pytest tests/unit/data/importers/test_imagefolder_csv.py tests/unit/test_cli.py -k exif -q`
Expected: FAIL（`res.exif_rotated == 0`、VERDICT 是 OK）。

- [ ] **Step 5: 六個匯入器傳遞政策與計數**

每個匯入器讀 `exif_policy = exif_policy_option(opts)`（`opts = spec.options`），把 `make_view(..., exif_policy=exif_policy)`，並在 `finalize_import(...)` 加 `exif_policy=exif_policy, exif_rotated=count_exif_rotated(samples)`：

- `csv_boxes.py`：`views = {rel: make_view(images_dir, rel, exif_policy=exif_policy) for rel in (rel_posix(p, images_dir) for p in iter_images(images_dir))}`；import 加 `count_exif_rotated, exif_policy_option`。
- `yolo.py`、`imagefolder.py`、`image_csv.py`：同樣把 `make_view(...)` 加關鍵字、`finalize_import` 加兩個參數。
- `coco.py`：`_load_views(images, images_dir, exif_policy)`；迴圈內改為

```python
        width, height, orientation = image_header(path)
        from_json = "width" in im and "height" in im
        if from_json:
            width, height = int(im["width"]), int(im["height"])
        meta: dict[str, Any] = {}
        if orientation is not None:
            meta["exif_orientation"] = orientation
            if exif_policy == "oriented" and not from_json and orientation in SWAPPED_ORIENTATIONS:
                width, height = height, width
        views[image_id] = (rel, View(path=rel, width=width, height=height, meta=meta))
```

（JSON 給的尺寸是標註空間的權威，不因政策對調；import 改為 `from vcp.data.importers.common import SWAPPED_ORIENTATIONS, count_exif_rotated, exif_policy_option, image_header`。）
- `jsonl.py`：只加 `exif_policy=exif_policy_option(spec.options)`，不掃影像，`exif_rotated=count_exif_rotated(samples)`（手寫 jsonl 也可能帶 meta）。

Run: `uv run pytest tests/unit -q`
Expected: PASS（含 Step 4 兩個測試）。

- [ ] **Step 6: 寫匯出 manifest 的失敗測試**

`tests/unit/data/exporters/test_exporters.py` 加（`from helpers import ...` 加 `write_exif_image`）：

```python
def test_export_manifest_records_exif(roots, tmp_path):
    paths = DatasetPaths.resolve("ex", data_root=roots.data, configs_root=roots.configs)
    raw = roots.data / "raw" / "ex"
    write_exif_image(raw / "a.jpg", size=(8, 4), orientation=6)
    Image.new("RGB", (8, 8)).save(raw / "b.jpg")
    samples = [
        Sample(
            sample_id="a.jpg",
            views=[View(path="a.jpg", width=8, height=4, meta={"exif_orientation": 6})],
            labels=Labels(boxes=[]),
            label_source="gold",
        ),
        Sample(
            sample_id="b.jpg",
            views=[View(path="b.jpg", width=8, height=8)],
            labels=Labels(boxes=[]),
            label_source="gold",
        ),
    ]
    ds = Dataset.from_parts(make_card("det", name="ex", image_root="raw/ex"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    res = export_subset(
        ExportSpec(name="ex", plan_id="p", subset="train", format="coco", out=tmp_path / "o",
                   data_root=roots.data, configs_root=roots.configs)
    )
    manifest = json.loads((tmp_path / "o" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["exif_policy"] == "stored" and manifest["exif_rotated"] == 1
    assert res.fields["exif_rotated"] == 1
    assert any("EXIF" in w for w in res.warnings)
```

Run: `uv run pytest tests/unit/data/exporters/test_exporters.py -k records_exif -q`
Expected: FAIL（`KeyError: 'exif_policy'`）。

- [ ] **Step 7: `ExportOutput` 與 manifest 欄位**

`src/vcp/data/exporters/base.py`：

```python
from dataclasses import dataclass, field as dc_field
from vcp.core.log import FieldValue
from vcp.data.importers.common import count_exif_rotated, rel_posix


@dataclass(frozen=True)
class ExportOutput:
    """What an exporter hands back: written files, human warnings, extra VERDICT fields and
    extra manifest entries (e.g. YOLO's class-index map)."""

    files: list[Path]
    warnings: list[str] = dc_field(default_factory=list)
    fields: dict[str, FieldValue] = dc_field(default_factory=dict)
    manifest: dict[str, object] = dc_field(default_factory=dict)


class ExportResult(BaseModel):
    out: Path
    manifest_path: Path
    files: int
    warnings: list[str]
    fields: dict[str, FieldValue] = Field(default_factory=dict)
```

`Exporter.run` 的回傳型別改 `-> ExportOutput`。`export_subset` 中：

```python
    output = exporter.run(dataset, samples, out, paths.resolve_image_root(dataset.card), spec.options)
    files, warnings, fields = list(output.files), list(output.warnings), dict(output.fields)
    if not samples:
        warnings.append("subset is empty")
    rotated = count_exif_rotated(samples)
    if rotated:
        fields["exif_rotated"] = rotated
        warnings.append(
            f"{rotated} views carry an EXIF orientation != 1 (policy {dataset.card.exif_policy}); "
            "verify the label space before training"
        )
    manifest = {
        ...(既有鍵不變)...
        "exif_policy": dataset.card.exif_policy,
        "exif_rotated": rotated,
        **output.manifest,
        "files": {rel_posix(f, out): sha256_file(f) for f in sorted(files)},
    }
    ...
    return ExportResult(out=out, manifest_path=manifest_path, files=len(files), warnings=warnings, fields=fields)
```

`coco.py` 的 `run` 結尾 `return files, warnings` 改 `return ExportOutput(files, warnings)`（型別註記與 import 同步）；`yolo.py` 同樣改 `return ExportOutput(files, warnings)`（Task 7 再填 `fields` / `manifest`）。`exporters/__init__.py` 匯出 `ExportOutput`。

`src/vcp/cli.py` `export_cmd.fn`：在 `if res.warnings:` 之前加 `fields.update(res.fields)`。

Run: `uv run pytest tests/unit -q && uv run ruff check .`
Expected: PASS、ruff 乾淨。

- [ ] **Step 8: Commit**

```bash
git add src/vcp/data/importers src/vcp/data/exporters src/vcp/cli.py tests/helpers.py tests/unit/data/importers/test_common.py tests/unit/data/importers/test_imagefolder_csv.py tests/unit/data/exporters/test_exporters.py tests/unit/test_cli.py
git commit -m "feat(data): EXIF 方向政策——匯入器記錄 exif_orientation 並依政策對調尺寸、exif_rotated 計數與 WARN、匯出 manifest 記錄政策；ExportOutput 取代 tuple 回傳

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: coords 稽核重定義（spec §15.2）

**Files:**
- Modify: `src/vcp/data/audit/base.py:19-26`（`AuditOptions`）
- Rewrite: `src/vcp/data/audit/coords.py`
- Modify: `src/vcp/cli.py:365-418`（audit 選項）
- Modify: `tests/unit/data/audit/test_checks.py:61-97`（改寫既有 coords 測試）、`tests/integration/test_marine_debris.py:31-38`
- Test: `tests/unit/data/audit/test_checks.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `AuditContext`、`CheckResult`、`write_jsonl`、`common.image_size`、`cache/import_skipped.jsonl`（csv_boxes 寫 `{"line","reason","row"}`、coco 寫 `{"annotation","reason"}`，Task 5 的 dicom 寫 `{"file"|"id","reason"}`）。
- Produces:
  - `AuditOptions.min_box_px: float = 2.0`、`max_aspect: float = 20.0`、`max_cover: float = 0.98`
  - `coords.suspicious_problems(box, width, height, opts) -> list[str]`、`coords.read_import_skipped(path) -> list[dict]`；`box_problems` / `polygon_problems` 不變
  - `coords_bad.jsonl` 每列帶 `kind ∈ {suspicious, out_of_bounds, unsized, import_skipped}`
  - `CheckResult.fields`：`suspicious`、`out_of_bounds`、`import_skipped`、`unsized`、`max_bad`、`report`；狀態：`out_of_bounds + import_skipped > max_bad` → FAIL；否則有 `suspicious` 或 `unsized` → WARN；否則 OK
  - CLI：`--min-box-px`、`--max-aspect`、`--max-cover`

- [ ] **Step 1: 改寫 coords 測試為新語意**

`tests/unit/data/audit/test_checks.py`：把 `test_coords_check_reads_sizes_and_fails_over_limit` 整個換成下面三個測試（import 加 `from vcp.data.audit.coords import box_problems, polygon_problems, read_import_skipped, suspicious_problems`）：

```python
def test_suspicious_problems_thresholds():
    opts = AuditOptions()
    ok = Box(x=1, y=1, w=5, h=5, category_id=0)
    assert suspicious_problems(ok, 32, 32, opts) == []
    assert suspicious_problems(Box(x=1, y=1, w=1, h=5, category_id=0), 32, 32, opts) == ["tiny"]
    assert suspicious_problems(Box(x=0, y=0, w=30, h=1, category_id=0), 64, 64, opts) == ["tiny", "aspect"]
    assert suspicious_problems(Box(x=0, y=0, w=32, h=32, category_id=0), 32, 32, opts) == ["cover"]
    loose = AuditOptions(min_box_px=0, max_aspect=100, max_cover=1.01)
    assert suspicious_problems(Box(x=0, y=0, w=32, h=1, category_id=0), 32, 32, loose) == []


def test_coords_check_classifies_kinds(roots):
    img = _gradient(1)
    specs = [
        ("ok.png", img, Labels(boxes=[Box(x=1, y=1, w=5, h=5, category_id=0)]), {"width": 32, "height": 32}),
        ("tiny.png", img, Labels(boxes=[Box(x=1, y=1, w=0, h=5, category_id=0)]), {"width": 32, "height": 32}),
        (
            "dup.png",
            img,
            Labels(boxes=[Box(x=1, y=1, w=5, h=5, category_id=0), Box(x=1, y=1, w=5, h=5, category_id=0)]),
            {"width": 32, "height": 32},
        ),
        ("unsized.png", img, Labels(boxes=[Box(x=0, y=0, w=100, h=100, category_id=1)]), {}),
        ("gone.png", None, Labels(boxes=[Box(x=0, y=0, w=4, h=4, category_id=1)]), {}),
        (
            "poly.png",
            img,
            Labels(boxes=[], masks=[Mask(category_id=2, polygon=[[0, 0, 50, 0, 5, 5]])]),
            {"width": 32, "height": 32},
        ),
    ]
    ds, paths = _dataset(roots, "cc", specs)
    (paths.cache_dir).mkdir(parents=True, exist_ok=True)
    (paths.cache_dir / "import_skipped.jsonl").write_text(
        '{"line": 7, "reason": "box exceeds image bounds 32x32", "row": {}}\n'
        '{"line": 9, "reason": "unknown image \'zz.png\'", "row": {}}\n',
        encoding="utf-8",
        newline="\n",
    )
    ctx = AuditContext(dataset=ds, paths=paths, opts=AuditOptions())
    res = get_check("coords").run(ctx)
    assert res.status == "FAIL"
    assert (res.fields["suspicious"], res.fields["out_of_bounds"]) == (2, 2)
    assert (res.fields["import_skipped"], res.fields["unsized"]) == (2, 1)
    rows = [
        json.loads(line)
        for line in (ctx.out_dir / "coords_bad.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = {(r.get("sample_id"), r["kind"]) for r in rows}
    assert kinds == {
        ("tiny.png", "suspicious"),
        ("dup.png", "suspicious"),
        ("unsized.png", "out_of_bounds"),
        ("gone.png", "unsized"),
        ("poly.png", "out_of_bounds"),
        (None, "import_skipped"),
    }
    assert [r["reason"] for r in rows if r["kind"] == "import_skipped"][0].startswith("box exceeds")
    # raising the budget to cover 2 out-of-bounds + 2 refused rows leaves only WARN-level findings
    res2 = get_check("coords").run(
        AuditContext(dataset=ds, paths=paths, opts=AuditOptions(max_bad_boxes=4))
    )
    assert res2.status == "WARN"
    assert not get_check("coords").applies(Dataset.from_parts(make_card("cls"), []))


def test_coords_check_clean_dataset_is_ok(roots):
    specs = [("a.png", _gradient(2), Labels(boxes=[Box(x=2, y=2, w=6, h=6, category_id=0)]), {"width": 32, "height": 32})]
    ds, paths = _dataset(roots, "clean", specs)
    res = get_check("coords").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert res.status == "OK" and res.fields["import_skipped"] == 0
    assert read_import_skipped(paths.cache_dir / "import_skipped.jsonl") == []
```

`_dataset` 對 `arr is None` 的 sample 不寫檔（既有邏輯），所以 `gone.png` 讀不到尺寸 → `unsized`。

Run: `uv run pytest tests/unit/data/audit/test_checks.py -k coords -q`
Expected: FAIL（ImportError：`suspicious_problems` 不存在）。

- [ ] **Step 2: `AuditOptions` 新門檻**

`src/vcp/data/audit/base.py` 的 `AuditOptions` 加：

```python
    min_box_px: float = 2.0
    max_aspect: float = 20.0
    max_cover: float = 0.98
```

- [ ] **Step 3: 改寫 `coords.py`**

```python
"""Coordinate sanity beyond what load-time validation already guarantees (spec §15.2).

Validation rejects out-of-bounds boxes on views that carry a size, so this check reports
(a) legal-but-suspicious boxes on sized views, (b) out-of-bounds boxes / polygon vertices where
validation could not see them (unsized views; polygons are never bounds-checked at load) and
(c) the rows the importer refused, merged from ``cache/import_skipped.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.audit.base import AuditContext, AuditOptions, CheckResult, write_jsonl
from vcp.data.dataset import Dataset
from vcp.data.importers.common import image_size
from vcp.data.schema import Box, Sample
from vcp.data.tasks import get_task

TOLERANCE_PX = 1.0
Row = dict[str, object]


def box_problems(box: Box, width: int, height: int) -> list[str]:
    ...（原樣保留）


def polygon_problems(polygon: list[list[float]], width: int, height: int) -> list[str]:
    ...（原樣保留）


def suspicious_problems(box: Box, width: int, height: int, opts: AuditOptions) -> list[str]:
    """Legal boxes that usually mean a labelling or unit mistake."""
    problems: list[str] = []
    if box.w < opts.min_box_px or box.h < opts.min_box_px:
        problems.append("tiny")
    if box.w > 0 and box.h > 0 and max(box.w / box.h, box.h / box.w) > opts.max_aspect:
        problems.append("aspect")
    if box.w * box.h >= opts.max_cover * width * height:
        problems.append("cover")
    return problems


def read_import_skipped(path: Path) -> list[Row]:
    """Rows refused at import time, tagged ``kind=import_skipped``; [] when the file is absent."""
    if not path.is_file():
        return []
    rows: list[Row] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(f"bad import_skipped row: {e}", location=f"{path}:{lineno}") from e
            rows.append({"kind": "import_skipped", **row})
    return rows


def _view_size(
    sample: Sample, index: int, image_root: Path, cache: dict[int, tuple[int, int] | None]
) -> tuple[int, int] | None:
    """Declared size, header size for unsized views, or None when the file cannot be read."""
    if index not in cache:
        v = sample.views[index]
        if v.width is not None and v.height is not None:
            cache[index] = (v.width, v.height)
        else:
            try:
                cache[index] = image_size(image_root / v.path)
            except ValidationFailed:
                cache[index] = None
    return cache[index]


def _check_boxes(
    s: Sample, image_root: Path, opts: AuditOptions, rows: list[Row], counts: dict[str, int]
) -> None:
    boxes = (s.labels.boxes if s.labels else None) or []
    sizes: dict[int, tuple[int, int] | None] = {}
    seen: dict[tuple[int, int, float, float, float, float], int] = {}
    for i, b in enumerate(boxes):
        key = (b.view, b.category_id, b.x, b.y, b.w, b.h)
        if key in seen:
            counts["suspicious"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "suspicious", "index": i,
                         "problems": [f"duplicate of box {seen[key]}"]})
            continue
        seen[key] = i
        size = _view_size(s, b.view, image_root, sizes)
        if size is None:
            counts["unsized"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "unsized", "index": i, "view": b.view})
            continue
        view = s.views[b.view]
        if view.width is None or view.height is None:  # validation never saw these bounds
            problems = box_problems(b, *size)
            if problems:
                counts["out_of_bounds"] += 1
                rows.append({"sample_id": s.sample_id, "kind": "out_of_bounds", "index": i,
                             "problems": problems})
                continue
        problems = suspicious_problems(b, *size, opts)
        if problems:
            counts["suspicious"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "suspicious", "index": i,
                         "problems": problems})


def _check_polygons(s: Sample, image_root: Path, rows: list[Row], counts: dict[str, int]) -> None:
    masks = (s.labels.masks if s.labels else None) or []
    sizes: dict[int, tuple[int, int] | None] = {}
    for i, m in enumerate(masks):
        if m.polygon is None:
            continue
        size = _view_size(s, m.view, image_root, sizes)
        if size is None:
            counts["unsized"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "unsized", "index": i, "view": m.view})
            continue
        problems = polygon_problems(m.polygon, *size)
        if problems:
            counts["out_of_bounds"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "out_of_bounds", "index": i,
                         "mask": True, "problems": problems})


class CoordsCheck:
    name = "coords"

    def applies(self, dataset: Dataset) -> bool:
        return get_task(dataset.card.task).label_field in ("boxes", "masks")

    def run(self, ctx: AuditContext) -> CheckResult:
        image_root = ctx.paths.resolve_image_root(ctx.dataset.card)
        rows: list[Row] = []
        counts = {"suspicious": 0, "out_of_bounds": 0, "unsized": 0}
        for s in ctx.dataset.samples:
            if s.labels is None:
                continue
            _check_boxes(s, image_root, ctx.opts, rows, counts)
            _check_polygons(s, image_root, rows, counts)
        skipped = read_import_skipped(ctx.paths.cache_dir / "import_skipped.jsonl")
        rows.extend(skipped)
        report = write_jsonl(ctx.out_dir / "coords_bad.jsonl", rows)
        bad = counts["out_of_bounds"] + len(skipped)
        if bad > ctx.opts.max_bad_boxes:
            status = "FAIL"
        elif counts["suspicious"] or counts["unsized"]:
            status = "WARN"
        else:
            status = "OK"
        fields = {
            "suspicious": counts["suspicious"],
            "out_of_bounds": counts["out_of_bounds"],
            "import_skipped": len(skipped),
            "unsized": counts["unsized"],
            "max_bad": ctx.opts.max_bad_boxes,
            "report": str(report),
        }
        human = [
            f"coords: {counts['suspicious']} suspicious, {counts['out_of_bounds']} out of bounds, "
            f"{len(skipped)} rows refused at import, {counts['unsized']} unsized"
        ]
        return CheckResult(status, fields, human)  # type: ignore[arg-type]
```

（`box_problems` / `polygon_problems` 函式體照舊；若 ruff 抱怨 `status` 型別，改用 `Status` 註記：`from vcp.core.log import Status` 並 `status: Status`。）

Run: `uv run pytest tests/unit/data/audit -q`
Expected: PASS（`test_box_and_polygon_problems`、`test_dedup...`、`test_provenance...` 不受影響；若 `test_provenance_and_run_audit` 斷言 `fields["bad"]`，改為 `fields["out_of_bounds"]`）。

- [ ] **Step 4: CLI 門檻選項與測試**

`src/vcp/cli.py` `audit_cmd` 在 `max_bad_boxes` 之後加：

```python
    min_box_px: Annotated[float, typer.Option("--min-box-px", help="boxes thinner than this are suspicious")] = 2.0,
    max_aspect: Annotated[float, typer.Option("--max-aspect", help="max w/h or h/w before suspicious")] = 20.0,
    max_cover: Annotated[float, typer.Option("--max-cover", help="box area / view area that is suspicious")] = 0.98,
```

並傳入 `AuditOptions(..., min_box_px=min_box_px, max_aspect=max_aspect, max_cover=max_cover)`。

`tests/unit/test_cli.py::test_audit_cli` 的第一段之後加：

```python
    r = runner.invoke(app, ["data", "audit", "--name", "tiny", "--min-box-px", "100"])
    assert r.exit_code == 0 and "VERDICT cmd=audit.coords status=WARN" in r.output
    assert "suspicious=" in r.output
```

（`tiny` 的框是 1 到 4 像素，門檻 100 讓全部變 tiny → coords WARN，整體 WARN、exit 0。）

Run: `uv run pytest tests/unit/test_cli.py -k audit -q`
Expected: PASS。

- [ ] **Step 5: 海廢整合測試改斷言被擋列數**

`tests/integration/test_marine_debris.py` 的 `test_marine_coords_audit_runs` 換成：

```python
def test_marine_coords_audit_reports_refused_rows(marine, real_roots):
    paths = DatasetPaths.resolve(
        "marine-debris", data_root=real_roots.data, configs_root=real_roots.configs
    )
    skipped_file = paths.cache_dir / "import_skipped.jsonl"
    expected = (
        len(skipped_file.read_text(encoding="utf-8").splitlines()) if skipped_file.is_file() else 0
    )
    ctx = AuditContext(dataset=marine, paths=paths, opts=AuditOptions(max_bad_boxes=expected))
    res = get_check("coords").run(ctx)
    assert res.fields["import_skipped"] == expected
    assert res.fields["out_of_bounds"] == 0  # sized det dataset: validation already blocked them
    assert res.status in ("OK", "WARN")
```

Run: `uv run pytest tests/integration -q`
Expected: 3 skipped（本機無海廢資料）。

- [ ] **Step 6: 全套、ruff、commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add src/vcp/data/audit/base.py src/vcp/data/audit/coords.py src/vcp/cli.py tests/unit/data/audit/test_checks.py tests/unit/test_cli.py tests/integration/test_marine_debris.py
git commit -m "feat(audit): coords 改報可疑框、無尺寸 view 越界與匯入被擋列；新增 --min-box-px/--max-aspect/--max-cover

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `dicomio`、解碼器登記表、合成 DICOM 夾具（spec §15.3-9/-11/-12 排序、§15.4-17/-18、§15.6-26）

**Files:**
- Create: `src/vcp/data/dicomio.py`
- Create: `src/vcp/data/materialize/__init__.py`（本任務先只匯出 decoders；Task 6 補執行器）、`src/vcp/data/materialize/decoders/__init__.py`、`decoders/base.py`、`decoders/image.py`、`decoders/dicom.py`、`src/vcp/data/materialize/window.py`
- Modify: `tests/helpers.py`（`write_dicom_study`）
- Test: `tests/unit/data/test_dicomio.py`、`tests/unit/data/materialize/__init__.py`（空）、`tests/unit/data/materialize/test_decoders.py`

**Interfaces:**
- Consumes: pydicom 3（`dcmread(stop_before_pixels=True)`、`pixel_array`、`compress(RLELossless)`、`save_as(enforce_file_format=True)`，皆已在本機驗證）；Task 2 的 `write_exif_image`。
- Produces:
  - `dicomio.require_pydicom() -> module`（缺 → `VcpError(INSTALL_HINT)`）、`dicomio.INSTALL_HINT`
  - `dicomio.SliceHeader(path, study_uid, series_uid, sop_uid, rows, columns, instance_number, position, orientation, tags)`（frozen dataclass；`tags: dict[str, Any]` 含請求的 keyword 與 `TransferSyntaxUID`）
  - `dicomio.read_header(path, *, extra=()) -> SliceHeader | str`（字串為跳過原因：`not_dicom`、`missing_tag:<kw>`、`multiframe`）
  - `dicomio.read_headers(paths, *, extra=(), workers=4) -> list[SliceHeader | str]`（保序）
  - `dicomio.sort_slices(headers) -> list[SliceHeader]`、`dicomio.jsonable(value)`
  - `decoders.Decoded(array: np.ndarray, info: dict[str, Any])`、`decoders.Decoder` 協定（`name`、`version`、`decode(path, *, exif_policy="stored") -> Decoded`、`decode_series(paths) -> Decoded`）、`DECODERS`、`register_decoder`、`get_decoder(name)`、`decoder_for(path, override=None)`
  - `window.WINDOW_MODES = ("dicom", "minmax", "percentile")`、`window.to_uint8(decoded, mode) -> np.ndarray`、`window.resize_long_side(arr_uint8, long_side) -> np.ndarray`
  - `helpers.write_dicom_study(root, *, study_uid, patient_id, series, slices, size, missing_instance_number, compress, descriptions) -> list[Path]`

- [ ] **Step 1: 夾具 helper**

`tests/helpers.py` 檔頭加 `import numpy as np`；末尾加：

```python
def write_dicom_study(
    root: Path,
    *,
    study_uid: str = "1.2.826.0.1.3680043.8.498.1",
    patient_id: str = "P1",
    series: int = 2,
    slices: int = 3,
    size: tuple[int, int] = (16, 16),
    missing_instance_number: bool = False,
    compress: str | None = None,
    descriptions: tuple[str, ...] = ("sag_t2", "cor_pd", "ax_t1"),
) -> list[Path]:
    """Synthetic MR study at ``<root>/<study>/<series>/<sop>.dcm``; returns the files written.

    InstanceNumber runs *backwards* relative to file name and slice position on purpose, so a
    consumer that sorts correctly yields pixel values [.., +20, +10, +0]. Pixel value of slice k in
    series s is ``100 * (s + 1) + 10 * k`` everywhere.
    """
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, RLELossless

    written: list[Path] = []
    for si in range(series):
        series_uid = f"{study_uid}.{si + 1}"
        for k in range(slices):
            sop = f"{series_uid}.{k + 1}"
            ds = Dataset()
            ds.file_meta = FileMetaDataset()
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            ds.file_meta.MediaStorageSOPClassUID = MRImageStorage
            ds.file_meta.MediaStorageSOPInstanceUID = sop
            ds.SOPClassUID = MRImageStorage
            ds.SOPInstanceUID = sop
            ds.StudyInstanceUID = study_uid
            ds.SeriesInstanceUID = series_uid
            ds.PatientID = patient_id
            ds.Modality = "MR"
            ds.SeriesDescription = descriptions[si % len(descriptions)]
            ds.SeriesNumber = si + 1
            if not missing_instance_number:
                ds.InstanceNumber = slices - k
            ds.ImagePositionPatient = [0.0, 0.0, float(k) * 3.0]
            ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            ds.PixelSpacing = [0.5, 0.5]
            ds.SliceThickness = 3.0
            ds.Rows, ds.Columns = size[1], size[0]
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
            ds.RescaleIntercept, ds.RescaleSlope = 0, 1
            ds.WindowCenter, ds.WindowWidth = 1000, 2000
            arr = np.full((size[1], size[0]), 100 * (si + 1) + 10 * k, dtype="<u2")
            ds.PixelData = arr.tobytes()
            if compress == "rle":
                ds.compress(RLELossless)
            path = root / study_uid / series_uid / f"{sop}.dcm"
            path.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(path, enforce_file_format=True)
            written.append(path)
    return written
```

- [ ] **Step 2: `dicomio` 的失敗測試**

`tests/unit/data/test_dicomio.py`：

```python
from pathlib import Path

import pytest

from helpers import write_dicom_study
from vcp.core.errors import VcpError
from vcp.data import dicomio
from vcp.data.dicomio import SliceHeader, read_header, read_headers, sort_slices


def test_read_header_and_sort_by_instance_number(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3)
    headers = read_headers(files, extra=("SeriesDescription", "PatientID"))
    assert all(isinstance(h, SliceHeader) for h in headers)
    h0 = headers[0]
    assert (h0.rows, h0.columns, h0.instance_number) == (16, 16, 3)
    assert h0.tags["SeriesDescription"] == "sag_t2" and h0.tags["PatientID"] == "P1"
    assert h0.tags["TransferSyntaxUID"] == "1.2.840.10008.1.2.1"
    ordered = sort_slices(headers)
    assert [h.instance_number for h in ordered] == [1, 2, 3]
    assert [h.path.name for h in ordered] == [f.name for f in reversed(files)]


def test_sort_falls_back_to_position_then_name(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3, missing_instance_number=True)
    ordered = sort_slices(read_headers(files))
    assert [h.path.name for h in ordered] == [f.name for f in files]  # z = 0, 3, 6
    bare = [SliceHeader(p, "s", "se", f"sop{i}", 4, 4, None, None, None, {}) for i, p in
            enumerate([Path("b.dcm"), Path("a.dcm")])]
    assert [h.path.name for h in sort_slices(bare)] == ["a.dcm", "b.dcm"]


def test_read_header_skip_reasons(tmp_path):
    (tmp_path / "junk.dcm").write_bytes(b"not a dicom file")
    assert read_header(tmp_path / "junk.dcm") == "not_dicom"
    [f] = write_dicom_study(tmp_path / "s", series=1, slices=1)
    import pydicom

    ds = pydicom.dcmread(f)
    del ds.SeriesInstanceUID
    ds.save_as(tmp_path / "noseries.dcm", enforce_file_format=True)
    assert read_header(tmp_path / "noseries.dcm") == "missing_tag:SeriesInstanceUID"
    ds = pydicom.dcmread(f)
    ds.NumberOfFrames = 2
    ds.save_as(tmp_path / "multi.dcm", enforce_file_format=True)
    assert read_header(tmp_path / "multi.dcm") == "multiframe"


def test_require_pydicom_reports_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pydicom":
            raise ImportError("no pydicom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VcpError, match="uv sync --extra dicom"):
        dicomio.require_pydicom()
```

Run: `uv run pytest tests/unit/data/test_dicomio.py -q`
Expected: FAIL（`ModuleNotFoundError: vcp.data.dicomio`）。

- [ ] **Step 3: 實作 `dicomio.py`**

```python
"""Header-only DICOM access shared by the ``dicom`` importer and the ``dicom`` decoder.

Grouping and ordering come from the header (StudyInstanceUID / SeriesInstanceUID /
InstanceNumber), never from folder names. Pixel data is never read here.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcp.core.errors import VcpError

REQUIRED_TAGS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "Rows", "Columns")
INSTALL_HINT = "DICOM support needs the 'dicom' extra: uv sync --extra dicom"


def require_pydicom() -> Any:
    try:
        import pydicom
    except ImportError as e:
        raise VcpError(INSTALL_HINT) from e
    return pydicom


@dataclass(frozen=True)
class SliceHeader:
    path: Path
    study_uid: str
    series_uid: str
    sop_uid: str
    rows: int
    columns: int
    instance_number: int | None
    position: tuple[float, float, float] | None
    orientation: tuple[float, ...] | None
    tags: dict[str, Any]


def jsonable(value: Any) -> Any:
    """pydicom values (MultiValue, DSfloat, IS, UID, PersonName) -> plain JSON-able Python."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        return [jsonable(v) for v in value]
    return str(value)


def _optional_int(ds: Any, keyword: str) -> int | None:
    value = getattr(ds, keyword, None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _floats(ds: Any, keyword: str, n: int) -> tuple[float, ...] | None:
    value = getattr(ds, keyword, None)
    if value is None or len(value) != n:
        return None
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None


def read_header(path: Path, *, extra: Iterable[str] = ()) -> SliceHeader | str:
    """Parsed header, or a skip reason: ``not_dicom`` / ``missing_tag:<kw>`` / ``multiframe``."""
    pydicom = require_pydicom()
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except (pydicom.errors.InvalidDicomError, ValueError, OSError):
        return "not_dicom"
    for kw in REQUIRED_TAGS:
        if getattr(ds, kw, None) in (None, ""):
            return f"missing_tag:{kw}"
    if (_optional_int(ds, "NumberOfFrames") or 1) > 1:
        return "multiframe"
    tags: dict[str, Any] = {kw: jsonable(getattr(ds, kw)) for kw in extra if kw in ds}
    meta = getattr(ds, "file_meta", None)
    tags["TransferSyntaxUID"] = (
        str(meta.TransferSyntaxUID) if meta is not None and "TransferSyntaxUID" in meta else None
    )
    position = _floats(ds, "ImagePositionPatient", 3)
    return SliceHeader(
        path=path,
        study_uid=str(ds.StudyInstanceUID),
        series_uid=str(ds.SeriesInstanceUID),
        sop_uid=str(ds.SOPInstanceUID),
        rows=int(ds.Rows),
        columns=int(ds.Columns),
        instance_number=_optional_int(ds, "InstanceNumber"),
        position=position,  # type: ignore[arg-type]
        orientation=_floats(ds, "ImageOrientationPatient", 6),
        tags=tags,
    )


def read_headers(
    paths: list[Path], *, extra: Iterable[str] = (), workers: int = 4
) -> list[SliceHeader | str]:
    wanted = tuple(extra)
    if workers <= 1 or len(paths) < 2:
        return [read_header(p, extra=wanted) for p in paths]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda p: read_header(p, extra=wanted), paths))


def _normal(orientation: tuple[float, ...]) -> tuple[float, float, float]:
    r, c = orientation[:3], orientation[3:]
    return (r[1] * c[2] - r[2] * c[1], r[2] * c[0] - r[0] * c[2], r[0] * c[1] - r[1] * c[0])


def sort_slices(headers: list[SliceHeader]) -> list[SliceHeader]:
    """InstanceNumber when every slice has one; else position projected on the slice normal
    when every slice has one; else file name. Ties always break on file name."""
    if headers and all(h.instance_number is not None for h in headers):
        return sorted(headers, key=lambda h: (h.instance_number, h.path.name))
    if headers and all(h.position is not None for h in headers):

        def projected(h: SliceHeader) -> float:
            normal = _normal(h.orientation) if h.orientation else (0.0, 0.0, 1.0)
            return sum(p * n for p, n in zip(h.position or (), normal, strict=True))

        return sorted(headers, key=lambda h: (projected(h), h.path.name))
    return sorted(headers, key=lambda h: h.path.name)
```

Run: `uv run pytest tests/unit/data/test_dicomio.py -q`
Expected: PASS。

- [ ] **Step 4: 解碼器與 window 的失敗測試**

`tests/unit/data/materialize/__init__.py` 建空檔。`tests/unit/data/materialize/test_decoders.py`：

```python
from pathlib import Path

import numpy as np
import pytest

from helpers import write_dicom_study, write_exif_image
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.materialize.decoders import DECODERS, Decoded, decoder_for, get_decoder
from vcp.data.materialize.window import resize_long_side, to_uint8


def test_registry_and_dispatch():
    assert set(DECODERS) == {"image", "dicom"}
    assert decoder_for(Path("x.dcm")).name == "dicom" and decoder_for(Path("x.DCM")).name == "dicom"
    assert decoder_for(Path("x.png")).name == "image"
    assert decoder_for(Path("x.png"), override="dicom").name == "dicom"
    with pytest.raises(RegistryError):
        get_decoder("nifti")


def test_dicom_decoder_rescale_and_series_order(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3)
    dec = get_decoder("dicom")
    d = dec.decode(files[0])
    assert d.array.dtype == np.uint16 and d.array.shape == (16, 16) and int(d.array[0, 0]) == 100
    assert d.info["window_center"] == 1000.0 and d.info["photometric"] == "MONOCHROME2"
    vol = dec.decode_series(files)
    assert vol.array.shape == (3, 16, 16)
    assert [int(v) for v in vol.array[:, 0, 0]] == [120, 110, 100]  # InstanceNumber order
    assert vol.info["slices"] == 3


def test_dicom_decoder_rle_and_position_fallback(tmp_path):
    files = write_dicom_study(
        tmp_path, series=1, slices=2, compress="rle", missing_instance_number=True
    )
    vol = get_decoder("dicom").decode_series(files)
    assert [int(v) for v in vol.array[:, 0, 0]] == [100, 110]  # position order
    assert vol.info["transfer_syntax"] == "1.2.840.10008.1.2.5"


def test_dicom_decoder_signed_and_float_rescale(tmp_path):
    import pydicom

    [f] = write_dicom_study(tmp_path, series=1, slices=1)
    ds = pydicom.dcmread(f)
    ds.RescaleIntercept = -1024
    ds.save_as(tmp_path / "signed.dcm", enforce_file_format=True)
    d = get_decoder("dicom").decode(tmp_path / "signed.dcm")
    assert d.array.dtype == np.int16 and int(d.array[0, 0]) == 100 - 1024
    ds.RescaleSlope = 0.5
    ds.save_as(tmp_path / "float.dcm", enforce_file_format=True)
    assert get_decoder("dicom").decode(tmp_path / "float.dcm").array.dtype == np.float32


def test_image_decoder_exif_policy(tmp_path):
    write_exif_image(tmp_path / "o.jpg", size=(8, 4), orientation=6)
    dec = get_decoder("image")
    assert dec.decode(tmp_path / "o.jpg").array.shape == (4, 8, 3)
    assert dec.decode(tmp_path / "o.jpg", exif_policy="oriented").array.shape == (8, 4, 3)
    stack = dec.decode_series([tmp_path / "o.jpg", tmp_path / "o.jpg"])
    assert stack.array.shape == (2, 4, 8, 3)


def test_to_uint8_modes_and_resize():
    arr = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    d = Decoded(arr, {"window_center": 1000.0, "window_width": 2000.0, "photometric": "MONOCHROME2"})
    assert to_uint8(d, "dicom").tolist() == [[0, 128], [255, 255]]
    assert to_uint8(d, "minmax").tolist() == [[0, 64], [128, 255]]
    inverted = to_uint8(Decoded(arr, {"photometric": "MONOCHROME1"}), "dicom")  # no window -> minmax
    assert inverted.tolist() == [[255, 191], [127, 0]]
    assert to_uint8(Decoded(np.zeros((2, 2), np.uint8), {}), "percentile").dtype == np.uint8
    rgb = np.zeros((4, 8, 3), np.uint8)
    assert to_uint8(Decoded(rgb, {}), "minmax") is rgb  # already 8-bit: untouched
    with pytest.raises(ValidationFailed, match="window"):
        to_uint8(d, "gamma")
    assert resize_long_side(rgb, 4).shape == (2, 4, 3)
    assert resize_long_side(np.zeros((16, 16), np.uint8), 8).shape == (8, 8)
```

Run: `uv run pytest tests/unit/data/materialize/test_decoders.py -q`
Expected: FAIL（`ModuleNotFoundError: vcp.data.materialize`）。

- [ ] **Step 5: 實作解碼器套件**

`src/vcp/data/materialize/__init__.py`（本任務版本）：

```python
"""Materialize: decode views once into a portable npy / png cache (spec §6.3, §15.4)."""

from vcp.data.materialize.decoders import DECODERS, Decoded, Decoder, decoder_for, get_decoder

__all__ = ["DECODERS", "Decoded", "Decoder", "decoder_for", "get_decoder"]
```

`decoders/base.py`：

```python
"""Decoder contract and registry. One decoder per file family, chosen by suffix."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from vcp.core.errors import RegistryError


@dataclass(frozen=True)
class Decoded:
    """Decoded pixels plus what a consumer needs to display or window them."""

    array: np.ndarray
    info: dict[str, Any] = field(default_factory=dict)


class Decoder(Protocol):
    name: str
    version: str

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded: ...

    def decode_series(self, paths: list[Path]) -> Decoded: ...


DECODERS: dict[str, Decoder] = {}
_BY_SUFFIX: dict[str, str] = {".dcm": "dicom"}
DEFAULT_DECODER = "image"


def register_decoder(decoder: Decoder) -> None:
    if decoder.name in DECODERS:
        raise RegistryError(f"decoder {decoder.name!r} already registered")
    DECODERS[decoder.name] = decoder


def get_decoder(name: str) -> Decoder:
    try:
        return DECODERS[name]
    except KeyError:
        raise RegistryError(f"unknown decoder {name!r}; known: {sorted(DECODERS)}") from None


def decoder_for(path: Path, override: str | None = None) -> Decoder:
    """Registry lookup by suffix (``.dcm`` -> dicom, else image) unless ``override`` names one."""
    if override:
        return get_decoder(override)
    return get_decoder(_BY_SUFFIX.get(path.suffix.lower(), DEFAULT_DECODER))
```

`decoders/image.py`：

```python
"""Pillow-backed decoder for ordinary images (JPEG / PNG / TIFF / ...)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from vcp.data.materialize.decoders.base import Decoded


class ImageDecoder:
    name = "image"
    version = "1"

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im) if exif_policy == "oriented" else im
            if img.mode not in ("L", "RGB", "I;16"):
                img = img.convert("RGB")
            return Decoded(np.asarray(img).copy(), {"mode": img.mode})

    def decode_series(self, paths: list[Path]) -> Decoded:
        frames = [self.decode(p) for p in paths]
        return Decoded(np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)})
```

`decoders/dicom.py`：

```python
"""pydicom-backed decoder: rescaled pixels (int16/uint16 when exact) and window/photometric info."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.dicomio import SliceHeader, read_header, require_pydicom, sort_slices
from vcp.data.materialize.decoders.base import Decoded


def _first_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value[0] if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue" else value)
    except (TypeError, ValueError, IndexError):
        return None


def rescale(arr: np.ndarray, slope: float, intercept: float, signed: bool) -> np.ndarray:
    """Integer-exact rescale keeps 16-bit ints; anything else becomes float32."""
    if slope.is_integer() and intercept.is_integer():
        out = arr.astype(np.int64) * int(slope) + int(intercept)
        if out.min() >= 0 and out.max() <= np.iinfo(np.uint16).max and not signed:
            return out.astype(np.uint16)
        if out.min() >= np.iinfo(np.int16).min and out.max() <= np.iinfo(np.int16).max:
            return out.astype(np.int16)
        return out.astype(np.int32)
    return arr.astype(np.float32) * np.float32(slope) + np.float32(intercept)


class DicomDecoder:
    name = "dicom"
    version = "1"

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded:
        pydicom = require_pydicom()
        ds = pydicom.dcmread(path)
        arr = ds.pixel_array
        slope = _first_float(getattr(ds, "RescaleSlope", None)) or 1.0
        intercept = _first_float(getattr(ds, "RescaleIntercept", None)) or 0.0
        signed = int(getattr(ds, "PixelRepresentation", 0) or 0) == 1 or intercept < 0
        info = {
            "window_center": _first_float(getattr(ds, "WindowCenter", None)),
            "window_width": _first_float(getattr(ds, "WindowWidth", None)),
            "photometric": str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")),
            "transfer_syntax": str(ds.file_meta.TransferSyntaxUID),
            "rescale": [slope, intercept],
        }
        return Decoded(rescale(arr, slope, intercept, signed), info)

    def decode_series(self, paths: list[Path]) -> Decoded:
        headers = [read_header(p) for p in paths]
        bad = [f"{p.name}: {h}" for p, h in zip(paths, headers, strict=True) if isinstance(h, str)]
        if bad:
            raise ValidationFailed(f"series contains unreadable slices: {bad[:3]}")
        ordered = sort_slices([h for h in headers if isinstance(h, SliceHeader)])
        frames = [self.decode(h.path) for h in ordered]
        shapes = {f.array.shape for f in frames}
        if len(shapes) != 1:
            raise ValidationFailed(f"series slices differ in shape: {sorted(shapes)}")
        return Decoded(np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)})
```

`decoders/__init__.py`：

```python
"""Decoder registry. Importing this package registers the built-in decoders."""

from vcp.data.materialize.decoders.base import (
    DECODERS,
    Decoded,
    Decoder,
    decoder_for,
    get_decoder,
    register_decoder,
)
from vcp.data.materialize.decoders.dicom import DicomDecoder
from vcp.data.materialize.decoders.image import ImageDecoder

register_decoder(ImageDecoder())
register_decoder(DicomDecoder())

__all__ = ["DECODERS", "Decoded", "Decoder", "DicomDecoder", "ImageDecoder", "decoder_for",
           "get_decoder", "register_decoder"]
```

`src/vcp/data/materialize/window.py`：

```python
"""8-bit mapping for png output and long-side resizing."""

from __future__ import annotations

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.materialize.decoders.base import Decoded

WINDOW_MODES = ("dicom", "minmax", "percentile")


def _bounds(decoded: Decoded, mode: str) -> tuple[float, float]:
    arr = decoded.array
    if mode == "dicom":
        c, w = decoded.info.get("window_center"), decoded.info.get("window_width")
        if c is not None and w:
            return c - w / 2, c + w / 2
        mode = "minmax"
    if mode == "percentile":
        lo, hi = np.percentile(arr, [0.5, 99.5])
        return float(lo), float(hi)
    return float(arr.min()), float(arr.max())


def to_uint8(decoded: Decoded, mode: str) -> np.ndarray:
    """Map any decoded array to uint8; uint8 input is returned untouched. MONOCHROME1 inverts."""
    if mode not in WINDOW_MODES:
        raise ValidationFailed(f"window mode must be one of {WINDOW_MODES}, got {mode!r}")
    arr = decoded.array
    if arr.dtype == np.uint8:
        return arr
    lo, hi = _bounds(decoded, mode)
    scaled = np.clip((arr.astype(np.float64) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    out = np.round(scaled * 255).astype(np.uint8)
    return (255 - out) if decoded.info.get("photometric") == "MONOCHROME1" else out


def resize_long_side(arr: np.ndarray, long_side: int) -> np.ndarray:
    """Proportional resize of a uint8 HW or HWC array so max(H, W) == long_side (LANCZOS)."""
    h, w = arr.shape[:2]
    scale = long_side / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return np.asarray(Image.fromarray(arr).resize(size, Image.LANCZOS))
```

Run: `uv run pytest tests/unit/data/materialize tests/unit/data/test_dicomio.py -q`
Expected: PASS。

- [ ] **Step 6: 全套、ruff、commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add src/vcp/data/dicomio.py src/vcp/data/materialize tests/helpers.py tests/unit/data/test_dicomio.py tests/unit/data/materialize
git commit -m "feat(data): dicomio（header 讀取、切片排序）、解碼器登記表（image、dicom）、window/resize、合成 DICOM 夾具

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `dicom` 匯入器（spec §15.3）

**Files:**
- Create: `src/vcp/data/importers/dicom.py`
- Modify: `src/vcp/data/importers/__init__.py`（登記，順序在 `image_csv` 之後）、`src/vcp/data/importers/base.py`（`ImportResult.extra_fields`、`finalize_import(extra_fields=)`）、`src/vcp/cli.py:154-167`（合併 `extra_fields`）
- Test: `tests/unit/data/importers/test_dicom.py`

**Interfaces:**
- Consumes: Task 4 的 `dicomio.read_headers / sort_slices / SliceHeader`、`helpers.write_dicom_study`；Task 2 的 `choice_option / exif_policy_option / read_csv / rel_posix`；`image_csv.infer_task`。
- Produces:
  - `DicomImporter`（`name="dicom"`, `version="1"`）與選項 `sample_level`、`view_level`、`glob`、`workers`、`tags`、`group_from`、`role_from`、`labels_csv`、`id_col`、`target_cols`、`task`、`meta_cols`、`seq_csv`、`seq_id_col`、`seq_cols`、`exif`
  - `ImportResult.extra_fields: dict[str, int] = {}`（CLI 合併進 VERDICT，非空 → WARN）；dicom 用它回報 `inconsistent_series`
  - skipped 列形狀：`{"file": <rel>, "reason": not_dicom|missing_tag:<kw>|multiframe}`、`{"id": <sample>, "reason": partial_targets|no_files}`、`{"id", "series", "reason": "series_split_across_dirs"}`
  - `sample.meta["series"][<SeriesInstanceUID>]` 摘要鍵：`description`、`number`、`n_slices`、`modality`、`rows`、`columns`、`pixel_spacing`、`slice_thickness`、`transfer_syntax`、`--opt tags` 的 keyword、`seq_csv` 欄

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_dicom.py`：

```python
import json

import pytest

from helpers import write_dicom_study
from vcp.core.errors import ValidationFailed, VcpError
from vcp.data import dicomio
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec


def _tree(roots):
    src = roots.data / "raw" / "dcm"
    write_dicom_study(src, study_uid="1.2.1", patient_id="PA", series=2, slices=3)
    write_dicom_study(src, study_uid="1.2.2", patient_id="PB", series=1, slices=2, compress="rle")
    write_dicom_study(src, study_uid="1.2.3", patient_id="PA", series=1, slices=1)
    (src / "labels.csv").write_text(
        "StudyInstanceUID,Report,ACL,MCL\n"
        "1.2.1,torn acl,1,0\n"
        "1.2.2,normal,,\n"
        "1.2.3,partial,1,\n"
        "1.2.9,ghost,0,0\n",
        encoding="utf-8",
    )
    (src / "series.csv").write_text(
        "SeriesInstanceUID,Plane\n1.2.1.1,Sagittal\n1.2.1.2,Coronal\n1.2.2.1,Axial\n",
        encoding="utf-8",
    )
    return src


def _spec(roots, src, name="dcm", **opts):
    return ImportSpec(
        importer="dicom",
        src=src,
        name=name,
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _skipped(res):
    if res.skipped_reasons_path is None:
        return []
    text = res.skipped_reasons_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_dicom_import_study_level_with_labels(roots):
    src = _tree(roots)
    res = get_importer("dicom").run(
        _spec(
            roots, src, labels_csv="labels.csv", target_cols="ACL,MCL", meta_cols="Report",
            seq_csv="series.csv", seq_cols="Plane", role_from="Plane",
        )
    )
    ds = res.dataset
    assert ds.card.task == "multilabel"
    assert [c.name for c in ds.card.categories] == ["ACL", "MCL"]
    assert ds.card.image_root == "raw/dcm"
    assert [s.sample_id for s in ds.samples] == ["1.2.1", "1.2.2"]
    assert (res.rows_read, res.samples_written, res.unlabeled) == (9, 2, 1)
    assert {(r.get("id"), r["reason"]) for r in _skipped(res)} == {
        ("1.2.3", "partial_targets"),
        ("1.2.9", "no_files"),
    }
    a = ds.by_id["1.2.1"]
    assert a.labels.targets == {"ACL": 1.0, "MCL": 0.0} and a.label_source == "gold"
    assert a.group == "PA" and a.meta["Report"] == "torn acl"
    assert len(a.views) == 6
    assert [v.seq_index for v in a.views] == [0, 1, 2, 0, 1, 2]
    assert [v.meta["instance_number"] for v in a.views[:3]] == [1, 2, 3]
    assert a.views[0].path == "1.2.1/1.2.1.1/1.2.1.1.3.dcm"  # InstanceNumber 1 is on-disk slice 3
    assert (a.views[0].width, a.views[0].height, a.views[0].seq_id) == (16, 16, "1.2.1.1")
    assert a.views[0].role == "Sagittal" and a.views[3].role == "Coronal"
    series = a.meta["series"]["1.2.1.1"]
    assert series["n_slices"] == 3 and series["Plane"] == "Sagittal"
    assert series["description"] == "sag_t2" and series["number"] == 1
    assert series["transfer_syntax"] == "1.2.840.10008.1.2.1" and series["pixel_spacing"] == [0.5, 0.5]
    b = ds.by_id["1.2.2"]
    assert b.labels is None and b.label_source == "none" and b.group == "PB"
    assert b.meta["series"]["1.2.2.1"]["transfer_syntax"] == "1.2.840.10008.1.2.5"


def test_dicom_import_series_views_and_series_samples(roots):
    src = _tree(roots)
    res = get_importer("dicom").run(_spec(roots, src, view_level="series", group_from="none"))
    a = res.dataset.by_id["1.2.1"]
    assert len(a.views) == 2 and a.views[0].path == "1.2.1/1.2.1.1"
    assert (a.views[0].width, a.views[0].height) == (16, 16)
    assert a.views[0].seq_id == "1.2.1.1" and a.views[0].seq_index is None
    assert a.views[0].meta["n_slices"] == 3 and a.group is None
    assert res.dataset.card.task == "multilabel" and res.unlabeled == 3
    res2 = get_importer("dicom").run(_spec(roots, src, sample_level="series", task="regression"))
    assert [s.sample_id for s in res2.dataset.samples] == ["1.2.1.1", "1.2.1.2", "1.2.2.1", "1.2.3.1"]
    assert res2.dataset.card.task == "regression" and res2.dataset.card.categories == []


def test_dicom_import_skips_non_dicom_and_validates_options(roots):
    src = _tree(roots)
    (src / "1.2.1" / "notes.dcm").write_bytes(b"hello")
    res = get_importer("dicom").run(_spec(roots, src))
    assert [(r["file"], r["reason"]) for r in _skipped(res)] == [("1.2.1/notes.dcm", "not_dicom")]
    assert res.rows_read == 7
    with pytest.raises(ValidationFailed, match="no files match"):
        get_importer("dicom").run(_spec(roots, src, glob="**/*.ima"))
    with pytest.raises(ValidationFailed, match="target_cols"):
        get_importer("dicom").run(_spec(roots, src, labels_csv="labels.csv"))
    with pytest.raises(ValidationFailed, match="sample_level="):
        get_importer("dicom").run(_spec(roots, src, sample_level="patient"))
    with pytest.raises(ValidationFailed, match="task="):
        get_importer("dicom").run(_spec(roots, src, task="cls"))


def test_dicom_import_without_pydicom_aborts(roots, monkeypatch):
    src = _tree(roots)

    def boom():
        raise VcpError(dicomio.INSTALL_HINT)

    monkeypatch.setattr(dicomio, "require_pydicom", boom)
    with pytest.raises(VcpError, match="uv sync --extra dicom"):
        get_importer("dicom").run(_spec(roots, src))
```

Run: `uv run pytest tests/unit/data/importers/test_dicom.py -q`
Expected: FAIL（`RegistryError: unknown importer 'dicom'`）。

- [ ] **Step 2: `ImportResult.extra_fields`**

`src/vcp/data/importers/base.py`：`ImportResult` 加 `extra_fields: dict[str, int] = Field(default_factory=dict)`；`finalize_import` 簽名加 `extra_fields: dict[str, int] | None = None,` 並在 `ImportResult(...)` 傳 `extra_fields=dict(extra_fields or {})`。

`src/vcp/cli.py` `import_cmd.fn`：狀態行加 `or res.extra_fields`；`if res.exif_rotated:` 區塊之後加 `fields.update(res.extra_fields)`。

- [ ] **Step 3: 實作 `importers/dicom.py`**

```python
"""DICOM directory tree -> one sample per study (or series); one view per slice (or per series).

Grouping and order come from headers (StudyInstanceUID / SeriesInstanceUID / InstanceNumber),
never from folder names. Labels join an optional CSV on the sample id; series attributes join an
optional CSV on SeriesInstanceUID. Nothing here knows any particular contest's column names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.dicomio import SliceHeader, read_headers, sort_slices
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import choice_option, exif_policy_option, read_csv, rel_posix
from vcp.data.importers.image_csv import infer_task
from vcp.data.schema import Category, Labels, Sample, View

SAMPLE_LEVELS = ("study", "series")
VIEW_LEVELS = ("slice", "series")
TASKS = ("multilabel", "regression")
SUMMARY_TAGS = ("SeriesDescription", "SeriesNumber", "Modality", "PixelSpacing", "SliceThickness")
SLICE_TAGS = ("SliceLocation",)
DEFAULT_ID_COLS = {"study": "StudyInstanceUID", "series": "SeriesInstanceUID"}
DEFAULT_GROUP_TAG = "PatientID"
Skipped = list[dict[str, Any]]


def split_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def find_files(src: Path, pattern: str) -> list[Path]:
    """Files matching ``pattern`` under ``src``; a ``.dcm`` pattern matches any suffix case."""
    if pattern.lower().endswith(".dcm"):
        found = (p for p in src.glob(pattern[: -len(".dcm")] + ".*") if p.suffix.lower() == ".dcm")
    else:
        found = src.glob(pattern)
    return sorted((p for p in found if p.is_file()), key=lambda p: rel_posix(p, src))


def _csv_path(src: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else src / path


@dataclass(frozen=True)
class LabelTable:
    rows: dict[str, dict[str, str]]
    id_col: str
    target_cols: list[str]
    meta_cols: list[str]
    task: str


def load_labels(src: Path, opts: dict[str, str], sample_level: str) -> LabelTable | None:
    if "labels_csv" not in opts:
        return None
    path = _csv_path(src, opts["labels_csv"])
    id_col = opts.get("id_col", DEFAULT_ID_COLS[sample_level])
    target_cols = split_list(opts.get("target_cols"))
    if not target_cols:
        raise ValidationFailed("labels_csv needs --opt target_cols=a,b,... (the label columns)")
    meta_cols = split_list(opts.get("meta_cols"))
    _, rows = read_csv(path, required=[id_col, *target_cols, *meta_cols])
    table: dict[str, dict[str, str]] = {}
    for lineno, row in enumerate(rows, start=2):
        key = row[id_col].strip()
        if key in table:
            raise ValidationFailed(f"duplicate id {key!r}", location=f"{path.name}:{lineno}")
        table[key] = row
    labeled = [r for r in rows if all(r[c].strip() for c in target_cols)]
    task = opts.get("task")
    if not task:
        guess = infer_task(labeled, target_cols) if labeled else "multilabel"
        binary = all(r[c].strip() in ("0", "1") for r in labeled for c in target_cols)
        task = guess if guess in TASKS else ("multilabel" if binary else "regression")
    if task not in TASKS:
        raise ValidationFailed(f"--opt task= must be one of {TASKS}, got {task!r}")
    return LabelTable(table, id_col, target_cols, meta_cols, task)


def labels_for(row: dict[str, str], table: LabelTable) -> tuple[Labels | None, str, str | None]:
    """(labels, label_source, skip_reason): all targets present -> gold; none -> unlabeled;
    some -> skipped as partial_targets."""
    values = [row[c].strip() for c in table.target_cols]
    if all(values):
        try:
            targets = {c: float(v) for c, v in zip(table.target_cols, values, strict=True)}
        except ValueError as e:
            raise ValidationFailed(f"unparsable target: {e}", location=row[table.id_col]) from e
        return Labels(targets=targets), "gold", None
    if not any(values):
        return None, "none", None
    return None, "none", "partial_targets"


def load_seq_csv(src: Path, opts: dict[str, str]) -> dict[str, dict[str, str]]:
    if "seq_csv" not in opts:
        return {}
    path = _csv_path(src, opts["seq_csv"])
    id_col = opts.get("seq_id_col", "SeriesInstanceUID")
    cols = split_list(opts.get("seq_cols"))
    header, rows = read_csv(path, required=[id_col, *cols])
    use = cols or [c for c in header if c != id_col]
    return {row[id_col].strip(): {c: row[c] for c in use} for row in rows}


def group_units(
    series: dict[str, list[SliceHeader]], sample_level: str
) -> dict[str, list[list[SliceHeader]]]:
    """sample id -> its series (each sorted by slice order), ordered by (SeriesNumber, uid)."""
    ordered = {uid: sort_slices(slices) for uid, slices in series.items()}

    def series_key(uid: str) -> tuple[int, int, str]:
        number = ordered[uid][0].tags.get("SeriesNumber")
        return (0, number, uid) if isinstance(number, int) else (1, 0, uid)

    units: dict[str, list[list[SliceHeader]]] = defaultdict(list)
    for uid in sorted(ordered, key=series_key):
        key = ordered[uid][0].study_uid if sample_level == "study" else uid
        units[key].append(ordered[uid])
    return units


def _series_summary(
    slices: list[SliceHeader], extra_tags: list[str], seq_row: dict[str, str]
) -> dict[str, Any]:
    first = slices[0]
    return {
        "description": first.tags.get("SeriesDescription"),
        "number": first.tags.get("SeriesNumber"),
        "n_slices": len(slices),
        "modality": first.tags.get("Modality"),
        "rows": first.rows,
        "columns": first.columns,
        "pixel_spacing": first.tags.get("PixelSpacing"),
        "slice_thickness": first.tags.get("SliceThickness"),
        "transfer_syntax": first.tags.get("TransferSyntaxUID"),
        **{kw: first.tags[kw] for kw in extra_tags if kw in first.tags},
        **seq_row,
    }


def _role(first: SliceHeader, role_from: str | None, seq_row: dict[str, str]) -> str | None:
    if not role_from:
        return None
    value = seq_row.get(role_from, first.tags.get(role_from))
    return None if value in (None, "") else str(value)


def build_views(
    sample_id: str,
    series_list: list[list[SliceHeader]],
    src: Path,
    *,
    view_level: str,
    role_from: str | None,
    seq_meta: dict[str, dict[str, str]],
    extra_tags: list[str],
    skipped: Skipped,
) -> tuple[list[View], dict[str, Any], int]:
    """(views, series summary, inconsistent_series count)."""
    views: list[View] = []
    summary: dict[str, Any] = {}
    inconsistent = 0
    for slices in series_list:
        first = slices[0]
        uid = first.series_uid
        seq_row = seq_meta.get(uid, {})
        role = _role(first, role_from, seq_row)
        if view_level == "slice":
            summary[uid] = _series_summary(slices, extra_tags, seq_row)
            for i, h in enumerate(slices):
                meta: dict[str, Any] = {}
                if h.instance_number is not None:
                    meta["instance_number"] = h.instance_number
                if h.tags.get("SliceLocation") is not None:
                    meta["slice_location"] = h.tags["SliceLocation"]
                views.append(
                    View(path=rel_posix(h.path, src), width=h.columns, height=h.rows, role=role,
                         seq_id=uid, seq_index=i, meta=meta)
                )
            continue
        dirs = {h.path.parent for h in slices}
        if len(dirs) != 1:
            skipped.append({"id": sample_id, "series": uid, "reason": "series_split_across_dirs"})
            continue
        summary[uid] = _series_summary(slices, extra_tags, seq_row)
        sizes = {(h.columns, h.rows) for h in slices}
        meta = {"n_slices": len(slices)}
        width, height = (first.columns, first.rows) if len(sizes) == 1 else (None, None)
        if len(sizes) != 1:
            meta["inconsistent_size"] = True
            inconsistent += 1
        views.append(
            View(path=rel_posix(dirs.pop(), src), width=width, height=height, role=role,
                 seq_id=uid, seq_index=None, meta=meta)
        )
    return views, summary, inconsistent


class DicomImporter:
    name = "dicom"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        sample_level = choice_option(opts, "sample_level", SAMPLE_LEVELS, "study")
        view_level = choice_option(opts, "view_level", VIEW_LEVELS, "slice")
        exif_policy = exif_policy_option(opts)
        pattern = opts.get("glob", "**/*.dcm")
        workers = int(opts.get("workers", "4"))
        extra_tags = split_list(opts.get("tags"))
        group_from = opts.get("group_from", DEFAULT_GROUP_TAG)
        role_from = opts.get("role_from")
        files = find_files(spec.src, pattern)
        if not files:
            raise ValidationFailed(f"no files match {pattern!r} under {spec.src}")
        wanted = [*SUMMARY_TAGS, *SLICE_TAGS, *extra_tags]
        if group_from != "none":
            wanted.append(group_from)
        if role_from:
            wanted.append(role_from)
        headers = read_headers(files, extra=wanted, workers=workers)
        skipped: Skipped = []
        series: dict[str, list[SliceHeader]] = defaultdict(list)
        for path, h in zip(files, headers, strict=True):
            if isinstance(h, str):
                skipped.append({"file": rel_posix(path, spec.src), "reason": h})
                continue
            series[h.series_uid].append(h)
        seq_meta = load_seq_csv(spec.src, opts)
        table = load_labels(spec.src, opts, sample_level)
        units = group_units(series, sample_level)
        samples: list[Sample] = []
        unlabeled = inconsistent = 0
        for sample_id in sorted(units):
            series_list = units[sample_id]
            views, summary, bad = build_views(
                sample_id, series_list, spec.src, view_level=view_level, role_from=role_from,
                seq_meta=seq_meta, extra_tags=extra_tags, skipped=skipped,
            )
            inconsistent += bad
            if not views:
                continue
            row = table.rows.get(sample_id) if table else None
            labels, source, reason = (
                labels_for(row, table) if table and row is not None else (None, "none", None)
            )
            if reason:
                skipped.append({"id": sample_id, "reason": reason})
                continue
            if source == "none":
                unlabeled += 1
            meta: dict[str, Any] = {"series": summary}
            if table and row is not None:
                meta.update({c: row[c] for c in table.meta_cols})
            first = series_list[0][0]
            group = None
            if group_from != "none" and first.tags.get(group_from) not in (None, ""):
                group = str(first.tags[group_from])
            samples.append(
                Sample(sample_id=sample_id, views=views, labels=labels, label_source=source,
                       group=group, meta=meta)
            )
        if table:
            for key in sorted(set(table.rows) - set(units)):
                skipped.append({"id": key, "reason": "no_files"})
            task, categories = table.task, [Category(id=i, name=c) for i, c in enumerate(table.target_cols)]
        else:
            task, categories = choice_option(opts, "task", TASKS, "multilabel"), []
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=str(spec.src),
            samples=samples,
            rows_read=len(files),
            skipped=skipped,
            unlabeled=unlabeled,
            exif_policy=exif_policy,
            extra_fields={"inconsistent_series": inconsistent} if inconsistent else None,
        )
```

`importers/__init__.py`：加 `from vcp.data.importers.dicom import DicomImporter`、`register_importer(DicomImporter())`（放在 `ImageCsvImporter` 之後）、`__all__` 加 `"DicomImporter"`。

Run: `uv run pytest tests/unit/data/importers/test_dicom.py -q`
Expected: PASS。若 `test_dicom_import_series_views...` 對 `seq_index=None` 的 series view 觸發 `Sample` 驗證問題，檢查 `schema.Sample._consistency` 只對非 None 的 seq_index 查重（現行如此）。

- [ ] **Step 4: 全套、ruff、commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add src/vcp/data/importers/dicom.py src/vcp/data/importers/__init__.py src/vcp/data/importers/base.py src/vcp/cli.py tests/unit/data/importers/test_dicom.py
git commit -m "feat(importers): dicom 匯入器——header 分組、切片排序、study/series 兩層級、labels/seq CSV join、PatientID 分組

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: materialize 執行器、manifest 與 `vcp data materialize`（spec §15.4-19 到 -23）

**Files:**
- Create: `src/vcp/data/materialize/base.py`、`src/vcp/data/materialize/manifest.py`、`src/vcp/data/materialize/run.py`
- Modify: `src/vcp/data/materialize/__init__.py`（加匯出）、`src/vcp/cli.py`（新命令，放在 `audit_cmd` 之後）
- Test: `tests/unit/data/materialize/test_run.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 4 的 `decoders`、`window.to_uint8 / resize_long_side / WINDOW_MODES`；Task 5 的 `dicom` 匯入器（測試用）；`DatasetPaths.cache_dir`、`resolve_image_root`；`hashing.sha256_file / sha256_text`；`time.stamp`。
- Produces:
  - `base.MODES = ("npy", "png")`、`base.MaterializeSpec(name, mode, resize=None, stack_seq=False, window="dicom", workers=1, force=False, decoder=None, data_root=None, configs_root=None)`、`base.MaterializeResult(out_dir, manifest_path, materialized, skipped, failed, warnings)`、`base.mode_dir_name(mode, resize)`、`base.safe_dir_name(sample_id)`
  - `manifest.ManifestRow`（欄位見 spec §15.4-21 加 `bytes`）、`manifest.row_key(sample_id, view, seq_id)`、`manifest.read_manifest(path) -> dict[str, ManifestRow]`、`manifest.write_manifest(path, rows)`
  - `run.materialize(spec) -> MaterializeResult`、`run.plan_jobs(dataset, spec)`、`run.run_job(job, ...)`（top-level，process pool 可 pickle）
  - CLI `vcp data materialize --name --mode npy|png [--resize N] [--stack-seq] [--window dicom|minmax|percentile] [--workers N] [--force] [--decoder NAME]`；VERDICT 欄位 `mode resize materialized skipped failed out`；`failed>0` → FAIL、有 warnings → WARN

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/materialize/test_run.py`：

```python
import json

import numpy as np
import pytest
from PIL import Image

from helpers import det_samples, make_card, write_dicom_study, write_images
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.materialize.base import safe_dir_name
from vcp.data.materialize.manifest import read_manifest, row_key


def _image_ds(roots, name="tiny", n=6):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples).save(paths)
    return paths


def _dicom_ds(roots, name, **opts):
    src = roots.data / "raw" / name
    write_dicom_study(src, study_uid="1.2.1", series=2, slices=3)
    return get_importer("dicom").run(
        ImportSpec(importer="dicom", src=src, name=name, options=opts, license="CC0",
                   url="u", downloaded_at="2026-09-03", data_root=roots.data,
                   configs_root=roots.configs)
    )


def _spec(roots, name="tiny", **kw):
    return MaterializeSpec(name=name, data_root=roots.data, configs_root=roots.configs, **kw)


def test_png_resize_then_skip_then_force(roots):
    _image_ds(roots)
    res = materialize(_spec(roots, mode="png", resize=4))
    assert (res.materialized, res.skipped, res.failed) == (6, 0, 0)
    assert res.out_dir.name == "png-r4" and (res.out_dir / "s0000" / "0.png").is_file()
    assert Image.open(res.out_dir / "s0000" / "0.png").size == (4, 4)
    rows = read_manifest(res.manifest_path)
    row = rows[row_key("s0000", 0, None)]
    assert row.shape == [4, 4, 3] and row.dtype == "uint8" and row.resize == 4
    assert row.out == "s0000/0.png" and row.src == "s0000.jpg" and row.decoder == "image"
    again = materialize(_spec(roots, mode="png", resize=4))
    assert (again.materialized, again.skipped) == (0, 6)
    forced = materialize(_spec(roots, mode="png", resize=4, force=True))
    assert forced.materialized == 6 and len(read_manifest(forced.manifest_path)) == 6


def test_npy_mode_and_option_validation(roots):
    _image_ds(roots, n=2)
    res = materialize(_spec(roots, mode="npy"))
    arr = np.load(res.out_dir / "s0000" / "0.npy")
    assert arr.shape == (8, 8, 3) and arr.dtype == np.uint8 and res.out_dir.name == "npy"
    with pytest.raises(ValidationFailed, match="resize"):
        materialize(_spec(roots, mode="npy", resize=8))
    with pytest.raises(ValidationFailed, match="mode"):
        materialize(_spec(roots, mode="tif"))
    with pytest.raises(ValidationFailed, match="window"):
        materialize(_spec(roots, mode="png", window="gamma"))


def test_failures_are_recorded_not_raised(roots):
    _image_ds(roots)
    (roots.data / "raw" / "tiny" / "s0001.jpg").write_bytes(b"broken")
    res = materialize(_spec(roots, mode="npy"))
    assert (res.materialized, res.failed) == (5, 1)
    failed = [
        json.loads(line)
        for line in (res.out_dir / "failed.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert failed[0]["sample_id"] == "s0001" and "UnidentifiedImageError" in failed[0]["error"]
    assert row_key("s0001", 0, None) not in read_manifest(res.manifest_path)


def test_dicom_stack_seq_png_window_and_series_views(roots):
    _dicom_ds(roots, "dcm")
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (res.materialized, res.failed) == (2, 0)
    vol = np.load(res.out_dir / "1.2.1" / "1.2.1.1.npy")
    assert vol.shape == (3, 16, 16) and [int(v) for v in vol[:, 0, 0]] == [120, 110, 100]
    row = read_manifest(res.manifest_path)[row_key("1.2.1", None, "1.2.1.1")]
    assert row.shape == [3, 16, 16] and row.dtype == "uint16" and row.decoder == "dicom"
    assert materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True)).skipped == 2
    png = materialize(_spec(roots, name="dcm", mode="png", resize=8))
    assert png.materialized == 6
    img = np.asarray(Image.open(png.out_dir / "1.2.1" / "0.png"))
    assert img.shape == (8, 8) and int(img[0, 0]) == 15  # (120 - 0) / 2000 * 255 under WindowCenter 1000 / Width 2000
    _dicom_ds(roots, "dcm2", view_level="series")
    vols = materialize(_spec(roots, name="dcm2", mode="npy"))
    assert vols.materialized == 2 and np.load(vols.out_dir / "1.2.1" / "0.npy").shape == (3, 16, 16)
    bad = materialize(_spec(roots, name="dcm2", mode="png"))
    assert bad.failed == 2 and bad.materialized == 0


def test_stack_seq_shape_mismatch_falls_back_per_view(roots):
    src = roots.data / "raw" / "mix"
    write_dicom_study(src, study_uid="1.2.5", series=1, slices=2)
    write_dicom_study(src, study_uid="1.2.5", series=1, slices=1, size=(8, 8))  # overwrites slice 1
    get_importer("dicom").run(
        ImportSpec(importer="dicom", src=src, name="mix", license="CC0", url="u",
                   downloaded_at="2026-09-03", data_root=roots.data, configs_root=roots.configs)
    )
    res = materialize(_spec(roots, name="mix", mode="npy", stack_seq=True))
    assert res.materialized == 2 and res.failed == 0
    assert any("shapes differ" in w for w in res.warnings)
    assert (res.out_dir / "1.2.5" / "0.npy").is_file() and (res.out_dir / "1.2.5" / "1.npy").is_file()


def test_process_pool_and_safe_dir_names(roots):
    _image_ds(roots, n=3)
    assert materialize(_spec(roots, mode="npy", workers=2)).materialized == 3
    assert safe_dir_name("a/b.jpg") == "a__b.jpg"
    assert safe_dir_name("1.2.826.0.1") == "1.2.826.0.1"
    assert len(safe_dir_name("weird:name?")) == 16 and safe_dir_name("..") != ".."
```

Run: `uv run pytest tests/unit/data/materialize/test_run.py -q`
Expected: FAIL（ImportError：`MaterializeSpec`）。

- [ ] **Step 2: `base.py` 與 `manifest.py`**

`src/vcp/data/materialize/base.py`：

```python
"""Materialize contract: spec, result, output naming."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths

MODES = ("npy", "png")
_SAFE = re.compile(r"[A-Za-z0-9._@+=,-]+")


class MaterializeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    mode: str
    resize: int | None = None
    stack_seq: bool = False
    window: str = "dicom"
    workers: int = 1
    force: bool = False
    decoder: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class MaterializeResult(BaseModel):
    out_dir: Path
    manifest_path: Path
    materialized: int
    skipped: int
    failed: int
    warnings: list[str]


def mode_dir_name(mode: str, resize: int | None) -> str:
    return f"{mode}-r{resize}" if resize else mode


def safe_dir_name(sample_id: str) -> str:
    """``/`` -> ``__``; anything else unsafe for a file name -> first 16 hex of sha256."""
    flat = sample_id.replace("/", "__")
    if _SAFE.fullmatch(flat) and flat not in (".", ".."):
        return flat
    return sha256_text(sample_id)[:16]
```

`src/vcp/data/materialize/manifest.py`：

```python
"""manifest.jsonl: one row per materialized output, paths relative to the mode directory."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from vcp.core.errors import ValidationFailed


class ManifestRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    view: int | None
    seq_id: str | None
    src: str
    out: str
    shape: list[int]
    dtype: str
    resize: int | None
    window: str | None
    exif_policy: str
    decoder: str
    decoder_version: str
    sha256: str
    bytes: int
    materialized_at: str


def row_key(sample_id: str, view: int | None, seq_id: str | None) -> str:
    return f"{sample_id}\x00{view}\x00{seq_id}"


def read_manifest(path: Path) -> dict[str, ManifestRow]:
    if not path.is_file():
        return {}
    rows: dict[str, ManifestRow] = {}
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = ManifestRow.model_validate_json(line)
            except ValidationError as e:
                raise ValidationFailed(f"bad manifest row: {e}", location=f"{path}:{lineno}") from e
            rows[row_key(row.sample_id, row.view, row.seq_id)] = row
    return rows


def write_manifest(path: Path, rows: Iterable[ManifestRow]) -> None:
    ordered = sorted(rows, key=lambda r: (r.sample_id, r.seq_id or "", -1 if r.view is None else r.view))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in ordered:
            f.write(row.model_dump_json() + "\n")
```

- [ ] **Step 3: `run.py`**

```python
"""Decode every view (or every sequence) of a dataset once into cache/materialize/<mode>/."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.materialize.base import MODES, MaterializeResult, MaterializeSpec, mode_dir_name, safe_dir_name
from vcp.data.materialize.decoders import Decoded, decoder_for, get_decoder
from vcp.data.materialize.manifest import ManifestRow, read_manifest, row_key, write_manifest
from vcp.data.materialize.window import WINDOW_MODES, resize_long_side, to_uint8


@dataclass(frozen=True)
class Job:
    sample_id: str
    view: int | None  # None for a stacked sequence
    seq_id: str | None
    views: tuple[int, ...]  # view indices behind this job (one, or the whole sequence)
    srcs: tuple[str, ...]  # view paths relative to image_root, in seq order
    out_dir: str  # safe sample directory
    decoder: str


@dataclass(frozen=True)
class JobOutcome:
    rows: list[ManifestRow] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Settings:
    image_root: Path
    out_root: Path
    mode: str
    resize: int | None
    window: str
    exif_policy: str


def _validate(spec: MaterializeSpec) -> None:
    if spec.mode not in MODES:
        raise ValidationFailed(f"--mode must be one of {MODES}, got {spec.mode!r}")
    if spec.window not in WINDOW_MODES:
        raise ValidationFailed(f"--window must be one of {WINDOW_MODES}, got {spec.window!r}")
    if spec.resize is not None and spec.mode != "png":
        raise ValidationFailed("--resize applies to png mode only (npy keeps native resolution)")
    if spec.resize is not None and spec.resize < 1:
        raise ValidationFailed(f"--resize must be >= 1, got {spec.resize}")
    if spec.workers < 1:
        raise ValidationFailed(f"--workers must be >= 1, got {spec.workers}")


def plan_jobs(dataset: Dataset, spec: MaterializeSpec) -> list[Job]:
    ext = spec.mode
    jobs: list[Job] = []
    dirs: dict[str, str] = {}
    for s in dataset.samples:
        d = safe_dir_name(s.sample_id)
        if d in dirs and dirs[d] != s.sample_id:
            raise ValidationFailed(f"output directory collision: {s.sample_id!r} vs {dirs[d]!r} -> {d!r}")
        dirs[d] = s.sample_id
        stacked: set[int] = set()
        if spec.stack_seq:
            by_seq: dict[str, list[int]] = {}
            for i, v in enumerate(s.views):
                if v.seq_id is not None and v.seq_index is not None:
                    by_seq.setdefault(v.seq_id, []).append(i)
            for seq_id, idxs in by_seq.items():
                idxs.sort(key=lambda i: s.views[i].seq_index or 0)
                stacked.update(idxs)
                jobs.append(Job(s.sample_id, None, seq_id, tuple(idxs),
                                tuple(s.views[i].path for i in idxs), d,
                                decoder_for(Path(s.views[idxs[0]].path), spec.decoder).name))
        for i, v in enumerate(s.views):
            if i in stacked:
                continue
            jobs.append(Job(s.sample_id, i, v.seq_id, (i,), (v.path,), d,
                            decoder_for(Path(v.path), spec.decoder).name))
    return jobs


def _write(decoded: Decoded, out: Path, cfg: Settings) -> tuple[list[int], str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if cfg.mode == "npy":
        np.save(out, decoded.array)
        return list(decoded.array.shape), str(decoded.array.dtype)
    arr = to_uint8(decoded, cfg.window)
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4) or arr.ndim > 3:
        raise ValidationFailed("png mode needs single 2-D views; use npy for volumes")
    if cfg.resize is not None:
        arr = resize_long_side(arr, cfg.resize)
    Image.fromarray(arr).save(out, format="PNG")
    return list(arr.shape), str(arr.dtype)


def _row(job: Job, view: int | None, src: str, out: Path, shape: list[int], dtype: str, cfg: Settings, version: str) -> ManifestRow:
    return ManifestRow(
        sample_id=job.sample_id, view=view, seq_id=job.seq_id, src=src,
        out=out.relative_to(cfg.out_root).as_posix(), shape=shape, dtype=dtype, resize=cfg.resize,
        window=cfg.window if cfg.mode == "png" else None, exif_policy=cfg.exif_policy,
        decoder=job.decoder, decoder_version=version, sha256=sha256_file(out),
        bytes=out.stat().st_size, materialized_at=stamp(),
    )


def run_job(job: Job, cfg: Settings) -> JobOutcome:
    """Decode and write one job. Never raises: problems become ``failures`` rows."""
    dec = get_decoder(job.decoder)
    ext = cfg.mode
    try:
        paths = [cfg.image_root / s for s in job.srcs]
        if len(paths) == 1 and paths[0].is_dir():  # series-level view: a directory of slices
            files = sorted(p for p in paths[0].iterdir() if p.is_file())
            decoded = dec.decode_series(files)
            out = cfg.out_root / job.out_dir / f"{job.view}.{ext}"
            shape, dtype = _write(decoded, out, cfg)
            return JobOutcome([_row(job, job.view, job.srcs[0], out, shape, dtype, cfg, dec.version)])
        frames = [dec.decode(p, exif_policy=cfg.exif_policy) for p in paths]
        if len(frames) == 1:
            out = cfg.out_root / job.out_dir / f"{job.view}.{ext}"
            shape, dtype = _write(frames[0], out, cfg)
            return JobOutcome([_row(job, job.view, job.srcs[0], out, shape, dtype, cfg, dec.version)])
        if len({f.array.shape for f in frames}) == 1:
            stacked = Decoded(np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)})
            out = cfg.out_root / job.out_dir / f"{safe_dir_name(job.seq_id or 'seq')}.{ext}"
            shape, dtype = _write(stacked, out, cfg)
            return JobOutcome([_row(job, None, job.srcs[0], out, shape, dtype, cfg, dec.version)])
        rows = []
        for idx, src, frame in zip(job.views, job.srcs, frames, strict=True):
            out = cfg.out_root / job.out_dir / f"{idx}.{ext}"
            shape, dtype = _write(frame, out, cfg)
            rows.append(_row(job, idx, src, out, shape, dtype, cfg, dec.version))
        return JobOutcome(rows, [], [f"{job.sample_id}/{job.seq_id}: slice shapes differ; wrote per-view files"])
    except Exception as e:  # noqa: BLE001 - every failure is reported in failed.jsonl, run continues
        return JobOutcome([], [{"sample_id": job.sample_id, "view": job.view, "seq_id": job.seq_id,
                                "src": job.srcs[0], "error": f"{type(e).__name__}: {e}"}])


def _is_current(job: Job, existing: dict[str, ManifestRow], spec: MaterializeSpec, out_root: Path) -> bool:
    """Skip when a matching row exists and its file is still there with the recorded size."""
    version = get_decoder(job.decoder).version
    window = spec.window if spec.mode == "png" else None
    keys = [row_key(job.sample_id, job.view, job.seq_id)]
    if job.view is None:  # a stack may have fallen back to per-view rows last time
        keys = [keys[0], *(row_key(job.sample_id, i, job.seq_id) for i in job.views)]
    rows = [existing.get(k) for k in keys]
    candidates = [rows[0]] if rows[0] is not None else [r for r in rows[1:]]
    if not candidates or (job.view is None and rows[0] is None and len(candidates) != len(job.views)):
        return False
    for r in candidates:
        if r is None or r.resize != spec.resize or r.window != window or r.decoder_version != version:
            return False
        f = out_root / r.out
        if not f.is_file() or f.stat().st_size != r.bytes:
            return False
    return True


def materialize(spec: MaterializeSpec) -> MaterializeResult:
    _validate(spec)
    paths = spec.paths()
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    out_root = paths.cache_dir / "materialize" / mode_dir_name(spec.mode, spec.resize)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.jsonl"
    existing = {} if spec.force else read_manifest(manifest_path)
    cfg = Settings(paths.resolve_image_root(dataset.card), out_root, spec.mode, spec.resize,
                   spec.window, dataset.card.exif_policy)
    jobs = plan_jobs(dataset, spec)
    todo = [j for j in jobs if not _is_current(j, existing, spec, out_root)]
    skipped = len(jobs) - len(todo)
    if spec.workers <= 1 or len(todo) < 2:
        outcomes = [run_job(j, cfg) for j in todo]
    else:
        with ProcessPoolExecutor(max_workers=spec.workers) as pool:
            outcomes = list(pool.map(partial(run_job, cfg=cfg), todo))
    rows = dict(existing)
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    for o in outcomes:
        for r in o.rows:
            rows[row_key(r.sample_id, r.view, r.seq_id)] = r
        failures.extend(o.failures)
        warnings.extend(o.warnings)
    write_manifest(manifest_path, rows.values())
    failed_path = out_root / "failed.jsonl"
    if failures:
        with failed_path.open("w", encoding="utf-8", newline="\n") as f:
            for row in failures:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    elif failed_path.exists():
        failed_path.unlink()
    return MaterializeResult(out_dir=out_root, manifest_path=manifest_path,
                             materialized=sum(len(o.rows) for o in outcomes), skipped=skipped,
                             failed=len(failures), warnings=warnings)
```

`src/vcp/data/materialize/__init__.py` 加：`from vcp.data.materialize.base import MODES, MaterializeResult, MaterializeSpec`、`from vcp.data.materialize.run import materialize`，並加入 `__all__`。

Run: `uv run pytest tests/unit/data/materialize -q`
Expected: PASS。若 `test_dicom_stack_seq...` 的 `int(img[0, 0]) == 15` 差 1，先用 `uv run python` 印出實際值確認是四捨五入（`120 / 2000 * 255 = 15.3`），不要改實作去湊。

- [ ] **Step 4: CLI 命令與測試**

`tests/unit/test_cli.py` 加：

```python
def test_materialize_cli(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, with_images=True).exit_code == 0
    r = runner.invoke(app, ["data", "materialize", "--name", "tiny", "--mode", "png", "--resize", "4"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "materialized=60" in v and "mode=png" in v and "resize=4" in v
    r = runner.invoke(app, ["data", "materialize", "--name", "tiny", "--mode", "png", "--resize", "4"])
    assert "skipped=60" in _last_verdict(r.output)
    (tmp_path / "src" / "s0003.jpg").unlink()
    r = runner.invoke(app, ["data", "materialize", "--name", "tiny", "--mode", "npy"])
    assert r.exit_code == 1 and "failed=1" in _last_verdict(r.output)
    r = runner.invoke(app, ["data", "materialize", "--name", "tiny", "--mode", "npy", "--resize", "3"])
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)
```

`src/vcp/cli.py`（import 加 `from vcp.data.materialize import MaterializeSpec, materialize`）：

```python
@data_app.command("materialize")
def materialize_cmd(
    name: NameOpt,
    mode: Annotated[str, typer.Option("--mode", help="npy | png")],
    resize: Annotated[int | None, typer.Option("--resize", help="png only: long side in px")] = None,
    stack_seq: Annotated[bool, typer.Option("--stack-seq", help="stack views of a seq into S×H×W")] = False,
    window: Annotated[str, typer.Option("--window", help="png 8-bit mapping: dicom | minmax | percentile")] = "dicom",
    workers: Annotated[int, typer.Option("--workers", help="decode processes")] = 1,
    force: Annotated[bool, typer.Option("--force", help="redo outputs that already exist")] = False,
    decoder: Annotated[str | None, typer.Option("--decoder", help="force a registered decoder")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Decode every view once into cache/materialize/<mode>/ with a portable manifest."""

    def fn() -> CmdResult:
        res = materialize(
            MaterializeSpec(name=name, mode=mode, resize=resize, stack_seq=stack_seq, window=window,
                            workers=workers, force=force, decoder=decoder, data_root=data_root,
                            configs_root=configs_root)
        )
        status: Status = "FAIL" if res.failed else ("WARN" if res.warnings else "OK")
        fields: dict[str, FieldValue] = {"name": name, "mode": mode}
        if resize is not None:
            fields["resize"] = resize
        fields.update({"materialized": res.materialized, "skipped": res.skipped,
                       "failed": res.failed, "out": str(res.out_dir)})
        human = [f"materialized {res.materialized}, skipped {res.skipped}, failed {res.failed} -> {res.out_dir}",
                 *res.warnings]
        if res.failed:
            human.append(f"see {res.out_dir / 'failed.jsonl'}")
        payload = {"manifest": str(res.manifest_path), "warnings": res.warnings}
        return status, fields, payload, human

    run_command("materialize", json_mode, data_root, fn)
```

Run: `uv run pytest tests/unit/test_cli.py -k materialize -q`
Expected: PASS。

- [ ] **Step 5: 全套、ruff、commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add src/vcp/data/materialize src/vcp/cli.py tests/unit/data/materialize/test_run.py tests/unit/test_cli.py
git commit -m "feat(data): vcp data materialize——npy/png 快取、resize、stack-seq、window、可搬移 manifest、failed.jsonl、process pool

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Plan 2a 遺留 hygiene（spec §15.7-29 到 -32）

**Files:**
- Modify: `src/vcp/data/exporters/yolo.py:18-30,107-113`、`src/vcp/data/importers/base.py:75-85`（`count_invalidated_plans`）、`src/vcp/cli.py`（import 的 `old_card` 欄位）
- Test: `tests/unit/data/exporters/test_exporters.py`、`tests/unit/data/importers/test_jsonl.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 2 的 `ExportOutput`。
- Produces:
  - YOLO 匯出 `manifest.json["categories"] = [{"index", "id", "name"}, ...]`；`ExportResult.fields["images"] ∈ {"copied", "symlinked"}`
  - `yolo._place_image` 只在 `errno ∈ {EPERM, EACCES}` 或 `winerror == 1314` 時退回複製，其他 `OSError` 傳播
  - `count_invalidated_plans(paths, new_digest) -> tuple[int, bool]`（計數, 舊 card 可讀）；`ImportResult.old_card_unreadable: bool = False`；VERDICT `old_card=unreadable`
  - CLI 層測試覆蓋 `unlabeled=`、空子集 `status=WARN`、`plans_invalidated` 行

- [ ] **Step 1: 匯出器的失敗測試**

`tests/unit/data/exporters/test_exporters.py` 加（檔頭補 `import errno`、`from vcp.data.exporters.yolo import _place_image`）：

```python
def test_yolo_manifest_categories_and_images_field(det_ds, roots, tmp_path):
    res = export_subset(_spec(roots, "yolo", tmp_path / "y", options={"copy": "true"}))
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["categories"] == [
        {"index": i, "id": c.id, "name": c.name} for i, c in enumerate(CATS)
    ]
    assert res.fields["images"] == "copied"
    res2 = export_subset(_spec(roots, "yolo", tmp_path / "y2"))
    assert res2.fields["images"] in ("copied", "symlinked")


def test_place_image_only_falls_back_on_permission_errors(tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    Image.new("RGB", (4, 4)).save(src)

    def denied(self, target, target_is_directory=False):
        raise OSError(errno.EPERM, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", denied)
    assert _place_image(src, tmp_path / "b.jpg", copy=False) is True
    assert (tmp_path / "b.jpg").is_file()

    def missing(self, target, target_is_directory=False):
        raise OSError(errno.ENOENT, "no such file")

    monkeypatch.setattr(Path, "symlink_to", missing)
    with pytest.raises(OSError, match="no such file"):
        _place_image(src, tmp_path / "c.jpg", copy=False)
```

（`det_ds` fixture 與 `_spec` 已在該檔；`_spec(roots, fmt, out, subset="valA", **kw)` 把 `options=` 透傳給 `ExportSpec`。）

Run: `uv run pytest tests/unit/data/exporters -k "manifest_categories or place_image" -q`
Expected: FAIL（`KeyError: 'categories'` / `_place_image` 對 ENOENT 也複製）。

- [ ] **Step 2: 實作 YOLO 變更**

`src/vcp/data/exporters/yolo.py`：

```python
import errno
...
_SYMLINK_DENIED_ERRNO = {errno.EPERM, errno.EACCES}
_WINERROR_PRIVILEGE_NOT_HELD = 1314


def _place_image(src: Path, dst: Path, *, copy: bool) -> bool:
    """Put ``src`` at ``dst`` by symlink, or copy. Returns True when the symlink was refused for
    lack of privilege and a copy was made instead; any other OSError propagates."""
    if copy:
        shutil.copy2(src, dst)
        return False
    try:
        dst.symlink_to(src)
        return False
    except OSError as e:
        denied = e.errno in _SYMLINK_DENIED_ERRNO or (
            getattr(e, "winerror", None) == _WINERROR_PRIVILEGE_NOT_HELD
        )
        if not denied:
            raise
        shutil.copy2(src, dst)
        return True
```

`run` 結尾改為：

```python
        return ExportOutput(
            files,
            warnings,
            fields={"images": "copied" if (copy or fell_back) else "symlinked"},
            manifest={
                "categories": [
                    {"index": i, "id": c.id, "name": c.name}
                    for i, c in enumerate(dataset.card.categories)
                ]
            },
        )
```

Run: `uv run pytest tests/unit/data/exporters -q`
Expected: PASS（既有的 symlink 退回測試若以 `OSError()` 無 errno 觸發，改為 `OSError(errno.EPERM, "denied")`）。

- [ ] **Step 3: `old_card=unreadable` 的失敗測試**

`tests/unit/data/importers/test_jsonl.py` 的 `test_reimport_over_corrupt_card_still_counts_plans` 末尾加 `assert again.old_card_unreadable is True`；並在 `test_raw_manifest_sizes_mode_is_recorded` 加 `assert res.old_card_unreadable is False`。

`tests/unit/test_cli.py` 加：

```python
def test_reimport_over_unreadable_card_reports_old_card(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "p1", "--seed", "1"])
    assert r.exit_code == 0, r.output
    card = roots.configs / "datasets" / "tiny" / "dataset.yaml"
    card.write_text("name: [broken\n", encoding="utf-8")
    r = _import_tiny(roots, tmp_path)
    v = _last_verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    assert "plans_invalidated=1" in v and "old_card=unreadable" in v
```

Run: `uv run pytest tests/unit/data/importers/test_jsonl.py tests/unit/test_cli.py -k "corrupt_card or unreadable" -q`
Expected: FAIL（`AttributeError: old_card_unreadable`）。

- [ ] **Step 4: 實作**

`src/vcp/data/importers/base.py`：

```python
def count_invalidated_plans(paths: DatasetPaths, new_digest: str) -> tuple[int, bool]:
    """(plans a re-import would orphan, old card readable). An unreadable old card cannot prove
    the plans still match, so every plan file counts."""
    if not paths.card_yaml.is_file() or not paths.splits_dir.is_dir():
        return 0, True
    try:
        old = load_yaml_model(paths.card_yaml, DatasetCard)
    except ValidationFailed:
        return len(list(paths.splits_dir.glob("*.json"))), False
    if old.samples_hash == new_digest:
        return 0, True
    return len(list(paths.splits_dir.glob("*.json"))), True
```

`ImportResult` 加 `old_card_unreadable: bool = False`；`finalize_import` 中 `plans_invalidated, readable = count_invalidated_plans(...)`，結果傳 `old_card_unreadable=not readable`。`src/vcp/cli.py` `import_cmd.fn` 在 `plans_invalidated` 區塊內加 `if res.old_card_unreadable: fields["old_card"] = "unreadable"`。

Run: `uv run pytest tests/unit -q`
Expected: PASS。

- [ ] **Step 5: 補 CLI 層覆蓋（`unlabeled=`、空子集 WARN）**

`tests/unit/test_cli.py` 加：

```python
def test_import_yolo_reports_unlabeled(roots, tmp_path):
    src = tmp_path / "yolo"
    for rel in ("images/a.jpg", "images/b.jpg"):
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(p)
    (src / "labels").mkdir()
    (src / "labels" / "a.txt").write_text("0 0.5 0.5 0.5 0.5\n", encoding="utf-8")
    (src / "classes.txt").write_text("thing\n", encoding="utf-8")
    r = runner.invoke(
        app,
        ["data", "import", "--importer", "yolo", "--src", str(src), "--name", "y",
         "--license", "CC0", "--url", "u", "--downloaded-at", "2026-09-03"],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "unlabeled=1" in v and "samples=2" in v


def test_export_empty_subset_is_warn(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, n=5, with_images=True).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "p1", "--seed", "1"])
    assert r.exit_code == 0 and "empty_subsets=" in _last_verdict(r.output)
    empty = _last_verdict(r.output).split("empty_subsets=")[1].split()[0].split(",")[0]
    r = runner.invoke(
        app,
        ["data", "export", "--name", "tiny", "--plan", "p1", "--subset", empty,
         "--format", "coco", "--out", str(tmp_path / "out")],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "subset is empty" in v
```

（檔頭補 `from PIL import Image`。若 5 個樣本的預設切分沒有空子集，把 `n=5` 改成 `n=3`。）

Run: `uv run pytest tests/unit/test_cli.py -q`
Expected: PASS。

- [ ] **Step 6: 全套、ruff、commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add src/vcp/data/exporters/yolo.py src/vcp/data/importers/base.py src/vcp/cli.py tests/unit/data/exporters/test_exporters.py tests/unit/data/importers/test_jsonl.py tests/unit/test_cli.py
git commit -m "fix(data,cli): YOLO manifest categories 與 images= 欄位、_place_image 只在權限不足時退回複製、old_card=unreadable、CLI 層補 unlabeled 與空子集 WARN 測試

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: RSNA 真資料整合測試、端到端流程、文件、最終驗證（spec §15.4-23、§15.6-27/-28、§15.7-33）

**Files:**
- Create: `tests/integration/test_rsna_knee.py`
- Modify: `tests/unit/test_e2e_flow.py`（加 dicom 流程）、`tests/integration/README.md`、`README.md`、`CLAUDE.md`

**Interfaces:**
- Consumes: 全部前置任務；真資料佈局 `VCP_REALDATA_ROOT/raw/rsna-knee/{train.csv,train_series.csv,train_series/<study>/<series>/*.dcm}` 與已匯入的資料集 `rsna-knee`（README 給出匯入命令）。
- Produces: 文件與測試，無新介面。

- [ ] **Step 1: 真資料整合測試**

`tests/integration/test_rsna_knee.py`：

```python
"""Real-data checks for the RSNA Knee subset (spec §15.6-27). Expectations are derived from the
competition CSVs on disk, never hard-coded, so any subset size works."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.materialize.manifest import read_manifest

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


@pytest.fixture(scope="module")
def raw(real_roots):
    root = real_roots.data / "raw" / NAME
    if not (root / "train.csv").is_file():
        pytest.skip("RSNA raw CSVs not present")
    return root


def test_knee_shape_matches_csvs(knee, raw):
    studies = sorted(p.name for p in (raw / "train_series").iterdir() if p.is_dir())
    assert [s.sample_id for s in knee.samples] == studies
    assert knee.card.task == "multilabel" and len(knee.card.categories) == 12
    labels = {r["StudyInstanceUID"]: r for r in _rows(raw / "train.csv")}
    target_cols = [c.name for c in knee.card.categories]
    gold = [sid for sid in studies if all(labels[sid][c].strip() for c in target_cols)]
    assert sum(s.label_source == "gold" for s in knee.samples) == len(gold)
    assert sum(s.label_source == "none" for s in knee.samples) == len(studies) - len(gold)
    per_study = Counter(r["StudyInstanceUID"] for r in _rows(raw / "train_series.csv"))
    for s in knee.samples:
        assert len(s.meta["series"]) == per_study[s.sample_id]
        assert s.meta["Report"].strip()
        by_seq: dict[str, list[int]] = {}
        for v in s.views:
            assert v.width and v.height and v.seq_id and v.role in ("Sagittal", "Coronal", "Axial")
            by_seq.setdefault(v.seq_id, []).append(v.seq_index)
        assert all(idx == list(range(len(idx))) for idx in by_seq.values())


def test_knee_materialize_three_studies(knee, real_roots, tmp_path):
    real_paths = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    image_root = real_paths.resolve_image_root(knee.card)
    data, configs = tmp_path / "data", tmp_path / "configs"
    paths = DatasetPaths.resolve("knee3", data_root=data, configs_root=configs)
    three = knee.samples[:3]
    card = knee.card.model_copy(update={"name": "knee3", "image_root": str(image_root)})
    Dataset.from_parts(card, three).save(paths)
    spec = dict(name="knee3", data_root=data, configs_root=configs)
    png = materialize(MaterializeSpec(mode="png", resize=256, **spec))
    assert png.failed == 0 and png.materialized == sum(len(s.views) for s in three)
    again = materialize(MaterializeSpec(mode="png", resize=256, **spec))
    assert again.skipped == png.materialized and again.materialized == 0
    forced = materialize(MaterializeSpec(mode="png", resize=256, force=True, **spec))
    assert {r.sha256 for r in read_manifest(forced.manifest_path).values()} == {
        r.sha256 for r in read_manifest(png.manifest_path).values()
    }
    vol = materialize(MaterializeSpec(mode="npy", stack_seq=True, **spec))
    assert vol.failed == 0 and vol.materialized == sum(len(s.meta["series"]) for s in three)
    row = next(iter(read_manifest(vol.manifest_path).values()))
    arr = np.load(vol.out_dir / row.out)
    assert arr.ndim == 3 and arr.shape[0] == three[0].meta["series"][row.seq_id]["n_slices"]
    assert arr.dtype in (np.uint16, np.int16)
```

Run: `uv run pytest tests/integration -q`
Expected: 全部 skipped（資料未匯入時），或 pass（已依 README 匯入子集）。

- [ ] **Step 2: 端到端流程加 dicom → materialize**

`tests/unit/test_e2e_flow.py` 加（檔頭補 `import numpy as np` 已有；加 `from helpers import write_dicom_study`）：

```python
def test_dicom_flow(roots, tmp_path):
    src = roots.data / "raw" / "knee"
    write_dicom_study(src, study_uid="1.2.1", patient_id="PA", series=2, slices=2)
    write_dicom_study(src, study_uid="1.2.2", patient_id="PB", series=1, slices=3, compress="rle")
    (src / "train.csv").write_text(
        "StudyInstanceUID,Report,ACL\n1.2.1,torn,1\n1.2.2,ok,\n", encoding="utf-8"
    )
    common = ["--license", "CC0", "--url", "u", "--downloaded-at", "2026-09-03"]
    r = runner.invoke(
        app,
        ["data", "import", "--importer", "dicom", "--src", str(src), "--name", "knee", *common,
         "--opt", "labels_csv=train.csv", "--opt", "target_cols=ACL", "--opt", "meta_cols=Report",
         "--opt", "role_from=SeriesDescription", "--raw-manifest", "sizes"],
    )
    assert r.exit_code == 0, r.output
    verdict = _verdicts(r.output)[-1]
    assert "samples=2" in verdict and "unlabeled=1" in verdict and "rows_read=7" in verdict
    r = runner.invoke(app, ["data", "materialize", "--name", "knee", "--mode", "npy", "--stack-seq"])
    assert r.exit_code == 0, r.output
    assert "materialized=3" in _verdicts(r.output)[-1]
    vol = np.load(roots.data / "datasets" / "knee" / "cache" / "materialize" / "npy" / "1.2.2" / "1.2.2.1.npy")
    assert vol.shape == (3, 16, 16)
    r = runner.invoke(app, ["data", "materialize", "--name", "knee", "--mode", "png", "--resize", "8"])
    assert r.exit_code == 0 and "materialized=7" in _verdicts(r.output)[-1]
    r = runner.invoke(app, ["data", "validate", "--name", "knee"])
    assert r.exit_code == 0
```

Run: `uv run pytest tests/unit/test_e2e_flow.py -q`
Expected: PASS。

- [ ] **Step 3: 文件**

`README.md` 換成：

````markdown
# vcp — vision contest pipeline

可重複使用的影像競賽框架：標準資料格式、多重驗證集切分、lineage、進場稽核、materialize 快取；後續子專案接量測護欄與提交治理。設計文件見 `docs/superpowers/specs/`，操作慣例見 `CLAUDE.md`。

```bash
uv sync                      # 核心 venv；DICOM 支援：uv sync --extra dicom
uv run vcp --help
uv run pytest --cov=vcp
```

## 資料層命令

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp data import` | 原始資料 → `dataset.yaml` + `samples.jsonl` | `--importer`、`--src`、`--name`、`--license`、`--url`、`--downloaded-at`、`--opt k=v`、`--raw-manifest full\|sizes` |
| `vcp data validate` | 重驗 card、samples 與 hash | `--name` |
| `vcp data audit` | 座標 sanity、近重複與 test 重疊、來源檢查 | `--against`、`--max-bad-boxes`、`--min-box-px`、`--max-aspect`、`--max-cover`、`--hamming`、`--corr` |
| `vcp data split` | 固定多子集 plan（進 git、不可改） | `--plan-id`、`--subsets`、`--stratify-key`、`--group-key`、`--group-from-audit`、`--strategy` |
| `vcp data lineage` | 某訓練用了哪些子集 → 哪些驗證集還乾淨 | `--plan`、`--trained-on` |
| `vcp data export` | 子集 → COCO / YOLO 目錄 + manifest | `--plan`、`--subset`、`--format`、`--out`、`--opt view=`、`--opt copy=true`、`--unseal --reason` |
| `vcp data materialize` | 每個 view 解碼一次成 npy / png 快取 + manifest | `--mode`、`--resize`、`--stack-seq`、`--window`、`--workers`、`--force`、`--decoder` |

每個命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；`--json` 時結果到 stdout、VERDICT 到 stderr。

## 匯入器與 `rows_read` 的語意

| 匯入器 | 來源 | `rows_read` 數的是 |
|---|---|---|
| `jsonl` | 已是標準格式的 `samples.jsonl` | sample 列 |
| `csv_boxes` | 一列一框的 CSV + 影像目錄 | CSV 資料列 |
| `coco` | instances JSON + 影像目錄 | annotations |
| `yolo` | `images/` + `labels/*.txt` + 類別表 | 影像 |
| `imagefolder` | `root/<class>/*` | 影像 |
| `image_csv` | CSV 路徑欄 + 標籤 / 目標欄 | CSV 資料列 |
| `dicom` | DICOM 目錄樹（header 分組） | 掃到的檔案 |

所有影像匯入器接受 `--opt exif=stored|oriented`（預設 `stored`），Orientation ≠ 1 的 view 會記進 `view.meta.exif_orientation` 並讓 VERDICT 帶 `exif_rotated=<n>` WARN；`materialize --mode png` 是唯一會把方向烙進像素的步驟。

## DICOM 形態的用法（以 RSNA Knee 為例）

```bash
uv run vcp data import --importer dicom --src C:/vcp-data/raw/rsna-knee/train_series --name rsna-knee \
  --license "Competition rules" --url https://www.kaggle.com/competitions/rsna-knee-abnormality-detection \
  --downloaded-at 2026-09-03 \
  --opt labels_csv=../train.csv --opt "target_cols=ACL,MCL,Medial Meniscus,Lateral Meniscus,Medial OA,Lateral OA,PF OA,Effusion,Synovitis,Baker's,Contusion,Fracture" \
  --opt meta_cols=Report --opt seq_csv=../train_series.csv --opt "seq_cols=Fluid_Sensitive,Fat_Suppression,Anatomical_Plane" \
  --opt role_from=Anatomical_Plane --opt workers=8
uv run vcp data materialize --name rsna-knee --mode png --resize 256 --workers 4
uv run vcp data materialize --name rsna-knee --mode npy --stack-seq --workers 4
```

`sample_level=study|series`、`view_level=slice|series`、`group_from=<tag>`（預設 PatientID）、`glob=`、`tags=` 為其餘選項。快取在 `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/`，`manifest.jsonl` 內路徑皆為相對路徑。

### 在 Kaggle notebook 上跑

```bash
pip install git+https://github.com/eric20041027/Vision-contest-pipeline
export VCP_DATA_ROOT=/kaggle/working/vcp-data VCP_CONFIGS_ROOT=/kaggle/working/vcp-configs
vcp data import --importer dicom --src /kaggle/input/<comp>/train_series --name <name> ... --raw-manifest sizes
vcp data materialize --name <name> --mode png --resize 256 --workers 4
```

把 `vcp-data/datasets/<name>/`（含 `cache/materialize/`）與 `vcp-configs/datasets/<name>/` 打包成 Kaggle Dataset 下載到本機同樣的相對位置即可沿用。`--raw-manifest sizes` 只記檔名與大小，card 的 `raw_manifest_mode` 會如實記錄。
````

`tests/integration/README.md` 末尾加：

```markdown
## 準備 RSNA Knee 子集（一次）

1. `uvx --from kaggle python projects/rsna-knee/list_files.py`（列出 82 萬筆檔案清單，可中斷續跑）。
2. `uvx --from kaggle python projects/rsna-knee/download_subset.py --extra 142 --workers 6`（58 個 gold study + 142 個隨機 study，約 26 GB，保留 `train_series/<study>/<series>/*.dcm` 佈局；CSV 已在 `raw/rsna-knee/`）。
3. 以 README 的 `vcp data import --importer dicom ...` 匯入為 `rsna-knee`。
4. `uv run pytest tests/integration -m realdata`。

期望值由 CSV 推算（子集內的 study 數、十二欄皆有值的 gold 數、每 study 的 series 數），不寫死。
```

`CLAUDE.md` 的「常用命令」加一行：`- uv run vcp data materialize --name X --mode npy|png [--resize L] [--stack-seq]`；「路徑」段補：`datasets/<name>/cache/materialize/<mode>/` 是可搬移的解碼快取，`manifest.jsonl` 為權威對照。

- [ ] **Step 4: 最終驗證**

Run:
```bash
uv run pytest -q
uv run pytest --cov=vcp -q | tail -3
uv run ruff check . && uv run ruff format --check .
uv run vcp data materialize --help
uv run vcp data import --help | grep -c raw-manifest
```
Expected: 全綠；TOTAL ≥ 80%（預期仍在 90% 以上）；`--help` 列出 materialize 選項。若覆蓋率低於 Plan 2a 的 96% 超過 2 個百分點，列出未覆蓋的行給審查者（不強求補齊）。

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_rsna_knee.py tests/integration/README.md tests/unit/test_e2e_flow.py README.md CLAUDE.md
git commit -m "test(integration)+docs: RSNA Knee 真資料測試、dicom→materialize 端到端流程、README 命令總表與 Kaggle 用法

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 自我審查紀錄（撰寫計畫時）

- **Spec 覆蓋**：§15.1（Task 1、2）、§15.2（Task 3）、§15.3（Task 4 排序 + Task 5）、§15.4（Task 4、6；-23 在 Task 8 README）、§15.5（Task 1）、§15.6（Task 4 夾具、Task 8 整合測試與驗收 12 到 15：12 → Task 8 Step 1；13 → Task 6 test_png_resize_then_skip_then_force；14 → Task 3 Step 5；15 → Task 2 Step 4）、§15.7（Task 7；-33 在 Task 8 README）、§15.8 未動。
- **計畫層決定（spec 沒寫、實作需要）**：`--resize` 只對 png（npy 保留原解析度）；png 模式遇到體積（series 層級 view 或堆疊）記 failed 而非另開目錄；materialize 的 `--workers` 預設 1；`dicom` 匯入器沒有 `labels_csv` 時 `task` 預設 `multilabel`（可用 `--opt task=regression`）；series 層級 view 的 `seq_index` 為 None；`ManifestRow` 多 `bytes` 欄位供 skip 判斷。這些寫進 Plan 2b 後記供 spec v5 收錄。
- **型別一致性**：`ExportOutput`（Task 2）→ Task 7 使用同名；`Decoded` / `decoder_for(path, override)`（Task 4）→ Task 6 呼叫 `decoder_for(Path, spec.decoder)`；`read_headers(paths, extra=, workers=)`（Task 4）→ Task 5 以關鍵字呼叫；`finalize_import(..., exif_policy, exif_rotated, extra_fields)` 在 Task 1 / 2 / 5 累加參數，皆為關鍵字且有預設值。
