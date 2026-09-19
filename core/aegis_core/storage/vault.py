"""The key vault — the user's provider API keys, encrypted at rest by Windows DPAPI.

`REMEMBER.md` invariant 9: *keys live in Windows Credential Manager (DPAPI). Never in
argv, env vars passed to children, logs, error payloads, or the renderer.* This module
is the only place in the core that reads or writes one.

Three rules it enforces rather than documents:

- **Only the Windows Credential Manager backend is ever used.** `keyring`'s module-level
  API (`keyring.get_password`) resolves a backend through a config file and installed
  entry points, so a `keyringrc.cfg` or a third-party plugin on `sys.path` could redirect
  every key to a plaintext file — silently, and still "via keyring". `KeyVault` therefore
  instantiates `WinVaultKeyring` itself and never asks `keyring` which backend to use.
- **No key is ever held.** `get_key()` reads through to the OS on every call, and
  `lookup()` hands an adapter a callable rather than a string, so nothing that outlives a
  request can hold one (`ARCHITECTURE.md § 5.3`).
- **No key reaches a log or an error message.** Every log line names the provider and the
  action; every `VaultError` names what went wrong. Neither ever interpolates key
  material, and `tests/storage/test_vault.py` fails if one starts to.

Display is `mask_key()` and nothing else: `ARCHITECTURE.md § 5.3` says the Settings UI
shows `sk-…abcd`, never the key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Final, Protocol

from keyring.errors import PasswordDeleteError

from aegis_core.models.schemas import ProviderId

log = logging.getLogger(__name__)

#: The Credential Manager target every AEGIS key is stored under. The provider id is the
#: account within it, so one entry per provider and nothing else in the user's vault.
SERVICE_NAME: Final = "Aegis"

#: Credential Manager caps a credential blob at 2560 bytes, which is 1280 UTF-16
#: characters. A real key is nowhere near this; the cap exists so an accidental paste of a
#: whole file fails here with a clear message instead of inside the Win32 call.
MAX_KEY_LEN: Final = 1024

#: Below this, `mask_key` reveals nothing at all. Seven characters of a fifteen-character
#: key is most of it, and every real provider key is far longer than this.
MIN_MASKABLE_LEN: Final = 16

PREFIX_CHARS: Final = 3
SUFFIX_CHARS: Final = 4

#: Anything a key may not contain. A key goes straight into an HTTP header
#: (`Authorization`, `x-api-key`, `x-goog-api-key`), so a CR or LF in one is header
#: injection; the rest are a paste that went wrong. Rejecting here is defence in depth —
#: the HTTP client refuses them too.
_FORBIDDEN_CHARS: Final = re.compile(r"[\x00-\x1f\x7f]")


class VaultError(Exception):
    """A key could not be read, written, or removed.

    The message is shown to the user and written to the log, so it says what went wrong
    and **never** quotes the key or any part of it.
    """


class CredentialStore(Protocol):
    """The slice of `keyring.backends.KeyringBackend` this module uses.

    Declared here so a test can supply an in-memory store without touching the real
    Credential Manager, and so the type says exactly which three calls are made.
    """

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def mask_key(key: str) -> str:
    """The only representation of a key that may be displayed: `sk-…abcd`.

    Reveals at most the first three and last four characters, and only when what stays
    hidden is the majority of the key. A short key gets `…` and nothing more.
    """
    trimmed = key.strip()
    if len(trimmed) < MIN_MASKABLE_LEN:
        return "…"
    return f"{trimmed[:PREFIX_CHARS]}…{trimmed[-SUFFIX_CHARS:]}"


def windows_backend() -> CredentialStore:
    """Windows Credential Manager, constructed directly — never `keyring.get_keyring()`.

    Raises `VaultError` where it is unavailable, which off Windows it always is. AEGIS is
    Windows-only (`REMEMBER.md § 4`), and a vault that quietly fell back to something else
    would break invariant 9 without anyone noticing.
    """
    from keyring.backends import Windows

    try:
        priority = Windows.WinVaultKeyring.priority
    except RuntimeError as error:  # keyring's own "this backend is not viable" signal
        raise VaultError(f"Windows Credential Manager is not available: {error}") from error
    if priority <= 0:  # pragma: no cover — keyring raises rather than returning this
        raise VaultError("Windows Credential Manager is not available on this machine")
    # `keyring` ships types but leaves its backends' `__init__` unannotated.
    return Windows.WinVaultKeyring()  # type: ignore[no-untyped-call]


class KeyVault:
    """Read, write, and remove one API key per provider.

    Holds a backend and a service name, and never a key — `vars(vault)` is asserted to be
    free of key material by a test, the same way every adapter's is.
    """

    def __init__(
        self,
        backend: CredentialStore | None = None,
        *,
        service: str = SERVICE_NAME,
    ) -> None:
        self._backend = windows_backend() if backend is None else backend
        self._service = service

    def get_key(self, provider_id: ProviderId) -> str | None:
        """The saved key, read from the OS on every call, or `None` if there is none."""
        try:
            key = self._backend.get_password(self._service, provider_id)
        except Exception as error:
            raise self._failure("read", provider_id, error) from error
        if key is None:
            return None
        stored = key.strip()
        return stored or None

    def set_key(self, provider_id: ProviderId, key: str) -> None:
        """Save `key` for `provider_id`, replacing any key already there.

        Surrounding whitespace is stripped, because a pasted key usually carries a newline
        and a key with one would be rejected by the provider for no visible reason.
        """
        cleaned = self._validate(key)
        try:
            self._backend.set_password(self._service, provider_id, cleaned)
        except Exception as error:
            raise self._failure("save", provider_id, error) from error
        log.info("vault.key_saved", extra={"provider_id": provider_id})

    def delete_key(self, provider_id: ProviderId) -> bool:
        """Remove the key for `provider_id`. Returns whether there was one to remove.

        Idempotent: removing a key that is not there is not an error, because the caller's
        intent — *this provider has no key* — is satisfied either way.
        """
        try:
            self._backend.delete_password(self._service, provider_id)
        except PasswordDeleteError:
            return False
        except Exception as error:
            raise self._failure("remove", provider_id, error) from error
        log.info("vault.key_removed", extra={"provider_id": provider_id})
        return True

    def has_key(self, provider_id: ProviderId) -> bool:
        return self.get_key(provider_id) is not None

    def masked_key(self, provider_id: ProviderId) -> str | None:
        """What Settings displays: `sk-…abcd`, or `None` when no key is saved."""
        key = self.get_key(provider_id)
        return None if key is None else mask_key(key)

    def lookup(self, provider_id: ProviderId) -> Callable[[], str | None]:
        """A `KeyLookup` for an adapter: called once per request, holding no key.

        This is the seam `ARCHITECTURE.md § 5.3` asks for — an adapter is given the means
        to fetch a key at call time, never the key itself.
        """

        def read() -> str | None:
            return self.get_key(provider_id)

        return read

    def _validate(self, key: str) -> str:
        cleaned = key.strip()
        if not cleaned:
            raise VaultError("A key cannot be empty.")
        if len(cleaned) > MAX_KEY_LEN:
            raise VaultError(
                f"That key is {len(cleaned)} characters long; the limit is {MAX_KEY_LEN}. "
                "Check you pasted the key and not a file."
            )
        if _FORBIDDEN_CHARS.search(cleaned):
            # Deliberately does not say *which* character or where: that is a description
            # of the key's contents.
            raise VaultError("That key contains characters an API key cannot contain.")
        return cleaned

    def _failure(self, action: str, provider_id: ProviderId, error: Exception) -> VaultError:
        """Turn a backend failure into a `VaultError` that quotes the type, not the key.

        A backend exception's message is written by `keyring` or by Win32, but the value it
        was handed is the key — so only the exception's class name is carried across.
        """
        log.warning(
            "vault.failed",
            extra={"provider_id": provider_id, "action": action, "error": type(error).__name__},
        )
        return VaultError(
            f"Could not {action} the key for {provider_id} "
            f"({type(error).__name__}). Windows Credential Manager refused the request."
        )
