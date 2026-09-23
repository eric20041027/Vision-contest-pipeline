from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from helpers import det_samples, det_with_runs, make_card
from vcp.cli import app
from vcp.core.paths import DatasetPaths, provenance_index_path
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.render_mermaid import MARKER

runner = CliRunner()


def _roots_args(roots):
    return ["--data-root", str(roots.data), "--configs-root", str(roots.configs)]


def _graph(roots, *args):
    return runner.invoke(app, ["provenance", "graph", *args, *_roots_args(roots)])


def _rebuild(roots) -> str:
    rebuilt = runner.invoke(app, ["provenance", "rebuild", "--json", *_roots_args(roots)])
    assert rebuilt.exit_code == 0, rebuilt.output
    return json.loads(rebuilt.stdout)["fields"]["hash"]


def _measured_contest(roots, tmp_path) -> str:
    det_with_runs(roots, tmp_path, n=40)
    measure_run(
        MeasureSpec(
            run_id="perfect",
            subsets=["valA", "valB"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    return _rebuild(roots)


def _evolved(roots) -> None:
    old = det_samples(3, seed=41)
    new = [sample.model_copy(deep=True) for sample in old]
    new[0] = new[0].model_copy(update={"group": "changed"})
    for name, samples in (("cli-g-old", old), ("cli-g-new", new)):
        paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
        dataset = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
        dataset.save(paths)
        write_source_audit(paths, dataset.card, data_root=roots.data)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="cli-g-old",
            to_dataset="cli-g-new",
            artifact_id="cli-g-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def test_graph_writes_html_and_leaves_the_index_untouched(roots, tmp_path):
    rebuild_hash = _measured_contest(roots, tmp_path)
    index = provenance_index_path(roots.data)
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    out = tmp_path / "views" / "graph.html"

    result = _graph(roots, "--out", str(out))

    assert result.exit_code == 0, result.output
    verdict = result.output.strip().splitlines()[-1]
    assert verdict.startswith("VERDICT cmd=provenance.graph status=OK")
    for field in ("backend=sqlite", "format=html", "detail=overview", "scope=all"):
        assert field in verdict
    assert f"hash={rebuild_hash}" in verdict
    text = out.read_text(encoding="utf-8")
    assert MARKER in text[:4096] and "flowchart LR" in text
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before


def test_graph_json_returns_the_drawn_view(roots, tmp_path):
    _measured_contest(roots, tmp_path)
    out = tmp_path / "graph.md"

    result = _graph(roots, "--out", str(out), "--json")

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert (doc["cmd"], doc["status"]) == ("provenance.graph", "OK")
    payload = doc["result"]
    assert (payload["format"], payload["out"]) == ("md", str(out.resolve()))
    assert {"dataset", "split", "run"} <= {node["kind"] for node in payload["nodes"]}
    perfect = next(node for node in payload["nodes"] if node["members"] == ["run:perfect"])
    assert perfect["label"][0] == "perfect" and perfect["status"] == "VALID"
    assert payload["counts"]["BROKEN"] == 0
    assert "VERDICT cmd=provenance.graph status=OK" in result.stderr


def test_graph_full_detail_for_one_run_draws_its_readings(roots, tmp_path):
    _measured_contest(roots, tmp_path)

    result = _graph(
        roots,
        "--out",
        str(tmp_path / "perfect.mmd"),
        "--entity",
        "run:perfect",
        "--detail",
        "full",
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["scope"] == "entity:run:perfect"
    readings = [node for node in payload["nodes"] if node["kind"] == "reading"]
    assert len(readings) == 2
    assert all(member != "run:noisy" for node in payload["nodes"] for member in node["members"])


def test_graph_without_an_index_fails_and_writes_nothing(roots, tmp_path):
    out = tmp_path / "graph.html"

    result = _graph(roots, "--out", str(out))

    assert result.exit_code == 1
    assert "VERDICT cmd=provenance.graph status=FAIL" in result.output
    assert "vcp provenance rebuild" in result.output
    assert not out.exists()


@pytest.mark.parametrize(
    ("case", "reason"),
    [("foreign", "exists"), ("data_root", "out_in_data_root"), ("suffix", "unsupported_format")],
)
def test_graph_refuses_unsafe_outputs(roots, tmp_path, case, reason):
    _measured_contest(roots, tmp_path)
    out = {
        "foreign": tmp_path / "notes.md",
        "data_root": roots.data / "graph.html",
        "suffix": tmp_path / "graph.svg",
    }[case]
    if case == "foreign":
        out.write_text("my notes\n", encoding="utf-8")

    result = _graph(roots, "--out", str(out))

    assert result.exit_code == 1
    assert "VERDICT cmd=provenance.graph status=FAIL" in result.output
    assert reason in result.output
    if case == "foreign":
        assert out.read_text(encoding="utf-8") == "my notes\n"
    else:
        assert not out.exists()


def test_graph_head_must_be_a_current_dataset_head(roots, tmp_path):
    _evolved(roots)
    _rebuild(roots)

    stale_head = _graph(roots, "--out", str(tmp_path / "old.html"), "--head", "cli-g-old")
    head = _graph(roots, "--out", str(tmp_path / "new.html"), "--head", "cli-g-new")

    assert stale_head.exit_code == 1 and "not_a_head" in stale_head.output
    assert head.exit_code == 0, head.output
    assert "head=dataset:cli-g-new@" in head.output


def test_graph_warns_when_the_scope_holds_broken_evidence(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=40)
    (roots.data / "runs" / "orphan").mkdir()  # a run directory without run.yaml
    _rebuild(roots)

    result = _graph(roots, "--out", str(tmp_path / "graph.html"))

    assert result.exit_code == 0, result.output
    verdict = result.output.strip().splitlines()[-1]
    assert verdict.startswith("VERDICT cmd=provenance.graph status=WARN")
    assert "broken=1" in verdict
    assert "run:orphan" in result.output


def test_graph_rejects_an_unknown_detail_level(roots, tmp_path):
    result = _graph(roots, "--out", str(tmp_path / "graph.html"), "--detail", "everything")

    assert result.exit_code == 1
    assert "unsupported_detail" in result.output
