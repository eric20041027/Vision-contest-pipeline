"""VCP-046 (spec 2026-10-04 §5, §9): a checkpoint copy `train upload` left on this machine is not
a backup somewhere else. A remote_copy is checked in place only when it is on an rclone remote or
inside the destination itself; any other is pushed to the destination like a file, verified
there, pulled from there (its local copy is the fallback), and counted by --forget-remote."""

import json
import sys

import pytest
from typer.testing import CliRunner

from backup_fixtures import FakeRemote
from submit_fixtures import EVAL, TEST
from vcp.backup.dest import covers
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.pull import pull
from vcp.backup.push import push
from vcp.backup.schema import BackupRow, RemoteCopy
from vcp.backup.status import status
from vcp.backup.verify import verify
from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.train.records import load_record, save_record
from vcp.train.schema import UploadRecord
from vcp.train.status import upload_run
from vcp.train.upload import merge_uploads

BEST = "data/work/good/weights/best.pt"
REMOTE_BEST = f"fake:vault/{BEST}"


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _ledger(world, name=TEST):
    return BackupLedger(DatasetPaths.resolve(name, **_kw(world)).backup_log)


def _manifest(world):
    """S1's manifest: best.pt is a remote_copy whose copy `train upload` put in a local folder."""
    res = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.kind == "remote_copy"]
    assert (entry.key, entry.remote.dest) == (BEST, str(world.vault))
    return res.manifest


def _status(world, dataset=TEST):
    [m] = status(dataset, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world)).manifests
    return m


def _uploads(world, *uploads):
    record = load_record(world.roots.data, "good")
    save_record(world.roots.data, merge_uploads(record, list(uploads)))


def _upload(world, dest: str, kind: str, at: str) -> UploadRecord:
    best = next(c for c in load_record(world.roots.data, "good").checkpoints if c.final)
    return UploadRecord(
        dest=dest, kind=kind, name="best.pt", sha256=best.sha256, verified=True, uploaded_at=at
    )


def test_covers_matrix(tmp_path):
    usb = tmp_path / "usb"
    local = RemoteCopy(dest=str(usb / "weights"), run="good", name="best.pt")
    remote = RemoteCopy(dest="gd:weights", run="good", name="best.pt")
    assert covers("fake:vault", remote) and covers(str(usb), remote)  # off this machine already
    assert not covers("fake:vault", local)  # a local copy, an rclone destination
    assert covers(str(usb), local)  # inside this local destination
    assert not covers(str(tmp_path / "other"), local)  # outside it


def test_a_tier_3_push_to_rclone_sends_a_local_copy_from_its_checkpoint(world):
    _manifest(world)
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 1 and out.failed == []
    assert remote.store[REMOTE_BEST] == b"best weights"
    assert _ledger(world).latest("push", "m1").local_copies == 1


def test_a_deleted_checkpoint_is_sent_from_its_local_copy(world):
    _manifest(world)
    (world.weights / "best.pt").unlink()  # disk freed after `train upload`
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 1 and remote.store[REMOTE_BEST] == b"best weights"


def test_push_fails_before_any_byte_moves_when_no_copy_holds_the_bytes(world):
    _manifest(world)
    (world.weights / "best.pt").write_bytes(b"retrained")
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")
    remote = FakeRemote()
    with pytest.raises(IntegrityError, match="drift:") as ei:
        push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert ei.value.fields == {"file": BEST} and remote.calls == []
    (world.weights / "best.pt").unlink()
    (world.vault / "good" / "best.pt").unlink()
    with pytest.raises(ValidationFailed, match="not_found:") as ei:
        push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert ei.value.fields == {"file": BEST} and remote.calls == []
    assert _ledger(world).of("push") == []


def test_a_tier_2_push_sends_no_local_copy(world):
    _manifest(world)
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=2, runner=remote, **_kw(world))
    assert out.local_copies == 0 and not any(key.endswith(".pt") for key in remote.store)
    assert _ledger(world).latest("push", "m1").local_copies is None


def test_the_manifest_prefers_an_rclone_upload_to_a_newer_local_one(world):
    _uploads(
        world,
        _upload(world, "fake:w", "rclone", "2026-10-04T00:00:00.000Z"),
        _upload(world, str(world.tmp / "later"), "local", "2026-10-04T01:00:00.000Z"),
    )
    res = build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.kind == "remote_copy"]
    assert entry.remote.dest == "fake:w"


def test_an_rclone_remote_copy_is_verified_in_place_and_never_pushed(world):
    _uploads(world, _upload(world, "fake:w", "rclone", "2026-10-04T00:00:00.000Z"))
    build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    remote = FakeRemote()
    remote.store["fake:w/good/best.pt"] = (world.weights / "best.pt").read_bytes()
    out = push(EVAL, "mg", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 0 and REMOTE_BEST not in remote.store
    res = verify(EVAL, "mg", dest="fake:vault", runner=remote, **_kw(world))
    assert res.ok and res.local_copies == 0


def test_verify_checks_a_local_copy_at_the_rclone_destination(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=2, runner=remote, **_kw(world))
    res = verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    assert f"missing:{BEST}" in res.copy_problems and res.local_copies == 1
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    res = verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    assert res.ok and res.local_copies == 1
    assert _ledger(world).latest("verify", "m1").local_copies == 1


def test_a_local_copy_listed_as_gone_is_missing_not_absent(world):
    (world.weights / "best.pt").unlink()
    _manifest(world)  # best.pt: present=false, still a remote_copy
    res = verify(TEST, "m1", dest="fake:vault", runner=FakeRemote(), **_kw(world))
    assert f"missing:{BEST}" in res.copy_problems and res.copies["absent"] == 0


def test_rows_written_before_013_do_not_vouch_for_a_local_copy(world):
    manifest = _manifest(world)
    n = len(manifest.files)
    ledger = _ledger(world)
    ledger.append(  # what 0.12 wrote: a tier-3 push without best.pt, best.pt checked in place
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            pushed=n - 1,
            skipped=0,
            verified=n - 1,
            failed=[],
            bytes=0,
        )
    )
    ledger.append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            copies={"ok": n, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
        )
    )
    m = _status(world)
    assert not m.verified and m.pushed_tiers == [1, 2]
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    m = _status(world)
    assert m.verified and m.pushed_tiers == [1, 2, 3]


def _passing_rows_012(world, dataset, manifest):
    """A passing tier-3 push and verify at fake:vault, the way vcp 0.12 wrote them: neither
    carries ``local_copies``."""
    n = len(manifest.files)
    ledger = _ledger(world, dataset)
    common = {"ts": stamp(), "manifest_id": manifest.manifest_id, "dest": "fake:vault", "tier": 3}
    ledger.append(
        BackupRow(event="push", pushed=n, skipped=0, verified=n, failed=[], bytes=0, **common)
    )
    ledger.append(
        BackupRow(
            event="verify",
            copies={"ok": n, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
            **common,
        )
    )


def test_rows_written_before_013_vouch_when_every_copy_is_on_an_rclone_remote(world):
    """spec 2026-10-04 §5.2: only a copy the destination does not cover asks for a 0.13 row."""
    _uploads(world, _upload(world, "fake:w", "rclone", "2026-10-04T00:00:00.000Z"))
    manifest = build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world)).manifest
    assert [f.remote.dest for f in manifest.files if f.remote is not None] == ["fake:w"]
    _passing_rows_012(world, EVAL, manifest)
    m = _status(world, EVAL)
    assert m.verified and m.pushed_tiers == [1, 2, 3]


def test_rows_written_before_013_vouch_for_a_manifest_without_copies(world):
    manifest = build_manifest(EVAL, "run:bad", manifest_id="mb", **_kw(world)).manifest
    assert [f for f in manifest.files if f.remote is not None] == []
    _passing_rows_012(world, EVAL, manifest)
    m = _status(world, EVAL)
    assert m.verified and m.pushed_tiers == [1, 2, 3]


def test_forget_remote_refuses_until_the_local_copy_is_at_the_destination(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    del remote.store[REMOTE_BEST]
    with pytest.raises(ValidationFailed, match="forget_refused") as ei:
        push(TEST, "m1", "fake:vault", tier=2, forget_remote=True, runner=remote, **_kw(world))
    assert ei.value.fields["unverified"] == 1 and remote.deleted == []
    out = push(TEST, "m1", "fake:vault", tier=3, forget_remote=True, runner=remote, **_kw(world))
    assert out.forgotten == "fake" and remote.deleted == ["fake"]


def test_pull_takes_a_local_copy_from_the_destination_then_falls_back_to_it(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    copy = world.vault / "good" / "best.pt"
    (world.weights / "best.pt").unlink()
    copy.unlink()  # as on a new machine: only the destination has it
    res = pull(TEST, "m1", "fake:vault", runner=remote, **_kw(world))
    assert res.pulled == 1 and (world.weights / "best.pt").read_bytes() == b"best weights"
    (world.weights / "best.pt").unlink()
    copy.write_bytes(b"best weights")  # this machine, and a destination that never got it
    res = pull(TEST, "m1", str(world.tmp / "empty-vault"), **_kw(world))
    assert res.pulled == 1 and (world.weights / "best.pt").read_bytes() == b"best weights"


def test_a_local_destination_that_holds_the_copy_checks_it_in_place(world):
    usb = world.tmp / "usb"
    upload_run(world.roots.data, "good", str(usb / "weights"), only_final=True)
    manifest = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world)).manifest
    [entry] = [f for f in manifest.files if f.kind == "remote_copy"]
    assert entry.remote.dest == str(usb / "weights")  # the newest local upload
    out = push(TEST, "m1", str(usb), tier=3, **_kw(world))
    assert out.local_copies == 0 and out.failed == []
    assert not (usb / "data" / "work" / "good" / "weights" / "best.pt").exists()
    res = verify(TEST, "m1", dest=str(usb), **_kw(world))
    assert res.ok and res.local_copies == 0


def test_a_local_destination_without_the_copy_receives_it(world):
    _manifest(world)
    usb = world.tmp / "usb"
    out = push(TEST, "m1", str(usb), tier=3, **_kw(world))
    assert out.local_copies == 1
    assert (usb / "data" / "work" / "good" / "weights" / "best.pt").read_bytes() == b"best weights"


# --- spec §5 in detail: a copy listed as gone, one covered copy among others, pull's fallback
# and its failure, the newest of two rclone uploads, Windows path case, the VERDICT fields ------


def _rclone_copy_of_last(world) -> None:
    """last.pt gains a copy on an rclone remote, next to best.pt's copy in a local folder."""
    last = next(c for c in load_record(world.roots.data, "good").checkpoints if not c.final)
    _uploads(
        world,
        UploadRecord(
            dest="fake:w",
            kind="rclone",
            name="last.pt",
            sha256=last.sha256,
            verified=True,
            uploaded_at="2026-10-04T00:00:00.000Z",
        ),
    )


def test_a_tier_3_push_sends_a_local_copy_the_manifest_lists_as_gone(world):
    (world.weights / "best.pt").unlink()
    _manifest(world)  # best.pt: present=false, still a remote_copy
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 1 and remote.store[REMOTE_BEST] == b"best weights"
    res = verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    assert res.ok and res.copies["absent"] == 0


def test_one_covered_copy_does_not_make_an_old_row_vouch_for_the_others(world):
    _rclone_copy_of_last(world)  # at fake:vault last.pt is covered, best.pt's copy is not
    n = len(build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world)).manifest.files)
    ledger = _ledger(world)
    ledger.append(
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            pushed=n,
            skipped=0,
            verified=n,
            failed=[],
            bytes=0,
        )
    )
    ledger.append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            copies={"ok": n, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
        )
    )
    m = _status(world)
    assert not m.verified and m.pushed_tiers == [1, 2]


def test_pull_falls_back_to_the_local_copy_when_the_destination_holds_other_bytes(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    remote.store[REMOTE_BEST] = b"rotted at the destination"
    (world.weights / "best.pt").unlink()
    res = pull(TEST, "m1", "fake:vault", runner=remote, **_kw(world))
    assert res.pulled == 1 and res.mismatch == []
    assert (world.weights / "best.pt").read_bytes() == b"best weights"


def test_pull_reports_the_copy_missing_when_no_copy_holds_the_bytes(world):
    _manifest(world)
    (world.weights / "best.pt").unlink()
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")  # and the destination has none
    with pytest.raises(IntegrityError, match="missing:") as ei:
        pull(TEST, "m1", str(world.tmp / "empty-vault"), **_kw(world))
    assert ei.value.fields["missing"] == 1 and ei.value.fields["mismatch"] == 0
    assert not (world.weights / "best.pt").exists()


def test_the_manifest_takes_the_newest_of_two_rclone_uploads(world):
    _uploads(
        world,
        _upload(world, "fake:old", "rclone", "2026-10-04T00:00:00.000Z"),
        _upload(world, "fake:new", "rclone", "2026-10-04T01:00:00.000Z"),
    )
    res = build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.kind == "remote_copy"]
    assert entry.remote.dest == "fake:new"


def test_covers_ignores_case_in_a_windows_path(tmp_path):
    if sys.platform != "win32":
        pytest.skip("only Windows paths ignore case")
    local = RemoteCopy(dest=str(tmp_path / "usb" / "weights"), run="good", name="best.pt")
    assert covers(str(tmp_path / "USB"), local)


def test_the_commands_report_local_copies(world):
    def run(*args: str) -> dict:
        r = CliRunner().invoke(app, ["backup", *args, "--json"])
        assert r.exit_code == 0, r.output
        return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))

    usb = str(world.tmp / "usb")
    common = ["--dataset", TEST, "--manifest", "m1"]
    doc = run("manifest", "--dataset", TEST, "--conclusion", "submission:S1", "--id", "m1")
    assert doc["fields"]["remote_copies"] == 1 and doc["fields"]["local_copies"] == 1
    doc = run("push", *common, "--dest", usb, "--tier", "3")
    assert doc["fields"]["local_copies"] == 1 and doc["result"]["local_copies"] == 1
    doc = run("verify", *common, "--dest", usb)
    assert doc["fields"]["local_copies"] == 1 and doc["result"]["local_copies"] == 1
    assert "local_copies" not in run("verify", *common)["fields"]  # no destination, no copies
    _rclone_copy_of_last(world)  # a copy on an rclone remote is not on this machine
    doc = run("manifest", "--dataset", TEST, "--conclusion", "submission:S1", "--id", "m2")
    assert doc["fields"]["remote_copies"] == 2 and doc["fields"]["local_copies"] == 1
