"""Structured logging for the core: one JSON object per line, to a file and the console.

Two things are deliberate here.

**The console stream is `stderr`, never `stdout`.** `ARCHITECTURE.md § 3.1` reserves
the core's stdout for exactly one line — the handshake JSON that MAIN parses before
closing the pipe. A log line written to stdout would corrupt that handshake, so the
console handler and uvicorn's own loggers all go to stderr.

**The file handler rotates.** `REVIEW.md § 2` forbids an unbounded log, and this one
runs for as long as the app does.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, Final

#: Attributes every `LogRecord` carries. Anything else on the record came from a
#: caller's ``extra={...}`` and is merged into the JSON payload.
_STANDARD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: Marks the handlers this module installed, so re-configuring replaces them
#: instead of stacking a second copy of every line.
_HANDLER_TAG: Final = "aegis"

LOG_FILE_NAME: Final = "core.log"
MAX_BYTES: Final = 5 * 1024 * 1024
BACKUP_COUNT: Final = 5


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object.

    Base fields always win over ``extra`` keys of the same name, so a caller
    cannot rewrite the timestamp or the level of its own line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
            "pid": record.process,
            "thread": record.threadName,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # default=str: an unserialisable `extra` value must not raise inside logging.
        return json.dumps(payload, default=str, ensure_ascii=False)


def default_log_dir() -> Path:
    """`%LOCALAPPDATA%\\Aegis\\logs`, falling back to `~/.aegis/logs` off Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Aegis" / "logs"
    return Path.home() / ".aegis" / "logs"


def configure_logging(
    *,
    log_dir: Path | None = None,
    level: int = logging.INFO,
    stream: IO[str] | None = None,
) -> Path:
    """Install the JSON file + console handlers on the root logger.

    Idempotent: a second call replaces the handlers this module installed.
    Returns the path of the log file being written.
    """
    directory = default_log_dir() if log_dir is None else log_dir
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    formatter = JsonFormatter()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()
    for handler in (file_handler, console_handler):
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(level)

    return log_file
