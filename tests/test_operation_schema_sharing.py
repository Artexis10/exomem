"""Shared operation fields retain each variant's admission and complete schema."""

from dataclasses import replace
from typing import Annotated, Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import WithJsonSchema

from exomem import operation_schema
from exomem.operation_entries import LiteralConstraint, OperationEntry, Rendering


def _rendering(kind, fields, *, required=(), constraints=()):
    return Rendering(
        name="read_example", command="synthetic", selector="action", effect="read",
        title="Read example", description="Read an example.",
        variants=(OperationEntry(
            action=kind, rendering="read_example", effect="read",
            fields=frozenset(fields), required=frozenset(required), constraints=constraints,
        ),),
    )


def test_shared_fields_keep_required_defaults_null_items_and_sibling_only_refusals():
    limit_schema = Annotated[int, WithJsonSchema({"type": "integer", "minimum": 1, "description": "Maximum returned rows."})]

    def first(action: str, path: str, first_only: str | None = None,
              limit: limit_schema = 7, tokens: list[str | None] | None = None):
        pass

    def second(action: str, path: str, second_only: str | None = None,
               limit: limit_schema = 7, tokens: list[str | None] | None = None):
        pass

    tool = operation_schema.shared_rendering_tool([
        (_rendering("first", {"path", "first_only", "limit", "tokens"}, required={"path"}), first),
        (_rendering("second", {"path", "second_only", "limit", "tokens"}, required={"path"}), second),
    ])
    operation = tool.parameters["properties"]["operation"]
    assert operation["properties"]["limit"] == {
        "type": "integer", "minimum": 1, "description": "Maximum returned rows.", "default": 7,
    }
    assert all(branch["properties"]["path"] == {} for branch in operation["oneOf"])
    validator = Draft202012Validator(tool.parameters)
    assert validator.is_valid({"operation": {"kind": "first", "path": "a", "tokens": [None, "x"]}})
    assert validator.is_valid({"operation": {"kind": "second", "path": "b", "second_only": "y"}})
    for fields in ({}, {"path": None}, {"path": "a", "tokens": None}, {"path": "a", "second_only": "x"}):
        assert not validator.is_valid({"operation": {"kind": "first", **fields}})


def test_hidden_union_alternatives_keep_their_different_nested_field_schemas():
    def ordinary(action: str, request: Annotated[dict, WithJsonSchema({
        "type": "object", "properties": {"mode": {"const": "ordinary"}, "path": {"type": "integer"}},
        "required": ["mode"], "additionalProperties": False,
    })], path: str):
        pass

    def conditional(action: str, request: dict, path: str, extra: str | None = None):
        pass

    first = _rendering("first", {"path", "request"}, required={"path", "request"})
    second = _rendering("second", {"path", "request"}, required={"path", "request"})
    left = _rendering("conditional", {"path", "request"}, required={"path", "request"},
                      constraints=(LiteralConstraint(("request", "mode"), "left"),))
    right = replace(left.variants[0], fields=frozenset({"path", "request", "extra"}),
                    constraints=(LiteralConstraint(("request", "mode"), "right"),))
    conditional_rendering = replace(left, variants=(*left.variants, right))
    tool = operation_schema.shared_rendering_tool([
        (first, ordinary), (second, ordinary), (conditional_rendering, conditional),
    ])
    operation = tool.parameters["properties"]["operation"]
    assert "path" in operation["properties"]
    assert "request" not in operation["properties"]
    validator = Draft202012Validator(tool.parameters)
    for mode in ("left", "right"):
        assert validator.is_valid({"operation": {"kind": "conditional", "path": "a", "request": {"mode": mode}}})
    assert not validator.is_valid({"operation": {"kind": "conditional", "path": "a", "request": {"mode": "other"}}})
    assert not validator.is_valid({"operation": {"kind": "first", "path": "a", "request": {"mode": "left"}}})


def test_boolean_and_integer_defaults_do_not_become_one_shared_annotation():
    def boolean(action: str, value: Any = False):
        pass

    def integer(action: str, value: Any = 0):
        pass

    tool = operation_schema.shared_rendering_tool([
        (_rendering("boolean", {"value"}), boolean), (_rendering("integer", {"value"}), integer),
    ])
    operation = tool.parameters["properties"]["operation"]
    assert "value" not in operation.get("properties", {})
    first, second = operation["oneOf"]
    assert first["properties"]["value"]["default"] is False
    assert type(second["properties"]["value"]["default"]) is int
    assert second["properties"]["value"]["default"] == 0


@pytest.mark.parametrize("open_fields", [{}, {"additionalProperties": False, "patternProperties": {".*": {}}}])
def test_an_open_operation_leaf_does_not_inherit_a_siblings_field_constraint(monkeypatch, open_fields):
    def leaf(action: str):
        pass

    branches = [{
        "type": "object", "properties": {"kind": {"const": kind}, "value": {"type": "string"}},
        "required": ["kind"], "additionalProperties": False,
    } for kind in ("first", "second")]
    branches.append({"type": "object", "properties": {"kind": {"const": "open"}},
                     "required": ["kind"], **open_fields})
    monkeypatch.setattr(operation_schema, "operation_schema", lambda *_: {"oneOf": branches})
    tool = operation_schema.shared_rendering_tool([(_rendering("example", ()), leaf)])
    validator = Draft202012Validator(tool.parameters)
    assert validator.is_valid({"operation": {"kind": "open", "value": 4}})
    assert not validator.is_valid({"operation": {"kind": "first", "value": 4}})
    assert "value" not in tool.parameters["properties"]["operation"].get("properties", {})
