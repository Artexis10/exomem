"""The agent tool surface through an in-process MCP client, one real workflow per test.

Each test lists or calls tools the way a host does and asserts what the vault holds afterwards,
not the wording of a reply. Nothing here calls a leaf directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import record_fixtures
from fastmcp import Client
from lifecycle_fixtures import PLANNING_PATH, planning_manifest

from exomem import call_ledger, writer_lease
from exomem import server as server_module

LISTED = {
    "recall_context", "search_memory", "read_page", "read_instructions", "create_note", "add_observation",
    "edit_note", "supersede_note", "save_source", "save_evidence", "save_episode", "upload_files",
    "download_files", "query_records", "add_record", "edit_record", "query_plan_items", "add_plan_item",
    "edit_plan_item", "list_review_items", "decide_review_item", "inspect_memory", "manage_memory",
}


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def _session(mcp, scenario):
    async def run():
        async with Client(mcp) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, name, arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
        body = body["result"]  # FastMCP wraps the result of some grouped commands under one key
    return body if body is not None else {
        "is_error": result.is_error,
        "text": " ".join(getattr(block, "text", "") for block in result.content),
    }


def test_a_local_server_lists_exactly_the_agent_tools(build) -> None:
    async def scenario(client):
        return [tool.name for tool in await client.list_tools()]

    names = _session(build(), scenario)

    assert set(names) == LISTED
    assert len(names) == len(LISTED)


def test_an_old_tool_name_is_refused_and_the_reply_lists_the_new_ones(build) -> None:
    async def scenario(client):
        return await _call(client, "ask_memory", {"query": "x"})

    reply = _session(build(), scenario)

    assert reply["is_error"] is True
    assert "search_memory" in reply["text"]


def _vault_text(vault: Path, title: str) -> str:
    """The text of the page whose first heading is `title`; empty when no page has it."""
    for path in (vault / "Knowledge Base" / "Notes").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if f"\n# {title}\n" in text:
            return text
    return ""


def test_an_agent_reads_creates_and_edits_notes_by_title(build, vault) -> None:
    async def scenario(client):
        found = await _call(client, "search_memory", {"query": "retry fixed interval"})
        page = await _call(client, "read_page", {"page": "Retry with a fixed interval"})
        created = await _call(client, "create_note", {
            "title": "Retry policy", "type": "insight",
            "content": (
                "# Retry policy\n\n## Observations\n\n"
                "- [operating constraint] Cap retries at 30 s #reliability\n\n"
                "## Relations\n\n- refines [[Retry with exponential backoff and full jitter]]\n"
            ),
        })
        edited = await _call(client, "edit_note", {
            "note": "Retry policy", "why": "Cap changed after review", "action": "replace_text",
            "old_text": "30 s", "new_text": "10 s",
        })
        recalled = await _call(client, "recall_context", {"turn": "retry policy for flaky clients"})
        return found, page, created, edited, recalled

    found, page, created, edited, recalled = _session(build(), scenario)

    assert "error" not in found and "error" not in page and "error" not in recalled, (found, page, recalled)
    assert "error" not in created, created
    assert "error" not in edited, edited
    text = _vault_text(vault, "Retry policy")
    assert "Cap retries at 10 s" in text
    assert "30 s" not in text


async def _new_note(client, title: str) -> dict:
    """Create a small active note with one observation and no links."""
    return await _call(client, "create_note", {
        "title": title, "type": "insight", "unlinked_reason": "A standalone test note.",
        "content": f"# {title}\n\n## Observations\n\n- [finding] Original wording #reliability\n",
    })


def test_an_agent_adds_an_observation_then_edits_it_by_unit_id(build, vault) -> None:
    async def scenario(client):
        await _new_note(client, "Jitter policy")
        added = await _call(client, "add_observation", {
            "note": "Jitter policy", "category": "finding",
            "text": "Fixed intervals line retrying clients up", "tags": ["reliability"],
        })
        units = await _call(client, "read_page", {"page": "Jitter policy", "part": "units"})
        edited = await _call(client, "edit_note", {
            "note": "Jitter policy", "why": "Sharper wording", "action": "edit_observation",
            "observation": added.get("unit_id"), "category": "finding",
            "text": "Fixed intervals line retrying clients up at once", "tags": ["reliability"],
        })
        return added, units, edited

    added, units, edited = _session(build(), scenario)

    assert "error" not in added and "error" not in edited, (added, edited)
    assert added["unit_id"] in str(units)
    text = _vault_text(vault, "Jitter policy")
    assert "line retrying clients up at once" in text
    assert "Fixed intervals line retrying clients up #reliability" not in text and "Original wording" in text


def test_a_stale_unit_id_is_refused_and_leaves_the_page_unchanged(build, vault) -> None:
    async def scenario(client):
        await _new_note(client, "Jitter policy")
        added = await _call(client, "add_observation", {
            "note": "Jitter policy", "category": "finding", "text": "First wording", "tags": ["reliability"],
        })
        edit = {"note": "Jitter policy", "why": "Reword", "action": "edit_observation",
                "observation": added.get("unit_id"), "category": "finding"}
        first = await _call(client, "edit_note", {**edit, "text": "Second wording"})
        stale = await _call(client, "edit_note", {**edit, "text": "Third wording"})
        return first, stale

    first, stale = _session(build(), scenario)

    assert "error" not in first, first
    assert stale.get("error", {}).get("code") == "STALE_UNIT_REFERENCE", stale
    text = _vault_text(vault, "Jitter policy")
    assert "Second wording" in text and "Third wording" not in text


def _seed_relation_candidate(vault: Path) -> Path:
    """Two pages where the first names the second in its body, and a rebuilt graph that suggests the edge."""
    from exomem import epistemic_graph, find

    notes = vault / "Knowledge Base" / "Notes" / "Insights"
    (notes / "acorn.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n# acorn\n\n"
        "Body names [[Knowledge Base/Notes/Insights/birch]] inline.\n", encoding="utf-8")
    (notes / "birch.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n# birch\n\nA measured fact.\n", encoding="utf-8")
    find.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return notes / "acorn.md"


async def _acorn_candidate(client):
    queue = await _call(client, "list_review_items", {"queue": "relation_suggestions", "limit": 50})
    rows = [item for group in queue.get("groups", []) for item in group.get("items", [])]
    return next((row for row in rows if row.get("from", "").endswith("acorn.md")
                 and row.get("to", "").endswith("birch.md")), None)


def test_an_agent_accepts_a_suggested_relation_by_review_item_id(build, vault) -> None:
    acorn = _seed_relation_candidate(vault)

    async def scenario(client):
        item = await _acorn_candidate(client)
        decided = await _call(client, "decide_review_item", {
            "review_item": item["id"], "decision": "accept", "why": "The body already depends on it"})
        return item, decided

    item, decided = _session(build(), scenario)

    assert item is not None
    assert "error" not in decided, decided
    assert "birch]]" in acorn.read_text(encoding="utf-8").partition("## Relations")[2]


def test_an_agent_holds_two_contradicting_pages_as_competing_alternatives(build, vault) -> None:
    from exomem import epistemic_graph, find

    notes = vault / "Knowledge Base" / "Notes" / "Insights"
    front = "---\ntype: insight\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
    (notes / "rival-a.md").write_text(
        f"{front}# rival-a\n\nCap retries at 3.\n\n## Relations\n\n- contradicts [[Knowledge Base/Notes/Insights/rival-b]]\n",
        encoding="utf-8")
    (notes / "rival-b.md").write_text(f"{front}# rival-b\n\nNever cap retries.\n", encoding="utf-8")
    find.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    async def scenario(client):
        pair = (await _call(client, "list_review_items", {"queue": "contradiction"}))["items"][0]
        decided = await _call(client, "decide_review_item", {
            "review_item": pair["id"], "decision": "competing", "why": "Both hold in different deployments"})
        return decided, await _call(client, "list_review_items", {"queue": "contradiction"})

    decided, after = _session(build(), scenario)

    assert "error" not in decided, decided
    assert after["state_summary"]["competing"] == 1 and after["state_summary"]["open"] == 0


def test_a_review_item_id_read_before_the_page_changed_is_refused(build, vault) -> None:
    acorn = _seed_relation_candidate(vault)

    async def scenario(client):
        item = await _acorn_candidate(client)
        acorn.write_text(acorn.read_text(encoding="utf-8") + "\nAn edit made after the list was read.\n",
                         encoding="utf-8")
        decided = await _call(client, "decide_review_item", {
            "review_item": item["id"], "decision": "accept", "why": "The body already depends on it"})
        return item, decided

    item, decided = _session(build(), scenario)

    assert item is not None
    assert decided.get("error", {}).get("code") == "REVIEW_ITEM_CHANGED", decided
    assert "## Relations" not in acorn.read_text(encoding="utf-8")


def _item_files(vault: Path, folder: str) -> str:
    """The concatenated text of every Markdown item under a collection folder, without its manifest."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((vault / folder).rglob("*.md"))
                     if path.name != "_collection.md")


def test_an_agent_adds_queries_and_edits_a_record_by_its_id(build, vault) -> None:
    async def scenario(client):
        created = await _call(client, "manage_memory", {"operation": "records.create", "arguments": {
            "manifest_path": record_fixtures.LEDGER_COLLECTION_PATH,
            "manifest_text": record_fixtures.LEDGER_MANIFEST_TEXT, "why": "Track published entries", "scaffold": True}})
        added = await _call(client, "add_record", {
            "collection": "Publications ledger", "item": record_fixtures.ledger_item(), "why": "Log the first entry"})
        rows = await _call(client, "query_records", {"collection": "Publications ledger", "columns": ["slug"]})
        row = rows["rows"][0]
        edit = {"collection": "Publications ledger", "record": row.get("id"), "why": "Corrected count"}
        edited = await _call(client, "edit_record", {**edit, "changes": {"word_count": 50}})
        stale = await _call(client, "edit_record", {**edit, "changes": {"word_count": 60}})
        return created, added, row, edited, stale

    created, added, row, edited, stale = _session(build(), scenario)

    assert "error" not in created and "error" not in added and "error" not in edited, (created, added, edited)
    assert row["slug"] == "quarterly-note" and row["id"]
    assert stale.get("error", {}).get("code") == "STALE_ITEM", stale
    items = _item_files(vault, "Knowledge Base/Records/Publications")
    assert "word_count: 50" in items and "word_count: 60" not in items


def test_an_agent_adds_a_plan_item_and_moves_it_by_status(build, vault) -> None:
    async def scenario(client):
        created = await _call(client, "manage_memory", {"operation": "plans.create", "arguments": {
            "manifest_path": PLANNING_PATH, "manifest_text": planning_manifest(), "why": "Plan the delivery",
            "scaffold": True}})
        added = await _call(client, "add_plan_item", {
            "collection": "Delivery plan", "item": {"title": "Ship the cutover"}, "why": "Capture the work"})
        rows = await _call(client, "query_plan_items", {"collection": "Delivery plan"})
        row = rows["rows"][0]
        moved = await _call(client, "edit_plan_item", {
            "collection": "Delivery plan", "plan_item": row.get("id"), "status": "cancelled", "why": "Superseded by another plan"})
        return created, added, row, moved

    created, added, row, moved = _session(build(), scenario)

    assert "error" not in created and "error" not in added and "error" not in moved, (created, added, moved)
    assert row["title"] == "Ship the cutover" and row["id"]
    items = _item_files(vault, "Knowledge Base/Planning/Delivery")
    assert "Ship the cutover" in items and "status: cancelled" in items


def _named_tools(node, found: set[str] | None = None) -> set[str]:
    """Every tool name that a reply spells as `{"tool": ...}`, wherever it nests."""
    found = set() if found is None else found
    if isinstance(node, dict):
        if isinstance(node.get("tool"), str):
            found.add(node["tool"])
        for value in node.values():
            _named_tools(value, found)
    elif isinstance(node, list):
        for value in node:
            _named_tools(value, found)
    return found


def test_the_compact_instructions_name_only_listed_tools_and_each_tool_shows_an_example(build) -> None:
    async def scenario(client):
        compact = await _call(client, "read_instructions", {"profile": "compact"})
        topic = await _call(client, "read_instructions", {"topic": "edit_plan_item"})
        door = await _call(client, "read_instructions", {"topic": "inspect_memory"})
        inspected = await _call(client, "inspect_memory", {"operation": "settings.inspect"})
        return compact, topic, door, inspected

    compact, topic, door, inspected = _session(build(), scenario)

    assert compact["profile"] == "compact" and _named_tools(compact) <= LISTED, compact
    assert topic["tool"] == "edit_plan_item" and topic["example"]
    assert "records" in door["operations"] and "error" not in inspected


def test_a_call_to_the_wrong_door_or_an_unknown_operation_is_refused_with_where_to_go(build) -> None:
    async def scenario(client):
        wrong_door = await _call(client, "manage_memory", {"operation": "settings.inspect"})
        misspelt = await _call(client, "manage_memory", {"operation": "records.creat", "arguments": {}})
        return wrong_door, misspelt

    wrong_door, misspelt = _session(build(), scenario)

    assert wrong_door["error"]["code"] == "INVALID_OPERATION" and "inspect_memory" in wrong_door["error"]["message"]
    assert misspelt["error"]["code"] == "INVALID_OPERATION"
    assert "records.create" in misspelt["error"]["candidates"]


def test_a_note_without_an_observation_is_refused_with_the_syntax_to_add(build, vault) -> None:
    async def scenario(client):
        return await _call(client, "create_note", {
            "title": "Plain prose", "type": "insight", "content": "# Plain prose\n\nJust prose.\n",
            "unlinked_reason": "A standalone test note."})

    refused = _session(build(), scenario)

    assert refused["error"]["code"] == "missing_semantic_unit"
    assert "## Observations" in refused["error"]["message"] and "- [" in refused["error"]["message"]
    assert _vault_text(vault, "Plain prose") == ""


def test_a_family_disposition_set_through_the_write_door_shows_in_the_read_door(build) -> None:
    async def scenario(client):
        quiet = await _call(client, "manage_memory", {"operation": "review.set_family", "arguments": {
            "ref": "exomem://review/family/prediction_window", "action": "quiet",
            "why": "too_frequent: fires more than it helps"}})
        listed = await _call(client, "inspect_memory", {"operation": "review.dispositions"})
        return quiet, listed

    quiet, listed = _session(build(), scenario)

    assert "error" not in quiet, quiet
    assert {row["family"]: row["disposition"] for row in listed["dispositions"]}["prediction_window"] == "quiet"


def test_upload_files_returns_an_upload_step_and_writes_nothing(build, vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "synthetic-upload-secret")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")
    before = sorted(path.name for path in (vault / "Knowledge Base" / "Sources").rglob("*"))

    async def scenario(client):
        return await _call(client, "upload_files", {"lane": "source"})

    step = _session(build(), scenario)

    assert step["upload_url"] == "https://capture.example/upload" and step["lane"] == "source"
    assert step["committed"] is False
    assert sorted(path.name for path in (vault / "Knowledge Base" / "Sources").rglob("*")) == before


def _evidence_bytes(vault: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in (vault / "Knowledge Base" / "Evidence").rglob("*") if path.is_file()}


def test_download_files_hands_off_two_originals_by_title_without_changing_the_vault(build, vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "synthetic-private-signing-key")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")

    async def scenario(client):
        for name in ("first.txt", "second.txt"):
            saved = await _call(client, "save_evidence", {
                "scope": "Test", "category": "samples", "name": name, "text": f"The {name} transcript."})
            assert "error" not in saved, saved
        before = _evidence_bytes(vault)
        handoff = await _call(client, "download_files", {"pages": ["Evidence: first.txt", "Evidence: second.txt"]})
        return before, handoff

    before, handoff = _session(build(), scenario)

    assert handoff["handoff_status"] == "handoff_prepared" and handoff["committed"] is False, handoff
    assert [item["artifact"]["file_name"] for item in handoff["artifacts"]] == ["first.txt", "second.txt"]
    assert all(item["deliveries"]["https"]["url"].startswith("https://capture.example/download") for item in handoff["artifacts"])
    assert before == _evidence_bytes(vault)


def _routes(node, found: list[dict] | None = None) -> list[dict]:
    """Every `{"tool": ..., "args": ...}` route that a reply carries, wherever it nests."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if isinstance(node.get("tool"), str):
            found.append(node)
        for value in node.values():
            _routes(value, found)
    elif isinstance(node, list):
        for value in node:
            _routes(value, found)
    return found


def _enum_values(prop: dict) -> set | None:
    """The values that a property's schema lists, directly or in one branch of a nullable union."""
    values = set(prop.get("enum", ()))
    for branch in prop.get("anyOf", ()):
        values |= set(branch.get("enum", ()))
    return values or None


def _route_faults(route: dict, schemas: dict[str, dict]) -> list[str]:
    """Why a listed tool would refuse the route's arguments, judged by its input schema."""
    schema = schemas.get(route["tool"])
    if schema is None:
        return [f"{route['tool']} is not a listed tool"]
    properties = schema["properties"]
    arguments = route.get("args", route.get("arguments")) or {}
    faults = [f"{route['tool']} takes no {key}" for key in arguments if key not in properties]
    for key, value in arguments.items():
        allowed = _enum_values(properties.get(key, {}))
        if allowed is not None and value not in allowed:
            faults.append(f"{route['tool']} takes {key} from {sorted(allowed)}, not {value!r}")
    return faults


def test_every_route_a_public_reply_carries_names_a_listed_tool_and_arguments_it_takes(
    build, monkeypatch, tmp_path
) -> None:
    import dreamer_fixture as fx

    from exomem import capabilities, dreamer, dreamer_store, freshness

    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    vault = fx.build(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    assert all(result.stop_reason != "error" for result in fx.run_to_quiet(vault))
    # The competing stance of a contradiction pair is the one decision that no write reply carries yet.
    public = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape="public"
    )
    with capabilities.active_surface(public):
        audit_fix = capabilities.active_call_route("triage_memory", {"action": "competing", "item_id": "<id>"})

    async def scenario(client):
        schemas = {tool.name: tool.input_schema for tool in await client.list_tools()}
        replies = [
            await _call(client, "read_instructions", {"profile": "full"}),
            await _call(client, "create_note", {
                "title": "Retry budget", "type": "insight", "detail": "full",
                "content": "# Retry budget\n\n## Observations\n\n- [operating constraint] Cap retries at 3 #reliability\n",
                "unlinked_reason": "A standalone test note.",
            }),
        ]
        replies.append(await _call(client, "list_review_items", {"queue": "upkeep"}))
        return schemas, replies

    try:
        schemas, replies = _session(build(), scenario)
    finally:
        dreamer.reset_for_tests()
        freshness.clear()
        dreamer_store.clear_reader_memo()

    routes = [route for reply in replies for route in _routes(reply)] + [audit_fix]
    faults = [fault for route in routes for fault in _route_faults(route, schemas)]

    assert not faults, faults
    assert not [reply for reply in replies if "error" in reply], replies
    # A route reply that carried no route would pass vacuously, so each spelling the surface re-writes must appear.
    assert {"edit_note", "decide_review_item", "create_note", "search_memory"} <= {route["tool"] for route in routes}


def test_an_uncoded_fault_names_the_tool_the_agent_called_not_the_command_behind_it(build, monkeypatch) -> None:
    def refuse(command, *args, **kwargs):
        raise RuntimeError("[withheld]")

    monkeypatch.setattr(writer_lease, "invoke_command", refuse)

    async def scenario(client):
        return await _call(client, "read_page", {"page": "Retry with a fixed interval"})

    reply = _session(build(), scenario)

    assert reply["is_error"] is True
    assert "'read_page'" in reply["text"] and "read_memory" not in reply["text"], reply
