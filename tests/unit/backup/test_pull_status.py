import pytest

from backup_fixtures import FakeRemote
from submit_fixtures import EVAL, TEST
from vcp.backup import dest as destmod
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.pull import pull
from vcp.backup.push import push
from vcp.backup.status import local_ok, passed, status
from vcp.backup.verify import verify
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.runs import load_run, run_dir
from vcp.train.checkpoints import register
from vcp.train.records import load_record, save_record


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world):
    return DatasetPaths.resolve(TEST, **_kw(world))


def _pred(world, subset="valB"):
    card = load_run(world.roots.data, "good")
    return run_dir(world.roots.data, "good") / card.predictions[subset].path


def _ready(world, tier=3):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=tier, **_kw(world))
    return vault, len(load_manifest(_paths(world), "m1").files)


def test_pull_restores_missing_files_and_verifies(world):
    vault, n = _ready(world)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.unlink()
    (world.weights / "best.pt").unlink()  # the remote_copy comes back from vault-train
    res = pull(TEST, "m1", str(vault), **_kw(world))
    assert res.pulled == 2 and res.skipped == n - 2
    assert res.conflicts == [] and res.missing == [] and res.mismatch == []
    assert pred.read_bytes() == original
    assert (world.weights / "best.pt").read_bytes() == b"best weights"
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.pulled == 2 and row.tier == 3 and row.conflicts == [] and row.dest == str(vault)
    again = pull(TEST, "m1", str(vault), **_kw(world))
    assert again.pulled == 0 and again.skipped == n
    with pytest.raises(ValidationFailed, match="tier"):
        pull(TEST, "m1", str(vault), tier=0, **_kw(world))
    with pytest.raises(ValidationFailed, match="not_found"):
        pull(TEST, "m9", str(vault), **_kw(world))


def test_pull_conflicts_overwrite_and_missing(world):
    vault, n = _ready(world, tier=2)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.write_bytes(b"edited locally\n")
    with pytest.raises(ValidationFailed, match="conflict") as ei:
        pull(TEST, "m1", str(vault), tier=2, **_kw(world))
    assert ei.value.fields["conflicts"] == 1 and pred.read_bytes() == b"edited locally\n"
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.conflicts == ["data/runs/good/predictions/valB.jsonl"] and row.pulled == 0
    res = pull(TEST, "m1", str(vault), tier=2, overwrite=True, **_kw(world))
    assert res.pulled == 1 and res.conflicts == [] and pred.read_bytes() == original
    baks = list(pred.parent.glob("valB.jsonl.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == b"edited locally\n"
    (world.weights / "last.pt").unlink()  # tier 3 was never pushed to the vault
    with pytest.raises(IntegrityError, match="missing") as ei:
        pull(TEST, "m1", str(vault), tier=3, **_kw(world))
    assert ei.value.fields["missing"] == 1 and not (world.weights / "last.pt").exists()
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.dest_missing == 1 and row.mismatch == [] and row.missing is None


def test_pull_rejects_corrupt_copies_without_keeping_them(world):
    vault, _ = _ready(world, tier=2)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.unlink()
    copy = vault / "data" / "runs" / "good" / "predictions" / "valB.jsonl"
    copy.write_bytes(b"corrupt")
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        pull(TEST, "m1", str(vault), tier=2, **_kw(world))
    assert not pred.exists() and ei.value.fields["mismatch"] == 1
    liar = FakeRemote(deliver=b"garbage")  # honest hashsum, dishonest copyto
    pred.write_bytes(original)
    push(TEST, "m1", "fake:vault", tier=2, runner=liar, **_kw(world))
    pred.unlink()
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        pull(TEST, "m1", "fake:vault", tier=2, runner=liar, **_kw(world))
    assert not pred.exists() and ei.value.fields["mismatch"] == 1


def test_overwrite_never_leaves_only_the_backup(world):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    honest = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=2, runner=honest, **_kw(world))
    pred = _pred(world)
    pred.write_bytes(b"edited locally\n")
    liar = FakeRemote(deliver=b"garbage")  # honest hashsum, dishonest copyto
    liar.store = honest.store
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        pull(TEST, "m1", "fake:vault", tier=2, overwrite=True, runner=liar, **_kw(world))
    assert ei.value.fields["mismatch"] == 1 and ei.value.fields["pulled"] == 0
    assert pred.read_bytes() == b"edited locally\n"
    assert list(pred.parent.glob("*.bak-*")) == []
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.pulled == 0 and row.mismatch == ["data/runs/good/predictions/valB.jsonl"]
    broken = FakeRemote(fail="copyto")
    broken.store = honest.store
    with pytest.raises(PlatformError, match="copyto failed") as ei:
        pull(TEST, "m1", "fake:vault", tier=2, overwrite=True, runner=broken, **_kw(world))
    assert ei.value.fields["pulled"] == 0 and pred.read_bytes() == b"edited locally\n"
    assert list(pred.parent.glob("*.bak-*")) == []


def test_pull_external_only_into_an_existing_directory(world, tmp_path):
    outside = tmp_path / "elsewhere" / "extra.pt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"extra")
    rec = load_record(world.roots.data, "good")
    rec, _ = register(rec, [outside], data_root=world.roots.data, attempt=2)
    save_record(world.roots.data, rec)
    build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    key = next(
        f.key
        for f in load_manifest(DatasetPaths.resolve(EVAL, **_kw(world)), "mg").files
        if f.root == "external"
    )
    vault = world.tmp / "vault-ext"
    push(EVAL, "mg", str(vault), tier=3, **_kw(world))
    assert (vault / "external" / key.split("/", 1)[1]).read_bytes() == b"extra"
    outside.unlink()
    outside.parent.rmdir()
    res = pull(EVAL, "mg", str(vault), **_kw(world))
    assert res.external_skipped == [key] and res.pulled == 0 and not outside.parent.exists()
    row = BackupLedger(DatasetPaths.resolve(EVAL, **_kw(world)).backup_log).latest("pull", "mg")
    assert row.external_skipped == 1
    outside.parent.mkdir()
    res = pull(EVAL, "mg", str(vault), **_kw(world))
    assert res.pulled == 1 and res.external_skipped == [] and outside.read_bytes() == b"extra"


def test_status_view(world, monkeypatch):
    conf = world.tmp / "rclone.conf"
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    assert view.manifests == [] and view.rclone_conf == "absent" and view.unverified == []
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    m = view.manifests[0]
    assert m.manifest_id == "m1" and m.conclusion == "submission:S1" and m.files > 10
    assert m.pushed_tiers == [] and m.unpushed_tiers == [1, 2, 3] and not m.verified
    assert m.last_push is None and m.last_verify is None and view.unverified == ["m1"]
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=2, **_kw(world))
    verify(TEST, "m1", dest=str(vault), tier=2, **_kw(world))
    conf.write_text("[gdrive]\n", encoding="utf-8")
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    m = view.manifests[0]
    assert m.pushed_tiers == [1, 2] and m.unpushed_tiers == [3]
    assert not m.verified and m.local_ok  # tier 2 says nothing about the weights' copies
    assert m.last_push.tier == 2 and m.last_verify.drift == 0 and view.unverified == ["m1"]
    assert view.rclone_conf == "present"
    push(TEST, "m1", str(vault), tier=3, **_kw(world))
    verify(TEST, "m1", dest=str(vault), **_kw(world))
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    m = view.manifests[0]
    assert m.pushed_tiers == [1, 2, 3] and m.verified and m.local_ok and view.unverified == []
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    assert status(TEST, **_kw(world)).rclone_conf == "unknown"
    rows = BackupLedger(_paths(world).backup_log).of("verify", "m1")
    assert passed(rows[-1]) and local_ok(rows[-1])
    bad = rows[-1].model_copy(update={"copies": {"ok": 1, "missing": 1, "mismatch": 0}})
    assert not passed(bad) and not passed(rows[-1].model_copy(update={"bad_stamps": 1}))
    verify(TEST, "m1", **_kw(world))  # no --dest: the copies layer never ran
    offline = BackupLedger(_paths(world).backup_log).of("verify", "m1")[-1]
    assert not passed(offline) and local_ok(offline) and offline.copies is None
