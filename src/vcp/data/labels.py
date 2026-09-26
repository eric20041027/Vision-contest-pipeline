"""``vcp data labels`` (spec 2026-09-26 §3.1, §4.1): a training label file checked against the
split plan -- every row labels a sample of the subsets it is for, or lies outside the dataset
altogether -- and kept as an immutable ``label_set`` artifact that runs attach.

The check reads the whole dataset the way ``vcp data audit`` does: in vcp's own process, through
no accessor, with no receipt and no unseal. It needs every sample's views and meta to map ids,
never a label."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir, validate_name
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan

LABEL_SET_KIND = "label_set"
SUMMARY = "label_set.json"
FORMATS = {".csv": "csv", ".jsonl": "jsonl"}
ID_FIELDS = ("sample_id", "view_path", "view_stem")  # and meta.<key>
SHOWN_IDS = 5


class LabelSetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    samples_hash: str
    plan_id: str
    subsets: list[str]
    id_field: str
    id_col: str
    format: str
    rows: int
    matched: dict[str, int]
    external: int


@dataclass(frozen=True)
class LabelSetSpec:
    name: str
    plan_id: str
    subsets: list[str]
    file: Path
    id_field: str
    label_set_id: str
    id_col: str = "id"
    notes: str = ""
    data_root: Path | None = None
    configs_root: Path | None = None


@dataclass(frozen=True)
class LabelSetResult:
    summary: LabelSetSummary
    reused: bool


def _keys(sample: Sample, id_field: str) -> list[str]:
    if id_field == "sample_id":
        return [sample.sample_id]
    if id_field == "view_path":
        return [v.path for v in sample.views]
    if id_field == "view_stem":
        return [Path(v.path).stem for v in sample.views]
    key = id_field.removeprefix("meta.")
    raw = sample.meta.get(key)
    return [] if raw is None else [str(raw)]


def _check_id_field(id_field: str) -> None:
    if id_field in ID_FIELDS or (id_field.startswith("meta.") and len(id_field) > len("meta.")):
        return
    raise ValidationFailed(
        f"id_field: {id_field!r} is not one of {list(ID_FIELDS)} or meta.<key>",
        fields={"id_field": id_field},
    )


def sample_ids_by_key(samples: list[Sample], id_field: str) -> dict[str, str]:
    """external id -> sample_id; one id behind two samples is ``duplicate_id:`` (the field
    cannot tell them apart). Every view of a multi-view sample contributes its own id."""
    _check_id_field(id_field)
    out: dict[str, str] = {}
    for s in samples:
        for key in _keys(s, id_field):
            other = out.get(key)
            if other is not None and other != s.sample_id:
                raise ValidationFailed(
                    f"duplicate_id: {id_field} {key!r} names samples {other!r} and {s.sample_id!r}",
                    fields={"id": key},
                )
            out[key] = s.sample_id
    return out


def _csv_ids(path: Path, id_col: str) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or id_col not in reader.fieldnames:
            raise ValidationFailed(
                f"not_found: column {id_col!r} in {path.name} (has {reader.fieldnames or []})",
                fields={"column": id_col},
            )
        return [str(row[id_col]).strip() for row in reader]


def _jsonl_ids(path: Path, id_col: str) -> list[str]:
    out: list[str] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(
                    f"bad label row: {e}", location=f"{path.name}:{lineno}"
                ) from e
            if not isinstance(row, dict) or id_col not in row:
                raise ValidationFailed(
                    f"not_found: column {id_col!r} in {path.name}:{lineno}",
                    fields={"column": id_col},
                )
            out.append(str(row[id_col]).strip())
    return out


def read_ids(path: Path, id_col: str) -> tuple[str, list[str]]:
    """``(format, ids)`` of a label file, ids as stripped strings in file order."""
    if not path.is_file():
        raise ValidationFailed(f"not_found: {path}", fields={"file": str(path)})
    fmt = FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise ValidationFailed(
            f"unsupported_format: {path.name} (want one of {sorted(FORMATS)})",
            fields={"file": str(path)},
        )
    return fmt, (_csv_ids(path, id_col) if fmt == "csv" else _jsonl_ids(path, id_col))


def _classify(
    ids: list[str], by_key: dict[str, str], plan: SplitPlan, subsets: list[str]
) -> tuple[dict[str, int], int]:
    """``(matched per allowed subset, external rows)``; a row on any other dataset sample is
    ``labels_outside_subsets:`` naming counts and a few sample ids, never label content."""
    twice = sorted(i for i, n in Counter(ids).items() if n > 1)
    if twice:
        raise ValidationFailed(
            f"duplicate_id: {len(twice)} ids appear more than once, e.g. {twice[:SHOWN_IDS]}",
            fields={"id": twice[0]},
        )
    matched = {s: 0 for s in subsets}
    outside: dict[str, list[str]] = {}
    external = 0
    for key in ids:
        sid = by_key.get(key)
        if sid is None:
            external += 1
            continue
        where = plan.assignment.get(sid, "unassigned")
        if where in matched:
            matched[where] += 1
        else:
            outside.setdefault(where, []).append(sid)
    if outside:
        roles = {s.name: s.role for s in plan.subsets}
        parts = [
            f"{len(v)} in {k} ({roles.get(k, 'unassigned')})" for k, v in sorted(outside.items())
        ]
        shown = sorted(sid for v in outside.values() for sid in v)[:SHOWN_IDS]
        raise ValidationFailed(
            f"labels_outside_subsets: {', '.join(parts)}; e.g. {shown}",
            fields={f"outside_{k}": len(v) for k, v in sorted(outside.items())},
        )
    return matched, external


def _subsets(plan: SplitPlan, names: list[str]) -> list[str]:
    subsets = sorted(set(names))
    if not subsets:
        raise ValidationFailed("--subset is required: the subsets these labels are for")
    for name in subsets:
        if plan.subset(name).role == "sealed":  # PlanMismatchError for an unknown subset
            raise ValidationFailed(
                f"labels_on_sealed: {name!r} is the sealed subset; no training label set may "
                "cover it",
                fields={"subset": name},
            )
    return subsets


def create_label_set(spec: LabelSetSpec) -> LabelSetResult:
    validate_name(spec.label_set_id)
    paths = DatasetPaths.resolve(
        spec.name, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset.card)
    subsets = _subsets(plan, spec.subsets)
    fmt, ids = read_ids(spec.file, spec.id_col)
    by_key = sample_ids_by_key(dataset.samples, spec.id_field)
    matched, external = _classify(ids, by_key, plan, subsets)
    summary = LabelSetSummary(
        dataset=spec.name,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        subsets=subsets,
        id_field=spec.id_field,
        id_col=spec.id_col,
        format=fmt,
        rows=len(ids),
        matched=matched,
        external=external,
    )
    art = ArtifactSpec(
        kind=LABEL_SET_KIND,
        id=spec.label_set_id,
        dataset=spec.name,
        plan_id=spec.plan_id,
        params={
            "samples_hash": dataset.card.samples_hash,
            "subsets": ",".join(subsets),
            "id_field": spec.id_field,
            "id_col": spec.id_col,
            "format": fmt,
        },
        inputs=[InputRef(name="labels", sha256=sha256_file(spec.file))],
        notes=spec.notes,
    )
    if store.reuse(art, paths.data_root, check_files=True) is not None:
        return LabelSetResult(load_label_set(paths.data_root, spec.label_set_id), reused=True)
    with ArtifactWriter.create(art, data_root=paths.data_root) as writer:
        writer.add_file(f"labels.{fmt}", spec.file)
        writer.write_json(SUMMARY, summary.model_dump(mode="json"))
        writer.commit()
    return LabelSetResult(summary, reused=False)


def load_label_set(data_root: Path, label_set_id: str) -> LabelSetSummary:
    """A committed label set that still verifies (spec §4.2); ``not_found:`` / ``partial:`` /
    ``mismatch:`` otherwise."""
    res = store.verify(data_root, LABEL_SET_KIND, label_set_id)  # not_found / partial raise
    if res.failed:
        raise IntegrityError(
            f"mismatch: label_set/{label_set_id} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"label_set": label_set_id},
        )
    path = artifact_dir(data_root, LABEL_SET_KIND, label_set_id) / SUMMARY
    return LabelSetSummary.model_validate_json(path.read_text(encoding="utf-8"))
