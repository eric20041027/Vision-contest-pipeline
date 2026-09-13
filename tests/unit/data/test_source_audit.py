"""source_audit artifacts (spec 4.1, 6, 7.1): one preparation-time pass over samples.jsonl
becomes a content-addressed, immutable row index that consumers verify against instead of
hashing the whole file again."""

import hashlib
import json
import shutil

import pytest

from helpers import det_samples, make_card, write_images
from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.access.access import index_samples
from vcp.data.dataset import Dataset
from vcp.data.source_audit import (
    AUDIT_FILE,
    INDEX_FILE,
    KIND,
    IndexRow,
    SourceAudit,
    audit_id,
    audit_spec,
    load_source_audit,
    peek_sample_id,
    write_source_audit,
)
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def test_peek_sample_id_reads_only_the_leading_key(tmp_path):
    assert peek_sample_id(b'{"sample_id": "s1", "views": BROKEN\n', tmp_path / "x", 1) == "s1"
    assert peek_sample_id(b'{"sample_id":"a\\"b"}\n', tmp_path / "x", 2) == 'a"b'
    with pytest.raises(ValidationFailed, match="not a samples.jsonl line"):
        peek_sample_id(b'{"views": []}\n', tmp_path / "x", 3)


def test_audit_id_and_spec_are_content_addressed():
    h = "0123456789abcdef" + "f" * 48
    assert audit_id("beach-test", h) == "src-beach-test-0123456789abcdef"
    card = make_card("det", name="beach-test", image_root="raw/x").model_copy(
        update={"samples_hash": h}
    )
    paths = DatasetPaths.resolve("beach-test", data_root=None, configs_root=None)
    spec = audit_spec(paths, card)
    assert isinstance(spec, ArtifactSpec)
    assert (spec.kind, spec.id, spec.dataset) == (
        KIND,
        "src-beach-test-0123456789abcdef",
        "beach-test",
    )
    assert spec.params == {"samples_hash": h} and spec.inputs == []
    assert spec.id_pattern is not None  # the dataset group must equal the dataset field


def test_write_source_audit_creates_then_reuses(roots):
    ds, plan, paths = _seed(roots)
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    assert res.state == "created" and res.artifact_id == audit_id("tiny", ds.card.samples_hash)
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    assert (
        (d / AUDIT_FILE).is_file()
        and (d / INDEX_FILE).is_file()
        and (d / "manifest.json").is_file()
    )
    assert res.manifest_sha256 == sha256_file(d / "manifest.json")
    audit = SourceAudit.model_validate_json((d / AUDIT_FILE).read_text(encoding="utf-8"))
    assert (audit.dataset, audit.samples_hash) == ("tiny", ds.card.samples_hash)
    assert audit.size_bytes == paths.samples_jsonl.stat().st_size and audit.line_count == 40
    rows = [
        IndexRow.model_validate_json(line)
        for line in (d / INDEX_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert [r.sample_id for r in rows] == [s.sample_id for s in ds.samples]
    raw = paths.samples_jsonl.read_bytes()
    for r in rows:
        chunk = raw[r.offset : r.offset + r.length]
        assert chunk.endswith(b"\n") and hashlib.sha256(chunk).hexdigest() == r.sha256
    assert not store.verify(roots.data, KIND, res.artifact_id).failed
    again = write_source_audit(paths, ds.card, data_root=roots.data)
    assert again.state == "reused" and again.artifact_id == res.artifact_id
    assert again.manifest_sha256 == res.manifest_sha256
    assert [p.name for p in (roots.data / "artifacts" / KIND).iterdir() if p.is_dir()] == [
        res.artifact_id
    ]


def test_write_source_audit_refuses_a_file_that_disagrees_with_the_card(roots):
    ds, plan, paths = _seed(roots)
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl sha256"):
        write_source_audit(paths, ds.card, data_root=roots.data)
    d = artifact_dir(roots.data, KIND, audit_id("tiny", ds.card.samples_hash))
    assert (d / "failure.json").is_file() and not (d / "manifest.json").is_file()
    with pytest.raises(ValidationFailed, match="^partial:"):  # the claim is taken, never committed
        write_source_audit(paths, ds.card, data_root=roots.data)


def test_load_source_audit_matches_index_samples_and_handles_absence(roots):
    ds, plan, paths = _seed(roots)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    loaded = load_source_audit(paths, ds.card, data_root=roots.data)
    assert loaded is not None and loaded.artifact_id == res.artifact_id
    assert loaded.manifest_sha256 == res.manifest_sha256
    assert loaded.audit.line_count == 40
    _, plain = index_samples(paths.samples_jsonl)
    assert {k: (o, n) for k, (o, n, _) in loaded.index.items()} == plain
    # a half-written audit is not an audit: the consumer falls back
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    (d / "manifest.json").unlink()
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None


def test_load_source_audit_fails_closed_on_tampering(roots):
    ds, plan, paths = _seed(roots)
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    index_bytes = (d / INDEX_FILE).read_bytes()
    (d / INDEX_FILE).write_bytes(index_bytes.replace(b'"offset": 0,', b'"offset": 1,', 1))
    with pytest.raises(IntegrityError, match="^mismatch: source audit"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    (d / INDEX_FILE).write_bytes(index_bytes)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is not None
    audit_bytes = (d / AUDIT_FILE).read_bytes()
    (d / AUDIT_FILE).write_bytes(audit_bytes.replace(b'"line_count": 40', b'"line_count": 39'))
    with pytest.raises(IntegrityError, match="^mismatch: source audit"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    (d / AUDIT_FILE).write_bytes(audit_bytes)
    # the file grew after the audit (card untouched): size disagrees
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b'{"sample_id": "zz"}\n')
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl is"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    shutil.rmtree(d)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None


def test_models_validate_their_hashes():
    with pytest.raises(ValueError, match="samples_hash must be 64 hex"):
        SourceAudit(
            dataset="x",
            samples_hash="nope",
            size_bytes=1,
            line_count=1,
            created_at="2026-09-13T00:00:00.000Z",
            vcp_version="0.6.0",
        )
    with pytest.raises(ValueError, match="sha256 must be 64 hex"):
        IndexRow(sample_id="a", offset=0, length=1, sha256="nope")
    row = IndexRow(sample_id="a", offset=0, length=1, sha256="a" * 64)
    assert json.loads(row.model_dump_json())["length"] == 1
