import pytest

from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import (
    default_manifest_id,
    load_manifest,
    local_path,
    write_manifest,
)
from vcp.backup.schema import BackupRow, FileEntry, Manifest
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths

T0 = "2026-09-06T00:00:00.000Z"
T1 = "2026-09-06T01:00:00.000Z"


def _manifest(mid="run-r1-20260906T000000Z") -> Manifest:
    return Manifest(
        manifest_id=mid,
        dataset="d",
        conclusion="run:r1",
        created_at=T0,
        vcp_version="0",
        data_root="C:/vcp-data",
        files=[
            FileEntry(
                root="data",
                path="runs/r1/run.yaml",
                sha256="a" * 64,
                bytes=3,
                role="run_card",
                tier=1,
                for_=["run:r1"],
            ),
            FileEntry(
                root="configs",
                path="datasets/d/dataset.yaml",
                sha256="b" * 64,
                bytes=4,
                role="dataset_card",
                tier=1,
                for_=["run:r1"],
            ),
            FileEntry(
                root="external",
                path="D/w/best.pt",
                source="D:/w/best.pt",
                sha256="c" * 64,
                bytes=5,
                role="checkpoint",
                tier=3,
                for_=["run:r1"],
            ),
        ],
    )


def test_ledger_round_trip_and_latest(tmp_path):
    led = BackupLedger(tmp_path / "backup.log.jsonl")
    assert led.rows == [] and led.manifest_ids() == []
    led.append(
        BackupRow(
            event="manifest",
            ts=T0,
            manifest_id="m1",
            conclusion="all",
            files=1,
            bytes_by_tier={"1": 1, "2": 0, "3": 0},
            missing=0,
            remote_copies=0,
        )
    )
    led.append(
        BackupRow(
            event="push",
            ts=T0,
            manifest_id="m1",
            dest="v",
            tier=1,
            pushed=1,
            skipped=0,
            verified=1,
            failed=[],
            bytes=1,
        )
    )
    led.append(
        BackupRow(
            event="push",
            ts=T1,
            manifest_id="m1",
            dest="v",
            tier=2,
            pushed=0,
            skipped=1,
            verified=1,
            failed=[],
            bytes=0,
        )
    )
    text = (tmp_path / "backup.log.jsonl").read_text(encoding="utf-8")
    assert "null" not in text and text.count("\n") == 3
    again = BackupLedger(tmp_path / "backup.log.jsonl")
    assert again.rows == led.rows and again.manifest_ids() == ["m1"]
    assert again.latest("push", "m1").tier == 2 and again.latest("verify", "m1") is None
    assert len(again.of("push")) == 2 and again.of("push", "m9") == []


def test_manifest_write_load_and_paths(roots):
    paths = DatasetPaths.resolve("d", data_root=roots.data, configs_root=roots.configs)
    m = _manifest()
    path = write_manifest(paths, m)
    assert path == paths.backup_manifest(m.manifest_id)
    text = path.read_text(encoding="utf-8")
    assert '"for": [' in text and "for_" not in text and ("\r" not in path.read_bytes().decode())
    assert load_manifest(paths, m.manifest_id) == m
    with pytest.raises(ValidationFailed, match="exists"):
        write_manifest(paths, m)
    with pytest.raises(ValidationFailed, match="not_found"):
        load_manifest(paths, "nope")
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad manifest"):
        load_manifest(paths, m.manifest_id)
    d, c, x = m.files
    assert local_path(d, roots.data, roots.configs) == (roots.data / "runs" / "r1" / "run.yaml")
    assert local_path(c, roots.data, roots.configs) == (
        roots.configs / "datasets" / "d" / "dataset.yaml"
    )
    assert local_path(x, roots.data, roots.configs).as_posix() == "D:/w/best.pt"


def test_default_manifest_id():
    mid = default_manifest_id("submission:SUB34")
    assert (
        mid.startswith("submission-SUB34-")
        and mid.endswith("Z")
        and len(mid) == len("submission-SUB34-20260906T000000Z")
    )
    assert default_manifest_id("all").startswith("all-")
