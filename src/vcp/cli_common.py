"""Shared CLI plumbing: VERDICT-terminated command runner and common option types."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.log import FieldValue, Status, Verdict, exit_code, setup_logging
from vcp.core.paths import logs_dir, resolve_data_root

CmdResult = tuple[Status, dict[str, FieldValue], Any, list[str]]

JsonOpt = Annotated[bool, typer.Option("--json", help="JSON result to stdout, VERDICT to stderr")]
DataRootOpt = Annotated[Path | None, typer.Option("--data-root", help="override VCP_DATA_ROOT")]
ConfigsRootOpt = Annotated[
    Path | None, typer.Option("--configs-root", help="override VCP_CONFIGS_ROOT")
]
NameOpt = Annotated[str, typer.Option("--name", help="dataset name")]


def parse_opts(opts: list[str] | None, option: str = "--opt") -> dict[str, str]:
    out: dict[str, str] = {}
    for item in opts or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValidationFailed(f"{option} expects key=value, got {item!r}")
        out[key] = value
    return out


def parse_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def render_table(table: dict[str, dict[str, int]], counts: dict[str, int]) -> str:
    subsets = list(table)
    labels = sorted({label for row in table.values() for label in row})
    header = ["label", *subsets]
    rows = [["(total)", *[str(counts.get(s, 0)) for s in subsets]]]
    rows += [[label, *[str(table[s].get(label, 0)) for s in subsets]] for label in labels]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    return "\n".join([fmt(header), *(fmt(r) for r in rows)])


def _logger(data_root: Path | None) -> logging.Logger:
    try:
        return setup_logging(logs_dir(resolve_data_root(data_root)))
    except Exception:
        return logging.getLogger("vcp")


def _error_fields(e: BaseException) -> dict[str, FieldValue]:
    """The machine-readable VERDICT fields a failure carries (``VcpError.fields``).

    Anything else -- including a third-party exception that happens to have a ``fields``
    attribute of some other shape -- contributes prose only: a VERDICT must still be printable
    when the command has already failed.
    """
    fields = getattr(e, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


def run_command(
    cmd: str, json_mode: bool, data_root: Path | None, fn: Callable[[], CmdResult]
) -> None:
    logger = _logger(data_root)
    payload: Any = None
    human: list[str] = []
    try:
        status, fields, payload, human = fn()
    except VcpError as e:
        # `reason` first: it is what a human reads. The error's own fields follow it.
        status = e.status  # type: ignore[assignment]
        fields = {"reason": f"{type(e).__name__}: {e}", **_error_fields(e)}
        logger.error("command failed", exc_info=True, extra={"vcp": {"cmd": cmd}})
    except Exception as e:
        status = "ABORT"
        fields = {"reason": f"{type(e).__name__}: {e}", **_error_fields(e)}
        logger.error("command aborted", exc_info=True, extra={"vcp": {"cmd": cmd}})
    verdict = Verdict(cmd=cmd, status=status, fields=fields)
    logger.info(verdict.line(), extra={"vcp": {"cmd": cmd, "status": status}})
    if json_mode:
        doc = {"cmd": cmd, "status": status, "fields": fields, "result": payload}
        typer.echo(json.dumps(doc, ensure_ascii=False, default=str))
        for line in human:
            if line.startswith("VERDICT "):
                typer.echo(line, err=True)
        typer.echo(verdict.line(), err=True)
    else:
        for line in human:
            typer.echo(line)
        typer.echo(verdict.line())
    raise typer.Exit(code=exit_code(status))
