"""Machine-readable VERDICT lines and JSON-lines logging."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from vcp.core.time import stamp

Status = Literal["OK", "WARN", "FAIL", "ABORT"]
STATUS_ORDER: dict[str, int] = {"OK": 0, "WARN": 1, "FAIL": 2, "ABORT": 3}
EXIT_CODES: dict[str, int] = {"OK": 0, "WARN": 0, "FAIL": 1, "ABORT": 2}
FieldValue = str | int | float | bool
_BARE = re.compile(r'[^\s"=]+')


def format_value(v: FieldValue) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    s = str(v)
    return s if _BARE.fullmatch(s) else json.dumps(s, ensure_ascii=False)


class Verdict(BaseModel):
    cmd: str
    status: Status
    fields: dict[str, FieldValue] = Field(default_factory=dict)

    def line(self) -> str:
        parts = [f"VERDICT cmd={self.cmd}", f"status={self.status}"]
        parts += [f"{k}={format_value(v)}" for k, v in self.fields.items()]
        return " ".join(parts)


def worst(*statuses: str) -> Status:
    if not statuses:
        return "OK"
    return max(statuses, key=lambda s: STATUS_ORDER[s])  # type: ignore[return-value]


def exit_code(status: str) -> int:
    return EXIT_CODES[status]


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": stamp(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "vcp", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, *, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vcp")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    path = log_dir / f"vcp-{stamp()[:10]}.jsonl"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(JsonLinesFormatter())
    logger.addHandler(fh)
    return logger
