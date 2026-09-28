"""Backup follows the ledger submit.yaml names (spec 2026-09-28 §3.1, §4.2). With ``ledger:
shared`` the walk lists ``data/submit/<test>/submissions.jsonl``, push and verify handle it
there, an adopted ledger runs forward in time, a row still being written is neither hashed nor
stamped, and the lock files never enter a manifest."""

import hashlib

import pytest

from submit_fixtures import TEST
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.push import push
from vcp.backup.verify import verify
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.submit.adopt import adopt
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow

SHARED = ("data", f"submit/{TEST}/submissions.jsonl")


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _to_shared(world) -> DatasetPaths:
    """The world's test profile says ``ledger: shared`` from now on (not adopted yet)."""
    paths = DatasetPaths.resolve(TEST, **_kw(world))
    profile, _ = load_profile(paths)
    dump_yaml_model(profile.model_copy(update={"ledger": "shared"}), paths.submit_yaml)
    return paths


def _ledgers(manifest):
    return [(f.root, f.path) for f in manifest.files if f.role == "submissions_log"]


def test_a_shared_ledger_is_backed_up_from_the_data_root(world):
    paths = _to_shared(world)
    adopt(TEST, **_kw(world))
    res = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    assert _ledgers(res.manifest) == [SHARED]
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=1, **_kw(world))
    copy = vault / "data" / "submit" / TEST / "submissions.jsonl"
    assert copy.read_bytes() == shared_ledger(paths).read_bytes()
    out = verify(TEST, "m1", dest=str(vault), tier=1, **_kw(world))
    assert out.ok, out.problems
    everything = build_manifest(TEST, "all", manifest_id="a1", **_kw(world))
    assert (world.roots.data / "locks").is_dir()
    assert not any(f.path.startswith("locks/") for f in everything.manifest.files)


def test_an_adopted_ledger_runs_forward_in_time_for_backup_verify(world):
    """Two sources interleaved: adopt sorts by ts, so the stamps layer has nothing to report."""
    paths = _to_shared(world)
    other = world.tmp / "other-checkout.jsonl"
    SubmissionLedger(other).append(
        LedgerRow(event="note", ts="2026-01-01T00:00:00.000Z", text="an older checkout")
    )
    res = adopt(TEST, sources=[paths.submissions_log, other], **_kw(world))
    assert [r.event for r in SubmissionLedger(res.path).rows] == ["note", "staged"]
    manifest = build_manifest(TEST, "submission:S1", manifest_id="m2", **_kw(world)).manifest
    assert _ledgers(manifest) == [SHARED]
    out = verify(TEST, "m2", **_kw(world))
    assert out.bad_stamps == [] and out.ok, out.problems


def test_a_row_still_being_written_is_left_out_of_the_manifest_and_the_stamps(world):
    paths = _to_shared(world)
    adopt(TEST, **_kw(world))
    whole = shared_ledger(paths).read_bytes()
    with shared_ledger(paths).open("ab") as f:
        f.write(b'{"event": "lock", "ts": "20')
    res = build_manifest(TEST, "submission:S1", manifest_id="m3", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.role == "submissions_log"]
    assert (entry.root, entry.path) == SHARED
    assert entry.bytes == len(whole) and entry.sha256 == hashlib.sha256(whole).hexdigest()
    out = verify(TEST, "m3", **_kw(world))
    assert out.ok, out.problems


def test_walk_all_steps_over_a_shared_ledger_not_adopted_yet(world):
    paths = _to_shared(world)
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_all(paths)
    assert [s.split(": ", 1)[0] for s in col.skipped] == ["submissions"]
    assert "not_adopted" in col.skipped[0]


def test_a_submission_conclusion_before_adopt_fails_not_adopted(world):
    """The ledger is the submission's evidence: with ``ledger: shared`` and nothing adopted yet
    there is no ledger to list, so the manifest is refused instead of written without it."""
    paths = _to_shared(world)
    with pytest.raises(ValidationFailed, match="not_adopted:") as ei:
        build_manifest(TEST, "submission:S1", manifest_id="m4", **_kw(world))
    assert ei.value.fields == {"ledger": "shared"}
    assert not paths.backup_manifest("m4").exists() and not paths.backup_log.exists()
