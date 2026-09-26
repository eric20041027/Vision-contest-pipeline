import json

import pytest
from typer.testing import CliRunner

from helpers import det_samples, seed_tiny
from vcp.cli import app
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.data.labels import LabelSetSpec, create_label_set, load_label_set, sample_ids_by_key
from vcp.data.schema import View


def _two_views(samples):
    """Every sample gets a lateral view and a case number in its meta."""
    return [
        s.model_copy(
            update={
                "views": [*s.views, View(path=f"{s.sample_id}_lat.jpg", width=8, height=8)],
                "meta": {"case": 1000 + i},
            }
        )
        for i, s in enumerate(samples)
    ]


def _csv(path, ids, *, col="id", bom=False):
    text = f"{col},y\n" + "".join(f"{i},1\n" for i in ids)
    path.write_bytes(((chr(0xFEFF) if bom else "") + text).encode("utf-8"))
    return path


def _spec(roots, file, **over):
    base = dict(
        name="tiny",
        plan_id="fixed-v1",
        subsets=["train"],
        file=file,
        id_field="sample_id",
        label_set_id="pseudo-v1",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return LabelSetSpec(**{**base, **over})


def test_a_label_set_of_train_samples_is_kept_as_an_artifact(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "pseudo.csv", [*train, "ext-1", "ext-2"])
    res = create_label_set(_spec(roots, f))
    assert not res.reused
    assert res.summary.rows == len(train) + 2 and res.summary.external == 2
    assert res.summary.matched == {"train": len(train)} and res.summary.subsets == ["train"]
    d = artifact_dir(roots.data, "label_set", "pseudo-v1")
    assert (d / "labels.csv").read_bytes() == f.read_bytes()
    assert load_label_set(roots.data, "pseudo-v1") == res.summary
    again = create_label_set(_spec(roots, f))
    assert again.reused and again.summary == res.summary


def test_labels_on_eval_or_sealed_samples_are_refused(roots, tmp_path):
    _, plan = seed_tiny(roots)
    ids = [
        *sorted(plan.ids_in("train"))[:3],
        *sorted(plan.ids_in("valA"))[:2],
        sorted(plan.ids_in("holdout"))[0],
    ]
    with pytest.raises(ValidationFailed, match="labels_outside_subsets") as ei:
        create_label_set(_spec(roots, _csv(tmp_path / "leak.csv", ids)))
    assert "2 in valA (eval)" in str(ei.value) and "1 in holdout (sealed)" in str(ei.value)
    assert ei.value.fields == {"outside_holdout": 1, "outside_valA": 2}
    assert not artifact_dir(roots.data, "label_set", "pseudo-v1").exists()


def test_a_sealed_subset_cannot_be_labelled_and_an_unknown_one_is_a_plan_mismatch(roots, tmp_path):
    _, plan = seed_tiny(roots)
    f = _csv(tmp_path / "h.csv", sorted(plan.ids_in("holdout")))
    with pytest.raises(ValidationFailed, match="labels_on_sealed"):
        create_label_set(_spec(roots, f, subsets=["holdout"]))
    with pytest.raises(PlanMismatchError):
        create_label_set(_spec(roots, f, subsets=["nope"]))


def test_a_row_given_twice_is_refused(roots, tmp_path):
    _, plan = seed_tiny(roots)
    one = sorted(plan.ids_in("train"))[0]
    with pytest.raises(ValidationFailed, match="duplicate_id"):
        create_label_set(_spec(roots, _csv(tmp_path / "d.csv", [one, one])))


def test_an_id_field_that_cannot_tell_samples_apart_is_refused():
    same = [
        s.model_copy(update={"views": [View(path="same.jpg", width=8, height=8)]})
        for s in det_samples(2, seed=0)
    ]
    with pytest.raises(ValidationFailed, match="duplicate_id"):
        sample_ids_by_key(same, "view_stem")
    with pytest.raises(ValidationFailed, match="id_field"):
        sample_ids_by_key(same, "filename")


@pytest.mark.parametrize(
    ("id_field", "key"),
    [
        ("sample_id", lambda s, i: s.sample_id),
        ("view_path", lambda s, i: f"{s.sample_id}_lat.jpg"),
        ("view_stem", lambda s, i: f"{s.sample_id}_lat"),
        ("meta.case", lambda s, i: str(1000 + i)),
    ],
)
def test_every_id_field_finds_its_sample(roots, tmp_path, id_field, key):
    samples = _two_views(det_samples(40, seed=0))
    _, plan = seed_tiny(roots, samples)
    train = plan.ids_in("train")
    ids = [key(s, i) for i, s in enumerate(samples) if s.sample_id in train]
    res = create_label_set(_spec(roots, _csv(tmp_path / "l.csv", ids), id_field=id_field))
    assert res.summary.matched == {"train": len(train)} and res.summary.external == 0


def test_csv_with_a_bom_and_its_own_id_column(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "bom.csv", train, col="study", bom=True)
    assert create_label_set(_spec(roots, f, id_col="study")).summary.matched == {
        "train": len(train)
    }
    with pytest.raises(ValidationFailed, match="not_found: column"):
        create_label_set(_spec(roots, f, label_set_id="x1"))


def test_jsonl_ids_may_be_numbers(roots, tmp_path):
    samples = _two_views(det_samples(40, seed=0))
    _, plan = seed_tiny(roots, samples)
    train = plan.ids_in("train")
    f = tmp_path / "soft.jsonl"
    f.write_text(
        "".join(
            json.dumps({"case": 1000 + i, "p": 0.5}) + "\n"
            for i, s in enumerate(samples)
            if s.sample_id in train
        ),
        encoding="utf-8",
    )
    res = create_label_set(_spec(roots, f, id_field="meta.case", id_col="case"))
    assert res.summary.format == "jsonl" and res.summary.matched == {"train": len(train)}


def test_unsupported_or_missing_files(roots, tmp_path):
    seed_tiny(roots)
    txt = tmp_path / "l.txt"
    txt.write_text("id\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unsupported_format"):
        create_label_set(_spec(roots, txt))
    with pytest.raises(ValidationFailed, match="not_found"):
        create_label_set(_spec(roots, tmp_path / "gone.csv"))


def test_the_same_id_with_other_labels_is_a_spec_mismatch(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    create_label_set(_spec(roots, _csv(tmp_path / "a.csv", train)))
    with pytest.raises(IntegrityError, match="spec_mismatch"):
        create_label_set(_spec(roots, _csv(tmp_path / "b.csv", train[:-1])))


def test_a_tampered_or_absent_label_set_does_not_load(roots, tmp_path):
    _, plan = seed_tiny(roots)
    create_label_set(_spec(roots, _csv(tmp_path / "a.csv", sorted(plan.ids_in("train")))))
    labels = artifact_dir(roots.data, "label_set", "pseudo-v1") / "labels.csv"
    labels.write_text("id\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="mismatch"):
        load_label_set(roots.data, "pseudo-v1")
    with pytest.raises(ValidationFailed, match="not_found"):
        load_label_set(roots.data, "nope")


runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_cli_labels(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "a.csv", [*train, "ext"])
    base = ["data", "labels", "--name", "tiny", "--plan", "fixed-v1", "--subset", "train"]
    base += ["--id-field", "sample_id"]
    ok = runner.invoke(app, [*base, "--file", str(f), "--id", "pseudo-v1"])
    assert ok.exit_code == 0, ok.output
    v = _verdict(ok.output)
    assert v.startswith("VERDICT cmd=labels status=OK")
    for part in (f"rows={len(train) + 1}", f"matched={len(train)}", "external=1"):
        assert part in v
    assert "reused=false" in v and "subsets=train" in v and "id=pseudo-v1" in v
    leak = _csv(tmp_path / "b.csv", sorted(plan.ids_in("valA")))
    bad = runner.invoke(app, [*base, "--file", str(leak), "--id", "leak-v1"])
    assert bad.exit_code == 1 and "status=FAIL" in _verdict(bad.output)
    assert "labels_outside_subsets" in _verdict(bad.output) and "id=leak-v1" in _verdict(bad.output)
    again = runner.invoke(app, [*base, "--file", str(f), "--id", "pseudo-v1", "--json"])
    assert again.exit_code == 0 and json.loads(again.stdout)["fields"]["reused"] is True
