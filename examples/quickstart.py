"""End-to-end quickstart: from raw images to a pre-registered, judged claim.

Runs the whole governance loop on a synthetic 240-image classification dataset, in a
throwaway data root, in about a minute. Nothing here touches ``VCP_DATA_ROOT``.

    uv run python examples/quickstart.py            # temporary directory, cleaned up
    uv run python examples/quickstart.py --root ./qs --keep

The point of the last two steps is the one vcp exists for: a candidate that looks better
is not admitted until a claim written down *beforehand* survives a paired bootstrap on at
least two independent validation subsets.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CLASSES = ("cat", "dog")
IMAGES_PER_CLASS = 120
BASELINE_ACCURACY = 0.70
CANDIDATE_ACCURACY = 0.93


def run(step: str, *args: str, env_root: Path, configs_root: Path) -> None:
    """Run one vcp command and echo its VERDICT line."""
    print(f"\n=== {step}\n$ vcp {' '.join(args)}")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vcp",
            *args,
            "--data-root",
            str(env_root),
            "--configs-root",
            str(configs_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in (completed.stdout or "").splitlines():
        print(f"  {line}")
    for line in (completed.stderr or "").splitlines():
        if line.startswith("VERDICT") or "Error" in line or "Traceback" in line:
            print(f"  {line}")
    if completed.returncode != 0:
        raise SystemExit(
            f"quickstart step failed: vcp {' '.join(args)} (exit {completed.returncode})"
        )


def make_images(raw: Path) -> None:
    from PIL import Image

    for label, base in zip(CLASSES, (40, 160), strict=True):
        directory = raw / label
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(IMAGES_PER_CLASS):
            Image.new("RGB", (64, 64), (base, (base + index) % 256, 128)).save(
                directory / f"{label}{index:03d}.png"
            )
    print(f"  {IMAGES_PER_CLASS * len(CLASSES)} images under {raw}")


def write_predictions(data_root: Path, configs_root: Path, out: Path) -> None:
    """Two fake models: one right ~70% of the time, one right ~93%."""
    out.mkdir(parents=True, exist_ok=True)
    plan = json.loads(
        (configs_root / "datasets/demo/splits/fixed-v1.json").read_text(encoding="utf-8")
    )
    truth = {}
    for line in (
        (data_root / "datasets/demo/samples.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        if line.strip():
            row = json.loads(line)
            truth[row["sample_id"]] = row["labels"]["cls"]
    for run_id, accuracy, seed in (
        ("baseline", BASELINE_ACCURACY, 1),
        ("candidate", CANDIDATE_ACCURACY, 2),
    ):
        rng = random.Random(seed)
        for subset in ("valA", "valB"):
            ids = sorted(sid for sid, name in plan["assignment"].items() if name == subset)
            lines = ["sample_id," + ",".join(CLASSES)]
            for sample_id in ids:
                predicted = truth[sample_id] if rng.random() < accuracy else 1 - truth[sample_id]
                scores = [0.12, 0.12]
                scores[predicted] = 0.88
                lines.append(sample_id + "," + ",".join(f"{value:.4f}" for value in scores))
            (out / f"{run_id}-{subset}.csv").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
            )
    print(f"  4 prediction files under {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="where to put the throwaway roots")
    parser.add_argument("--keep", action="store_true", help="do not delete --root afterwards")
    args = parser.parse_args()

    root = (
        Path(args.root).resolve() if args.root else Path(tempfile.mkdtemp(prefix="vcp-quickstart-"))
    )
    data_root, configs_root, preds = root / "vcp-data", root / "vcp-configs", root / "preds"
    for directory in (data_root, configs_root):
        directory.mkdir(parents=True, exist_ok=True)
    roots = {"env_root": data_root, "configs_root": configs_root}
    print(f"quickstart roots: {root}")

    try:
        print("\n=== 1. a tiny image-folder dataset")
        make_images(data_root / "raw" / "demo")

        run(
            "2. import: raw files become a dataset card, samples.jsonl and a per-row source audit",
            "data",
            "import",
            "--importer",
            "imagefolder",
            "--src",
            str(data_root / "raw" / "demo"),
            "--name",
            "demo",
            "--license",
            "CC0-1.0",
            "--url",
            "https://example.invalid/demo",
            "--downloaded-at",
            "2026-09-15",
            **roots,
        )
        run(
            "3. validate: re-verify the card, the rows and their hashes",
            "data",
            "validate",
            "--name",
            "demo",
            **roots,
        )
        run(
            "4. split: one immutable plan, two independent eval subsets",
            "data",
            "split",
            "--name",
            "demo",
            "--plan-id",
            "fixed-v1",
            "--subsets",
            "train:train:0.5,valA:eval:0.25,valB:eval:0.25",
            **roots,
        )

        print("\n=== 5. two models' predictions")
        write_predictions(data_root, configs_root, preds)

        for run_id in ("baseline", "candidate"):
            for subset in ("valA", "valB"):
                run(
                    f"6. ingest {run_id}/{subset}: framework output becomes the"
                    " canonical prediction file",
                    "eval",
                    "ingest",
                    "--run",
                    run_id,
                    "--dataset",
                    "demo",
                    "--plan",
                    "fixed-v1",
                    "--subset",
                    subset,
                    "--format",
                    "scores_csv",
                    "--src",
                    str(preds / f"{run_id}-{subset}.csv"),
                    "--trained-on",
                    "train",
                    **roots,
                )
        run(
            "7. measure the baseline: guardrails first, then one reading per subset x metric",
            "eval",
            "measure",
            "--run",
            "baseline",
            "--metrics",
            "accuracy",
            **roots,
        )
        run(
            "8. anchor: freeze this reading as the guardrail every later run is checked against",
            "eval",
            "anchor",
            "--run",
            "baseline",
            "--subset",
            "valA",
            "--metric",
            "accuracy",
            **roots,
        )
        run(
            "9. preregister: the claim is written down BEFORE the candidate is measured",
            "eval",
            "preregister",
            "--dataset",
            "demo",
            "--id",
            "p1",
            "--claim",
            "candidate beats baseline on accuracy",
            "--component",
            "candidate-model",
            "--class",
            "model",
            "--baseline-run",
            "baseline",
            "--candidate-run",
            "candidate",
            "--metric",
            "accuracy",
            **roots,
        )
        run(
            "10. measure the candidate",
            "eval",
            "measure",
            "--run",
            "candidate",
            "--metrics",
            "accuracy",
            **roots,
        )
        run(
            "11. judge: paired bootstrap on every base; admission needs"
            " t >= 2.0 on at least 2 of them",
            "eval",
            "judge",
            "--dataset",
            "demo",
            "--prereg",
            "p1",
            **roots,
        )
        run("12. the ledger", "eval", "report", "--dataset", "demo", **roots)

        print("\nDone. The verdict above is PASS because the candidate cleared the threshold on")
        print("both valA and valB. Lower CANDIDATE_ACCURACY in this file to about 0.80 and run it")
        print("again: the gap still looks good, but the verdict flips to FAIL. That is the point.")
        return 0
    finally:
        if args.root is None or not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
