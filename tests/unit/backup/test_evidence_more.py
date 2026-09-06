import pytest

from backup_fixtures import make_fusion
from submit_fixtures import EVAL, TEST
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths, logs_dir


def _paths(world, name):
    return DatasetPaths.resolve(name, data_root=world.roots.data, configs_root=world.roots.configs)


def _roles(entries):
    out = {}
    for e in entries:
        out.setdefault(e.role, []).append(e.path)
    return out


def test_walk_judgement(world):
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_judgement(_paths(world, EVAL), "p-good", "judgement:p-good")
    roles = _roles(col.files_of())
    assert roles["prereg"] == [f"datasets/{EVAL}/prereg/p-good.yaml"]
    assert roles["prereg_log"] == [f"datasets/{EVAL}/prereg.log.jsonl"]
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/good/run.yaml"]
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_judgement(_paths(world, EVAL), "p-none", "judgement:p-none")


def test_walk_submission(world):
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_submission(_paths(world, TEST), "S1", "submission:S1")
    roles = _roles(col.files_of())
    assert roles["submit_profile"] == [f"datasets/{TEST}/submit.yaml"]
    assert roles["submissions_log"] == [f"datasets/{TEST}/submissions.jsonl"]
    assert roles["stage"] == [f"submit/{TEST}/S1/stage.json"]
    assert roles["artifact"] == [f"submit/{TEST}/S1/submission.csv"]
    assert sorted(roles["run_card"]) == [
        "runs/bad/run.yaml",
        "runs/good.test/run.yaml",
        "runs/good/run.yaml",
    ]
    assert roles["prereg"] == [f"datasets/{EVAL}/prereg/p-good.yaml"]
    assert sorted(roles["dataset_card"]) == [
        f"datasets/{TEST}/dataset.yaml",
        f"datasets/{EVAL}/dataset.yaml",
    ]
    assert f"datasets/{TEST}/splits/all-v1.json" in roles["plan"]
    assert "logs" not in roles
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_submission(_paths(world, TEST), "S9", "submission:S9")


def test_walk_all_on_eval_and_test_datasets(world):
    make_fusion(world)
    (logs_dir(world.roots.data)).mkdir(parents=True, exist_ok=True)
    (logs_dir(world.roots.data) / "vcp-2026-09-06.jsonl").write_text(
        '{"ts": "2026-09-06T00:00:00.000Z"}\n', encoding="utf-8"
    )
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_all(_paths(world, EVAL))
    roles = _roles(col.files_of())
    assert sorted(roles["run_card"]) == [
        "runs/bad/run.yaml",
        "runs/fx/run.yaml",
        "runs/good/run.yaml",
    ]
    assert sorted(roles["prereg"]) == [
        f"datasets/{EVAL}/prereg/p-bad.yaml",
        f"datasets/{EVAL}/prereg/p-good.yaml",
    ]
    assert roles["samples"] == [f"datasets/{EVAL}/samples.jsonl"]
    assert roles["logs"] == ["logs/vcp-2026-09-06.jsonl"]
    assert all(e.for_ == ["all"] for e in col.files_of())
    col2 = Collector(world.roots.data, world.roots.configs)
    col2.walk_all(_paths(world, TEST))
    roles2 = _roles(col2.files_of())
    assert roles2["stage"] == [f"submit/{TEST}/S1/stage.json"]
    assert (
        "runs/good.test/run.yaml" in roles2["run_card"]
        and "runs/good/run.yaml" in roles2["run_card"]
    )


def test_build_manifest_writes_file_and_ledger_row(world):
    res = build_manifest(
        "beach-test", "submission:S1", data_root=world.roots.data, configs_root=world.roots.configs
    )
    assert res.manifest.manifest_id.startswith("submission-S1-") and res.path.is_file()
    assert res.missing == [] and res.unlisted == []
    loaded = load_manifest(_paths(world, TEST), res.manifest.manifest_id)
    assert loaded == res.manifest and loaded.conclusion == "submission:S1"
    row = BackupLedger(_paths(world, TEST).backup_log).rows[-1]
    assert row.event == "manifest" and row.files == len(loaded.files) and row.remote_copies == 1
    assert row.bytes_by_tier["1"] > 0 and row.bytes_by_tier["3"] > 0
    with pytest.raises(ValidationFailed, match="exists"):
        build_manifest(
            "beach-test",
            "submission:S1",
            manifest_id=res.manifest.manifest_id,
            data_root=world.roots.data,
            configs_root=world.roots.configs,
        )
    with pytest.raises(ValidationFailed, match="belongs to dataset"):
        build_manifest(
            "beach-test", "run:good", data_root=world.roots.data, configs_root=world.roots.configs
        )
    with pytest.raises(ValidationFailed, match="not_found"):
        build_manifest("nope", "all", data_root=world.roots.data, configs_root=world.roots.configs)
