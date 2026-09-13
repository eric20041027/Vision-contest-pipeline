from __future__ import annotations

import json

from typer.testing import CliRunner

from helpers import det_samples, make_card
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit

runner = CliRunner()


def _dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    write_source_audit(paths, ds.card, data_root=roots.data)


def test_data_diff_human_and_json_report_the_same_summary(roots):
    old = det_samples(2, seed=9)
    new = [s.model_copy(deep=True) for s in old]
    new[0] = new[0].model_copy(update={"group": "changed"})
    _dataset(roots, "cli-old", old)
    _dataset(roots, "cli-new", new)

    human = runner.invoke(
        app,
        [
            "data",
            "diff",
            "--from",
            "cli-old",
            "--to",
            "cli-new",
            "--id",
            "cli-human",
            "--data-root",
            str(roots.data),
            "--configs-root",
            str(roots.configs),
        ],
    )
    machine = runner.invoke(
        app,
        [
            "data",
            "diff",
            "--from",
            "cli-old",
            "--to",
            "cli-new",
            "--id",
            "cli-json",
            "--json",
            "--data-root",
            str(roots.data),
            "--configs-root",
            str(roots.configs),
        ],
    )

    assert human.exit_code == machine.exit_code == 0
    assert "from=cli-old" in human.output
    assert "to=cli-new" in human.output
    assert "modified=1" in human.output
    assert "VERDICT cmd=diff status=OK" in human.output
    doc = json.loads(machine.stdout)
    assert doc["result"]["summary"]["total_changes"] == 1
    assert doc["fields"]["modified"] == 1
    assert "VERDICT cmd=diff status=OK" in machine.stderr


def test_data_diff_failure_verdict_keeps_both_dataset_identities(roots):
    result = runner.invoke(
        app,
        [
            "data",
            "diff",
            "--from",
            "missing-a",
            "--to",
            "missing-b",
            "--id",
            "missing-diff",
            "--data-root",
            str(roots.data),
            "--configs-root",
            str(roots.configs),
        ],
    )

    assert result.exit_code == 1
    assert "VERDICT cmd=diff status=FAIL" in result.output
    assert "from=missing-a" in result.output
    assert "to=missing-b" in result.output
    assert "id=missing-diff" in result.output
