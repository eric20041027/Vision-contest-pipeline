import json

import pytest

from backup_fixtures import SECRET, FakeRemote
from submit_fixtures import EVAL, STAMP, TEST
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.push import push
from vcp.backup.verify import Drift, sha256_prefix, verify
from vcp.core.errors import PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run, run_dir
from vcp.train.records import load_record, save_record
from vcp.train.schema import UploadRecord
from vcp.train.upload import merge_uploads


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world, name=TEST):
    return DatasetPaths.resolve(name, **_kw(world))


def _pred(world, run="good", subset="valB"):
    card = load_run(world.roots.data, run)
    return run_dir(world.roots.data, run) / card.predictions[subset].path


@pytest.fixture
def pushed(world):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=3, **_kw(world))
    return vault


def test_sha256_prefix(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abcdef")
    (tmp_path / "g").write_bytes(b"abc")
    assert sha256_prefix(p, 3) == sha256_file(tmp_path / "g")
    assert sha256_prefix(p, 6) == sha256_file(p) and sha256_prefix(p, 99) == sha256_file(p)


def test_verify_clean_world(world, pushed):
    res = verify(TEST, "m1", dest=str(pushed), **_kw(world))
    assert res.ok and res.drift == [] and res.bad_stamps == [] and res.first_bad is None
    assert res.reason is None and res.problems == []
    n = len(load_manifest(_paths(world), "m1").files)
    # the remote_copy verified in place
    assert res.copies == {"ok": n, "missing": 0, "mismatch": 0, "absent": 0}
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.copies == res.copies and row.drift == 0 and row.bad_stamps == 0
    assert row.dest == str(pushed) and row.first_bad is None
    offline = verify(TEST, "m1", **_kw(world))
    assert offline.ok and offline.copies is None and offline.copy_problems == []
    assert BackupLedger(_paths(world).backup_log).latest("verify", "m1").dest is None
    with pytest.raises(ValidationFailed, match="not_found"):
        verify(TEST, "nope", **_kw(world))


def test_verify_records_a_row_when_the_copies_layer_fails(world):
    """The copies layer dying (rclone flaked) must not lose the consistency / timestamp results
    that already ran -- the row still lands, with `copies=None` and the failure's own message."""
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    with pytest.raises(PlatformError, match="hashsum failed"):
        verify(TEST, "m1", dest="fake:vault", runner=FakeRemote(fail="hashsum"), **_kw(world))
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.error is not None and row.error.startswith("rclone hashsum failed")
    assert row.copies is None and row.tier == 3
    assert row.drift == 0 and row.bad_stamps == 0
    assert SECRET not in row.error


def test_verify_tier_bounds_the_copies_layer(world):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=2, **_kw(world))
    m = load_manifest(_paths(world), "m1")
    res = verify(TEST, "m1", dest=str(vault), tier=2, **_kw(world))
    within = sum(1 for f in m.files if f.tier <= 2)
    assert res.ok and res.copies == {"ok": within, "missing": 0, "mismatch": 0, "absent": 0}
    assert BackupLedger(_paths(world).backup_log).latest("verify", "m1").tier == 2
    res = verify(TEST, "m1", dest=str(vault), **_kw(world))  # default tier 3: everything
    assert res.copies["missing"] == 1 and res.reason == "missing"
    assert res.copy_problems == ["missing:data/work/good/weights/last.pt"]
    assert BackupLedger(_paths(world).backup_log).latest("verify", "m1").tier == 3
    assert verify(TEST, "m1", **_kw(world)).copies is None
    assert BackupLedger(_paths(world).backup_log).latest("verify", "m1").tier is None
    with pytest.raises(ValidationFailed, match="tier"):
        verify(TEST, "m1", dest=str(vault), tier=0, **_kw(world))


def test_verify_copies_missing_and_mismatch(world, pushed):
    (pushed / "data" / "runs" / "good" / "run.yaml").unlink()
    (pushed / "configs" / "datasets" / TEST / "submit.yaml").write_text(
        "dataset: x\n", encoding="utf-8"
    )
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")
    res = verify(TEST, "m1", dest=str(pushed), **_kw(world))
    assert res.copies["missing"] == 1 and res.copies["mismatch"] == 2 and not res.ok
    assert sorted(res.copy_problems) == [
        f"mismatch:configs/datasets/{TEST}/submit.yaml",
        "mismatch:data/work/good/weights/best.pt",
        "missing:data/runs/good/run.yaml",
    ]
    assert res.reason == "mismatch" and res.drift == [] and res.bad_stamps == []
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.copies == {"ok": res.copies["ok"], "missing": 1, "mismatch": 2, "absent": 0}


def test_verify_absent_files_do_not_fail(world):
    (world.weights / "last.pt").unlink()  # listed with the sha the train record vouches for
    res = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    assert res.missing == ["data/work/good/weights/last.pt"]
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=3, **_kw(world))
    n = len(res.manifest.files)
    out = verify(TEST, "m1", dest=str(vault), **_kw(world))
    assert out.ok and out.copies == {"ok": n - 1, "missing": 0, "mismatch": 0, "absent": 1}
    stray = vault / "data" / "work" / "good" / "weights" / "last.pt"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"not last")
    out = verify(TEST, "m1", dest=str(vault), **_kw(world))
    assert out.copies["mismatch"] == 1 and out.copies["absent"] == 0 and not out.ok


def test_verify_consistency_drift(world, pushed):
    card = load_run(world.roots.data, "good")
    pred = _pred(world)
    pred.write_bytes(pred.read_bytes() + b'{"sample_id": "zzz", "scores": {"a": 1.0}}\n')
    res = verify(TEST, "m1", **_kw(world))
    assert sorted(d.what for d in res.drift) == [
        "data/runs/good/predictions/valB.jsonl",
        "good/run.yaml:predictions.valB",
    ]
    d = next(d for d in res.drift if d.what == "good/run.yaml:predictions.valB")
    assert d == Drift(d.what, card.predictions["valB"].sha256, sha256_file(pred))
    assert res.reason == "drift" and res.bad_stamps == [] and res.copies is None
    stage = _paths(world).submission_dir("S1") / "stage.json"
    doc = json.loads(stage.read_text(encoding="utf-8"))
    doc["artifact"]["sha256"] = "f" * 64
    stage.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    res = verify(TEST, "m1", **_kw(world))
    whats = {d.what for d in res.drift}
    assert f"submit/{TEST}/S1/stage.json:artifact" in whats
    assert f"datasets/{TEST}/submissions.jsonl:staged.S1" in whats
    assert f"data/submit/{TEST}/S1/stage.json" in whats


def test_verify_stamps(world, pushed):
    readings = _paths(world, EVAL).measure_dir / READINGS_LEDGER
    lines = readings.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    grown = {**last, "reading_id": "f" * 64, "ts": "2999-01-01T00:00:00.000Z"}
    readings.write_text(
        "\n".join([*lines, json.dumps(grown)]) + "\n", encoding="utf-8", newline="\n"
    )
    res = verify(TEST, "m1", **_kw(world))
    assert res.ok  # an append-only ledger that only grew is not drift
    stale = {**grown, "ts": "2000-01-01T00:00:00.000Z"}
    readings.write_text(
        "\n".join([*lines, json.dumps(grown), json.dumps(stale), "not json"]) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    res = verify(TEST, "m1", **_kw(world))
    n = len(lines)
    label = f"data/measure/{EVAL}/{READINGS_LEDGER}"
    assert res.drift == [] and res.bad_stamps == [f"{label}:{n + 2}", f"{label}:{n + 3}"]
    assert res.first_bad == f"{label}:{n + 2}" and res.reason == "bad_stamps" and not res.ok
    profile = _paths(world).submit_yaml
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(STAMP, "yesterday"),
        encoding="utf-8",
        newline="\n",
    )
    res = verify(TEST, "m1", **_kw(world))
    assert f"configs/datasets/{TEST}/submit.yaml:created_at" in res.bad_stamps
    assert any(d.what == f"configs/datasets/{TEST}/submit.yaml" for d in res.drift)
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.bad_stamps == len(res.bad_stamps) and row.first_bad == res.first_bad
    assert row.drift == len(res.drift)
    log = _paths(world).backup_log
    rows = log.read_text(encoding="utf-8").splitlines()
    bad = json.loads(rows[-1])
    bad["ts"] = "2000-01-01T00:00:00.000Z"
    log.write_text("\n".join([*rows[:-1], json.dumps(bad)]) + "\n", encoding="utf-8", newline="\n")
    res = verify(TEST, "m1", **_kw(world))
    assert f"backup.log.jsonl:{len(rows)}" in res.bad_stamps


def test_verify_remote_copy_on_rclone(world):
    rec = load_record(world.roots.data, "good")
    best = next(c for c in rec.checkpoints if c.final)
    rec = merge_uploads(
        rec,
        [
            UploadRecord(
                dest="fake:w",
                kind="rclone",
                name="best.pt",
                sha256=best.sha256,
                verified=True,
                uploaded_at="2026-09-06T00:00:00.000Z",
            )
        ],
    )
    save_record(world.roots.data, rec)
    build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    m = load_manifest(_paths(world, EVAL), "mg")
    entry = next(f for f in m.files if f.kind == "remote_copy")
    assert entry.remote.dest == "fake:w"  # the newest verified upload wins
    remote = FakeRemote()
    remote.store["fake:w/good/best.pt"] = (world.weights / "best.pt").read_bytes()
    res = verify(EVAL, "mg", dest="fake:vault", runner=remote, **_kw(world))
    assert res.copies == {"ok": 1, "missing": len(m.files) - 1, "mismatch": 0, "absent": 0}
    assert ["rclone", "hashsum", "sha256", "fake:w/good"] in remote.calls
    assert res.reason == "missing"
    assert SECRET not in json.dumps(res.copy_problems)
