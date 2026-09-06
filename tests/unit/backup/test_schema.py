import pytest
from pydantic import ValidationError

from vcp.backup.schema import (
    CARD_ROLES,
    LEDGER_ROLES,
    ROLES,
    TIER_OF,
    BackupRow,
    FileEntry,
    Manifest,
    RemoteCopy,
)
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths

STAMP = "2026-09-06T00:00:00.000Z"


def _entry(**over) -> FileEntry:
    base = {
        "root": "data",
        "path": "runs/r1/run.yaml",
        "sha256": "a" * 64,
        "bytes": 10,
        "role": "run_card",
        "tier": 1,
        "for": ["run:r1"],
    }
    return FileEntry.model_validate({**base, **over})


def test_roles_and_tiers():
    assert ROLES[0] == "submit_profile" and ROLES[-1] == "checkpoint"
    assert TIER_OF["run_card"] == 1 and TIER_OF["prediction"] == 2
    assert TIER_OF["checkpoint_final"] == 3 and TIER_OF["checkpoint"] == 3
    assert ROLES.index("checkpoint_final") < ROLES.index("checkpoint")
    assert "readings" in LEDGER_ROLES and "logs" in LEDGER_ROLES
    assert "run_card" in CARD_ROLES and "anchors" in CARD_ROLES
    assert set(TIER_OF) == set(ROLES)


def test_entry_alias_and_key():
    e = _entry()
    assert e.for_ == ["run:r1"] and e.key == "data/runs/r1/run.yaml"
    assert FileEntry(
        root="data",
        path="x",
        sha256="b" * 64,
        bytes=1,
        role="logs",
        tier=2,
        for_=["all"],
    ).for_ == ["all"]
    dumped = e.model_dump(mode="json", by_alias=True)
    assert dumped["for"] == ["run:r1"] and "for_" not in dumped


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "photo"},
        {"tier": 2},
        {"kind": "remote_copy"},
        {"remote": {"dest": "d", "run": "r1", "name": "best.pt"}},
        {"root": "external"},
        {"source": "C:/x"},
        {"for": []},
        {"extra": 1},
    ],
)
def test_entry_rejects(bad):
    with pytest.raises(ValidationError):
        _entry(**bad)


@pytest.mark.parametrize("path", ["../x", "a/../b", "/abs", "C:/x", "a\\b", "", "./a", "a//b"])
def test_entry_rejects_paths_that_can_leave_the_root(path):
    with pytest.raises(ValidationError):
        _entry(path=path)


def test_external_entry_needs_an_absolute_source():
    with pytest.raises(ValidationError):
        _entry(root="external", source="rel/x")


def test_remote_copy_and_external_entries():
    r = _entry(
        role="checkpoint",
        tier=3,
        kind="remote_copy",
        remote={"dest": "gdrive:w", "run": "r1", "name": "best.pt"},
        path="work/best.pt",
    )
    assert r.remote == RemoteCopy(dest="gdrive:w", run="r1", name="best.pt")
    x = _entry(
        root="external",
        path="D/weights/best.pt",
        source="D:/weights/best.pt",
        role="checkpoint",
        tier=3,
    )
    assert x.key == "external/D/weights/best.pt"


def test_manifest_uniqueness_and_bytes():
    m = Manifest(
        manifest_id="run-r1-20260906T000000Z",
        dataset="d",
        conclusion="run:r1",
        created_at=STAMP,
        vcp_version="0",
        data_root="C:/vcp-data",
        files=[
            _entry(),
            _entry(
                path="runs/r1/predictions/valA.jsonl",
                role="prediction",
                tier=2,
                bytes=5,
            ),
        ],
    )
    assert m.bytes_by_tier() == {"1": 10, "2": 5, "3": 0}
    with pytest.raises(ValidationError, match="duplicate"):
        Manifest(**{**m.model_dump(by_alias=True), "files": [_entry(), _entry()]})


def test_backup_rows_require_event_fields():
    BackupRow(
        event="manifest",
        ts=STAMP,
        manifest_id="m",
        conclusion="all",
        files=3,
        bytes_by_tier={"1": 1, "2": 0, "3": 0},
        missing=0,
        remote_copies=0,
    )
    with pytest.raises(ValidationError, match="push needs"):
        BackupRow(event="push", ts=STAMP, manifest_id="m")
    BackupRow(event="verify", ts=STAMP, manifest_id="m", drift=0, bad_stamps=0)
    BackupRow(event="remote_forgotten", ts=STAMP, manifest_id="m", remote="gdrive")
    with pytest.raises(ValidationError):
        BackupRow(event="party", ts=STAMP)


def test_paths(roots):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    assert paths.backup_dir == roots.configs / "datasets" / "t" / "backup"
    assert paths.backup_log == roots.configs / "datasets" / "t" / "backup.log.jsonl"
    assert paths.backup_manifest("m1") == paths.backup_dir / "m1.json"
    with pytest.raises(ValidationFailed):
        paths.backup_manifest("bad id")
