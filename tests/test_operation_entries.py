"""Operation entries and `resolve_invocation`: the pure half of the single-purpose tools.

These tests need no server. They check that every operation a rendering carries resolves
to the grouped command and the flat arguments a direct call would send, that an operation
a rendering does not carry is refused with the grouped command's own refusal, and that the
startup coverage assertion rejects each way the entries can drift from the lease registry.
"""

from __future__ import annotations

import dataclasses

import pytest

from exomem import commands
from exomem.cli_ops import OpError
from exomem.command_surface import UnresolvedInvocation, resolve_invocation

CONVERTED = tuple(command for command in commands.PRODUCT_COMMANDS if command.renderings)
VARIANTS = [
    pytest.param(command, rendering, variant, id=f"{rendering.name}-{variant.action}-{variant.mode}")
    for command in CONVERTED
    for rendering in command.renderings
    for variant in rendering.variants
]

# Either/or rules that a variant's required fields do not state: the leaf checks them.
_LEAF_EITHER_OR = {
    ("record_memory", "append"): {"item": {}},
    ("record_memory", "update"): {"changes": {}},
    ("record_memory", "validate"): {"manifest_path": "Records/x/_collection.md"},
    ("plan_memory", "validate"): {"manifest_path": "Plans/x/_collection.md"},
    ("plan_memory", "update"): {"changes": {}},
}


def _fields(command, variant) -> dict[str, object]:
    fields: dict[str, object] = dict.fromkeys(variant.required, "x")
    fields.update(_LEAF_EITHER_OR.get((command.name, variant.action), {}))
    if variant.mode is not None:
        fields["import_request"] = {"mode": variant.mode}
    return fields


def _nested(kind: str, fields: dict[str, object]) -> dict[str, object]:
    return {"operation": {"kind": kind, **fields}}


def _flat(kind: str, fields: dict[str, object]) -> dict[str, object]:
    return {"action": kind, **fields}


@pytest.mark.parametrize("shape,build", [("nested", _nested), ("flat", _flat)])
@pytest.mark.parametrize("command,rendering,variant", VARIANTS)
def test_every_variant_resolves_to_its_grouped_command_and_flat_arguments(
    command, rendering, variant, shape, build
) -> None:
    fields = _fields(command, variant)

    resolved, arguments = resolve_invocation(
        rendering.name, build(variant.action, fields), commands.PRODUCT_COMMANDS, shape=shape
    )

    assert resolved.name == command.name
    assert arguments == {"action": variant.action, **fields}


def test_a_grouped_spelling_resolves_to_itself_with_its_arguments_untouched() -> None:
    arguments = {"action": "append", "collection": "c", "bogus": 1}

    resolved, flat = resolve_invocation("record_memory", arguments, commands.PRODUCT_COMMANDS)

    assert resolved.name == "record_memory"
    assert flat == arguments


@pytest.mark.parametrize(
    "name,kind,sibling",
    [
        ("read_records", "append", "change_records"),
        ("read_records", "revise", "change_records"),
        ("change_records", "query", "read_records"),
        ("read_plans", "triage", "change_plans"),
        ("change_plans", "history", "read_plans"),
    ],
)
def test_an_operation_the_rendering_does_not_carry_is_refused_naming_the_sibling(
    name, kind, sibling
) -> None:
    with pytest.raises(OpError) as refusal:
        resolve_invocation(name, _nested(kind, {"collection": "c"}), commands.PRODUCT_COMMANDS)

    assert refusal.value.code in {"INVALID_RECORD_ARGUMENTS", "INVALID_PLAN_ARGUMENTS"}
    assert f"send it to {sibling}" in refusal.value.message


@pytest.mark.parametrize(
    "request_body,sibling",
    [
        ({"mode": "start"}, "change_records"),
        ({"mode": "cancel"}, "change_records"),
        ({}, None),
        ({"mode": 7}, None),
        ({"mode": "Start"}, None),
    ],
)
def test_an_import_mode_outside_read_records_is_refused_before_it_reaches_a_lease(
    request_body, sibling
) -> None:
    arguments = _nested("import", {"collection": "c", "import_request": request_body})

    with pytest.raises(OpError) as refusal:
        resolve_invocation("read_records", arguments, commands.PRODUCT_COMMANDS)

    assert refusal.value.code == "INVALID_RECORD_ARGUMENTS"
    if sibling is not None:
        assert f"send it to {sibling}" in refusal.value.message
    else:
        assert "change_records" not in refusal.value.message
        assert "import_request.mode to be one of: preview, status" in refusal.value.message


def test_a_missing_import_request_is_refused_as_a_missing_argument() -> None:
    with pytest.raises(OpError) as refusal:
        resolve_invocation(
            "change_records", _nested("import", {"collection": "c"}), commands.PRODUCT_COMMANDS
        )

    assert refusal.value.code == "INVALID_RECORD_ARGUMENTS"


def test_a_field_of_another_operation_is_refused_with_the_kind_and_the_field() -> None:
    arguments = _nested(
        "append", {"collection": "c", "why": "w", "item": {}, "acknowledged_gap_codes": ["g"]}
    )

    with pytest.raises(OpError) as refusal:
        resolve_invocation("change_records", arguments, commands.PRODUCT_COMMANDS)

    assert "unexpected for append: acknowledged_gap_codes" in refusal.value.message


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation": "append"},
        {"operation": {"collection": "c"}},
        {"operation": {"kind": ["append"]}},
        {"operation": {"kind": "no_such_kind"}},
        {"operation": {"kind": "describe"}, "action": "describe"},
    ],
    ids=["no-operation", "operation-not-an-object", "no-kind", "kind-not-a-string", "unknown-kind", "extra-argument"],
)
def test_a_malformed_operation_fails_closed_with_the_grouped_refusal(arguments) -> None:
    with pytest.raises(OpError) as refusal:
        resolve_invocation("read_records", arguments, commands.PRODUCT_COMMANDS)

    assert refusal.value.code == "INVALID_RECORD_ARGUMENTS"


def test_an_unknown_name_fails_closed() -> None:
    with pytest.raises(UnresolvedInvocation):
        resolve_invocation("read_everything", {}, commands.PRODUCT_COMMANDS)


def test_the_grouped_shape_does_not_resolve_a_rendering_name() -> None:
    with pytest.raises(UnresolvedInvocation):
        resolve_invocation(
            "read_records", _nested("describe", {}), commands.PRODUCT_COMMANDS, shape="grouped"
        )


def test_transport_keys_and_success_detail_cross_untouched_and_a_read_takes_no_detail() -> None:
    credential = "authorization_session_credential"
    arguments = {
        **_nested("create", {"manifest_path": "p", "manifest_text": "t", "why": "w"}),
        "response_detail": "full",
        credential: "opaque",
    }

    _, flat = resolve_invocation(
        "change_records", arguments, commands.PRODUCT_COMMANDS, passthrough=frozenset({credential})
    )

    assert flat["response_detail"] == "full"
    assert flat[credential] == "opaque"
    with pytest.raises(OpError):
        resolve_invocation(
            "read_records",
            {**_nested("describe", {}), "response_detail": "full"},
            commands.PRODUCT_COMMANDS,
        )


def _records():
    return next(command for command in CONVERTED if command.name == "record_memory")


def _swap(command, index, **changes):
    renderings = list(command.renderings)
    renderings[index] = dataclasses.replace(renderings[index], **changes)
    return dataclasses.replace(command, renderings=tuple(renderings))


def test_startup_accepts_the_registered_entries() -> None:
    commands.validate_operation_entries()


def test_startup_fails_when_a_variant_is_missing() -> None:
    command = _records()
    read = command.renderings[0]

    with pytest.raises(RuntimeError, match=r"\('import', 'status'\) has no variant"):
        commands.validate_operation_entries([_swap(command, 0, variants=read.variants[:-1])])


def test_startup_fails_when_a_pair_has_two_variants() -> None:
    command = _records()
    read = command.renderings[0]

    with pytest.raises(RuntimeError, match=r"\('describe', None\) has 2 variants"):
        commands.validate_operation_entries(
            [_swap(command, 0, variants=(*read.variants, read.variants[0]))]
        )


def test_startup_fails_when_a_write_sits_in_a_read_tool() -> None:
    command = _records()
    read, change = command.renderings
    append = next(variant for variant in change.variants if variant.action == "append")
    moved = _swap(command, 0, variants=(*read.variants, append))
    moved = _swap(
        moved, 1, variants=tuple(variant for variant in change.variants if variant is not append)
    )

    with pytest.raises(RuntimeError, match=r"\('append', None\) is a write variant in read_records"):
        commands.validate_operation_entries([moved])


def test_startup_fails_when_the_lease_would_treat_a_read_variant_as_a_write() -> None:
    command = _records()
    read, change = command.renderings
    import_start = next(variant for variant in change.variants if variant.mode == "start")
    # `start` is declared a read here, so the lease's own classification must disagree.
    declared_read = dataclasses.replace(import_start, effect="read", destructive=False)
    moved = _swap(command, 0, variants=(*read.variants, declared_read))
    moved = _swap(
        moved, 1, variants=tuple(variant for variant in change.variants if variant is not import_start)
    )

    with pytest.raises(RuntimeError, match=r"\('import', 'start'\) in read_records classifies"):
        commands.validate_operation_entries([moved])
