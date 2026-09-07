"""Build private Kaggle input artifacts; upload only after local verification."""

from pathlib import Path
from typing import Annotated

import typer
from rsna_knee.bundling import build_bundle

from vcp.cli_common import CmdResult, JsonOpt, run_command


def main(
    out: Annotated[Path, typer.Option("--out")],
    wheels: Annotated[Path, typer.Option("--wheels")],
    weights: Annotated[list[Path], typer.Option("--weights")],
    kernel: Annotated[str, typer.Option("--kernel")],
    dataset: Annotated[str, typer.Option("--dataset")],
    json_mode: JsonOpt = False,
) -> None:
    def fn() -> CmdResult:
        result = build_bundle(out, wheels, weights, kernel, dataset)
        return "OK", {"files": len(result["files"]), "weights": len(weights)}, result, []

    run_command("rsna.bundle", json_mode, None, fn, context={"kernel": kernel, "dataset": dataset})


if __name__ == "__main__":
    typer.run(main)
