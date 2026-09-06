import subprocess
from pathlib import Path

import pytest

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.train import upload as upmod
from vcp.train.checkpoints import mark_final, register
from vcp.train.schema import TrainRecord
from vcp.train.upload import NAME_COLLISION, dest_kind, merge_uploads, upload


def _record() -> TrainRecord:
    return TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )


def _registered(roots):
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best")
    (w / "last.pt").write_bytes(b"last")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    return mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt")), w


@pytest.mark.parametrize(
    "dest, kind",
    [
        ("gdrive:vcp/weights", "rclone"),
        ("s3-team:bucket/w", "rclone"),
        ("remote:", "rclone"),
        ("C:/vault/weights", "local"),
        ("C:\\vault", "local"),
        ("/mnt/vault", "local"),
        ("vault", "local"),
    ],
)
def test_dest_kind(dest, kind):
    assert dest_kind(dest) == kind


def test_local_upload_copies_verifies_and_is_idempotent(roots, tmp_path):
    rec, w = _registered(roots)
    dest = tmp_path / "vault"
    out = upload(rec, str(dest), data_root=roots.data)
    assert out.uploaded == 2 and out.skipped == 0
    assert sorted(r.name for r in out.records) == ["best.pt", "last.pt"]
    assert all(r.verified and r.kind == "local" and r.dest == str(dest) for r in out.records)
    assert sha256_file(dest / "r1" / "best.pt") == sha256_file(w / "best.pt")
    again = upload(rec, str(dest), data_root=roots.data)
    assert again.uploaded == 0 and again.skipped == 2 and all(r.verified for r in again.records)
    only = upload(rec, str(tmp_path / "v2"), data_root=roots.data, only_final=True)
    assert [r.name for r in only.records] == ["best.pt"]


def test_local_upload_refuses_changed_or_missing_source(roots, tmp_path):
    rec, w = _registered(roots)
    (w / "best.pt").write_bytes(b"changed")
    with pytest.raises(ValidationFailed, match="changed since") as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "work/weights/best.pt"}
    (w / "best.pt").unlink()
    with pytest.raises(ValidationFailed, match="missing") as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "work/weights/best.pt"}
    assert not (tmp_path / "vault" / "r1" / "last.pt").exists()  # nothing copied before the check


def test_name_collision(roots, tmp_path):
    rec, w = _registered(roots)
    other = roots.data / "work" / "run2" / "best.pt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"different best")
    rec, _ = register(rec, [other], data_root=roots.data, attempt=1)
    with pytest.raises(ValidationFailed, match=NAME_COLLISION) as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "best.pt"}


def test_resume_same_path_different_sha_uploads_newest_not_a_collision(roots, tmp_path):
    """I1: a --resume that changes a checkpoint's bytes registers a SECOND CheckpointRecord for
    the SAME path (old sha, then new sha) -- that is history, not a name collision, and only
    the newest bytes should be uploaded."""
    rec = _record()
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best v1")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    old_sha = sha256_file(w / "best.pt")
    (w / "best.pt").write_bytes(b"best v2")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=2)
    new_sha = sha256_file(w / "best.pt")
    rec = mark_final(rec, "work/weights/best.pt", new_sha)
    assert [c.sha256 for c in rec.checkpoints] == [old_sha, new_sha]

    dest = tmp_path / "vault"
    out = upload(rec, str(dest), data_root=roots.data)
    assert out.uploaded == 1 and out.skipped == 0
    assert [r.sha256 for r in out.records] == [new_sha]
    assert sha256_file(dest / "r1" / "best.pt") == new_sha

    # last.pt also present (a normal run's checkpoints): still no collision, one more upload
    (w / "last.pt").write_bytes(b"last")
    rec, _ = register(rec, [w / "last.pt"], data_root=roots.data, attempt=2)
    out2 = upload(rec, str(tmp_path / "vault2"), data_root=roots.data)
    assert out2.uploaded == 2 and out2.skipped == 0
    assert sorted(r.name for r in out2.records) == ["best.pt", "last.pt"]


class FakeRclone:
    """A remote that remembers what was copied; ``hashsum`` reports what it holds."""

    def __init__(self, *, corrupt: str | None = None, fail_copy: bool = False):
        self.store: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.corrupt = corrupt
        self.fail_copy = fail_copy

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "copyto":
            if self.fail_copy:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="copy failed")
            src, target = args[2], args[3]
            self.store[target.rsplit("/", 1)[1]] = sha256_file(Path(src))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[1] == "hashsum":
            lines = [
                f"{'0' * 64 if name == self.corrupt else sha}  {name}"
                for name, sha in self.store.items()
            ]
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")
        raise AssertionError(args)


def test_rclone_upload_verifies_via_hashsum(roots):
    rec, w = _registered(roots)
    remote = FakeRclone()
    out = upload(rec, "gdrive:vcp/weights", data_root=roots.data, runner=remote)
    assert out.uploaded == 2 and out.skipped == 0 and all(r.verified for r in out.records)
    assert all(r.kind == "rclone" and r.dest == "gdrive:vcp/weights" for r in out.records)
    copy_calls = [c for c in remote.calls if c[1] == "copyto"]
    assert copy_calls[0][3] == "gdrive:vcp/weights/r1/best.pt" and "--checksum" in copy_calls[0]
    again = upload(rec, "gdrive:vcp/weights", data_root=roots.data, runner=remote)
    assert again.uploaded == 0 and again.skipped == 2 and all(r.verified for r in again.records)


def test_rclone_upload_reports_unverified_and_failures(roots):
    rec, w = _registered(roots)
    bad = FakeRclone(corrupt="last.pt")
    out = upload(rec, "gdrive:w", data_root=roots.data, runner=bad)
    assert {r.name: r.verified for r in out.records} == {"best.pt": True, "last.pt": False}
    with pytest.raises(VcpError, match="copyto failed"):
        upload(rec, "gdrive:w", data_root=roots.data, runner=FakeRclone(fail_copy=True))


def test_default_runner_requires_rclone(roots, monkeypatch):
    rec, _ = _registered(roots)
    monkeypatch.setattr(upmod.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(VcpError, match="rclone not found"):
        upload(rec, "gdrive:w", data_root=roots.data)


def test_merge_uploads_replaces_same_identity(roots, tmp_path):
    rec, w = _registered(roots)
    out = upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    merged = merge_uploads(rec, out.records)
    assert len(merged.uploads) == 2
    merged2 = merge_uploads(
        merged, upload(rec, str(tmp_path / "vault"), data_root=roots.data).records
    )
    assert (
        len(merged2.uploads) == 2
        and merged2.uploads[0].uploaded_at >= merged.uploads[0].uploaded_at
    )
    merged3 = merge_uploads(
        merged2, upload(rec, str(tmp_path / "v2"), data_root=roots.data).records
    )
    assert len(merged3.uploads) == 4
