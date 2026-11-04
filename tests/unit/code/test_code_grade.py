"""``vcp.code.grade`` (spec 2026-10-31 §5.4, §10.4): the code grade ``traced > snapshot > commit >
declared``, computed on read from ``train.yaml``, ``env.<n>.json``, the event log and the artifacts
the run's refs point at. The records are written by hand; snapshots and traces are the real
artifacts."""

import json
import shutil
import types
from pathlib import Path

import pytest

import vcp.data.evidence as evidence_module
from backup_fixtures import make_fusion, make_world
from compat_v0170 import ENV_JSON_0100, ENV_JSON_0170
from vcp.code import trace
from vcp.code.grade import (
    AttemptCode,
    CodeInfo,
    RootCode,
    code_info,
    lowest,
    notes_by_attempt,
)
from vcp.code.scan import scan_root
from vcp.code.schema import CodeRoot, CodeSection
from vcp.code.snapshot import write_snapshot
from vcp.core.paths import artifact_dir, store_path
from vcp.data.evidence import IntactMemo, artifact_ref
from vcp.data.evidence_ref import CODE_ROLE, EvidenceRef
from vcp.measure.runs import load_run, run_dir, save_run
from vcp.measure.schema import RunCard, RunSource
from vcp.train.records import append_event, events_path, load_record, save_record, train_dir
from vcp.train.schema import Attempt, TrainRecord
from vcp.train.status import status

C = "6f867fe" + "0" * 33
S, D = "8" * 64, "9" * 64
STAMP = "2026-11-03T00:00:00.000Z"
CMD = "test.grade"


# -- the record of a run, by hand ------------------------------------------------------------------


def _src(tmp_path: Path, name: str = "src") -> Path:
    src = tmp_path / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.py").write_bytes(b"A = 1\n")
    (src / "b.py").write_bytes(b"B = 2\n")
    return src


def _snapshot(roots, src: Path) -> str:
    scanned = scan_root(src, kind="dir", keep="capped")
    outcome = write_snapshot(roots.data, scanned, command=CMD)
    assert outcome.state in ("created", "reused"), outcome
    return outcome.snapshot_id


def _ref(roots, sid: str, label: str = "cwd", attempt: int = 1) -> EvidenceRef:
    return artifact_ref(
        roots.data,
        "code_snapshot",
        sid,
        name=f"code.{label}",
        role=CODE_ROLE,
        attempt=attempt,
        binding="cli",
    )


def _code_root(roots, src: Path, snapshot: str | None, label: str = "cwd", reason=None) -> CodeRoot:
    return CodeRoot(
        label=label,
        source="cwd",
        kind="dir",
        path=store_path(src, roots.data),
        commit=None,
        snapshot=snapshot,
        reason=reason,
        files=2,
        untracked_code=0,
        untracked_code_paths=[],
        excluded=0,
        excluded_paths=[],
    )


def _write_env(roots, run_id: str, n: int, *, git, code: list[CodeRoot] | None) -> None:
    """``train/env.<n>.json`` with just the sections the grade reads."""
    doc: dict = {"git": git}
    if code is not None:
        doc["code"] = CodeSection(rules=1, excluded_dirs=[], roots=code).model_dump(mode="json")
    directory = train_dir(roots.data, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"env.{n}.json").write_bytes(json.dumps(doc).encode("utf-8"))


def _attempt(n: int) -> Attempt:
    return Attempt(
        n=n, started_at=STAMP, console=f"train/console.{n}.log", env=f"train/env.{n}.json"
    )


def _record(run_id: str, attempts: int, evidence: list[EvidenceRef]) -> TrainRecord:
    return TrainRecord(
        run_id=run_id,
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
        attempts=[_attempt(n) for n in range(1, attempts + 1)],
        evidence=evidence,
    )


CLEAN = {"commit": C, "dirty": False}


class Built:
    """A hand-written training run ``r`` over a real snapshot of ``src``."""

    def __init__(self, roots, src: Path, sid: str, run_id: str):
        self.roots, self.src, self.sid, self.run_id = roots, src, sid, run_id

    @property
    def grade(self) -> str:
        return code_info(self.roots.data, self.run_id).grade

    def info(self) -> CodeInfo:
        return code_info(self.roots.data, self.run_id)

    def note(self, key: str, attempt: int = 1) -> None:
        append_event(self.roots.data, self.run_id, "note", attempt, key=key, value=1)

    def trace(self, attempt: int = 1, *, outside: Path | None = None) -> str:
        """A real ``code_trace`` artifact of ``attempt``: ``a.py`` imported, plus a module from
        ``outside`` when given (that is a gap)."""
        modules = {"__main__": _module("__main__", self.src / "a.py")}
        if outside is not None:
            modules["stray"] = _module("stray", outside)
        return trace.write_trace(self.run_id, self.roots.data, attempt, modules=modules)

    def record(self) -> TrainRecord:
        return load_record(self.roots.data, self.run_id)


def _module(name: str, file: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(file)
    return module


def _build(
    roots,
    tmp_path,
    *,
    git=CLEAN,
    attempts: int = 1,
    snapshot: bool = True,
    run_id: str = "r",
) -> Built:
    """Run ``run_id`` with ``attempts`` attempts, each over the same snapshot of one ``cwd`` root
    (the ref is bound once, on attempt 1, as ``train run`` does for an unchanged resume)."""
    src = _src(tmp_path)
    sid = _snapshot(roots, src)
    evidence = [_ref(roots, sid)] if snapshot else []
    save_record(roots.data, _record(run_id, attempts, evidence))
    for n in range(1, attempts + 1):
        _write_env(
            roots, run_id, n, git=git, code=[_code_root(roots, src, sid if snapshot else None)]
        )
    return Built(roots, src, sid, run_id)


def _gap(tmp_path: Path) -> Path:
    stray = tmp_path / "elsewhere" / "stray.py"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"S = 1\n")
    return stray


# -- the pieces ------------------------------------------------------------------------------------


def test_lowest_ranks_the_grades_and_an_empty_set_is_declared():
    assert lowest(["traced", "snapshot", "traced"]) == "snapshot"
    assert lowest(iter(["commit", "snapshot"])) == "commit"
    assert lowest(["traced"]) == "traced"
    assert lowest([]) == "declared"


def test_to_json_is_the_documented_shape():
    info = CodeInfo(
        "snapshot",
        [
            AttemptCode(1, "snapshot", [RootCode("cwd", "code-0123456789abcdef", None)]),
            AttemptCode(2, "declared", [RootCode("root-1", None, "too_large")]),
        ],
    )
    assert info.to_json() == {
        "grade": "snapshot",
        "attempts": [
            {
                "n": 1,
                "grade": "snapshot",
                "roots": [{"label": "cwd", "snapshot": "code-0123456789abcdef", "reason": None}],
            },
            {
                "n": 2,
                "grade": "declared",
                "roots": [{"label": "root-1", "snapshot": None, "reason": "too_large"}],
            },
        ],
    }


def test_notes_by_attempt_reads_keys_and_skips_what_it_cannot_read(roots):
    path = events_path(roots.data, "r")
    path.parent.mkdir(parents=True)
    good = [
        {"event": "note", "attempt": 1, "key": "code_changed", "value": 3},
        {"event": "note", "attempt": 1, "key": "git_changed", "value": "x"},
        {"event": "note", "attempt": 2, "key": "code_changed", "value": 1},
        {"event": "checkpoint", "attempt": 1, "key": "code_changed"},  # not a note
    ]
    rows = [
        json.dumps(good[0]),
        "{not json",  # a torn line
        "",
        "[1, 2]",  # not an object
        json.dumps({"event": "note", "attempt": "1", "key": "x"}),  # attempt of the wrong type
        json.dumps({"event": "note", "attempt": True, "key": "x"}),
        json.dumps({"event": "note", "attempt": 1, "key": 7}),  # key of the wrong type
        json.dumps({"event": "note", "attempt": 1}),  # no key
        json.dumps(good[1]),
        json.dumps(good[2]),
        json.dumps(good[3]),
        '{"event": "note", "attempt": 3, "key": "code_ch',  # cut short
    ]
    path.write_bytes(("\n".join(rows) + "\n").encode("utf-8") + b"\xff\xfe\n")
    assert notes_by_attempt(roots.data, "r") == {
        1: {"code_changed", "git_changed"},
        2: {"code_changed"},
    }
    assert notes_by_attempt(roots.data, "no-such-run") == {}


# -- one attempt -----------------------------------------------------------------------------------


def test_traced_needs_complete_snapshots_and_complete_intact_traces(roots, tmp_path):
    b = _build(roots, tmp_path)
    assert b.grade == "snapshot"  # a snapshot, no trace yet
    b.trace()
    info = b.info()
    assert info.grade == "traced"
    assert [(a.n, a.grade) for a in info.attempts] == [(1, "traced")]
    assert info.attempts[0].roots == [RootCode("cwd", b.sid, None)]

    # a second trace with a gap: not every trace of the attempt is complete
    gap = b.trace(outside=_gap(tmp_path))
    assert b.grade == "snapshot"
    shutil.rmtree(artifact_dir(roots.data, "code_trace", gap))  # ...and now it is not even there
    assert b.grade == "snapshot"


def test_a_trace_that_is_not_intact_does_not_count(roots, tmp_path):
    b = _build(roots, tmp_path)
    trace_id = b.trace()
    assert b.grade == "traced"
    doc = artifact_dir(roots.data, "code_trace", trace_id) / "trace.json"
    doc.write_bytes(doc.read_bytes().replace(b'"complete": true', b'"complete": true '))
    assert b.grade == "snapshot"


def test_a_trace_of_another_attempt_does_not_count(roots, tmp_path):
    b = _build(roots, tmp_path, attempts=2)
    b.trace(attempt=2)
    info = b.info()
    assert [(a.n, a.grade) for a in info.attempts] == [(1, "snapshot"), (2, "traced")]
    assert info.grade == "snapshot"  # the lowest attempt


def test_code_changed_does_not_lower_traced(roots, tmp_path):
    b = _build(roots, tmp_path)
    b.trace()
    b.note("code_changed")
    assert b.grade == "traced"


def test_snapshot_needs_complete_snapshots_and_no_code_changed(roots, tmp_path):
    b = _build(roots, tmp_path)
    assert b.grade == "snapshot"
    b.note("code_changed", attempt=2)  # another attempt's note is not this attempt's
    assert b.grade == "snapshot"
    b.note("code_changed")
    assert b.grade == "commit"  # the git rule: a clean tree at a commit


def test_code_changed_drops_snapshot_to_the_git_grade(roots, tmp_path):
    dirty = {"commit": C, "dirty": True, "modified": 1, "status_sha256": S}
    # modified tracked files and no patch of them: the git rule does not hold
    b = _build(roots, tmp_path, git=dirty)
    assert b.grade == "snapshot"
    b.note("code_changed")
    assert b.grade == "declared"


@pytest.mark.parametrize(
    "snapshot_of_the_roots",
    ["no_roots", "root_without_snapshot", "no_ref", "foreign_role_ref", "one_of_two_missing"],
)
def test_an_incomplete_snapshot_is_never_snapshot_or_traced(roots, tmp_path, snapshot_of_the_roots):
    src = _src(tmp_path)
    sid = _snapshot(roots, src)
    other = _src(tmp_path, "other")
    (other / "c.py").write_bytes(b"C = 3\n")
    other_sid = _snapshot(roots, other)
    ref = _ref(roots, sid)
    code = {
        "no_roots": [],  # open point 12: an empty set is never complete
        "root_without_snapshot": [_code_root(roots, src, None, reason="too_large")],
        "no_ref": [_code_root(roots, src, sid)],
        "foreign_role_ref": [_code_root(roots, src, sid)],
        "one_of_two_missing": [
            _code_root(roots, src, sid),
            _code_root(roots, other, other_sid, label="root-1"),  # a snapshot, but no ref
        ],
    }[snapshot_of_the_roots]
    evidence = {
        "no_roots": [ref],
        "root_without_snapshot": [],
        "no_ref": [],
        "foreign_role_ref": [ref.model_copy(update={"role": "labels"})],  # by kind and role
        "one_of_two_missing": [ref],
    }[snapshot_of_the_roots]
    save_record(roots.data, _record("r", 1, evidence))
    _write_env(roots, "r", 1, git=CLEAN, code=code)
    info = code_info(roots.data, "r")
    assert info.grade == "commit"
    assert [r.label for r in info.attempts[0].roots] == [c.label for c in code]


def test_the_ref_must_point_at_the_id_the_env_names(roots, tmp_path):
    src = _src(tmp_path)
    sid = _snapshot(roots, src)
    other = _src(tmp_path, "other")
    (other / "c.py").write_bytes(b"C = 3\n")
    other_sid = _snapshot(roots, other)
    save_record(roots.data, _record("r", 1, [_ref(roots, other_sid)]))
    _write_env(roots, "r", 1, git=CLEAN, code=[_code_root(roots, src, sid)])
    assert code_info(roots.data, "r").grade == "commit"


COMMIT_CASES = [
    (
        {"commit": C, "dirty": True, "modified": 0, "untracked": 2, "status_sha256": S},
        set(),
        "commit",
    ),
    (
        {"commit": C, "dirty": True, "modified": 1, "status_sha256": S, "diff_sha256": D},
        set(),
        "commit",
    ),
    ({"commit": C, "dirty": True, "modified": 1, "status_sha256": S}, set(), "declared"),
    ({"commit": C, "dirty": False}, set(), "commit"),
    ({"commit": C, "dirty": True}, set(), "declared"),
    ({"commit": C, "dirty": False}, {"git_changed"}, "declared"),
    ({"commit": C, "dirty": False}, {"some_other_note"}, "commit"),
    (None, set(), "declared"),
]


@pytest.mark.parametrize(("git", "notes", "grade"), COMMIT_CASES)
def test_commit_rules(roots, tmp_path, git, notes, grade):
    save_record(roots.data, _record("r", 1, []))
    _write_env(roots, "r", 1, git=git, code=None)  # an env.json from before the code section
    for key in notes:
        append_event(roots.data, "r", "note", 1, key=key, value="x")
    assert code_info(roots.data, "r").grade == grade


def test_a_0170_env_json_grades_commit(roots):
    save_record(roots.data, _record("r", 1, []))
    directory = train_dir(roots.data, "r")
    directory.mkdir(parents=True)
    (directory / "env.1.json").write_bytes(ENV_JSON_0170.encode("utf-8"))  # no code section
    info = code_info(roots.data, "r")
    assert info.grade == "commit" and info.attempts == [AttemptCode(1, "commit", [])]


def test_a_pre_0110_env_json_grades_commit_when_clean(roots):
    save_record(roots.data, _record("r", 1, []))
    directory = train_dir(roots.data, "r")
    directory.mkdir(parents=True)
    (directory / "env.1.json").write_bytes(ENV_JSON_0100.encode("utf-8"))  # commit, dirty: false
    assert code_info(roots.data, "r").grade == "commit"
    (directory / "env.1.json").write_bytes(
        ENV_JSON_0100.replace('"dirty": false', '"dirty": true').encode("utf-8")
    )
    assert code_info(roots.data, "r").grade == "declared"


def test_a_deleted_snapshot_drops_the_grade(roots, tmp_path):
    b = _build(roots, tmp_path)
    assert b.grade == "snapshot"
    shutil.rmtree(artifact_dir(roots.data, "code_snapshot", b.sid))
    assert b.grade == "commit"


def test_a_snapshot_with_a_changed_file_drops_the_grade(roots, tmp_path):
    b = _build(roots, tmp_path)
    kept = artifact_dir(roots.data, "code_snapshot", b.sid) / "files" / "a.py"
    kept.write_bytes(b"A = 99\n")
    assert b.grade == "commit"


def test_an_unreadable_env_is_declared(roots, tmp_path):
    b = _build(roots, tmp_path)
    env = train_dir(roots.data, "r") / "env.1.json"
    env.write_bytes(b'{"git": {"commit": ')  # cut short
    assert b.info().attempts == [AttemptCode(1, "declared", [])]
    env.write_bytes(b"[]")
    assert b.grade == "declared"
    env.unlink()
    assert b.grade == "declared"
    record = b.record()
    save_record(
        roots.data,
        record.model_copy(
            update={"attempts": [Attempt(**{**_attempt(1).model_dump(), "env": None})]}
        ),
    )
    assert b.grade == "declared"  # an attempt that never got as far as an env


def test_a_broken_code_section_leaves_the_git_section_alone(roots, tmp_path):
    save_record(roots.data, _record("r", 1, []))
    directory = train_dir(roots.data, "r")
    directory.mkdir(parents=True)
    (directory / "env.1.json").write_bytes(
        json.dumps({"git": CLEAN, "code": {"rules": "one"}}).encode("utf-8")
    )
    assert code_info(roots.data, "r").grade == "commit"


# -- a whole run -----------------------------------------------------------------------------------


def test_the_run_takes_its_lowest_attempt(roots, tmp_path):
    b = _build(roots, tmp_path, attempts=2)
    b.trace(attempt=1)
    b.trace(attempt=2)
    assert b.grade == "traced"
    b.note("code_changed", attempt=2)
    assert b.grade == "traced"  # a trace outranks code_changed
    # attempt 2 resumed with its code gone and a dirty tree: it falls to the git rule, then declared
    dirty = {"commit": C, "dirty": True, "modified": 2, "status_sha256": S}
    _write_env(roots, "r", 2, git=dirty, code=[])
    info = b.info()
    assert [(a.n, a.grade) for a in info.attempts] == [(1, "traced"), (2, "declared")]
    assert info.grade == "declared"


def test_a_run_without_attempts_is_declared(roots):
    save_record(roots.data, _record("r", 0, []))
    assert code_info(roots.data, "r") == CodeInfo("declared", [])


def test_code_info_never_raises(roots, tmp_path, monkeypatch):
    assert code_info(roots.data, "nothing-here") == CodeInfo("declared", [])
    assert code_info(roots.data, "../escape") == CodeInfo("declared", [])
    b = _build(roots, tmp_path)
    assert b.grade == "snapshot"

    def broken(*_args, **_kwargs):
        raise OSError("disk went away")

    with monkeypatch.context() as patched:  # undo this patch only, never the conftest's isolation
        patched.setattr(evidence_module, "ref_intact", broken)
        assert b.info().grade == "declared"  # unreadable: declared, not an exception
    assert b.grade == "snapshot"
    train_yaml = run_dir(roots.data, "r") / "train.yaml"
    train_yaml.write_bytes(b"run_id: [unclosed\n")
    assert b.info() == CodeInfo("declared", [])
    train_yaml.write_bytes(b"- not\n- a mapping\n")
    assert b.info() == CodeInfo("declared", [])


# -- a run without train.yaml, and a fusion --------------------------------------------------------


def _ingest_card(run_id: str, evidence: list[EvidenceRef]) -> RunCard:
    return RunCard(
        run_id=run_id,
        dataset="tiny",
        samples_hash="aa" * 32,
        plan_id="fixed-v1",
        trained_on=["train"],
        source=RunSource(framework="ext"),
        created_at=STAMP,
        evidence=evidence,
    )


def test_an_ingest_run_with_intact_code_refs_is_snapshot(roots, tmp_path):
    src = _src(tmp_path)
    sid = _snapshot(roots, src)
    save_run(roots.data, _ingest_card("ext", []))
    assert code_info(roots.data, "ext") == CodeInfo("declared", [])  # no code ref
    ref = _ref(roots, sid).model_copy(update={"binding": "manual"})  # as `eval ingest --code`
    save_run(roots.data, _ingest_card("ext", [ref]))
    assert code_info(roots.data, "ext") == CodeInfo("snapshot", [])
    other = _src(tmp_path, "other")
    (other / "c.py").write_bytes(b"C = 3\n")
    second = _ref(roots, _snapshot(roots, other), label="extra").model_copy(
        update={"binding": "manual"}
    )
    save_run(roots.data, _ingest_card("ext", [ref, second]))
    assert code_info(roots.data, "ext").grade == "snapshot"
    wrong_role = ref.model_copy(update={"role": "labels"})  # picked by kind and role, not by name
    save_run(roots.data, _ingest_card("ext", [wrong_role]))
    assert code_info(roots.data, "ext").grade == "declared"
    save_run(roots.data, _ingest_card("ext", [ref, second]))
    shutil.rmtree(artifact_dir(roots.data, "code_snapshot", sid))  # one of them is gone
    assert code_info(roots.data, "ext").grade == "declared"


def _counting(monkeypatch) -> list[tuple[str, str]]:
    """Every artifact ``ref_intact`` is really asked about from now on, as (kind, id)."""
    asked: list[tuple[str, str]] = []
    real = evidence_module.ref_intact

    def counted(data_root, ref):
        asked.append((ref.kind, ref.artifact_id))
        return real(data_root, ref)

    monkeypatch.setattr(evidence_module, "ref_intact", counted)
    return asked


def test_a_fused_run_takes_its_lowest_member(roots, tmp_path, monkeypatch):
    world = make_world(roots, tmp_path)  # runs good and bad, with eval predictions
    fused = make_fusion(world)  # run fx over good + bad
    assert code_info(roots.data, fused) == CodeInfo("declared", [])  # neither member has code

    src = _src(tmp_path)
    sid = _snapshot(roots, src)
    ref = _ref(roots, sid)
    good = load_record(roots.data, "good")
    save_record(roots.data, good.model_copy(update={"attempts": [_attempt(1)], "evidence": [ref]}))
    _write_env(roots, "good", 1, git=CLEAN, code=[_code_root(roots, src, sid)])
    assert code_info(roots.data, "good").grade == "snapshot"
    assert code_info(roots.data, "bad").grade == "declared"
    assert code_info(roots.data, fused) == CodeInfo("declared", [])  # bad is the weak member

    bad = load_run(roots.data, "bad")
    save_run(
        roots.data,
        bad.model_copy(update={"evidence": [ref.model_copy(update={"binding": "manual"})]}),
    )
    assert code_info(roots.data, "bad").grade == "snapshot"
    assert code_info(roots.data, fused) == CodeInfo("snapshot", [])
    b = Built(roots, src, sid, "good")
    b.trace()
    assert code_info(roots.data, "good").grade == "traced"
    assert code_info(roots.data, fused).grade == "snapshot"  # still the weakest member
    asked = _counting(monkeypatch)  # the members of one fusion share one memo
    assert code_info(roots.data, fused).grade == "snapshot"
    assert len(asked) == len(set(asked)) and ("code_snapshot", sid) in asked


# -- verifying once --------------------------------------------------------------------------------


def test_one_memo_across_runs_verifies_a_shared_snapshot_once(roots, tmp_path, monkeypatch):
    first = _build(roots, tmp_path, run_id="r1")
    second = _build(roots, tmp_path, run_id="r2", attempts=2)  # the same code: the same snapshot
    assert first.sid == second.sid
    asked = _counting(monkeypatch)
    memo = IntactMemo()
    infos = [code_info(roots.data, run_id, intact=memo) for run_id in ("r1", "r2")]
    assert [i.grade for i in infos] == ["snapshot", "snapshot"]
    assert asked == [("code_snapshot", first.sid)]  # two runs, three attempts: hashed once

    asked.clear()  # nothing shared, nothing remembered: each call verifies for itself
    alone = [code_info(roots.data, run_id) for run_id in ("r1", "r2")]
    assert alone == infos and asked == [("code_snapshot", first.sid)] * 2


def test_a_shared_memo_still_sees_a_snapshot_that_is_not_what_the_run_pinned(roots, tmp_path):
    good = _build(roots, tmp_path, run_id="r1")
    stale = _build(roots, tmp_path, run_id="r2")
    other = _ref(roots, good.sid).model_copy(update={"manifest_sha256": "0" * 64})
    save_record(roots.data, stale.record().model_copy(update={"evidence": [other]}))
    memo = IntactMemo()
    # the pinned manifest is part of the key: r1's verdict is not lent to r2
    assert [code_info(roots.data, r, intact=memo).grade for r in ("r1", "r2")] == [
        "snapshot",
        "commit",
    ]


def test_status_verify_hashes_each_pinned_artifact_once(roots, tmp_path, monkeypatch):
    b = _build(roots, tmp_path)
    b.trace()
    asked = _counting(monkeypatch)
    verified = status(roots.data, "r", verify=True)
    assert verified.drift == [] and verified.code.grade == "traced"
    assert len(asked) == len(set(asked)) and {kind for kind, _ in asked} == {
        "code_snapshot",
        "code_trace",
    }
    asked.clear()
    assert status(roots.data, "r").code.grade == "traced"  # without --verify as well
    assert len(asked) == len(set(asked)) == 2

    kept = artifact_dir(roots.data, "code_snapshot", b.sid) / "files" / "a.py"
    kept.write_bytes(b"A = 99\n")  # the results are what they were without the memo
    broken = status(roots.data, "r", verify=True)
    assert broken.drift == ["evidence:code.cwd"] and broken.code.grade == "commit"
