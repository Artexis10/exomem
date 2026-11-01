"""Plain write outcomes: exact review-field errors and one user-facing summary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from exomem import commands, mutation_terminal, relation_review, writer_lease
from exomem import server as server_module
from exomem.governance import principal as principal_module

REASON = "No honest relation exists for this fixture."
IGNORED_WARNING = (
    "This page needs no relation review; "
    "relation_disposition and relation_review_reason were ignored."
)
_PAGE = "## Observations\n\n- [finding] The page keeps one finding. ^one\n"


def _seed_peer(vault: Path) -> str:
    """Create a page so later pages in the fixture vault need a relation review."""
    arguments = {"title": "Peer", "slug": "peer", "content": _PAGE}
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    return commands.op_remember(
        vault,
        **arguments,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason=REASON,
    )["path"]


def _connected_page(peer: str) -> dict[str, str]:
    return {
        "title": "Connected",
        "slug": "connected",
        "content": f"{_PAGE}\n## Connections\n\n- [[{peer.removesuffix('.md')}]]\n",
    }


def _unconnected_page() -> dict[str, str]:
    return {"title": "Alone", "slug": "alone", "content": _PAGE}


def _commit_arguments(arguments: dict[str, str], validation: dict) -> dict[str, object]:
    return {
        **arguments,
        "draft_id": validation["draft_id"],
        "draft_hash": validation["draft_hash"],
        "draft_token": validation["draft_token"],
    }


def test_a_page_that_needs_no_review_commits_and_drops_the_review_fields(vault: Path) -> None:
    arguments = _connected_page(_seed_peer(vault))
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    assert validation["relation_review_hash"] is None
    reviews_before = relation_review.load_relation_reviews(vault)

    result = commands.op_remember(
        vault,
        **_commit_arguments(arguments, validation),
        relation_disposition="reviewed_none",
        relation_review_reason=REASON,
    )

    assert (vault / result["path"]).is_file()
    assert [w for w in result["warnings"] if "ignored" in w] == [IGNORED_WARNING]
    receipt = relation_review.load_creation_receipt(vault, validation["draft_id"])
    assert receipt is not None and receipt.kind == "qualifying"
    assert relation_review.load_relation_reviews(vault) == reviews_before


def test_a_page_that_needs_review_refuses_a_commit_that_lacks_the_hash(vault: Path) -> None:
    _seed_peer(vault)
    arguments = _unconnected_page()
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    assert validation["relation_review_hash"] == validation["draft_hash"]

    with pytest.raises(ValueError) as refused:
        commands.op_remember(
            vault,
            **_commit_arguments(arguments, validation),
            relation_disposition="reviewed_none",
            relation_review_reason=REASON,
        )

    text = str(refused.value)
    assert text.startswith("INVALID_RELATION_REVIEW")
    assert "(missing: ['relation_review_hash'])" in text
    assert "validate_only" in text


def test_validation_names_the_review_fields_a_commit_would_lack(vault: Path) -> None:
    _seed_peer(vault)

    needs_review = commands.op_remember(
        vault,
        validate_only=True,
        **_unconnected_page(),
        relation_disposition="reviewed_none",
        relation_review_reason=REASON,
    )

    assert needs_review["relation_review_fields"] == {"missing": ["relation_review_hash"]}


def test_validation_names_the_review_fields_a_commit_would_ignore(vault: Path) -> None:
    arguments = _connected_page(_seed_peer(vault))

    needs_no_review = commands.op_remember(
        vault,
        validate_only=True,
        **arguments,
        relation_disposition="reviewed_none",
        relation_review_reason=REASON,
    )

    assert needs_no_review["relation_review_fields"] == {
        "ignored": ["relation_disposition", "relation_review_reason"]
    }


def _mcp(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    return server_module.build_server(require_auth=False)


def _call(mcp, name: str, arguments: dict) -> dict:
    with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
        result = asyncio.run(mcp.call_tool(name, arguments, run_middleware=True))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


def test_a_review_error_through_the_tool_names_the_field_and_never_an_empty_list(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(monkeypatch)
    _seed_peer(vault)
    arguments = _unconnected_page()
    validation = _call(mcp, "create_note", {**arguments, "validate_only": True})

    refused = _call(
        mcp,
        "create_note",
        {
            **_commit_arguments(arguments, validation),
            "relation_disposition": "reviewed_none",
            "relation_review_reason": REASON,
        },
    )

    assert refused["success"] is False
    assert refused["error"]["code"] == "INVALID_RELATION_REVIEW"
    assert "relation_review_hash" in refused["error"]["message"]
    assert "(missing: [])" not in refused["error"]["message"]
    assert refused["error"]["remediation"]


def _manage_file(vault: Path, **arguments: object) -> dict:
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "manage_memory_file")
    return writer_lease.invoke_command(command, vault, **arguments)


def test_a_committed_save_leads_with_a_plain_summary(vault: Path) -> None:
    out = _manage_file(
        vault, operation="create", path="Knowledge Base/Notes/plain.md", content="hi\n"
    )

    assert next(iter(out)) == "user_summary"
    assert out["user_summary"] == "Saved -> Knowledge Base/Notes/plain.md"


def test_a_delete_summary_says_deleted_not_saved(vault: Path) -> None:
    target = vault / "Knowledge Base/Notes/Insights/to-delete.md"
    target.write_text("---\ntype: insight\n---\nbody\n", encoding="utf-8")

    out = _manage_file(
        vault,
        operation="delete",
        path="Knowledge Base/Notes/Insights/to-delete.md",
        confirm=True,
    )

    assert out["user_summary"] == "Deleted -> Knowledge Base/Notes/Insights/to-delete.md"


def test_a_move_summary_says_moved_and_names_both_ends(vault: Path) -> None:
    _manage_file(vault, operation="create", path="Knowledge Base/Notes/old-name.md", content="hi\n")

    out = _manage_file(
        vault,
        operation="move",
        old_path="Knowledge Base/Notes/old-name.md",
        new_path="Knowledge Base/Notes/new-name.md",
    )

    assert out["user_summary"] == (
        "Moved Knowledge Base/Notes/old-name.md -> Knowledge Base/Notes/new-name.md"
    )


def test_a_write_that_stores_several_files_names_the_first_and_counts_the_rest() -> None:
    terminal = mutation_terminal.committed_terminal(
        {"paths": ["Sources/a.md", "Sources/b.md", "Sources/c.md"]},
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt-1",
        idempotency_key=None,
    )

    summary = mutation_terminal.project_terminal(terminal)["user_summary"]

    assert summary == "Saved 3 files -> Sources/a.md and 2 more"


def test_a_repeated_request_returns_the_same_summary_as_the_first(vault: Path) -> None:
    request = {
        "operation": "create",
        "path": "Knowledge Base/Notes/repeat.md",
        "content": "hi\n",
        "idempotency_key": "plain-write-outcomes-repeat",
    }

    first = _manage_file(vault, **request)
    again = _manage_file(vault, **request)

    assert again["user_summary"] == first["user_summary"]


def test_a_replayed_write_says_it_was_already_saved() -> None:
    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": "a" * 64,
        "after_item_hash": "a" * 64,
        "before_container_hash": "b" * 64,
        "after_container_hash": "b" * 64,
        "affected_paths": ["Knowledge Base/Records/log.md"],
        "payload_hash": "c" * 64,
        "outcome": "replayed",
        "audit_correlation": "d" * 24,
    }
    terminal = mutation_terminal.replayed_terminal(
        receipt,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt-1",
        idempotency_key="same-call",
    )

    projected = mutation_terminal.project_terminal(terminal)

    assert next(iter(projected)) == "user_summary"
    assert projected["user_summary"] == "Already saved -> Knowledge Base/Records/log.md"
    assert mutation_terminal.project_terminal(terminal) == projected


def test_the_narration_rule_reaches_the_bootstrap_and_the_skill(vault: Path) -> None:
    """The skill and the bootstrap cannot import the rule, so both copies are compared here."""
    rule = mutation_terminal.NARRATION_RULE
    loop = commands.op_bootstrap(vault, profile="compact")["write"]["canonical_loop"]
    skill_text = (
        Path(__file__).parents[1] / "src/exomem/_scaffold/_Schema/SKILL.md"
    ).read_text(encoding="utf-8")
    skill = " ".join(skill_text.split())

    assert rule in loop
    assert rule in skill
    assert "Working on Exomem does not make an unrelated conversation diagnostic." in skill
