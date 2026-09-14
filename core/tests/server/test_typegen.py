"""`aegis_core.server.typegen`: schemas.py → the TS in `packages/shared/src/api.ts`."""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, get_args

import pytest
from aegis_core.server import schemas, typegen
from pydantic import JsonValue

CORE_DIR = Path(__file__).resolve().parents[2]


ModuleFactory = Callable[[str, str], types.ModuleType]


@pytest.fixture
def make_module(tmp_path: Path) -> Iterator[ModuleFactory]:
    """Build real modules from source, so `inspect.getsource` can read them back."""
    created: list[str] = []

    def make(name: str, source: str) -> types.ModuleType:
        path = tmp_path / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        created.append(name)
        exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102 - fixture source
        return module

    yield make
    for name in created:
        sys.modules.pop(name, None)


def test_real_schemas_export_the_names_the_apps_import() -> None:
    out = typegen.render(schemas)

    assert "export const EVENT_TYPES = [" in out
    assert "export type EventType = (typeof EVENT_TYPES)[number];" in out
    assert "export const RISK_TIERS = [" in out
    assert "export interface StreamEvent {" in out
    assert "export interface HealthResponse {" in out
    assert "  readonly type: EventType;" in out
    assert "  readonly task_id: string | null;" in out
    assert "  readonly payload: Readonly<Record<string, unknown>>;" in out


def test_every_event_type_is_emitted_in_order() -> None:
    out = typegen.render(schemas)
    values = get_args(schemas.EventType)

    positions = [out.index(f'"{value}"') for value in values]
    assert positions == sorted(positions)


def test_output_is_deterministic_and_lf_only() -> None:
    first = typegen.render(schemas)

    assert first == typegen.render(schemas)
    assert "\r" not in first
    assert first.startswith("/**\n * GENERATED FILE")


def test_main_writes_utf8_lf_to_stdout() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aegis_core.server.typegen"],
        cwd=CORE_DIR,
        capture_output=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.decode("utf-8") == typegen.render(schemas)
    assert b"\r\n" not in result.stdout


def test_const_name() -> None:
    assert typegen.const_name("EventType") == "EVENT_TYPES"
    assert typegen.const_name("RiskTier") == "RISK_TIERS"
    assert typegen.const_name("Tier") == "TIERS"


def test_annotation_mapping(make_module: ModuleFactory) -> None:
    module = make_module(
        "typegen_mapping",
        '''
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, JsonValue

Color = Literal["red", "green"]
"""A colour."""

class Inner(BaseModel):
    n: int

class Outer(BaseModel):
    """Line one.

    Line three.
    """
    s: str
    f: float = 1.0
    b: bool
    c: Color
    inline: Literal["a", 2, True]
    maybe: Optional[int]
    either: int | str | None
    items: list[Inner | None]
    table: dict[str, JsonValue]
    inner: Inner
    tricky: str = Field(description="ends with */ here")
''',
    )

    out = typegen.render(module)

    assert '/** A colour. */\nexport const COLORS = ["red", "green"] as const;' in out
    assert "/**\n * Line one.\n *\n * Line three.\n */\nexport interface Outer {" in out
    assert "  readonly s: string;" in out
    assert "  readonly f: number;" in out
    assert "  readonly b: boolean;" in out
    assert "  readonly c: Color;" in out
    assert '  readonly inline: "a" | 2 | true;' in out
    assert "  readonly maybe: number | null;" in out
    assert "  readonly either: number | string | null;" in out
    assert "  readonly items: ReadonlyArray<Inner | null>;" in out
    assert "  readonly table: Readonly<Record<string, unknown>>;" in out
    assert "  readonly inner: Inner;" in out
    assert "  /** ends with *\\/ here */" in out
    assert out.index("export interface Inner") < out.index("export interface Outer")


def test_imported_models_are_not_exported(make_module: ModuleFactory) -> None:
    module = make_module(
        "typegen_imports",
        """
from pydantic import BaseModel
from aegis_core.server.schemas import HealthResponse

class Mine(BaseModel):
    x: int
""",
    )

    out = typegen.render(module)

    assert "export interface Mine" in out
    assert "HealthResponse" not in out


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        ("typing.Any", "unsupported annotation"),
        ("datetime.datetime", "unsupported annotation"),
        ("dict[int, str]", "unsupported annotation"),
        ("tuple[int, str]", "unsupported annotation"),
        ("aegis_core.server.schemas.HealthResponse", "not defined in the schemas"),
    ],
)
def test_unsupported_annotations_fail_loudly(
    make_module: ModuleFactory, annotation: str, message: str
) -> None:
    module = make_module(
        "typegen_bad",
        f"""
import datetime, typing
import aegis_core.server.schemas
from pydantic import BaseModel

class Bad(BaseModel):
    field: {annotation}
""",
    )

    with pytest.raises(typegen.TypeGenError, match=message) as info:
        typegen.render(module)
    assert "Bad.field" in str(info.value)


def test_serialization_alias_is_the_wire_name(make_module: ModuleFactory) -> None:
    module = make_module(
        "typegen_alias",
        """
from pydantic import BaseModel, Field

class Aliased(BaseModel):
    internal: int = Field(serialization_alias="wire")
""",
    )

    assert "  readonly wire: number;" in typegen.render(module)


def test_a_wire_name_that_is_not_an_identifier_fails(make_module: ModuleFactory) -> None:
    module = make_module(
        "typegen_alias_bad",
        """
from pydantic import BaseModel, Field

class Aliased(BaseModel):
    internal: int = Field(serialization_alias="not-an-identifier")
""",
    )

    with pytest.raises(typegen.TypeGenError, match="not a plain TS identifier"):
        typegen.render(module)


def test_unsupported_literal_value() -> None:
    with pytest.raises(typegen.TypeGenError, match="unsupported literal value"):
        typegen._literal(1.5, "X")


def test_json_value_alone_is_unknown() -> None:
    assert typegen._ts_type(JsonValue, {}, {}, "X") == "unknown"
    assert typegen._ts_type(Literal["x"], {}, {}, "X") == '"x"'
