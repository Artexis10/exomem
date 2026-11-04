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
    identifier = served_context.append(stance_vault, _packet())
    with sqlite3.connect(served_context.sidecar_path(stance_vault)) as locked:
        locked.execute("BEGIN EXCLUSIVE")
        packet = _packet()
        served_context.finalize(stance_vault, packet)
        assert "served_context_id" not in packet
        assert "served_context_unrecorded" in packet["diagnostics"]
        locked.rollback()
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
    from exomem.writer_lease import invoke_command

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    working_set_runtime.reset_caches_for_tests()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")
    first = invoke_command(command, stance_vault, turn="Keep a source", max_chars=500)
    assert not first["abstained"]
    assert first["budget"]["used_chars"] <= 500
    assert first["budget"]["serialized_chars"] == context_views.serialized_chars(first)
    record = served_context.read(stance_vault, served_context_id=first["served_context_id"])["record"]
    assert record["map_generation"] == first["map"]["generation"]
    assert record["map_unchanged"] is first["map"].get("unchanged", False)
    assert record["stance_state"] == first["stance"]["state"]
    assert record["stance_refs"] == [item["ref"] for item in first["stance"]["items"]]
    assert record["slice_state"] == first["turn_slice"]["state"]
    assert record["slice_refs"] == [{"ref": item["ref"], "match": item["match"]} for item in first["turn_slice"]["items"]]
    assert record["next_action_state"] == (first.get("next_action") or {}).get("state")
    second = invoke_command(command, stance_vault, turn="Keep a source", continuity=first.get("continuity"), max_chars=500)
    assert second["served_context_id"] != first["served_context_id"]
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
    first, _ = hook._format_map_with_slice(packet, 4000, "")
    held, _ = hook._format_map_with_slice(packet, 4000, "map", "one")
    changed, _ = hook._format_map_with_slice(packet, 4000, "map", "zero")
    assert "Keep the source." in first and "review due" in first and "2 more" in first
    assert "Keep the source." not in held and "already held" in held
    assert "Keep the source." in changed
    tiny, _ = hook._format_map_with_slice(packet, 30, "")
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
    from test_activate_context_surface import _rest_client, _run_cli

    from exomem import commands

    identifier = served_context.append(stance_vault, _packet())
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(stance_vault))
    code, output = _run_cli(["served-context", "--served-context-id", identifier, "--json"], capsys)
    assert code == 0, output
    cli_record = json.loads(output)["data"]["record"]
    client = _rest_client(monkeypatch)
    result = client.post("/api/served_context", json={"served_context_id": identifier},
                         headers={"Authorization": "Bearer sekret"})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["record"] == cli_record
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
