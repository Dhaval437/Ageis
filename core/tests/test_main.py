"""Tests for the sidecar entry point."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aegis_core import __main__ as entry
from aegis_core import __version__
from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep `main()`'s `configure_logging()` inside the test's own directory."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
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


@pytest.fixture
def served() -> Iterator[dict[str, Any]]:
    """Capture the `uvicorn.run` call instead of binding a socket."""
    call: dict[str, Any] = {}

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        call["app"] = app
        call.update(kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("aegis_core.__main__.uvicorn.run", fake_run)
        yield call


def test_serves_on_loopback_only(served: dict[str, Any]) -> None:
    entry.main(["--port", "8765"])
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8765
    assert isinstance(served["app"], FastAPI)


def test_port_defaults_to_ephemeral(served: dict[str, Any]) -> None:
    entry.main([])
    assert served["port"] == 0


def test_uvicorn_gets_no_log_config_of_its_own(served: dict[str, Any]) -> None:
    """Otherwise uvicorn writes a second, plain-text copy to stdout."""
    entry.main([])
    assert served["log_config"] is None


def test_main_logs_a_startup_line_to_file(served: dict[str, Any], tmp_path: Path) -> None:
    entry.main([])
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = (tmp_path / "Aegis" / "logs" / "core.log").read_text(encoding="utf-8")
    assert "core.starting" in contents


def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        entry.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__
