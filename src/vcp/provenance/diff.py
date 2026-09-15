"""Verified, source-audit-assisted dataset diffs published as immutable artifacts."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir, resolve_stored_path, validate_name
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, dump_sample
from vcp.data.source_audit import LoadedAudit, load_source_audit
from vcp.provenance.policy import classify_rows, policy_versions
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    DatasetDiffSummary,
    SampleChange,
    SemanticEffect,
    make_change_id,
)

KIND = "dataset_diff"
CHANGES_FILE = "changes.jsonl"
SUMMARY_FILE = "summary.json"


@dataclass(frozen=True)
class DatasetDiffSpec:
    from_dataset: str
    to_dataset: str
    artifact_id: str | None = None
    policy_names: tuple[str, ...] = ()
    data_root: Path | None = None
    configs_root: Path | None = None


class _Version(NamedTuple):
    name: str
    paths: DatasetPaths
    samples_hash: str
    grade: Literal["source_audit", "fallback"]
    audit: LoadedAudit | None
    hashes: dict[str, str]
    rows: dict[str, dict[str, Any]]


class DatasetDiffResult(NamedTuple):
    artifact_id: str
    summary: DatasetDiffSummary
    changes: list[SampleChange]
    artifact_dir: Path


class DatasetComparison(NamedTuple):
    artifact_id: str
    summary: DatasetDiffSummary
    changes: list[SampleChange]


def _validate_spec(spec: DatasetDiffSpec) -> None:
    validate_name(spec.from_dataset)
    validate_name(spec.to_dataset)
    if spec.artifact_id is not None:
        validate_name(spec.artifact_id)
    if len(set(spec.policy_names)) != len(spec.policy_names):
        raise ValidationFailed("duplicate impact policy names")
    policy_versions(list(spec.policy_names))


def _load_version(name: str, *, data_root: Path | None, configs_root: Path | None) -> _Version:
    paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
    card = Dataset.load_card(name, data_root=data_root, configs_root=configs_root)
    if not paths.samples_jsonl.is_file():
        raise ValidationFailed(f"not_found: samples file {paths.samples_jsonl}")
    actual = sha256_file(paths.samples_jsonl)
    if actual != card.samples_hash:
        raise IntegrityError(
            f"mismatch: samples.jsonl sha256 {actual[:12]} != card samples_hash "
            f"{card.samples_hash[:12]}",
            location=str(paths.samples_jsonl),
        )
    audit = load_source_audit(paths, card, data_root=paths.data_root)
    hashes: dict[str, str] = {}
    rows: dict[str, dict[str, Any]] = {}
    offset = 0
    with paths.samples_jsonl.open("rb") as source:
        for lineno, raw in enumerate(iter(source.readline, b""), start=1):
            try:
                sample = Sample.model_validate_json(raw)
            except ValidationError as error:
                raise ValidationFailed(
                    f"bad sample row: {error}", location=f"{paths.samples_jsonl}:{lineno}"
                ) from error
            if sample.sample_id in rows:
                raise ValidationFailed(
                    f"duplicate sample_id {sample.sample_id!r}",
                    location=f"{paths.samples_jsonl}:{lineno}",
                )
            digest = hashlib.sha256(raw).hexdigest()
            if audit is not None:
                indexed = audit.index.get(sample.sample_id)
                if indexed != (offset, len(raw), digest):
                    raise IntegrityError(
                        f"mismatch: source audit row for {sample.sample_id!r} does not match "
                        "samples.jsonl",
                        fields={"audit": audit.artifact_id, "sample": sample.sample_id},
                    )
            hashes[sample.sample_id] = digest
            rows[sample.sample_id] = dump_sample(sample)
            offset += len(raw)
    if len(rows) != card.sample_count:
        raise IntegrityError(
            f"mismatch: samples.jsonl has {len(rows)} rows, card says {card.sample_count}"
        )
    if audit is not None and set(rows) != set(audit.index):
        raise IntegrityError(
            f"mismatch: source audit indexes {len(audit.index)} ids, samples.jsonl has {len(rows)}",
            fields={"audit": audit.artifact_id},
        )
    grade: Literal["source_audit", "fallback"] = "source_audit" if audit is not None else "fallback"
    return _Version(name, paths, card.samples_hash, grade, audit, hashes, rows)


def _row(version: _Version, sample_id: str) -> dict[str, Any]:
    return version.rows[sample_id]


def _event(
    before_version: _Version,
    after_version: _Version,
    sample_id: str,
    change_type: ChangeType,
    *,
    policy_names: list[str],
) -> SampleChange:
    before_hash = before_version.hashes.get(sample_id)
    after_hash = after_version.hashes.get(sample_id)
    if change_type == ChangeType.MODIFIED:
        fields, domains, effects = classify_rows(
            _row(before_version, sample_id),
            _row(after_version, sample_id),
            policy_names=policy_names,
        )
    else:
        fields = ["*"]
        domains = sorted(
            {
                ChangeDomain.VIEWS,
                ChangeDomain.LABELS,
                ChangeDomain.LABEL_SOURCE,
                ChangeDomain.GROUP,
                ChangeDomain.META,
            },
            key=str,
        )
        effects = sorted(
            {
                SemanticEffect.INPUT_AFFECTING,
                SemanticEffect.TRAINING_AFFECTING,
                SemanticEffect.SPLIT_AFFECTING,
                SemanticEffect.EVALUATION_AFFECTING,
            },
            key=str,
        )
    return SampleChange(
        change_id=make_change_id(
            before_version.samples_hash,
            after_version.samples_hash,
            sample_id,
            change_type,
            before_hash,
            after_hash,
        ),
        from_dataset=before_version.name,
        from_samples_hash=before_version.samples_hash,
        to_dataset=after_version.name,
        to_samples_hash=after_version.samples_hash,
        sample_id=sample_id,
        change_type=change_type,
        changed_domains=domains,
        changed_fields=fields,
        semantic_effects=effects,
        before_row_hash=before_hash,
        after_row_hash=after_hash,
    )


def _changes(before: _Version, after: _Version, policies: list[str]) -> list[SampleChange]:
    old = before.hashes
    new = after.hashes
    events = [
        *(
            _event(before, after, sample_id, ChangeType.REMOVED, policy_names=policies)
            for sample_id in old.keys() - new.keys()
        ),
        *(
            _event(before, after, sample_id, ChangeType.ADDED, policy_names=policies)
            for sample_id in new.keys() - old.keys()
        ),
        *(
            _event(before, after, sample_id, ChangeType.MODIFIED, policy_names=policies)
            for sample_id in old.keys() & new.keys()
            if old[sample_id] != new[sample_id]
        ),
    ]
    return sorted(events, key=lambda event: (event.sample_id, event.change_type.value))


def _derived_id(before: _Version, after: _Version) -> str:
    return f"diff-{before.name}-{after.name}-{before.samples_hash[:8]}-{after.samples_hash[:8]}"


def _summary(
    artifact_id: str,
    before: _Version,
    after: _Version,
    changes: list[SampleChange],
    policies: list[str],
) -> DatasetDiffSummary:
    change_counts = Counter(change.change_type.value for change in changes)
    domain_counts = Counter(domain.value for change in changes for domain in change.changed_domains)
    effect_counts = Counter(
        effect.value for change in changes for effect in change.semantic_effects
    )
    grade: Literal["source_audit", "fallback"] = (
        "source_audit" if before.grade == after.grade == "source_audit" else "fallback"
    )
    return DatasetDiffSummary(
        artifact_id=artifact_id,
        from_dataset=before.name,
        from_samples_hash=before.samples_hash,
        to_dataset=after.name,
        to_samples_hash=after.samples_hash,
        grade=grade,
        policies=policy_versions(policies),
        total_changes=len(changes),
        counts=dict(sorted(change_counts.items())),
        domain_counts=dict(sorted(domain_counts.items())),
        effect_counts=dict(sorted(effect_counts.items())),
    )


def _artifact_spec(
    artifact_id: str,
    before: _Version,
    after: _Version,
    policies: list[str],
) -> ArtifactSpec:
    inputs = [
        InputRef(
            name="from_samples",
            path=str(before.paths.samples_jsonl),
            sha256=before.samples_hash,
        ),
        InputRef(name="to_samples", path=str(after.paths.samples_jsonl), sha256=after.samples_hash),
    ]
    for side, version in (("from", before), ("to", after)):
        if version.audit is not None:
            inputs.append(
                InputRef(
                    name=f"{side}_source_audit",
                    path=str(
                        store.manifest_path(
                            version.paths.data_root,
                            "source_audit",
                            version.audit.artifact_id,
                        )
                    ),
                    sha256=version.audit.manifest_sha256,
                )
            )
    return ArtifactSpec(
        kind=KIND,
        id=artifact_id,
        dataset=after.name,
        params={
            "from_dataset": before.name,
            "from_samples_hash": before.samples_hash,
            "to_dataset": after.name,
            "to_samples_hash": after.samples_hash,
            "policies": ",".join(policy_versions(policies)),
        },
        inputs=inputs,
    )


def create_dataset_diff(spec: DatasetDiffSpec) -> DatasetDiffResult:
    """Validate both versions completely, compute changes, then claim and publish the id."""
    before, after, comparison = _compare_dataset_versions(spec)
    artifact_spec = _artifact_spec(comparison.artifact_id, before, after, list(spec.policy_names))
    text = "".join(change.model_dump_json() + "\n" for change in comparison.changes)
    with ArtifactWriter.create(artifact_spec, data_root=before.paths.data_root) as writer:
        writer.write_text(CHANGES_FILE, text)
        writer.write_json(SUMMARY_FILE, comparison.summary.model_dump(mode="json"))
        writer.commit()
    return DatasetDiffResult(
        comparison.artifact_id,
        comparison.summary,
        comparison.changes,
        artifact_dir(before.paths.data_root, KIND, comparison.artifact_id),
    )


def compare_dataset_versions(spec: DatasetDiffSpec) -> DatasetComparison:
    """Compute and validate a diff without publishing any artifact."""
    return _compare_dataset_versions(spec)[2]


def _compare_dataset_versions(
    spec: DatasetDiffSpec,
) -> tuple[_Version, _Version, DatasetComparison]:
    _validate_spec(spec)
    before = _load_version(
        spec.from_dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    after = _load_version(spec.to_dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    artifact_id = spec.artifact_id or _derived_id(before, after)
    validate_name(artifact_id)
    policies = list(spec.policy_names)
    changes = _changes(before, after, policies)
    summary = _summary(artifact_id, before, after, changes, policies)
    return before, after, DatasetComparison(artifact_id, summary, changes)


class DatasetDiffStream(NamedTuple):
    """A committed diff validated end to end whose events are replayed one at a time.

    ``change_ids`` is the complete, validated id list from the first pass. ``events`` is a
    second pass over the same bytes that yields one :class:`SampleChange` at a time and fails
    closed if the file changed between the passes, so no consumer ever holds every event.
    """

    artifact_id: str
    summary: DatasetDiffSummary
    artifact_dir: Path
    change_ids: list[str]
    events: Iterator[SampleChange]


def _raw_lines(directory: Path) -> Iterator[bytes]:
    try:
        with (directory / CHANGES_FILE).open("rb") as handle:
            yield from handle
    except OSError as e:
        raise ValidationFailed(f"bad dataset diff: {e}", location=str(directory)) from e


def _parse_change(raw: bytes, directory: Path) -> SampleChange:
    try:
        return SampleChange.model_validate_json(raw.decode("utf-8"))
    except (ValidationError, ValueError) as e:
        raise ValidationFailed(f"bad dataset diff: {e}", location=str(directory)) from e


def _stream_changes(
    directory: Path, artifact_id: str, summary: DatasetDiffSummary, digest: Any | None
) -> Iterator[SampleChange]:
    """Yield ``changes.jsonl`` in file order with the same integrity checks as a full load.

    Row invariants (sorted order, unique ids, shared endpoints) are enforced as each row
    arrives; the aggregate invariants (event total, summary counters) after the last row. Only
    ids and counters stay in memory, and ``digest`` (when given) absorbs every raw byte.
    """
    endpoints = (
        summary.from_dataset,
        summary.from_samples_hash,
        summary.to_dataset,
        summary.to_samples_hash,
    )
    previous: tuple[str, str] | None = None
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    effects: Counter[str] = Counter()
    for raw in _raw_lines(directory):
        if digest is not None:
            digest.update(raw)
        if not raw.strip():
            continue
        change = _parse_change(raw, directory)
        key = (change.sample_id, change.change_type.value)
        if previous is not None and key < previous:
            raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} changes are not sorted")
        previous = key
        if change.change_id in seen:
            raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} has duplicate change ids")
        seen.add(change.change_id)
        if (
            change.from_dataset,
            change.from_samples_hash,
            change.to_dataset,
            change.to_samples_hash,
        ) != endpoints:
            raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} event endpoints disagree")
        counts[change.change_type.value] += 1
        for domain in change.changed_domains:
            domains[domain.value] += 1
        for effect in change.semantic_effects:
            effects[effect.value] += 1
        yield change
    if summary.total_changes != len(seen):
        raise IntegrityError(
            f"mismatch: dataset diff summary does not match artifact/events for {artifact_id!r}"
        )
    if (
        summary.counts != dict(sorted(counts.items()))
        or summary.domain_counts != dict(sorted(domains.items()))
        or summary.effect_counts != dict(sorted(effects.items()))
    ):
        raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} summary counts disagree")


def _verify_committed_diff(
    data_root: Path, artifact_id: str, *, verify_inputs: bool
) -> tuple[Path, DatasetDiffSummary]:
    """Verify the artifact files, summary and spec pins; the events are checked by the stream."""
    res = store.verify(data_root, KIND, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: dataset_diff/{artifact_id} fails verification "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"artifact": artifact_id},
        )
    manifest = store.load_manifest(data_root, KIND, artifact_id)
    directory = artifact_dir(data_root, KIND, artifact_id)
    try:
        summary = DatasetDiffSummary.model_validate_json(
            (directory / SUMMARY_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as e:
        raise ValidationFailed(f"bad dataset diff: {e}", location=str(directory)) from e
    if summary.artifact_id != artifact_id:
        raise IntegrityError(
            f"mismatch: dataset diff summary does not match artifact/events for {artifact_id!r}"
        )
    params = manifest.spec.params
    expected_params = {
        "from_dataset": summary.from_dataset,
        "from_samples_hash": summary.from_samples_hash,
        "to_dataset": summary.to_dataset,
        "to_samples_hash": summary.to_samples_hash,
        "policies": ",".join(summary.policies),
    }
    if any(params.get(key) != value for key, value in expected_params.items()):
        raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} spec disagrees with summary")
    inputs = {ref.name: ref.sha256 for ref in manifest.spec.inputs}
    if (
        inputs.get("from_samples") != summary.from_samples_hash
        or inputs.get("to_samples") != summary.to_samples_hash
    ):
        raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} input pins disagree")
    audited = {"from_source_audit", "to_source_audit"} <= set(inputs)
    if (summary.grade == "source_audit") != audited:
        raise IntegrityError(f"mismatch: dataset diff {artifact_id!r} provenance grade disagrees")
    if verify_inputs:
        for ref in manifest.spec.inputs:
            if ref.path is None or ref.sha256 is None:
                raise IntegrityError(
                    f"mismatch: dataset diff input {ref.name!r} is not fully pinned"
                )
            path = resolve_stored_path(ref.path, data_root)
            if not path.is_file():
                raise IntegrityError(
                    f"mismatch: dataset diff input {ref.name!r} is missing: {path}"
                )
            if sha256_file(path) != ref.sha256:
                raise IntegrityError(
                    f"mismatch: dataset diff input {ref.name!r} changed: {path}",
                    location=str(path),
                    fields={"artifact": artifact_id, "input": ref.name},
                )
    return directory, summary


def open_dataset_diff(
    data_root: Path, artifact_id: str, *, verify_inputs: bool = False
) -> DatasetDiffStream:
    """Validate a committed diff completely, then replay it one event at a time.

    The first pass runs every check :func:`load_dataset_diff` runs but keeps only ids and
    counters; the second pass (``events``) re-reads the same bytes for consumers that mutate a
    graph or index per event and fails closed if the file changed in between.
    """
    directory, summary = _verify_committed_diff(data_root, artifact_id, verify_inputs=verify_inputs)
    first = hashlib.sha256()
    change_ids = [
        change.change_id for change in _stream_changes(directory, artifact_id, summary, first)
    ]
    expected = first.hexdigest()

    def events() -> Iterator[SampleChange]:
        second = hashlib.sha256()
        yield from _stream_changes(directory, artifact_id, summary, second)
        if second.hexdigest() != expected:
            raise IntegrityError(
                f"mismatch: dataset diff {artifact_id!r} changed while it was being replayed",
                fields={"artifact": artifact_id},
            )

    return DatasetDiffStream(artifact_id, summary, directory, change_ids, events())


def load_dataset_diff(
    data_root: Path, artifact_id: str, *, verify_inputs: bool = False
) -> DatasetDiffResult:
    """Verify a committed diff and load its strict summary/event records."""
    directory, summary = _verify_committed_diff(data_root, artifact_id, verify_inputs=verify_inputs)
    changes = list(_stream_changes(directory, artifact_id, summary, None))
    return DatasetDiffResult(artifact_id, summary, changes, directory)
