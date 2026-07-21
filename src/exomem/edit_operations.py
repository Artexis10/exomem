"""Discriminated edit operations and the one-release flat-call shim.

Public surfaces advertise :data:`EditOperation`. Every adapter and the shared
writer invocation boundary use the normalizers here so mutation identity and
the existing edit leaf see one stable payload regardless of compatibility shape.
"""

from __future__ import annotations

import warnings
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .multi_edit import normalize_edit_item


class _EditOperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SemanticEditOperation(_EditOperationModel):
    transition_token: str | None = None
    relation_disposition: str | None = None
    relation_review_hash: str | None = None
    relation_review_reason: str | None = None


class _GuardedSemanticEditOperation(_SemanticEditOperation):
    expected_hash: str | None = None


class ReplaceBodyOperation(_GuardedSemanticEditOperation):
    kind: Literal["replace_body"]
    new_body: str
    tags: list[str] | None = None


class ReplaceTagsOperation(_GuardedSemanticEditOperation):
    kind: Literal["replace_tags"]
    tags: list[str]


class ReplaceStringOperation(_GuardedSemanticEditOperation):
    kind: Literal["replace_string"]
    old_string: str
    new_string: str
    replace_all: bool = False
    tags: list[str] | None = None
    validate_only: bool = False


class BatchReplaceItem(_EditOperationModel):
    old_string: str
    new_string: str
    replace_all: bool = False

    @model_validator(mode="after")
    def reject_no_op(self) -> Self:
        if self.old_string == self.new_string:
            raise ValueError(
                "new_string must differ from old_string; batch edit is a no-op"
            )
        return self


ConnectorBatchReplaceItem = Annotated[
    BatchReplaceItem,
    BeforeValidator(normalize_edit_item),
]


class BatchReplaceOperation(_GuardedSemanticEditOperation):
    kind: Literal["batch_replace"]
    edits: Annotated[list[ConnectorBatchReplaceItem], Field(min_length=1)]
    validate_only: bool = False


class EditSectionOperation(_GuardedSemanticEditOperation):
    kind: Literal["edit_section"]
    heading: str
    new_string: str
    section_position: Literal["append", "prepend", "replace"] = "append"
    tags: list[str] | None = None


class PatchFrontmatterOperation(_SemanticEditOperation):
    kind: Literal["patch_frontmatter"]
    field: str
    value: Any
    allow_curated: bool = False
    validate_only: bool = False


class FillRowOperation(_EditOperationModel):
    kind: Literal["fill_row"]
    row_key: str
    take: str
    overwrite: bool = False


EditOperation = Annotated[
    ReplaceBodyOperation
    | ReplaceTagsOperation
    | ReplaceStringOperation
    | BatchReplaceOperation
    | EditSectionOperation
    | PatchFrontmatterOperation
    | FillRowOperation,
    Field(discriminator="kind"),
]

EDIT_OPERATION_ADAPTER = TypeAdapter(EditOperation)

LEGACY_EDIT_FIELDS = frozenset(
    {
        "new_body",
        "tags",
        "old_string",
        "new_string",
        "replace_all",
        "heading",
        "section_position",
        "edits",
        "row_key",
        "take",
        "overwrite",
        "field",
        "value",
        "allow_curated",
        "expected_hash",
        "validate_only",
        "transition_token",
        "relation_disposition",
        "relation_review_hash",
        "relation_review_reason",
    }
)

_TOP_LEVEL_EDIT_FIELDS = frozenset({"path", "why", "operation", "response_detail"})


def _inline_schema_references(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_schema_references(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        name = value["$ref"].rsplit("/", 1)[-1]
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        resolved = {**definitions[name], **siblings}
        return _inline_schema_references(resolved, definitions)
    return {
        key: _inline_schema_references(item, definitions)
        for key, item in value.items()
    }


def public_edit_operation_schema() -> dict[str, Any]:
    """Return a self-contained discriminator schema FastMCP can inline safely."""
    schema = EDIT_OPERATION_ADAPTER.json_schema()
    definitions = schema.pop("$defs")
    branches = [
        _inline_schema_references(branch, definitions) for branch in schema["oneOf"]
    ]
    return {
        "oneOf": branches,
        "discriminator": {"propertyName": "kind"},
    }


def _invalid_from_validation(operation: object, error: ValidationError) -> ValueError:
    kind = operation.get("kind") if isinstance(operation, dict) else None
    selected = f" operation kind `{kind}`" if kind is not None else " operation"
    guidance: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "operation"
        guidance.append(f"{location}: {item.get('msg', 'invalid value')}")
    return ValueError(f"INVALID_EDIT:{selected} is invalid: {'; '.join(guidance)}")


def _validate_operation(operation: object) -> _EditOperationModel:
    try:
        return EDIT_OPERATION_ADAPTER.validate_python(operation)
    except ValidationError as error:
        raise _invalid_from_validation(operation, error) from error


def _legacy_operation(legacy: dict[str, Any]) -> dict[str, Any]:
    modes: list[str] = []
    if "new_body" in legacy:
        modes.append("replace_body")
    if "edits" in legacy:
        modes.append("batch_replace")
    if any(field in legacy for field in ("row_key", "take", "overwrite")):
        modes.append("fill_row")
    if any(field in legacy for field in ("field", "value", "allow_curated")):
        modes.append("patch_frontmatter")
    section_selected = any(field in legacy for field in ("heading", "section_position"))
    if section_selected:
        modes.append("edit_section")
    if (
        any(field in legacy for field in ("old_string", "replace_all"))
        or ("new_string" in legacy and not section_selected)
    ):
        modes.append("replace_string")
    if not modes and "tags" in legacy:
        modes.append("replace_tags")

    if len(modes) > 1:
        raise ValueError(
            "INVALID_EDIT: ambiguous legacy edit modes: " + ", ".join(modes)
        )
    if not modes:
        raise ValueError(
            "INVALID_EDIT: legacy flat edit must select exactly one mode; use nested "
            "`operation` with kind replace_body, replace_tags, replace_string, "
            "batch_replace, edit_section, patch_frontmatter, or fill_row"
        )

    operation = {"kind": modes[0], **legacy}
    return operation


def operation_to_leaf_payload(operation: _EditOperationModel) -> dict[str, Any]:
    """Return the stable existing-leaf kwargs for one validated operation."""
    payload = operation.model_dump(exclude={"kind"}, exclude_none=True)
    if isinstance(operation, BatchReplaceOperation):
        payload["edits"] = [
            item.model_dump(exclude_defaults=True) for item in operation.edits
        ]
    if isinstance(operation, PatchFrontmatterOperation):
        # `None` is a meaningful explicit frontmatter value, not omission.
        payload["value"] = operation.value
    return payload


def _resolve_edit_operation(
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], _EditOperationModel]:
    raw = dict(arguments)
    unknown = set(raw) - _TOP_LEVEL_EDIT_FIELDS - LEGACY_EDIT_FIELDS
    if unknown:
        raise ValueError(
            "INVALID_EDIT: unknown top-level field(s): " + ", ".join(sorted(unknown))
        )

    has_operation = "operation" in raw
    legacy_names = sorted(set(raw) & LEGACY_EDIT_FIELDS)
    if has_operation and legacy_names:
        raise ValueError(
            "INVALID_EDIT: cannot combine nested `operation` with legacy flat fields: "
            + ", ".join(legacy_names)
        )

    if has_operation:
        operation_input = raw.pop("operation")
    else:
        legacy = {name: raw.pop(name) for name in legacy_names}
        operation_input = _legacy_operation(legacy)

    operation = _validate_operation(operation_input)
    if not has_operation:
        warnings.warn(
            "Flat edit_memory arguments are deprecated and retained for one "
            "compatibility release; send nested `operation` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return raw, operation


def normalize_edit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize public and legacy edit calls to the existing leaf payload."""
    raw, operation = _resolve_edit_operation(arguments)
    return {**raw, **operation_to_leaf_payload(operation)}


def normalize_edit_surface_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize an adapter request while retaining the nested public shape."""
    raw, operation = _resolve_edit_operation(arguments)
    nested = {"kind": operation.kind, **operation_to_leaf_payload(operation)}
    return {**raw, "operation": nested}
