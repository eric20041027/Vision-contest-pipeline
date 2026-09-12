import json

import pytest

from vcp.artifact import store
from vcp.artifact.ledger import read_supersession, supersession_log, supersession_of
from vcp.artifact.lineage import head, lineage, list_manifests
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file


def _commit(roots, artifact_id, **over):
    spec = ArtifactSpec.model_validate({"kind": "receipt", "id": artifact_id, **over})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("a.txt", artifact_id)
        return art.commit()


def _rewrite_log(log, rows):
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")


def _rows(log):
    return [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_supersession_is_checked_at_open_and_commit_and_indexed(roots):
    new = ArtifactSpec(kind="receipt", id="new", supersedes="old", supersedes_reason="r")
    with pytest.raises(ValidationFailed, match="^not_found: artifact receipt/old"):
        ArtifactWriter.create(new, data_root=roots.data)
    assert not store.manifest_path(roots.data, "receipt", "new").parent.exists()
    half = ArtifactWriter.create(ArtifactSpec(kind="receipt", id="old"), data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^partial: artifact receipt/old"):
        ArtifactWriter.create(new, data_root=roots.data)
    half.write_text("a.txt", "old")
    old = half.commit()
    old_sha = sha256_file(store.manifest_path(roots.data, "receipt", "old"))
    committed = _commit(roots, "new", supersedes="old", supersedes_reason="seed was 43 not 42")
    assert committed.supersedes_sha256 == old_sha and old.spec.id == "old"
    rows = read_supersession(roots.data, "receipt")
    assert len(rows) == 1 and rows[0].id == "new" and rows[0].supersedes_id == "old"
    assert rows[0].supersedes_sha256 == old_sha and rows[0].reason == "seed was 43 not 42"
    assert rows[0].manifest_sha256 == sha256_file(store.manifest_path(roots.data, "receipt", "new"))
    assert rows[0].ts.endswith("Z")
    assert store.verify(roots.data, "receipt", "new") == store.VerifyResult([], [], [], False)
    # a tampered old artifact cannot be superseded again until it verifies
    (store.manifest_path(roots.data, "receipt", "old").parent / "a.txt").write_text(
        "x", encoding="utf-8"
    )
    again = ArtifactSpec(kind="receipt", id="new2", supersedes="old", supersedes_reason="again")
    with ArtifactWriter.create(again, data_root=roots.data) as art:
        art.write_text("a.txt", "n2")
        with pytest.raises(
            IntegrityError, match="^mismatch: superseded artifact receipt/old"
        ) as ei:
            art.commit()
        assert ei.value.fields == {"kind": "receipt", "id": "new2", "supersedes": "old"}
    assert store.is_partial(roots.data, "receipt", "new2")
    assert len(read_supersession(roots.data, "receipt")) == 1


def test_lineage_head_forks_and_relink(roots):
    _commit(roots, "r1")
    _commit(roots, "r2", supersedes="r1", supersedes_reason="a")
    _commit(roots, "r3", supersedes="r2", supersedes_reason="b")
    _commit(roots, "r3b", supersedes="r2", supersedes_reason="fork")
    lin = lineage(roots.data, "receipt", "r2")
    assert [m.spec.id for m in lin.chain] == ["r1", "r2"]
    assert [m.spec.id for m in lin.successors] == ["r3", "r3b"]
    assert lin.heads == ["r3", "r3b"] and lin.forks == 1
    assert head(roots.data, "receipt", "r1") == ["r3", "r3b"]
    tip = lineage(roots.data, "receipt", "r3")
    assert [m.spec.id for m in tip.chain] == ["r1", "r2", "r3"]
    assert tip.successors == [] and tip.heads == ["r3"] and tip.forks == 1
    alone = _commit(roots, "solo")
    assert lineage(roots.data, "receipt", "solo").chain == [alone]
    assert head(roots.data, "receipt", "solo") == ["solo"]
    assert set(list_manifests(roots.data, "receipt")) == {"r1", "r2", "r3", "r3b", "solo"}
    assert list_manifests(roots.data, "nothing") == {}
    with pytest.raises(ValidationFailed, match="^not_found: "):
        lineage(roots.data, "receipt", "r9")
    # the crash window after manifest.json: drop r3's row, verify warns, relink restores it
    log = supersession_log(roots.data, "receipt")
    _rewrite_log(log, [r for r in _rows(log) if r["id"] != "r3"])
    assert supersession_of(roots.data, "receipt", "r3") is None
    assert store.verify(roots.data, "receipt", "r3").unlinked
    assert store.relink(roots.data, "receipt", "r3") is True
    assert store.relink(roots.data, "receipt", "r3") is False
    assert store.relink(roots.data, "receipt", "r1") is False  # supersedes nothing
    assert not store.verify(roots.data, "receipt", "r3").unlinked
    row = supersession_of(roots.data, "receipt", "r3")
    assert row is not None and row.reason == "b" and row.supersedes_id == "r2"
    # a ledger row that disagrees with the manifest on disk is a mismatch
    rows = _rows(log)
    for r in rows:
        if r["id"] == "r3":
            r["manifest_sha256"] = "0" * 64
    _rewrite_log(log, rows)
    assert store.verify(roots.data, "receipt", "r3").mismatch == ["manifest.json"]
    # a partial id is neither a chain member nor a valid start
    ArtifactWriter.create(ArtifactSpec(kind="receipt", id="half"), data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^partial: "):
        lineage(roots.data, "receipt", "half")
