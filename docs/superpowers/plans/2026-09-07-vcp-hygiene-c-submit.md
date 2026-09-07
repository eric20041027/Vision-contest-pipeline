# Hygiene C — submission-layer leftovers (Plan 6 §6 items 9 and 10, the parts Plan 6c left)

Source: `docs/superpowers/plans/2026-09-06-vcp-plan6-followups.md` §6 items 9–10 and §8's "未做" lists. Spec: `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md` (§6.2 upload order, §7 pairing, §8 gate, §9 writers, §17 補充決定). Read the code an item touches before changing it; the items state the decision, not the code.

Conventions that bind: clock only via `vcp.core.time`; VERDICT/exit-code contract; `reason=` vocabulary; ledger rows append-only; no Click-level validation; tests use `roots` / `make_pair` fixtures and never the real data root; `newline="\n"` when rewriting a ledger; `uv run ruff check .` and `uv run ruff format --check .` clean; coverage ≥ 80 %; never `ruff format` markdown. TDD per item.

## Commit 1 — `test(submit): 補 columns 重名表頭、巢狀融合配對、同 prereg 判兩次、allow_missing WARN、融合 test run 的 verify`

- **C1a** `columns` duplicate header: whichever writer(s) accept a `columns` option (read `src/vcp/submit/writers/*.py`): a duplicated column name must be a `ValidationFailed` naming the duplicate at stage time (add the check if it is missing); test through the writer and through `vcp submit stage --writer-opt columns=...`.
- **C1b** nested fusion pairing: a fusion recipe whose member is itself a fusion run (build both sides — eval and test — with the fuse fixtures or by hand as `tests/backup_fixtures.make_fusion` does) → `verify_pairing` recurses and passes when the leaves match, and fails with the leaf's run in `fields` when a leaf differs. If the pairing code does not recurse today, make it (the spec §7 says fusion pairing is per member, recursively).
- **C1c** the same prereg judged twice: `gate.admit` uses the LATEST judgement of a prereg (a PASS then a FAIL → not admitted; a FAIL then a PASS → admitted). Pin it with a test (fix if it reads the first).
- **C1d** `allow_missing` WARN: stage with a writer option that allows missing predictions and a test run that lacks a prediction for one sample → stage succeeds with `status=WARN`, `missing=1` in the VERDICT (Plan 6c added the field) and the warning line. Test at the CLI level.
- **C1e** `submit verify` on a fusion test run: stage a submission whose test run is a fusion (`vcp fuse build` on the test side, or the hand-built equivalent) and run `vcp submit verify --id …` → exit 0 with `checks=` covering the fusion rebuild check; then tamper a member's prediction file → verify FAILs naming the member. If `verify` does not cover fusion members today, note what it does and pin that instead (do not add a feature; report it).
- **C1f** RSNA integration test `tests/integration/test_rsna_knee_submit.py`: inspect the RSNA Knee competition's expected submission id (study id vs. view stem) against the dataset card; if the current `scores_csv` id choice is wrong for the competition, switch the test to `id_field=view_stem`; otherwise leave it and say why in the report.

## Commit 2 — `fix(submit): 事件列舉單一來源；upload 的檢查順序照 spec §6.2；ingest 的 weights 衝突訊息提示 omit --weights；e2e 斷言補欄位；註解修正`

- **C2a** `Event` / `EVENTS` / `_REQUIRED` in `src/vcp/submit/schema.py`: derive `EVENTS` from `typing.get_args(Event)` and add an import-time check (or a test) that `_REQUIRED` has exactly those keys; same pattern for the training layer's `EVENTS` if it duplicates a Literal.
- **C2b** `actions.upload` performs its checks in the order spec §6.2 lists them; if the current order differs, reorder (this only changes which `reason=` wins on a compound failure) and pin the order with a test that sets up two failing conditions at once.
- **C2c** `measure/ingest.py`'s weights-hash conflict message ends with "(omit --weights to keep the recorded hash)"; test pins it.
- **C2d** `src/vcp/submit/stage.py` (or wherever it is) — the comment on `samples == rows` says why they are compared (a writer that writes one row per sample must not drop or duplicate); fix if misleading.
- **C2e** `tests/unit/test_e2e_submit.py`: the calls that only assert `exit_code` also assert one VERDICT field each (`_verdict(r.output)`): at least the `stage` calls (`admission=` / `pairing=`), `record` (`quota=`), `score`, `final` (`chosen=`). Keep the file-level `# ruff: noqa: E501` (long CLI argument lists; wrapping is noise).
- **C2f** spec §17: add one bullet documenting the accepted risk "an upload that succeeded on the platform but whose ledger row failed to write is recovered by `sync` as a `foreign` row" (no code change).

## Delivery
- Worktree only; plain single git commands (`git add <files>` then `git commit -m "<message above>" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"`); no `&&` chains with git; no git inside scripts.
- Gate after each commit: `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/measure/test_ingest.py -o addopts="" -q` green and `uv run ruff check . && uv run ruff format --check .` clean. After the second commit: `uv run pytest --cov=vcp` (summary + TOTAL) and `uv run pytest tests/integration -o addopts="" -q -m realdata` (must stay green).
- Do not touch the followups documents (the controller writes the closing section). Return the full report in your final message: per item what changed, the commit shas, tests added/changed, exact commands and summary lines, coverage, integration result, open choices. If an item cannot be done as specified, stop and report NEEDS_CONTEXT with the exact conflict.
