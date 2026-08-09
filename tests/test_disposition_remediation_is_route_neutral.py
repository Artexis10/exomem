"""Relation-disposition remediation must be followable from every route.

Regression cover for the 2026-08-06 friction: an ordinary `edit_section` was blocked by
a relation-disposition finding whose remediation named `validate_only=true` and a draft
trio. None of those are `edit_memory` parameters — they belong to the creation writers —
so the caller went hunting for a field their operation did not have and switched to
`replace_string`, an unnatural operation for the edit they wanted, purely to reach it.

The fields the remediation names must exist on the operation that provoked it.
"""

from __future__ import annotations

import pytest

from exomem import edit_operations
from exomem.edit_operations import EDIT_OPERATION_ADAPTER

# Every guarded edit kind that can provoke a relation-disposition finding.
GUARDED_KINDS = [
    {"kind": "replace_body", "new_body": "body"},
    {"kind": "replace_tags", "tags": ["a"]},
    {"kind": "replace_string", "old_string": "a", "new_string": "b"},
    {"kind": "batch_replace", "edits": [{"old_string": "a", "new_string": "b"}]},
    {"kind": "edit_section", "heading": "## Notes", "new_string": "text"},
]

DISPOSITION_FIELDS = (
    "relation_disposition",
    "relation_review_hash",
    "relation_review_reason",
)


def _remediation() -> str:
    from exomem import semantic_contract

    source = semantic_contract._disposition_finding.__code__.co_consts
    text = " ".join(c for c in source if isinstance(c, str))
    assert "qualifying typed relation" in text, "remediation text moved"
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


def test_remediation_does_not_name_validate_only():
    """`validate_only` previews a surgical match; three of five guarded kinds lack it."""
    assert "validate_only" not in _remediation()


def test_remediation_qualifies_the_creation_only_draft_trio():
    """Naming the trio unconditionally is what sent an edit caller off-route."""
    text = _remediation()
    for token in ("draft_id", "draft_hash", "draft_token"):
        assert token in text, "the creation path still needs these"
    assert "Creation writers" in text, "the trio must be marked creation-only"


def test_remediation_names_only_universally_available_fields_unqualified():
    """Everything before the creation-writer caveat must exist on every guarded kind."""
    unqualified = _remediation().split("Creation writers")[0]
    for field in DISPOSITION_FIELDS:
        assert field in unqualified

    edit_section = EDIT_OPERATION_ADAPTER.validate_python(
        {"kind": "edit_section", "heading": "## Notes", "new_string": "text"}
    )
    named = {
        token.strip('",=')
        for token in unqualified.replace("(", " ").replace(")", " ").split()
        if token.strip('",=').isidentifier()
    }
    creation_only = {"draft_id", "draft_hash", "draft_token", "validate_only"}
    assert not (named & creation_only), (
        "the unqualified half names a field the edit route does not have"
    )
    assert edit_section.relation_disposition is None


def test_validate_only_remains_surgical_only():
    """Guard the boundary: it must not quietly acquire a second meaning."""
    surgical = {"replace_string", "batch_replace"}
    for payload in GUARDED_KINDS:
        operation = EDIT_OPERATION_ADAPTER.validate_python(payload)
        has_field = "validate_only" in type(operation).model_fields
        assert has_field == (payload["kind"] in surgical), payload["kind"]
