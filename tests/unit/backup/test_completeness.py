"""VCP-045 (spec 2026-10-04 §4, §9): a manifest is complete when it lists every checkpoint its
runs had registered -- and every evidence or label set they had attached -- by the time it was
written. verify, status, a tier-3 push, --forget-remote and the manifest's own self-check all
judge any manifest by that one rule, whatever its conclusion."""

import json
import time

import pytest
from typer.testing import CliRunner

from backup_fixtures import FOLDS, FakeRemote, register_folds, write_old_manifest
from helpers import make_label_set
from submit_fixtures import EVAL, TEST
from vcp.backup import dest as destmod
from vcp.backup import evidence
from vcp.backup.completeness import checkable, manifest_gaps
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import entry_key
from vcp.backup.push import push
from vcp.backup.schema import BackupRow
from vcp.backup.status import local_ok, status
from vcp.backup.verify import verify
from vcp.cli import app
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.evidence import RunScope, label_ref
from vcp.data.split import load_plan
from vcp.measure.runs import load_run, save_run
from vcp.train.checkpoints import register
from vcp.train.records import load_record, save_record, train_dir, train_yaml

runner = CliRunner()


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world, name=EVAL):
    return DatasetPaths.resolve(name, **_kw(world))


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _old_091(world, dataset=EVAL, conclusion="run:good"):
    """Five folds that each wrote model.pt, and the manifest vcp 0.9.1 wrote of them: fold 4."""
    register_folds(world)
    return write_old_manifest(world, dataset, conclusion, "old-091", drop=set(FOLDS[:4]))


def _register(world, files):
    record, _ = register(
        load_record(world.roots.data, "good"), files, data_root=world.roots.data, attempt=3
    )
    save_record(world.roots.data, record)


# --- the rule ---------------------------------------------------------------------------------


def test_a_manifest_written_the_091_way_lists_one_fold_and_is_incomplete(world):
    gaps = manifest_gaps(_old_091(world), _paths(world))
    assert [g.key for g in gaps] == FOLDS[:4]
    assert gaps[0].run == "good"
    assert gaps[0].what == "good/train.yaml:checkpoints.work/good/fold-0/model.pt"


def test_a_manifest_built_today_of_five_folds_is_complete(world):
    register_folds(world)
    res = build_manifest(EVAL, "run:good", manifest_id="today", **_kw(world))
    assert set(FOLDS) <= {f.key for f in res.manifest.files}
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_checkpoints_registered_after_the_manifest_are_not_required(world):
    res = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    register_folds(world)  # the manifest is stale now -- drift says so -- not incomplete
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_a_re_registered_path_is_required_once(world):
    folds = register_folds(world, 1)
    folds[0].write_bytes(b"fold 0 weights, resumed")
    _register(world, folds)
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={FOLDS[0]})
    assert [g.key for g in manifest_gaps(old, _paths(world))] == [FOLDS[0]]


def test_a_checkpoint_listed_as_a_train_dir_file_counts_as_listed(world):
    inside = train_dir(world.roots.data, "good") / "ckpt" / "epoch3.pt"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"epoch 3")
    _register(world, [inside])
    res = build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    entry = {f.key: f for f in res.manifest.files}["data/runs/good/train/ckpt/epoch3.pt"]
    assert (entry.role, entry.tier) == ("train_dir", 2)
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_an_external_checkpoint_is_keyed_the_way_the_walk_keys_it(world, tmp_path):
    outside = tmp_path / "elsewhere" / "extra.pt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"extra")
    _register(world, [outside])
    key = entry_key(outside, world.roots.data, world.roots.configs)
    assert key.startswith("external/")
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={key})
    assert [g.key for g in manifest_gaps(old, _paths(world))] == [key]


@pytest.mark.parametrize(
    ("dataset", "conclusion"),
    [(EVAL, "run:good"), (EVAL, "judgement:p-good"), (TEST, "submission:S1"), (EVAL, "all")],
)
def test_gaps_are_found_whatever_the_conclusion(world, dataset, conclusion):
    old = _old_091(world, dataset, conclusion)
    assert [g.key for g in manifest_gaps(old, _paths(world, dataset))] == FOLDS[:4]


def test_evidence_attached_before_the_manifest_must_be_listed(world, tmp_path):
    make_label_set(world.roots, tmp_path, load_plan(_paths(world), "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    ref = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    save_run(world.roots.data, card.model_copy(update={"evidence": [ref]}))
    key = "data/artifacts/label_set/pseudo-v1/manifest.json"
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={key})
    what = "good/run.yaml:evidence.label_set/pseudo-v1"
    assert [(g.key, g.what) for g in manifest_gaps(old, _paths(world))] == [(key, what)]


def test_an_unreadable_train_yaml_fails_as_in_the_consistency_layer(world):
    res = build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    train_yaml(world.roots.data, "good").write_bytes(b"run_id: [unclosed\n")
    with pytest.raises(ValidationFailed, match="invalid YAML"):
        manifest_gaps(res.manifest, _paths(world))


# --- verify -----------------------------------------------------------------------------------


def test_verify_reports_manifest_incomplete_with_its_count(world):
    _old_091(world)
    res = verify(EVAL, "old-091", **_kw(world))
    assert not res.ok and res.reason == "manifest_incomplete" and len(res.incomplete) == 4
    first = "manifest_incomplete:good/train.yaml:checkpoints.work/good/fold-0/model.pt"
    assert res.problems[0] == first
    assert BackupLedger(_paths(world).backup_log).latest("verify", "old-091").incomplete == 4
    result = runner.invoke(app, ["backup", "verify", "--dataset", EVAL, "--manifest", "old-091"])
    verdict = _verdict(result.output)
    assert result.exit_code == 1 and "reason=manifest_incomplete" in verdict
    assert "incomplete=4" in verdict and "drift=0" in verdict


def test_the_verify_row_of_a_complete_manifest_is_written_as_before(world):
    build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    res = verify(EVAL, "m", **_kw(world))
    assert res.ok and res.incomplete == []
    row = _paths(world).backup_log.read_text(encoding="utf-8").splitlines()[-1]
    assert set(json.loads(row)) == {"event", "ts", "manifest_id", "drift", "bad_stamps"}
    result = runner.invoke(app, ["backup", "verify", "--dataset", EVAL, "--manifest", "m"])
    assert result.exit_code == 0 and "incomplete=0" in _verdict(result.output)


# --- status -----------------------------------------------------------------------------------


def _row_012_wrote(world, manifest_id):
    """A passing verify row of vcp 0.12: copies checked at a destination, no ``incomplete``."""
    BackupLedger(_paths(world).backup_log).append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=str(world.tmp / "vault"),
            tier=3,
            copies={"ok": 9, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
        )
    )


def test_status_recomputes_so_an_old_passing_row_cannot_vouch(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no real rclone
    _old_091(world)
    _row_012_wrote(world, "old-091")
    [m] = status(EVAL, **_kw(world)).manifests
    assert (m.verified, m.local_ok) == (False, False)
    assert (m.incomplete, m.completeness) == (4, "incomplete")
    result = runner.invoke(app, ["backup", "status", "--dataset", EVAL])
    verdict = _verdict(result.output)
    assert result.exit_code == 0 and "status=WARN" in verdict and "incomplete=1" in verdict
    note = "manifest_incomplete: these list fewer files than their runs registered: old-091"
    assert note in result.output


def test_status_without_the_run_records_falls_back_to_the_rows(world):
    _old_091(world)
    verify(EVAL, "old-091", **_kw(world))  # its row records incomplete=4
    train_yaml(world.roots.data, "good").unlink()  # a new machine, before a pull
    [m] = status(EVAL, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world)).manifests
    assert (m.incomplete, m.verified) == (4, False)


def test_status_without_the_run_records_cannot_check_a_manifest_older_than_010(world):
    write_old_manifest(world, EVAL, "run:good", "old", drop=set(), version="0.9.1")
    write_old_manifest(world, EVAL, "run:good", "new", drop=set(), version="0.10.0")
    train_yaml(world.roots.data, "good").unlink()
    view = status(EVAL, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world))
    by_id = {m.manifest_id: m for m in view.manifests}
    assert by_id["old"].completeness == "unchecked" and not by_id["old"].verified
    assert by_id["new"].completeness == "complete"
    assert view.unchecked == ["old"]


# --- push -------------------------------------------------------------------------------------


def test_a_tier_3_push_of_an_incomplete_manifest_fails_before_any_byte_moves(world):
    _old_091(world)
    vault = world.tmp / "vault"
    with pytest.raises(ValidationFailed, match="manifest_incomplete:") as ei:
        push(EVAL, "old-091", str(vault), tier=3, **_kw(world))
    assert ei.value.fields == {"incomplete": 4} and not vault.exists()
    assert BackupLedger(_paths(world).backup_log).of("push") == []
    for tier in (1, 2):  # the lower tiers hold no checkpoint: unaffected
        assert push(EVAL, "old-091", str(vault), tier=tier, **_kw(world)).failed == []


def test_forget_remote_refuses_an_incomplete_manifest(world):
    _old_091(world)
    remote = FakeRemote()
    with pytest.raises(ValidationFailed, match="forget_refused:") as ei:
        push(EVAL, "old-091", "fake:vault", tier=2, forget_remote=True, runner=remote, **_kw(world))
    assert ei.value.fields["incomplete"] == 4 and remote.deleted == []


# --- manifest ---------------------------------------------------------------------------------


def test_the_manifest_self_check_aborts_on_a_walk_that_drops_a_checkpoint(world, monkeypatch):
    monkeypatch.setattr(evidence.Collector, "_checkpoints", lambda self, record, conclusion: None)
    args = ["backup", "manifest", "--dataset", EVAL, "--conclusion", "run:good", "--id", "bug"]
    result = runner.invoke(app, args)
    verdict = _verdict(result.output)
    assert result.exit_code == 2 and "status=ABORT" in verdict, result.output
    assert "manifest_incomplete:" in verdict and "incomplete=2" in verdict
    assert not _paths(world).backup_manifest("bug").exists()
    assert BackupLedger(_paths(world).backup_log).of("manifest") == []


# --- pins beyond the brief (droppable): spec rules its tests leave unpinned ---------------------


def test_a_registration_stamp_that_does_not_parse_counts_as_registered_in_time(world):
    res = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    register_folds(world, 1)  # after the manifest, so not required -- until its stamp is edited
    record = load_record(world.roots.data, "good")
    *earlier, fold = record.checkpoints
    edited = [*earlier, fold.model_copy(update={"registered_at": "last tuesday"})]
    save_record(world.roots.data, record.model_copy(update={"checkpoints": edited}))
    assert [g.key for g in manifest_gaps(res.manifest, _paths(world))] == [FOLDS[0]]


def test_a_manifest_stamp_that_does_not_parse_requires_every_registration(world):
    res = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    register_folds(world, 1)  # registered after the manifest -- unless its own stamp was edited
    edited = res.manifest.model_copy(update={"created_at": "some day"})
    assert [g.key for g in manifest_gaps(edited, _paths(world))] == [FOLDS[0]]


def test_evidence_attached_after_the_manifest_is_not_required(world, tmp_path):
    res = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    make_label_set(world.roots, tmp_path, load_plan(_paths(world), "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    ref = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    save_run(world.roots.data, card.model_copy(update={"evidence": [ref]}))
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_a_run_record_that_is_not_on_this_machine_is_skipped(world):
    old = _old_091(world)
    train_yaml(world.roots.data, "good").unlink()  # a new machine, before a pull
    assert not checkable(old, _paths(world))
    assert manifest_gaps(old, _paths(world)) == []


def test_manifest_incomplete_leads_every_other_finding_of_verify(world):
    _old_091(world)
    (world.weights / "last.pt").write_bytes(b"changed since the manifest")  # drift as well
    res = verify(EVAL, "old-091", **_kw(world))
    assert res.reason == "manifest_incomplete"
    assert res.problems[0].startswith("manifest_incomplete:")
    assert any(p.startswith("drift:") for p in res.problems)


def test_status_cannot_check_a_manifest_it_cannot_read_or_date(world):
    write_old_manifest(world, EVAL, "run:good", "gone", drop=set(), version="0.12.0")
    write_old_manifest(world, EVAL, "run:good", "odd", drop=set(), version="not-a-version")
    _paths(world).backup_manifest("gone").unlink()
    train_yaml(world.roots.data, "good").unlink()
    view = status(EVAL, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world))
    by_id = {m.manifest_id: m.completeness for m in view.manifests}
    assert by_id == {"gone": "unchecked", "odd": "unchecked"}


def test_status_says_so_when_it_cannot_check_a_manifest(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no real rclone
    write_old_manifest(world, EVAL, "run:good", "old", drop=set(), version="0.9.1")
    train_yaml(world.roots.data, "good").unlink()
    result = runner.invoke(app, ["backup", "status", "--dataset", EVAL])
    verdict = _verdict(result.output)
    assert result.exit_code == 0 and "status=WARN" in verdict and "incomplete=0" in verdict
    assert "completeness=unchecked" in result.output
    assert "completeness unchecked (written before vcp 0.10.0" in result.output


def _json(result) -> dict:
    return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))


def test_the_json_payloads_name_the_gaps(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no real rclone
    _old_091(world)
    args = ["backup", "verify", "--dataset", EVAL, "--manifest", "old-091", "--json"]
    doc = _json(runner.invoke(app, args))
    assert doc["fields"]["incomplete"] == 4
    assert [g["key"] for g in doc["result"]["incomplete"]] == FOLDS[:4]
    assert doc["result"]["incomplete"][0] == {
        "run": "good",
        "what": "good/train.yaml:checkpoints.work/good/fold-0/model.pt",
        "key": FOLDS[0],
    }
    doc = _json(runner.invoke(app, ["backup", "status", "--dataset", EVAL, "--json"]))
    [m] = doc["result"]["manifests"]
    assert (m["completeness"], m["incomplete"]) == ("incomplete", 4)
    assert doc["fields"]["incomplete"] == 1


def test_a_verify_row_that_found_gaps_is_not_locally_ok():
    row = BackupRow(
        event="verify", ts=stamp(), manifest_id="m", drift=0, bad_stamps=0, incomplete=2
    )
    assert not local_ok(row)


def test_a_checkpoint_registered_while_the_walk_runs_is_not_the_manifests_to_list(
    world, monkeypatch
):
    walk = evidence.Collector.walk_run

    def racing(self, run_id, conclusion):
        walk(self, run_id, conclusion)  # train.yaml is read: the walk is over...
        time.sleep(0.01)
        register_folds(world, 1)  # ...and a framework registers one more weights file

    monkeypatch.setattr(evidence.Collector, "walk_run", racing)
    res = build_manifest(EVAL, "run:good", manifest_id="race", **_kw(world))  # no self-check ABORT
    assert FOLDS[0] not in {f.key for f in res.manifest.files}
    assert manifest_gaps(res.manifest, _paths(world)) == []
