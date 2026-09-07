"""Run through vcp train run; see RUNBOOK.md for the complete invocation."""

from pathlib import Path
from typing import Annotated

import typer
from rsna_knee.training import train

from vcp.cli_common import CmdResult, JsonOpt, run_command


def main(
    config: Annotated[Path, typer.Option("--config")],
    out: Annotated[Path, typer.Option("--out")],
    dataset: Annotated[str, typer.Option("--dataset")] = "rsna-knee",
    plan: Annotated[str, typer.Option("--plan")] = "fixed-v1",
    subset: Annotated[str, typer.Option("--subset")] = "train",
    device: Annotated[str, typer.Option("--device")] = "cuda",
    json_mode: JsonOpt = False,
) -> None:
    def fn() -> CmdResult:
        result = train(dataset, plan, subset, config, out, device)
        return "OK", result, result, []

    run_command(
        "rsna.train",
        json_mode,
        None,
        fn,
        context={"dataset": dataset, "plan": plan, "subset": subset},
    )


if __name__ == "__main__":
    typer.run(main)
