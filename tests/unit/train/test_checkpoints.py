import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import store_path
from vcp.train.checkpoints import (
    FINAL_AMBIGUOUS,
    drift,
    expand,
    mark_final,
    missing,
    register,
    resolve_final,
)
from vcp.train.schema import TrainRecord


def _record() -> TrainRecord:
    return TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python", "train.py"],
    )


def _weights(root):
    (root / "weights").mkdir(parents=True)
    (root / "weights" / "best.pt").write_bytes(b"best")
    (root / "weights" / "last.pt").write_bytes(b"last")
    (root / "weights" / "notes.txt").write_text("x", encoding="utf-8")
    (root / "weights" / "sub").mkdir()
    (root / "weights" / "sub" / "epoch1.pt").write_bytes(b"e1")
    return root / "weights"


def test_expand_globs_relative_to_cwd(tmp_path):
    w = _weights(tmp_path)
    assert expand(["weights/*.pt"], tmp_path) == [w / "best.pt", w / "last.pt"]
    assert expand(["weights/**/*.pt"], tmp_path) == [
        w / "best.pt",
        w / "last.pt",
        w / "sub" / "epoch1.pt",
    ]
    assert expand(["weights/*.pt", "weights/best.pt", str(w / "last.pt")], tmp_path) == [
        w / "best.pt",
        w / "last.pt",
    ]
    assert expand(["weights"], tmp_path) == []  # directories are not checkpoints
    assert expand(["nothing/*.pt"], tmp_path) == []


def test_register_dedups_by_path_and_sha(roots, tmp_path):
    w = _weights(roots.data / "work")
    rec, added = register(
        _record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1
    )
    assert [c.path for c in added] == ["work/weights/best.pt", "work/weights/last.pt"]
    assert added[0].sha256 == sha256_file(w / "best.pt") and added[0].bytes == 4
    assert added[0].attempt == 1 and added[0].source == "glob" and not added[0].final
    again, added2 = register(rec, [w / "best.pt"], data_root=roots.data, attempt=2)
    assert added2 == [] and again == rec
    (w / "best.pt").write_bytes(b"best-v2")
    third, added3 = register(
        again, [w / "best.pt"], data_root=roots.data, attempt=2, source="session"
    )
    assert len(added3) == 1 and added3[0].attempt == 2 and added3[0].source == "session"
    assert [c.path for c in third.checkpoints] == [
        "work/weights/best.pt",
        "work/weights/last.pt",
        "work/weights/best.pt",
    ]
    outside = tmp_path / "elsewhere.pt"
    outside.write_bytes(b"o")
    fourth, added4 = register(third, [outside], data_root=roots.data, attempt=2)
    assert added4[0].path == store_path(outside, roots.data) and added4[0].path.endswith(
        "elsewhere.pt"
    )


def test_resolve_final(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec, final = resolve_final(
        rec, "weights/best.pt", cwd=roots.data / "work", data_root=roots.data
    )
    assert final is not None and final.path == "work/weights/best.pt"
    assert [c.final for c in rec.checkpoints] == [True, False]
    with pytest.raises(ValidationFailed, match=FINAL_AMBIGUOUS) as ei:
        resolve_final(rec, "weights/*.pt", cwd=roots.data / "work", data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "weights/*.pt"}
    with pytest.raises(ValidationFailed, match="matched 0"):
        resolve_final(rec, "weights/none.pt", cwd=roots.data / "work", data_root=roots.data)
    # no pattern: a session-marked final wins, else None
    rec2, _ = register(_record(), [w / "last.pt"], data_root=roots.data, attempt=1)
    assert resolve_final(rec2, None, cwd=roots.data / "work", data_root=roots.data)[1] is None
    rec2 = mark_final(rec2, "work/weights/last.pt", sha256_file(w / "last.pt"))
    assert (
        resolve_final(rec2, None, cwd=roots.data / "work", data_root=roots.data)[1].path
        == "work/weights/last.pt"
    )


def test_mark_final_is_exclusive(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/weights/last.pt", sha256_file(w / "last.pt"))
    assert [c.final for c in rec.checkpoints] == [False, True]
    rec = mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt"))
    assert [c.final for c in rec.checkpoints] == [True, False]


def test_missing_and_drift(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    assert missing(rec, roots.data) == [] and drift(rec, roots.data) == []
    (w / "best.pt").write_bytes(b"tampered")
    (w / "last.pt").unlink()
    assert missing(rec, roots.data) == ["work/weights/last.pt"]
    assert drift(rec, roots.data) == [
        "work/weights/best.pt",
        "work/weights/last.pt",
    ]
