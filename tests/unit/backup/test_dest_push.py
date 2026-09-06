import pytest

from backup_fixtures import SECRET, FakeRemote
from submit_fixtures import TEST
from vcp.backup import dest as destmod
from vcp.backup.dest import LocalDest, RcloneDest, open_dest, rclone_conf_state
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.push import PushResult, push
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _manifest(world):
    return build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))


def _push(world, dest, **kw) -> PushResult:
    return push(TEST, "m1", dest, **_kw(world), **kw)


def _ledger(world) -> BackupLedger:
    return BackupLedger(DatasetPaths.resolve(TEST, **_kw(world)).backup_log)


def _files(res, tier):
    return [f for f in res.manifest.files if f.tier == tier and f.kind == "file"]


def test_local_dest_round_trip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    d = LocalDest(str(tmp_path / "vault"))
    assert d.kind == "local" and d.hashes("data", ["x/a.txt"]) == {}
    d.put(src, "data", "x/a.txt")
    assert d.hashes("data", ["x/a.txt", "nope"]) == {"x/a.txt": sha256_file(src)}
    back = tmp_path / "back" / "a.txt"
    d.get("data", "x/a.txt", back)
    assert back.read_text(encoding="utf-8") == "hello"


def test_rclone_dest_commands_and_redaction(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    remote = FakeRemote()
    d = RcloneDest("fake:vault", remote)
    assert d.kind == "rclone" and d.hashes("data", ["x/a.txt"]) == {}
    assert remote.calls[-1] == ["rclone", "hashsum", "sha256", "fake:vault/data"]
    d.put(src, "data", "x/a.txt")
    assert remote.calls[-1] == [
        "rclone",
        "copyto",
        str(src),
        "fake:vault/data/x/a.txt",
        "--checksum",
    ]
    assert d.hashes("data", ["x/a.txt"]) == {"x/a.txt": sha256_file(src)}
    back = tmp_path / "back.txt"
    d.get("data", "x/a.txt", back)
    assert remote.calls[-1] == ["rclone", "copyto", "fake:vault/data/x/a.txt", str(back)]
    assert back.read_bytes() == b"hello"
    assert d.forget() == "fake" and remote.deleted == ["fake"]
    assert remote.calls[-1] == ["rclone", "config", "delete", "fake"]
    with pytest.raises(PlatformError, match="copyto failed") as ei:
        RcloneDest("fake:vault", FakeRemote(fail="copyto")).put(src, "data", "x/a.txt")
    assert SECRET not in str(ei.value) and "<redacted>" in str(ei.value)
    assert ei.value.fields == {"exit_code": 1}
    with pytest.raises(PlatformError, match="config delete failed"):
        RcloneDest("fake:vault", FakeRemote(fail="config")).forget()


def test_open_dest_and_conf_state(monkeypatch, tmp_path):
    assert isinstance(open_dest(str(tmp_path)), LocalDest)
    assert isinstance(open_dest("gdrive:x", FakeRemote()), RcloneDest)
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(VcpError, match="rclone_not_found") as ei:
        open_dest("gdrive:x")
    assert ei.value.fields == {"dest": "gdrive:x"}
    assert rclone_conf_state() == "unknown"
    conf = tmp_path / "rclone.conf"
    assert rclone_conf_state(FakeRemote(conf=conf)) == "absent"
    conf.write_text("[gdrive]\ntoken = x\n", encoding="utf-8")
    assert rclone_conf_state(FakeRemote(conf=conf)) == "present"
    assert rclone_conf_state(FakeRemote(fail="config")) == "unknown"


def test_push_local_copies_verifies_and_is_idempotent(world):
    res = _manifest(world)
    vault = world.tmp / "vault"
    out = _push(world, str(vault), tier=1)
    tier1 = _files(res, 1)
    assert out.pushed == len(tier1) and out.skipped == 0 and out.verified == len(tier1)
    assert out.failed == [] and out.bytes == sum(f.bytes for f in tier1) and out.forgotten is None
    assert (vault / "configs" / "datasets" / TEST / "submit.yaml").is_file()
    assert (vault / "data" / "runs" / "good" / "run.yaml").is_file()
    assert not (vault / "data" / "runs" / "good" / "predictions").exists()  # tier 2 not yet
    assert not (vault / "data" / "work").exists()  # never a checkpoint at tier 1
    again = _push(world, str(vault), tier=2)
    tier2 = _files(res, 2)
    assert again.pushed == len(tier2) and again.skipped == len(tier1)
    third = _push(world, str(vault), tier=2)
    assert third.pushed == 0 and third.skipped == len(tier1) + len(tier2) and third.failed == []
    rows = _ledger(world).of("push", "m1")
    assert [r.tier for r in rows] == [1, 2, 2] and rows[-1].pushed == 0 and rows[-1].bytes == 0
    _push(world, str(vault), tier=3)
    assert (vault / "data" / "work" / "good" / "weights" / "last.pt").is_file()
    assert not (vault / "data" / "work" / "good" / "weights" / "best.pt").exists()  # remote_copy


def test_push_refuses_a_stale_manifest_before_moving_anything(world):
    _manifest(world)
    vault = world.tmp / "vault"
    card = world.roots.data / "runs" / "good" / "run.yaml"
    card.write_bytes(card.read_bytes() + b"# touched\n")
    with pytest.raises(IntegrityError, match="drift") as ei:
        _push(world, str(vault), tier=1)
    assert ei.value.fields == {"file": "data/runs/good/run.yaml"} and not vault.exists()
    card.unlink()
    with pytest.raises(ValidationFailed, match="not_found") as ei:
        _push(world, str(vault), tier=1)
    assert ei.value.fields == {"file": "data/runs/good/run.yaml"} and not vault.exists()
    assert _ledger(world).of("push") == []
    with pytest.raises(ValidationFailed, match="tier") as ei:
        _push(world, str(vault), tier=4)
    assert ei.value.fields == {"tier": 4}
    with pytest.raises(ValidationFailed, match="not_found"):
        push(TEST, "m9", str(vault), **_kw(world))


def test_push_rclone_reports_mismatch_and_records_before_raising(world):
    res = _manifest(world)
    remote = FakeRemote(corrupt="submit.yaml")
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        _push(world, "fake:vault", tier=1, runner=remote)
    n = len(_files(res, 1))
    assert ei.value.fields == {"pushed": n, "skipped": 0, "verified": n - 1, "failed": 1}
    row = _ledger(world).latest("push", "m1")
    assert row.failed == [f"configs/datasets/{TEST}/submit.yaml"] and row.pushed == n
    assert remote.deleted == []
    assert len([c for c in remote.calls if c[1] == "hashsum"]) == 4  # 2 roots x before/after
    assert SECRET not in str(ei.value)


def test_push_rclone_failure_mid_way_still_writes_the_row(world):
    res = _manifest(world)
    remote = FakeRemote(fail="copyto")
    with pytest.raises(PlatformError, match="copyto failed") as ei:
        _push(world, "fake:vault", tier=1, runner=remote)
    n = len(_files(res, 1))
    assert ei.value.fields["pushed"] == 0 and ei.value.fields["failed"] == n
    assert ei.value.fields["exit_code"] == 1 and SECRET not in str(ei.value)
    row = _ledger(world).latest("push", "m1")
    assert row.pushed == 0 and row.verified == 0 and len(row.failed) == n


def test_forget_remote_only_after_everything_verified(world):
    _manifest(world)
    with pytest.raises(ValidationFailed, match="forget_refused"):
        _push(world, str(world.tmp / "vault"), tier=1, forget_remote=True)
    assert not (world.tmp / "vault").exists() and _ledger(world).of("push") == []
    remote = FakeRemote(corrupt="submit.yaml")
    with pytest.raises(IntegrityError):
        _push(world, "fake:vault", tier=1, forget_remote=True, runner=remote)
    assert remote.deleted == []
    good = FakeRemote()
    out = _push(world, "fake:vault", tier=1, forget_remote=True, runner=good)
    assert out.forgotten == "fake" and good.deleted == ["fake"]
    rows = _ledger(world).rows
    assert [r.event for r in rows[-2:]] == ["push", "remote_forgotten"]
    assert rows[-1].remote == "fake"
