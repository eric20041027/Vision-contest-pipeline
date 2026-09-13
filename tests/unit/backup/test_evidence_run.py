from pathlib import Path

import pytest

from backup_fixtures import make_fusion
from submit_fixtures import EVAL
from vcp.backup.evidence import CONCLUSIONS, Collector, external_path, parse_conclusion
from vcp.backup.schema import CARD_ROLES, ROLES, TIER_OF
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.measure.provenance import attach_receipts
from vcp.measure.runs import load_run, run_dir, save_run


def _col(world) -> Collector:
    return Collector(world.roots.data, world.roots.configs)


def _roles(col) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in col.files_of():
        out.setdefault(e.role, []).append(e.path)
    return out


def test_parse_conclusion():
    assert CONCLUSIONS == ("submission", "judgement", "run", "all")
    assert parse_conclusion("all") == ("all", "")
    assert parse_conclusion("run:good.test") == ("run", "good.test")
    for bad in ("all:x", "run", "run:", "photo:1", "run:bad id"):
        with pytest.raises(ValidationFailed):
            parse_conclusion(bad)


def test_external_path():
    assert external_path(Path("C:/w/best.pt")).endswith("C/w/best.pt")
    assert ":" not in external_path(Path("D:/x/y.pt"))


def test_walk_run_collects_the_run_its_dataset_and_its_training(world):
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["run_card"] == ["runs/good/run.yaml"]
    assert sorted(roles["prediction"]) == [
        "runs/good/predictions/holdout.jsonl",
        "runs/good/predictions/valA.jsonl",
        "runs/good/predictions/valB.jsonl",
    ]
    assert roles["dataset_card"] == [f"datasets/{EVAL}/dataset.yaml"]
    assert roles["plan"] == [f"datasets/{EVAL}/splits/fixed-v1.json"]
    assert roles["unseal_log"] == [f"datasets/{EVAL}/splits/fixed-v1.unseal.jsonl"]
    assert "readings" in roles and "judgements" in roles
    assert roles["train_record"] == ["runs/good/train.yaml"] and roles["train_log"] == [
        "runs/good/train.log.jsonl"
    ]
    assert roles["train_dir"] == ["runs/good/train/console.1.log"]
    assert roles["checkpoint_final"] == ["work/good/weights/best.pt"]
    assert roles["checkpoint"] == ["work/good/weights/last.pt"]
    by_key = {e.key: e for e in col.files_of()}
    final = by_key["data/work/good/weights/best.pt"]
    assert (
        final.kind == "remote_copy"
        and final.remote.dest == str(world.vault)
        and final.remote.run == "good"
    )
    assert final.remote.name == "best.pt" and final.tier == 3 and final.present
    last = by_key["data/work/good/weights/last.pt"]
    assert last.kind == "file" and last.sha256 == sha256_file(world.weights / "last.pt")
    assert all(e.for_ == ["run:good"] for e in col.files_of())
    assert col.missing == [] and col.unlisted == []
    tiers = [e.tier for e in col.files_of()]
    assert tiers == sorted(tiers)


def test_walk_run_merges_conclusions_and_records_missing_files(world):
    col = _col(world)
    col.walk_run("good", "run:good")
    col.walk_run("good", "judgement:p-good")
    by_key = {e.key: e for e in col.files_of()}
    assert by_key["data/runs/good/run.yaml"].for_ == ["run:good", "judgement:p-good"]
    card = load_run(world.roots.data, "bad")
    gone = run_dir(world.roots.data, "bad") / card.predictions["valB"].path
    gone.unlink()
    col.walk_run("bad", "run:bad")
    entry = {e.key: e for e in col.files_of()}["data/runs/bad/predictions/valB.jsonl"]
    assert (
        not entry.present and entry.sha256 == card.predictions["valB"].sha256 and entry.bytes == 0
    )
    assert col.missing == ["data/runs/bad/predictions/valB.jsonl"]
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_run("nope", "run:nope")


def test_walk_run_recurses_into_fusion_members(world):
    fx = make_fusion(world)
    col = _col(world)
    col.walk_run(fx, f"run:{fx}")
    roles = _roles(col)
    assert roles["fuse_record"] == [f"runs/{fx}/fuse.json"]
    assert roles["recipe"] == [f"datasets/{EVAL}/fuse/r1.yaml"]
    assert sorted(roles["run_card"]) == [
        "runs/bad/run.yaml",
        f"runs/{fx}/run.yaml",
        "runs/good/run.yaml",
    ]
    assert "checkpoint_final" in roles  # good's training came along


def test_external_checkpoint(world, tmp_path):
    outside = tmp_path / "elsewhere" / "extra.pt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"extra")
    from vcp.train.checkpoints import register
    from vcp.train.records import load_record, save_record

    rec = load_record(world.roots.data, "good")
    rec, _ = register(rec, [outside], data_root=world.roots.data, attempt=2)
    save_record(world.roots.data, rec)
    col = _col(world)
    col.walk_run("good", "run:good")
    ext = [e for e in col.files_of() if e.root == "external"]
    assert len(ext) == 1 and ext[0].source == outside.resolve().as_posix()
    assert ext[0].path == external_path(outside) and ext[0].role == "checkpoint"
    paths = DatasetPaths.resolve(EVAL, data_root=world.roots.data, configs_root=world.roots.configs)
    assert col.locate(paths.card_yaml)[0] == "configs"


def test_unlisted_is_counted_once_across_walks(world):
    paths = DatasetPaths.resolve(EVAL, data_root=world.roots.data, configs_root=world.roots.configs)
    paths.plan_json("fixed-v1").unlink()
    col = _col(world)
    col.walk_run("good", "run:good")
    col.walk_run("bad", "run:bad")
    assert col.unlisted == [f"configs/datasets/{EVAL}/splits/fixed-v1.json"]


def test_walk_run_collects_access_receipts(world):
    assert ROLES.index("access_receipt") == ROLES.index("run_card") + 1
    assert TIER_OF["access_receipt"] == 1 and "access_receipt" in CARD_ROLES
    with DatasetAccess.open(
        EVAL,
        "fixed-v1",
        subsets={"train"},
        purpose="train",
        run_id="good",
        data_root=world.roots.data,
        configs_root=world.roots.configs,
    ) as access:
        list(access.iter("train"))
    rid = access.receipt_id
    card = attach_receipts(load_run(world.roots.data, "good"), [rid], data_root=world.roots.data)
    save_run(world.roots.data, card)
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["access_receipt"] == [
        f"artifacts/access_receipt/{rid}/manifest.json",
        f"artifacts/access_receipt/{rid}/receipt.json",
    ]
    by_key = {e.key: e for e in col.files_of()}
    entry = by_key[f"data/artifacts/access_receipt/{rid}/receipt.json"]
    assert entry.sha256 == card.access[0].receipt_sha256 and entry.tier == 1 and entry.present
    assert col.missing == [] and col.unlisted == []
