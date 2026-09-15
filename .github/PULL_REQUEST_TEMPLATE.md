## Summary

<!-- What changes, and why. If it fixes an issue, link it. -->

## Type of change

<!-- Delete what does not apply. A change to what gets written into an artifact or
     ledger, or to the CLI contract, is MINOR; everything else is PATCH. -->

- [ ] Bug fix (PATCH)
- [ ] New feature or CLI/artifact contract change (MINOR)
- [ ] Documentation only
- [ ] Refactor, tests or tooling

## Checklist

- [ ] `uv run pytest --cov=vcp` passes
- [ ] `uv run ruff check . && uv run ruff format --check .` passes
- [ ] New behaviour has a test that failed before the change
- [ ] No existing assertion was weakened (or the pull request explains why it was wrong)
- [ ] No credential is read, written, logged or added as an option
- [ ] No competition name added under `src/vcp`
- [ ] Tests do not touch a real data root
- [ ] `CHANGELOG.md` updated if this is a release-worthy change

## Verification

<!-- The commands you ran and the VERDICT lines or test output they produced. -->
