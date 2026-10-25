"""Published MCP signatures for the renderings of a grouped command.

A rendering never runs: middleware rewrites its call to the grouped command before
dispatch (`command_surface.resolve_invocation`), so these signatures exist to tell a
client what to send. Every field's type and description is read from the grouped
command's own published signature, and every variant's fields from the operation
entries, so neither is declared a second time.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from pydantic import TypeAdapter, WithJsonSchema

from .edit_operations import inline_schema
from .operation_entries import OperationEntry, Rendering

_NULL = {"type": "null"}


def _without_null(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop the null arm of a field a variant requires, since null counts as not supplied."""
    arms = schema.get("anyOf")
    if not isinstance(arms, list) or _NULL not in arms:
        return schema
    rest = [arm for arm in arms if arm != _NULL]
    outer = {key: value for key, value in schema.items() if key != "anyOf"}
    return {**outer, **rest[0]} if len(rest) == 1 else {**outer, "anyOf": rest}


def _field_schema(parameter: inspect.Parameter, *, required: bool) -> dict[str, Any]:
    if parameter.annotation is inspect.Parameter.empty:
        return {}
    schema = inline_schema(TypeAdapter(parameter.annotation).json_schema())
    return _without_null(schema) if required else schema


def _variant_branch(
    rendering: Rendering, kind: str, parameters: Mapping[str, inspect.Parameter]
) -> dict[str, Any]:
    """The JSON schema of one operation kind: its own fields, and no others."""
    variant: OperationEntry = next(v for v in rendering.variants if v.action == kind)
    properties: dict[str, Any] = {"kind": {"const": kind, "type": "string"}}
    for name, parameter in parameters.items():
        if name in variant.fields:
            properties[name] = _field_schema(parameter, required=name in variant.required)
    axis = rendering.mode_axes.get(kind)
    if axis is not None:
        # The mode is part of the operation's identity, so the tool names the modes it carries.
        container = dict(properties[axis.argument])
        container["properties"] = {
            axis.key: {"enum": list(rendering.modes_of(kind)), "type": "string"}
        }
        container["required"] = [axis.key]
        properties[axis.argument] = container
    required = ["kind", *(name for name in parameters if name in variant.required)]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def operation_schema(
    rendering: Rendering, parameters: Mapping[str, inspect.Parameter]
) -> dict[str, Any]:
    """The `operation` object of a nested rendering: one branch per kind, selected by `kind`."""
    return {
        "description": "One operation, selected by `kind`.",
        "oneOf": [_variant_branch(rendering, kind, parameters) for kind in rendering.kinds],
        "discriminator": {"propertyName": "kind"},
    }


def rendering_handler(rendering: Rendering, grouped: Callable) -> Callable:
    """A callable whose signature FastMCP publishes as this rendering's input schema.

    `grouped` is the bound grouped command, whose published parameters supply each
    field's type and description.
    """
    parameters = {
        name: parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for name, parameter in inspect.signature(grouped).parameters.items()
    }
    carried = [parameters[name] for name in rendering.tool_arguments]
    schema = operation_schema(rendering, parameters)
    operation = inspect.Parameter(
        "operation",
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=Annotated[dict[str, Any], WithJsonSchema(schema)],
    )

    def handler(**_: object) -> typing.NoReturn:
        # Middleware rewrites every rendering call to its grouped command, so a call that
        # reaches this body skipped authorization's view of the real command: fail closed.
        raise RuntimeError(f"{rendering.name} reached its handler without translation")

    handler.__signature__ = inspect.Signature([operation, *carried])  # type: ignore[attr-defined]
    handler.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in handler.__signature__.parameters.values()  # type: ignore[attr-defined]
        if parameter.annotation is not inspect.Parameter.empty
    }
    handler.__name__ = handler.__qualname__ = rendering.name
    handler.__doc__ = rendering.description
    return handler
