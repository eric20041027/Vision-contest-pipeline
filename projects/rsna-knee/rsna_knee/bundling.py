"""Explicit artifact allowlist for a private, offline Kaggle notebook."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.time import stamp

NOTEBOOK_CODE = """import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

try:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("This bundle requires Python 3.12")
    inputs = Path("/kaggle/input")
    runtime = Path("/kaggle/working/rsna-runtime")
    expanded = list(inputs.rglob("bundle.json"))
    if len(expanded) == 1:
        shutil.copytree(expanded[0].parent, runtime)
    else:
        archives = list(inputs.rglob("bundle.zip"))
        if len(archives) != 1:
            raise RuntimeError("Attach exactly one RSNA vcp bundle dataset")
        runtime.mkdir()
        with zipfile.ZipFile(archives[0]) as archive:
            for name in archive.namelist():
                target = (runtime / name).resolve()
                if not target.is_relative_to(runtime.resolve()):
                    raise RuntimeError("Unsafe archive member")
            archive.extractall(runtime)
    bundle = json.loads((runtime / "bundle.json").read_text())
    for name, digest in bundle["files"].items():
        if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Bundle hash mismatch: " + name)
    deps = runtime / "deps"
    wheels = sorted((runtime / "wheels").glob("*.whl"))
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                    "--target", str(deps), *map(str, wheels)], check=True)
    raw_roots = [p.parent for p in inputs.rglob("test.csv") if (p.parent / "test_series").is_dir()]
    if len(raw_roots) != 1:
        raise RuntimeError("Attach the RSNA Knee competition data")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(deps), str(runtime)])
    env["VCP_DATA_ROOT"] = "/kaggle/working/vcp-data"
    args = [sys.executable, str(runtime / "predict.py"), "--raw", str(raw_roots[0]),
            "--device", "cpu", "--out", "/kaggle/working/submission.csv"]
    for name in bundle["weights"]:
        args += ["--weights", str(runtime / name)]
    subprocess.run(args, env=env, check=True)
    print("VERDICT cmd=rsna.notebook status=OK output=submission.csv")
except Exception:
    print("VERDICT cmd=rsna.notebook status=ABORT", file=sys.stderr)
    raise
"""


def build_bundle(out: Path, wheels: Path, weights: list[Path], kernel_id: str, dataset_id: str):
    if out.exists():
        raise ValidationFailed("exists: bundle output; choose a new version directory")
    wheel_files = sorted(wheels.glob("*.whl"))
    if not wheel_files or not weights or any(not p.is_file() for p in weights):
        raise ValidationFailed("not_found: wheels and checkpoint files are required")
    if not any(p.name.startswith("vcp-") for p in wheel_files):
        raise ValidationFailed("not_found: vcp wheel")
    if any(len(value.split("/")) != 2 for value in (kernel_id, dataset_id)):
        raise ValidationFailed("id: expected owner/slug")
    project = Path(__file__).resolve().parents[1]
    files = {"predict.py": project / "predict.py"}
    files.update(
        {
            f"rsna_knee/{name}": project / "rsna_knee" / name
            for name in ("__init__.py", "data.py", "model.py")
        }
    )
    files.update({f"wheels/{p.name}": p for p in wheel_files})
    files.update({f"weights/{i}.pt": p for i, p in enumerate(weights)})
    manifest = {
        "version": 1,
        "created_at": stamp(),
        "files": {name: sha256_file(p) for name, p in files.items()},
        "weights": [f"weights/{i}.pt" for i in range(len(weights))],
    }
    dataset_dir, kernel_dir = out / "dataset", out / "kernel"
    dataset_dir.mkdir(parents=True)
    kernel_dir.mkdir()
    with zipfile.ZipFile(
        dataset_dir / "bundle.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name, path in files.items():
            archive.write(path, name)
        archive.writestr("bundle.json", json.dumps(manifest, indent=2) + "\n")
    metadata = {
        "title": dataset_id.split("/")[1].replace("-", " "),
        "id": dataset_id,
        "licenses": [{"name": "copyright-authors"}],
        "description": (
            "Private RSNA Knee inference artifacts. Original code, model and dependency "
            "licenses remain applicable. No competition images or patient metadata are included."
        ),
    }
    kernel = {
        "id": kernel_id,
        "title": kernel_id.split("/")[1].replace("-", " "),
        "code_file": "inference.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": False,
        "enable_internet": False,
        "dataset_sources": [dataset_id],
        "competition_sources": ["rsna-knee-abnormality-detection"],
        "kernel_sources": [],
    }
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}
        },
        "cells": [
            {
                "cell_type": "code",
                "id": "rsna-inference",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": NOTEBOOK_CODE.splitlines(keepends=True),
            }
        ],
    }
    for path, doc in (
        (dataset_dir / "dataset-metadata.json", metadata),
        (kernel_dir / "kernel-metadata.json", kernel),
        (kernel_dir / "inference.ipynb", notebook),
        (out / "bundle-manifest.json", manifest),
    ):
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
