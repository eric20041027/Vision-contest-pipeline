from __future__ import annotations

import json

import pytest

from helpers import det_samples, make_card
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.store import verify
from vcp.artifact.writer import ArtifactWriter
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.data.source_audit import write_source_audit
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff, load_dataset_diff
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    DatasetDiffSummary,
    SemanticEffect,
)


def _save(roots, name: str, samples: list[Sample], *, audit: bool = True) -> Dataset:
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    if audit:
        write_source_audit(paths, ds.card, data_root=roots.data)
    return ds


def test_diff_publishes_verified_sorted_changed_rows_only(roots):
    old = det_samples(4, seed=1)
    new = [s.model_copy(deep=True) for s in old]
    new.pop(0)
    new[0] = new[0].model_copy(update={"group": "new-group"})
    new.append(old[-1].model_copy(update={"sample_id": "s9999", "meta": {"new": True}}))
    from_ds = _save(roots, "version-a", old)
    to_ds = _save(roots, "version-b", new)

    result = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="version-a",
            to_dataset="version-b",
            artifact_id="a-to-b",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    assert result.summary.total_changes == 3
    assert result.summary.counts == {"ADDED": 1, "MODIFIED": 1, "REMOVED": 1}
    assert result.summary.grade == "source_audit"
    assert result.summary.from_samples_hash == from_ds.card.samples_hash
    assert result.summary.to_samples_hash == to_ds.card.samples_hash
    assert [(c.sample_id, c.change_type) for c in result.changes] == [
        ("s0000", ChangeType.REMOVED),
        ("s0001", ChangeType.MODIFIED),
        ("s9999", ChangeType.ADDED),
    ]
    modified = result.changes[1]
    assert modified.changed_fields == ["group"]
    assert modified.changed_domains == [ChangeDomain.GROUP]
    assert modified.semantic_effects == [SemanticEffect.SPLIT_AFFECTING]
    assert verify(roots.data, "dataset_diff", "a-to-b").failed is False
    out = artifact_dir(roots.data, "dataset_diff", "a-to-b")
    assert {p.name for p in out.iterdir()} == {
        "spec.json",
        "changes.jsonl",
        "summary.json",
        "manifest.json",
    }
    recorded_ids = [
        json.loads(line)["sample_id"] for line in (out / "changes.jsonl").read_text().splitlines()
    ]
    assert recorded_ids == [
        "s0000",
        "s0001",
        "s9999",
    ]


def test_noop_transition_has_summary_but_no_fake_events(roots):
    samples = det_samples(3, seed=2)
    _save(roots, "same-a", samples)
    _save(roots, "same-b", [s.model_copy(deep=True) for s in samples])

    result = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="same-a",
            to_dataset="same-b",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    assert result.changes == []
    assert result.summary.total_changes == 0
    out = artifact_dir(roots.data, "dataset_diff", result.artifact_id)
    assert (out / "changes.jsonl").read_bytes() == b""


def test_fallback_without_source_audit_is_explicit_and_semantically_equal(roots):
    old = det_samples(2, seed=3)
    new = [s.model_copy(deep=True) for s in old]
    new[1] = new[1].model_copy(update={"meta": {"opaque": 1}})
    _save(roots, "legacy-a", old, audit=False)
    _save(roots, "legacy-b", new, audit=False)

    result = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="legacy-a",
            to_dataset="legacy-b",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    assert result.summary.grade == "fallback"
    assert result.changes[0].changed_domains == [ChangeDomain.META]
    assert result.changes[0].semantic_effects == [SemanticEffect.UNKNOWN]


def test_hash_failure_happens_before_artifact_id_is_claimed(roots):
    _save(roots, "bad-a", det_samples(2, seed=4))
    _save(roots, "bad-b", det_samples(2, seed=5))
    paths = DatasetPaths.resolve("bad-b", data_root=roots.data, configs_root=roots.configs)
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b" ")

    with pytest.raises(IntegrityError, match="mismatch: samples.jsonl"):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset="bad-a",
                to_dataset="bad-b",
                artifact_id="must-not-exist",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )

    assert not artifact_dir(roots.data, "dataset_diff", "must-not-exist").exists()


def test_duplicate_sample_ids_fail_before_publish(roots):
    _save(roots, "good", det_samples(2, seed=6), audit=False)
    paths = DatasetPaths.resolve("dupe", data_root=roots.data, configs_root=roots.configs)
    rows = det_samples(2, seed=7)
    rows[1] = rows[1].model_copy(update={"sample_id": rows[0].sample_id})
    paths.samples_jsonl.parent.mkdir(parents=True, exist_ok=True)
    paths.samples_jsonl.write_text(
        "".join(s.model_dump_json(exclude_none=True, exclude_defaults=True) + "\n" for s in rows),
        encoding="utf-8",
        newline="\n",
    )
    card = make_card("det", name="dupe", image_root="raw/dupe").model_copy(
        update={"sample_count": 2, "samples_hash": sha256_file(paths.samples_jsonl)}
    )
    dump_yaml_model(card, paths.card_yaml)

    with pytest.raises(ValidationFailed, match="duplicate sample_id"):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset="good",
                to_dataset="dupe",
                artifact_id="dupe-diff",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    assert not artifact_dir(roots.data, "dataset_diff", "dupe-diff").exists()


def test_source_audit_does_not_replace_sample_schema_validation(roots):
    _save(roots, "valid", det_samples(1, seed=8))
    paths = DatasetPaths.resolve("invalid", data_root=roots.data, configs_root=roots.configs)
    paths.samples_jsonl.parent.mkdir(parents=True, exist_ok=True)
    paths.samples_jsonl.write_text(
        '{"sample_id":"bad","views":[],"label_source":"none"}\n',
        encoding="utf-8",
        newline="\n",
    )
    card = make_card("det", name="invalid", image_root="raw/invalid").model_copy(
        update={"sample_count": 1, "samples_hash": sha256_file(paths.samples_jsonl)}
    )
    dump_yaml_model(card, paths.card_yaml)
    write_source_audit(paths, card, data_root=roots.data)

    with pytest.raises(ValidationFailed, match="bad sample row"):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset="valid",
                to_dataset="invalid",
                artifact_id="invalid-schema",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    assert not artifact_dir(roots.data, "dataset_diff", "invalid-schema").exists()


def test_fallback_and_audit_use_exact_original_row_bytes(roots):
    sample = det_samples(1, seed=10)[0]
    _save(roots, "spacing-a", [sample], audit=False)
    paths = DatasetPaths.resolve("spacing-b", data_root=roots.data, configs_root=roots.configs)
    paths.samples_jsonl.parent.mkdir(parents=True, exist_ok=True)
    paths.samples_jsonl.write_text(
        sample.model_dump_json(exclude_none=True, exclude_defaults=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    card = make_card("det", name="spacing-b", image_root="raw/spacing-b").model_copy(
        update={"sample_count": 1, "samples_hash": sha256_file(paths.samples_jsonl)}
    )
    dump_yaml_model(card, paths.card_yaml)

    fallback = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="spacing-a",
            to_dataset="spacing-b",
            artifact_id="spacing-fallback",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    write_source_audit(
        DatasetPaths.resolve("spacing-a", data_root=roots.data, configs_root=roots.configs),
        Dataset.load_card("spacing-a", data_root=roots.data, configs_root=roots.configs),
        data_root=roots.data,
    )
    write_source_audit(paths, card, data_root=roots.data)
    audited = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="spacing-a",
            to_dataset="spacing-b",
            artifact_id="spacing-audited",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    assert fallback.summary.total_changes == audited.summary.total_changes == 1
    assert fallback.changes[0].before_row_hash == audited.changes[0].before_row_hash
    assert fallback.changes[0].after_row_hash == audited.changes[0].after_row_hash


def test_writer_open_rechecks_the_exact_inputs_used_for_computation(roots, monkeypatch):
    _save(roots, "race-a", det_samples(1, seed=11))
    _save(roots, "race-b", det_samples(1, seed=12))
    paths = DatasetPaths.resolve("race-a", data_root=roots.data, configs_root=roots.configs)
    original = ArtifactWriter.create

    def mutate_then_create(spec, *, data_root):
        paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b" ")
        return original(spec, data_root=data_root)

    monkeypatch.setattr(ArtifactWriter, "create", mutate_then_create)
    with pytest.raises(IntegrityError, match="mismatch: input 'from_samples'"):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset="race-a",
                to_dataset="race-b",
                artifact_id="race-diff",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    assert not artifact_dir(roots.data, "dataset_diff", "race-diff").exists()


def test_loader_rejects_manifest_valid_but_semantically_inconsistent_summary(roots):
    old_hash = "a" * 64
    new_hash = "b" * 64
    spec = ArtifactSpec(
        kind="dataset_diff",
        id="bad-summary",
        dataset="new",
        params={
            "from_dataset": "old",
            "from_samples_hash": old_hash,
            "to_dataset": "new",
            "to_samples_hash": new_hash,
            "policies": "",
        },
        inputs=[
            InputRef(name="from_samples", sha256=old_hash),
            InputRef(name="to_samples", sha256=new_hash),
        ],
    )
    summary = DatasetDiffSummary(
        artifact_id="bad-summary",
        from_dataset="old",
        from_samples_hash=old_hash,
        to_dataset="new",
        to_samples_hash=new_hash,
        grade="fallback",
        total_changes=0,
        counts={},
        domain_counts={"VIEWS": 999},
        effect_counts={},
    )
    with ArtifactWriter.create(spec, data_root=roots.data) as writer:
        writer.write_text("changes.jsonl", "")
        writer.write_json("summary.json", summary.model_dump(mode="json"))
        writer.commit()

    with pytest.raises(IntegrityError, match="summary counts"):
        load_dataset_diff(roots.data, "bad-summary")
