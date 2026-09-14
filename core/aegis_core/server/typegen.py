"""Render `server/schemas.py` as TypeScript for `packages/shared/src/api.ts`.

`ARCHITECTURE.md § 4`: the Pydantic models are the single source of truth, and no TS
type that mirrors one is written by hand. `scripts/gen-types.ts` runs this module,
formats the output with prettier and writes it (or, with `--check`, fails if the
committed file differs).

What is exported, in definition order:

* every `BaseModel` subclass *defined* in the schemas module → a `readonly` interface;
* every module-level `Literal[...]` alias → a `const` tuple plus a type derived from it
  (`EventType` → `EVENT_TYPES` and `type EventType`), so the renderer can validate
  against the same list it is typed with.

Fields are emitted as the **serialised** shape: a field with a default is still always
present in the JSON the core sends, so it is not optional in TS. Docstrings and field
descriptions become JSDoc; an alias is documented by a string literal directly after
its assignment. An annotation the generator does not understand is an error, never a
silent `unknown`.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import types
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel, JsonValue

from aegis_core.server import schemas

HEADER = """\
/**
 * GENERATED FILE — do not edit by hand.
 *
 * Source: `core/aegis_core/server/schemas.py`, rendered by `aegis_core.server.typegen`.
 * Regenerate with `pnpm gen:types`; `pnpm test` fails while this file is stale.
 */
"""


class TypeGenError(Exception):
    """A schema uses something the generator cannot express faithfully."""


def render(module: types.ModuleType = schemas) -> str:
    """Return the TypeScript source for every model and `Literal` alias in `module`."""
    models: dict[str, type[BaseModel]] = {}
    aliases: dict[str, object] = {}
    for name, value in vars(module).items():
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value.__module__ == module.__name__
        ):
            models[name] = value
        elif get_origin(value) is Literal:
            aliases[name] = value

    docs = _attribute_docstrings(module)
    blocks: list[str] = []
    for name, alias in aliases.items():
        blocks.append(_render_alias(name, alias, docs.get(name)))
    for model in models.values():
        blocks.append(_render_model(model, aliases, models))
    return HEADER + "".join(f"\n{block}" for block in blocks)


def const_name(alias_name: str) -> str:
    """`EventType` → `EVENT_TYPES`: the tuple that holds an alias' values."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", alias_name).upper() + "S"


def _render_alias(name: str, alias: object, doc: str | None) -> str:
    values = ", ".join(_literal(value, name) for value in get_args(alias))
    const = const_name(name)
    return (
        f"{_jsdoc(doc, '')}export const {const} = [{values}] as const;\n\n"
        f"export type {name} = (typeof {const})[number];\n"
    )


def _render_model(
    model: type[BaseModel], aliases: dict[str, object], models: dict[str, type[BaseModel]]
) -> str:
    lines = [f"{_jsdoc(model.__doc__, '')}export interface {model.__name__} {{\n"]
    for field_name, field in model.model_fields.items():
        where = f"{model.__name__}.{field_name}"
        wire_name = field.serialization_alias or field.alias or field_name
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", wire_name):
            raise TypeGenError(f"{where}: {wire_name!r} is not a plain TS identifier")
        ts_type = _ts_type(field.annotation, aliases, models, where)
        lines.append(f"{_jsdoc(field.description, '  ')}  readonly {wire_name}: {ts_type};\n")
    lines.append("}\n")
    return "".join(lines)


def _ts_type(
    annotation: object,
    aliases: dict[str, object],
    models: dict[str, type[BaseModel]],
    where: str,
) -> str:
    for name, alias in aliases.items():
        if annotation == alias:
            return name
    if annotation is JsonValue:
        return "unknown"
    if annotation is type(None) or annotation is None:
        return "null"
    if annotation is str:
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation is int or annotation is float:
        return "number"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if models.get(annotation.__name__) is not annotation:
            raise TypeGenError(f"{where}: {annotation.__name__} is not defined in the schemas")
        return annotation.__name__

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return " | ".join(_literal(value, where) for value in args)
    if origin is Union or origin is types.UnionType:
        return " | ".join(_ts_type(arg, aliases, models, where) for arg in args)
    if origin is list and len(args) == 1:
        return f"ReadonlyArray<{_ts_type(args[0], aliases, models, where)}>"
    if origin is dict and len(args) == 2 and args[0] is str:
        return f"Readonly<Record<string, {_ts_type(args[1], aliases, models, where)}>>"
    raise TypeGenError(f"{where}: unsupported annotation {annotation!r}")


def _literal(value: object, where: str) -> str:
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, str | int):
        return json.dumps(value, ensure_ascii=False)
    raise TypeGenError(f"{where}: unsupported literal value {value!r}")


def _jsdoc(text: str | None, indent: str) -> str:
    if not text:
        return ""
    body = inspect.cleandoc(text).replace("*/", "*\\/").splitlines()
    if len(body) == 1:
        return f"{indent}/** {body[0]} */\n"
    inner = "".join(f"{indent} *{' ' + line if line else ''}\n" for line in body)
    return f"{indent}/**\n{inner}{indent} */\n"


def _attribute_docstrings(module: types.ModuleType) -> dict[str, str]:
    """Map `Name = ...` to the string literal on the statement right after it."""
    tree = ast.parse(inspect.getsource(module))
    docs: dict[str, str] = {}
    for statement, following in zip(tree.body, tree.body[1:], strict=False):
        target: ast.expr | None = statement.target if isinstance(statement, ast.AnnAssign) else None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
        if (
            isinstance(target, ast.Name)
            and isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
            and isinstance(following.value.value, str)
        ):
            docs[target.id] = following.value.value
    return docs


def main() -> None:
    """Write the rendered source to stdout as UTF-8 with LF line endings."""
    sys.stdout.buffer.write(render().encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
