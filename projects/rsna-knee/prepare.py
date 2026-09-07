"""Project-only import/export adapters with standard VERDICT output."""

from pathlib import Path
from typing import Annotated

import typer
from rsna_knee.preparation import export_training, import_test

from vcp.cli_common import CmdResult, JsonOpt, run_command

app = typer.Typer(no_args_is_help=True)


@app.command("export")
def export_cmd(
    out: Annotated[Path, typer.Option("--out")],
    dataset: Annotated[str, typer.Option("--dataset")] = "rsna-knee",
    plan: Annotated[str, typer.Option("--plan")] = "fixed-v1",
    subset: Annotated[str, typer.Option("--subset")] = "train",
    json_mode: JsonOpt = False,
) -> None:
    def fn() -> CmdResult:
        result = export_training(dataset, plan, subset, out)
        return "OK", {"files": result.files, **result.fields}, result.model_dump(mode="json"), []

    run_command(
        "rsna.export",
        json_mode,
        None,
        fn,
        context={"dataset": dataset, "plan": plan, "subset": subset},
    )


@app.command("import-test")
def import_cmd(
    raw: Annotated[Path, typer.Option("--raw")],
    downloaded_at: Annotated[str, typer.Option("--downloaded-at")],
    dataset: Annotated[str, typer.Option("--dataset")] = "rsna-knee-test",
    json_mode: JsonOpt = False,
) -> None:
    def fn() -> CmdResult:
        result = import_test(raw, downloaded_at=downloaded_at, name=dataset)
        fields = {
            "samples": result.samples_written,
            "unlabeled": result.unlabeled,
            "skipped": result.rows_skipped,
        }
        return ("WARN" if result.rows_skipped else "OK"), fields, fields, []

    run_command("rsna.import-test", json_mode, None, fn, context={"dataset": dataset})


if __name__ == "__main__":
    app()
