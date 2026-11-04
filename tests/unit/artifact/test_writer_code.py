"""``ArtifactWriter`` and its reserved input ``code_snapshot.<label>`` (spec 2026-10-31 §5.5, §10.5;
VCP-004 acceptance 1-2): the input must be a code snapshot of this data root that holds, checked
before anything is claimed and again at commit; a snapshot this process took with
``snapshot_inputs`` also holds the imported modules against its root."""

import shutil
from pathlib import Path

import pytest

from code_fixtures import also_loaded, write_file
from vcp.artifact.schema import AUTHORIZATION_INPUT, CODE_INPUT_PREFIX, ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.code import process
from vcp.code.process import snapshot_inputs
from vcp.code.scan import scan_root
from vcp.code.snapshot import FILES_DIR, KIND, snapshot_json_path, write_snapshot
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir


def _features(inputs: list[InputRef], artifact_id: str = "f1") -> ArtifactSpec:
    return ArtifactSpec(kind="features", id=artifact_id, inputs=inputs)


def _job(roots, inputs: list[InputRef], artifact_id: str = "f1"):
    writer = ArtifactWriter.create(_features(inputs, artifact_id), data_root=roots.data)
    writer.write_bytes("features.bin", b"f")
    return writer


def _static_snapshot(roots, tmp_path) -> Path:
    """A snapshot another process took: written directly, never through ``snapshot_inputs``."""
    code = tmp_path / "code"
    write_file(code / "a.py", b"a = 1\n")
    outcome = write_snapshot(roots.data, scan_root(code, kind="dir"), command="test")
    return snapshot_json_path(roots.data, outcome.snapshot_id)


def _evidence_manifest(roots, tmp_path) -> Path:
    src = write_file(tmp_path / "e.txt", b"e")
    with ArtifactWriter.create(ArtifactSpec(kind="evidence", id="e1"), data_root=roots.data) as w:
        w.add_file("e.txt", src)
        w.commit()
    return artifact_dir(roots.data, "evidence", "e1") / "manifest.json"


def test_the_reserved_names():
    """The names are a contract (spec §5.5): old artifacts with such inputs are not re-read."""
    assert (CODE_INPUT_PREFIX, AUTHORIZATION_INPUT) == ("code_snapshot.", "authorization")


@pytest.mark.parametrize(
    ("bad", "word"),
    [
        ("not_a_snapshot", "^not_found:"),
        ("elsewhere", "^not_found:"),
        ("sha_only", "^not_found:"),
        ("tampered", "^mismatch:"),
    ],
)
def test_a_wrong_or_broken_snapshot_input_leaves_no_directory(roots, tmp_path, bad, word):
    snapshot = _static_snapshot(roots, tmp_path)
    if bad == "not_a_snapshot":
        ref = InputRef(name="code_snapshot.cwd", path=str(_evidence_manifest(roots, tmp_path)))
    elif bad == "elsewhere":  # the right shape, but not in this data root
        copy = tmp_path / "other" / snapshot.relative_to(roots.data)
        copy.parent.mkdir(parents=True)
        shutil.copyfile(snapshot, copy)
        ref = InputRef(name="code_snapshot.cwd", path=str(copy))
    elif bad == "sha_only":
        ref = InputRef(name="code_snapshot.cwd", sha256=sha256_file(snapshot))
    else:
        (snapshot.parent / FILES_DIR / "a.py").write_bytes(b"a = 2\n")
        ref = InputRef(name="code_snapshot.cwd", path=str(snapshot))
    with pytest.raises((ValidationFailed, IntegrityError), match=word) as ei:
        ArtifactWriter.create(_features([ref]), data_root=roots.data)
    assert ei.value.fields["input"] == "code_snapshot.cwd"
    assert ei.value.fields["kind"] == "features" and ei.value.fields["id"] == "f1"
    assert not (roots.data / "artifacts" / "features").exists()


def test_a_static_snapshot_input_is_only_checked_for_integrity(roots, tmp_path, monkeypatch):
    """A snapshot another process took: the modules of this process are not its business."""
    snapshot = _static_snapshot(roots, tmp_path)
    also_loaded(monkeypatch, "stray", write_file(tmp_path / "elsewhere" / "stray.py"))
    ref = InputRef(name="code_snapshot.cwd", path=str(snapshot))
    manifest = _job(roots, [ref]).commit()
    assert [(i.name, i.sha256) for i in manifest.spec.inputs] == [
        ("code_snapshot.cwd", sha256_file(snapshot))
    ]


def test_a_snapshot_damaged_during_the_job_fails_commit(roots, tmp_path):
    snapshot = _static_snapshot(roots, tmp_path)
    writer = _job(roots, [InputRef(name="code_snapshot.cwd", path=str(snapshot))])
    (snapshot.parent / FILES_DIR / "a.py").write_bytes(b"a = 2\n")
    with pytest.raises(IntegrityError, match="^mismatch:"):
        writer.commit()
    assert not (writer.dir / "manifest.json").exists()


def test_an_imported_file_changed_during_the_job_fails_commit(roots, proj):
    writer = _job(roots, snapshot_inputs(roots.data))
    (proj / "helper.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="^drift: imported cwd:helper.py changed"):
        writer.commit()
    assert not (writer.dir / "manifest.json").exists()


def test_a_file_nobody_imported_may_change(roots, proj):
    inputs = snapshot_inputs(roots.data)
    writer = _job(roots, inputs)
    (proj / "unused.py").write_text("y = 2\n", encoding="utf-8")
    manifest = writer.commit()
    assert [i.name for i in manifest.spec.inputs] == ["code_snapshot.cwd"]
    assert manifest.spec.inputs[0].path.startswith(f"artifacts/{KIND}/")


def test_a_module_outside_the_roots_fails_at_create(roots, proj, tmp_path, monkeypatch):
    stray = write_file(tmp_path / "elsewhere" / "stray.py")
    also_loaded(monkeypatch, "stray", stray)
    inputs = snapshot_inputs(roots.data)
    with pytest.raises(ValidationFailed, match="^code_outside: 1 imported module") as ei:
        ArtifactWriter.create(_features(inputs), data_root=roots.data)
    assert stray.resolve().as_posix() in str(ei.value) and "extra_roots" in str(ei.value)
    assert not (roots.data / "artifacts" / "features").exists()


def test_a_module_imported_during_the_job_is_checked_at_commit(roots, proj, monkeypatch):
    writer = _job(roots, snapshot_inputs(roots.data))
    also_loaded(monkeypatch, "late", write_file(proj / "late.py"))  # not in the snapshot
    with pytest.raises(ValidationFailed, match="^code_outside: 1 imported module") as ei:
        writer.commit()
    assert "cwd:late.py" in str(ei.value)


def test_generated_modules_do_not_trigger_code_outside(roots, proj, monkeypatch):
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "inductor")  # read from this process's cwd
    also_loaded(monkeypatch, "kernel", write_file(proj / "inductor" / "ab" / "kernel.py"))
    writer = _job(roots, snapshot_inputs(roots.data))
    assert writer.commit().spec.inputs[0].name == "code_snapshot.cwd"


def test_extra_roots_cover_a_module(roots, proj, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    also_loaded(monkeypatch, "stray", write_file(elsewhere / "stray.py"))
    inputs = snapshot_inputs(roots.data, extra_roots=[elsewhere])
    assert [i.name for i in inputs] == ["code_snapshot.cwd", "code_snapshot.root-1"]
    manifest = _job(roots, inputs).commit()
    assert [i.name for i in manifest.spec.inputs] == ["code_snapshot.cwd", "code_snapshot.root-1"]


def test_the_tracked_roots_belong_to_one_data_root(roots, proj, tmp_path, monkeypatch):
    """The same content snapshotted in another data root by another process is a static input
    there, although this process tracks that id for ``roots.data``."""
    also_loaded(monkeypatch, "stray", write_file(tmp_path / "elsewhere" / "stray.py"))
    tracked = snapshot_inputs(roots.data)
    with pytest.raises(ValidationFailed, match="^code_outside:"):
        ArtifactWriter.create(_features(tracked), data_root=roots.data)
    other = tmp_path / "other-data"
    outcome = write_snapshot(other, scan_root(proj, kind="dir"), command="test")
    assert outcome.snapshot_id == Path(tracked[0].path).parent.name  # the same content
    ref = InputRef(
        name="code_snapshot.cwd", path=str(snapshot_json_path(other, outcome.snapshot_id))
    )
    writer = ArtifactWriter.create(_features([ref]), data_root=other)
    writer.write_bytes("features.bin", b"f")
    assert writer.commit().spec.inputs[0].name == "code_snapshot.cwd"
    assert len(process._TRACKED) == 1
