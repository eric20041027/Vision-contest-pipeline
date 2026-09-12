import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY

import pytest

from vcp.artifact import clean as cleanmod
from vcp.artifact.clean import CleanResult, KindStatus, PartialInfo, clean, parse_age, scan
from vcp.artifact.ledger import supersession_log
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifact_dir, artifacts_root
from vcp.core.time import stamp, utc_now


def _commit(roots, kind, artifact_id, **over):
    spec = ArtifactSpec.model_validate({"kind": kind, "id": artifact_id, **over})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("a.txt", artifact_id)
        return art.commit()


def _partial(roots, kind, artifact_id, *, fail=False) -> Path:
    art = ArtifactWriter.create(ArtifactSpec(kind=kind, id=artifact_id), data_root=roots.data)
    if fail:
        with pytest.raises(RuntimeError):
            with art:
                raise RuntimeError("boom")
    return art.dir


def _age(spec_json: Path, *, days: int) -> None:
    doc = json.loads(spec_json.read_text(encoding="utf-8"))
    doc["opened_at"] = stamp(utc_now() - timedelta(days=days))
    spec_json.write_text(json.dumps(doc), encoding="utf-8", newline="\n")


def _touch_old(p: Path, *, days: int) -> None:
    t = (utc_now() - timedelta(days=days)).timestamp()
    os.utime(p, (t, t))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", timedelta(0)),
        ("24h", timedelta(hours=24)),
        ("30m", timedelta(minutes=30)),
        ("2d", timedelta(days=2)),
        (" 1h ", timedelta(hours=1)),
    ],
)
def test_parse_age(text, expected):
    assert parse_age(text) == expected


@pytest.mark.parametrize("bad", ["", "24", "1w", "h", "-1h", "1.5h", "0h0"])
def test_parse_age_rejects(bad):
    with pytest.raises(ValidationFailed, match="--older-than"):
        parse_age(bad)


def test_scan_classifies_every_directory(roots):
    assert scan(roots.data) == []
    _commit(roots, "receipt", "r1")
    _commit(roots, "receipt", "r2", supersedes="r1", supersedes_reason="x")
    _commit(roots, "receipt", "r2b", supersedes="r1", supersedes_reason="fork")
    _partial(roots, "receipt", "half")
    _partial(roots, "receipt", "crashed", fail=True)
    (artifacts_root(roots.data) / "receipt" / "stray").mkdir()
    (artifacts_root(roots.data) / "receipt" / "loose.txt").write_text("x", encoding="utf-8")
    _commit(roots, "snapshot", "s1")
    supersession_log(roots.data, "receipt").unlink()
    receipt, snapshot = scan(roots.data)
    assert receipt.kind == "receipt" and receipt.complete == ["r1", "r2", "r2b"]
    assert receipt.partial == [
        PartialInfo("crashed", ANY, "RuntimeError"),
        PartialInfo("half", ANY, None),
    ]
    assert all(p.opened_at.endswith("Z") for p in receipt.partial)
    assert receipt.unlinked == ["r2", "r2b"] and receipt.forks == 1
    assert receipt.foreign == ["stray"]
    assert snapshot == KindStatus("snapshot", ["s1"], [], [], 0, [])
    assert scan(roots.data, "snapshot") == [snapshot] and scan(roots.data, "nothing") == []
    with pytest.raises(ValidationFailed):
        scan(roots.data, "../x")


def test_clean_lists_then_removes_only_old_partials_and_temps(roots):
    _commit(roots, "receipt", "done")
    old = _partial(roots, "receipt", "old", fail=True)
    fresh = _partial(roots, "receipt", "fresh")
    stray = artifacts_root(roots.data) / "receipt" / "stray"
    stray.mkdir()
    (stray / "x.bin").write_bytes(b"x")
    done_tmp = artifact_dir(roots.data, "receipt", "done") / ".a.txt.0a1b2c3d.tmp"
    kind_tmp = artifacts_root(roots.data) / "receipt" / ".supersession.jsonl.deadbeef.tmp"
    fresh_tmp = fresh / ".b.txt.01234567.tmp"
    for p in (done_tmp, kind_tmp, fresh_tmp):
        p.write_bytes(b"t")
    _age(old / "spec.json", days=2)
    for p in (done_tmp, kind_tmp):
        _touch_old(p, days=2)
    expected = [
        "receipt/old",
        "receipt/.supersession.jsonl.deadbeef.tmp",
        "receipt/done/.a.txt.0a1b2c3d.tmp",
    ]
    assert clean(roots.data) == CleanResult(expected, [])
    assert old.is_dir() and done_tmp.exists() and kind_tmp.exists()
    assert clean(roots.data, kind="snapshot") == CleanResult([], [])
    res = clean(roots.data, kind="receipt", apply=True)
    assert res == CleanResult(expected, expected)
    assert not old.exists() and not done_tmp.exists() and not kind_tmp.exists()
    assert fresh.is_dir() and fresh_tmp.exists() and stray.is_dir()
    assert (artifact_dir(roots.data, "receipt", "done") / "manifest.json").is_file()
    assert (artifact_dir(roots.data, "receipt", "done") / "a.txt").read_text(
        encoding="utf-8"
    ) == "done"
    assert clean(roots.data, apply=True) == CleanResult([], [])
    # a temp inside a candidate directory is not listed on its own
    assert clean(roots.data, older_than=timedelta(0)).candidates == ["receipt/fresh"]


def test_clean_never_touches_what_it_cannot_read_or_what_committed_meanwhile(roots, monkeypatch):
    broken = _partial(roots, "receipt", "broken")
    (broken / "spec.json").write_text("{", encoding="utf-8")
    _commit(roots, "receipt", "done")
    assert clean(roots.data, older_than=timedelta(0), apply=True) == CleanResult([], [])
    assert broken.is_dir()
    late = _partial(roots, "receipt", "late")
    _age(late / "spec.json", days=2)
    real_info = cleanmod._partial_info

    def commit_meanwhile(d):
        info = real_info(d)
        (d / "manifest.json").write_text("{}", encoding="utf-8")  # a job commits after listing
        return info

    monkeypatch.setattr(cleanmod, "_partial_info", commit_meanwhile)
    res = clean(roots.data, apply=True)
    assert res.candidates == ["receipt/late"] and res.removed == []
    assert (late / "manifest.json").is_file()
