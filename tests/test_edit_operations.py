from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from exomem.edit_operations import (
    EDIT_OPERATION_ADAPTER,
    EditOperation,
    normalize_edit_arguments,
    normalize_edit_surface_arguments,
)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            {"kind": "replace_body", "new_body": "After", "tags": ["new"]},
            {"new_body": "After", "tags": ["new"], "validate_only": False},
        ),
        (
            {"kind": "replace_tags", "tags": ["new"]},
            {"tags": ["new"], "validate_only": False},
        ),
        (
            {
                "kind": "replace_string",
                "old_string": "Before",
                "new_string": "After",
            },
            {
                "old_string": "Before",
                "new_string": "After",
                "replace_all": False,
                "validate_only": False,
            },
        ),
        (
            {
                "kind": "batch_replace",
                "edits": [{"old_string": "Before", "new_string": "After"}],
            },
            {
                "edits": [{"old_string": "Before", "new_string": "After"}],
                "validate_only": False,
            },
        ),
        (
            {"kind": "edit_section", "heading": "Claim", "new_string": "After"},
            {
                "heading": "Claim",
                "new_string": "After",
                "section_position": "append",
                "validate_only": False,
            },
        ),
        (
            {"kind": "patch_frontmatter", "field": "domain", "value": None},
            {
                "field": "domain",
                "value": None,
                "allow_curated": False,
                "validate_only": False,
            },
        ),
        (
            {"kind": "fill_row", "row_key": "Example", "take": "A view"},
            {
                "row_key": "Example",
                "take": "A view",
                "overwrite": False,
                "validate_only": False,
            },
        ),
    ],
)
def test_nested_variants_normalize_to_plain_leaf_payload(
    operation: dict, expected: dict
) -> None:
    assert normalize_edit_arguments(
        {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update", "operation": operation}
    ) == {
        "path": "Knowledge Base/Notes/Insights/example.md",
        "why": "update",
        **expected,
    }


def test_each_variant_forbids_fields_from_other_branches() -> None:
    adapter = TypeAdapter(EditOperation)

    with pytest.raises(ValidationError) as exc:
        adapter.validate_python(
            {
                "kind": "fill_row",
                "row_key": "Example",
                "take": "A view",
                "heading": "ignored-by-the-leaf",
            }
        )

    assert "fill_row" in str(exc.value)
    assert "heading" in str(exc.value)
    schema = adapter.json_schema()
    assert schema["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            kind: f"#/$defs/{model}"
            for kind, model in (
                ("replace_body", "ReplaceBodyOperation"),
                ("replace_tags", "ReplaceTagsOperation"),
                ("replace_string", "ReplaceStringOperation"),
                ("batch_replace", "BatchReplaceOperation"),
                ("edit_section", "EditSectionOperation"),
                ("patch_frontmatter", "PatchFrontmatterOperation"),
                ("fill_row", "FillRowOperation"),
            )
        },
    }


@pytest.mark.parametrize(
    ("legacy", "nested"),
    [
        ({"new_body": "After", "tags": ["new"]}, {"kind": "replace_body", "new_body": "After", "tags": ["new"]}),
        ({"tags": ["new"]}, {"kind": "replace_tags", "tags": ["new"]}),
        (
            {"old_string": "Before", "new_string": "After"},
            {"kind": "replace_string", "old_string": "Before", "new_string": "After"},
        ),
        (
            {"edits": [{"old_string": "Before", "new_string": "After"}]},
            {"kind": "batch_replace", "edits": [{"old_string": "Before", "new_string": "After"}]},
        ),
        (
            {"heading": "Claim", "new_string": "After"},
            {"kind": "edit_section", "heading": "Claim", "new_string": "After"},
        ),
        (
            {"field": "domain", "value": None},
            {"kind": "patch_frontmatter", "field": "domain", "value": None},
        ),
        (
            {"row_key": "Example", "take": "A view"},
            {"kind": "fill_row", "row_key": "Example", "take": "A view"},
        ),
    ],
)
def test_legacy_and_nested_forms_have_one_canonical_payload(
    legacy: dict, nested: dict
) -> None:
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update"}

    with pytest.warns(DeprecationWarning):
        normalized_legacy = normalize_edit_arguments({**common, **legacy})
    assert normalized_legacy == normalize_edit_arguments({**common, "operation": nested})


def test_batch_connector_json_objects_normalize_like_objects() -> None:
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update"}
    edit = {"old_string": "Before", "new_string": "After", "replace_all": True}

    encoded = normalize_edit_arguments(
        {**common, "operation": {"kind": "batch_replace", "edits": [json.dumps(edit)]}}
    )
    native = normalize_edit_arguments(
        {**common, "operation": {"kind": "batch_replace", "edits": [edit]}}
    )

    assert encoded == native
    assert encoded["edits"] == [edit]


@pytest.mark.parametrize(
    ("edits", "guidance"),
    [
        ([], "at least 1 item"),
        ([{"old_string": "Before"}], "new_string"),
        (
            [
                {
                    "old_string": "Before",
                    "new_string": "After",
                    "comment": "not consumed by the leaf",
                }
            ],
            "comment",
        ),
        ([{"old_string": "Same", "new_string": "Same"}], "must differ"),
        (["[]"], "old_string"),
    ],
)
def test_batch_items_fail_shared_validation_with_precise_guidance(
    edits: list, guidance: str
) -> None:
    with pytest.raises(ValueError, match="INVALID_EDIT") as exc:
        normalize_edit_arguments(
            {
                "path": "Knowledge Base/Notes/Insights/example.md",
                "why": "update",
                "operation": {"kind": "batch_replace", "edits": edits},
            }
        )

    assert guidance in str(exc.value)


@pytest.mark.parametrize(
    ("payload", "guidance"),
    [
        (
            {"operation": {"kind": "replace_tags", "tags": []}, "tags": []},
            "cannot combine nested `operation` with legacy flat fields: tags",
        ),
        (
            {"new_body": "After", "old_string": "Before", "new_string": "After"},
            "ambiguous legacy edit modes: replace_body, replace_string",
        ),
        (
            {"operation": {"kind": "replace_string", "old_string": "Before"}},
            "replace_string",
        ),
        (
            {"operation": {"kind": "fill_row", "row_key": "Example", "take": "A view", "heading": "Notes"}},
            "heading",
        ),
        ({"operation": {"kind": "nonsense"}}, "nonsense"),
        ({"take": "A view"}, "row_key"),
    ],
)
def test_invalid_calls_raise_precise_invalid_edit_before_any_leaf(
    payload: dict, guidance: str
) -> None:
    with pytest.raises(ValueError, match="INVALID_EDIT") as exc:
        normalize_edit_arguments(
            {
                "path": "Knowledge Base/Notes/Insights/example.md",
                "why": "update",
                **payload,
            }
        )

    assert guidance in str(exc.value)


def test_patch_frontmatter_requires_value_but_accepts_explicit_null() -> None:
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "clear domain"}

    with pytest.raises(ValueError, match=r"INVALID_EDIT.*value"):
        normalize_edit_arguments(
            {**common, "operation": {"kind": "patch_frontmatter", "field": "domain"}}
        )

    normalized = normalize_edit_arguments(
        {**common, "operation": {"kind": "patch_frontmatter", "field": "domain", "value": None}}
    )
    assert "value" in normalized
    assert normalized["value"] is None


def test_legacy_flat_form_is_marked_deprecated_for_one_release() -> None:
    with pytest.warns(DeprecationWarning, match="one compatibility release"):
        normalize_edit_arguments(
            {
                "path": "Knowledge Base/Notes/Insights/example.md",
                "why": "update",
                "old_string": "Before",
                "new_string": "After",
            }
        )


def test_shared_modifier_validate_only_top_level_and_nested() -> None:
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update"}
    operation = {
        "kind": "replace_string",
        "old_string": "Before",
        "new_string": "After",
    }

    # Top-level-only: a shared modifier selects no mode, so it must not be
    # treated as legacy/ambiguous, and it must fold into the operation.
    normalized = normalize_edit_arguments(
        {**common, "operation": operation, "validate_only": True}
    )
    assert normalized["validate_only"] is True

    # Both given and agreeing: fine, folds the same way.
    normalized_agree = normalize_edit_arguments(
        {
            **common,
            "operation": {**operation, "validate_only": True},
            "validate_only": True,
        }
    )
    assert normalized_agree["validate_only"] is True

    # Both given and disagreeing: errors clearly, naming the field.
    with pytest.raises(ValueError, match="INVALID_EDIT.*validate_only"):
        normalize_edit_arguments(
            {
                **common,
                "operation": {**operation, "validate_only": False},
                "validate_only": True,
            }
        )

    # Legacy flat call is unaffected: validate_only still folds into the
    # legacy operation exactly as before this modifier existed, and the call
    # still gets the deprecation warning every legacy flat call gets.
    with pytest.warns(DeprecationWarning):
        legacy_normalized = normalize_edit_arguments(
            {
                **common,
                "old_string": "Before",
                "new_string": "After",
                "validate_only": True,
            }
        )
    assert legacy_normalized["validate_only"] is True


def test_shared_modifier_matrix_survives_a_pre_validated_operation_instance() -> None:
    """On the real MCP call path, FastMCP's own pydantic validation turns the
    incoming `operation` JSON into an already-validated `_EditOperationModel`
    instance (e.g. `ReplaceStringOperation`) before `normalize_edit_surface_arguments`
    / `normalize_edit_arguments` ever sees it -- not a plain dict. Exercise the
    modifier merge/conflict logic with that exact shape via the repo's own
    canonical adapter, `EDIT_OPERATION_ADAPTER`, covering the same matrix as
    the dict-shaped test above.
    """
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update"}

    def validated_operation(**overrides: object) -> EditOperation:
        payload = {
            "kind": "replace_string",
            "old_string": "Before",
            "new_string": "After",
            **overrides,
        }
        return EDIT_OPERATION_ADAPTER.validate_python(payload)

    # Nested-only: no top-level modifier given, unaffected either way.
    nested_only = normalize_edit_arguments(
        {**common, "operation": validated_operation()}
    )
    assert nested_only["validate_only"] is False

    # Top-level-only: the pre-validated instance never mentioned validate_only
    # (not in its `model_fields_set`), so the top-level modifier must win
    # with no false conflict against the field's default value.
    top_level_only = normalize_edit_arguments(
        {**common, "operation": validated_operation(), "validate_only": True}
    )
    assert top_level_only["validate_only"] is True

    # Both given and agreeing: explicit nested True + top-level True, fine.
    both_agree = normalize_edit_arguments(
        {
            **common,
            "operation": validated_operation(validate_only=True),
            "validate_only": True,
        }
    )
    assert both_agree["validate_only"] is True

    # Both given and disagreeing: explicit nested False + top-level True
    # must error clearly, naming the field -- same as the dict-shaped path.
    with pytest.raises(ValueError, match="INVALID_EDIT.*validate_only"):
        normalize_edit_arguments(
            {
                **common,
                "operation": validated_operation(validate_only=False),
                "validate_only": True,
            }
        )


def test_surface_normalization_keeps_primary_shape_for_adapter_validation() -> None:
    nested = normalize_edit_surface_arguments(
        {
            "path": "Knowledge Base/Notes/Insights/example.md",
            "why": "update",
            "operation": {
                "kind": "replace_string",
                "old_string": "Before",
                "new_string": "After",
            },
            "response_detail": "full",
        }
    )

    assert nested == {
        "path": "Knowledge Base/Notes/Insights/example.md",
        "why": "update",
        "operation": {
            "kind": "replace_string",
            "old_string": "Before",
            "new_string": "After",
            "replace_all": False,
            "validate_only": False,
        },
        "response_detail": "full",
    }

    with pytest.warns(DeprecationWarning):
        legacy = normalize_edit_surface_arguments(
            {
                "path": "Knowledge Base/Notes/Insights/example.md",
                "why": "update",
                "old_string": "Before",
                "new_string": "After",
            }
        )
    assert legacy["operation"] == nested["operation"]


def test_product_metadata_and_bound_signature_advertise_only_primary_form() -> None:
    from exomem import command_surface
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "edit_memory"
    )
    assert [param.name for param in command.params] == [
        "path",
        "why",
        "operation",
        "validate_only",
        "response_detail",
    ]
    assert command.params[2].required is True

    bound = command_surface.bind_vault(command.leaf, Path("/vault"), command=command)
    signature = inspect.signature(bound)
    assert list(signature.parameters) == [
        "path",
        "why",
        "operation",
        "validate_only",
        "response_detail",
    ]
    assert signature.parameters["operation"].default is inspect.Parameter.empty


def test_python_runtime_accepts_nested_and_deprecated_flat_forms(monkeypatch) -> None:
    from exomem import commands

    calls: list[dict] = []
    monkeypatch.setattr(
        commands,
        "op_edit",
        lambda _vault, **kwargs: calls.append(kwargs) or {"path": kwargs["path"]},
    )
    common = {"path": "Knowledge Base/Notes/Insights/example.md", "why": "update"}

    nested = commands.op_edit_memory(
        Path("/vault"),
        **common,
        operation={"kind": "replace_body", "new_body": "After"},
    )
    with pytest.warns(DeprecationWarning):
        legacy = commands.op_edit_memory(Path("/vault"), **common, new_body="After")

    assert nested == legacy == {"path": common["path"]}
    assert calls == [
        {**common, "new_body": "After", "validate_only": False},
        {**common, "new_body": "After", "validate_only": False},
    ]
