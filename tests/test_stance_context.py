"""Admitted global context and truthful metadata at the response boundary."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline
from test_context_catalogue import _built, _page, _seed, _write
from test_governance_egress import _external, _reset_caches
from test_working_set_withheld_twins import _withhold

from exomem import (
    context_views,
    lexstore,
    read_next_action,
    served_context,
    stance,
)
from exomem.governance import principal


@pytest.fixture
def stance_vault(tmp_path):
    _seed(tmp_path)
    _write(tmp_path, "Knowledge Base/_Schema/SKILL.md", "# Memory schema\n")
    schema = Path(stance.__file__).parent / "_scaffold/_Schema/references"
    for name in ("frontmatter.md", "page-types.md"):
        _write(tmp_path, f"Knowledge Base/_Schema/references/{name}", (schema / name).read_text(encoding="utf-8"))
    _page(tmp_path, "global", "stance: true\n", body="- [rule] Keep a source for each claim. ^source\n")
    initialize_vault_state_offline(tmp_path, source="stance workflow fixture")
    _built(tmp_path).close()
    with principal.library_scope():
        yield tmp_path


def _stance(root, *, today=date(2026, 11, 2), limit=800, who=None):
    lexstore.ensure_fresh(root)
    _built(root)
    _reset_caches()
    with principal.request_scope(who or principal.owner_principal(surface="library")), principal.invocation_scope():
        caller = context_views.CallerView.current(root)
        block = stance.compile_block(root, caller, limit, today=today)
        return stance.guard_block(root, caller, block)


def test_stance_excludes_marked_sources_and_invalid_marks(stance_vault):
    _write(stance_vault, "Knowledge Base/Sources/raw.md", "---\ntype: source\nstance: true\n---\n- [rule] Raw text is not global. ^raw\n")
    _page(stance_vault, "invalid", 'stance: "yes"\n', body="- [rule] Invalid mark is not global. ^invalid\n")
    block = _stance(stance_vault)
    assert block["state"] == "ready"
    assert [item["text"] for item in block["items"]] == ["Keep a source for each claim."]


def test_stance_expiry_and_review_change_without_a_write(stance_vault):
    _page(stance_vault, "global", "stance: true\n", body=(
        "## Claim\n- id: source\n- until: 2026-11-02\n\nKeep a source.\n"
        "## Claim\n- id: review\n- check_by: 2026-11-02\n\nCheck the release.\n"
    ))
    before = _stance(stance_vault)
    assert len(before["items"]) == 2 and not before["expired"]
    after = _stance(stance_vault, today=date(2026, 11, 3))
    assert after["expired"] == 1
    assert after["items"][0]["review_due"] == "2026-11-02"
    assert before["generation"] != after["generation"]
    assert after == _stance(stance_vault, today=date(2026, 11, 3))


def test_stance_whole_lines_and_withheld_prose_do_not_count(stance_vault):
    _page(stance_vault, "hidden", "stance: true\n", body="- [rule] Secret instruction. ^secret\n")
    _withhold(stance_vault, "Knowledge Base/Notes/Insights/hidden.md", ceiling=0)
    visible = _stance(stance_vault, who=_external())
    _page(stance_vault, "global", "stance: true\n", body=(
        "- [rule] Keep a source for each claim. ^source\n"
        "- [rule] Follow [[Knowledge Base/Notes/Insights/hidden]]. ^link\n"
    ))
    after = _stance(stance_vault, who=_external())
    assert after == visible
    tiny = _stance(stance_vault, limit=1, who=_external())
    assert tiny["items"] == [] and tiny["omitted"] == 1 and tiny["expired"] == 0


@pytest.mark.parametrize(
    ("packet", "state"),
    [
        ({"abstained": True, "abstention": {"reason": "ambiguous"}}, "ambiguous"),
        ({"abstention": {"reason": "unresolved"}, "anchors": [{"status": "partial", "evidence": ["rare_term"]}]}, "ambiguous"),
        ({"map": {}, "stance": {"items": [{"review_due": "2026-01-01"}]}, "turn_slice": {"state": "empty"}}, "review_due"),
        ({"map": {}, "turn_slice": {"state": "empty"}}, "slice_empty"),
        ({"abstention": {"reason": "unresolved"}, "related": [{"path": "example.md"}]}, "related"),
        ({"map": {}, "turn_slice": {"state": "ready", "items": [{"match": "ambiguous"}]}}, None),
    ],
)
def test_moves_follow_protocol_states(packet, state):
    action = read_next_action.move(packet)
    assert (action or {}).get("state") == state
    if action:
        assert len(action["say"]) <= 240
        assert "args" not in action


def _packet():
    return {
        "abstained": False, "map": {"generation": "map-one", "unchanged": True, "items": []},
        "stance": {"state": "ready", "items": [{"ref": "example.md#unit", "text": "Private prose is never recorded."}]},
        "turn_slice": {"state": "ready", "items": [{"ref": "example.md", "match": "named"}]},
        "next_action": {"state": "review_due", "say": "Review this."},
        "upkeep": {"items": [{"ref": "exomem://review/upkeep/item-one", "fingerprint": "current-fingerprint", "label": "Private label"}]},
    }


def test_records_keep_final_metadata_and_unknown_usage_without_heat(stance_vault):
    packet = _packet()
    packet["upkeep"]["items"][0]["context_route"] = {
        "tool": "review_item_context", "args": {"ref": "exomem://review/upkeep/" + "a" * 24},
    }
    identifiers = [served_context.append(stance_vault, packet) for _ in range(2)]
    assert all(identifiers) and len(set(identifiers)) == 2
    record = served_context.read(stance_vault, served_context_id=identifiers[0])
    assert record["record"]["map_unchanged"] is True
    assert record["record"]["session_state"] == "absent"
    assert record["record"]["upkeep"] == [{
        "ref": "exomem://review/upkeep/item-one", "fingerprint": "current-fingerprint",
        "context_ref": "exomem://review/upkeep/" + "a" * 24, "queue_item_id": "a" * 24,
    }]
    assert record["record"]["stance_refs"] == ["example.md#unit"]
    assert "Private" not in json.dumps(record)
    summary = served_context.read(stance_vault)
    assert summary["retained_rows"] == 2 and summary["ref_occurrences"] == 4
    assert summary["session_total"]["state"] == "unknown"
    assert summary["usage"]["opened"]["state"] == summary["usage"]["cited"]["state"] == "unknown"
    with principal.request_scope(_external()):
        assert served_context.read(stance_vault, served_context_id=identifiers[0]) == {"available": False, "reason": "owner_required"}


def test_locked_and_concurrent_records_do_not_fail_the_read(stance_vault):
    from exomem import commands
    from exomem.writer_lease import invoke_command

    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    first = invoke_command(command, stance_vault, turn="Keep a source")
    identifier = first["served_context_id"]
    with sqlite3.connect(served_context.sidecar_path(stance_vault)) as locked:
        locked.execute("BEGIN EXCLUSIVE")
        packet = invoke_command(command, stance_vault, turn="Keep a source")
        assert not packet["abstained"] and packet["map"]["generation"] == first["map"]["generation"]
        assert "served_context_id" not in packet
        assert "served_context_evidence" not in packet
        assert "served_context_unrecorded" in packet["diagnostics"]
        locked.rollback()
    served_context.finalize(stance_vault, packet)
    assert "served_context_id" not in packet and "served_context_evidence" not in packet
    abstained = {"abstained": True, "map": {}}
    served_context.finalize(stance_vault, abstained)
    assert "served_context_id" not in abstained and "served_context_evidence" not in abstained
    def append(_):
        with principal.library_scope():
            return served_context.append(stance_vault, _packet())
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(append, range(4)))
    successful = [identifier for identifier in ids if identifier is not None]
    assert successful and len(set(successful)) == len(successful)
    assert served_context.read(stance_vault, served_context_id=identifier)["state"] == "ready"
    assert served_context.read(stance_vault, served_context_id="missing")["state"] == "absent"
    assert served_context.sweep(stance_vault, now=10**12) == 1 + len(successful)
    assert served_context.read(stance_vault, served_context_id=identifier)["expiry"] == "unknown"


def test_boolean_stance_preview_and_commit_refuse_the_same_invalid_write(stance_vault):
    from exomem import commands

    path = "Knowledge Base/Notes/Insights/global.md"
    before = (stance_vault / path).read_bytes()
    outcomes = []
    for preview in (True, False):
        try:
            result = commands.op_edit(stance_vault, path, why="Validate the global mark", field="stance", value="yes", validate_only=preview)
            outcomes.append(json.dumps(result))
        except ValueError as error:
            outcomes.append(str(error))
    assert all("stance" in result and "true or false" in result for result in outcomes)
    assert (stance_vault / path).read_bytes() == before


def test_final_maps_metadata_matches_the_authorized_response(stance_vault, monkeypatch):
    from exomem import commands, working_set_runtime
    from exomem.governance import egress
    from exomem.writer_lease import invoke_command

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    working_set_runtime.reset_caches_for_tests()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    first = invoke_command(command, stance_vault, turn="Keep a source", max_chars=500)
    assert not first["abstained"]
    assert first["budget"]["used_chars"] <= 500
    assert first["budget"]["serialized_chars"] == context_views.serialized_chars(first)
    evidence = first["served_context_evidence"]
    assert evidence == {
        "scope": "server_composition", "client_receipt": "unknown", "opening": "unknown",
        "citation": "unknown", "use": "unknown", "historical_text": "not_retained",
    }
    with principal.invocation_scope():
        assert egress.postfilter("activate_context", first, stance_vault,
                                 request_kwargs={"turn": "Keep a source", "max_chars": 500}) == first
    assert served_context.read(stance_vault)["retained_rows"] == 1
    lookup = served_context.read(stance_vault, served_context_id=first["served_context_id"])
    assert lookup["served_context_evidence"] == evidence
    lookup["served_context_evidence"]["opening"] = "observed"
    assert served_context.read(stance_vault, served_context_id=first["served_context_id"])["served_context_evidence"] == evidence
    record = lookup["record"]
    assert record["map_generation"] == first["map"]["generation"]
    assert record["map_unchanged"] is first["map"].get("unchanged", False)
    assert record["stance_state"] == first["stance"]["state"]
    assert record["stance_refs"] == [item["ref"] for item in first["stance"]["items"]]
    assert record["slice_state"] == first["turn_slice"]["state"]
    assert record["slice_refs"] == [{"ref": item["ref"], "match": item["match"]} for item in first["turn_slice"]["items"]]
    assert record["next_action_state"] == (first.get("next_action") or {}).get("state")
    second = invoke_command(command, stance_vault, turn="Keep a source", continuity=first.get("continuity"), max_chars=500)
    assert second["served_context_id"] != first["served_context_id"]
    assert second["served_context_evidence"] == evidence
    assert second["map"]["unchanged"] is True
    monkeypatch.setenv("EXOMEM_ACTIVATION_STANCE", "off")
    off = invoke_command(command, stance_vault, turn="Keep a source", max_chars=500)
    assert "stance" not in off


def test_same_page_and_page_supersession_stop_global_lines(stance_vault):
    _page(stance_vault, "global", "stance: true\n", body=(
        "## Claim\n- id: earlier\n\nOld operating rule.\n"
        "## Claim\n- id: replacement\n- relations: supersedes: [[#earlier]]\n\nNew operating rule.\n"
    ))
    block = _stance(stance_vault)
    assert [item["text"] for item in block["items"]] == ["New operating rule."]
    assert block["omitted"] == block["expired"] == 0
    _page(stance_vault, "global", "stance: true\nsuperseded_by: [successor]\n", body="- [rule] Old operating rule. ^old\n")
    assert _stance(stance_vault)["items"] == []


def test_hook_uses_supplied_or_generated_moves_and_stance_generation():
    from exomem._hooks import exomem_retrieve_nudge as hook

    old = {"abstained": True, "abstention": {"reason": "ambiguous"},
           "ambiguity": [{"title": "Sample", "ref": "sample.md"}]}
    fallback = hook._format_working_set_block(old, 4000)
    assert "anchor" in fallback and "recall_context" in fallback
    old["next_action"] = {"state": "ambiguous", "say": "Choose the admitted sample."}
    supplied = hook._format_working_set_block(old, 4000)
    assert supplied.count("Choose the admitted sample.") == 1
    assert "again with the original turn" not in supplied
    related = {"abstained": True, "abstention": {"reason": "unresolved"},
               "related": [{"path": "sample.md", "title": "Sample"}]}
    assert "not loaded" in hook._format_working_set_block(related, 4000)
    packet = {"map": {"generation": "map", "ref": "exomem://map", "reason": "empty", "items": []},
              "stance": {"state": "ready", "generation": "one", "items": [
                  {"ref": "sample.md#rule", "text": "Keep the source.", "review_due": "2026-01-01"}], "omitted": 2}}
    first, *_ = hook._format_map_with_slice(packet, 4000, "")
    held, *_ = hook._format_map_with_slice(packet, 4000, "map", "one")
    changed, *_ = hook._format_map_with_slice(packet, 4000, "map", "zero")
    assert "Keep the source." in first and "review due" in first and "2 more" in first
    assert "Keep the source." not in held and "already held" in held
    assert "Keep the source." in changed
    tiny, *_ = hook._format_map_with_slice(packet, 30, "")
    assert len(tiny) <= 30 and "Keep the source." not in tiny


def test_move_search_miss_and_reduced_surface_keep_a_supported_route():
    from exomem.capabilities import ActiveSurfaceDescriptor, active_surface

    reduced = ActiveSurfaceDescriptor(surface="test", profile="reduced", tier2_enabled=False,
                                      product_commands=("bootstrap", "search_memory"))
    with active_surface(reduced):
        assert read_next_action.move({"hits": []}, search=True)["state"] == "search_miss"
        assert read_next_action.move({"hits": [{"path": "sample.md"}]}, search=True) is None
        empty = read_next_action.move({"map": {}, "turn_slice": {"state": "empty"}})
        assert empty["tool"] == "search_memory"
        assert "recall_context" not in empty["say"]
        assert read_next_action.move({"abstention": {"reason": "ambiguous"}}) is None


def test_unattributed_read_stays_unknown_and_recording_does_not_change_heat(stance_vault):
    from exomem import commands, working_set_heat
    from exomem.writer_lease import invoke_command

    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")
    invoke_command(command, stance_vault, path="Knowledge Base/Notes/Insights/global.md")
    before = working_set_heat.load(stance_vault)
    identifier = served_context.append(stance_vault, _packet(), session="example-session")
    record = served_context.read(stance_vault, served_context_id=identifier)
    assert record["record"]["session_state"] == "present"
    assert record["record"]["session_key"] != "example-session"
    assert record["usage"]["opened"]["state"] == record["usage"]["cited"]["state"] == "unknown"
    assert working_set_heat.load(stance_vault).events == before.events
    historical = record["record"]["stance_refs"]
    (stance_vault / "example.md").write_text("temporary current page", encoding="utf-8")
    current = served_context.read(stance_vault, served_context_id=identifier)
    assert current["record"]["stance_refs"] == historical
    assert current["current_resolution"][0]["state"] == "resolved"
    (stance_vault / "example.md").rename(stance_vault / "renamed.md")
    renamed = served_context.read(stance_vault, served_context_id=identifier)
    assert renamed["record"]["stance_refs"] == historical
    assert renamed["current_resolution"][0]["state"] == "absent"


def test_owner_cli_and_rest_read_the_same_retained_row(stance_vault, monkeypatch, capsys):
    import asyncio

    from starlette.testclient import TestClient
    from test_activate_context_surface import _run_cli, _surface_server

    from exomem import commands

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(stance_vault))
    mcp = _surface_server(monkeypatch)
    client = TestClient(mcp.http_app())
    packet = asyncio.run(mcp.call_tool("activate_context", {"turn": "Keep a source"},
                                       run_middleware=False)).structured_content
    activated = client.post("/api/activate_context", json={"turn": "Keep a source"},
                            headers={"Authorization": "Bearer sekret"})
    assert activated.status_code == 200, activated.text
    rest_packet = activated.json()["data"]
    assert packet["served_context_evidence"] == rest_packet["served_context_evidence"]
    assert packet["served_context_id"] != rest_packet["served_context_id"]
    for response in (packet, rest_packet):
        assert response["budget"]["used_chars"] <= response["budget"]["limit_chars"]
        assert response["budget"]["serialized_chars"] == context_views.serialized_chars(response)
    identifier = packet["served_context_id"]
    code, output = _run_cli(["served-context", "--served-context-id", identifier, "--json"], capsys)
    assert code == 0, output
    cli_data = json.loads(output)["data"]
    assert cli_data["served_context_evidence"] == packet["served_context_evidence"]
    result = client.post("/api/served_context", json={"served_context_id": identifier},
                         headers={"Authorization": "Bearer sekret"})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["record"] == cli_data["record"]
    assert result.json()["data"]["served_context_evidence"] == packet["served_context_evidence"]
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "served_context")
    assert command.surfaces == frozenset({"cli", "rest"})


def test_recorded_view_keeps_the_requests_declared_purpose(stance_vault):
    from exomem import commands
    from exomem.writer_lease import invoke_command

    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    plain = invoke_command(command, stance_vault, turn="Keep a source")
    scoped = invoke_command(command, stance_vault, turn="Keep a source", purpose="source check")
    repeated = invoke_command(command, stance_vault, turn="Keep a source", purpose="source check")
    rows = [served_context.read(stance_vault, served_context_id=packet["served_context_id"])["record"]
            for packet in (plain, scoped, repeated)]
    assert rows[0]["view"] != rows[1]["view"]
    assert rows[1]["view"] == rows[2]["view"]
    assert all("source check" not in json.dumps(row) for row in rows)


def test_stance_failure_keeps_the_map_and_reports_unknown_counts(stance_vault, monkeypatch):
    from exomem import commands
    from exomem.writer_lease import invoke_command

    def unavailable(*args, **kwargs):
        raise OSError("fixture stance projection unavailable")

    monkeypatch.setattr(stance, "compile_block", unavailable)
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    packet = invoke_command(command, stance_vault, turn="Keep a source")
    assert packet["abstained"] is False and "map" in packet
    assert packet["stance"]["state"] == "unavailable"
    assert packet["stance"]["omitted"] is None and packet["stance"]["expired"] is None
    assert packet["stance"]["generation"] is None


def test_final_size_includes_a_late_collection_store_notice(stance_vault, monkeypatch):
    from exomem import commands
    from exomem.collection_store import preview
    from exomem.writer_lease import invoke_command

    original = preview.note_withheld_pages

    def unavailable_notice(command, packet, root):
        with monkeypatch.context() as terminal:
            terminal.setattr(preview, "pages_unavailable", lambda _root: True)
            return original(command, packet, root)

    monkeypatch.setattr(preview, "note_withheld_pages", unavailable_notice)
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    packet = invoke_command(command, stance_vault, turn="Keep a source")
    assert packet["generation"]["collection_store"] == "unavailable"
    budget = packet["budget"]
    without_size = {**packet, "budget": {key: value for key, value in budget.items() if key != "serialized_chars"}}
    assert budget["serialized_chars"] == len(json.dumps(without_size, ensure_ascii=False, separators=(",", ":")))


def test_canonical_stance_does_not_depend_on_a_status_overlay(stance_vault):
    before = _stance(stance_vault)
    _write(stance_vault, "Knowledge Base/_Schema/statuses.yaml", "invalid: [\n")
    after = _stance(stance_vault)
    assert before["state"] == "ready" and before["items"]
    assert after == before


@pytest.mark.parametrize("long_stance", [False, True])
def test_method_reset_stance_and_move_fit_the_minimum_public_cap(stance_vault, monkeypatch, long_stance):
    """A crowded first response invalidates held methods without losing standing lines or its next move."""
    from exomem import commands, working_set_runtime
    from exomem.writer_lease import invoke_command

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    statement = ("Keep the source with each measurement and retain its inspection record for later checks."
                 if long_stance else "Keep a source for each claim.")
    _page(stance_vault, "global", "stance: true\n", body=f"- [rule] {statement} ^source\n")
    lexstore.ensure_fresh(stance_vault)
    _built(stance_vault).close()
    token = working_set_runtime.encode_continuity(
        identity="old", roles_hash="r", conventions_hash="c", generation=0,
        refs=[], roles=[], map_generation="old",
        method_versions=[{"ref": "missing#unit", "source_ref": "missing.md", "source_version": "old"}],
    )
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    first = invoke_command(command, stance_vault, turn="", continuity=token, max_chars=500)
    assert first["task_context"]["methods"]["reset"] is True
    assert [item["text"] for item in first["stance"]["items"]] == [statement]
    assert first["next_action"]["state"] == "method_reset"
    assert first["budget"]["used_chars"] == context_views.packet_chars(
        first["map"], first.get("turn_slice"), first["task_context"], first["stance"], first["next_action"],
    ) <= 500
    from exomem import context_task

    reserve = int((500 - read_next_action.MAX_CHARS - stance.chars(first["stance"])) * context_views.TURN_SLICE_SHARE)
    assert context_task.packet_chars(first["task_context"]) <= reserve
    row = served_context.read(stance_vault, served_context_id=first["served_context_id"])["record"]
    assert row["task_context"]["methods"]["reset"] is True
    second = invoke_command(command, stance_vault, turn="", continuity=first["continuity"], max_chars=500)
    assert second["task_context"]["methods"]["reset"] is False
    assert second["stance"] == first["stance"]
    assert second["map"]["unchanged"] is True
    assert second["budget"]["used_chars"] <= 500


def test_hook_keeps_reset_stance_and_move_when_optional_routes_cannot_fit():
    """Hook framing must not swallow an invalidation while it remembers a fresh token."""
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "map": {"generation": "map", "ref": "exomem://map", "items": [
            {"title": "A long optional map entry " * 20, "ref": "index.md"}]},
        "stance": {"state": "ready", "generation": "stance", "omitted": 0,
                   "items": [{"ref": "rule.md#source", "text": "Keep the source."}]},
        "task_context": {"methods": {"reset": True, "items": [
            {"attribution": "Inspection procedure", "read_ref": "method.md"}]}},
        "next_action": {"state": "method_reset", "say": "Read the current method source."},
    }
    stance_text, _ = hook._format_stance_block(packet["stance"], 4000)
    cap = len(hook._METHOD_RESET_LINE) + len(stance_text) + len(packet["next_action"]["say"]) + 2
    first, complete, stance_complete = hook._format_map_with_slice(packet, cap, "")
    assert first.splitlines()[0] == hook._METHOD_RESET_LINE
    assert "Keep the source." in first and first.endswith(packet["next_action"]["say"])
    assert "method.md" not in first and not complete and stance_complete
    assert len(first) <= cap
    full, *_ = hook._format_map_with_slice(packet, 4000, "")
    assert full.index(hook._METHOD_RESET_LINE) < full.index("method.md")
    packet["task_context"]["methods"]["reset"] = False
    second, _, held_complete = hook._format_map_with_slice(packet, cap, "", "stance")
    assert "already held" in second and second.endswith(packet["next_action"]["say"])
    assert hook._METHOD_RESET_LINE not in second and held_complete and len(second) <= cap


def test_served_task_routes_keep_identities_without_retrieved_prose(stance_vault):
    """The owner can audit task methods and read routes without retaining their content or prompts."""
    route = {"ref": "method.md#unit", "source_ref": "method.md", "source_version": "version", "read_ref": "method.md"}
    packet = {**_packet(), "task_context": {
        "state": "ready", "candidates": [{**route, "title": "Private source title", "reason": "Private task details"}],
        "methods": {"reset": False, "items": [{**route, "content": "Private method text"}]},
    }}
    identifier = served_context.append(stance_vault, packet)
    record = served_context.read(stance_vault, served_context_id=identifier)
    assert record["record"]["task_context"] == {
        "state": "ready", "candidates": [route], "methods": {"reset": False, "items": [route]},
    }
    assert "Private" not in json.dumps(record)
    assert sum(item["ref"] == "method.md" for item in record["current_resolution"]) == 2
    assert served_context.read(stance_vault)["ref_occurrences"] == 4
    with principal.request_scope(_external()):
        assert served_context.read(stance_vault, served_context_id=identifier) == {"available": False, "reason": "owner_required"}


def test_served_rows_from_before_task_metadata_remain_readable(stance_vault):
    """Adding task metadata must retain old audit rows and distinguish their unrecorded task state."""
    before = served_context.append(stance_vault, _packet())
    with sqlite3.connect(served_context.sidecar_path(stance_vault)) as connection:
        connection.execute("ALTER TABLE served_context DROP COLUMN task_context")
    old = served_context.read(stance_vault, served_context_id=before)
    assert old["record"]["task_context"] == {"state": "unknown", "reason": "not_recorded"}
    assert served_context.read(stance_vault)["retained_rows"] == 1
    after = served_context.append(stance_vault, {**_packet(), "task_context": {"state": "empty"}})
    assert after and after != before
    assert served_context.read(stance_vault, served_context_id=before)["record"] == old["record"]
    assert served_context.read(stance_vault, served_context_id=after)["record"]["task_context"]["state"] == "empty"
    assert served_context.read(stance_vault)["retained_rows"] == 2
