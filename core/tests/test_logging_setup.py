"""Tests for the structured logging setup."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest
from aegis_core.logging_setup import (
    _HANDLER_TAG,
    BACKUP_COUNT,
    LOG_FILE_NAME,
    MAX_BYTES,
    JsonFormatter,
    configure_logging,
    default_log_dir,
)


def _installed_handlers() -> list[logging.Handler]:
    """The root handlers this module put there — pytest installs its own too."""
    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False)]


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Give each test the root logger back exactly as it was."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)


def _emit(record_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Format one record through `JsonFormatter` and parse it back."""
    record = logging.LogRecord(
        name="aegis_core.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def test_formatter_emits_one_json_object_per_record() -> None:
    line = JsonFormatter().format(
        logging.LogRecord("l", logging.WARNING, __file__, 7, "boom", None, None)
    )
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "l"
    assert payload["msg"] == "boom"
    assert payload["line"] == 7
    assert payload["ts"].endswith("Z")


def test_formatter_interpolates_args() -> None:
    assert _emit()["msg"] == "hello world"


def test_formatter_merges_extra_fields() -> None:
    payload = _emit({"task_id": "t-1", "step": 3})
    assert payload["task_id"] == "t-1"
    assert payload["step"] == 3


def test_extra_cannot_overwrite_a_base_field() -> None:
    payload = _emit({"level": "DEBUG", "logger": "somewhere-else"})
    assert payload["level"] == "INFO"
    assert payload["logger"] == "aegis_core.test"


def test_unserialisable_extra_does_not_raise() -> None:
    assert _emit({"thing": object()})["thing"].startswith("<object object")


def test_exception_is_rendered_as_text() -> None:
    try:
        raise ValueError("nope")
    except ValueError:
        record = logging.LogRecord("l", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: nope" in payload["exc"]


def test_configure_logging_writes_json_lines_to_the_file(tmp_path: Path) -> None:
    log_file = configure_logging(log_dir=tmp_path, stream=io.StringIO())
    assert log_file == tmp_path / LOG_FILE_NAME

    logging.getLogger("aegis_core.test").info("wrote it", extra={"n": 1})
    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["msg"] for line in lines] == ["wrote it"]
    assert json.loads(lines[0])["n"] == 1


def test_configure_logging_writes_to_the_console_stream(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(log_dir=tmp_path, stream=stream)
    logging.getLogger("aegis_core.test").info("on the console")
    assert json.loads(stream.getvalue().strip())["msg"] == "on the console"


def test_console_handler_defaults_to_stderr_never_stdout(tmp_path: Path) -> None:
    """`ARCHITECTURE.md § 3.1` reserves stdout for the handshake line."""
    configure_logging(log_dir=tmp_path)
    console = [
        h
        for h in _installed_handlers()
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert len(console) == 1
    assert console[0].stream is sys.stderr
    assert console[0].stream is not sys.stdout


def test_reconfiguring_does_not_duplicate_lines(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path, stream=io.StringIO())
    second = io.StringIO()
    configure_logging(log_dir=tmp_path, stream=second)

    logging.getLogger("aegis_core.test").info("once")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert len(second.getvalue().strip().splitlines()) == 1
    assert len((tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()) == 1


def test_configure_logging_creates_a_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "logs"
    configure_logging(log_dir=target, stream=io.StringIO())
    assert target.is_dir()


def test_the_file_log_is_bounded(tmp_path: Path) -> None:
    """REVIEW.md § 2: no unbounded log."""
    configure_logging(log_dir=tmp_path, stream=io.StringIO())
    rotating = [h for h in _installed_handlers() if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == MAX_BYTES
    assert rotating[0].backupCount == BACKUP_COUNT


def test_default_log_dir_follows_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_log_dir() == Path(r"C:\Users\someone\AppData\Local\Aegis\logs")


def test_default_log_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_log_dir() == Path.home() / ".aegis" / "logs"
