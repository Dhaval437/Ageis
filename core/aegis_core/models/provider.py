"""The one interface every model adapter implements (`ARCHITECTURE.md § 5.1`).

Seven providers, one protocol, and five of the seven are OpenAI-compatible — so the
real work is two adapters plus a registry of base URLs, never seven bespoke clients
(`REMEMBER.md § 4`). Nothing above this line knows which provider answered.

The error hierarchy is part of the contract, not an afterthought: the router's fallback
chain fires on `ProviderTransientError` and on nothing else. `ARCHITECTURE.md § 5.3` is
explicit that a fallback triggers on 429/5xx/timeout and **never on a refusal** — a
model declining to do something is a successful call that finishes `content_filter` or
returns prose, and retrying it on another provider would be shopping for a yes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from aegis_core.models.schemas import Capabilities, ChatDelta, ChatRequest, KeyStatus, ProviderId


class ProviderError(Exception):
    """Something went wrong talking to a provider.

    The message is shown to the user and written to the log, so it carries a short
    reason and a status code — **never** a request, a response body, or a header.
    `REVIEW.md § 5` forbids a key reaching a log or an error payload, and a provider's
    error body is the most likely place for one to be echoed back.
    """

    #: Whether the router should try the next model in the chain.
    retryable = False

    def __init__(
        self,
        provider_id: ProviderId,
        reason: str,
        *,
        status: int | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.reason = reason
        self.status = status
        where = f"{provider_id} ({status})" if status is not None else provider_id
        super().__init__(f"{where}: {reason}")


class ProviderTransientError(ProviderError):
    """Rate limited, a 5xx, or a timeout. **The only error that triggers fallback.**"""

    retryable = True


class ProviderAuthError(ProviderError):
    """The key was rejected. Another provider's key will not help, so no fallback."""


class ProviderCapabilityError(ProviderError):
    """The request needs something this model does not have — vision, tools, JSON mode.

    The router's capability gate should catch this at task start. Reaching it mid-run
    means a model's advertised `Capabilities` were wrong.
    """


class ProviderProtocolError(ProviderError):
    """The provider answered, but not with anything this adapter can read.

    A malformed stream, a tool call whose arguments never parse, a missing field. It is
    a bug or an incompatible gateway, not a hiccup, so it does not retry.
    """


@runtime_checkable
class ModelProvider(Protocol):
    """One provider. Implementations live in `aegis_core.models.providers`.

    Three obligations that are easy to get wrong:

    - **`chat` is an async generator**, so it is declared `def ... -> AsyncIterator`,
      not `async def`. It must release its connection in a `finally`: the consumer
      abandons the stream on preemption and on a budget breach, and an adapter that
      leaks a socket there leaks one on every stop.
    - **The stream ends in exactly one `DoneDelta`**, or raises a `ProviderError`.
      No other exception type may escape; wrap the client library's own.
    - **The key is read at call time** from the vault (`P1-07`) and not kept on the
      instance, so nothing that outlives a call can hold one.
    """

    id: ProviderId

    def capabilities(self, model: str) -> Capabilities:
        """Describe one model.

        Takes the model id because a provider serves many — `openrouter` serves
        hundreds, and whether vision is available is a property of the model, not of
        the account.

        An adapter whose models this build can enumerate raises
        `ProviderCapabilityError` for one it does not know, because an unknown id there
        is a typo. An adapter that cannot — a gateway, or a vendor that ships models
        faster than the table is updated — may instead answer a conservative default
        whose **price is `None`**, and let the provider's own 404 reject the typo. What
        it may never do is guess a price: an unknown price is unknown, never free.
        """
        ...

    def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        """Stream one completion. See the obligations above."""
        ...

    async def validate_key(self) -> KeyStatus:
        """Answer the Settings *Test* button. Never raises for a bad key."""
        ...
