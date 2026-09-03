import json

import pytest

from helpers import det_samples, make_card
from vcp.core.errors import IntegrityError, InvariantError, RegistryError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset, read_samples_jsonl, samples_digest, write_samples_jsonl
from vcp.data.schema import Box, Labels
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets


def test_write_is_sorted_and_hash_stable(tmp_path):
    samples = det_samples(5, seed=1)
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    h1 = write_samples_jsonl(p1, list(reversed(samples)))
    h2 = write_samples_jsonl(p2, samples)
    assert h1 == h2 == sha256_file(p1)
    ids = [json.loads(line)["sample_id"] for line in p1.read_text(encoding="utf-8").splitlines()]
    assert ids == sorted(ids) and len(ids) == 5
    assert b"\r\n" not in p1.read_bytes()
    assert [s.sample_id for s in read_samples_jsonl(p1)] == ids


def test_read_reports_line_number(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(
        '{"sample_id":"a","views":[{"path":"a.jpg"}],"label_source":"none"}\n{"sample_id":"b"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed) as ei:
        list(read_samples_jsonl(p))
    assert str(ei.value).endswith("bad.jsonl:2)")


def test_read_rejects_blank_line(tmp_path):
    p = tmp_path / "blank.jsonl"
    p.write_text(
        '{"sample_id":"a","views":[{"path":"a.jpg"}],"label_source":"none"}\n\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="blank"):
        list(read_samples_jsonl(p))


def test_duplicate_sample_id_rejected():
    samples = det_samples(2, seed=0)
    with pytest.raises(ValidationFailed, match="duplicate sample_id"):
        Dataset.from_parts(make_card("det"), samples + [samples[0]])


def test_from_parts_fills_count_runs_task_validation_and_rejects_unknown_task():
    ds = Dataset.from_parts(make_card("det"), det_samples(3))
    assert ds.card.sample_count == 3
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    with pytest.raises(ValidationFailed, match="unknown category id"):
        Dataset.from_parts(make_card("det"), bad)
    with pytest.raises(RegistryError):
        Dataset.from_parts(make_card("pose"), det_samples(1))


def test_samples_and_by_id_are_immutable_views():
    ds = Dataset.from_parts(make_card("det"), det_samples(2))
    assert isinstance(ds.samples, tuple)
    with pytest.raises(TypeError):
        ds.by_id["x"] = ds.samples[0]  # type: ignore[index]


def test_validate_checks_sample_count():
    ds = Dataset.from_parts(make_card("det"), det_samples(3))
    ds.card = ds.card.model_copy(update={"sample_count": 99})
    with pytest.raises(ValidationFailed, match="sample_count"):
        ds.validate()


def test_save_and_load_roundtrip(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(6, seed=2)
    ds = Dataset.from_parts(make_card("det", name="ds"), samples)
    ds.save(paths)
    assert paths.card_yaml.is_file() and paths.samples_jsonl.is_file()
    assert ds.card.sample_count == 6
    assert ds.card.samples_hash == sha256_file(paths.samples_jsonl)
    loaded = Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    assert loaded.card == ds.card
    assert loaded.samples == ds.samples
    assert loaded.by_id[samples[0].sample_id] == samples[0]


def test_save_refuses_name_mismatch(roots):
    paths = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name="ds"), det_samples(1))
    with pytest.raises(ValidationFailed, match="name"):
        ds.save(paths)


def test_load_detects_tampered_samples_and_missing_files(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="not found"):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    Dataset.from_parts(make_card("det", name="ds"), det_samples(3, seed=0)).save(paths)
    with paths.samples_jsonl.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"sample_id":"zzz","views":[{"path":"z.jpg"}],"label_source":"none"}\n')
    with pytest.raises(IntegrityError):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="sample_count"):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs, verify_hash=False)


def test_samples_digest_matches_written_file(tmp_path):
    samples = det_samples(4, seed=3)
    assert samples_digest(samples) == write_samples_jsonl(tmp_path / "s.jsonl", samples)


def test_subset_rejects_tampered_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(30, seed=0))
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    victim = next(iter(plan.ids_in("valA")))
    tampered = plan.model_copy(
        update={"assignment": {k: v for k, v in plan.assignment.items() if k != victim}}
    )
    with pytest.raises(InvariantError, match="does not cover"):
        ds.subset("valA", tampered)
