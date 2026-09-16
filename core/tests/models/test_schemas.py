"""The shapes every adapter speaks (`ARCHITECTURE.md § 5.1`)."""

from __future__ import annotations

from typing import Any

import pytest
from aegis_core.models.schemas import (
    AssistantMessage,
    Capabilities,
    ChatDelta,
    ChatMessage,
    ChatRequest,
    DoneDelta,
    ImagePart,
    KeyStatus,
    SystemMessage,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallDelta,
    ToolDef,
    ToolResultMessage,
    Usage,
    UsageDelta,
    UserMessage,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

MESSAGES: TypeAdapter[ChatMessage] = TypeAdapter(ChatMessage)
DELTAS: TypeAdapter[ChatDelta] = TypeAdapter(ChatDelta)

A_TOOL_CALL = ToolCall(id="call_1", name="fs.move", arguments={"src": "a", "dst": "b"})


def a_request(**overrides: Any) -> ChatRequest:
    """A minimal valid request, so each test states only what it is about."""
    fields: dict[str, Any] = {
        "model": "gpt-4o",
        "messages": (UserMessage(content=(TextPart(text="hello"),)),),
    }
    fields.update(overrides)
    return ChatRequest(**fields)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        SystemMessage(content="You are AEGIS."),
        UserMessage(content=(TextPart(text="rename the file"),)),
        AssistantMessage(content=(TextPart(text="on it"),)),
        AssistantMessage(tool_calls=(A_TOOL_CALL,)),
        ToolResultMessage(tool_call_id="call_1", content=(TextPart(text="done"),)),
    ],
)
def test_role_discriminates_every_message_type(message: BaseModel) -> None:
    """Each message parses back to its own class, chosen by `role` alone."""
    assert MESSAGES.validate_python(message.model_dump()) == message


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValidationError):
        MESSAGES.validate_python({"role": "developer", "content": "hi"})


def test_an_assistant_message_may_carry_text_and_tool_calls_together() -> None:
    message = AssistantMessage(content=(TextPart(text="moving it"),), tool_calls=(A_TOOL_CALL,))

    assert message.content[0].text == "moving it"
    assert message.tool_calls[0].name == "fs.move"


def test_an_empty_assistant_message_is_refused() -> None:
    """A turn with neither text nor a tool call is a bug, not an empty answer."""
    with pytest.raises(ValidationError, match="text, tool calls, or both"):
        AssistantMessage()


def test_a_user_message_needs_content() -> None:
    with pytest.raises(ValidationError):
        UserMessage(content=())


def test_a_user_message_carries_text_and_images() -> None:
    message = UserMessage(
        content=(TextPart(text="what is on screen?"), ImagePart(media_type="image/webp", data="Aa"))
    )

    assert [part.type for part in message.content] == ["text", "image"]


def test_image_bytes_stay_out_of_repr() -> None:
    """An observation is the user's screen. It must not land in a log line."""
    part = ImagePart(media_type="image/webp", data="SECRETSCREENBYTES")

    assert "SECRETSCREENBYTES" not in repr(part)
    assert "image/webp" in repr(part)


def test_an_unknown_media_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        ImagePart(media_type="image/gif", data="Aa")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def test_a_request_defaults_to_no_tools_and_a_timeout() -> None:
    """`REVIEW.md § 2`: every external call has a timeout, so it cannot be left unset."""
    request = a_request()

    assert request.tools == ()
    assert request.tool_choice == "auto"
    assert request.json_mode is False
    assert request.timeout_s > 0


def test_a_request_needs_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        a_request(messages=())


def test_a_non_positive_timeout_is_refused() -> None:
    with pytest.raises(ValidationError):
        a_request(timeout_s=0)


def test_an_unknown_request_field_is_refused() -> None:
    """`extra="forbid"` everywhere: a provider-specific knob fails at the boundary."""
    with pytest.raises(ValidationError):
        a_request(top_k=40)


def test_a_request_is_frozen() -> None:
    request = a_request()

    with pytest.raises(ValidationError):
        request.model = "gpt-4o-mini"


def test_a_request_has_nowhere_to_put_a_key() -> None:
    """`ARCHITECTURE.md § 5.3`: keys are read at call time and never held in an object.

    A request is logged, replayed and put into error payloads, so a field a key could
    sit in is the field that eventually leaks one.
    """
    forbidden = {
        "key",
        "apikey",
        "token",
        "secret",
        "password",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "bearer",
    }
    for name in ChatRequest.model_fields:
        words = set(name.lower().split("_"))
        assert not words & forbidden, name


def test_a_tool_definition_carries_json_schema() -> None:
    tool = ToolDef(
        name="fs.move",
        description="Move a file.",
        parameters={"type": "object", "properties": {"src": {"type": "string"}}},
    )

    assert a_request(tools=(tool,)).tools[0].parameters["type"] == "object"


# ---------------------------------------------------------------------------
# Response stream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta",
    [
        TextDelta(text="thinking"),
        ToolCallDelta(call=A_TOOL_CALL),
        UsageDelta(usage=Usage(input_tokens=10, output_tokens=2)),
        DoneDelta(finish_reason="tool_calls"),
    ],
)
def test_type_discriminates_every_delta(delta: BaseModel) -> None:
    assert DELTAS.validate_python(delta.model_dump()) == delta


def test_an_error_is_not_a_finish_reason() -> None:
    """A failed call raises a `ProviderError`; it does not finish the stream."""
    with pytest.raises(ValidationError):
        DoneDelta(finish_reason="error")  # type: ignore[arg-type]


def test_tool_arguments_are_already_parsed() -> None:
    """Adapters buffer JSON fragments; nothing downstream sees half a call."""
    delta = ToolCallDelta(call=A_TOOL_CALL)

    assert delta.call.arguments == {"src": "a", "dst": "b"}


def test_unknown_cost_is_none_and_never_negative() -> None:
    assert Usage(input_tokens=0, output_tokens=0).cost_cents is None

    with pytest.raises(ValidationError):
        Usage(input_tokens=0, output_tokens=0, cost_cents=-0.1)


def test_a_fractional_cost_survives() -> None:
    """Integer cents would round a whole cheap task to zero (`ARCHITECTURE.md § 7`)."""
    assert Usage(input_tokens=1, output_tokens=1, cost_cents=0.004).cost_cents == 0.004


# ---------------------------------------------------------------------------
# Provider description
# ---------------------------------------------------------------------------


def test_capabilities_state_every_gate_explicitly() -> None:
    """No default: an adapter that forgets to answer fails, it does not claim `False`."""
    with pytest.raises(ValidationError):
        Capabilities(vision=True, tool_calling=True, ctx_window=128_000)  # type: ignore[call-arg]


def test_capabilities_price_input_and_output_separately() -> None:
    caps = Capabilities(
        vision=True,
        tool_calling=True,
        json_mode=True,
        ctx_window=128_000,
        cost_per_mtok_input=250.0,
        cost_per_mtok_output=1000.0,
    )

    assert caps.cost_per_mtok_input != caps.cost_per_mtok_output


def test_an_empty_context_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        Capabilities(vision=False, tool_calling=False, json_mode=False, ctx_window=0)


def test_key_status_says_yes_or_no_with_a_reason() -> None:
    status = KeyStatus(valid=False, detail="That key was rejected. Check it and try again.")

    assert status.valid is False
    assert status.detail
