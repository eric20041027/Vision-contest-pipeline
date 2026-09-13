"""``vcp submit stage`` / ``verify`` (spec 6.1): every check, then the file, then the ledger row.

A failed stage leaves nothing: no directory, no row. The three verifications of an artifact's
bytes live here too -- hashed at stage, re-hashed before an upload (actions.py), and re-rendered
from the test-side run by ``verify``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import md5_file, sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.core.time import stamp, utc_now
from vcp.data.access.access import DatasetAccess
from vcp.data.access.schema import GRADE_RANK
from vcp.data.dataset import Dataset
from vcp.data.schema import DatasetCard
from vcp.data.split import load_plan
from vcp.fuse.build import load_record
from vcp.measure.predictions import read_predictions
from vcp.measure.provenance import provenance
from vcp.measure.runs import assert_run_matches, load_run, verify_prediction
from vcp.measure.schema import RunCard
from vcp.submit.gate import admit
from vcp.submit.guards import assert_before_deadline, assert_unlocked
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.pairing import UNCHECKED, is_fusion, verify_pairing, verify_weights
from vcp.submit.profile import load_profile
from vcp.submit.schema import (
    Artifact,
    CandidateKind,
    LedgerRow,
    Pairing,
    PlatformProfile,
    Staged,
    WeightRef,
)
from vcp.submit.writers import writer_for
from vcp.submit.writers.base import WriteContext

STAGE_FILE = "stage.json"
MAX_ID_LENGTH = 31


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    submission_id: str
    eval_run: str
    test_run: str | None = None
    kind: CandidateKind = "candidate"
    reason: str | None = None
    kernel: str | None = None
    version: int | None = None
    output: str = "submission.csv"
    weights: list[str] = Field(default_factory=list)
    writer_opts: dict[str, str] = Field(default_factory=dict)
    data_root: Path | None = None
    configs_root: Path | None = None


@dataclass(frozen=True)
class StageResult:
    staged: Staged
    path: Path
    warnings: list[str]
    identity: str | None = None


def stage_json(paths: DatasetPaths, submission_id: str) -> Path:
    return paths.submission_dir(submission_id) / STAGE_FILE


def load_staged(paths: DatasetPaths, submission_id: str) -> Staged:
    path = stage_json(paths, submission_id)
    if not path.is_file():
        raise ValidationFailed(f"not_staged: {path} does not exist", fields={"id": submission_id})
    try:
        return Staged.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad stage.json: {e}", location=str(path)) from e


def _run_on(
    data_root: Path, run_id: str, dataset_card: DatasetCard, plan_id: str, side: str
) -> RunCard:
    card = load_run(data_root, run_id)
    assert_run_matches(card, dataset_card)
    if card.plan_id != plan_id:
        raise PlanMismatchError(
            f"run {run_id!r} uses plan {card.plan_id!r}; the profile's {side} plan is {plan_id!r}",
            fields={"side": side, "run": run_id},
        )
    return card


def _file_pairing(
    spec: StageSpec,
    profile: PlatformProfile,
    data_root: Path,
    eval_card: RunCard,
    test_card: RunCard,
) -> Pairing:
    if spec.kind == "probe":
        mode = "fusion" if is_fusion(data_root, test_card.run_id) else "single"
        return Pairing(mode=mode, checks=["identity=skipped"])
    return verify_pairing(data_root, eval_card, test_card, test_subset=profile.test_subset)


def _render(
    spec: StageSpec,
    profile: PlatformProfile,
    paths: DatasetPaths,
    test_card: RunCard,
    warnings: list[str],
) -> tuple[Artifact, Path, str]:
    """The submission file, written into a temporary directory that becomes the submission
    directory only once everything else has succeeded. The test subset is read through a
    role-scoped access whose receipt names the candidate's eval run (spec 6.7)."""
    with DatasetAccess.open(
        profile.dataset,
        profile.test_plan,
        subsets={profile.test_subset},
        purpose="submit",
        caller="vcp submit stage",
        run_id=spec.eval_run,
        data_root=paths.data_root,
        configs_root=paths.configs_root,
    ) as access:
        samples = list(access.iter(profile.test_subset))
        test_ds = Dataset(access.card, samples)
        identity = access.identity
    writer = writer_for(profile.writer or "", test_ds.card.task)
    options = {**profile.writer_opts, **spec.writer_opts}
    preds = read_predictions(verify_prediction(paths.data_root, test_card, profile.test_subset))
    tmp = paths.submit_dir / f".tmp-{spec.submission_id}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    out = tmp / writer.file_name
    try:
        res = writer.write(preds, WriteContext(test_ds, samples, options, out))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if res.missing:
        warnings.append(f"{len(res.missing)} samples without a prediction (allow_missing)")
    artifact = Artifact(
        kind="file",
        path=writer.file_name,
        bytes=out.stat().st_size,
        sha256=sha256_file(out),
        md5=md5_file(out),
        writer=writer.name,
        writer_version=writer.version,
        writer_opts=options,
        rows=res.rows,
        samples=res.samples,
        missing=len(res.missing),
    )
    return artifact, tmp, identity


def stage(spec: StageSpec) -> StageResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    profile, profile_sha = load_profile(paths)
    if profile.submission_kind == "file" and (spec.kernel or spec.version or spec.weights):
        raise ValidationFailed(
            "kernel_options: --kernel / --version / --weights apply to kernel submissions; "
            "this profile is submission_kind=file",
            fields={"kind": "file"},
        )
    if profile.submission_kind == "kernel" and spec.test_run:
        raise ValidationFailed(
            "test_run: kernel submissions are not rendered from a test run; drop --test-run",
            fields={"kind": "kernel"},
        )
    ledger = SubmissionLedger(paths.submissions_log)
    assert_unlocked(ledger)
    assert_before_deadline(profile, utc_now())
    validate_name(spec.submission_id)
    if len(spec.submission_id) > MAX_ID_LENGTH:
        raise ValidationFailed(
            f"submission id {spec.submission_id!r} is longer than {MAX_ID_LENGTH} characters "
            "(sync matches ids inside platform descriptions, and redaction hides anything longer)"
        )
    if paths.submission_dir(spec.submission_id).exists() or ledger.staged(spec.submission_id):
        raise ValidationFailed(
            f"exists: submission {spec.submission_id!r} is already staged",
            fields={"id": spec.submission_id},
        )
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    eval_dataset_card = Dataset.load_card(
        profile.eval_dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    roles = {s.name: s.role for s in eval_plan.subsets}
    if roles.get(profile.sealed_subset) != "sealed":
        raise ValidationFailed(
            f"sealed_subset: {profile.sealed_subset!r} is not a sealed subset of plan "
            f"{profile.plan_id!r} (subsets: {roles})"
        )
    eval_card = _run_on(paths.data_root, spec.eval_run, eval_dataset_card, profile.plan_id, "eval")
    if spec.kind == "candidate" and profile.sealed_subset in eval_card.trained_on:
        raise ValidationFailed(
            f"trained_on_sealed: {spec.eval_run!r} trained on {profile.sealed_subset!r}; it can "
            "never have a clean sealed reading",
            fields={"run": spec.eval_run},
        )
    info = provenance(eval_card, data_root=paths.data_root, configs_root=paths.configs_root)
    if spec.kind == "candidate":
        if profile.sealed_subset in info.observed:
            raise ValidationFailed(
                f"observed_sealed: {spec.eval_run!r} read {profile.sealed_subset!r} (access "
                "receipt); it can never have a clean sealed reading",
                fields={"run": spec.eval_run},
            )
        if GRADE_RANK[info.grade] < GRADE_RANK[profile.require_provenance]:
            raise ValidationFailed(
                f"provenance_required: run {spec.eval_run!r} is {info.grade}, profile requires "
                f"{profile.require_provenance}",
                fields={"run": spec.eval_run, "provenance": info.grade},
            )
    warnings: list[str] = []
    test_dataset_card = Dataset.load_card(
        profile.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    test_card: RunCard | None = None
    weights: list[WeightRef] = []
    if profile.submission_kind == "file":
        if spec.test_run is None:
            raise ValidationFailed(
                "test_run required: file submissions are rendered from a test-side run"
            )
        test_card = _run_on(
            paths.data_root, spec.test_run, test_dataset_card, profile.test_plan, "test"
        )
        verify_prediction(paths.data_root, test_card, profile.test_subset)
        pairing = _file_pairing(spec, profile, paths.data_root, eval_card, test_card)
    else:
        if spec.kernel is None or spec.version is None:
            raise ValidationFailed("kernel submissions need --kernel and --version")
        pairing, weights = verify_weights(paths.data_root, spec.eval_run, spec.weights)
    if UNCHECKED in pairing.checks:
        warnings.append("config_hash unchecked (missing on one side)")
    fuse = (
        load_record(paths.data_root, spec.eval_run)
        if is_fusion(paths.data_root, spec.eval_run)
        else None
    )
    gate = admit(paths.data_root, eval_paths, eval_card, fuse, spec.kind, spec.reason)
    final_dir = paths.submission_dir(spec.submission_id)
    identity: str | None
    if test_card is not None:
        artifact, tmp, identity = _render(spec, profile, paths, test_card, warnings)
        tmp.rename(final_dir)
    else:
        identity = None
        artifact = Artifact(
            kind="kernel",
            kernel=spec.kernel,
            version=spec.version,
            output=spec.output,
            weights=weights,
        )
        final_dir.mkdir(parents=True)
    try:
        staged = Staged(
            submission_id=spec.submission_id,
            dataset=spec.dataset,
            kind=spec.kind,
            eval_run=spec.eval_run,
            test_run=spec.test_run if test_card is not None else None,
            pairing=pairing,
            artifact=artifact,
            gate=gate,
            profile_sha256=profile_sha,
            staged_at=stamp(),
            vcp_version=build_string(),
            provenance=info.grade,
        )
        text = json.dumps(staged.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        (final_dir / STAGE_FILE).write_text(text, encoding="utf-8", newline="\n")
        ledger.append(
            LedgerRow(
                event="staged",
                ts=stamp(),
                submission_id=spec.submission_id,
                kind=spec.kind,
                eval_run=spec.eval_run,
                test_run=staged.test_run,
                sha256=artifact.sha256,
                md5=artifact.md5,
                gate=gate,
                profile_sha256=profile_sha,
            )
        )
    except BaseException:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise
    return StageResult(staged, final_dir, warnings, identity)


def verify(
    dataset: str, submission_id: str, *, data_root: Path | None, configs_root: Path | None
) -> list[str]:
    """The third verification: the bytes on disk, re-rendered from the test-side run."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    staged = load_staged(paths, submission_id)
    checks: list[str] = []
    if staged.artifact.kind == "kernel":
        for ref in staged.artifact.weights:
            have = load_run(paths.data_root, ref.run).source.weights_hash
            if have != ref.sha256:
                raise IntegrityError(
                    f"weights_hash of {ref.run!r} is now {(have or 'none')[:12]}, staged "
                    f"{ref.sha256[:12]}",
                    fields={"run": ref.run},
                )
            checks.append(f"weights:{ref.run}=ok")
        return checks
    path = paths.submission_dir(submission_id) / str(staged.artifact.path)
    if not path.is_file():
        raise IntegrityError(f"artifact missing: {path}")
    actual = sha256_file(path)
    if actual != staged.artifact.sha256:
        raise IntegrityError(
            f"artifact sha256 {actual[:12]} != staged {str(staged.artifact.sha256)[:12]}",
            location=str(path),
        )
    checks.append(f"sha256={actual[:12]}")
    test_ds = Dataset.load(profile.dataset, data_root=data_root, configs_root=configs_root)
    test_plan = load_plan(paths, profile.test_plan)
    test_card = load_run(paths.data_root, str(staged.test_run))
    writer = writer_for(str(staged.artifact.writer), test_ds.card.task)
    if writer.version != staged.artifact.writer_version:
        raise ValidationFailed(
            f"writer {writer.name!r} is now version {writer.version}; the artifact was written "
            f"by version {staged.artifact.writer_version}, so its bytes cannot be reproduced"
        )
    preds = read_predictions(verify_prediction(paths.data_root, test_card, profile.test_subset))
    samples = test_ds.subset(profile.test_subset, test_plan, paths=paths)
    with tempfile.TemporaryDirectory(dir=paths.submit_dir) as tmp:
        out = Path(tmp) / writer.file_name
        writer.write(preds, WriteContext(test_ds, samples, dict(staged.artifact.writer_opts), out))
        rebuilt = sha256_file(out)
    if rebuilt != staged.artifact.sha256:
        raise IntegrityError(
            f"rebuilt artifact sha256 {rebuilt[:12]} != staged {str(staged.artifact.sha256)[:12]}"
        )
    checks.append("rebuild=ok")
    if is_fusion(paths.data_root, test_card.run_id):
        build = load_record(paths.data_root, test_card.run_id).subsets.get(profile.test_subset)
        entry = test_card.predictions.get(profile.test_subset)
        if build is None or entry is None or build.output_sha256 != entry.sha256:
            raise IntegrityError(
                f"fuse.json output sha != run.yaml predictions sha for {test_card.run_id!r}",
                fields={"run": test_card.run_id},
            )
        checks.append("fusion_output=ok")
    return checks
