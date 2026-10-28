"""Synthetic calls exercise future discriminants without registering new public tools."""

from __future__ import annotations

import dataclasses
import inspect
import typing

import pytest
from jsonschema import Draft202012Validator

from exomem import operation_entries as entries
from exomem.command_surface import ArgumentRules, Command, resolve_invocation
from exomem.operation_schema import operation_schema


@pytest.fixture
def family(monkeypatch):
    def leaf(
        action: str,
        dry_run: bool = True,
        request: dict | None = None,
        kind: str | None = None,
        subject: str | None = None,
    ):
        raise AssertionError("normalization must not invoke the leaf")

    def refuse(detail):
        raise ValueError(detail)

    # These synthetic selector literals model a closed API, not a vault vocabulary.
    operations = entries.CommandOperations(
        command="synthetic_command",
        selector="action",
        entries=tuple(
            entries.OperationEntry(
                action="run",
                rendering=name,
                effect=effect,
                fields=frozenset({"dry_run", "request", "kind", "subject"}),
                required=frozenset(),
                constraints=(
                    entries.LiteralConstraint(("dry_run",), dry_run, allow_omitted=dry_run),
                    entries.LiteralConstraint(("request", "mode"), "check" if dry_run else "apply"),
                    entries.LiteralConstraint(("subject",), None, allow_omitted=True),
                ),
            )
            for name, effect, dry_run in (
                ("synthetic_read", "read", True),
                ("synthetic_write", "write", False),
            )
        ),
        renderings=tuple(
            entries.RenderingSpec(
                name,
                effect,
                name,
                name,
                field_aliases=(("unit_kind", "kind"), ("file_kind", "subject")),
            )
            for name, effect in (("synthetic_read", "read"), ("synthetic_write", "write"))
        ),
    )
    monkeypatch.setattr(entries, "OPERATIONS", {operations.command: operations})
    command = Command(
        name=operations.command,
        leaf=leaf,
        params=(),
        surfaces=frozenset(),
        renderings=operations.renderings_with_entries(),
        argument_rules=ArgumentRules(validate=lambda action, fields: None, refuse=refuse),
    )
    yield command


@pytest.mark.parametrize("extra", [{}, {"dry_run": True}, {"subject": None}])
def test_constraints_and_alias_preserve_explicit_values_and_omission(family, extra):
    grouped = {"action": "run", "request": {"mode": "check", "payload": 7}, "kind": "fact", **extra}

    tool, wire = entries.spell_call(family.name, grouped, "nested")
    command, flat = resolve_invocation(tool, wire, [family])

    assert tool == "synthetic_read"
    assert wire["operation"]["kind"] == "run"
    assert wire["operation"]["unit_kind"] == "fact"
    assert (command.name, flat) == (family.name, grouped)
    assert entries.unspell_call(tool, wire, "nested") == (family.name, grouped)
    assert entries.unspell_call(family.name, grouped, "grouped") == (family.name, grouped)


@pytest.mark.parametrize(
    "changes",
    [
        {"dry_run": False},
        {"dry_run": 1},
        {"dry_run": None},
        {"request": {}},
        {"request": None},
        {"file_kind": "other"},
    ],
)
def test_wrong_missing_or_malformed_constraint_never_resolves_to_read(family, changes):
    wire = {"operation": {"kind": "run", "request": {"mode": "check"}, **changes}}

    with pytest.raises(ValueError):
        resolve_invocation("synthetic_read", wire, [family], strict=False)
    assert entries.unspell_call("synthetic_read", wire, "nested") is None


def test_opposite_effect_call_names_the_write_rendering(family):
    wire = {"operation": {"kind": "run", "dry_run": False, "request": {"mode": "apply"}}}

    with pytest.raises(ValueError, match="send it to synthetic_write"):
        resolve_invocation("synthetic_read", wire, [family])
    assert resolve_invocation("synthetic_write", wire, [family])[1] == {
        "action": "run",
        "dry_run": False,
        "request": {"mode": "apply"},
    }


def test_alias_collision_refuses_instead_of_losing_a_grouped_argument(family):
    grouped = {
        "action": "run",
        "request": {"mode": "check"},
        "kind": "fact",
        "unit_kind": "different",
    }

    with pytest.raises(ValueError, match="conflict"):
        entries.spell_call(family.name, grouped, "nested")
    wire = {
        "operation": {
            "kind": "run",
            "request": {"mode": "check"},
            "subject": None,
            "file_kind": None,
        }
    }
    with pytest.raises(ValueError, match="conflict"):
        resolve_invocation("synthetic_read", wire, [family])
    assert entries.unspell_call("synthetic_read", wire, "nested") is None


def test_schema_and_dispatch_agree_on_multiple_constraints_and_alias(family):
    rendering = family.renderings[0]
    schema = operation_schema(
        rendering,
        {
            name: parameter.replace(annotation=typing.get_type_hints(family.leaf)[name])
            for name, parameter in inspect.signature(family.leaf).parameters.items()
        },
    )
    validator = Draft202012Validator(schema)
    valid = {"kind": "run", "request": {"mode": "check"}, "unit_kind": "fact"}

    assert validator.is_valid(valid)
    assert validator.is_valid({**valid, "dry_run": True, "file_kind": None})
    for changes in (
        {"dry_run": 1},
        {"dry_run": False},
        {"dry_run": None},
        {"request": {}},
        {"file_kind": "other"},
    ):
        assert not validator.is_valid({**valid, **changes})


def test_kind_can_carry_disjoint_constraint_alternatives(family):
    rendering = family.renderings[0]
    first = rendering.variants[0]
    second = dataclasses.replace(
        first,
        constraints=(
            entries.LiteralConstraint(("dry_run",), False),
            entries.LiteralConstraint(("request", "mode"), "inspect"),
        ),
    )
    rendering = dataclasses.replace(rendering, variants=(first, second))
    validator = Draft202012Validator(
        operation_schema(
            rendering,
            {
                name: parameter.replace(annotation=typing.get_type_hints(family.leaf)[name])
                for name, parameter in inspect.signature(family.leaf).parameters.items()
            },
        )
    )

    assert validator.is_valid({"kind": "run", "request": {"mode": "check"}})
    assert validator.is_valid({"kind": "run", "dry_run": False, "request": {"mode": "inspect"}})
    assert not validator.is_valid({"kind": "run", "dry_run": False, "request": {"mode": "check"}})


def test_coverage_rejects_constraints_that_overlap_on_omitted_defaults(family):
    first = dataclasses.replace(
        family.renderings[0].variants[0],
        constraints=(entries.LiteralConstraint(("dry_run",), True, allow_omitted=True),),
    )
    second = dataclasses.replace(
        first, constraints=(entries.LiteralConstraint(("dry_run",), False, allow_omitted=True),)
    )
    rendering = dataclasses.replace(family.renderings[0], variants=(first, second))

    with pytest.raises(RuntimeError, match="overlap"):
        entries.assert_coverage(
            family.name,
            [rendering],
            actions=["run"],
            modes={},
            is_read_only=lambda rendering, variant: True,
        )


def test_coverage_rejects_an_omitted_default_with_the_opposite_effect(family):
    variant = dataclasses.replace(
        family.renderings[0].variants[0],
        constraints=(entries.LiteralConstraint(("dry_run",), True, allow_omitted=True),),
    )
    rendering = dataclasses.replace(family.renderings[0], variants=(variant,))

    with pytest.raises(RuntimeError, match="effect when omitted"):
        entries.assert_coverage(
            family.name,
            [rendering],
            actions=["run"],
            modes={},
            is_read_only=lambda rendering, variant: (
                rendering.arguments_for(variant).get("dry_run") is True
            ),
        )


@pytest.mark.parametrize(
    "constraint,accepted,rejected",
    [
        (
            entries.LiteralConstraint(("dry_run",), True, allow_omitted=True),
            [{}, {"dry_run": True}],
            [{"dry_run": 1}, {"dry_run": None}, {"dry_run": False}],
        ),
        (
            entries.LiteralConstraint(("subject",), None),
            [{"file_kind": None}],
            [{}, {"file_kind": "other"}],
        ),
    ],
)
def test_scalar_discriminants_publish_the_same_admission_as_dispatch(
    family, constraint, accepted, rejected
):
    rendering = dataclasses.replace(
        family.renderings[0],
        variants=(
            dataclasses.replace(family.renderings[0].variants[0], constraints=(constraint,)),
        ),
    )
    command = dataclasses.replace(family, renderings=(rendering,))
    parameters = {
        name: parameter.replace(annotation=typing.get_type_hints(family.leaf)[name])
        for name, parameter in inspect.signature(family.leaf).parameters.items()
    }
    validator = Draft202012Validator(operation_schema(rendering, parameters))

    for fields in accepted:
        operation = {"kind": "run", **fields}
        assert validator.is_valid(operation)
        assert resolve_invocation(rendering.name, {"operation": operation}, [command])[1] == {
            "action": "run",
            **rendering.leaf_fields(fields),
        }
    for fields in rejected:
        operation = {"kind": "run", **fields}
        assert not validator.is_valid(operation)
        with pytest.raises(ValueError):
            resolve_invocation(rendering.name, {"operation": operation}, [command])
