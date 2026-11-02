"""``vcp gc scan`` through the CLI (0.17.0 spec 5.1, 5.6-5.8 and 7): the human lines, ``--json``,
the VERDICT contract, the ``gc_scan`` artifact it leaves and that nothing else changes."""

from __future__ import annotations

import errno
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import vcp.gc.record as record_mod
import vcp.gc.scan as scan_mod
from backup_fixtures import make_world
from vcp.cli import app
from vcp.cli_gc import LAST_LINE, floor_gib, item_line, scan_cmd, size_text
from vcp.gc.layout import CLASSES
from vcp.gc.record import KIND, new_scan_id, result_json, scan_json, write_record
from vcp.gc.scan import MIB, Item, Root, ScanResult, Unreadable
from vcp.train.records import load_record, save_record
from vcp.train.schema import Attempt

runner = CliRunner()
GIB = 1024**3
STAMP = "2026-11-01T00:00:00.000Z"
FIELD_ORDER = [
    "scan",
    "roots",
    "root_options",
    "items",
    "min_mib",
    "referenced_gib",
    "record_gib",
    "rebuildable_gib",
    "debris_gib",
    "unreferenced_gib",
    "protected_gib",
    "duplicate_gib",
    "missing",
    "skipped",
    "unreadable",
    "running",
]
ARTIFACT_FILES = ("spec.json", "manifest.json", "scan.json", "items.jsonl")


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _field(verdict: str, key: str) -> str:
    for token in verdict.split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1]
    raise AssertionError(f"{key}= not in {verdict}")


def _doc(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _gc(*args: str):
    return runner.invoke(app, ["gc", *args])


def _snap(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for here, _dirs, files in os.walk(root):
        for name in files:
            path = Path(here, name)
            st = path.stat()
            out[path.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _put(base: Path, rel: str, size: int = 1, data: bytes | None = None) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else b"x" * size)
    return path


def _deny_scandir(monkeypatch, *denied: Path) -> None:
    """The scan's own listing raises PermissionError for ``denied`` (the references' walk and
    pathlib keep the real one)."""
    real = os.scandir
    targets = {path.resolve() for path in denied}

    def scandir(path="."):
        if Path(path).resolve() in targets:
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(scan_mod, "_scandir", scandir)


def test_item_line_and_sizes():
    d = Item(
        path="runs/r7",
        kind="dir",
        bytes=3 * GIB + 1,
        files=4,
        cls="referenced",
        referrers=("a#x", "b#y", "c#z", "d#w"),
        copy="D:/vault",
        duplicate_of=None,
        duplicate_bytes=0,
        mtime=STAMP,
    )
    assert item_line(d) == (
        "referenced 3.0 GiB runs/r7/ (files=4) by a#x; b#y; c#z; +1 more copy D:/vault"
    )
    f = Item(
        path="C:/proj/copy.pt",
        kind="file",
        bytes=150 * MIB + 5,
        files=1,
        cls="unreferenced",
        referrers=(),
        copy=None,
        duplicate_of="work/w/best.pt",
        duplicate_bytes=150 * MIB + 5,
        mtime=STAMP,
    )
    assert item_line(f) == "unreferenced 150.0 MiB C:/proj/copy.pt duplicate of work/w/best.pt"
    assert size_text(int(99.96 * MIB)) == "99.9 MiB" and floor_gib(int(9.96 * GIB)) == 9.9
    assert size_text(GIB) == "1.0 GiB" and size_text(GIB - 1) == "1023.9 MiB"
    assert size_text(0) == "0.0 MiB" and floor_gib(0) == 0.0


def test_item_line_prints_at_most_three_referrers_and_no_more_line_for_exactly_three():
    three = Item("a.bin", "file", MIB, 1, "referenced", ("a#1", "b#2", "c#3"), None, None, 0, STAMP)
    assert item_line(three) == "referenced 1.0 MiB a.bin by a#1; b#2; c#3"
    one = Item("a.bin", "file", MIB, 1, "record", (), None, None, 0, STAMP)
    assert item_line(one) == "record 1.0 MiB a.bin"


def test_gc_scan_writes_an_artifact_that_verifies(world):
    r = _gc("scan", "--min-mib", "0", "--json")
    v = _verdict(r.output)
    doc = _doc(r)
    assert r.exit_code == 0 and "status=OK" in v and v.split()[1] == "cmd=gc.scan"
    assert [k.split("=")[0] for k in v.split()[3:]] == FIELD_ORDER
    sid = _field(v, "scan")
    adir = world.roots.data / "artifacts" / "gc_scan" / sid
    assert re.fullmatch(r"gc-\d{8}T\d{6}Z-[0-9a-f]{8}", sid)
    assert {p.name for p in adir.iterdir()} == set(ARTIFACT_FILES)
    lines = adir.joinpath("items.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == doc["result"]["items"]
    assert len(lines) == int(_field(v, "items")) > 0
    scan_doc = json.loads(adir.joinpath("scan.json").read_text(encoding="utf-8"))
    assert scan_doc["items"] == len(lines)
    assert {k: v for k, v in doc["result"].items() if k != "items"} == {
        k: v for k, v in scan_doc.items() if k != "items"
    }
    assert _gc("scan", "--no-record").exit_code == 0  # a later scan sees this one, as a record
    verify = runner.invoke(app, ["artifact", "verify", "--kind", "gc_scan", "--id", sid])
    assert verify.exit_code == 0, verify.output


def test_gc_scan_fields_keep_their_order_with_every_option_given(world, tmp_path):
    proj = tmp_path / "proj"
    _put(proj, "a.bin", 7)

    r = _gc("scan", "--root", str(proj), "--min-mib", "0.5", "--no-record")
    v = _verdict(r.output)

    assert r.exit_code == 0, r.output
    assert [k.split("=")[0] for k in v.split()[3:]] == FIELD_ORDER
    assert (_field(v, "scan"), _field(v, "root_options"), _field(v, "min_mib")) == (
        "none",
        "1",
        "0.5",
    )
    assert int(_field(v, "roots")) >= 2


def test_gc_scan_json_matches_scan_json_with_the_items_listed(world):
    r = _gc("scan", "--min-mib", "0", "--json")
    sid = _field(_verdict(r.output), "scan")
    adir = world.roots.data / "artifacts" / "gc_scan" / sid
    scan_doc = json.loads(adir.joinpath("scan.json").read_text(encoding="utf-8"))

    assert list(scan_doc) == [
        "scanned_at",
        "finished_at",
        "data_root",
        "configs_root",
        "roots",
        "min_bytes",
        "totals",
        "duplicates",
        "items",
        "references",
        "missing",
        "skipped",
        "unreadable",
        "running",
    ]
    assert list(_doc(r)["result"]) == list(scan_doc)
    assert scan_doc["min_bytes"] == 0
    assert scan_doc["data_root"] == world.roots.data.resolve().as_posix()
    assert scan_doc["configs_root"] == world.roots.configs.resolve().as_posix()
    assert scan_doc["roots"][0] == {"path": ".", "recursive": True, "source": "data_root"}
    assert list(scan_doc["totals"]) == list(CLASSES)
    assert all(set(t) == {"bytes", "files"} for t in scan_doc["totals"].values())
    assert set(scan_doc["duplicates"]) == {"bytes", "files"}
    spec = json.loads(adir.joinpath("spec.json").read_text(encoding="utf-8"))["spec"]
    assert (spec["kind"], spec["params"], spec["inputs"]) == (
        KIND,
        {"min_bytes": "0", "root_options": "0"},
        [],
    )


def test_gc_scan_deletes_and_changes_nothing(world, tmp_path):
    data = world.roots.data
    proj = tmp_path / "proj"
    _put(proj, "bundle/up.zip", 40)
    _put(proj, "bundle/notes.txt", 3)
    before, proj_before = _snap(data), _snap(proj)

    r = _gc("scan", "--root", str(proj), "--min-mib", "0")

    assert r.exit_code == 0, r.output
    sid = _field(_verdict(r.output), "scan")
    assert _snap(proj) == proj_before
    after = _snap(data)
    assert {k for k in after if k not in before and not k.startswith("logs/")} == {
        f"artifacts/gc_scan/{sid}/{n}" for n in ARTIFACT_FILES
    }
    assert all(after[k] == v for k, v in before.items() if not k.startswith("logs/"))

    again = _snap(data)
    r = _gc("scan", "--root", str(proj), "--min-mib", "0", "--no-record")

    final = _snap(data)
    lines = r.output.splitlines()
    assert r.exit_code == 0 and _field(_verdict(r.output), "scan") == "none"
    assert lines[-2] == LAST_LINE.format(id="none")
    assert {k for k in final if k not in again and not k.startswith("logs/")} == set()
    assert all(final[k] == v for k, v in again.items() if not k.startswith("logs/"))
    assert _snap(proj) == proj_before


def test_gc_scan_human_output_ends_with_the_listed_only_line(world):
    r = _gc("scan", "--min-mib", "0")

    lines = r.output.splitlines()
    assert r.exit_code == 0, r.output
    assert lines[-2] == LAST_LINE.format(id=_field(lines[-1], "scan"))
    assert lines[-1].startswith("VERDICT ")
    assert any(x.startswith("referenced: ") for x in lines)
    assert any(x.startswith("duplicates: ") for x in lines)
    totals = [x for x in lines if re.match(r"[a-z]+: [\d.]+ (Mi|Gi)B in \d+ files", x)]
    assert [x.split(":")[0] for x in totals] == [*CLASSES, "duplicates"]
    assert any(re.match(r"referenced [\d.]+ (Mi|Gi)B .* by ", x) for x in lines)


def test_gc_scan_warns_when_it_could_not_see_everything(world, monkeypatch):
    data = world.roots.data
    base = _gc("scan")
    assert "status=OK" in _verdict(base.output)
    assert [_field(_verdict(base.output), k) for k in ("skipped", "unreadable", "running")] == [
        "0",
        "0",
        "0",
    ]

    _put(data, "runs/x/run.yaml", data=b"\xff\xfe\x00bad")
    r = _gc("scan")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and _field(v, "skipped") == "1"
    assert any(x.startswith("skipped: run:x: ") for x in r.output.splitlines())
    (data / "runs" / "x" / "run.yaml").unlink()

    record = load_record(data, "good")
    running = Attempt(n=1, started_at=STAMP, console="train/console.1.log")  # status: running
    save_record(data, record.model_copy(update={"attempts": [*record.attempts, running]}))
    r = _gc("scan")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and _field(v, "running") == "1"
    assert "running: good" in r.output.splitlines()
    save_record(data, record)

    _deny_scandir(monkeypatch, data / "measure")
    r = _gc("scan")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and _field(v, "unreadable") == "1"
    assert "unreadable: measure (PermissionError)" in r.output.splitlines()


def test_gc_scan_input_failures(roots, tmp_path):
    r = _gc("scan", "--min-mib=-1")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v and "min_mib=-1.0" in v
    assert "roots=" not in v and "root_options" not in v

    r = _gc("scan", "--min-mib", "nan")
    assert r.exit_code == 1 and "invalid:" in _verdict(r.output)

    r = _gc("scan", "--root", str(tmp_path / "nope"), "--min-mib", "5")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_found:" in v and "root_options=1" in v and "min_mib=5.0" in v
    assert "roots=" not in v

    fresh = tmp_path / "new"
    r = _gc("scan", "--data-root", str(fresh))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and _field(v, "items") == "0"
    assert (fresh / "logs").is_dir()


def test_a_record_that_cannot_be_written_aborts_and_leaves_no_partial(world, monkeypatch):
    def create(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(record_mod.ArtifactWriter, "create", create)

    r = _gc("scan", "--min-mib", "0", "--json")

    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "OSError" in v
    assert "min_mib=0.0" in v and "roots=" not in v
    assert not (world.roots.data / "artifacts" / "gc_scan").exists()


def test_gc_scan_lists_a_non_ascii_name(roots, tmp_path):  # Review Focus 5
    proj = tmp_path / "proj"
    _put(proj, "\u4e0a\u50b3\u5305.zip", 200)

    r = _gc("scan", "--root", str(proj), "--min-mib", "0", "--json")

    doc = _doc(r)
    assert r.exit_code == 0, r.output
    assert any(i["path"].endswith("\u4e0a\u50b3\u5305.zip") for i in doc["result"]["items"])
    sid = _field(_verdict(r.output), "scan")
    lines = (roots.data / "artifacts" / "gc_scan" / sid / "items.jsonl").read_text(encoding="utf-8")
    assert "\u4e0a\u50b3\u5305.zip" in lines

    r = _gc("scan", "--root", str(proj), "--min-mib", "0")
    assert r.exit_code == 0, r.output
    assert "\u4e0a\u50b3\u5305.zip" in r.output
    assert r.output.splitlines()[-1].startswith("VERDICT ")


def _code_page_streams(monkeypatch) -> dict[str, io.BytesIO]:
    """stdout / stderr as a piped Windows process sees them: the locale code page, strict."""
    streams = {}
    for name in ("stdout", "stderr"):
        raw = io.BytesIO()
        streams[name] = raw
        monkeypatch.setattr(sys, name, io.TextIOWrapper(raw, encoding="cp950", errors="strict"))
    return streams


@pytest.mark.parametrize("json_mode", [False, True])
def test_gc_scan_still_prints_its_verdict_when_the_console_cannot_encode_a_name(
    roots, tmp_path, monkeypatch, json_mode
):
    """A simplified-Chinese name has no character on a Big5 code page: the human line is written
    with escapes and the VERDICT still comes, while the record keeps the name as it is."""
    proj = tmp_path / "proj"
    name = "\u6570\u636e.zip"
    _put(proj, name, 200)
    streams = _code_page_streams(monkeypatch)

    with pytest.raises(typer.Exit) as e:
        scan_cmd(
            root=[proj],
            min_mib=0.0,
            no_record=False,
            json_mode=json_mode,
            data_root=None,
            configs_root=None,
        )
    sys.stdout.flush()
    sys.stderr.flush()

    out = streams["stdout"].getvalue().decode("cp950")
    err = streams["stderr"].getvalue().decode("cp950")
    verdict = _verdict(err if json_mode else out)
    assert e.value.exit_code == 0 and "status=OK" in verdict
    if json_mode:
        assert any(i["path"].endswith(name) for i in json.loads(out)["result"]["items"])
    else:
        assert name.encode("ascii", "backslashreplace").decode("ascii") in out
        assert out.splitlines()[-1].startswith("VERDICT ")
    items = next((roots.data / "artifacts" / "gc_scan").iterdir()) / "items.jsonl"
    assert name in items.read_text(encoding="utf-8")


def _result(**changes) -> ScanResult:
    base = {
        "scanned_at": "2026-11-01T01:02:03.456Z",
        "finished_at": "2026-11-01T01:02:04.000Z",
        "data_root": Path.cwd(),
        "configs_root": Path.cwd(),
        "roots": [Root(Path.cwd(), True, "data_root")],
        "min_bytes": 0,
        "totals": {cls: (0, 0) for cls in CLASSES},
        "duplicates": (0, 0),
        "items": [],
        "references": {},
        "missing": 0,
        "skipped": [],
        "unreadable": [],
        "running": [],
    }
    return ScanResult(**{**base, **changes})


def test_scan_ids_come_from_the_scan_time_and_a_nonce():
    a, b = new_scan_id("2026-11-01T01:02:03.456Z"), new_scan_id("2026-11-01T01:02:03.456Z")
    assert re.fullmatch(r"gc-20261101T010203Z-[0-9a-f]{8}", a) and a != b


def test_a_name_with_no_utf8_form_is_recorded_with_escapes_not_lost(tmp_path):
    """A lone surrogate (what a name with invalid bytes becomes on Linux) cannot be encoded as
    UTF-8: the record falls back to JSON escapes rather than ending the scan unrecorded."""
    odd = "weird\udcff.bin"
    item = Item(odd, "file", 5, 1, "unreferenced", (), None, None, 0, STAMP)
    res = _result(
        data_root=tmp_path,
        configs_root=tmp_path,
        roots=[Root(tmp_path, True, "data_root")],
        items=[item],
        unreadable=[Unreadable(odd, "PermissionError")],
    )

    sid = write_record(res, root_options=0)

    adir = tmp_path / "artifacts" / KIND / sid
    assert json.loads((adir / "items.jsonl").read_text(encoding="utf-8")) == item.to_json()
    assert json.loads((adir / "scan.json").read_text(encoding="utf-8"))["unreadable"] == [
        {"path": odd, "error": "PermissionError"}
    ]
    assert scan_json(res)["items"] == 1 and result_json(res)["items"] == [item.to_json()]
