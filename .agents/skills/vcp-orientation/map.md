# vcp 地圖：檔案落點、文件與版本

## 兩個根目錄
- `VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`；比賽工作區常自帶 `<repo>/vcp-data`）：
  `raw/<name>/`、`datasets/<name>/{samples.jsonl,raw_manifest.txt,cache/}`、`runs/<run>/`、`measure/<name>/`、
  `submit/<test>/<id>/`、`artifacts/<kind>/<id>/`、`indexes/provenance.sqlite3`、`logs/`。不進 git。
- `VCP_CONFIGS_ROOT`（預設 repo 的 `configs/`）：`datasets/<name>/{dataset.yaml,splits/*.json,prereg/*.yaml,prereg.log.jsonl,fuse/*.yaml,submit.yaml,submissions.jsonl,backup/*.json,backup.log.jsonl}`。進 git。
- 比賽膠水：`projects/<contest>/`（README / RUNBOOK / DESIGN、prepare.py、train.py、predict.py、metrics 模組、notebook 打包腳本、`requirements-*.txt`、專案 `.venv`）。

## `runs/<run>/`
`run.yaml`（身分、`trained_on`、`access` 收據、`source.weights_hash`）、`predictions/<subset>.jsonl`、`history.jsonl`、`train.yaml` + `train.log.jsonl` + `train/{config.N.json,env.N.json,console.N.log}`、`fuse.json`（融合 run）。

## `measure/<name>/`
`readings.jsonl`、`judgements.jsonl`、`sigma.jsonl`（只增）；`anchors.json`（整份換寫，先寫 `anchors.log.jsonl`）。

## artifacts 的 kind
`access_receipt`（存取器收據）、`source_audit`（逐列 sha 索引）、`dataset_diff`（版本差異，`changes.jsonl` + `summary.json`）、`provenance_policy`（adaptive policy）、比賽自訂 kind。目錄有 `spec.json` 沒 `manifest.json` = 半途；`failure.json` = 例外離開；`supersession.jsonl` 只增。

## 十二個登記表（加一種形態 = 加一個登記項）
任務 `register_task`、匯入器 `register_importer`、匯出器 `register_exporter`、解碼器 `register_decoder`、切分策略（函式）、稽核 `register_check`、轉換器 `register_converter`、指標 `register_metric`、σ_p 方法 `register_sigma_method`、融合器 `register_fuser`、輸出格式 `register_writer`、平台 `register_platform`；另有 provenance 的 `register_impact_policy`。比賽專屬的以 `--plugin projects.<contest>.<module>` 匯入登記。

## 文件
| 問題 | 讀 |
|---|---|
| 某命令的選項 | `--help`，其次 `docs/reference/cli.md` |
| 資料改版與索引語意 | `docs/guides/DATASET_EVOLUTION_PROVENANCE.md` |
| PostgreSQL 安裝、service、flags、修復 | `docs/guides/POSTGRESQL_PROVENANCE.md` |
| 圖解 | `docs/guides/VCP_VISUAL_GUIDE.md` |
| 某層為什麼這樣設計 | `docs/superpowers/specs/<date>-vcp-<layer>-design.md` |
| 合併後的裁決與開放待辦 | `docs/superpowers/plans/*-followups.md` 最後一節 |
| 現況、程式碼地圖、陷阱 | `docs/handover/HANDOVER.md`；provenance 另有 `docs/handover/POSTGRESQL_ADAPTIVE_PROVENANCE_HANDOFF.md` |
| PostgreSQL 研究的證據與數字 | `docs/benchmarks/postgres-provenance-v1.md`（acceptance record）、`-report-v1.md`（十項總結）、`-course-brief-v1.md`（簡報素材） |
| 稽核發現與 wave 規劃 | `docs/audits/2026-09-11-vcp-improvement-audit.md` |
| 起點與教訓 | `docs/postmortems/2026-08-aidea-marine-debris-detection.md` |
| 版本規則與發版四步 | `CHANGELOG.md` 表頭 |

## 版本與 build string
`src/vcp/__init__.py` 的 `__version__` 是唯一來源。產物寫 `vcp.core.build.build_string()`：`0.8.1`（發版 wheel）、`0.8.1+g<commit>`（checkout）、`…dirty`（有未提交變更）。`vcp version` 印同一字串。MINOR = 產物／台帳內容或 CLI 契約改變；PATCH = 其餘。

## 歷史（看 CHANGELOG 才是權威）
0.2 資料層 → 0.3 量測 → 0.4 不可變產物（Wave 1a）→ 0.5 access receipt（1b-1）→ 0.6 source audit（1b-2）→ 0.7 dataset evolution provenance → 0.8 PostgreSQL adaptive provenance → 0.8.1 串流 replay、`python -m vcp`、receipt nonce、開源門面 → 0.9.0 `vcp provenance graph` 與 graph skill → 0.9.1 skill 打包成 Claude Code plugin `vcp`（別的專案用 `/vcp:<skill>`）、SQLite 索引保存 graph gaps（舊索引 rebuild 一次）。Wave 1c（程式碼快照與授權）尚未做。
