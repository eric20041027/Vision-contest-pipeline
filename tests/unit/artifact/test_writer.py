import os
from datetime import timedelta

import pytest

from vcp.artifact import store
from vcp.artifact.clean import clean
from vcp.artifact.schema import ArtifactSpec, FailureRecord, InputRef, SpecRecord
from vcp.artifact.writer import ArtifactWriter
from vcp.core import atomic
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

SECRET = "fakesecretfakesecretfakesecret1234"


def _spec(**over) -> ArtifactSpec:
    return ArtifactSpec.model_validate({"kind": "receipt", "id": "r1", **over})


def test_open_claims_the_id_and_writes_spec_json(roots):
    with ArtifactWriter.create(_spec(seed=7), data_root=roots.data) as art:
        d = artifact_dir(roots.data, "receipt", "r1")
        assert art.dir == d and d.is_dir() and art.manifest is None
        rec = SpecRecord.model_validate_json((d / "spec.json").read_text(encoding="utf-8"))
        assert rec.spec.seed == 7 and rec.opened_at.endswith("Z") and rec.vcp_version
        assert not (d / "manifest.json").exists()
        with pytest.raises(ValidationFailed, match="^exists: artifact receipt/r1") as ei:
            ArtifactWriter.create(_spec(), data_root=roots.data)
        assert ei.value.fields == {"kind": "receipt", "id": "r1"}
    # left without commit: a partial, no failure.json, and the id stays taken
    assert store.is_partial(roots.data, "receipt", "r1")
    assert not (d / "failure.json").exists()
    with pytest.raises(ValidationFailed, match="^exists: "):
        ArtifactWriter.create(_spec(), data_root=roots.data)


def test_open_rejects_bad_inputs_before_claiming(roots, tmp_path):
    bad = _spec(inputs=[InputRef(name="plan", path=str(tmp_path / "gone"))])
    with pytest.raises(ValidationFailed, match="^not_found: input 'plan'"):
        ArtifactWriter.create(bad, data_root=roots.data)
    assert not artifact_dir(roots.data, "receipt", "r1").exists()
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"{}")
    wrong = _spec(inputs=[InputRef(name="plan", path=str(plan), sha256="a" * 64)])
    with pytest.raises(IntegrityError, match="^mismatch: input 'plan'"):
        ArtifactWriter.create(wrong, data_root=roots.data)
    assert not artifact_dir(roots.data, "receipt", "r1").exists()


def test_write_add_reserve_and_commit(roots, tmp_path):
    src = tmp_path / "weights.pt"
    src.write_bytes(b"w" * 3000)
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan")
    spec = _spec(inputs=[InputRef(name="plan", path=str(plan))], params={"fold": "1"})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        e1 = art.write_json("receipt.json", {"ok": True})
        art.write_text("notes/readme.txt", "hi\n")
        art.write_bytes("raw.bin", b"\x00\x01")
        e2 = art.add_file("weights.pt", src)
        out = art.reserve("features.npy")
        assert out == art.dir / "features.npy" and not out.exists()
        out.write_bytes(b"f" * 10)
        manifest = art.commit()
    d = art.dir
    assert (d / "receipt.json").read_text(encoding="utf-8") == '{\n "ok": true\n}\n'
    assert e1.bytes == 16 and e1.sha256 == sha256_file(d / "receipt.json")
    assert e2.bytes == 3000 and e2.sha256 == sha256_file(src)
    assert (d / "weights.pt").read_bytes() == b"w" * 3000
    assert [f.name for f in manifest.files] == [
        "features.npy",
        "notes/readme.txt",
        "raw.bin",
        "receipt.json",
        "weights.pt",
    ]
    features = manifest.file("features.npy")
    assert features is not None and features.bytes == 10 and features.sha256 == sha256_file(out)
    assert manifest.spec.inputs[0].path == "plan.json"
    assert manifest.spec.inputs[0].sha256 == sha256_file(plan)
    assert manifest.spec.params == {"fold": "1"} and manifest.created_at.endswith("Z")
    assert manifest.supersedes_sha256 is None and manifest.schema_version == 1
    assert store.load_manifest(roots.data, "receipt", "r1") == manifest
    assert art.manifest == manifest
    assert not [p for p in d.rglob("*") if atomic.is_tmp_name(p.name)]
    assert (d / "manifest.json").read_bytes().count(b"\r") == 0


def test_file_name_rules_and_double_names(roots, tmp_path):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        with pytest.raises(ValidationFailed, match="^exists: 'a.txt'") as ei:
            art.write_text("a.txt", "b")
        assert ei.value.fields == {"kind": "receipt", "id": "r1", "file": "a.txt"}
        art.reserve("big.npy")
        with pytest.raises(ValidationFailed, match="^exists: 'big.npy'"):
            art.write_text("big.npy", "x")
        with pytest.raises(ValidationFailed, match="^exists: 'big.npy'"):
            art.reserve("big.npy")
        for bad, prefix in (
            ("../x", "unsafe_path"),
            ("/abs", "unsafe_path"),
            ("manifest.json", "reserved_name"),
            ("spec.json", "reserved_name"),
            ("failure.json", "reserved_name"),
            (".x.0a1b2c3d.tmp", "reserved_name"),
        ):
            with pytest.raises(ValidationFailed, match=f"^{prefix}: ") as ei:
                art.write_text(bad, "x")
            assert ei.value.fields == {"kind": "receipt", "id": "r1", "file": bad}
        with pytest.raises(ValidationFailed, match="^not_found: .*add_file source"):
            art.add_file("w.pt", tmp_path / "missing.pt")
        assert (art.dir / "a.txt").read_text(encoding="utf-8") == "a"
        with pytest.raises(ValidationFailed, match="^not_found: reserved file 'big.npy'"):
            art.commit()
    assert store.is_partial(roots.data, "receipt", "r1")


def test_commit_refuses_drifted_inputs(roots):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"v1")
    spec = _spec(inputs=[InputRef(name="plan", path=str(plan))])
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        plan.write_bytes(b"v2")
        with pytest.raises(
            IntegrityError, match="^drift: input 'plan' changed during the job"
        ) as ei:
            art.commit()
        assert ei.value.fields == {"kind": "receipt", "id": "r1", "input": "plan"}
    assert store.is_partial(roots.data, "receipt", "r1")
    plan.write_bytes(b"v1")
    with ArtifactWriter.create(
        _spec(id="r2", inputs=[InputRef(name="plan", path=str(plan))]), data_root=roots.data
    ) as art:
        plan.unlink()
        with pytest.raises(IntegrityError, match="^drift: input 'plan' .*gone"):
            art.commit()


def test_closed_after_commit(roots):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        art.commit()
        with pytest.raises(ValidationFailed, match="^closed: ") as ei:
            art.write_text("b.txt", "b")
        assert ei.value.fields == {"kind": "receipt", "id": "r1"}
        with pytest.raises(ValidationFailed, match="^closed: "):
            art.commit()
    with pytest.raises(ValidationFailed, match="^closed: "):
        art.reserve("c.npy")
    assert [f.name for f in store.load_manifest(roots.data, "receipt", "r1").files] == ["a.txt"]
    assert not (art.dir / "failure.json").exists()


def test_exception_leaves_a_partial_with_a_redacted_failure_record(roots):
    with pytest.raises(RuntimeError, match="upload failed"):
        with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
            art.write_text("a.txt", "a")
            raise RuntimeError(f"upload failed key={SECRET}")
    d = art.dir
    assert store.is_partial(roots.data, "receipt", "r1")
    rec = FailureRecord.model_validate_json((d / "failure.json").read_text(encoding="utf-8"))
    assert rec.exception == "RuntimeError" and rec.message == "upload failed key=<redacted>"
    assert rec.ts.endswith("Z")
    for p in d.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
    with pytest.raises(ValidationFailed, match="^closed: "):
        art.write_text("late.txt", "x")


def test_manifest_publish_failure_leaves_a_partial(roots, monkeypatch):
    real_replace = os.replace

    def flaky(src, dst):
        if str(dst).endswith("manifest.json"):
            raise OSError("power cut")
        return real_replace(src, dst)

    with pytest.raises(OSError, match="power cut"):
        with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
            art.write_text("a.txt", "a")
            monkeypatch.setattr(atomic.os, "replace", flaky)
            art.commit()
    assert store.is_partial(roots.data, "receipt", "r1") and art.manifest is None
    assert sorted(p.name for p in art.dir.iterdir()) == ["a.txt", "failure.json", "spec.json"]


def test_writer_refuses_to_continue_after_clean_removed_its_directory(roots):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("early.txt", "e")
        clean(roots.data, older_than=timedelta(0), apply=True)  # removes the open job's directory
        with pytest.raises(
            ValidationFailed, match="^not_found: .*removed while the job was open"
        ) as ei:
            art.write_text("late.txt", "l")
        assert ei.value.fields == {"kind": "receipt", "id": "r1"}
        with pytest.raises(ValidationFailed, match="^not_found: .*removed while the job was open"):
            art.reserve("x.npy")
        with pytest.raises(ValidationFailed, match="^not_found: .*removed while the job was open"):
            art.commit()
    # a normal (non-exceptional) exit must not recreate the directory it was removed from
    assert not art.dir.exists()
    assert not store.is_partial(roots.data, "receipt", "r1")


def test_failure_record_does_not_resurrect_a_removed_directory(roots):
    with pytest.raises(RuntimeError, match="boom"):
        with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
            art.write_text("early.txt", "e")
            clean(roots.data, older_than=timedelta(0), apply=True)
            raise RuntimeError("boom")
    # writing failure.json alone would resurrect the directory as an uncleanable foreign one
    assert not art.dir.exists()
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art2:
        art2.write_text("a.txt", "a")
        art2.commit()
    assert store.load_manifest(roots.data, "receipt", "r1") == art2.manifest
