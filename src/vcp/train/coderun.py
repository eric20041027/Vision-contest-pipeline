"""The code a ``vcp train run`` attempt runs (spec 2026-10-31 §5.1 steps 3-6 and 8-9, §4.3).

Before the first write: the training venv's vcp must share this vcp's MINOR (``check_child_vcp``),
and the roots are planned and scanned in memory (``plan_code``) -- a ``--code-root`` that is not a
directory is bad input, every other problem with a root is only that root's ``reason``. The names
vcp's own refs will take must be free (``check_ref_names``), and a run with ``--authorization``
computes its claims (``claims_now``) for ``consume``. Then, under ``own_writes``, each scanned
root's snapshot is written or reused (``write_all``); the attempt's ``env.<n>.json`` records them
(``code_section``) and the run binds every snapshot as a ``code.<label>`` ref
(``attach_code_refs``), and the authorization as ``authorization`` (``authorization_ref``). A root
without a snapshot is a WARN, never a reason not to train -- except in a run that carries an
authorization, whose snapshots fail closed (``write_all(strict=True)``). After the child exits,
``changed_since_snapshot`` hashes the files each snapshot lists once more, and ``trace_summary``
reads the import traces the child's ``Session`` left on the attempt (§5.3).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import vcp
from vcp.artifact import store
from vcp.artifact.authorization import KIND as AUTHORIZATION_KIND
from vcp.code import rules
from vcp.code.process import raise_for_outcome
from vcp.code.roots import (
    PlannedRoot,
    checkpoint_exclusions,
    generated_exclusions,
    plan_roots,
    skip_dirs_for,
)
from vcp.code.scan import IndexEntry, RootScan, scan_root, snapshot_json_bytes
from vcp.code.schema import CodeRoot, CodeSection, RootReason, TraceDoc
from vcp.code.snapshot import KIND as SNAPSHOT_KIND
from vcp.code.snapshot import SnapshotOutcome, write_snapshot
from vcp.code.trace import GAP_CLASSES, TRACE_FILE
from vcp.code.trace import KIND as TRACE_KIND
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, path_key, store_path
from vcp.data.evidence import artifact_ref
from vcp.data.evidence_ref import (
    AUTHORIZATION_NAME,
    AUTHORIZATION_ROLE,
    CODE_ROLE,
    CODE_TRACE_ROLE,
    EvidenceRef,
    check_name,
    current,
)
from vcp.data.labels import LABEL_SET_KIND
from vcp.train.authorize import train_claims
from vcp.train.env import ProbeReport
from vcp.train.records import bind_ref
from vcp.train.schema import TrainRecord

__all__ = [
    "CHANGED_PATHS",
    "CodeChange",
    "CodePlan",
    "RootResult",
    "SnapshotState",
    "TraceState",
    "TraceSummary",
    "attach_code_refs",
    "authorization_ref",
    "changed_since_snapshot",
    "check_child_vcp",
    "check_ref_names",
    "claims_now",
    "code_section",
    "plan_code",
    "snapshot_state",
    "trace_summary",
    "untracked_notice",
    "untracked_total",
    "warnings",
    "write_all",
]

SnapshotState = Literal["ok", "partial", "none"]
TraceState = Literal["complete", "incomplete", "none"]
COMMAND = "train.run"  # who holds a snapshot's lock, as the lock file says
NOTICE_PATHS = 5  # untracked paths the line before the child names; the count stays exact
CHANGED_PATHS = 5  # changed paths the code_changed note lists; the count stays exact
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


def _code_ref_name(label: str) -> str:
    return f"code.{label}"


def _check_free(
    existing: list[EvidenceRef], labels: list[str], name: str, role: str, kind: str
) -> None:
    """``name`` can be bound as ``kind`` / ``role``: no ref of the run's records has it under
    another kind or role (``check_name``), and it is not a ``--labels`` id of this call."""
    check_name(existing, name, role, kind)
    if name in labels:
        raise ValidationFailed(
            f"evidence_conflict: {name!r} is both a --labels id and the name of vcp's {kind} ref",
            fields={"evidence_name": name},
        )


def check_ref_names(
    existing: list[EvidenceRef], labels: list[str], plan: CodePlan, authorization: str | None
) -> None:
    """Before the first write: the ``code.<label>`` name of every root that may get a snapshot,
    and with an authorization the name ``authorization``, are free for vcp's refs. A run from
    before 0.18 may hold an ``--evidence code.cwd`` and a label set may be called ``code.cwd`` or
    ``authorization``; ``bind_ref`` would meet the clash only after the run's first writes.
    ``existing`` are the refs of the run's record and card."""
    for root, scan in zip(plan.roots, plan.scans, strict=True):
        if scan.reason is None:
            _check_free(existing, labels, _code_ref_name(root.label), CODE_ROLE, SNAPSHOT_KIND)
    if authorization is not None:
        _check_free(existing, labels, AUTHORIZATION_NAME, AUTHORIZATION_ROLE, AUTHORIZATION_KIND)


def claims_now(
    *,
    dataset: str,
    plan_id: str,
    seed: int | None,
    samples_hash: str,
    trained_on: list[str],
    chash: str,
    command: list[str],
    plan_sha256: str,
    evidence: list[tuple[str, Path]],
    labels: list[str],
    plan: CodePlan,
    report: ProbeReport,
    data_root: Path,
) -> dict[str, str]:
    """spec §5.1 step 6: this run's ``train`` claims (§4.6), computed from its inputs before its
    first write, through ``train_claims`` so they come out byte-equal to a basis run's for the
    same setup. A root's code claim is the sha of the ``snapshot.json`` its snapshot has: the
    bytes depend on the scan's entries alone, so they are known before the snapshot is written.
    A root whose scan has a reason has no code claim, and an authorization that binds it is then
    ``unauthorized:``. Evidence is each ``--evidence`` file's sha, labels each label set's
    manifest sha, ``env:`` the probe's identity."""
    code = {
        root.label: hashlib.sha256(snapshot_json_bytes(scan.entries)).hexdigest()
        for root, scan in zip(plan.roots, plan.scans, strict=True)
        if scan.reason is None
    }
    return train_claims(
        dataset=dataset,
        plan_id=plan_id,
        seed=seed,
        samples_hash=samples_hash,
        trained_on=trained_on,
        config_sha256=chash,
        command=command,
        plan_sha256=plan_sha256,
        code=code,
        evidence={name: sha256_file(path) for name, path in evidence},
        labels={
            label_set_id: sha256_file(store.manifest_path(data_root, LABEL_SET_KIND, label_set_id))
            for label_set_id in labels
        },
        identity=report.identity,
    )


# -- the writes -----------------------------------------------------------------------------------


def write_all(data_root: Path, plan: CodePlan, *, strict: bool) -> list[RootResult]:
    """spec §5.1 step 8: write or reuse the snapshot of every root the scan could read. Without
    ``strict`` a snapshot that cannot be written (``locked``, ``partial``, ``unwritable``,
    ``drift``) is only that root's reason; a committed snapshot of this content that does not
    hold is ``mismatch:`` and a full disk the ``OSError`` ``own_writes`` turns into
    ``disk_full:``, both raised by ``write_snapshot``.

    ``strict`` is a run with an authorization, which fails closed (C3): a snapshot that cannot be
    written raises ``raise_for_outcome``'s refusal -- ``drift:`` FAIL, ``code_unavailable:`` ABORT
    -- before the run has a file. A scan's reason is not raised here: the authorization was held
    against the scans already (a root it binds whose scan failed is ``unauthorized:``), so a root
    it frees keeps the downgrade."""
    results = []
    for root, scan in zip(plan.roots, plan.scans, strict=True):
        outcome = (
            None if scan.reason is not None else write_snapshot(data_root, scan, command=COMMAND)
        )
        if strict and outcome is not None and outcome.reason is not None:
            raise_for_outcome(root.label, outcome.reason)
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
            SNAPSHOT_KIND,
            r.snapshot,
            name=_code_ref_name(r.root.label),
            role=CODE_ROLE,
            attempt=attempt,
            binding="cli",
        )
        record, _ = bind_ref(data_root, record, ref, attempt)
    return record


def authorization_ref(data_root: Path, authorization_id: str, attempt: int) -> EvidenceRef:
    """The run's pin on the authorization it was checked against (spec §5.6): named, kinded and
    roled ``authorization``, bound beside the ``code.<label>`` refs. One authorization serves any
    number of runs, each of which pins it here."""
    return artifact_ref(
        data_root,
        AUTHORIZATION_KIND,
        authorization_id,
        name=AUTHORIZATION_NAME,
        role=AUTHORIZATION_ROLE,
        attempt=attempt,
        binding="cli",
    )


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


def warnings(
    state: SnapshotState,
    missing: list[str],
    untracked: int | None,
    changed: int,
    *,
    trace: TraceSummary,
) -> list[str]:
    """The WARN lines about a run's code: a root without a snapshot, untracked code that went
    into a snapshot (spec 2026-10-31 §5.1), listed files the child changed and an import trace
    with gaps (§5.3). The arguments are what ``snapshot_state``, ``untracked_total``,
    ``changed_since_snapshot`` and ``trace_summary`` said, so the VERDICT and its warnings read
    the same values. No trace is no WARN: most children never use ``Session``."""
    lines = []
    if state != "ok":
        lines.append(f"code_snapshot={state} (code_missing={','.join(missing)})")
    if untracked:
        lines.append(f"untracked_code={untracked}")
    if changed:
        lines.append(f"code_changed={changed}")
    if trace.state == "incomplete":
        lines.append(f"code_trace=incomplete (code_gaps={trace.gaps})")
    return lines


def untracked_notice(results: list[RootResult]) -> str | None:
    """The line printed before the child starts when untracked code went into the snapshot."""
    count = untracked_total(results)
    if not count:
        return None
    paths = [f"{r.root.label}:{path}" for r in results for path in r.scan.untracked_code]
    shown = ", ".join(paths[:NOTICE_PATHS])
    return f"untracked_code={count}: {shown} (in the snapshot; git add or ignore them)"


# -- after the child exits ------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeChange:
    """What the child did to the files its snapshots list (spec §5.3): how many differ or are
    gone, the first ``CHANGED_PATHS`` of them as ``<label>:<path>``, and the labels of the roots
    that hold them, in root order."""

    count: int
    paths: list[str]
    roots: list[str]


def _differs(root: Path, entry: IndexEntry) -> bool:
    """Is the file the index lists gone, or other bytes than the index says? A file that cannot be
    read back differs too: nothing says it is the same."""
    try:
        return sha256_file(root / entry.path) != entry.sha256
    except OSError:
        return True


def changed_since_snapshot(results: list[RootResult]) -> CodeChange:
    """spec §5.3: every file a root's snapshot lists, hashed again. Only the listed files are
    looked at -- a file the child added is not code anybody snapshotted -- and a root without a
    snapshot has nothing to compare. The listing is the scan's entries: the ones the snapshot was
    written from or, on reuse, found to have the same identity as its index. The snapshot
    directory is never read here, so one removed or damaged while the child ran cannot cost the
    run its records (the child's exit code and the checkpoints come first)."""
    count = 0
    paths: list[str] = []
    roots: list[str] = []
    for r in results:
        if r.snapshot is None:
            continue
        label = r.root.label
        differing = [e.path for e in r.scan.entries if _differs(r.root.path, e)]
        if differing:
            count += len(differing)
            paths.extend(f"{label}:{path}" for path in differing[:CHANGED_PATHS])
            roots.append(label)
    return CodeChange(count=count, paths=paths[:CHANGED_PATHS], roots=roots)


@dataclass(frozen=True)
class TraceSummary:
    """The import traces of one attempt (spec §5.3): ``none`` when its children left none,
    ``complete`` when every one reads back complete, ``incomplete`` otherwise; ``gaps`` sums their
    ``outside + unlisted + changed`` (a trace that cannot be read adds none, and is a gap of its
    own kind: nothing says what it held)."""

    state: TraceState
    gaps: int


def _trace_doc(data_root: Path, ref: EvidenceRef) -> TraceDoc | None:
    path = artifact_dir(data_root, TRACE_KIND, ref.artifact_id) / TRACE_FILE
    try:
        return TraceDoc.model_validate_json(path.read_bytes())
    except (OSError, ValueError):  # pydantic's ValidationError is a ValueError
        return None


def trace_summary(data_root: Path, record: TrainRecord, attempt: int) -> TraceSummary:
    """What the ``code_trace`` refs of ``attempt`` say, picked by kind and role."""
    refs = [
        r
        for r in current(record.evidence)
        if r.kind == TRACE_KIND and r.role == CODE_TRACE_ROLE and r.attempt == attempt
    ]
    if not refs:
        return TraceSummary(state="none", gaps=0)
    docs = [_trace_doc(data_root, ref) for ref in refs]
    gaps = sum(doc.counts.get(cls, 0) for doc in docs if doc is not None for cls in GAP_CLASSES)
    complete = all(doc is not None and doc.complete for doc in docs)
    return TraceSummary(state="complete" if complete else "incomplete", gaps=gaps)
