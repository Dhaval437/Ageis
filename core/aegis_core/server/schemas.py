"""Pydantic models for the core's HTTP and WebSocket surface.

`ARCHITECTURE.md § 4`: these models are the single source of truth for the TypeScript
types in `packages/shared/src/api.ts`, which `aegis_core.server.typegen` renders from
this module (`pnpm gen:types`). Never hand-write a TS type that mirrors one of them.

Every `BaseModel` defined here and every module-level `Literal` alias is exported. An
alias is documented by a string literal on the line after it, which becomes its JSDoc.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aegis_core.models.schemas import ProviderId

#: `ProviderId` is imported rather than restated: it is a vocabulary, not behaviour, and
#: the wire says the same seven ids the model layer does. `typegen` exports every
#: module-level `Literal` alias, imported or not, so the renderer gets `PROVIDER_IDS` and
#: validates against the same list it is typed with. Same argument as `storage/vault.py`
#: importing it (Decision Log, 2026-09-19); the reverse import (`models/budget.py` →
#: `EventType`) already exists, and neither creates a cycle.


class HealthResponse(BaseModel):
    """`GET /v1/health` (`ARCHITECTURE.md § 9.1`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = Field(description="Always `ok`; a core that cannot answer is down.")
    version: str = Field(description="The `aegis_core` package version.")
    uptime: float = Field(ge=0, description="Seconds since the app was created.")


RiskTier = Literal["SAFE", "CAUTION", "DANGEROUS", "FORBIDDEN"]
"""Risk tiers every tool call is classified into. See `REMEMBER.md § 6`."""


EventType = Literal[
    "task.created",
    "task.status",
    "step.started",
    "step.thought",
    "step.action",
    "step.observation",
    "approval.requested",
    "approval.resolved",
    "preempt.triggered",
    "error",
    "cost.updated",
    "log",
]
"""Every event type on `WS /v1/stream`. Adding one is an `ARCHITECTURE.md § 9.2` edit first."""


class StreamEvent(BaseModel):
    """One message on `WS /v1/stream` (`ARCHITECTURE.md § 9.2`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1, description="Monotonic within one core process, starting at 1.")
    ts: str = Field(description="UTC ISO-8601 with milliseconds.")
    task_id: str | None = Field(description="The task this belongs to; `null` if app-wide.")
    type: EventType
    payload: dict[str, JsonValue] = Field(description="Type-specific body.")


# ---------------------------------------------------------------------------
# The Models screen (`UI.md § 8.4`, `P1-10`)
# ---------------------------------------------------------------------------
#
# These are the **wire** shapes. The model layer's own types — `ModelChoice`,
# `RoleRoute`, `RoleMap` (`models/router.py`) and `BudgetLimits` (`models/budget.py`) —
# stay internal, as `ARCHITECTURE.md § 5.3` and the 2026-09-16 Decision Log row say they
# must: a `RoleMap` is a `Mapping` keyed by a `Literal`, which has no faithful TS
# spelling, and a role that is not mapped yet has to be expressible here and cannot be
# there. `models/service.py` is the one place that converts between the two.


class ModelInfo(BaseModel):
    """One model a provider can serve, as the Models screen lists it.

    A price of `null` is **unknown, never free** (`models/schemas.py`, `Capabilities`);
    a local model is `0.0`, which says the price is known and is nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="The provider's own model id, sent on the wire as-is.")
    vision: bool
    tool_calling: bool
    json_mode: bool
    ctx_window: int = Field(gt=0, description="Total tokens the model accepts, in + out.")
    cost_per_mtok_input: float | None = Field(
        default=None, ge=0, description="US dollars per million input tokens; `null` is unknown."
    )
    cost_per_mtok_output: float | None = Field(default=None, ge=0)


class ProviderCatalog(BaseModel):
    """One provider card on the Models screen.

    It never carries a key. `masked_key` is `mask_key()`'s `sk-…abcd` and nothing else
    (`ARCHITECTURE.md § 5.3`), and it is `null` when no key is saved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    label: str = Field(description="What the card is called, e.g. `Local (Ollama)`.")
    local: bool = Field(description="Runs on this machine: the *Nothing leaves your PC* badge.")
    requires_key: bool = Field(description="`false` for an endpoint with nobody to authenticate.")
    has_key: bool
    masked_key: str | None = Field(default=None, description="`sk-…abcd`, never the key.")
    base_url: str | None = Field(
        default=None, description="The address, where the user may set one."
    )
    editable_base_url: bool = Field(description="Whether this card shows an address field.")
    models: list[ModelInfo] = Field(
        description="What this build knows this provider serves. May be empty."
    )
    free_text_model: bool = Field(
        description="No enumerable catalogue: the user types a model id instead of picking one."
    )
    detail: str | None = Field(
        default=None, description="Why the list is empty, when there is a reason worth showing."
    )


class ModelCatalog(BaseModel):
    """`GET /v1/models/catalog` — every provider and what it can serve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    providers: list[ProviderCatalog]


class ModelChoiceSpec(BaseModel):
    """One model in one slot of one role's chain, as the screen saves it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    model: str = Field(min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)


class RoleRouteSpec(BaseModel):
    """Who answers for one role, and who answers when they cannot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: ModelChoiceSpec
    fallbacks: list[ModelChoiceSpec] = Field(default_factory=list, max_length=3)


class BudgetLimitsSpec(BaseModel):
    """The four ceilings. An explicit `null` is *no ceiling on this one*.

    The defaults are `models/budget.py`'s, restated rather than imported because that
    module already imports this one for `EventType` and a cycle is the worse trade. A
    test asserts the two agree, the same way `storage/usage.py`'s `UsageRole` is held to
    `models/router.py`'s `ModelRole`.

    Restating them also makes an omitted `limits` mean *the defaults* rather than *no
    ceilings at all*, so a settings document written by an older build cannot silently
    remove every ceiling the user was relying on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_cents: float | None = Field(default=100.0, gt=0)
    day_cents: float | None = Field(default=1_000.0, gt=0)
    task_tokens: int | None = Field(default=10_000_000, gt=0)
    day_tokens: int | None = Field(default=100_000_000, gt=0)


class ModelSettings(BaseModel):
    """Everything the Models screen owns, as it is saved and read back.

    A role is `null` until the user maps it — the state the app is in on first run, and
    the reason this is not a `RoleMap` (which requires all three).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    planner: RoleRouteSpec | None = None
    grounder: RoleRouteSpec | None = None
    utility: RoleRouteSpec | None = None
    limits: BudgetLimitsSpec = Field(default_factory=BudgetLimitsSpec)
    custom_base_url: str | None = Field(
        default=None, max_length=2000, description="The user's OpenAI-compatible gateway."
    )
    ollama_base_url: str | None = Field(
        default=None, max_length=2000, description="`null` means the detected default."
    )


class SettingsResponse(BaseModel):
    """`GET /v1/settings` and the answer to `PUT /v1/settings`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: ModelSettings


class SettingsRequest(BaseModel):
    """The body of `PUT /v1/settings`. Whole sections, never a patch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: ModelSettings


class KeyRequest(BaseModel):
    """The body of `PUT /v1/models/keys/{provider_id}`.

    `repr=False` so the key cannot reach a log line, a traceback or an error payload
    through a model repr — the same guard `ImagePart.data` carries. It is deliberately
    **unconstrained** here: every refusal comes from `storage/vault.py`, whose messages
    quote nothing, because a Pydantic validation error echoes the value it rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(repr=False, description="The provider API key. Written, never read back.")


class KeyResponse(BaseModel):
    """What a key write answers with: the masked form, never the key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    has_key: bool
    masked_key: str | None = Field(default=None, description="`sk-…abcd`, never the key.")


class ValidateRequest(BaseModel):
    """The body of `POST /v1/models/validate` — the Models screen's *Test* button."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId


class ValidateResponse(BaseModel):
    """What *Test* found out. `detail` is shown to the user and holds no key material."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    valid: bool
    detail: str = Field(description="Plain, second person, no key and no response body.")
    latency_ms: int = Field(ge=0, description="How long the provider took to answer.")


class SpendTotals(BaseModel):
    """What some slice of the usage ledger adds up to (`storage/usage.py`, `Spend`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cents: float = Field(ge=0, description="The sum of the prices that are known.")
    tokens: int = Field(ge=0)
    unpriced_calls: int = Field(
        ge=0, description="Calls left out of `cents`; non-zero means *at least* that much."
    )


class SpendResponse(BaseModel):
    """`GET /v1/models/spend` — what the spend meter starts from.

    It is a first value, not a poll: `cost.updated` (`ARCHITECTURE.md § 9.2`) keeps the
    meter live, and invariant 15 says the UI is a function of the stream.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: SpendTotals
    limits: BudgetLimitsSpec
