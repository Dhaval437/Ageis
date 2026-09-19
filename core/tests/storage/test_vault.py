"""The key vault (`P1-07`) — security-gated, so most of this file is about what the
vault must *not* do: hold a key, log one, put one in an error, or store one anywhere but
Windows Credential Manager.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid

import keyring
import pytest
from aegis_core.storage.vault import (
    MAX_KEY_LEN,
    SERVICE_NAME,
    CredentialStore,
    KeyVault,
    VaultError,
    mask_key,
    windows_backend,
)
from keyring.errors import KeyringError, PasswordDeleteError

# A key-shaped string used throughout. Long enough to be maskable, obviously fake.
FAKE_KEY = "sk-proj-0123456789abcdefghijklmnop"


class MemoryStore:
    """An in-memory `CredentialStore`, so no test touches the real Credential Manager."""

    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], str] = {}
        self.reads = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return self.saved.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.saved[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.saved[(service, username)]
        except KeyError as error:
            raise PasswordDeleteError("not found") from error


class ExplodingStore:
    """A backend that fails every call with the key in the exception message.

    That is the realistic shape of the leak this module guards against: a backend or a
    Win32 wrapper echoing the value it was handed.
    """

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError(f"read failed for {FAKE_KEY}")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError(f"write failed for {password}")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringError(f"delete failed for {FAKE_KEY}")


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def vault(store: MemoryStore) -> KeyVault:
    return KeyVault(store)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_set_then_get_returns_the_key(vault: KeyVault) -> None:
    vault.set_key("openai", FAKE_KEY)
    assert vault.get_key("openai") == FAKE_KEY


def test_get_with_nothing_saved_is_none(vault: KeyVault) -> None:
    assert vault.get_key("anthropic") is None
    assert vault.has_key("anthropic") is False


def test_keys_are_stored_per_provider(vault: KeyVault) -> None:
    vault.set_key("openai", FAKE_KEY)
    vault.set_key("google", "AIza-9876543210zyxwvu")
    assert vault.get_key("openai") == FAKE_KEY
    assert vault.get_key("google") == "AIza-9876543210zyxwvu"


def test_set_replaces_an_existing_key(vault: KeyVault) -> None:
    vault.set_key("openai", FAKE_KEY)
    vault.set_key("openai", "sk-replacement-abcdefgh")
    assert vault.get_key("openai") == "sk-replacement-abcdefgh"


def test_everything_is_stored_under_the_one_service_name(
    vault: KeyVault, store: MemoryStore
) -> None:
    vault.set_key("openrouter", FAKE_KEY)
    assert list(store.saved) == [(SERVICE_NAME, "openrouter")]


def test_delete_removes_the_key_and_reports_it(vault: KeyVault) -> None:
    vault.set_key("nvidia", FAKE_KEY)
    assert vault.delete_key("nvidia") is True
    assert vault.get_key("nvidia") is None


def test_delete_with_nothing_saved_is_not_an_error(vault: KeyVault) -> None:
    assert vault.delete_key("nvidia") is False


# ---------------------------------------------------------------------------
# Nothing holds a key
# ---------------------------------------------------------------------------


def test_the_vault_instance_holds_no_key(vault: KeyVault, store: MemoryStore) -> None:
    vault.set_key("openai", FAKE_KEY)
    vault.get_key("openai")
    assert FAKE_KEY not in repr(vars(vault))
    assert FAKE_KEY not in repr(vault)


def test_every_get_reads_through_to_the_backend(vault: KeyVault, store: MemoryStore) -> None:
    """No cache: a key removed elsewhere must stop being returned immediately."""
    vault.set_key("openai", FAKE_KEY)
    before = store.reads
    vault.get_key("openai")
    vault.get_key("openai")
    vault.get_key("openai")
    assert store.reads == before + 3


def test_lookup_reads_at_call_time(vault: KeyVault) -> None:
    read = vault.lookup("openai")
    assert read() is None  # nothing saved yet — the callable was made before the key
    vault.set_key("openai", FAKE_KEY)
    assert read() == FAKE_KEY
    vault.delete_key("openai")
    assert read() is None


def test_lookup_closure_holds_no_key(vault: KeyVault) -> None:
    vault.set_key("openai", FAKE_KEY)
    read = vault.lookup("openai")
    read()
    cells = [cell.cell_contents for cell in (read.__closure__ or ())]
    assert FAKE_KEY not in repr(cells)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_an_empty_key_is_refused(vault: KeyVault, blank: str) -> None:
    with pytest.raises(VaultError, match="cannot be empty"):
        vault.set_key("openai", blank)


def test_surrounding_whitespace_is_stripped(vault: KeyVault) -> None:
    """A pasted key usually carries a newline; the provider would reject it silently."""
    vault.set_key("openai", f"  {FAKE_KEY}\r\n")
    assert vault.get_key("openai") == FAKE_KEY


def test_an_over_long_key_is_refused(vault: KeyVault) -> None:
    with pytest.raises(VaultError, match="limit is"):
        vault.set_key("openai", "s" * (MAX_KEY_LEN + 1))


def test_a_key_at_the_limit_is_accepted(vault: KeyVault) -> None:
    vault.set_key("openai", "s" * MAX_KEY_LEN)
    assert vault.get_key("openai") == "s" * MAX_KEY_LEN


@pytest.mark.parametrize(
    "bad",
    [
        "sk-abcdefghij\r\nX-Injected: yes",  # header injection into Authorization
        "sk-abcdefghij\nsecond line",
        "sk-abcdefghij\x00truncated",
        "sk-abcdefghij\x7fdel",
    ],
)
def test_control_characters_are_refused(vault: KeyVault, bad: str) -> None:
    """A key goes straight into an HTTP header, so a CR or LF in one is injection."""
    with pytest.raises(VaultError) as caught:
        vault.set_key("openai", bad)
    assert "characters an API key cannot contain" in str(caught.value)


def test_a_refused_key_is_not_stored(vault: KeyVault, store: MemoryStore) -> None:
    with pytest.raises(VaultError):
        vault.set_key("openai", "sk-abcdefghij\r\nX-Injected: yes")
    assert store.saved == {}


def test_a_refusal_never_quotes_the_key(vault: KeyVault) -> None:
    leaky = "sk-secret-value\r\nX-Injected: yes"
    with pytest.raises(VaultError) as caught:
        vault.set_key("openai", leaky)
    assert "secret-value" not in str(caught.value)


# ---------------------------------------------------------------------------
# Masking — the only display form (`ARCHITECTURE.md § 5.3`)
# ---------------------------------------------------------------------------


def test_mask_keeps_the_prefix_and_last_four() -> None:
    assert mask_key("sk-proj-0123456789abcd") == "sk-…abcd"


def test_mask_reveals_nothing_of_a_short_key() -> None:
    assert mask_key("sk-short") == "…"
    assert mask_key("sk-0123456789abc") == "sk-…9abc"  # 16: the shortest that reveals
    assert mask_key("sk-0123456789ab") == "…"  # 15: one short
    assert mask_key("") == "…"


def test_mask_never_reveals_most_of_a_key() -> None:
    for length in range(1, 80):
        key = "k" * length
        masked = mask_key(key)
        revealed = len(masked.replace("…", ""))
        assert revealed == 0 or revealed * 2 < length


def test_masked_key_reads_from_the_vault(vault: KeyVault) -> None:
    assert vault.masked_key("openai") is None
    vault.set_key("openai", FAKE_KEY)
    assert vault.masked_key("openai") == "sk-…mnop"


def test_masked_key_is_not_reversible(vault: KeyVault) -> None:
    vault.set_key("openai", FAKE_KEY)
    masked = vault.masked_key("openai")
    assert masked is not None
    assert masked not in FAKE_KEY
    assert FAKE_KEY not in masked


# ---------------------------------------------------------------------------
# Nothing leaks into a log or an error
# ---------------------------------------------------------------------------


def test_saving_logs_the_provider_and_not_the_key(
    vault: KeyVault, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="aegis_core.storage.vault"):
        vault.set_key("openai", FAKE_KEY)
        vault.get_key("openai")
        vault.masked_key("openai")
        vault.delete_key("openai")
    text = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert "vault.key_saved" in text
    assert "vault.key_removed" in text
    assert "openai" in text
    assert FAKE_KEY not in text
    assert "0123456789" not in text


@pytest.mark.parametrize("action", ["read", "save", "remove"])
def test_a_backend_failure_never_carries_the_key(
    action: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The backend puts the key in its own exception message. It must not reach ours."""
    vault = KeyVault(ExplodingStore())
    with (
        caplog.at_level(logging.DEBUG, logger="aegis_core.storage.vault"),
        pytest.raises(VaultError) as caught,
    ):
        if action == "read":
            vault.get_key("openai")
        elif action == "save":
            vault.set_key("openai", FAKE_KEY)
        else:
            vault.delete_key("openai")
    message = str(caught.value)
    assert FAKE_KEY not in message
    assert "KeyringError" in message
    assert action in message
    text = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert FAKE_KEY not in text


def test_a_backend_failure_is_a_vault_error_not_a_keyring_error() -> None:
    """Nothing above this module should have to know `keyring` exists."""
    vault = KeyVault(ExplodingStore())
    with pytest.raises(VaultError):
        vault.get_key("openai")


# ---------------------------------------------------------------------------
# Only DPAPI. Never keyring's own backend resolution.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager is Windows-only")
def test_the_default_backend_is_windows_credential_manager() -> None:
    from keyring.backends.Windows import WinVaultKeyring

    assert isinstance(windows_backend(), WinVaultKeyring)
    assert isinstance(KeyVault()._backend, WinVaultKeyring)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager is Windows-only")
def test_a_redirected_global_keyring_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 9 is about DPAPI, not about `keyring`.

    `keyring.set_keyring(...)`, a `keyringrc.cfg`, or a third-party backend plugin on
    `sys.path` all change what `keyring.get_password()` does. If the vault went through
    that resolution, any of them could send every key to a plaintext file while the code
    still read "via keyring". It must not, so redirecting the global keyring here changes
    nothing.
    """
    plaintext = MemoryStore()  # stands in for a backend that does not encrypt
    monkeypatch.setattr(keyring, "get_keyring", lambda: plaintext)

    from keyring.backends.Windows import WinVaultKeyring

    vault = KeyVault(service=f"Aegis-test-{uuid.uuid4()}")
    assert isinstance(vault._backend, WinVaultKeyring)

    # And the redirected backend never saw a thing.
    vault.set_key("openai", FAKE_KEY)
    try:
        assert plaintext.saved == {}
        assert plaintext.reads == 0
    finally:
        vault.delete_key("openai")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager is Windows-only")
def test_a_real_round_trip_through_dpapi() -> None:
    """The one test that writes to the machine's actual Credential Manager.

    A throwaway service name keeps it out of the user's real AEGIS entry, and it is
    removed on every path.
    """
    service = f"Aegis-test-{uuid.uuid4()}"
    vault = KeyVault(service=service)
    try:
        assert vault.get_key("openai") is None
        vault.set_key("openai", FAKE_KEY)
        assert vault.get_key("openai") == FAKE_KEY
        assert vault.masked_key("openai") == "sk-…mnop"

        # A second vault over the same service reads what the first wrote: the key is in
        # the OS, not in the object.
        assert KeyVault(service=service).get_key("openai") == FAKE_KEY

        assert vault.delete_key("openai") is True
        assert vault.get_key("openai") is None
        assert vault.delete_key("openai") is False
    finally:
        with contextlib.suppress(VaultError):  # cleanup; it may already be gone
            vault.delete_key("openai")


def test_memory_store_satisfies_the_protocol(store: MemoryStore) -> None:
    """Keeps the fake honest: if `CredentialStore` grows a method, this fails."""
    checked: CredentialStore = store
    assert checked.get_password(SERVICE_NAME, "openai") is None
