"""Scaffold smoke test: the package layout from ARCHITECTURE.md § 4 imports cleanly.

This is the only test P0-01 ships. Real tests arrive with the modules they cover.
"""

from __future__ import annotations

import importlib

import aegis_core

SUBPACKAGES = [
    "actuation",
    "agent",
    "guardian",
    "models",
    "perception",
    "recovery",
    "server",
    "storage",
    "telemetry",
    "tools",
]


def test_version_is_exposed() -> None:
    assert aegis_core.__version__ == "0.0.0"


def test_every_subpackage_imports() -> None:
    for name in SUBPACKAGES:
        assert importlib.import_module(f"aegis_core.{name}") is not None
