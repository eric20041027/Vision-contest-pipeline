# Contributing to vcp

Thanks for taking the time. This project is small and opinionated, so this document is mostly about the rules the test suite already enforces — knowing them up front saves a review round.

## Development setup

Python 3.12 and [uv](https://docs.astral.sh/uv/) are the only prerequisites.

```bash
git clone https://github.com/eric20041027/Vision-contest-pipeline
cd Vision-contest-pipeline
uv sync
uv run pytest --cov=vcp          # about six minutes; coverage is gated at 80%
uv run ruff check . && uv run ruff format --check .
uv run python examples/quickstart.py
```

`uv sync --extra dicom` adds the DICOM decoders, `--extra postgres` adds the optional PostgreSQL backend. The dev dependency group pins every optional extra, and a test asserts it, so add new extras in both places.

Tests never touch a real data root. Unit tests build their own roots through the `roots` fixture; integration tests read `VCP_REALDATA_ROOT` / `VCP_REALDATA_CONFIGS` and skip when the data is absent. A test that writes into `VCP_DATA_ROOT` will be rejected.

## The rules a reviewer will check

These are not style preferences; most of them are enforced by lint rules or tests.

1. **Clocks.** Only `vcp.core.time.utc_now()` / `stamp()`. Ruff bans `datetime.now`, `utcnow`, `today` and `time.time` everywhere except `src/vcp/core/time.py`.
2. **The VERDICT contract.** Every command ends with `VERDICT cmd=<group>.<name> status=OK|WARN|FAIL|ABORT k=v…`, exit `0/0/1/2`. Plain mode prints VERDICT on stdout; `--json` puts the result JSON on stdout and VERDICT on stderr. Commands never prompt, and range checks live in the function layer so a bad value still produces a VERDICT.
3. **Error vocabulary.** `ValidationFailed` / `IntegrityError` / `PlatformError` are FAIL; `VcpError` / `PlanMismatchError` / `RegistryError` are ABORT. Messages start with a `reason=` token such as `not_found:`, `exists:`, `mismatch:`. `VcpError.fields` holds machine-readable keys only.
4. **Privacy.** vcp never reads, writes, validates or logs a credential. No token options, no credential fields. Every byte of third-party CLI output passes through `vcp.core.proc.redact` before it lands.
5. **Immutability.** Ledgers are append-only. Split plans, pre-registrations, fusion recipes and backup manifests are written once; a change means a new id. Artifacts publish `manifest.json` last — without it there is no artifact.
6. **Genericity.** `src/vcp` never contains a competition name. New task types, importers, exporters, metrics, σ_p methods, fusers and platforms are registry entries, not schema or CLI changes. Contest-specific code belongs in `projects/<contest>/` and loads through `--plugin`.
7. **Encoding.** UTF-8 and LF. Tests that rewrite a ledger or card must pass `newline="\n"`, or CRLF will break the hashes on Windows.

## Making a change

- Branch per change, one logical change per commit, message as `type(scope): description` (`feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `ci`). Please don't use `git add -A`.
- Write the failing test first, watch it fail, then implement. Don't weaken an existing assertion to make something pass — if the assertion is wrong, say why in the pull request.
- Run the three gates above before pushing. CI runs the same ones on Linux and Windows.
- Changes that alter what gets written into an artifact or ledger, or that change the CLI contract, are a MINOR version bump; everything else is PATCH. The release steps are at the top of [CHANGELOG.md](CHANGELOG.md).
- Never run `ruff format` on Markdown.

## Design decisions

Larger changes get a design document in `docs/superpowers/specs/` before implementation, and an execution postscript in `docs/superpowers/plans/` afterwards recording every ruling as *decision — rationale — cost if wrong*. If you are proposing something structural, open an issue first so we can agree on the shape before you write it.

Most documents under `docs/` are in Traditional Chinese. Contributions in either English or Traditional Chinese are fine; code, identifiers, CLI help and commit subjects are in English.

## Reporting bugs

Please include the full `VERDICT` line, the exit code, the vcp build string from `vcp version`, and your OS. The VERDICT fields usually identify the dataset, run or artifact involved, which is most of the reproduction.
