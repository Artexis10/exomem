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

from exomem import server as server_module


def _build(monkeypatch: pytest.MonkeyPatch):
    """Build the server against the fixture vault, embeddings off for speed."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    return server_module.build_server(require_auth=False)


def _call(mcp, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args, run_middleware=False))
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


# ---------------- edit: mode routing ----------------

def test_edit_batch_mode_routes_to_multi_edit(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nalpha\nbeta\n")
    out = _call(mcp, "edit_memory", {
        "path": rel,
        "why": "batch tweak",
        "edits": [
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "beta", "new_string": "BETA"},
        ],
    })
    assert out.get("edits_applied") == 2
    text = (vault / rel).read_text(encoding="utf-8")
    assert "ALPHA" in text and "BETA" in text


def test_edit_take_mode_routes_to_set_take(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(
        vault,
        "# S\n\n## Opinions\n\n- Whiplash (2014) — 10/10 — [take: ]  <!-- x -->\n",
    )
    out = _call(mcp, "edit_memory", {
        "path": rel, "why": "fill", "row_key": "Whiplash (2014)", "take": "relentless",
    })
    assert "relentless" in out.get("row", "")
    assert "[take: relentless]" in (vault / rel).read_text(encoding="utf-8")


def test_edit_frontmatter_mode_routes_to_set_fm(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nbody\n")
    out = _call(mcp, "edit_memory", {
        "path": rel, "why": "set status", "field": "status", "value": "active",
    })
    assert out.get("field") == "status"
    assert out.get("new_value") == "active"
    assert "status: active" in (vault / rel).read_text(encoding="utf-8")


def test_edit_default_surgical_still_works(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nhello world\n")
    _call(mcp, "edit_memory", {
        "path": rel, "why": "tweak", "old_string": "hello world", "new_string": "goodbye world",
    })
    assert "goodbye world" in (vault / rel).read_text(encoding="utf-8")


def test_edit_rejects_two_modes_at_once(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\nx\n")
    with pytest.raises(Exception) as exc:
        _call(mcp, "edit_memory", {
            "path": rel, "why": "bad", "row_key": "x", "take": "y",
            "field": "status", "value": "active",
        })
    assert "edit mode" in str(exc.value).lower()


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
    })
    assert out.get("created") is True
    assert (vault / "Knowledge Base/Notes/Insights/new-folder").is_dir()


def test_create_file_default_writes_file(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    out = _call(mcp, "manage_memory_file", {
        "operation": "create",
        "path": "Knowledge Base/Notes/Insights/plain.md", "content": "hi\n",
    })
    assert out.get("path", "").endswith("plain.md")
    assert (vault / "Knowledge Base/Notes/Insights/plain.md").read_text(encoding="utf-8") == "hi\n"


# ---------------- delete: file vs dir auto-detection ----------------

def test_delete_detects_file(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    rel = _make_page(vault, "# S\n\norphan file\n", name="to-delete.md")
    out = _call(mcp, "manage_memory_file", {
        "operation": "delete",
        "path": rel,
        "confirm": True,
    })
    assert "inbound_ignored_count" in out  # file-shaped result
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
    })
    assert "file_count" in out  # directory-shaped result
    assert not d.exists()


# ---------------- note: project key description is dynamic + open ----------------

def test_remember_project_description_is_dynamic_and_open(vault: Path, monkeypatch) -> None:
    """The `remember` tool schema must advertise the live project-key set + the
    auto-register contract, not a frozen closed list.

    Regression: claude.ai burned reasoning cycles (and nearly misfiled a note)
    treating an unlisted scope like `home` as illegal, because the docstring
    listed a fixed `Valid: ...` enum with no hint that keys auto-register.
    """
    mcp = _build(monkeypatch)
    tool = asyncio.run(mcp.get_tool("remember"))
    project_desc = tool.parameters["properties"]["project"]["description"]
    projects_desc = tool.parameters["properties"]["projects"]["description"]

    # The sentinel must be fully substituted at registration time.
    assert "__PROJECT_KEYS_HINT__" not in project_desc
    assert "__PROJECT_KEYS_HINT__" not in projects_desc
    # Open-set framing: the model must know unlisted keys are legal.
    assert "auto-register" in project_desc.lower()
    assert "not exhaustive" in project_desc.lower()
    assert "auto-register" in projects_desc.lower()
    # The list is sourced from the live registry (fixture falls back to the
    # built-in set, which includes these), not a hand-maintained string.
    assert "project-alpha" in project_desc
    assert "personal" in project_desc


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


def test_triage_memory_mcp_write_is_explicit_and_reversible(vault: Path, monkeypatch) -> None:
    mcp = _build(monkeypatch)
    review = _call(mcp, "review_memory", {"mode": "attention", "limit": 1})
    item = review["items"][0]

    dismissed = _call(
        mcp,
        "triage_memory",
        {"ref": item["ref"], "action": "dismiss", "why": "reviewed"},
    )
    assert dismissed["state"] == "dismissed"
    assert (vault / "Knowledge Base/.review-state.json").exists()

    reopened = _call(
        mcp,
        "triage_memory",
        {"ref": item["ref"], "action": "reopen"},
    )
    assert reopened["state"] == "open"
