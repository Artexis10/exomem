"""Relation-disposition remediation must be followable from every route.

Regression cover for the 2026-08-06 friction: an ordinary `edit_section` was blocked by
a relation-disposition finding whose remediation named `validate_only=true` and a draft
trio. None of those are `edit_memory` parameters — they belong to the creation writers —
so the caller went hunting for a field their operation did not have and switched to
`replace_string`, an unnatural operation for the edit they wanted, purely to reach it.

The fields the remediation names must exist on the operation that provoked it. The
remediation now names only the review fields every writer shares, and the text that
still spells a tool call (the validate route, the relation read) spells it for the
surface serving the call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from exomem import capabilities, edit_operations, semantic_contract
from exomem.edit_operations import EDIT_OPERATION_ADAPTER

# Every guarded edit kind that can provoke a relation-disposition finding.
GUARDED_KINDS = [
    {"kind": "replace_body", "new_body": "body", "expected_hash": "a" * 64},
    {"kind": "replace_tags", "tags": ["a"]},
    {"kind": "replace_string", "old_string": "a", "new_string": "b"},
    {"kind": "batch_replace", "edits": [{"old_string": "a", "new_string": "b"}]},
    {"kind": "edit_section", "heading": "## Notes", "new_string": "text"},
    {"kind": "patch_frontmatter", "field": "status", "value": "active"},
    {"kind": "fill_row", "row_key": "Film (2026)", "take": "Sharp."},
]

DISPOSITION_FIELDS = (
    "relation_disposition",
    "relation_review_hash",
    "relation_review_reason",
)


def _remediation(*, existing: bool = True) -> str:
    finding = semantic_contract._disposition_finding(
        SimpleNamespace(
            eligible_compiled=True,
            path="Knowledge Base/Notes/Insights/example.md",
            identity_kind="exomem_id",
        ),
        SimpleNamespace(satisfied=False, kind="stale"),
        existing=existing,
    )
    assert finding is not None
    text = finding.remediation
    assert "Link a related page" in text, "remediation text moved"
    return text


@pytest.mark.parametrize("payload", GUARDED_KINDS, ids=lambda p: p["kind"])
def test_every_guarded_kind_accepts_the_disposition_recovery_fields(payload):
    """The recovery the remediation names must be expressible on the blocked call."""
    operation = EDIT_OPERATION_ADAPTER.validate_python(
        {
            **payload,
            "relation_disposition": "reviewed_none",
            "relation_review_hash": "a" * 64,
            "relation_review_reason": "no qualifying relation applies",
        }
    )
    for field in DISPOSITION_FIELDS:
        assert hasattr(operation, field), f"{payload['kind']} cannot carry {field}"


@pytest.mark.parametrize("field", DISPOSITION_FIELDS)
def test_recovery_fields_live_on_the_shared_semantic_base(field):
    """Shared base, so a new edit kind inherits the recovery instead of forgetting it."""
    assert field in edit_operations._SemanticEditOperation.model_fields


def test_every_guarded_kind_accepts_validate_only():
    for payload in GUARDED_KINDS:
        operation = EDIT_OPERATION_ADAPTER.validate_python(
            {**payload, "validate_only": True}
        )
        assert operation.validate_only is True


def test_existing_remediation_names_only_fields_every_edit_route_has():
    """One call, same fields: no validate route, token or hash for the caller to hunt for."""
    text = _remediation()
    for token in ("relation_disposition", "relation_review_reason"):
        assert token in text
    for token in (
        "validate_only",
        "transition_token",
        "relation_review_hash",
        "draft_id",
        "draft_hash",
        "draft_token",
        "operation=validate",
    ):
        assert token not in text
    assert len(text.encode("utf-8")) <= 224


def test_creation_remediation_names_only_the_one_call_review_fields():
    text = _remediation(existing=False)
    for token in ("relation_disposition", "relation_review_reason"):
        assert token in text
    for token in ("validate_only", "draft_id", "draft_hash", "draft_token", "transition_token"):
        assert token not in text
    assert "creation" in text


def _nested() -> capabilities.ActiveSurfaceDescriptor:
    return capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="product",
        tier2_enabled=True,
        product_commands=(),
        tool_shape="nested",
    )


def test_the_validate_route_of_an_observe_write_is_spelled_for_the_serving_surface():
    """A nested surface has no `observe_memory(operation=validate)`; use `prepare_note`."""
    assert semantic_contract.validate_form("observe") == "observe_memory(operation='validate')"
    with capabilities.active_surface(_nested()):
        nested = semantic_contract.validate_form("observe")
    assert nested.startswith("prepare_note(")
    assert "observe_memory" not in nested
    # An operation whose validate is the shared `validate_only` parameter keeps that spelling.
    assert semantic_contract.validate_form("edit") == semantic_contract.VALIDATE_FORM


@pytest.mark.parametrize("code", sorted(semantic_contract.RELATION_QUALITY_CODES))
def test_a_relation_quality_advisory_names_a_relation_read_the_surface_serves(code):
    grouped = semantic_contract.relation_quality_advisory(code)["remediation"]
    assert "schema_memory(" in grouped
    with capabilities.active_surface(_nested()):
        nested = semantic_contract.relation_quality_advisory(code)["remediation"]
    assert "read_schema(" in nested
    assert "schema_memory" not in nested


def test_validate_only_lives_on_the_shared_semantic_base():
    assert "validate_only" in edit_operations._SemanticEditOperation.model_fields
    for payload in GUARDED_KINDS:
        operation = EDIT_OPERATION_ADAPTER.validate_python(payload)
        assert operation.validate_only is False
