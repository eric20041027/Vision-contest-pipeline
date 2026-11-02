"""``vcp.gc.layout``: which class a file belongs to and how far up a directory item may merge
(0.17.0 spec 5.4 and 5.5). Pure path-segment matching: nothing here touches a file system."""

from __future__ import annotations

import pytest

import vcp.gc.layout as layout
from vcp.gc.layout import CLASSES, classify, unit_of


@pytest.mark.parametrize(
    ("rel", "where", "flags", "cls"),
    [
        ("raw/beach/a.png", "data", {}, "protected"),
        ("raw/beach/x.tmp", "data", {}, "protected"),
        ("raw/beach/a.png", "data", {"referenced": True}, "protected"),
        ("locks/submissions-ab.lock", "data", {}, "protected"),
        ("repo/.git/objects/ab", "option", {}, "protected"),
        ("runs/r1/train/.train.yaml.0a1b2c3d.tmp", "data", {"referenced": True}, "debris"),
        ("w/resume.PTH.TMP", "checkpoint", {}, "debris"),
        ("artifacts/k/i1/a.bin", "data", {"partial_artifact": True}, "debris"),
        ("work/w/best.pt", "data", {"referenced": True}, "referenced"),
        ("datasets/d/cache/materialize/png/x.png", "data", {}, "rebuildable"),
        ("datasets/d/cache/audit/groups.json", "data", {}, "rebuildable"),
        ("datasets/d/cache/dhash.jsonl", "data", {}, "rebuildable"),
        ("indexes/provenance-0123.sqlite3", "data", {}, "rebuildable"),
        ("datasets/d/cache/import_skipped.jsonl", "data", {}, "record"),
        ("datasets/d/samples.jsonl", "data", {}, "record"),
        ("datasets/d/raw_manifest.txt", "data", {}, "record"),
        ("runs/r1/inputs/preds.csv", "data", {}, "record"),
        ("runs/r1/big.bin", "data", {"skipped_run": True}, "record"),
        ("runs/r1/big.bin", "data", {}, "unreferenced"),
        ("runs/r9/a.bin", "data", {}, "unreferenced"),
        ("artifacts/gc_scan/gc-1/scan.json", "data", {"committed_artifact": True}, "record"),
        ("artifacts/k/supersession.jsonl", "data", {}, "record"),
        ("artifacts/k/foreign/a.bin", "data", {}, "unreferenced"),
        ("measure/d/readings.jsonl", "data", {}, "record"),
        ("submit/t/S1/stage.json", "data", {}, "record"),
        ("logs/vcp-2026-11-01.jsonl", "data", {}, "record"),
        ("configs/datasets/d/prereg.log.jsonl", "option", {"in_configs": True}, "record"),
        ("junk.bin", "data", {}, "unreferenced"),
        ("bundle/model.zip", "option", {}, "unreferenced"),
    ],
)
def test_the_class_of_a_file_is_the_first_rule_that_holds(rel, where, flags, cls):
    assert classify(tuple(rel.split("/")), where, **{"referenced": False, **flags}) == cls


@pytest.mark.parametrize(
    ("rel", "where", "unit"),
    [
        ("raw/beach/a/b.png", "data", "raw/beach"),
        ("raw/readme.txt", "data", "raw/readme.txt"),
        ("datasets/d/cache/materialize/png/x.png", "data", "datasets/d/cache/materialize/png"),
        (
            "datasets/d/cache/materialize/npy-r256/x.npy",
            "data",
            "datasets/d/cache/materialize/npy-r256",
        ),
        ("datasets/d/cache/audit/groups.json", "data", "datasets/d/cache/audit"),
        ("datasets/d/cache/dhash.jsonl", "data", "datasets/d/cache/dhash.jsonl"),
        ("datasets/d/samples.jsonl", "data", "datasets/d"),
        ("runs/r1/train/console.1.log", "data", "runs/r1"),
        ("measure/d/readings.jsonl", "data", "measure/d"),
        ("submit/t/S1/submission.csv", "data", "submit/t/S1"),
        ("submit/t/submissions.jsonl", "data", "submit/t/submissions.jsonl"),
        ("artifacts/k/i1/a.bin", "data", "artifacts/k/i1"),
        ("artifacts/k/supersession.jsonl", "data", "artifacts/k/supersession.jsonl"),
        ("indexes/provenance-0123.sqlite3", "data", "indexes"),
        ("logs/vcp-2026-11-01.jsonl", "data", "logs"),
        ("work/w/best.pt", "data", "work"),
        ("junk.bin", "data", "junk.bin"),
        ("bundle/a/b.zip", "option", "bundle"),
        ("top.zip", "option", "top.zip"),
        ("best.pt", "checkpoint", "best.pt"),
    ],
)
def test_units(rel, where, unit):
    assert "/".join(unit_of(tuple(rel.split("/")), where)) == unit


@pytest.mark.parametrize(
    ("rel", "where", "flags", "cls"),
    [
        # protected looks at directories: a file named .git (a worktree pointer) is not one
        ("repo/.git", "option", {}, "unreferenced"),
        ("raw", "data", {}, "unreferenced"),
        ("raw/beach/a.png", "option", {}, "unreferenced"),
        ("locks/a.lock", "checkpoint", {}, "unreferenced"),
        ("repo/.git/HEAD", "checkpoint", {"referenced": True}, "protected"),
        # a rule that names the data root's layout does not fire under another root
        ("datasets/d/cache/materialize/png/x.png", "option", {}, "unreferenced"),
        ("indexes/provenance-0123.sqlite3", "checkpoint", {}, "unreferenced"),
        ("runs/r1/inputs/preds.csv", "option", {}, "unreferenced"),
        ("measure/d/readings.jsonl", "option", {}, "unreferenced"),
        ("runs/r1/big.bin", "option", {"skipped_run": True}, "unreferenced"),
        ("artifacts/k/i1/a", "option", {"committed_artifact": True}, "unreferenced"),
        # order: debris before referenced before rebuildable before record
        ("configs/datasets/d/x.tmp", "option", {"in_configs": True}, "debris"),
        ("configs/a.yaml", "option", {"in_configs": True, "referenced": True}, "referenced"),
        ("datasets/d/cache/dhash.jsonl", "data", {"referenced": True}, "referenced"),
        ("datasets/d/cache/materialize/png/x.png.tmp", "data", {}, "debris"),
        ("runs/r1/inputs/preds.csv", "data", {"referenced": True}, "referenced"),
        # a path that only looks like the layout is not it
        ("datasets/d/cache/materialize", "data", {}, "record"),
        ("datasets/d/cache/other/a.bin", "data", {}, "record"),
        ("datasets/d/notes.txt", "data", {}, "unreferenced"),
        ("datasets/d/sub/samples.jsonl", "data", {}, "unreferenced"),
        ("artifacts/supersession.jsonl", "data", {}, "unreferenced"),
        ("artifacts/k/i1/supersession.jsonl", "data", {}, "unreferenced"),
        ("runs/r1", "data", {"skipped_run": True}, "unreferenced"),
        ("measure", "data", {}, "unreferenced"),
        ("logs", "data", {}, "unreferenced"),
    ],
)
def test_the_conditions_of_each_rule_are_exactly_the_ones_the_spec_names(rel, where, flags, cls):
    assert classify(tuple(rel.split("/")), where, **{"referenced": False, **flags}) == cls


@pytest.mark.parametrize(
    ("rel", "where", "unit"),
    [
        ("datasets/d", "data", "datasets/d"),
        ("datasets/d/cache", "data", "datasets/d"),
        ("datasets/d/cache/materialize/png", "data", "datasets/d/cache/materialize/png"),
        ("datasets/d/cache/audit/sub/a.json", "data", "datasets/d/cache/audit"),
        ("datasets/d/cache/import_skipped.jsonl", "data", "datasets/d/cache/import_skipped.jsonl"),
        (
            "datasets/d/cache/materialize/png-r512/a/b.png",
            "data",
            "datasets/d/cache/materialize/png-r512",
        ),
        ("runs/r1", "data", "runs/r1"),
        ("runs/r1/inputs/preds.csv", "data", "runs/r1"),
        ("measure/d", "data", "measure/d"),
        ("submit/t/S1", "data", "submit/t/S1"),
        ("artifacts/k/i1", "data", "artifacts/k/i1"),
        ("locks/a.lock", "data", "locks"),
        ("bundle/a/b/c.zip", "option", "bundle"),
        ("raw/beach/a.png", "option", "raw"),
        ("w/best.pt", "checkpoint", "w/best.pt"),
    ],
)
def test_units_at_the_edges_of_the_table(rel, where, unit):
    assert "/".join(unit_of(tuple(rel.split("/")), where)) == unit


def test_the_classes_are_the_six_the_report_counts():
    assert CLASSES == (
        "referenced",
        "record",
        "rebuildable",
        "debris",
        "unreferenced",
        "protected",
    )


@pytest.fixture(params=[True, False], ids=["folding-fs", "exact-fs"])
def either_fs(request, monkeypatch):
    """The layout module on a case-insensitive file system and on a case-sensitive one."""
    monkeypatch.setattr(layout, "_FOLD_CASE", request.param)
    return request.param


@pytest.mark.parametrize(
    ("rel", "where", "unit"),
    [
        ("Raw/x/a.bin", "data", "Raw/x"),
        ("RAW/x/a.bin", "data", "RAW/x"),
        ("Locks/l.lock", "data", "Locks"),
        ("LOCKS/submissions-ab.lock", "data", "LOCKS"),
    ],
)
def test_the_untouchable_top_levels_are_protected_whatever_their_case(either_fs, rel, where, unit):
    parts = tuple(rel.split("/"))

    assert classify(parts, where, referenced=False) == "protected"
    assert classify(parts, where, referenced=True) == "protected"
    assert "/".join(unit_of(parts, where)) == unit  # the unit keeps the on-disk spelling


@pytest.mark.parametrize(
    ("rel", "where"),
    [
        ("repo/.GIT/objects/o", "option"),
        ("repo/.Git/HEAD", "checkpoint"),
        ("work/proj/.gIT/config", "data"),
    ],
)
def test_a_git_directory_is_protected_whatever_its_case(either_fs, rel, where):
    assert classify(tuple(rel.split("/")), where, referenced=False) == "protected"


@pytest.mark.parametrize(
    ("rel", "flags", "cls", "unit"),
    [
        (
            "Datasets/d/Cache/Materialize/png/a.npy",
            {},
            "rebuildable",
            "Datasets/d/Cache/Materialize/png",
        ),
        ("Datasets/d/CACHE/Audit/g.json", {}, "rebuildable", "Datasets/d/CACHE/Audit"),
        ("DATASETS/d/cache/DHash.jsonl", {}, "rebuildable", "DATASETS/d/cache/DHash.jsonl"),
        ("Indexes/provenance-0123.sqlite3", {}, "rebuildable", "Indexes"),
        ("Datasets/d/Samples.jsonl", {}, "record", "Datasets/d"),
        ("Datasets/d/Raw_Manifest.txt", {}, "record", "Datasets/d"),
        (
            "Datasets/d/Cache/import_skipped.jsonl",
            {},
            "record",
            "Datasets/d/Cache/import_skipped.jsonl",
        ),
        ("Runs/r1/Inputs/preds.csv", {}, "record", "Runs/r1"),
        ("Runs/r1/big.bin", {"skipped_run": True}, "record", "Runs/r1"),
        ("Artifacts/k/Supersession.jsonl", {}, "record", "Artifacts/k/Supersession.jsonl"),
        ("Measure/d/readings.jsonl", {}, "record", "Measure/d"),
        ("SUBMIT/t/S1/stage.json", {}, "record", "SUBMIT/t/S1"),
        ("Logs/vcp-2026-11-01.jsonl", {}, "record", "Logs"),
    ],
)
def test_the_layout_matches_names_in_any_case_on_a_case_insensitive_file_system(
    monkeypatch, rel, flags, cls, unit
):
    monkeypatch.setattr(layout, "_FOLD_CASE", True)
    parts = tuple(rel.split("/"))

    assert classify(parts, "data", referenced=False, **flags) == cls
    assert "/".join(unit_of(parts, "data")) == unit  # the on-disk spelling, never the folded one


@pytest.mark.parametrize(
    ("rel", "flags", "unit"),
    [
        ("Datasets/d/Cache/Materialize/png/a.npy", {}, "Datasets"),
        ("Indexes/provenance-0123.sqlite3", {}, "Indexes"),
        ("Datasets/d/Samples.jsonl", {}, "Datasets"),
        ("Runs/r1/Inputs/preds.csv", {}, "Runs"),
        ("Runs/r1/big.bin", {"skipped_run": True}, "Runs"),
        ("Measure/d/readings.jsonl", {}, "Measure"),
        ("Logs/vcp-2026-11-01.jsonl", {}, "Logs"),
    ],
)
def test_the_layout_is_exact_on_a_case_sensitive_file_system(monkeypatch, rel, flags, unit):
    monkeypatch.setattr(layout, "_FOLD_CASE", False)
    parts = tuple(rel.split("/"))

    assert classify(parts, "data", referenced=False, **flags) == "unreferenced"
    assert "/".join(unit_of(parts, "data")) == unit  # a name it does not know is a top-level item
