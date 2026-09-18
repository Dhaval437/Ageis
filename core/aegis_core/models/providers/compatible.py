"""The OpenAI-compatible providers that are configuration and nothing else.

`REMEMBER.md § 4`: five of the seven providers speak the same dialect, so they are one
adapter with different base URLs — **not** seven clients. `openai.py` is that adapter
and `ProviderConfig` is the whole of the difference, so this module holds three configs
and no code that talks to a network. `P1-06`'s `ollama` is the fifth, and lives apart
only because it also has to be discovered on `localhost`.

Two of the three serve models this build cannot enumerate — `openrouter` lists hundreds
and a `custom` gateway lists whatever its owner loaded — so both answer
`COMPATIBLE_UNKNOWN` for a model they have no table for, exactly as `anthropic` does and
for the same reason: refusing a model the user's key can actually use is worse than
letting the provider's own 404 reject a typo one call later. Its **price is `None`**,
because `provider.py` forbids guessing one, and an unknown price is never free.
"""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from aegis_core.models.providers.openai import ProviderConfig
from aegis_core.models.schemas import Capabilities

#: What a model this build has no table for can be assumed to do.
#:
#: Permissive on features and pessimistic on size, because the two failures are not
#: symmetric. A capability claimed but absent is refused by the provider on the next
#: call, with the model named; a capability denied here is a model the user cannot reach
#: at all. A context window is the opposite: it is a budget `P4-04` fills, so guessing
#: low costs some compression and guessing high costs a 400 in the middle of a task.
COMPATIBLE_UNKNOWN: Final = Capabilities(
    vision=True,
    tool_calling=True,
    json_mode=True,
    ctx_window=32_768,
    cost_per_mtok_input=None,
    cost_per_mtok_output=None,
)


#: NVIDIA's hosted catalogue, OpenAI-compatible (`ARCHITECTURE.md § 5.1`). No model
#: table: the catalogue is large, moves quickly, and NVIDIA does not publish a
#: per-model dollar rate this build could copy — so every model here reports no price
#: rather than a guessed one, and `P1-09` treats that as unknown, never as free.
NVIDIA: Final = ProviderConfig(
    id="nvidia",
    base_url="https://integrate.api.nvidia.com/v1",
    models={},
    unknown_model=COMPATIBLE_UNKNOWN,
)


#: OpenRouter — one key, hundreds of models from many vendors.
#:
#: `key_check_path` is the one thing it does not share with the others: OpenRouter
#: serves `/models` to anyone, so the Test button (`P1-10`) would call any string a
#: working key. `/api/v1/key` is authenticated and answers for the key that asked.
OPENROUTER: Final = ProviderConfig(
    id="openrouter",
    base_url="https://openrouter.ai/api/v1",
    models={},
    unknown_model=COMPATIBLE_UNKNOWN,
    key_check_path="/key",
)


_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

#: A user-entered URL is settings input, not scraped screen text, but it decides where
#: a key is sent — so it is checked once, here, rather than trusted.
MAX_BASE_URL_CHARS: Final = 2048


def custom_config(base_url: str) -> ProviderConfig:
    """Build the `custom` config for a user-entered OpenAI-compatible gateway.

    vLLM, LM Studio, Groq, Together — anything that speaks Chat Completions. The URL is
    the one part of a provider the user types, and it is where their key gets sent, so
    it is validated here and normalised to the form `httpx2` joins paths onto.

    Raises `ValueError`, with a message written for the user, when the URL cannot be
    used. Settings (`P1-10`) shows that message; nothing here reaches a model.
    """
    return ProviderConfig(
        id="custom",
        base_url=normalise_base_url(base_url),
        models={},
        unknown_model=COMPATIBLE_UNKNOWN,
    )


def normalise_base_url(base_url: str) -> str:
    """Check a user-entered base URL and return the canonical form of it.

    Separate from `custom_config` because `P1-06` validates a URL for the same reason
    and `P1-10` wants to check one before saving it.
    """
    raw = base_url.strip()
    if not raw:
        raise ValueError("Enter the address of the gateway, for example https://host/v1")
    if len(raw) > MAX_BASE_URL_CHARS:
        raise ValueError("That address is too long.")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:  # a malformed IPv6 literal, a bad port
        raise ValueError("That is not a valid address.") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("The address must start with https:// or http://")
    if parts.query or parts.fragment:
        raise ValueError("The address cannot have a query string or a #fragment.")
    # A key in the URL ends up in every access log the request passes through, and this
    # gateway already takes one as a header.
    if parts.username or parts.password:
        raise ValueError("Put the key in the key field, not in the address.")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("That address has an invalid port.") from exc
    if not host:
        raise ValueError("The address is missing a host name.")

    # Plaintext to another machine would put the key on the wire in the clear. A local
    # gateway (vLLM, LM Studio) is served over http and has no wire to speak of, so that
    # stays allowed — this refuses the case where the key really would travel.
    if scheme == "http" and not _is_loopback(host):
        raise ValueError("Use https:// for an address that is not on this machine.")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name this process would have to resolve to judge. Resolution is not a
        # decision to make here: it can change between now and the call.
        return False
