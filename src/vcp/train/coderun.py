"""The code a ``vcp train run`` attempt runs (spec 2026-10-31 §5.1 steps 3-5 and 8-9, §4.3).

Before the first write: the training venv's vcp must share this vcp's MINOR (``check_child_vcp``),
and the roots are planned and scanned in memory (``plan_code``) -- a ``--code-root`` that is not a
directory is bad input, every other problem with a root is only that root's ``reason``. Then, under
``own_writes``, each scanned root's snapshot is written or reused (``write_all``); the attempt's
``env.<n>.json`` records them (``code_section``) and the run binds every snapshot as a
``code.<label>`` ref (``attach_code_refs``). A root without a snapshot is a WARN, never a reason
not to train: that refusal belongs to a run that carries an authorization.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import vcp
from vcp.code import rules
from vcp.code.roots import (
    PlannedRoot,
    checkpoint_exclusions,
    generated_exclusions,
    plan_roots,
    skip_dirs_for,
)
from vcp.code.scan import RootScan, scan_root
from vcp.code.schema import CodeRoot, CodeSection, RootReason
from vcp.code.snapshot import SnapshotOutcome, write_snapshot
from vcp.core.errors import ValidationFailed
from vcp.core.paths import path_key, store_path
from vcp.data.evidence import artifact_ref
from vcp.data.evidence_ref import CODE_ROLE
from vcp.train.env import ProbeReport
from vcp.train.records import bind_ref
from vcp.train.schema import TrainRecord

__all__ = [
    "CodePlan",
    "RootResult",
    "SnapshotState",
    "attach_code_refs",
    "check_child_vcp",
    "code_section",
    "plan_code",
    "snapshot_state",
    "untracked_notice",
    "untracked_total",
    "warnings",
    "write_all",
]

SnapshotState = Literal["ok", "partial", "none"]
COMMAND = "train.run"  # who holds a snapshot's lock, as the lock file says
NOTICE_PATHS = 5  # untracked paths the line before the child names; the count stays exact
_MINOR = re.compile(r"^(\d+)\.(\d+)")


@dataclass(frozen=True)
class CodePlan:
    """The roots of one attempt, their in-memory scans (same order) and the directories the scans
    left out because a checkpoint glob or a generated-code location put them there."""

    roots: list[PlannedRoot]
    scans: list[RootScan]
    excluded_dirs: list[Path]


@dataclass(frozen=True)
class RootResult:
    """One root after ``write_all``: ``outcome`` is None when the scan already had a reason."""

    root: PlannedRoot
    scan: RootScan
    outcome: SnapshotOutcome | None

    @property
    def snapshot(self) -> str | None:
        """The snapshot id the root has, None when it has none."""
        if self.outcome is None or self.outcome.reason is not None:
            return None
        return self.outcome.snapshot_id

    @property
    def reason(self) -> RootReason | None:
        """Why the root has no snapshot: the scan's reason, else the write's."""
        if self.scan.reason is not None:
            return self.scan.reason
        return None if self.outcome is None else self.outcome.reason


# -- before the first write -----------------------------------------------------------------------


def _minor(version: str) -> str:
    """``MAJOR.MINOR`` of a version; the whole string when it does not start with one."""
    found = _MINOR.match(version)
    return f"{found[1]}.{found[2]}" if found else version


def check_child_vcp(report: ProbeReport, venv: Path | None) -> None:
    """spec §5.1 step 3: a training venv whose vcp is another MINOR cannot read what this vcp
    writes (its ``Session`` would fail at the first checkpoint), so the run stops here. Only with
    ``--venv``: without it the child runs in this environment, this vcp."""
    child = report.child_vcp
    if venv is None or child is None:
        return
    ours = vcp.__version__
    if _minor(child) != _minor(ours):
        raise ValidationFailed(
            f"vcp_mismatch: the training venv has vcp {child}, this vcp is {ours}; "
            f"install the same MINOR in {venv}",
            fields={"child_vcp": child},
        )


def _once(directories: Sequence[Path]) -> list[Path]:
    """``directories`` without a second spelling of one path, in order."""
    kept: dict[str, Path] = {}
    for directory in directories:
        kept.setdefault(path_key(directory), directory)
    return list(kept.values())


def plan_code(
    *,
    cwd: Path,
    command: list[str],
    report: ProbeReport,
    code_roots: list[Path],
    patterns: list[str],
    data_root: Path,
    configs_root: Path,
) -> CodePlan:
    """spec §5.1 steps 4-5: the roots (``cwd``, ``script``, the probe's editables, ``code_roots``)
    and their scans, read-only. Each scan skips the data and configs roots, every root nested in
    it, and the checkpoint and generated-code directories (§4.2); bytes are kept ``capped``."""
    for path in code_roots:
        if not Path(path).is_dir():
            raise ValidationFailed(f"not_found: --code-root {path} is not a directory")
    planned = plan_roots(cwd=cwd, command=command, editables=report.editables, extra=code_roots)
    excluded = _once(
        [*checkpoint_exclusions(patterns, cwd, planned), *generated_exclusions(planned)]
    )
    fixed = [Path(data_root), Path(configs_root), *excluded]
    scans = [
        scan_root(
            root.path,
            kind=root.kind,
            skip_dirs=skip_dirs_for(planned, i, fixed=fixed),
            keep="capped",
        )
        for i, root in enumerate(planned)
    ]
    return CodePlan(roots=planned, scans=scans, excluded_dirs=excluded)


# -- the writes -----------------------------------------------------------------------------------


def write_all(data_root: Path, plan: CodePlan, *, strict: bool) -> list[RootResult]:
    """spec §5.1 step 8: write or reuse the snapshot of every root the scan could read. Without
    ``strict`` a snapshot that cannot be written (``locked``, ``partial``, ``unwritable``,
    ``drift``) is only that root's reason; a committed snapshot of this content that does not
    hold is ``mismatch:`` and a full disk the ``OSError`` ``own_writes`` turns into
    ``disk_full:``, both raised by ``write_snapshot``. ``strict`` -- the fail-closed path of a run
    with an authorization -- arrives with ``--authorization``."""
    if strict:
        raise NotImplementedError("write_all(strict=True) comes with train run --authorization")
    results = []
    for root, scan in zip(plan.roots, plan.scans, strict=True):
        outcome = (
            None if scan.reason is not None else write_snapshot(data_root, scan, command=COMMAND)
        )
        results.append(RootResult(root=root, scan=scan, outcome=outcome))
    return results


def code_section(plan: CodePlan, results: list[RootResult], data_root: Path) -> CodeSection:
    """The ``code`` section of ``env.<n>.json`` (spec §4.3): counts are exact, the path lists keep
    the first ``rules.LISTED_PATHS``."""
    listed = rules.LISTED_PATHS
    return CodeSection(
        rules=rules.RULES_VERSION,
        excluded_dirs=[store_path(directory, data_root) for directory in plan.excluded_dirs],
        roots=[
            CodeRoot(
                label=r.root.label,
                source=r.root.source,
                kind=r.root.kind,
                path=store_path(r.root.path, data_root),
                commit=r.root.commit,
                snapshot=r.snapshot,
                reason=r.reason,
                files=r.scan.files,
                untracked_code=len(r.scan.untracked_code),
                untracked_code_paths=list(r.scan.untracked_code[:listed]),
                excluded=len(r.scan.excluded_paths),
                excluded_paths=list(r.scan.excluded_paths[:listed]),
            )
            for r in results
        ],
    )


def attach_code_refs(
    data_root: Path, record: TrainRecord, results: list[RootResult], attempt: int
) -> TrainRecord:
    """Bind every root's snapshot to the run as ``code.<label>`` (spec §4.3), beside the
    ``--evidence`` refs: a snapshot the run already has under that name adds no row and no
    event, so a ``--resume`` on the same code leaves the list as it was."""
    for r in results:
        if r.snapshot is None:
            continue
        ref = artifact_ref(
            data_root,
            "code_snapshot",
            r.snapshot,
            name=f"code.{r.root.label}",
            role=CODE_ROLE,
            attempt=attempt,
            binding="cli",
        )
        record, _ = bind_ref(data_root, record, ref, attempt)
    return record


# -- what the VERDICT says ------------------------------------------------------------------------


def snapshot_state(results: list[RootResult]) -> tuple[SnapshotState, list[str]]:
    """``ok`` when every root has a snapshot, ``none`` when none has (or there is no root),
    ``partial`` otherwise; and the labels of the roots without one."""
    missing = [r.root.label for r in results if r.snapshot is None]
    if not results or len(missing) == len(results):
        return "none", missing
    return ("partial" if missing else "ok"), missing


def untracked_total(results: list[RootResult]) -> int | None:
    """The untracked ``CODE`` files of every git root, summed; None when no root is a git root."""
    git_roots = [r for r in results if r.root.kind == "git"]
    if not git_roots:
        return None
    return sum(len(r.scan.untracked_code) for r in git_roots)


def warnings(state: SnapshotState, missing: list[str], untracked: int | None) -> list[str]:
    """The WARN lines about a run's code: a root without a snapshot, and untracked code that went
    into a snapshot (spec 2026-10-31 §5.1). The arguments are what ``snapshot_state`` and
    ``untracked_total`` said, so the VERDICT and its warnings read the same values."""
    lines = []
    if state != "ok":
        lines.append(f"code_snapshot={state} (code_missing={','.join(missing)})")
    if untracked:
        lines.append(f"untracked_code={untracked}")
    return lines


def untracked_notice(results: list[RootResult]) -> str | None:
    """The line printed before the child starts when untracked code went into the snapshot."""
    count = untracked_total(results)
    if not count:
        return None
    paths = [f"{r.root.label}:{path}" for r in results for path in r.scan.untracked_code]
    shown = ", ".join(paths[:NOTICE_PATHS])
    return f"untracked_code={count}: {shown} (in the snapshot; git add or ignore them)"
