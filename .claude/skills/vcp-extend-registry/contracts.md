# 六個登記軸的契約

所有簽名以 `src/vcp` 現況為準；本檔只告訴你去哪裡看與框架替你做了什麼。

## 匯入器 `src/vcp/data/importers/`

- 契約（`base.py`）：類別屬性 `name`、`version`；`run(self, spec: ImportSpec) -> ImportResult`。`ImportSpec` 帶 `src`、`name`、`options: dict[str, str]`、`license`、`url`、`downloaded_at`、`notes`、`raw_manifest`、`data_root`、`configs_root`。
- 結尾一律呼叫 `finalize_import(*, spec, importer=self, task, categories, image_root, samples, rows_read, skipped, unlabeled=0, exif_policy="stored", exif_rotated=0, extra_fields=None)`：它驗證 samples、算 `count_invalidated_plans`、寫 `raw_manifest.txt`、組 card（`store_path` 規則）、`Dataset.save`、寫或刪 `cache/import_skipped.jsonl`。
- `skipped` 每列一個 dict，至少 `{"reason": ...}`，其餘鍵自訂（csv_boxes 用 `line`/`row`，dicom 用 `file`/`id`）；`rows_read` 的語意自己定並寫進 README 表。
- 影像形態的 helper 在 `common.py`：`iter_images(root)`（`IMAGE_EXTS`）、`image_header`、`make_view(root, rel, *, exif_policy)`、`read_csv(path, *, required)`、`load_categories(value, base)`、`choice_option`、`exif_policy_option`、`count_exif_rotated`。sample_id 慣例 = `view.path`（相對 image_root 的 posix 路徑）。
- `label_source`：人工標籤 `gold`；程式推導 `derived`；沒標籤 `none` 且 `labels=None`（schema 強制一致）。eval / sealed 子集只收 gold。
- 登記：`importers/__init__.py` `register_importer(XImporter())`。測試：`tests/unit/data/importers/test_<名>.py`，用 `ImportSpec(..., data_root=roots.data, configs_root=roots.configs)`。

## 匯出器 `src/vcp/data/exporters/`

- 契約（`base.py`）：`name`、`version`；`run(self, dataset, samples, out, image_root, options) -> ExportOutput`，其中 `ExportOutput(files: list[Path], warnings: list[str] = [], fields: dict[str, FieldValue] = {}, manifest: dict = {})`（frozen dataclass）。`FieldValue = str | int | float | bool`。
- 框架（`export_subset`）替你做：載入 dataset 與 plan、`Dataset.subset()`（含 sealed 開封與留痕）、輸出目錄必須為空、呼叫 `run`、空子集 WARN、EXIF 欄位、寫 `manifest.json`（`dataset`、`samples_hash`、`plan_id`、`subset`、`format`、`exporter_version`、`exported_at`、`sample_count`、`exif_policy`、`exif_rotated`、你的 `manifest` 鍵、`files: {相對路徑: sha256}`；基底鍵優先）。CLI 把 `fields` 以 `setdefault` 併進 VERDICT（基底欄位優先），有 warnings 就 WARN。
- 你要做：檢查 `dataset.card.task`（不支援 → `ValidationFailed`）、`select_view`、寫檔、回傳所有檔案。影像放置可參考 `yolo._place_image`（symlink → 權限不足退回複製並 warning；`--opt copy=true` 直接複製；VERDICT 欄位 `images=copied|symlinked`）。
- 登記：`exporters/__init__.py` `register_exporter(XExporter())`。測試：`tests/unit/data/exporters/test_exporters.py`（`det_ds` fixture、`_spec(roots, fmt, out, subset=, options=)`）。

## 解碼器 `src/vcp/data/materialize/decoders/`

- 契約（`base.py`）：`name`、`version`；`decode(self, path, *, exif_policy="stored") -> Decoded`；`decode_series(self, paths: list[Path], *, exif_policy="stored") -> Decoded`（S×H×W；exif_policy 只對影像解碼器有意義，DICOM 忽略）。`Decoded(array: np.ndarray, info: dict)`，`info` 供 png 映射：`window_center`、`window_width`、`photometric`、`slices`。
- 分派：`_BY_SUFFIX`（`.dcm` → dicom，其餘 → image）；新副檔名 = 在 `_BY_SUFFIX` 加一項 + `register_decoder`。`version` 變了 materialize 會重做快取。
- 缺選用套件 → 在 `decode` 內呼叫類似 `dicomio.require_pydicom()` 的檢查，拋 `VcpError` 並附安裝提示。
- 測試：`tests/unit/data/materialize/test_decoders.py`；夾具 `write_dicom_study`。

## 稽核檢查 `src/vcp/data/audit/`

- 契約（`base.py`）：`name`；`applies(self, dataset) -> bool`（只在資料形態適用時 True，例如 `coords` 看 `get_task(...).label_field in ("boxes","masks")`，`dedup` 要求所有 view 副檔名在 `IMAGE_EXTS`）；`run(self, ctx: AuditContext) -> CheckResult(status, fields, human)`。
- `CheckResult(status: Status, fields: dict[str, FieldValue], human: list[str])`；`Status` 是 `"OK" | "WARN" | "FAIL" | "ABORT"`（`vcp.core.log`），`FieldValue = str | int | float | bool`。
- `AuditContext`：`dataset`、`paths`、`opts: AuditOptions`、`against`、`against_paths`、`out_dir`（= `cache/audit/`）。輸出檔用 `write_jsonl(ctx.out_dir / "<名>.jsonl", rows)`。
- 加門檻選項三步：`AuditOptions`（`audit/base.py`，pydantic、`extra="forbid"`）加欄位含預設；`cli.py` 的 `audit_cmd` 加 `typer.Option("--<名>")` 參數並傳進 `AuditOptions(...)`；測試用 `AuditOptions(<名>=...)` 直接建。
- 框架（`run_audit`）替你做：依登記順序跑 `applies` 為真的檢查、算最差狀態、寫 `summary.json`；CLI 每項印一行 `VERDICT cmd=audit.<名> status=... <fields>`，並在最後一行加 `<名>=<狀態>` 欄位（既有 CLI 測試以字面值斷言這些，登記新檢查要一併更新）。
- 登記：`audit/__init__.py` `register_check(XCheck())`（順序 = 執行順序）。測試：`tests/unit/data/audit/test_checks.py`（`_dataset(roots, name, specs)` helper）。

## 切分策略 `src/vcp/data/split.py`

- 契約：`STRATEGIES: dict[str, Callable]`，值與 `generate_fixed` 同簽名，回傳 `(assignment: dict[sample_id, subset], params: dict)`；`build_plan(..., strategy=)` 依名稱取用，`assert_plan_invariants` 之後驗證：全覆蓋、互斥、group 不跨子集、eval/sealed 只 gold、比例。
- 加策略 = 在 `STRATEGIES` 加一項；CLI `--strategy` 不用改。測試：`tests/unit/data/test_split_generate.py`。

## 任務類型 `src/vcp/data/tasks.py`

- 契約：`TaskSpec(name, label_field, validate(sample, card), stratify_key(sample, card))`；`register_task`。`label_field ∈ {"cls","targets","boxes","masks"}` 決定哪個 `Labels` 欄位必填；`validate` 用 `_fail(sample, msg)` 拋 `ValidationFailed`；`stratify_key` 回傳分層鍵（字串或向量）。
- 新任務要同時決定：哪些匯入器能產生它、`coords` 是否適用、匯出器是否支援。

## 每次擴充的檢查清單

- [ ] 失敗測試先寫（`roots` fixture、`helpers`），再實作，再 `uv run pytest -q`
- [ ] `register_*` 登記 + `__all__`
- [ ] `ValidationFailed` / `VcpError` 分清楚；`choice_option` 驗選項
- [ ] 文字檔 `encoding="utf-8", newline="\n"`；CSV `newline=""` + `lineterminator="\n"`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `cli.py` 的 help 字串、`README.md` 表（含 `rows_read` 語意）、必要時 `CLAUDE.md`
- [ ] `src/vcp` 無比賽名稱與比賽欄名
