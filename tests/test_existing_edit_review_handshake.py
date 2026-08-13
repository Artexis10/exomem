"""Existing edit validation must describe and commit one exact after-state."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import commands, relation_review, semantic_contract, temporal, vault

PAGE_ID = "00000000-0000-4000-8000-000000000091"
PAGE = "Knowledge Base/Notes/Research/edit-review-handshake.md"
ABERDEEN = (
    "Knowledge Base/Notes/Research/Travel/"
    "rita-aberdeen-trip-accommodation-strategy-august-2026.md"
)
REVIEW_REASON = "No honest typed relation applies to this isolated transition."


def _source(updated: str | None, *, title: str = "Edit review handshake") -> str:
    updated_line = f"updated: {updated}\n" if updated is not None else ""
    return (
        "---\n"
        f"title: {title}\n"
        "type: research-note\n"
        "status: active\n"
        f"{updated_line}"
        f"exomem_id: {PAGE_ID}\n"
        "tags: [before]\n"
        "---\n\n"
        "## Observations\n\n"
        "- [finding] Before marker remains reviewable #memory\n\n"
        "## Notes\n\n"
        "Before section.\n\n"
        "- Film (2026) [take: ]\n\n"
        "## Relations\n"
    )


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "replace_body", "new_body": "# Template\n\nAfter body.\n"},
        {
            "kind": "replace_string",
            "old_string": "Before marker",
            "new_string": "After marker",
        },
        {
            "kind": "batch_replace",
            "edits": [
                {"old_string": "Before marker", "new_string": "After marker"},
                {"old_string": "Before section", "new_string": "After section"},
            ],
        },
        {
            "kind": "edit_section",
            "heading": "Notes",
            "new_string": "After section.",
            "section_position": "replace",
        },
    ],
)
def test_frontmatterless_body_operations_validate_and_commit_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: dict
) -> None:
    relative = "Knowledge Base/Templates/ordinary-template.md"
    source = "# Template\n\nBefore marker\n\n## Notes\n\nBefore section\n"
    page = _write(tmp_path, relative, source)
    ticks = iter(
        [
            dt.datetime(2026, 8, 10, 12, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 10, 12, 0, 1, tzinfo=dt.UTC),
        ]
    )
    monkeypatch.setattr(temporal, "now", lambda: next(ticks))

    preview = commands.op_edit_memory(
        tmp_path,
        path=relative,
        why="validate template edit",
        operation={**operation, "validate_only": True},
    )
    semantic = preview["semantic"]
    committed = commands.op_edit_memory(
        tmp_path,
        path=relative,
        why="commit template edit",
        operation={
            **operation,
            "transition_token": semantic["transition_token"],
        },
    )

    committed_source = page.read_text(encoding="utf-8")
    assert committed["semantic"]["mutated"] is True
    assert semantic["after_hash"] == vault.content_hash(committed_source)
    assert not committed_source.startswith("---\n")
    assert "updated:" not in committed_source


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "replace_tags", "tags": ["template"]},
    ],
)
def test_frontmatter_operations_refuse_resolved_frontmatterless_paths(
    tmp_path: Path, operation: dict
) -> None:
    relative = "Knowledge Base/Templates/ordinary-template.md"
    _write(tmp_path, relative, "# Template\n")

    with pytest.raises(ValueError) as exc:
        commands.op_edit_memory(
            tmp_path,
            path=relative,
            why="metadata requires frontmatter",
            operation=operation,
        )

    assert "FRONTMATTER_REQUIRED" in str(exc.value)
    assert "(missing: ['path'])" not in str(exc.value)


def _save_current_review(root: Path, source: str) -> None:
    decision = relation_review.build_lifecycle_decision(
        page_identity=PAGE_ID,
        after_fingerprint=semantic_contract.review_content_fingerprint(PAGE_ID, source),
        reason="The current page has no honest typed relation.",
    )
    path = relation_review.lifecycle_decision_path(
        root, PAGE_ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        relation_review.serialize_lifecycle_decision(decision),
        encoding="utf-8",
        newline="\n",
    )


def _operation(kind: str) -> dict:
    operations = {
        "replace_body": {
            "kind": "replace_body",
            "new_body": (
                "## Observations\n\n"
                "- [finding] After body remains reviewable #memory\n\n"
                "## Notes\n\nAfter body.\n\n"
                "- Film (2026) [take: ]\n\n"
                "## Relations\n"
            ),
        },
        "replace_tags": {"kind": "replace_tags", "tags": ["after"]},
        "replace_string": {
            "kind": "replace_string",
            "old_string": "Before marker",
            "new_string": "After marker",
        },
        "batch_replace": {
            "kind": "batch_replace",
            "edits": [
                {"old_string": "Before marker", "new_string": "After marker"},
                {"old_string": "Before section", "new_string": "After section"},
            ],
        },
        "edit_section": {
            "kind": "edit_section",
            "heading": "Notes",
            "new_string": "After section.\n\n- Film (2026) [take: ]",
            "section_position": "replace",
        },
        "patch_frontmatter": {
            "kind": "patch_frontmatter",
            "field": "status",
            "value": "active",
        },
        "fill_row": {
            "kind": "fill_row",
            "row_key": "Film (2026)",
            "take": "Sharp.",
        },
    }
    return dict(operations[kind])


@pytest.mark.parametrize(
    "updated",
    [None, "2026-08-09", "2026-08-10T12:00:00Z"],
    ids=["missing-updated", "stale-updated", "current-updated"],
)
@pytest.mark.parametrize(
    "kind",
    [
        "replace_body",
        "replace_tags",
        "replace_string",
        "batch_replace",
        "edit_section",
        "patch_frontmatter",
        "fill_row",
    ],
)
def test_every_edit_kind_commits_the_exact_validated_bytes_across_a_clock_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    updated: str | None,
) -> None:
    source = _source(updated)
    page = _write(tmp_path, PAGE, source)
    _save_current_review(tmp_path, source)
    ticks = iter(
        [
            dt.datetime(2026, 8, 10, 12, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 10, 12, 0, 1, tzinfo=dt.UTC),
        ]
    )
    monkeypatch.setattr(temporal, "now", lambda: next(ticks))

    preview_operation = {**_operation(kind), "validate_only": True}
    preview = commands.op_edit_memory(
        tmp_path,
        path=PAGE,
        why=f"validate {kind}",
        operation=preview_operation,
    )
    semantic = preview["semantic"]
    assert semantic["contract_result"]["should_block"] is True

    commit_operation = {
        **_operation(kind),
        "transition_token": semantic["transition_token"],
        "relation_disposition": "reviewed_none",
        "relation_review_hash": semantic["transition_hash"],
        "relation_review_reason": REVIEW_REASON,
    }
    committed = commands.op_edit_memory(
        tmp_path,
        path=PAGE,
        why=f"commit {kind}",
        operation=commit_operation,
    )

    assert committed["semantic"]["mutated"] is True
    committed_source = page.read_text(encoding="utf-8")
    assert semantic.get("after_hash") == vault.content_hash(committed_source)
    assert semantic.get("before_hash") == vault.content_hash(source)
    assert semantic.get("relation_review_hash") == semantic["transition_hash"]
    assert "updated: 2026-08-10T12:00:00Z" in committed_source


def test_validation_with_review_intent_returns_the_hash_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("2026-08-09")
    _write(tmp_path, PAGE, source)
    _save_current_review(tmp_path, source)
    monkeypatch.setattr(
        temporal,
        "now",
        lambda: dt.datetime(2026, 8, 10, 12, 0, 0, tzinfo=dt.UTC),
    )
    operation = _operation("replace_string")
    initial = commands.op_edit_memory(
        tmp_path,
        path=PAGE,
        why="initial validation",
        operation={**operation, "validate_only": True},
    )["semantic"]

    reviewed = commands.op_edit_memory(
        tmp_path,
        path=PAGE,
        why="validate review intent",
        operation={
            **operation,
            "validate_only": True,
            "transition_token": initial["transition_token"],
            "relation_disposition": "reviewed_none",
            "relation_review_reason": REVIEW_REASON,
        },
    )["semantic"]

    assert reviewed["contract_result"]["should_block"] is False
    assert reviewed.get("relation_review_hash") == reviewed["transition_hash"]


def test_commit_still_rejects_the_wrong_relation_review_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("2026-08-09")
    _write(tmp_path, PAGE, source)
    _save_current_review(tmp_path, source)
    monkeypatch.setattr(
        temporal,
        "now",
        lambda: dt.datetime(2026, 8, 10, 12, 0, 0, tzinfo=dt.UTC),
    )
    operation = _operation("replace_string")
    semantic = commands.op_edit_memory(
        tmp_path,
        path=PAGE,
        why="validate mismatch guard",
        operation={**operation, "validate_only": True},
    )["semantic"]

    with pytest.raises(ValueError, match="LIFECYCLE_TRANSITION_REVIEW_MISMATCH"):
        commands.op_edit_memory(
            tmp_path,
            path=PAGE,
            why="reject wrong review hash",
            operation={
                **operation,
                "transition_token": semantic["transition_token"],
                "relation_disposition": "reviewed_none",
                "relation_review_hash": "0" * 64,
                "relation_review_reason": REVIEW_REASON,
            },
        )


def test_semantic_transition_error_has_no_fake_missing_suffix(tmp_path: Path) -> None:
    _write(tmp_path, PAGE, _source("2026-08-09"))

    with pytest.raises(ValueError) as exc:
        commands.op_edit_memory(
            tmp_path,
            path=PAGE,
            why="reject invalid transition token",
            operation={
                **_operation("replace_string"),
                "transition_token": "not-a-transition-token",
            },
        )

    assert "LIFECYCLE_TRANSITION" in str(exc.value)
    assert "(missing:" not in str(exc.value)


def test_full_bootstrap_and_edit_description_teach_both_recovery_paths(
    vault: Path,
) -> None:
    bootstrap = commands.op_bootstrap(vault, profile="full")
    full = json.dumps(bootstrap)
    description = commands.op_edit_memory.__doc__ or ""

    for text in (full, description):
        assert "relation_review_hash" in text
        assert "transition_token" in text
        assert "- supports [[Knowledge Base/Notes/Research/example-target]]" in text
        assert "supports:: [[" in text
        assert "not" in text.lower() or "unsupported" in text.lower()
    reviewed = bootstrap["authoring_contract"]["reviewed_existing_edit"]
    assert reviewed["validate_call"]["tool"] == "edit_memory"
    assert reviewed["commit_call"]["tool"] == "edit_memory"
    assert reviewed["validate_call"]["arguments"]["operation"]["validate_only"] is True
    assert (
        reviewed["commit_call"]["arguments"]["operation"]["relation_disposition"]
        == "reviewed_none"
    )


def test_aberdeen_style_append_commits_four_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("2026-08-09", title="Rita Aberdeen accommodation strategy")
    source = source.replace("Before section.", "Accommodation baseline.")
    page = _write(tmp_path, ABERDEEN, source)
    _save_current_review(tmp_path, source)
    ticks = iter(
        [
            dt.datetime(2026, 8, 10, 15, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 10, 15, 0, 1, tzinfo=dt.UTC),
        ]
    )
    monkeypatch.setattr(temporal, "now", lambda: next(ticks))
    addition = (
        "Accommodation baseline.\n\n"
        "## Booking approach\n\nBook refundable inventory first.\n\n"
        "## Observations\n\n"
        "- [constraint] Keep the first booking refundable #travel\n"
        "- [preference] Prefer a central base over a car-dependent stay #travel\n"
        "- [risk] August inventory can tighten quickly #travel\n"
        "- [next action] Compare the short list before committing #travel"
    )
    operation = {
        "kind": "replace_string",
        "old_string": "Accommodation baseline.",
        "new_string": addition,
    }
    semantic = commands.op_edit_memory(
        tmp_path,
        path=ABERDEEN,
        why="validate Aberdeen additions",
        operation={**operation, "validate_only": True},
    )["semantic"]
    commands.op_edit_memory(
        tmp_path,
        path=ABERDEEN,
        why="commit Aberdeen additions",
        operation={
            **operation,
            "transition_token": semantic["transition_token"],
            "relation_disposition": "reviewed_none",
            "relation_review_hash": semantic["transition_hash"],
            "relation_review_reason": REVIEW_REASON,
        },
    )

    written = page.read_text(encoding="utf-8")
    assert "## Booking approach" in written
    assert written.count("#travel") == 4
    assert semantic.get("after_hash") == vault.content_hash(written)
