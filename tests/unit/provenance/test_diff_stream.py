"""The streaming diff reader must match the full loader exactly and fail closed."""

from __future__ import annotations

import pytest

from helpers import det_samples, make_card
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.data.source_audit import write_source_audit
from vcp.provenance import diff as diff_module
from vcp.provenance.diff import (
    DatasetDiffSpec,
    create_dataset_diff,
    load_dataset_diff,
    open_dataset_diff,
)
from vcp.provenance.graph import build_graph, dataset_version_id
from vcp.provenance.schema import DatasetDiffSummary


def _save(roots, name: str, samples: list[Sample]) -> Dataset:
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    write_source_audit(paths, ds.card, data_root=roots.data)
    return ds


def _published_diff(roots, ident: str = "a-to-b"):
    """REMOVED s0000, MODIFIED s0001 (group), ADDED s9999 — three sorted events."""
    old = det_samples(4, seed=1)
    new = [s.model_copy(deep=True) for s in old]
    new.pop(0)
    new[0] = new[0].model_copy(update={"group": "new-group"})
    new.append(old[-1].model_copy(update={"sample_id": "s9999", "meta": {"new": True}}))
    _save(roots, "version-a", old)
    _save(roots, "version-b", new)
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="version-a",
            to_dataset="version-b",
            artifact_id=ident,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def _foreign_event(roots):
    """A valid event whose endpoints belong to a different transition (c -> d)."""
    old = det_samples(2, seed=5)
    new = [s.model_copy(deep=True) for s in old]
    new[1] = new[1].model_copy(update={"group": "other-group"})
    _save(roots, "version-c", old)
    _save(roots, "version-d", new)
    result = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="version-c",
            to_dataset="version-d",
            artifact_id="c-to-d",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    (event,) = result.changes
    assert event.sample_id == "s0001"
    return event


def _publish_crafted(roots, ident: str, summary: DatasetDiffSummary, lines: list[str]) -> None:
    """Commit an artifact whose manifest is valid but whose events are what the test says."""
    summary = summary.model_copy(update={"artifact_id": ident, "grade": "fallback"})
    spec = ArtifactSpec(
        kind="dataset_diff",
        id=ident,
        dataset=summary.to_dataset,
        params={
            "from_dataset": summary.from_dataset,
            "from_samples_hash": summary.from_samples_hash,
            "to_dataset": summary.to_dataset,
            "to_samples_hash": summary.to_samples_hash,
            "policies": ",".join(summary.policies),
        },
        inputs=[
            InputRef(name="from_samples", sha256=summary.from_samples_hash),
            InputRef(name="to_samples", sha256=summary.to_samples_hash),
        ],
    )
    with ArtifactWriter.create(spec, data_root=roots.data) as writer:
        writer.write_text("changes.jsonl", "".join(line + "\n" for line in lines))
        writer.write_json("summary.json", summary.model_dump(mode="json"))
        writer.commit()


def test_open_dataset_diff_matches_the_full_loader(roots):
    result = _published_diff(roots)
    loaded = load_dataset_diff(roots.data, "a-to-b")
    stream = open_dataset_diff(roots.data, "a-to-b")

    assert stream.summary == loaded.summary
    assert stream.artifact_dir == loaded.artifact_dir
    assert stream.change_ids == [change.change_id for change in loaded.changes]
    assert list(stream.events) == loaded.changes == result.changes


@pytest.mark.parametrize(
    ("case", "error", "message"),
    [
        ("unsorted", IntegrityError, "changes are not sorted"),
        ("duplicate", IntegrityError, "duplicate change ids"),
        ("foreign_endpoints", IntegrityError, "event endpoints disagree"),
        ("total_disagrees", IntegrityError, "summary does not match artifact/events"),
        ("counts_disagree", IntegrityError, "summary counts disagree"),
        ("bad_line", ValidationFailed, "bad dataset diff"),
    ],
)
def test_stream_rejects_exactly_what_the_full_loader_rejects(roots, case, error, message):
    result = _published_diff(roots)
    events = [change.model_dump_json() for change in result.changes]
    summary = result.summary
    # The summary model itself insists that counts sum to total_changes, so every crafted
    # summary keeps that invariant and only breaks the one thing the case is about.
    if case == "unsorted":
        lines = list(reversed(events))
    elif case == "duplicate":
        lines = [events[0], events[0]]
        summary = summary.model_copy(update={"total_changes": 2, "counts": {"REMOVED": 2}})
    elif case == "foreign_endpoints":
        lines = [events[0], _foreign_event(roots).model_dump_json()]
        summary = summary.model_copy(
            update={"total_changes": 2, "counts": {"MODIFIED": 1, "REMOVED": 1}}
        )
    elif case == "total_disagrees":
        lines = events
        summary = summary.model_copy(
            update={"total_changes": 5, "counts": {"ADDED": 1, "MODIFIED": 1, "REMOVED": 3}}
        )
    elif case == "counts_disagree":
        lines = events
        summary = summary.model_copy(update={"counts": {"ADDED": 3}})
    else:
        lines = ["{not json"]
        summary = summary.model_copy(update={"total_changes": 1, "counts": {"ADDED": 1}})
    _publish_crafted(roots, "crafted", summary, lines)

    with pytest.raises(error, match=message):
        load_dataset_diff(roots.data, "crafted")
    with pytest.raises(error, match=message):
        open_dataset_diff(roots.data, "crafted")


def test_graph_replay_streams_events_instead_of_loading_them(roots, monkeypatch):
    result = _published_diff(roots)
    reference = build_graph(roots.data, roots.configs)

    def refuse(*args, **kwargs):
        raise AssertionError("graph replay must stream the diff, not load it whole")

    monkeypatch.setattr(diff_module, "load_dataset_diff", refuse)
    graph = build_graph(roots.data, roots.configs)

    assert graph.normalized() == reference.normalized()
    old = dataset_version_id(result.summary.from_dataset, result.summary.from_samples_hash)
    new = dataset_version_id(result.summary.to_dataset, result.summary.to_samples_hash)
    assert graph.transitions[(old, new)] == [change.change_id for change in result.changes]
    assert set(graph.changes) == {change.change_id for change in result.changes}
    assert graph.gaps == []


def test_replay_fails_closed_if_the_events_change_between_the_two_passes(roots):
    _published_diff(roots)
    stream = open_dataset_diff(roots.data, "a-to-b")
    path = artifact_dir(roots.data, "dataset_diff", "a-to-b") / "changes.jsonl"
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"\n")  # every row still valid; the bytes are not

    with pytest.raises(IntegrityError, match="changed while it was being replayed"):
        list(stream.events)
