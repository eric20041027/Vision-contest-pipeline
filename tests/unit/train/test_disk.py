import errno
from pathlib import Path

import pytest

from vcp.core.errors import ValidationFailed, VcpError
from vcp.train import disk
from vcp.train.disk import (
    DISK_FULL_ERRNOS,
    DISK_FULL_MARKERS,
    DISK_FULL_TAIL,
    DISK_FULL_WINERRORS,
    GIB,
    DiskFailure,
    assert_room,
    check_min_free_gib,
    console_marker,
    diagnose,
    disk_free_total,
    free_gib,
    glob_prefix,
    is_disk_full,
    lowest,
    measure,
    nearest_dir,
    own_writes,
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


def test_a_reading_just_under_the_bar_never_prints_as_the_bar():
    """9.96 GiB against a bar of 10 reads 9.9, not 10.0: the message must not claim the bar is
    unmet while showing a number that meets it."""
    check = _check(_volume(int(9.96 * GIB)), min_free_gib=10.0)
    with pytest.raises(VcpError, match="^low_disk:") as ei:
        assert_room(check)
    assert ei.value.fields["free_gib"] == 9.9 and "9.9 GiB free" in str(ei.value)
    assert free_gib(check) == 9.9


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


def _log(tmp_path, text, name="console.log"):
    return _log_bytes(tmp_path, text.encode("utf-8"), name)


def _log_bytes(tmp_path, data, name="console.log"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _after(free, min_free=10.0):
    """The reading taken after a failed attempt: one volume with ``free`` bytes left (None =
    the probe failed)."""
    return _check(
        _volume(free, path=".", total=None if free is None else 100 * GIB), min_free_gib=min_free
    )


def test_the_constants_are_the_ones_the_spec_names():
    assert DISK_FULL_TAIL == 64 * 1024 == 65536
    assert DISK_FULL_MARKERS == (
        "No space left on device",
        "[Errno 28]",
        "[WinError 112]",
        "[WinError 39]",
        "There is not enough space on the disk",
        "Disk quota exceeded",
    )
    assert DISK_FULL_ERRNOS == {errno.ENOSPC, errno.EDQUOT}
    assert DISK_FULL_WINERRORS == {112, 39}


@pytest.mark.parametrize(
    ("line", "marker"),
    [
        ("OSError: [Errno 28] No space left on device", "No space left on device"),
        ("RuntimeError: [errno 28] write failed", "[Errno 28]"),
        ("OSError: [WinError 112] 磁碟空間不足。", "[WinError 112]"),
        ("OSError: [WinError 39] handle disk full", "[WinError 39]"),
        ("there is not enough space on the disk", "There is not enough space on the disk"),
        ("DISK QUOTA EXCEEDED", "Disk quota exceeded"),
    ],
)
def test_console_marker_finds_each_word_in_any_case(tmp_path, line, marker):
    assert console_marker(_log(tmp_path, f"epoch 3\n{line}\n")) == marker
    assert console_marker(tmp_path / "missing.log") is None


def test_console_marker_answers_none_for_a_console_without_a_word(tmp_path):
    assert console_marker(_log(tmp_path, "epoch 3\nterminate called: ios_base::badbit\n")) is None
    assert console_marker(_log(tmp_path, "")) is None


def test_console_marker_takes_the_first_word_of_the_list_not_the_earliest_in_the_text(tmp_path):
    text = "[WinError 112] first in the text\nNo space left on device\n"
    assert console_marker(_log(tmp_path, text)) == "No space left on device"


def test_console_marker_reads_only_the_last_64_kib(tmp_path):
    word = b"No space left on device\n"
    assert console_marker(_log_bytes(tmp_path, word + b"x" * DISK_FULL_TAIL)) is None
    # the word ends exactly at the start of the window: still outside it
    assert console_marker(_log_bytes(tmp_path, word + b"x" * (DISK_FULL_TAIL - 1))) is None
    assert console_marker(_log_bytes(tmp_path, word + b"x" * DISK_FULL_TAIL + b"\n" + word)) == (
        "No space left on device"
    )


def test_a_tail_that_starts_inside_a_multibyte_character_still_reads(tmp_path):
    body = ("中" * 30000).encode() + b"\n[Errno 28] write\n"
    assert (len(body) - DISK_FULL_TAIL) % 3 != 0
    assert console_marker(_log_bytes(tmp_path, body)) == "[Errno 28]"


def test_diagnose_prefers_the_console_then_low_disk_then_nothing(tmp_path):
    full = _log(tmp_path, "epoch 3\nOSError: [Errno 28] No space left on device\n", "full.log")
    badbit = _log(tmp_path, "terminate called: ios_base::badbit\n", "badbit.log")
    assert diagnose(1, full, _after(2 * GIB)) == DiskFailure(
        key="disk_full",
        marker="No space left on device",
        free_bytes=2 * GIB,
        evidence="console",
        reason='disk_full: the command exited 1 and its console says "No space left on device"',
    )
    low = diagnose(1, badbit, _after(2 * GIB))
    assert (low.key, low.marker, low.evidence, low.free_bytes) == ("low_disk", None, None, 2 * GIB)
    assert low.reason == (
        "low_disk: the command exited 1 with 2.0 GiB free "
        "(below --min-free-gib 10); the disk may have filled"
    )
    assert diagnose(1, badbit, _after(50 * GIB)) is None
    assert diagnose(1, badbit, _after(2 * GIB, min_free=0.0)) is None
    assert diagnose(1, badbit, _after(None)) is None


def test_diagnose_reports_the_exit_code_and_a_fractional_bar(tmp_path):
    badbit = _log(tmp_path, "ios_base::badbit\n", "badbit.log")
    low = diagnose(137, badbit, _after(GIB * 3 // 2, min_free=2.5))
    assert low.reason == (
        "low_disk: the command exited 137 with 1.5 GiB free "
        "(below --min-free-gib 2.5); the disk may have filled"
    )


def test_diagnose_floors_a_reading_just_under_the_bar(tmp_path):
    badbit = _log(tmp_path, "ios_base::badbit\n", "badbit.log")
    low = diagnose(1, badbit, _after(int(9.96 * GIB)))
    assert low.key == "low_disk" and "9.9 GiB free" in low.reason
    assert "10.0 GiB free" not in low.reason


def test_diagnose_exactly_at_the_bar_is_not_low(tmp_path):
    badbit = _log(tmp_path, "ios_base::badbit\n", "badbit.log")
    assert diagnose(1, badbit, _after(10 * GIB)) is None
    assert diagnose(1, badbit, _after(10 * GIB - 1)).key == "low_disk"


def test_a_console_word_is_reported_even_when_the_free_space_is_unknown(tmp_path):
    full = _log(tmp_path, "No space left on device\n", "full.log")
    got = diagnose(1, full, _after(None))
    assert got.key == "disk_full" and got.free_bytes is None and got.evidence == "console"


def test_diagnose_reads_the_volume_with_the_least_room(tmp_path):
    badbit = _log(tmp_path, "ios_base::badbit\n", "badbit.log")
    two = _check(_volume(50 * GIB, path="data"), _volume(3 * GIB, path="D:/ckpt"))
    got = diagnose(1, badbit, two)
    assert got.key == "low_disk" and got.free_bytes == 3 * GIB and "3.0 GiB free" in got.reason


class Win112(OSError):
    winerror = 112


class Win39(OSError):
    winerror = 39


def test_is_disk_full_knows_enospc_edquot_and_the_two_winerrors():
    assert is_disk_full(OSError(errno.ENOSPC, "x")) and is_disk_full(OSError(errno.EDQUOT, "x"))
    assert is_disk_full(Win112(errno.EINVAL, "x")) and is_disk_full(Win39(errno.EINVAL, "x"))
    assert not is_disk_full(OSError(errno.EACCES, "x"))
    assert not is_disk_full(OSError("no errno at all"))


def test_own_writes_turns_disk_full_into_an_abort_and_lets_others_through():
    with pytest.raises(VcpError, match="^disk_full: vcp could not write") as ei:
        with own_writes():
            raise OSError(errno.ENOSPC, "No space left on device")
    assert ei.value.status == "ABORT" and ei.value.fields == {"disk_evidence": "vcp"}
    assert "; free space, then --resume" in str(ei.value)
    assert isinstance(ei.value.__cause__, OSError)  # the original stays on the chain
    with pytest.raises(PermissionError, match="denied"):
        with own_writes():
            raise PermissionError(errno.EACCES, "denied")
    with pytest.raises(ValueError, match="not an OSError"):
        with own_writes():
            raise ValueError("not an OSError")
    with own_writes():
        pass


def test_own_writes_hint_replaces_the_recovery_step_in_the_message():
    hint = "free space, then: vcp train upload --run r1 --dest vault"
    with pytest.raises(VcpError, match="^disk_full: vcp could not write") as ei:
        with own_writes(hint=hint):
            raise OSError(errno.ENOSPC, "No space left on device")
    assert str(ei.value).endswith(f"); {hint}") and "--resume" not in str(ei.value)
    assert ei.value.fields == {"disk_evidence": "vcp"}
