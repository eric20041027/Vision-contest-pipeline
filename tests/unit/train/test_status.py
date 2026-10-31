import shutil

import pytest

from helpers import seed_tiny
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.data.evidence import RunScope, attach_evidence
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import (
    append_event,
    bind_ref,
    events_path,
    load_record,
    read_events,
    save_record,
)
from vcp.train.schema import Attempt, TrainRecord, UploadRecord
from vcp.train.status import TmpFile, status, upload_run

STAMP = "2026-09-05T00:00:00.000Z"


def _seeded(roots):
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best")
    (w / "last.pt").write_bytes(b"last")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt"))
    save_record(roots.data, rec)
    return rec, w


def test_status_counts_backed_and_running(roots, tmp_path):
    rec, w = _seeded(roots)
    st = status(roots.data, "r1")
    assert st.backed == 0 and st.unbacked == ["work/weights/best.pt", "work/weights/last.pt"]
    assert st.missing == [] and st.drift == [] and st.running == 0
    upload_run(roots.data, "r1", str(tmp_path / "vault"), only_final=True)
    st = status(roots.data, "r1")
    assert st.backed == 1 and st.unbacked == ["work/weights/last.pt"]
    (w / "best.pt").write_bytes(b"tampered")
    (w / "last.pt").unlink()
    st = status(roots.data, "r1")
    assert st.missing == ["work/weights/last.pt"] and st.drift == []
    st = status(roots.data, "r1", verify=True)
    assert st.drift == ["work/weights/best.pt", "work/weights/last.pt"]
    running = rec.model_copy(
        update={
            "attempts": [
                rec.attempts[0].model_copy(update={"status": "running", "exit_code": None})
            ]
        }
    )
    save_record(roots.data, running)
    assert status(roots.data, "r1").running == 1
    with pytest.raises(ValidationFailed, match="not found"):
        status(roots.data, "ghost")


def test_status_keys_backed_by_bytes_not_path(roots):
    """C2 regression: a --resume that changes a checkpoint's bytes adds a second
    CheckpointRecord for the same path. A verified upload of the OLD bytes must not mark the
    NEW record backed -- backed-ness has to be keyed by sha256, not by path."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best-old")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    old_sha = sha256_file(w / "best.pt")
    (w / "best.pt").write_bytes(b"best-new")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    new_sha = sha256_file(w / "best.pt")
    assert old_sha != new_sha
    rec = mark_final(rec, "work/weights/best.pt", new_sha)
    old_upload = UploadRecord(
        dest="vault", kind="local", name="best.pt", sha256=old_sha, verified=True, uploaded_at=STAMP
    )
    rec = rec.model_copy(update={"uploads": [old_upload]})
    save_record(roots.data, rec)

    st = status(roots.data, "r1")
    assert st.backed == 1 and st.unbacked == ["work/weights/best.pt"]

    rec = rec.model_copy(
        update={"uploads": [old_upload, old_upload.model_copy(update={"sha256": new_sha})]}
    )
    save_record(roots.data, rec)
    st = status(roots.data, "r1")
    assert st.unbacked == [] and st.backed == 2


def test_status_counts_superseded_bytes_apart_from_unbacked(roots, tmp_path):
    """5-10: a --resume that changes a checkpoint's bytes leaves the older record behind. Those
    bytes are gone from the path and, if they were never uploaded, no copy will ever appear --
    so `unbacked` listed them forever: honest, and pure noise. They are counted as `superseded`
    instead, leaving `unbacked` to mean "current bytes with no copy" -- the number to act on."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best-old")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    old_sha = sha256_file(w / "best.pt")
    (w / "best.pt").write_bytes(b"best-new")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=2)
    save_record(roots.data, rec)

    # before any upload: the current bytes are unbacked, the old ones are already superseded
    st = status(roots.data, "r1")
    assert st.unbacked == ["work/weights/best.pt"] and st.superseded == ["work/weights/best.pt"]
    assert st.backed == 0

    # uploading takes the newest record of the path (spec 14-4), so only the new bytes get a copy
    _, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 1
    st = status(roots.data, "r1")
    assert st.unbacked == [] and st.superseded == ["work/weights/best.pt"] and st.backed == 1
    assert old_sha not in {u.sha256 for u in st.record.uploads}


def test_upload_run_records_and_is_idempotent(roots, tmp_path):
    rec, w = _seeded(roots)
    rec2, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 2 and len(rec2.uploads) == 2 and all(u.verified for u in rec2.uploads)
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["uploaded", "uploaded"] and events[0]["attempt"] == 1
    rec3, again = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert again.uploaded == 0 and again.skipped == 2 and len(rec3.uploads) == 2
    assert len(read_events(roots.data, "r1")) == 2  # a re-verification is not an event


def test_superseded_names_a_path_once_however_many_times_it_was_replaced(roots):
    """Three registrations of one path with three shas, none uploaded: one superseded entry,
    not two -- the count is files, and the human lines must not repeat."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    for n, payload in enumerate((b"v1", b"v2", b"v3"), start=1):
        (w / "best.pt").write_bytes(payload)
        rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=n)
    save_record(roots.data, rec)
    st = status(roots.data, "r1")
    assert st.superseded == ["work/weights/best.pt"] and st.unbacked == ["work/weights/best.pt"]


def test_five_folds_named_model_pt_all_upload_and_leave_nothing_unbacked(roots, tmp_path):
    """VCP-039 acceptance: five fold-k/model.pt checkpoints upload under distinct names and are
    all verified, so a multi-fold run is no longer stuck at unbacked=5."""
    rec, w = _seeded(roots)
    folds = []
    for k in range(5):
        fold = roots.data / "work" / f"fold-{k}" / "model.pt"
        fold.parent.mkdir(parents=True)
        fold.write_bytes(f"fold {k}".encode())
        folds.append(fold)
    rec, _ = register(rec, folds, data_root=roots.data, attempt=1)
    save_record(roots.data, rec)

    _, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))

    assert sorted(r.name for r in out.records if r.name.endswith("model.pt")) == [
        f"fold-{k}__model.pt" for k in range(5)
    ]
    assert all(r.verified for r in out.records)
    assert status(roots.data, "r1").unbacked == []


def test_status_lists_leftover_tmp_files_in_the_run_and_beside_checkpoints(roots):
    """5.5: a failed torch.save left a 266 MB `.tmp` beside the checkpoints and nobody noticed.
    The run directory is scanned recursively, each registered checkpoint's directory is not, and
    the name suffix is matched without regard to case."""
    _, w = _seeded(roots)
    train_dir = roots.data / "runs" / "r1" / "train"
    train_dir.mkdir(parents=True, exist_ok=True)
    (train_dir / ".train.yaml.0a1b2c3d.tmp").write_bytes(b"12345")
    (w / "resume.pth.TMP").write_bytes(b"x" * 266)
    (w / "sub").mkdir()
    (w / "sub" / "deep.tmp").write_bytes(b"deep")  # beside-checkpoint listing is not recursive
    (roots.data / "elsewhere").mkdir()
    (roots.data / "elsewhere" / "junk.tmp").write_bytes(b"junk")  # nobody's directory

    assert status(roots.data, "r1").tmp == [
        TmpFile("runs/r1/train/.train.yaml.0a1b2c3d.tmp", 5),
        TmpFile("work/weights/resume.pth.TMP", 266),
    ]


def test_status_without_tmp_files_and_with_a_vanished_checkpoint_dir(roots):
    _, w = _seeded(roots)
    assert status(roots.data, "r1").tmp == []
    shutil.rmtree(w)
    assert status(roots.data, "r1").tmp == []


def test_a_checkpoint_outside_the_data_root_has_its_tmp_listed_by_absolute_path(roots, tmp_path):
    """store_path's rule: under the data root -> relative posix, anywhere else -> absolute posix."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "m.pt").write_bytes(b"model")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [outside / "m.pt"], data_root=roots.data, attempt=1)
    save_record(roots.data, rec)
    (outside / "m.pt.tmp").write_bytes(b"tt")

    assert status(roots.data, "r1").tmp == [TmpFile((outside / "m.pt.tmp").resolve().as_posix(), 2)]


def test_status_lists_a_tmp_file_once_when_two_checkpoints_share_a_directory(roots):
    """`_seeded` registers best.pt and last.pt in one directory; a `.tmp` there is one finding,
    not one per checkpoint."""
    _, w = _seeded(roots)
    (w / "resume.pth.tmp").write_bytes(b"r")
    assert status(roots.data, "r1").tmp == [TmpFile("work/weights/resume.pth.tmp", 1)]


def _unregistered(roots, *, cwd="work"):
    """A run whose first checkpoint save failed: a record with no checkpoint registered, so
    nothing but the `started` event says where the child was writing."""
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd=cwd,
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="failed", exit_code=1
            )
        ],
    )
    save_record(roots.data, rec)
    return rec


def _started(roots, attempt=1, **payload):
    append_event(roots.data, "r1", "started", attempt, command=["python"], seed=None, **payload)


def test_status_scans_the_glob_directories_of_the_started_event(roots):
    """The RSNA failure mode: the first checkpoint save died, nothing was ever registered, and
    the `.tmp` it left in the checkpoint directory was invisible. The `started` event's
    patterns say where to look: the nearest existing directory of cwd + the pattern's fixed
    prefix, for `--checkpoints` and for `--final`."""
    _unregistered(roots)
    _started(roots, checkpoints=["ckpt/epoch-*.pt"], final="out/best.pt")
    ckpt = roots.data / "work" / "ckpt"
    ckpt.mkdir(parents=True)
    (ckpt / "epoch-1.pt.tmp").write_bytes(b"tt")
    (ckpt / "sub").mkdir()
    (ckpt / "sub" / "deep.tmp").write_bytes(b"deep")  # not recursive
    out = roots.data / "work" / "out"
    out.mkdir()
    (out / "best.pt.tmp").write_bytes(b"b")  # `--final` is a pattern too
    (roots.data / "work" / "elsewhere.tmp").write_bytes(b"e")  # not on any pattern's path

    assert status(roots.data, "r1").tmp == [
        TmpFile("work/ckpt/epoch-1.pt.tmp", 2),
        TmpFile("work/out/best.pt.tmp", 1),
    ]


def test_status_measures_a_glob_directory_the_child_never_created_where_it_would_be(roots):
    _unregistered(roots)
    _started(roots, checkpoints=["not/yet/there/*.pt"], final=None)
    (roots.data / "work").mkdir()
    (roots.data / "work" / "stray.tmp").write_bytes(b"s")  # the nearest existing directory

    assert status(roots.data, "r1").tmp == [TmpFile("work/stray.tmp", 1)]


def test_status_lists_the_tmp_of_an_absolute_pattern_outside_the_data_root_by_absolute_path(
    roots, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "m.pt.tmp").write_bytes(b"tt")
    _unregistered(roots)
    _started(roots, checkpoints=[(outside / "*.pt").as_posix()], final=None)

    assert status(roots.data, "r1").tmp == [TmpFile((outside / "m.pt.tmp").resolve().as_posix(), 2)]


def test_status_does_not_recurse_for_a_double_star_pattern(roots):
    """`**/x.pt` has no fixed prefix, so its directory is the cwd itself; walking under it would
    walk venvs. Only files directly in the cwd count."""
    _unregistered(roots)
    _started(roots, checkpoints=["**/x.pt"], final=None)
    work = roots.data / "work"
    (work / ".venv" / "lib").mkdir(parents=True)
    (work / ".venv" / "lib" / "cache.tmp").write_bytes(b"c")
    (work / "x.pt.tmp").write_bytes(b"x")

    assert status(roots.data, "r1").tmp == [TmpFile("work/x.pt.tmp", 1)]


def test_status_adds_nothing_for_a_started_event_without_the_keys(roots):
    """An older vcp wrote no patterns; its runs read exactly as before."""
    _unregistered(roots)
    _started(roots)
    ckpt = roots.data / "work" / "ckpt"
    ckpt.mkdir(parents=True)
    (ckpt / "epoch-1.pt.tmp").write_bytes(b"tt")

    assert status(roots.data, "r1").tmp == []


def test_status_adds_nothing_when_there_is_no_event_log_at_all(roots):
    _unregistered(roots)
    (roots.data / "work").mkdir()
    (roots.data / "work" / "x.tmp").write_bytes(b"x")
    assert status(roots.data, "r1").tmp == []


def test_status_reads_the_event_log_tolerantly(roots):
    """A line that does not parse -- a torn last line from a crash, a half-written multi-byte
    character, a row that is not an object -- is skipped; it must not make `train status`
    ABORT, and the readable `started` rows still count."""
    _unregistered(roots)
    _started(roots, checkpoints=["ckpt/*.pt"], final=None)
    log = events_path(roots.data, "r1")
    with log.open("ab") as f:
        f.write(b"this is not json\n")
        f.write(b"[1, 2]\n")
        f.write(b'{"event": "started", "checkpoints": "ckpt/*.pt"}\n')  # not a list
        f.write(b'{"event": "started", "checkpoints": [5, null, ["x"]], "final": 3}\n')
        f.write(b'{"event": "note", "checkpoints": ["ignored/*.pt"]}\n')  # not a started event
        f.write('{"ts": "2026-10-30T00:00:00.000Z", "event": "started", "cwd": "é'.encode()[:-1])
    ckpt = roots.data / "work" / "ckpt"
    ckpt.mkdir(parents=True)
    (ckpt / "epoch-1.pt.tmp").write_bytes(b"tt")

    assert status(roots.data, "r1").tmp == [TmpFile("work/ckpt/epoch-1.pt.tmp", 2)]


def test_status_survives_a_log_line_too_deeply_nested_to_parse(roots):
    """`json.loads` answers a runaway `[[[[...` with RecursionError, not ValueError; a corrupt
    line of that shape is skipped like any other."""
    _unregistered(roots)
    _started(roots, checkpoints=["ckpt/*.pt"], final=None)
    with events_path(roots.data, "r1").open("ab") as f:
        f.write(b"[" * 200_000 + b"\n")
    ckpt = roots.data / "work" / "ckpt"
    ckpt.mkdir(parents=True)
    (ckpt / "epoch-1.pt.tmp").write_bytes(b"tt")

    assert status(roots.data, "r1").tmp == [TmpFile("work/ckpt/epoch-1.pt.tmp", 2)]


def test_status_does_not_scan_the_parents_of_a_cwd_that_is_gone(roots, tmp_path):
    """A stale absolute `cwd` in an old event: climbing from it to the nearest existing
    directory would list an unrelated ancestor's `.tmp` files."""
    _unregistered(roots)
    (tmp_path / "stray.tmp").write_bytes(b"s")  # an ancestor of the stale cwd
    gone = tmp_path / "gone" / "run"  # never created
    _started(roots, cwd=str(gone), checkpoints=["ckpt/*.pt"], final="out/best.pt")

    assert status(roots.data, "r1").tmp == []


def test_status_skips_a_relative_pattern_when_the_records_cwd_is_gone(roots):
    """The same bound for an event without a `cwd`: the record's is used, and if it is gone the
    pattern says nothing (the data root above it is nobody's checkpoint directory)."""
    _unregistered(roots)  # cwd "work", never created
    (roots.data / "junk.tmp").write_bytes(b"j")
    _started(roots, checkpoints=["ckpt/*.pt"], final=None)

    assert status(roots.data, "r1").tmp == []


def test_status_lets_a_relative_pattern_reach_a_sibling_but_never_an_ancestor(roots):
    _unregistered(roots)
    (roots.data / "work").mkdir()
    (roots.data / "side").mkdir()
    (roots.data / "side" / "m.pt.tmp").write_bytes(b"m")
    (roots.data / "junk.tmp").write_bytes(b"j")  # in the parent of the cwd
    _started(roots, checkpoints=["../side/*.pt", "../not-there/*.pt"], final=None)

    # `../not-there` does not exist: its nearest directory is the cwd's parent, an ancestor
    assert status(roots.data, "r1").tmp == [TmpFile("side/m.pt.tmp", 1)]


def test_status_collects_the_patterns_of_every_attempt(roots):
    _unregistered(roots)
    _started(roots, attempt=1, checkpoints=["a/*.pt"], final=None)
    _started(roots, attempt=2, checkpoints=["b/*.pt"], final="c/best.pt")
    for d in ("a", "b", "c"):
        (roots.data / "work" / d).mkdir(parents=True)
        (roots.data / "work" / d / "m.tmp").write_bytes(d.encode())

    assert [t.path for t in status(roots.data, "r1").tmp] == [
        "work/a/m.tmp",
        "work/b/m.tmp",
        "work/c/m.tmp",
    ]


def test_status_resolves_each_attempts_globs_against_that_attempts_own_cwd(roots, tmp_path):
    """`--resume` may run from another directory; the second attempt's relative patterns point
    under the cwd its `started` event recorded, not under the cwd of the first attempt."""
    second = tmp_path / "second"
    (second / "ckpt").mkdir(parents=True)
    (second / "ckpt" / "m.pt.tmp").write_bytes(b"22")
    first = roots.data / "work" / "ckpt"
    first.mkdir(parents=True)
    (first / "m.pt.tmp").write_bytes(b"1")
    _unregistered(roots)  # the record's cwd is "work": the first attempt's
    _started(roots, attempt=1, cwd=str(roots.data / "work"), checkpoints=["ckpt/*.pt"], final=None)
    _started(roots, attempt=2, cwd=str(second), checkpoints=["ckpt/*.pt"], final=None)

    assert status(roots.data, "r1").tmp == [
        TmpFile((second / "ckpt" / "m.pt.tmp").resolve().as_posix(), 2),
        TmpFile("work/ckpt/m.pt.tmp", 1),
    ]


@pytest.mark.parametrize(
    "cwd",
    [None, "", 5, ["x"]],
    ids=["no-key", "empty", "not-a-string", "not-a-string-list"],
)
def test_status_falls_back_to_the_records_cwd_when_the_event_has_no_usable_cwd(roots, cwd):
    _unregistered(roots)
    extra = {} if cwd is None else {"cwd": cwd}
    _started(roots, checkpoints=["ckpt/*.pt"], final=None, **extra)
    ckpt = roots.data / "work" / "ckpt"
    ckpt.mkdir(parents=True)
    (ckpt / "m.pt.tmp").write_bytes(b"tt")

    assert status(roots.data, "r1").tmp == [TmpFile("work/ckpt/m.pt.tmp", 2)]


def test_status_lists_a_tmp_once_when_a_glob_directory_is_also_a_checkpoint_directory(roots):
    _, w = _seeded(roots)
    _started(roots, checkpoints=["weights/*.pt"], final="weights/best.pt")
    (w / "resume.pth.tmp").write_bytes(b"r")

    assert status(roots.data, "r1").tmp == [TmpFile("work/weights/resume.pth.tmp", 1)]


def test_status_verify_reports_a_broken_evidence_artifact(roots, tmp_path):
    ds, _ = seed_tiny(roots)
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )
    save_record(roots.data, rec)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t", encoding="utf-8")
    scope = RunScope("r1", "tiny", ds.card.samples_hash, "fixed-v1", ("train",))
    ref = attach_evidence(roots.data, scope, "teacher", f, role="teacher", attempt=1, binding="cli")
    bind_ref(roots.data, load_record(roots.data, "r1"), ref, 1)
    assert status(roots.data, "r1", verify=True).drift == []
    (artifact_dir(roots.data, "evidence", ref.artifact_id) / "teacher.jsonl").write_text(
        "x", encoding="utf-8"
    )
    assert status(roots.data, "r1", verify=True).drift == ["evidence:teacher"]
