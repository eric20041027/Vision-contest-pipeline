"""import (csv_boxes) -> audit (dedup finds planted duplicate) -> split --group-from-audit
-> export yolo + coco, all through the CLI."""

import json
from pathlib import Path

import numpy as np
from PIL import Image
from typer.testing import CliRunner

from vcp.cli import app

runner = CliRunner()


def _img(path: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    arr = np.kron(blocks, np.ones((4, 4), dtype=np.uint8))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.stack([arr, arr, arr], axis=-1)).save(path)


def _verdicts(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("VERDICT ")]


def test_full_flow(roots, tmp_path):
    src = roots.data / "raw" / "flow"
    for i in range(12):
        _img(src / "images" / f"img{i:02d}.jpg", seed=i)
    _img(src / "images" / "img00_copy.jpg", seed=0)  # planted duplicate of img00
    rows = ["image_filename,label_id,x,y,w,h"]
    for i in range(12):
        rows.append(f"img{i:02d}.jpg,{i % 3},2,2,{10 + i % 5},{8 + i % 4}")
    rows.append("img00_copy.jpg,0,2,2,10,8")
    (src / "labels.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "csv_boxes",
            "--src",
            str(src),
            "--name",
            "flow",
            "--license",
            "CC0",
            "--url",
            "https://example.org",
            "--downloaded-at",
            "2026-09-03",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "samples=13" in _verdicts(r.output)[-1]
    card = (roots.configs / "datasets" / "flow" / "dataset.yaml").read_text(encoding="utf-8")
    assert "image_root: raw/flow/images" in card

    r = runner.invoke(app, ["data", "audit", "--name", "flow"])
    assert r.exit_code == 0, r.output
    groups = json.loads(
        (roots.data / "datasets" / "flow" / "cache" / "audit" / "groups.json").read_text()
    )
    assert set(groups) == {"img00.jpg", "img00_copy.jpg"}

    r = runner.invoke(
        app,
        [
            "data",
            "split",
            "--name",
            "flow",
            "--plan-id",
            "p1",
            "--seed",
            "1",
            "--subsets",
            "train:train:0.6,val:eval:0.4",
            "--group-from-audit",
        ],
    )
    assert r.exit_code == 0, r.output
    plan = json.loads((roots.configs / "datasets" / "flow" / "splits" / "p1.json").read_text())
    assert plan["assignment"]["img00.jpg"] == plan["assignment"]["img00_copy.jpg"]
    assert plan["params"]["group_from_audit"] is True

    out_yolo = tmp_path / "yolo"
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "flow",
            "--plan",
            "p1",
            "--subset",
            "val",
            "--format",
            "yolo",
            "--out",
            str(out_yolo),
            "--opt",
            "copy=true",
        ],
    )
    assert r.exit_code == 0, r.output
    n_val = sum(1 for v in plan["assignment"].values() if v == "val")
    assert len(list((out_yolo / "images").iterdir())) == n_val
    manifest = json.loads((out_yolo / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["subset"] == "val" and len(manifest["files"]) == 2 * n_val + 1

    out_coco = tmp_path / "coco"
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "flow",
            "--plan",
            "p1",
            "--subset",
            "val",
            "--format",
            "coco",
            "--out",
            str(out_coco),
        ],
    )
    assert r.exit_code == 0, r.output
    doc = json.loads((out_coco / "instances.json").read_text(encoding="utf-8"))
    assert len(doc["images"]) == n_val and len(doc["annotations"]) == n_val
