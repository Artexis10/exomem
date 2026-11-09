"""Integration tests for the consolidated tool surface.

The other test files exercise the backend modules directly. These drive the
merged *server tools* through `mcp.call_tool`, so the dispatch routing added
when folding multiple tools into one is actually covered:

  - `edit_memory` routes to multi_edit / set_take / set_frontmatter_field by mode arg
  - `read_memory(frontmatter_only=True)` routes to get_frontmatter
  - `manage_memory_file(operation="create", kind="dir")` routes to create_directory
  - `manage_memory_file(operation="delete")` auto-detects file vs directory
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _public_call import public_call

from exomem import commands, review_state, semantic_index
from exomem import server as server_module


def _build(monkeypatch: pytest.MonkeyPatch):
    """Build the server against the fixture vault, embeddings off for speed."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    return server_module.build_server(require_auth=False)


def _call(mcp, command: str, args: dict, *, run_middleware: bool = True) -> dict:
    """Call one grouped command through the public tool that carries it."""
    return _tool(mcp, *public_call(command, args), run_middleware=run_middleware)


def _tool(mcp, name: str, args: dict, *, run_middleware: bool = True) -> dict:
    result = asyncio.run(mcp.call_tool(name, args, run_middleware=run_middleware))
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            return json.loads(text)
    return {}


def _make_page(vault: Path, body: str, *, name: str = "scratch-test.md") -> str:
    rel = f"Knowledge Base/Notes/Insights/{name}"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        "type: insight\n"
        "created: 2026-06-01\n"
        "updated: 2026-06-01\n"
        "tags: []\n"
        "---\n" + body,
        encoding="utf-8",
    )
    return rel


def test_read_memory_exact_unit_matches_canonical_response(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = "Knowledge Base/Notes/Insights/mcp-exact-unit.md"
    (vault / rel).write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 12345678-1234-5678-1234-567812345678\n"
        "title: MCP exact unit\n"
        "status: active\n"
        "updated: 2026-07-16\n"
        "---\n\n"
        "- [config] MCP can read this unit ^mcp-unit\n",
        encoding="utf-8",
    )
    state = semantic_index.current_parent_index_state(vault, rel)
    unit_ref = state.document.units[0].unit_ref
    assert unit_ref is not None
    expected = commands.op_read_memory(vault, path=rel, unit_ref=unit_ref)

    out = _call(
        _build(monkeypatch),
        "read_memory",
        {"path": rel, "unit_ref": unit_ref},
    )

    assert out == expected


# ---------------- edit: mode routing ----------------

def test_edit_note_discovery_is_one_discriminated_operation(
    vault: Path, monkeypatch
) -> None:
    tools = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json", by_alias=True)
        for tool in asyncio.run(_build(monkeypatch).list_tools())
    }
    schema = tools["edit_note"]["inputSchema"]

    assert set(schema["required"]) == {"note", "why", "action"}
    assert set(schema["properties"]["action"]["enum"]) == {
        "replace_text",
        "replace_section",
        "replace_body",
        "set_tags",
        "set_fields",
        "fill_row",
        "edit_observation",
        "remove_observation",
    }
    for field in ("edits", "links", "preview", "detail", "unlinked_reason"):
        assert field in schema["properties"]
    edit = schema["properties"]["edits"]["items"]
    assert set(edit["required"]) == {"old_text", "new_text"}
    assert set(edit["properties"]) == {"old_text", "new_text", "replace_all"}

def test_edit_note_refuses_a_field_it_does_not_take(vault: Path, monkeypatch) -> None:
    """The published schema leaves extra fields open to fit the budget; the server refuses them."""
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nalpha\n")

    out = _tool(mcp, "edit_note", {"note": rel, "why": "w", "action": "replace_body", "body": "x", "bogus": 1})

    assert "error" in out
    assert "bogus" in json.dumps(out["error"])


def test_edit_batch_mode_routes_to_multi_edit(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nalpha\nbeta\n")
    out = _call(mcp, "edit_memory", {
        "path": rel,
        "why": "batch tweak",
        "operation": {
            "kind": "batch_replace",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "beta", "new_string": "BETA"},
            ],
        },
        "response_detail": "full",
    })
    assert out["diagnostics"]["edits_applied"] == 2
    text = (vault / rel).read_text(encoding="utf-8")
    assert "ALPHA" in text and "BETA" in text


def test_edit_batch_mode_accepts_connector_encoded_object_strings(
    vault: Path, monkeypatch
) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nalpha\nbeta\n")
    tools = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json", by_alias=True)
        for tool in asyncio.run(mcp.list_tools())
    }
    assert tools["edit_note"]["inputSchema"]["properties"]["edits"]["items"]["type"] == "object"

    out = _tool(
        mcp,
        "edit_note",
        {
            "note": rel,
            "why": "connector-encoded batch",
            "action": "replace_text",
            "edits": [
                json.dumps({"old_text": "alpha", "new_text": "ALPHA"}),
                json.dumps({"old_text": "beta", "new_text": "BETA"}),
            ],
            "detail": "full",
        },
    )

    assert out["diagnostics"]["edits_applied"] == 2
    text = (vault / rel).read_text(encoding="utf-8")
    assert "ALPHA" in text and "BETA" in text


@pytest.mark.parametrize(
    "edits",
    [
        [],
        ["[]"],
        [{"old_text": "alpha"}],
        [{"old_text": "alpha", "new_text": "ALPHA", "ignored": True}],
        [{"old_text": "alpha", "new_text": "alpha"}],
    ],
)
def test_edit_batch_invalid_items_are_refused_and_change_nothing(
    vault: Path, monkeypatch, edits: list
) -> None:
    rel = _make_page(vault, "# S\n\nalpha\n", name="invalid-batch-edit.md")
    before = (vault / rel).read_text(encoding="utf-8")

    out = _tool(
        _build(monkeypatch),
        "edit_note",
        {"note": rel, "why": "invalid batch", "action": "replace_text", "edits": edits},
    )

    assert out["error"]["code"] == "INVALID_ARGUMENT"
    assert (vault / rel).read_text(encoding="utf-8") == before


def test_edit_batch_encoded_blob_is_rejected_by_middleware(
    vault: Path, monkeypatch
) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nalpha\n", name="encoded-blob.md")
    before = (vault / rel).read_text(encoding="utf-8")
    encoded = json.dumps(
        {
            "old_text": "alpha",
            "new_text": "data:image/png;base64," + "A" * 40_000,
        }
    )

    with pytest.raises(Exception, match="BINARY_BLOB_REJECTED"):
        _tool(
            mcp,
            "edit_note",
            {"note": rel, "why": "must be rejected", "action": "replace_text", "edits": [encoded]},
        )

    assert (vault / rel).read_text(encoding="utf-8") == before


def test_edit_take_mode_routes_to_set_take(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(
        vault,
        "# S\n\n## Opinions\n\n- Whiplash (2014) — 10/10 — [take: ]  <!-- x -->\n",
    )
    out = _call(mcp, "edit_memory", {
        "path": rel,
        "why": "fill",
        "operation": {
            "kind": "fill_row",
            "row_key": "Whiplash (2014)",
            "take": "relentless",
        },
        "response_detail": "full",
    })
    assert "relentless" in out["diagnostics"]["row"]
    assert "[take: relentless]" in (vault / rel).read_text(encoding="utf-8")


def test_edit_frontmatter_mode_routes_to_set_fm(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nbody\n")
    out = _call(mcp, "edit_memory", {
        "path": rel,
        "why": "set status",
        "operation": {"kind": "patch_frontmatter", "field": "status", "value": "active"},
        "response_detail": "full",
    })
    assert out["diagnostics"]["field"] == "status"
    assert out["diagnostics"]["new_value"] == "active"
    assert "status: active" in (vault / rel).read_text(encoding="utf-8")


def test_edit_default_surgical_still_works(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nhello world\n")
    _call(mcp, "edit_memory", {
        "path": rel,
        "why": "tweak",
        "operation": {
            "kind": "replace_string",
            "old_string": "hello world",
            "new_string": "goodbye world",
        },
    })
    assert "goodbye world" in (vault / rel).read_text(encoding="utf-8")


def test_edit_refuses_a_field_that_the_action_does_not_take(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nx\n")
    before = (vault / rel).read_text(encoding="utf-8")

    two_actions = _tool(
        mcp,
        "edit_note",
        {"note": rel, "why": "bad", "action": "fill_row", "row": "x", "take": "y", "fields": {"status": "active"}},
    )
    assert two_actions["error"]["code"] == "INVALID_ARGUMENT"
    assert "fields does not apply to fill_row" in two_actions["error"]["message"]

    unknown = _tool(mcp, "edit_note", {"note": rel, "why": "unknown action", "action": "nonsense"})
    assert unknown["error"]["code"] == "INVALID_ARGUMENT"
    assert "replace_text" in unknown["error"]["message"]
    assert (vault / rel).read_text(encoding="utf-8") == before


# ---------------- get: frontmatter_only routing ----------------

def test_get_frontmatter_only_routes(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nlots of body text here\n")
    out = _call(mcp, "read_memory", {"path": rel, "frontmatter_only": True})
    assert out.get("has_frontmatter") is True
    assert out["frontmatter"].get("type") == "insight"
    assert "body" not in out  # frontmatter-only shape, no body


def test_get_full_still_returns_body(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nunique-body-marker\n")
    out = _call(mcp, "read_memory", {"path": rel})
    assert "unique-body-marker" in out.get("body", "")
    assert "content_hash" in out


# ---------------- create_file: kind=dir routing ----------------

def test_create_file_kind_dir_routes_to_mkdir(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    out = _call(mcp, "manage_memory_file", {
        "operation": "create",
        "path": "Knowledge Base/Notes/Insights/new-folder", "kind": "dir",
        "response_detail": "full",
    })
    assert out["diagnostics"].get("created") is True
    assert (vault / "Knowledge Base/Notes/Insights/new-folder").is_dir()


def test_create_file_default_writes_file(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    out = _call(mcp, "manage_memory_file", {
        "operation": "create",
        "path": "Knowledge Base/Notes/plain.md", "content": "hi\n",
    })
    assert out.get("path", "").endswith("plain.md")
    assert (vault / "Knowledge Base/Notes/plain.md").read_text(encoding="utf-8") == "hi\n"


# ---------------- delete: file vs dir auto-detection ----------------

def test_delete_detects_file(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\norphan file\n", name="to-delete.md")
    out = _call(mcp, "manage_memory_file", {
        "operation": "delete",
        "path": rel,
        "confirm": True,
        "response_detail": "full",
    })
    assert "inbound_ignored_count" in out["diagnostics"]  # file-shaped result
    assert not (vault / rel).exists()


def test_delete_detects_directory(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    d = vault / "Knowledge Base/Notes/Insights/doomed"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.md").write_text("---\ntype: insight\n---\nbody\n", encoding="utf-8")
    out = _call(mcp, "manage_memory_file", {
        "operation": "delete",
        "path": "Knowledge Base/Notes/Insights/doomed", "confirm": True,
        "recursive": True, "force_orphan": True,
        "response_detail": "full",
    })
    assert "file_count" in out["diagnostics"]  # directory-shaped result
    assert not d.exists()


# ---------------- note: project key description is stable + open ----------------

def test_create_note_project_description_is_stable_and_open(vault: Path, monkeypatch) -> None:
    """The `create_note` schema must expose a stable open-set project contract.

    Regression: claude.ai burned reasoning cycles (and nearly misfiled a note)
    treating an unlisted scope like `home` as illegal, because the docstring
    listed a fixed `Valid: ...` enum with no hint that keys auto-register.

    Hosted connector clients cache discovered schemas, so live project names
    must not leak into the tool description and cause per-vault schema drift.
    """
    mcp = _build(monkeypatch)
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "create_note")
    project_desc = tool.parameters["properties"]["project"]["description"]
    projects_desc = tool.parameters["properties"]["projects"]["description"]

    # The sentinel must be fully substituted at registration time.
    assert "__PROJECT_KEYS_HINT__" not in project_desc
    assert "__PROJECT_KEYS_HINT__" not in projects_desc
    # Open-set framing: the model must know a new key is legal.
    assert "registers itself" in project_desc.lower()
    assert "registers itself" in projects_desc.lower()
    # Live registry values must not alter the discovery surface.
    assert "project-alpha" not in project_desc
    assert "personal" not in project_desc


def test_review_memory_attention_mode_composes_review_surface(vault: Path, monkeypatch) -> None:
    """The `review_memory` MCP tool returns one ranked review surface, read-only, and
    honors the `categories` subset filter — driven end-to-end through call_tool."""
    mcp = _build(monkeypatch)
    out = _call(mcp, "review_memory", {"mode": "attention", "limit": 10})

    # Shape contract (mirrors AttentionReport.as_dict()).
    assert {"items", "summary", "shown", "total", "truncated", "upstream_truncated"} <= set(out)
    assert isinstance(out["items"], list)
    assert out["shown"] == len(out["items"]) <= 10
    for item in out["items"]:
        assert {
            "path",
            "score",
            "severity",
            "categories",
            "reasons",
            "proposed_fix",
            "item_id",
            "ref",
            "target_ref",
            "fingerprint",
            "state",
        } <= set(item)
        assert item["categories"], "every item must name at least one queue"
        assert "review only" in item["proposed_fix"].lower()

    # Category subset is honored: only the requested queue can appear.
    only_sources = _call(mcp, "review_memory", {"mode": "attention", "categories": ["unprocessed_source"]})
    surfaced = {c for it in only_sources["items"] for c in it["categories"]}
    assert surfaced <= {"unprocessed_source"}


def test_review_memory_activation_mode_surfaces_corpus_coverage(vault: Path, monkeypatch) -> None:
    rel = _make_page(
        vault,
        "# Activation-only\n\n## Overview\n\nSee [[Knowledge Base/Notes/Insights/other]].\n",
        name="activation-only.md",
    )
    mcp = _build(monkeypatch)

    out = _call(mcp, "review_memory", {"mode": "activation", "limit": 0})

    assert out["coverage"]["eligible_pages"] > 0
    item = next(item for item in out["items"] if item["path"] == rel)
    assert item["categories"] == ["typed_relation_debt"]
    assert item["ref"].startswith("exomem://review/")
    assert item["reasons"][0]["meta"]["next_actions"]


def test_review_item_context_mcp_composes_stable_bounded_context(
    vault: Path, monkeypatch
) -> None:
    mcp = _build(monkeypatch)
    review = _call(mcp, "review_memory", {"mode": "activation", "limit": 1})
    item = review["items"][0]

    out = _call(
        mcp,
        "review_item_context",
        {
            "ref": item["ref"],
            "expected_fingerprint": item["fingerprint"],
            "max_body_chars": 200,
        },
    )

    assert out["item"]["ref"] == item["ref"]
    assert out["target"]["path"] == item["path"]
    assert {
        "related",
        "provenance",
        "graph",
        "history",
        "evolution",
        "availability",
        "truncation",
    } <= set(out)


def test_decide_review_item_write_is_explicit_and_reversible(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    review = _tool(mcp, "list_review_items", {"limit": 1})
    item = review["items"][0]

    dismissed = _tool(
        mcp,
        "decide_review_item",
        {"review_item": item["id"], "decision": "dismiss", "why": "reviewed", "detail": "full"},
    )
    assert dismissed["state"] == "dismissed"
    assert review_state.state_path(vault).exists()

    reopened = _tool(
        mcp,
        "decide_review_item",
        {"review_item": item["id"], "decision": "reopen", "detail": "full"},
    )
    assert reopened["state"] == "open"
