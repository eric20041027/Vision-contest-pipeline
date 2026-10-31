from pathlib import Path

import pytest

from vcp.core.errors import ValidationFailed, VcpError
from vcp.train import disk
from vcp.train.disk import (
    GIB,
    assert_room,
    check_min_free_gib,
    disk_free_total,
    free_gib,
    glob_prefix,
    lowest,
    measure,
    nearest_dir,
    targets,
    unavailable,
)
from vcp.train.schema import DiskCheck, DiskVolume


def _volume(free, *, path="runs", roles=("run",), total=100 * GIB):
    return DiskVolume(path=path, roles=list(roles), free_bytes=free, total_bytes=total)


def _check(*volumes, min_free_gib=10.0):
    return DiskCheck(
        min_free_gib=min_free_gib, checked_at="2026-10-30T00:00:00.000Z", volumes=list(volumes)
    )


def test_glob_prefix_stops_at_the_first_wildcard():
    assert glob_prefix("weights/*.pt") == "weights/"
    assert glob_prefix("runs/**/best.pt") == "runs/"
    assert glob_prefix("ck[0-9]/x") == "ck"
    assert glob_prefix("w?/x") == "w"
    assert glob_prefix("weights/best.pt") == "weights/best.pt"
    assert glob_prefix("*.pt") == ""


def test_nearest_dir_climbs_to_an_existing_directory(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.pt").write_bytes(b"x")
    assert nearest_dir(tmp_path / "a" / "b" / "c.pt") == tmp_path / "a"
    assert nearest_dir(tmp_path / "a" / "f.pt") == tmp_path / "a"
    assert nearest_dir(tmp_path) == tmp_path


def test_an_absolute_or_recursive_glob_is_measured_where_it_points(tmp_path):
    """Review focus 1: a checkpoint glob may leave --cwd (an absolute path) or only start below
    it (``sub/**``); the volume that matters is where the child writes, not where it starts."""
    cwd = tmp_path / "cwd"
    other = tmp_path / "other"
    (cwd / "sub").mkdir(parents=True)
    other.mkdir()
    got = targets(tmp_path / "runs" / "r1", cwd, [f"{other.as_posix()}/**/*.pt", "sub/**/x.pt"])
    assert [role for role, _ in got] == ["run", "cwd", "checkpoints", "checkpoints"]
    assert got[0][1] == tmp_path / "runs" / "r1" and got[1][1] == cwd
    assert nearest_dir(got[2][1]) == other
    assert nearest_dir(got[3][1]) == cwd / "sub"


def test_a_run_without_checkpoint_globs_has_only_the_run_and_cwd_targets(tmp_path):
    got = targets(tmp_path / "runs" / "r1", tmp_path, [])
    assert [role for role, _ in got] == ["run", "cwd"]


def _fake_devices(monkeypatch, work):
    """Everything under ``work/ckpt`` is on device 2, the rest on device 1; ``usage`` is logged."""
    ckpt = work / "ckpt"
    monkeypatch.setattr(disk, "device", lambda p: 2 if Path(p) == ckpt else 1)
    calls = []

    def fake_usage(p):
        calls.append(Path(p))
        return (5 * GIB, 100 * GIB) if Path(p) == ckpt else (50 * GIB, 100 * GIB)

    monkeypatch.setattr(disk, "usage", fake_usage)
    return calls


def test_measure_groups_by_device_and_takes_one_reading_per_volume(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "ckpt").mkdir(parents=True)
    calls = _fake_devices(monkeypatch, work)
    check = measure(
        [("run", tmp_path / "runs" / "r1"), ("cwd", work), ("checkpoints", work / "ckpt")],
        min_free_gib=10.0,
        data_root=tmp_path,
    )
    assert [v.roles for v in check.volumes] == [["run", "cwd"], ["checkpoints"]]
    assert len(calls) == 2
    assert lowest(check).free_bytes == 5 * GIB
    assert free_gib(check) == 5.0
    assert not unavailable(check)
    assert check.min_free_gib == 10.0 and check.checked_at.endswith("Z")
    assert [v.total_bytes for v in check.volumes] == [100 * GIB, 100 * GIB]


def test_measure_keeps_first_seen_order_and_one_role_once_per_volume(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "ckpt").mkdir(parents=True)
    _fake_devices(monkeypatch, work)
    check = measure(
        [
            ("checkpoints", work / "ckpt"),
            ("run", tmp_path / "runs" / "r1"),
            ("checkpoints", work / "ckpt"),
            ("cwd", work),
            ("checkpoints", work),
        ],
        min_free_gib=10.0,
        data_root=tmp_path,
    )
    assert [v.roles for v in check.volumes] == [["checkpoints"], ["run", "cwd", "checkpoints"]]


def test_a_volume_path_is_stored_the_way_cards_store_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "runs").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "ckpt").mkdir(parents=True)
    _fake_devices(monkeypatch, work)
    check = measure(
        [("run", data / "runs" / "r1"), ("cwd", work), ("checkpoints", work / "ckpt")],
        min_free_gib=10.0,
        data_root=data,
    )
    assert check.volumes[0].path == "runs"  # inside the data root: relative, posix
    assert check.volumes[1].path == (work / "ckpt").resolve().as_posix()  # elsewhere: absolute


def test_an_unreadable_volume_is_null_and_all_unreadable_is_unknown(tmp_path, monkeypatch):
    def broken(path):
        raise OSError("probe failed")

    monkeypatch.setattr(disk, "usage", broken)
    check = measure(
        [("run", tmp_path / "runs" / "r1"), ("cwd", tmp_path)],
        min_free_gib=10.0,
        data_root=tmp_path,
    )
    assert check.volumes[0].free_bytes is None and check.volumes[0].total_bytes is None
    assert unavailable(check) and free_gib(check) == "unknown" and lowest(check) is None


def test_one_unreadable_volume_among_readable_ones_is_still_unavailable(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "ckpt").mkdir(parents=True)
    monkeypatch.setattr(disk, "device", lambda p: 2 if Path(p) == work / "ckpt" else 1)

    def half_broken(path):
        if Path(path) == work / "ckpt":
            raise OSError("probe failed")
        return (50 * GIB, 100 * GIB)

    monkeypatch.setattr(disk, "usage", half_broken)
    check = measure(
        [("run", tmp_path / "runs" / "r1"), ("cwd", work), ("checkpoints", work / "ckpt")],
        min_free_gib=10.0,
        data_root=tmp_path,
    )
    assert unavailable(check) and free_gib(check) == 50.0  # the readable volume still counts
    assert lowest(check).roles == ["run", "cwd"]


def test_a_path_whose_device_cannot_be_read_is_its_own_volume(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()

    def broken(path):
        raise OSError("stat failed")

    monkeypatch.setattr(disk, "device", broken)
    check = measure(
        [("run", tmp_path / "runs" / "r1"), ("cwd", work)], min_free_gib=10.0, data_root=tmp_path
    )
    assert [v.roles for v in check.volumes] == [["run"], ["cwd"]]
    assert not unavailable(check)  # usage still answered


def test_free_gib_is_the_smallest_reading_to_one_decimal():
    assert free_gib(_check(_volume(2 * GIB + GIB // 4), _volume(30 * GIB))) == 2.2
    assert free_gib(_check(_volume(None, total=None), _volume(30 * GIB))) == 30.0


def test_assert_room_aborts_below_the_bar_and_never_at_zero():
    low = _check(_volume(2 * GIB), min_free_gib=10.0)
    with pytest.raises(VcpError, match="^low_disk:") as ei:
        assert_room(low)
    assert ei.value.status == "ABORT" and "--min-free-gib" in str(ei.value)
    assert ei.value.fields == {"free_gib": 2.0, "min_free_gib": 10.0, "disk_path": "runs"}
    assert "2.0 GiB free on runs, below --min-free-gib 10;" in str(ei.value)
    assert_room(_check(_volume(2 * GIB), min_free_gib=0.0))  # 0: record, never abort
    assert_room(_check(_volume(None, total=None), min_free_gib=10.0))  # unreadable: never abort
    assert_room(_check(_volume(10 * GIB), min_free_gib=10.0))  # exactly at the bar is enough


def test_assert_room_names_the_volume_with_the_least_room():
    check = _check(_volume(50 * GIB, path="data"), _volume(3 * GIB, path="D:/ckpt"))
    with pytest.raises(VcpError, match="^low_disk:") as ei:
        assert_room(check)
    assert ei.value.fields["disk_path"] == "D:/ckpt" and ei.value.fields["free_gib"] == 3.0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_min_free_gib_must_be_finite_and_not_negative(bad):
    with pytest.raises(ValidationFailed, match="^invalid: --min-free-gib") as ei:
        check_min_free_gib(bad)
    assert ei.value.status == "FAIL" and "finite number >= 0" in str(ei.value)


@pytest.mark.parametrize("good", [0.0, 0.5, 10.0, 4096.0])
def test_min_free_gib_accepts_zero_and_up(good):
    check_min_free_gib(good)


def test_the_real_probe_reads_this_disk(tmp_path):
    free, total = disk_free_total(tmp_path)
    assert total > 0 and 0 <= free <= total


def test_the_real_device_of_a_directory_is_an_int(tmp_path):
    assert isinstance(disk.device(tmp_path), int)
