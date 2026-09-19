"""Ollama — the local provider, and with it the privacy story (`P1-06`).

`ARCHITECTURE.md § 5.1`: *fully local, zero cost, zero data egress.* Everything this
module does has to keep working on a machine with no internet at all, so nothing here
resolves a name, reaches a vendor, or needs a key.

Ollama serves the OpenAI dialect on `/v1`, so **the client is still `OpenAIProvider`** —
`REMEMBER.md § 4` allows two adapters and a registry of base URLs, not seven clients.
What it cannot be is one more `ProviderConfig`, because two things about a local server
are not a URL:

- **There is no key.** A server on this machine has nobody to authenticate, and a Test
  button that demanded a key would make the offline story unreachable. `ProviderConfig`
  gained `requires_key` for exactly this, and `validate_key()` here answers *is Ollama
  running and does it have a model?* instead.
- **The model table is whatever the user pulled.** No build-time table can know it, and
  unlike a cloud gateway we can simply ask: `/api/tags` lists the models and `/api/show`
  reports each one's capabilities. `refresh()` fills the table the config reads, so
  `capabilities()` stays synchronous, as the protocol requires.

Prices are **0.0, not `None`**. `provider.py` forbids guessing a price, and this is not
a guess: the inference runs on the user's own hardware and no money changes hands.
Reporting it unknown would make `P1-09` pause a task that is genuinely free, which is
the one thing the local path must never do.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2

from aegis_core.models.provider import ProviderTransientError
from aegis_core.models.providers.common import KeyLookup, new_client
from aegis_core.models.providers.compatible import normalise_base_url
from aegis_core.models.providers.openai import OpenAIProvider, ProviderConfig
from aegis_core.models.schemas import Capabilities, ChatDelta, ChatRequest, KeyStatus, ProviderId

log = logging.getLogger(__name__)

#: Where Ollama listens unless it was told otherwise. **`127.0.0.1`, not `localhost`**:
#: a name has to be resolved, `localhost` resolves to `::1` first on a default Windows
#: install, and Ollama binds the v4 loopback. An address that needs no resolver is also
#: the one that cannot be affected by a hosts file or a DNS outage.
DEFAULT_BASE_URL: Final = "http://127.0.0.1:11434"

DEFAULT_PORT: Final = 11434

#: Discovery runs at startup on a machine that may not have Ollama at all, so it may not
#: cost the user a visible pause. A refused connection on loopback is immediate; this is
#: the budget for the case where something is listening but not answering.
DETECT_TIMEOUT_S: Final = 5.0

#: `REVIEW.md § 2`: no unbounded list. A machine with more models than this has them
#: listed, and the rest fall back to the conservative default rather than being probed.
MAX_MODELS: Final = 64

#: What the whole describe phase gets, however many models are installed.
#:
#: `DETECT_TIMEOUT_S` bounds one call, which is not the same as bounding the set: asking
#: about `MAX_MODELS` models one after another would let a server that accepts and then
#: stalls hold startup for minutes, which is exactly the visible pause discovery is not
#: allowed to cost. A model not described inside this budget keeps `OLLAMA_UNKNOWN` — the
#: same answer a server too old to describe it gives.
DISCOVERY_BUDGET_S: Final = 10.0

#: How many models to ask about at once. Enough that loopback latency overlaps, few
#: enough not to bury a server that is also loading a model for somebody.
MAX_CONCURRENT_DESCRIBES: Final = 8

#: What Ollama accepts before it silently truncates the prompt.
#:
#: The server, not the model, decides this: it loads every model with `num_ctx`, which
#: defaults to 4096 whatever the model was trained for, and **drops the overflow without
#: an error**. Advertising a model's trained window would therefore lose the top of the
#: conversation quietly, which is worse than the 400 a cloud provider would return. So
#: the number here is the server's default, and a user who raised `OLLAMA_CONTEXT_LENGTH`
#: is under-served rather than misled — worth revisiting in `P1-10`, where the user can
#: say so.
DEFAULT_CTX_WINDOW: Final = 4096

#: What a model this build could not ask about can be assumed to do.
#:
#: Conservative on vision, unlike the cloud gateways in `compatible.py`, because the two
#: fail differently: OpenRouter answers a 404 that names the model, while Ollama takes an
#: image for a text-only model and answers as though it were not there — silently wrong
#: is worse than refused. Tools are permissive: Ollama does return an error for a model
#: that has none. JSON mode is constrained decoding in the server, so every model has it.
OLLAMA_UNKNOWN: Final = Capabilities(
    vision=False,
    tool_calling=True,
    json_mode=True,
    ctx_window=DEFAULT_CTX_WINDOW,
    cost_per_mtok_input=0.0,
    cost_per_mtok_output=0.0,
)


def default_base_url() -> str:
    """Where to look for Ollama: `OLLAMA_HOST` if the user set one, else loopback.

    `OLLAMA_HOST` is how Ollama's own tooling is pointed somewhere else, so honouring it
    is what makes auto-detect work on a machine that has already been configured. It is
    checked exactly as a user-entered gateway URL is (`P1-05`) — `http` only to this
    machine — because the same environment variable could otherwise send observations of
    the user's screen to another host in the clear.

    Raises `ValueError`, with a message written for the user, when it cannot be used.
    """
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    # `OLLAMA_HOST=127.0.0.1:11434` and `OLLAMA_HOST=myhost` are both spellings Ollama
    # accepts, and neither is a URL until it has a scheme.
    if "://" not in raw:
        raw = f"http://{raw}"
    base_url = normalise_base_url(raw)
    if urlsplit(base_url).port is None:
        base_url = f"{base_url}:{DEFAULT_PORT}"
    return base_url


class OllamaProvider:
    """A `ModelProvider` over a local Ollama server.

    `chat()` and `capabilities()` are `OpenAIProvider`'s, over a config pointed at
    `/v1` — every byte on the wire is the code `P1-02` already proved. What this class
    adds is the part of a local server that is not a base URL: discovery of the installed
    models, their real capabilities, and a Test button that means *is it running?*

    The config holds the **same dict** `refresh()` writes, so a refresh is visible to
    `capabilities()` without rebuilding the provider. The native `/api/*` endpoints sit
    beside `/v1` rather than under it, so they get their own client; on loopback that
    costs nothing.
    """

    def __init__(
        self,
        key_lookup: KeyLookup = lambda: None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.id: ProviderId = "ollama"
        self.base_url = normalise_base_url(base_url)
        self._models: dict[str, Capabilities] = {}
        self._config = ProviderConfig(
            id="ollama",
            base_url=f"{self.base_url}/v1",
            models=self._models,
            unknown_model=OLLAMA_UNKNOWN,
            requires_key=False,
        )
        self._inner = OpenAIProvider(key_lookup, config=self._config, transport=transport)
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = new_client(self.base_url, self._transport)
        return self._client

    async def aclose(self) -> None:
        """Release both connection pools. Safe to call more than once."""
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()
        await self._inner.aclose()

    # -- contract ----------------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        return self._inner.capabilities(model)

    def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        """Stream one completion through the OpenAI-compatible endpoint.

        Deliberately not an `async def`: returning the inner generator rather than
        wrapping it in a second one keeps the stop path one link long, which is what
        makes abandoning a stream on preemption close the socket at the `yield` it was
        suspended on (`REMEMBER.md § 5`, 2026-09-17).
        """
        return self._inner.chat(req)

    async def validate_key(self) -> KeyStatus:
        """Answer the Test button. For a local server the question is *are you there?*

        There is no key to be wrong, so the two useful answers are that Ollama is not
        running and that it is running with nothing pulled. Both name the fix.
        """
        try:
            models = await self._list_models()
        except ProviderTransientError:
            return KeyStatus(
                valid=False,
                detail=f"Could not reach Ollama at {self.base_url}. Check that it is running.",
            )
        if not models:
            return KeyStatus(
                valid=False,
                detail="Ollama is running, but no models are installed. "
                "Install one with: ollama pull llama3.1",
            )
        return KeyStatus(
            valid=True,
            detail=f"Ollama is running with {len(models)} "
            f"model{'' if len(models) == 1 else 's'} installed.",
        )

    # -- discovery ---------------------------------------------------------

    async def refresh(self) -> tuple[str, ...]:
        """Ask the server what it has, and what each of those models can do.

        Raises `ProviderTransientError` if the server cannot be reached — the same error
        a call would raise, so a caller that already handles a provider being down needs
        nothing new.
        """
        names = await self._list_models()
        # Conservative first, so a model we never get to describe is still listed with an
        # answer rather than dropped off the machine.
        found = dict.fromkeys(names, OLLAMA_UNKNOWN)
        if found:
            await self._describe_all(found)
        self._models.clear()
        self._models.update(found)
        log.debug("model.ollama_refreshed", extra={"models": len(found)})
        return tuple(found)

    async def _describe_all(self, found: dict[str, Capabilities]) -> None:
        """Fill in what each model can do, under one budget for the whole set.

        Concurrent because these are independent reads of a server on this machine, and
        serial asking is what turns a stalled server into a startup that hangs. Bounded
        twice — by `MAX_CONCURRENT_DESCRIBES` at once and `DISCOVERY_BUDGET_S` overall —
        so the cost of discovery does not scale with how many models the user pulled.
        """
        limit = asyncio.Semaphore(MAX_CONCURRENT_DESCRIBES)

        async def describe_one(model: str) -> None:
            async with limit:
                found[model] = await self._describe(model)

        try:
            async with asyncio.timeout(DISCOVERY_BUDGET_S):
                await asyncio.gather(*(describe_one(model) for model in found))
        except TimeoutError:
            # Whatever finished is kept; the rest keep the conservative default. Losing
            # a capability is a smaller failure than losing the provider.
            log.warning("model.ollama_describe_timed_out", extra={"models": len(found)})

    @property
    def models(self) -> Mapping[str, Capabilities]:
        """What the last `refresh()` found. Empty until one has run."""
        return dict(self._models)

    async def _list_models(self) -> tuple[str, ...]:
        payload = await self._get("/api/tags")
        raw = payload.get("models")
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        for entry in raw[:MAX_MODELS]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model") or entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    async def _describe(self, model: str) -> Capabilities:
        """Read one model's capabilities, falling back rather than failing discovery.

        A server too old to report them, or a model it cannot describe, must not cost the
        user every *other* model on the machine.
        """
        try:
            payload = await self._post("/api/show", {"model": model})
        except ProviderTransientError:
            return OLLAMA_UNKNOWN
        raw = payload.get("capabilities")
        if not isinstance(raw, list):
            return OLLAMA_UNKNOWN
        named = {entry for entry in raw if isinstance(entry, str)}
        return Capabilities(
            vision="vision" in named,
            tool_calling="tools" in named,
            json_mode=True,
            ctx_window=DEFAULT_CTX_WINDOW,
            cost_per_mtok_input=0.0,
            cost_per_mtok_output=0.0,
        )

    # -- the native API ----------------------------------------------------

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._read(self._http().get(path, timeout=httpx2.Timeout(DETECT_TIMEOUT_S)))

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._read(
            self._http().post(path, json=body, timeout=httpx2.Timeout(DETECT_TIMEOUT_S))
        )

    async def _read(self, call: Awaitable[httpx2.Response]) -> dict[str, Any]:
        """Run one native-API call. Nothing but a `ProviderError` escapes this module."""
        try:
            response = await call
        except httpx2.HTTPError as exc:
            raise ProviderTransientError(self.id, "could not reach Ollama on this machine") from exc
        if response.status_code != httpx2.codes.OK:
            raise ProviderTransientError(
                self.id, "Ollama answered with an error", status=response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderTransientError(self.id, "Ollama did not answer with JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderTransientError(self.id, "Ollama did not answer with JSON")
        return payload


async def detect(
    *,
    base_url: str | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> OllamaProvider | None:
    """Look for a running Ollama and return a provider that knows its models.

    `None` means there is nothing to connect to, which is the ordinary case on a machine
    that has never installed it — so this never raises and never blocks for long. An
    address that cannot be used is the same answer with a logged reason, whether it came
    from `OLLAMA_HOST` or was passed in: startup must not fail because an environment
    variable is wrong, and `P1-10` hands this a URL the user typed, which is not a
    programming error either.
    """
    try:
        resolved = base_url if base_url is not None else default_base_url()
        provider = OllamaProvider(base_url=resolved, transport=transport)
    except ValueError as exc:
        # The message never quotes the address back (`P1-05`), so it is safe to log.
        log.warning("model.ollama_host_unusable", extra={"reason": str(exc)})
        return None

    try:
        await provider.refresh()
    except ProviderTransientError:
        await provider.aclose()
        return None
    return provider
