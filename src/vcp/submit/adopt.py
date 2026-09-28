"""``vcp submit ledger adopt`` (spec 2026-09-28 §4.1): the shared ledger's first content, merged
once from checkouts' configs ledgers inside the shared ledger's lock. The sources are left as
they are; vcp stops reading them once ``ledger: shared`` is set, and whether git keeps them is a
person's call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import ledger_lock, shared_ledger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow


@dataclass(frozen=True)
class AdoptResult:
    path: Path
    rows: int
    sources: int
    duplicates: int


def merge_ledgers(sources: list[list[LedgerRow]]) -> tuple[list[LedgerRow], int]:
    """Every source's rows; a row identical in every field is kept once (checkouts share the
    history git gave them). Rows are concatenated in ``--from`` order, then stably sorted by
    ``ts``: two rows with equal ``ts`` therefore keep source order first, and the order within a
    source second. Two different ``staged`` rows of one id are ``ledger_conflict:``. Returns
    ``(rows, duplicates dropped)``."""
    seen: set[str] = set()
    merged: list[LedgerRow] = []
    for rows in sources:
        for row in rows:
            key = row.model_dump_json(exclude_none=True)
            if key not in seen:
                seen.add(key)
                merged.append(row)
    duplicates = sum(len(rows) for rows in sources) - len(merged)
    staged: dict[str, int] = {}
    for row in merged:
        if row.event == "staged" and row.submission_id:
            staged[row.submission_id] = staged.get(row.submission_id, 0) + 1
    conflicts = sorted(sid for sid, n in staged.items() if n > 1)
    if conflicts:
        raise ValidationFailed(
            f"ledger_conflict: {len(conflicts)} id(s) staged differently in the sources: "
            f"{', '.join(conflicts)}; nothing was written"
        )
    return sorted(merged, key=lambda r: parse_stamp(r.ts)), duplicates


def _source(path: Path) -> list[LedgerRow]:
    """A ledger file that parses whole: a torn row fails here too (``bad ledger row``)."""
    if not path.is_file():
        raise ValidationFailed(f"not_found: ledger source {path}")
    return SubmissionLedger(path).rows


def adopt(
    dataset: str,
    *,
    sources: list[Path] | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> AdoptResult:
    """Every check before the one write: ``not_shared:``, ``exists:``, sources that parse, no
    ``ledger_conflict:``; then ``.tmp`` and one rename (``write_once_text``). The merged ``ts``
    never runs backwards, so ``backup verify`` has nothing to say about it.

    Each source is read under its own lock too -- ``lock_path(data_root, "submissions", p)``,
    the very file a sibling checkout's own transaction on that source computes (the hash is of
    the source's own resolved path, and the lock lives under the shared data root, so it does
    not matter which checkout takes it): without that lock, a sibling still writing its
    ``configs`` ledger could hand adopt a torn row or a snapshot silently missing one, and adopt
    only gets one shot -- a retry is ``exists:``. One source is locked, read and released before
    the next, never two at once; a deadlock is therefore impossible because every multi-lock
    holder here takes the target lock first."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    if profile.ledger != "shared":
        raise ValidationFailed(
            f"not_shared: submit.yaml says ledger: {profile.ledger}; adopt makes the shared "
            "ledger, so set ledger: shared first",
            fields={"ledger": profile.ledger},
        )
    target = shared_ledger(paths)
    froms = [Path(p) for p in sources] if sources else [paths.submissions_log]
    with ledger_lock(paths, target, command="submit.ledger.adopt"):
        if target.exists():
            raise ValidationFailed(
                f"exists: {target}; the shared ledger is adopted once", location=str(target)
            )
        parsed: list[list[LedgerRow]] = []
        for p in froms:
            # Lock order: the target above, then one source at a time -- never two locks held
            # at once, so this can never deadlock against another command's own transaction.
            with ledger_lock(paths, p, command="submit.ledger.adopt"):
                parsed.append(_source(p))
        rows, duplicates = merge_ledgers(parsed)
        write_once_text(target, "".join(r.model_dump_json(exclude_none=True) + "\n" for r in rows))
    return AdoptResult(target, len(rows), len(froms), duplicates)
