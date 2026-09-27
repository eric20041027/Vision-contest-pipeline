import json

import pytest
from typer.testing import CliRunner

from helpers import det_samples, seed_tiny
from vcp.cli import app
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data import labels as labels_mod
from vcp.data.labels import LabelSetSpec, create_label_set, load_label_set, sample_ids_by_key
from vcp.data.schema import View
from vcp.data.split import save_plan


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
    assert ei.value.fields == {"outside": 3}
    assert not artifact_dir(roots.data, "label_set", "pseudo-v1").exists()


def test_a_label_on_a_sample_the_plan_leaves_unassigned_is_refused(roots, tmp_path):
    _, plan = seed_tiny(roots)
    left_out = sorted(plan.ids_in("train"))[0]
    partial = plan.model_copy(
        update={
            "plan_id": "partial-v1",
            "assignment": {k: v for k, v in plan.assignment.items() if k != left_out},
        }
    )
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    save_plan(partial, paths)
    f = _csv(tmp_path / "u.csv", [left_out])
    with pytest.raises(ValidationFailed, match="labels_outside_subsets") as ei:
        create_label_set(_spec(roots, f, plan_id="partial-v1"))
    assert "1 in unassigned (unassigned)" in str(ei.value) and ei.value.fields == {"outside": 1}


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
    with pytest.raises(ValidationFailed, match="duplicate_id") as ei:
        sample_ids_by_key(same, "view_stem")
    assert ei.value.fields == {"duplicate": "same"}
    with pytest.raises(ValidationFailed, match="id_field"):
        sample_ids_by_key(same, "filename")


def test_a_padded_meta_value_still_finds_its_sample(roots, tmp_path):
    """File ids are stripped, so meta values are too: a padded case number on a valA sample
    must not slip past the leak check as an external row."""
    samples = [
        s.model_copy(update={"meta": {"case": f" {1000 + i} "}})
        for i, s in enumerate(det_samples(40, seed=0))
    ]
    _, plan = seed_tiny(roots, samples)
    val = plan.ids_in("valA")
    ids = [str(1000 + i) for i, s in enumerate(samples) if s.sample_id in val]
    with pytest.raises(ValidationFailed, match="labels_outside_subsets"):
        create_label_set(_spec(roots, _csv(tmp_path / "m.csv", ids), id_field="meta.case"))


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


def test_a_blank_id_or_a_broken_jsonl_row_is_invalid(roots, tmp_path):
    """A row with no id is a malformed row, not an external one."""
    _, plan = seed_tiny(roots)
    one = sorted(plan.ids_in("train"))[0]
    blank_csv = tmp_path / "blank.csv"
    blank_csv.write_text(f"id,y\n{one},1\n  ,1\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match=r"^invalid: blank id at blank\.csv:3$"):
        create_label_set(_spec(roots, blank_csv))
    null_jsonl = tmp_path / "null.jsonl"
    null_jsonl.write_text(json.dumps({"id": one}) + "\n" + '{"id": null}\n', encoding="utf-8")
    with pytest.raises(ValidationFailed, match=r"^invalid: blank id at null\.jsonl:2$"):
        create_label_set(_spec(roots, null_jsonl))
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"id": "s0000"\n', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="^invalid: label row") as ei:
        create_label_set(_spec(roots, broken))
    assert ei.value.location == "broken.jsonl:1"
    assert not artifact_dir(roots.data, "label_set", "pseudo-v1").exists()


def test_a_label_file_that_changes_while_it_is_checked_is_drift(roots, tmp_path, monkeypatch):
    """The bytes kept are the bytes checked: a change between the read and the copy fails."""
    _, plan = seed_tiny(roots)
    f = _csv(tmp_path / "a.csv", sorted(plan.ids_in("train")))
    real = labels_mod.read_ids

    def read_then_change(path, id_col):
        out = real(path, id_col)
        path.write_text("id,y\nchanged,1\n", encoding="utf-8")
        return out

    monkeypatch.setattr(labels_mod, "read_ids", read_then_change)
    with pytest.raises(IntegrityError, match="^drift: "):
        create_label_set(_spec(roots, f))
    assert not (artifact_dir(roots.data, "label_set", "pseudo-v1") / "manifest.json").exists()


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


LABELS = (
    *("data", "labels", "--name", "tiny", "--plan", "fixed-v1"),
    *("--subset", "train", "--id-field", "sample_id"),
)


def test_cli_a_repeated_row_keeps_the_label_set_id_in_the_verdict(roots, tmp_path):
    _, plan = seed_tiny(roots)
    one = sorted(plan.ids_in("train"))[0]
    f = _csv(tmp_path / "d.csv", [one, one])
    r = runner.invoke(app, [*LABELS, "--file", str(f), "--id", "pseudo-v1"])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "duplicate_id" in v
    assert "id=pseudo-v1" in v and f"duplicate={one}" in v


def test_cli_a_headerless_csv_fails_without_echoing_its_first_row(roots, tmp_path):
    """With no header row the first data row stands in for one: only its column count may
    reach the VERDICT or the log, never a cell."""
    _, plan = seed_tiny(roots)
    rows = [f"{i},zz-leak-marker-{n},0.93\n" for n, i in enumerate(sorted(plan.ids_in("train")))]
    f = tmp_path / "pseudo.csv"
    f.write_text("".join(rows), encoding="utf-8")
    r = runner.invoke(app, [*LABELS, "--file", str(f), "--id", "p1"])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "not_found: column" in v
    assert "3 columns" in v and "id=p1" in v
    assert "zz-leak-marker" not in r.output
    logs = "".join(p.read_text(encoding="utf-8") for p in (roots.data / "logs").iterdir())
    assert "status=FAIL" in logs and "zz-leak-marker" not in logs


def test_cli_a_file_where_no_row_matches_warns_but_is_kept(roots, tmp_path):
    """Every row external is most likely a wrong --id-field / --id-col: kept, but a WARN."""
    seed_tiny(roots)
    f = _csv(tmp_path / "ext.csv", ["ext-1", "ext-2"])
    for reused in ("false", "true"):
        r = runner.invoke(app, [*LABELS, "--file", str(f), "--id", "ext-v1"])
        v = _verdict(r.output)
        assert r.exit_code == 0 and v.startswith("VERDICT cmd=labels status=WARN")
        assert "matched=0" in v and "external=2" in v and f"reused={reused}" in v
        assert "no row matched a dataset sample" in r.output
    assert (artifact_dir(roots.data, "label_set", "ext-v1") / "manifest.json").is_file()
