"""The settings store (`P1-10`, `storage/settings.py`).

A key-value table is easy; what is worth testing is the two decisions on top of it. A
value is bounded, because the writer is the renderer and the renderer is a hostile
caller. And a document this build cannot read is *absent*, not an exception — the only
screen that could replace a bad one is the screen that would otherwise fail to open.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from aegis_core.storage.db import StorageError, bootstrap, connect
from aegis_core.storage.settings import (
    MAX_KEY_LEN,
    MAX_VALUE_BYTES,
    SettingsStore,
)
from pydantic import JsonValue


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "Dhaval's data ü" / "aegis.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[SettingsStore]:
    bootstrap(db_path)
    with SettingsStore.open(db_path) as open_store:
        yield open_store


def test_a_key_with_nothing_saved_reads_as_none(store: SettingsStore) -> None:
    assert store.get("models") is None


def test_a_document_round_trips(store: SettingsStore) -> None:
    document: JsonValue = {"planner": {"primary": {"provider_id": "openai", "model": "gpt-4o"}}}
    store.put("models", document)
    assert store.get("models") == document


def test_a_put_replaces_rather_than_appends(store: SettingsStore) -> None:
    store.put("models", {"a": 1})
    store.put("models", {"b": 2})
    assert store.get("models") == {"b": 2}


def test_a_document_survives_reopening_the_database(db_path: Path) -> None:
    """A setting that dies with the process is not a setting."""
    bootstrap(db_path)
    with SettingsStore.open(db_path) as first:
        first.put("models", {"day_cents": 250})
    with SettingsStore.open(db_path) as second:
        assert second.get("models") == {"day_cents": 250}


def test_delete_reports_whether_there_was_anything(store: SettingsStore) -> None:
    assert store.delete("models") is False
    store.put("models", {"a": 1})
    assert store.delete("models") is True
    assert store.get("models") is None


def test_non_ascii_survives_the_round_trip(store: SettingsStore) -> None:
    store.put("models", {"model": "модель-ü-漢字"})
    assert store.get("models") == {"model": "модель-ü-漢字"}


def test_a_value_over_the_cap_is_refused(store: SettingsStore) -> None:
    """`REVIEW.md § 2`: no unbounded anything, and the writer is the renderer."""
    with pytest.raises(StorageError, match="the limit is"):
        store.put("models", {"junk": "x" * (MAX_VALUE_BYTES + 1)})
    assert store.get("models") is None


def test_the_cap_counts_utf8_bytes_not_characters(store: SettingsStore) -> None:
    """A multi-byte character costs what it costs on disk."""
    with pytest.raises(StorageError):
        store.put("models", {"junk": "é" * MAX_VALUE_BYTES})


def test_an_empty_or_oversized_key_is_refused(store: SettingsStore) -> None:
    with pytest.raises(StorageError, match="settings key"):
        store.put("", {"a": 1})
    with pytest.raises(StorageError, match="settings key"):
        store.get("k" * (MAX_KEY_LEN + 1))


def test_unreadable_json_reads_as_absent_and_logs(
    store: SettingsStore, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row written by something else must not stop the screen that could fix it."""
    # `value_json` is CHECKed as valid JSON, so the only way in is round-tripping a
    # value this build cannot load back — a JSON document that is not an object is
    # valid here, so the corruption is made at the SQLite level deliberately.
    with closing(connect(db_path)) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("INSERT INTO settings (key, value_json) VALUES ('models', 'not json at all')")
    with caplog.at_level(logging.WARNING):
        assert store.get("models") is None
    assert "settings.unreadable" in caplog.text


def test_a_closed_store_refuses_rather_than_crashes(store: SettingsStore) -> None:
    store.close()
    store.close()  # idempotent
    with pytest.raises(StorageError, match="closed"):
        store.get("models")
    with pytest.raises(StorageError, match="closed"):
        store.put("models", {"a": 1})
    with pytest.raises(StorageError, match="closed"):
        store.delete("models")
