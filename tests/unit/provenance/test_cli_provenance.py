from __future__ import annotations

import json

from typer.testing import CliRunner

from helpers import det_samples, make_card
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import dataset_version_id

runner = CliRunner()


def _setup(roots):
    old_samples = det_samples(3, seed=31)
    new_samples = [sample.model_copy(deep=True) for sample in old_samples]
    new_samples[0] = new_samples[0].model_copy(update={"group": "changed"})
    versions = []
    for name, samples in (("cli-p-old", old_samples), ("cli-p-new", new_samples)):
        paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
        ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
        ds.save(paths)
        write_source_audit(paths, ds.card, data_root=roots.data)
        versions.append(ds)
    diff = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="cli-p-old",
            to_dataset="cli-p-new",
            artifact_id="cli-p-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    return versions[0], versions[1], diff


def _roots_args(roots):
    return [
        "--data-root",
        str(roots.data),
        "--configs-root",
        str(roots.configs),
    ]


def test_provenance_rebuild_query_status_and_verify_json(roots):
    old, new, diff = _setup(roots)
    rebuilt = runner.invoke(app, ["provenance", "rebuild", "--json", *_roots_args(roots)])
    assert rebuilt.exit_code == 0
    assert "VERDICT cmd=provenance.rebuild status=OK" in rebuilt.stderr

    impact = runner.invoke(
        app,
        [
            "provenance",
            "impact",
            "--dataset",
            "cli-p-old",
            "--sample",
            diff.changes[0].sample_id,
            "--json",
            *_roots_args(roots),
        ],
    )
    assert impact.exit_code == 0
    impact_doc = json.loads(impact.stdout)
    assert impact_doc["result"]["dataset_version_id"] == dataset_version_id(
        "cli-p-old", old.card.samples_hash
    )
    assert (
        dataset_version_id("cli-p-new", new.card.samples_hash) in impact_doc["result"]["entity_ids"]
    )

    status = runner.invoke(app, ["provenance", "status", "--json", *_roots_args(roots)])
    verify = runner.invoke(app, ["provenance", "verify-index", "--json", *_roots_args(roots)])
    assert status.exit_code == verify.exit_code == 0
    assert json.loads(status.stdout)["result"]["sample_changes"] == 1
    assert json.loads(verify.stdout)["result"]["ok"] is True

    non_head = runner.invoke(
        app,
        [
            "provenance",
            "stale",
            "--head",
            dataset_version_id("cli-p-old", old.card.samples_hash),
            *_roots_args(roots),
        ],
    )
    assert non_head.exit_code == 1
    assert "not_a_head" in non_head.output


def test_provenance_ingest_is_idempotent_and_explain_is_read_only(roots):
    _setup(roots)
    rebuilt = runner.invoke(app, ["provenance", "rebuild", *_roots_args(roots)])
    assert rebuilt.exit_code == 0
    duplicate = runner.invoke(
        app,
        [
            "provenance",
            "ingest",
            "--artifact",
            "cli-p-diff",
            "--json",
            *_roots_args(roots),
        ],
    )
    explained = runner.invoke(
        app,
        [
            "provenance",
            "explain",
            "--entity",
            "artifact:dataset_diff/cli-p-diff",
            "--json",
            *_roots_args(roots),
        ],
    )
    assert duplicate.exit_code == explained.exit_code == 0
    assert json.loads(duplicate.stdout)["result"]["inserted"] is False
    assert json.loads(explained.stdout)["result"]["entity_id"] == (
        "artifact:dataset_diff/cli-p-diff"
    )
    synced = runner.invoke(app, ["provenance", "sync", "--json", *_roots_args(roots)])
    assert synced.exit_code == 0
    assert "VERDICT cmd=provenance.sync status=OK" in synced.stderr


def test_provenance_missing_index_fails_with_command_context(roots):
    result = runner.invoke(
        app,
        [
            "provenance",
            "ingest",
            "--artifact",
            "missing",
            *_roots_args(roots),
        ],
    )
    assert result.exit_code == 1
    assert "VERDICT cmd=provenance.ingest status=FAIL" in result.output
    assert "artifact=missing" in result.output


def test_no_backend_option_still_uses_sqlite(roots):
    _setup(roots)
    rebuilt = runner.invoke(app, ["provenance", "rebuild", *_roots_args(roots)])
    assert rebuilt.exit_code == 0

    result = runner.invoke(app, ["provenance", "status", "--json", *_roots_args(roots)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["fields"]["backend"] == "sqlite"
