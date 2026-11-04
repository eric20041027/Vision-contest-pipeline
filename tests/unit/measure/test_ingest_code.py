"""``vcp eval ingest --code LABEL=ID`` (spec 2026-10-31 §5.8): a snapshot taken with ``vcp code
snapshot`` is bound to an ingested run as ``code.<label>`` (binding ``manual``, no attempt). A
snapshot that is not there is ``not_found:``, one that does not hold is ``mismatch:``, a malformed
item is ``invalid:`` -- every one of them before the first byte is written. ``code_ref`` is the
one place the ref is made; ``eval status`` grades each run's ``code`` with one shared memo."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from helpers import det_with_runs, make_label_set, perfect_predictions
from vcp.cli import app
from vcp.code.scan import scan_root
from vcp.code.snapshot import KIND, code_ref, write_snapshot
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data import evidence as evidence_module
from vcp.data.evidence_ref import EvidenceRef
from vcp.measure.ingest import IngestSpec, ingest, parse_code_args
from vcp.measure.predictions import write_predictions
from vcp.measure.report import status
from vcp.measure.runs import load_run, run_dir

CMD = "test.ingest_code"
GHOST = "code-0000000000000000"  # well-formed, and no snapshot has it


@pytest.fixture
def world(roots, tmp_path):
    """The tiny detection dataset with its two runs, and a prediction file for one more."""
    ds, plan, _ = det_with_runs(roots, tmp_path, n=20)
    preds = tmp_path / "ext-valA.jsonl"
    write_predictions(preds, perfect_predictions(ds.subset("valA", plan), ds.card))
    return SimpleNamespace(ds=ds, plan=plan, preds=preds)


def _snapshot(roots, tmp_path, name="src", extra=b"") -> str:
    src = tmp_path / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.py").write_bytes(b"A = 1\n" + extra)
    (src / "b.py").write_bytes(b"B = 2\n")
    scanned = scan_root(src, kind="dir", keep="capped")
    outcome = write_snapshot(roots.data, scanned, command=CMD)
    assert outcome.state in ("created", "reused"), outcome
    return outcome.snapshot_id


def _break(roots, snapshot_id: str) -> None:
    """A kept file of the snapshot is not what the index says any more."""
    (artifact_dir(roots.data, KIND, snapshot_id) / "files" / "a.py").write_bytes(b"A = 99\n")


def _args(world, run="ext", *extra) -> list[str]:
    base = ["eval", "ingest", "--run", run, "--dataset", "tiny", "--plan", "fixed-v1"]
    base += ["--subset", "valA", "--format", "jsonl", "--src", str(world.preds)]
    base += ["--trained-on", "train"]
    return [*base, *extra]


def _cli(world, *extra, run="ext"):
    return CliRunner().invoke(app, _args(world, run, *extra))


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _spec(roots, world, **over) -> IngestSpec:
    base = dict(
        run_id="ext",
        dataset="tiny",
        plan_id="fixed-v1",
        subset="valA",
        format="jsonl",
        src=world.preds,
        trained_on=["train"],
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return IngestSpec(**{**base, **over})


def _evidence_dir(roots):
    return roots.data / "artifacts" / "evidence"


# -- parse_code_args -------------------------------------------------------------------------------


def test_parse_code_args_splits_label_and_id_in_order():
    assert parse_code_args([]) == []
    assert parse_code_args(["cwd=code-aaaaaaaaaaaaaaaa", "extra.v2=code-bbbbbbbbbbbbbbbb"]) == [
        ("cwd", "code-aaaaaaaaaaaaaaaa"),
        ("extra.v2", "code-bbbbbbbbbbbbbbbb"),
    ]
    # one snapshot may stand for two roots
    assert parse_code_args(["a=code-aaaaaaaaaaaaaaaa", "b=code-aaaaaaaaaaaaaaaa"]) == [
        ("a", "code-aaaaaaaaaaaaaaaa"),
        ("b", "code-aaaaaaaaaaaaaaaa"),
    ]


@pytest.mark.parametrize(
    "items",
    [
        ["bad/label=code-aaaaaaaaaaaaaaaa"],  # a label is a name
        ["has space=code-aaaaaaaaaaaaaaaa"],
        ["nolabel"],  # no `=`
        ["=code-aaaaaaaaaaaaaaaa"],  # no label
        ["cwd="],  # no id
        ["cwd=bad/id"],  # an id is a name too: it becomes a path segment
        ["cwd=a=b"],
        ["cwd=code-aaaaaaaaaaaaaaaa", "cwd=code-bbbbbbbbbbbbbbbb"],  # a label given twice
    ],
)
def test_parse_code_args_refuses_what_is_not_label_equals_id(items):
    with pytest.raises(ValidationFailed, match=r"^invalid: "):
        parse_code_args(items)


# -- code_ref --------------------------------------------------------------------------------------


def test_code_ref_pins_an_intact_snapshot(roots, tmp_path):
    sid = _snapshot(roots, tmp_path)
    ref = code_ref(roots.data, "cwd", sid, attempt=None, binding="manual")
    assert isinstance(ref, EvidenceRef)
    assert (ref.name, ref.role, ref.kind, ref.artifact_id) == ("code.cwd", "code", KIND, sid)
    assert (ref.attempt, ref.binding) == (None, "manual")
    assert ref.manifest_sha256 == sha256_file(artifact_dir(roots.data, KIND, sid) / "manifest.json")
    again = code_ref(roots.data, "cwd", sid, attempt=3, binding="cli")
    assert (again.attempt, again.binding) == (3, "cli")


def test_code_ref_refuses_a_snapshot_that_is_missing_partial_or_broken(roots, tmp_path):
    with pytest.raises(ValidationFailed, match=r"^not_found: "):
        code_ref(roots.data, "cwd", GHOST, attempt=None, binding="manual")
    sid = _snapshot(roots, tmp_path)
    (artifact_dir(roots.data, KIND, sid) / "manifest.json").unlink()
    with pytest.raises(ValidationFailed, match=r"^partial: "):  # claimed, never committed
        code_ref(roots.data, "cwd", sid, attempt=None, binding="manual")
    other = _snapshot(roots, tmp_path, "other", extra=b"# other\n")
    _break(roots, other)
    with pytest.raises(IntegrityError, match=r"^mismatch: code_snapshot/code-"):
        code_ref(roots.data, "cwd", other, attempt=None, binding="manual")


# -- eval ingest --code ----------------------------------------------------------------------------


def test_ingest_code_attaches_a_snapshot(roots, tmp_path, world):
    sid = _snapshot(roots, tmp_path)
    r = _cli(world, "--code", f"cwd={sid}")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert v.startswith("VERDICT cmd=eval.ingest status=OK") and "code=snapshot" in v
    assert "evidence=0" in v  # vcp's own refs are not evidence
    (ref,) = load_run(roots.data, "ext").evidence
    assert ref.name == "code.cwd" and ref.binding == "manual" and ref.attempt is None
    assert (ref.role, ref.kind, ref.artifact_id) == ("code", KIND, sid)


def test_ingest_without_code_is_declared(roots, tmp_path, world):
    r = _cli(world)
    assert r.exit_code == 0, r.output
    assert "code=declared" in _verdict(r.output)
    assert load_run(roots.data, "ext").evidence == []


def test_ingest_code_is_repeatable_and_sits_beside_evidence_and_labels(roots, tmp_path, world):
    make_label_set(roots, tmp_path, world.plan)
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text("t", encoding="utf-8")
    first = _snapshot(roots, tmp_path)
    second = _snapshot(roots, tmp_path, "other", extra=b"# other\n")
    r = _cli(
        world,
        "--evidence",
        f"teacher={teacher}",
        "--labels",
        "pseudo-v1",
        "--code",
        f"cwd={first}",
        "--code",
        f"extra={second}",
    )
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "code=snapshot" in v and "evidence=2" in v and "labels=pseudo-v1" in v
    refs = load_run(roots.data, "ext").evidence
    assert [(x.name, x.kind) for x in refs] == [
        ("teacher", "evidence"),
        ("pseudo-v1", "label_set"),
        ("code.cwd", KIND),
        ("code.extra", KIND),
    ]


def test_ingest_code_again_adds_a_row_only_for_another_snapshot(roots, tmp_path, world):
    first = _snapshot(roots, tmp_path)
    assert _cli(world, "--code", f"cwd={first}").exit_code == 0
    again = _cli(world, "--replace", "--code", f"cwd={first}")
    assert again.exit_code == 0, again.output
    assert len(load_run(roots.data, "ext").evidence) == 1  # the same manifest: no new row
    second = _snapshot(roots, tmp_path, "other", extra=b"# other\n")
    moved = _cli(world, "--replace", "--code", f"cwd={second}")
    assert moved.exit_code == 0, moved.output
    refs = load_run(roots.data, "ext").evidence
    assert [x.artifact_id for x in refs] == [first, second]  # the list only grows
    assert "code=snapshot" in _verdict(moved.output)


def test_ingest_code_refuses_a_missing_or_broken_snapshot(roots, tmp_path, world):
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text("t", encoding="utf-8")
    card = run_dir(roots.data, "ext") / "run.yaml"

    # a new run: nothing is created, not even the evidence that came with the same command
    missing = _cli(world, "--evidence", f"teacher={teacher}", "--code", f"cwd={GHOST}")
    assert missing.exit_code == 1, missing.output
    v = _verdict(missing.output)
    assert v.startswith("VERDICT cmd=eval.ingest status=FAIL") and "not_found:" in v
    assert "dataset=tiny" in v and "run=ext" in v  # the command keeps saying what it was
    assert not card.exists()
    assert not _evidence_dir(roots).exists() or not any(_evidence_dir(roots).iterdir())

    broken = _snapshot(roots, tmp_path)
    _break(roots, broken)
    bad = _cli(world, "--evidence", f"teacher={teacher}", "--code", f"cwd={broken}")
    assert bad.exit_code == 1, bad.output
    assert "status=FAIL" in _verdict(bad.output) and "mismatch:" in _verdict(bad.output)
    assert not card.exists()
    assert not _evidence_dir(roots).exists() or not any(_evidence_dir(roots).iterdir())

    # an existing run: its card is the same bytes, its predictions are not replaced
    assert _cli(world).exit_code == 0
    before = card.read_bytes()
    for bad_id, word in ((GHOST, "not_found:"), (broken, "mismatch:")):
        r = _cli(world, "--replace", "--code", f"cwd={bad_id}")
        assert r.exit_code == 1 and word in _verdict(r.output), r.output
        assert card.read_bytes() == before
    assert not (run_dir(roots.data, "ext") / "history.jsonl").exists()


def test_ingest_code_label_must_be_a_name(roots, tmp_path, world):
    sid = _snapshot(roots, tmp_path)
    for item in (f"bad/label={sid}", sid, f"cwd=bad/{sid}"):
        r = _cli(world, "--code", item)
        assert r.exit_code == 1, r.output
        v = _verdict(r.output)
        assert "status=FAIL" in v and "invalid" in v, v
        assert not (run_dir(roots.data, "ext") / "run.yaml").exists()
    twice = _cli(world, "--code", f"cwd={sid}", "--code", f"cwd={sid}")
    assert twice.exit_code == 1 and "invalid:" in _verdict(twice.output)
    assert not (run_dir(roots.data, "ext") / "run.yaml").exists()


def test_ingest_code_name_is_vcps_own_and_must_be_free(roots, tmp_path, world):
    sid = _snapshot(roots, tmp_path)
    make_label_set(roots, tmp_path, world.plan, label_set_id="code.cwd")
    # a --labels id named like the ref: both are in this command, nothing is written for either
    clash = _cli(world, "--labels", "code.cwd", "--code", f"cwd={sid}")
    assert clash.exit_code == 1 and "evidence_conflict:" in _verdict(clash.output), clash.output
    assert not (run_dir(roots.data, "ext") / "run.yaml").exists()
    # a name the run already holds under another role: the list never renames a name
    assert _cli(world, "--labels", "code.cwd").exit_code == 0
    card = run_dir(roots.data, "ext") / "run.yaml"
    before = card.read_bytes()
    held = _cli(world, "--replace", "--code", f"cwd={sid}")
    assert held.exit_code == 1 and "evidence_conflict:" in _verdict(held.output), held.output
    assert card.read_bytes() == before
    # and --evidence still cannot take a name that is vcp's own
    note = tmp_path / "note.txt"
    note.write_text("n", encoding="utf-8")
    refused = _cli(world, "--replace", "--evidence", f"code.other={note}")
    assert refused.exit_code == 1 and "role_reserved:" in _verdict(refused.output)


def test_ingest_result_carries_the_grade_the_saved_card_has(roots, tmp_path, world):
    sid = _snapshot(roots, tmp_path)
    plain = ingest(_spec(roots, world))
    assert plain.code == "declared"
    bound = ingest(_spec(roots, world, replace=True, code=[f"cwd={sid}"]))
    assert bound.code == "snapshot"
    assert [r.name for r in bound.run.evidence] == ["code.cwd"]
    _break(roots, sid)  # the grade is read when it is asked: a snapshot that fails it is declared
    assert ingest(_spec(roots, world, replace=True)).code == "declared"


# -- the status view -------------------------------------------------------------------------------


def test_status_grades_each_run_with_one_memo(roots, tmp_path, world, monkeypatch):
    sid = _snapshot(roots, tmp_path)
    for run in ("ext1", "ext2", "ext3"):
        assert _cli(world, "--code", f"cwd={sid}", run=run).exit_code == 0
    asked: list[str] = []
    real = evidence_module.ref_intact

    def counted(data_root, ref):
        asked.append(ref.artifact_id)
        return real(data_root, ref)

    monkeypatch.setattr(evidence_module, "ref_intact", counted)
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    result = status(paths)
    assert result.code == {
        "ext1": "snapshot",
        "ext2": "snapshot",
        "ext3": "snapshot",
        "noisy": "declared",
        "perfect": "declared",
    }
    assert asked == [sid]  # three runs share it: hashed once
    # a memo is for one command: the next call looks again, and sees the change
    _break(roots, sid)
    assert set(status(paths).code.values()) == {"declared"}
