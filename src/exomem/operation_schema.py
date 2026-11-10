"""The `operation` schema of one rendering of a grouped command: one branch per kind.

A rendering never runs: middleware rewrites its call to the grouped command before
dispatch (`command_surface.resolve_invocation`). Every field's type and description is
read from the grouped command's own published signature, and every variant's fields from
the operation entries, so neither is declared a second time.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import TypeAdapter

from .edit_operations import inline_schema
from .operation_entries import Rendering

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
    if not required and parameter.default is not inspect.Parameter.empty and parameter.default is not None:
        schema["default"] = parameter.default
    return _without_null(schema) if required else schema


def _constrain(
    branch: dict[str, Any], path: tuple[str, ...], values: list[bool | str | None],
    *, allow_omitted: bool,
) -> None:
    """Narrow one field path while retaining the grouped parameter's other properties."""
    name, *rest = path
    properties = branch.setdefault("properties", {})
    field = dict(properties.get(name, {}))
    properties[name] = field
    if not allow_omitted and name not in branch.setdefault("required", []):
        branch["required"].append(name)
    if rest:
        field["type"] = "object"
        field["properties"] = dict(field.get("properties", {}))
        field["required"] = list(field.get("required", []))
        _constrain(field, tuple(rest), values, allow_omitted=allow_omitted)
    else:
        # Literal constraints use JSON scalar types, a closed JSON Schema protocol set.
        scalar_type = "null" if values[0] is None else "boolean" if type(values[0]) is bool else "string"
        for keyword in ("anyOf", "oneOf", "const", "default"):
            field.pop(keyword, None)
        field.update(enum=values, type=scalar_type)


def _variant_branch(
    rendering: Rendering, kind: str, parameters: Mapping[str, inspect.Parameter]
) -> dict[str, Any]:
    """The JSON schema of one operation kind, including its constrained alternatives."""
    variants = [variant for variant in rendering.variants if variant.kind == kind]
    shapes = {}
    for variant in variants:
        shapes.setdefault((variant.fields, variant.required), []).append(variant)
    if len(shapes) > 1:
        return {"type": "object", "properties": {"kind": {"const": kind, "type": "string"}},
                "required": ["kind"], "anyOf": [
                    _variant_branch(replace(rendering, variants=tuple(group)), kind, parameters)
                    for group in shapes.values()
                ]}
    variant = variants[0]
    aliases = {leaf: wire for wire, leaf in rendering.field_aliases}
    properties: dict[str, Any] = {"kind": {"const": kind, "type": "string"}}
    for name, parameter in parameters.items():
        if name in variant.fields and name not in rendering.tool_arguments:
            properties[aliases.get(name, name)] = _field_schema(parameter, required=name in variant.required)
    if rendering.command == "schema_memory" and variant.action == "propose":
        # This registry operation takes a delta; other schema operations take whole documents.
        from .vocabulary.registry import delta_schema

        properties["proposal"] = delta_schema()
    branch = {
        "type": "object",
        "properties": properties,
        "required": ["kind", *(aliases.get(name, name) for name in parameters
                               if name in variant.required and name not in rendering.tool_arguments)],
        "additionalProperties": False,
    }
    constraints = [entry.constraints for entry in variants]
    if not any(constraints):
        return branch
    # One shared axis can use an enum; this also preserves the pilot's published schemas.
    shared_axis = all(
        len(items) == 1 and items[0].path == constraints[0][0].path
        and items[0].allow_omitted == constraints[0][0].allow_omitted
        and type(items[0].value) is type(constraints[0][0].value)
        for items in constraints
    ) if constraints[0] else False
    if shared_axis:
        constraint = constraints[0][0]
        path = (aliases.get(constraint.path[0], constraint.path[0]), *constraint.path[1:])
        _constrain(branch, path, [items[0].value for items in constraints], allow_omitted=constraint.allow_omitted or constraint.implied)
    elif len(variants) == 1:
        for constraint in variant.constraints:
            path = (aliases.get(constraint.path[0], constraint.path[0]), *constraint.path[1:])
            _constrain(branch, path, [constraint.value], allow_omitted=constraint.allow_omitted or constraint.implied)
    else:
        alternatives = []
        for items in constraints:
            alternative: dict[str, Any] = {}
            for constraint in items:
                path = (aliases.get(constraint.path[0], constraint.path[0]), *constraint.path[1:])
                _constrain(alternative, path, [constraint.value], allow_omitted=constraint.allow_omitted or constraint.implied)
            alternatives.append(alternative)
        branch["anyOf"] = alternatives
    return branch


def operation_schema(
    rendering: Rendering, parameters: Mapping[str, inspect.Parameter]
) -> dict[str, Any]:
    """The `operation` object of a nested rendering: one branch per kind, selected by `kind`."""
    branches = [_variant_branch(rendering, kind, parameters) for kind in rendering.kinds]
    if rendering.command == "govern_memory":
        from .governance.authorization_request import CredentialRule, credential_rule

        # These are the transport registry's closed credential-presence rules.
        descriptions = {
            CredentialRule.OPTIONAL: "A protected authorization-session credential is optional.",
            CredentialRule.REQUIRED: "A protected authorization-session credential is required.",
            CredentialRule.FORBIDDEN: "A protected authorization-session credential must be absent.",
            CredentialRule.NON_AUTHORIZING: "A session credential does not authorize this owner operation.",
        }
        for kind, branch in zip(rendering.kinds, branches, strict=True):
            variants = [entry for entry in rendering.variants if entry.kind == kind]
            rules = {credential_rule(rendering.command, rendering.arguments_for(entry)) for entry in variants}
            if len(rules) != 1:
                raise ValueError(f"{rendering.name}.{kind} mixes credential-presence rules")
            branch["description"] = descriptions[rules.pop()]
    return {
        "description": "One operation, selected by `kind`.",
        "oneOf": branches,
        "discriminator": {"propertyName": "kind"},
    }
