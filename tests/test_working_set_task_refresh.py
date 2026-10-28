"""Agent refresh keeps human intent separate from the task packet."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import test_working_set_conversation as conversation_fixtures
import test_working_set_material as material_fixtures
from starlette.testclient import TestClient
from test_activate_context_surface import PARITY_BLOCKS, _run_cli, _surface_server
from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope

from exomem import commands, lexstore, query_log, working_set_index, working_set_runtime
from exomem import request_budget
from exomem.governance import egress
from exomem.governance.principal import library_scope, request_scope

# This module tests the `anchors` packet, the rollback mode; `maps` is the shipped default.
pytestmark = pytest.mark.usefixtures("anchors_packet")

# Reuse the maintained fixture factories without copying their setup.
cvault = conversation_fixtures.cvault
competing_projects = conversation_fixtures.competing_projects
material_vault = material_fixtures.material_vault


def test_resolved_task_does_not_choose_between_ambiguous_human_referents(
    cvault: Path, competing_projects: None,
) -> None:
    packet = commands.op_activate_context(
        cvault, turn="what about the Estuary Programme", trigger="task_refresh",
        conversation={"focus": "review the Tidewater Grant"},
    )
    assert packet["intent_resolution"] == {"status": "ambiguous"}
    assert packet["abstained"] is False and packet["units"]
    assert [anchor["title"] for anchor in packet["anchors"]] == ["Tidewater Grant"]
    assert packet["anchors"][0]["origin"] == "focus"
    assert packet["trigger"] == "task_refresh"


def test_task_ambiguity_abstains_even_when_the_human_named_one_subject(
    cvault: Path, competing_projects: None,
) -> None:
    packet = commands.op_activate_context(
        cvault, turn="review the Tidewater Grant", trigger="task_refresh",
        conversation={"focus": "what about the Estuary Programme"},
    )
    assert packet["intent_resolution"] == {"status": "resolved"}
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert len(packet["ambiguity"]) == 2
    assert not packet["units"]
    assert all(anchor["origin"] == "focus" for anchor in packet["ambiguity"])


def test_agent_choice_resolves_only_the_task_and_retains_its_evidence(
    cvault: Path, competing_projects: None,
) -> None:
    path = "Knowledge Base/Entities/Projects/Kestrel Hiring Plan.md"
    packet = commands.op_activate_context(
        cvault, turn="what about the Estuary Programme", trigger="task_refresh",
        conversation={"focus": "what about the Estuary Programme"}, anchor=path,
    )
    assert packet["intent_resolution"] == {"status": "ambiguous"}
    assert packet["abstained"] is False
    assert len(packet["anchors"]) == 1
    assert packet["anchors"][0]["path"] == path
    assert packet["anchors"][0]["evidence"] == ["agent_choice"]
    assert "learning" not in packet


def test_roles_follow_the_agent_task_instead_of_the_original_request(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="what are the constraints on the Tidewater Grant",
        trigger="task_refresh", conversation={"focus": "preferences for the Tidewater Grant"},
    )
    assert packet["abstained"] is False
    selected = {role["id"] for role in packet["roles"]}
    assert "preferences" in selected
    assert "constraints" not in selected


def test_two_task_requests_keep_their_claims_and_uncompiled_prose(
    material_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harvest, rival = material_fixtures._allocation_material(material_vault)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(material_vault))
    focus = (
        "In harbor-study, what guides violet worksheet calibration? "
        "In orchard-survey: what guides shaded crates against heat damage?"
    )
    packet = commands.op_activate_context(
        material_vault, turn="review the latest evidence", trigger="task_refresh",
        conversation={"focus": focus},
    )
    material = [unit for unit in packet["units"] if unit["role"] == "material"]
    assert len(material) == 2
    assert all("worksheet-procedure.md" in unit["ref"] for unit in material)
    pointers = [pointer["ref"] for pointer in packet["pointers"] if pointer["role"] == "material"]
    assert harvest in pointers and rival not in pointers
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_refresh_without_task_focus_does_not_replay_the_human_packet(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="review the Tidewater Grant", trigger="task_refresh",
    )
    assert packet["intent_resolution"] == {"status": "resolved"}
    assert packet["abstention"] == {"reason": "unresolved"}
    assert not packet["units"] and not packet["anchors"]


def test_new_focus_replaces_the_task_context_instead_of_reusing_a_prior_packet(cvault: Path) -> None:
    arguments = {"turn": "review the latest evidence", "trigger": "task_refresh"}
    first = commands.op_activate_context(
        cvault, **arguments, conversation={"focus": "review the Tidewater Grant"},
    )
    second = commands.op_activate_context(
        cvault, **arguments, conversation={"focus": "review the Harbor Lantern Budget"},
    )
    assert [anchor["title"] for anchor in first["anchors"]] == ["Tidewater Grant"]
    assert [anchor["title"] for anchor in second["anchors"]] == ["Harbor Lantern Budget"]


def test_hidden_human_subject_is_indistinguishable_from_an_absent_subject(cvault: Path) -> None:
    path = "Knowledge Base/Entities/Projects/Kestrel Hiring Plan.md"
    write_scope(cvault, paths="Entities/Projects/Kestrel Hiring Plan.md")
    write_rule(cvault, ceiling=0)
    arguments = {
        "turn": "review the Kestrel Hiring Plan", "trigger": "task_refresh",
        "conversation": {"focus": "review the Tidewater Grant"},
    }
    with request_scope(_external()):
        hidden = commands.op_activate_context(cvault, **arguments)
    (cvault / path).unlink()
    lexstore.ensure_fresh(cvault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(cvault).rebuild()
    with request_scope(_external()):
        absent = commands.op_activate_context(cvault, **arguments)
    assert hidden["intent_resolution"] == absent["intent_resolution"] == {"status": "unresolved"}
    assert {key: hidden.get(key) for key in PARITY_BLOCKS} == {
        key: absent.get(key) for key in PARITY_BLOCKS
    }
    assert path not in json.dumps(hidden)


@pytest.mark.parametrize("loss", ["revoke", "delete"])
def test_lost_human_evidence_does_not_disclose_stale_intent_or_remove_the_task(
    cvault: Path, monkeypatch: pytest.MonkeyPatch, loss: str,
) -> None:
    path = "Knowledge Base/Entities/Projects/Kestrel Hiring Plan.md"
    write_scope(cvault, paths="Entities/Projects/Kestrel Hiring Plan.md")
    write_rule(cvault, ceiling=6)
    final_guard = egress.guard_working_set

    def withdraw_human_page(root, packet, release, **arguments):
        if loss == "revoke":
            write_rule(cvault, ceiling=0)
        else:
            (cvault / path).unlink()
        _reset_governance_caches()
        return final_guard(root, packet, release, **arguments)

    monkeypatch.setattr(egress, "guard_working_set", withdraw_human_page)
    with request_scope(_external()):
        packet = commands.op_activate_context(
            cvault, turn="review the Kestrel Hiring Plan", trigger="task_refresh",
            conversation={"focus": "review the Tidewater Grant"},
        )
    assert packet["intent_resolution"] == {"status": "unavailable"}
    assert packet["abstained"] is False and packet["units"]
    assert [anchor["title"] for anchor in packet["anchors"]] == ["Tidewater Grant"]
    assert path not in json.dumps(packet) and "_witness" not in json.dumps(packet)


def test_deleting_an_already_hidden_human_page_does_not_signal_a_lost_witness(
    cvault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "Knowledge Base/Entities/Projects/Kestrel Hiring Plan.md"
    write_scope(cvault, paths="Entities/Projects/Kestrel Hiring Plan.md")
    write_rule(cvault, ceiling=0)
    final_guard = egress.guard_working_set

    def delete_hidden_page(root, packet, release, **arguments):
        (cvault / path).unlink()
        _reset_governance_caches()
        return final_guard(root, packet, release, **arguments)

    monkeypatch.setattr(egress, "guard_working_set", delete_hidden_page)
    with request_scope(_external()):
        packet = commands.op_activate_context(
            cvault, turn="review the Kestrel Hiring Plan", trigger="task_refresh",
            conversation={"focus": "review the Tidewater Grant"},
        )
    assert packet["intent_resolution"] == {"status": "unresolved"}
    assert packet["abstained"] is False and packet["units"]
    assert path not in json.dumps(packet)


def test_lost_task_evidence_cannot_invalidate_an_unchanged_human_subject(
    cvault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_scope(cvault, paths="Notes/Insights/tidewater-grant-hub.md")
    write_rule(cvault, ceiling=6)
    final_guard = egress.guard_working_set

    def withdraw_task(root, packet, release, **arguments):
        write_rule(cvault, ceiling=0)
        _reset_governance_caches()
        return final_guard(root, packet, release, **arguments)

    monkeypatch.setattr(egress, "guard_working_set", withdraw_task)
    with request_scope(_external()):
        packet = commands.op_activate_context(
            cvault, turn="review the Kestrel Hiring Plan", trigger="task_refresh",
            conversation={"focus": "review the Tidewater Grant"},
        )
    assert packet["intent_resolution"] == {"status": "resolved"}
    assert packet["abstained"] is True
    assert not packet["units"] and not packet["anchors"]


def test_insufficient_budget_for_the_task_does_not_claim_an_unresolved_subject(cvault: Path) -> None:
    class LimitedHeadroom(request_budget.RequestBudget):
        def remaining(self):
            return 1.03

    token = request_budget.set_current(LimitedHeadroom(seconds=6))
    try:
        packet = commands.op_activate_context(
            cvault, turn="review the Tidewater Grant", trigger="task_refresh",
            conversation={"focus": "review the Harbor Lantern Budget"},
        )
    finally:
        request_budget.reset_current(token)
    assert packet["abstention"] == {"reason": "unavailable"}
    assert packet["intent_resolution"] == {"status": "unavailable"}


def test_old_calls_keep_their_packet_and_do_not_claim_agent_refresh(cvault: Path) -> None:
    arguments = {
        "turn": "review the Tidewater Grant",
        "conversation": {"focus": "preferences for the Harbor Lantern Budget"},
    }
    omitted = commands.op_activate_context(cvault, **arguments)
    explicit = commands.op_activate_context(cvault, **arguments, trigger="user_turn")
    assert {key: omitted.get(key) for key in PARITY_BLOCKS} == {
        key: explicit.get(key) for key in PARITY_BLOCKS
    }
    assert "trigger" not in omitted and "intent_resolution" not in omitted


def test_unavailable_refresh_is_labelled_and_does_not_invent_intent(
    cvault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    packet = commands.op_activate_context(
        cvault, turn="review the Tidewater Grant", trigger="task_refresh", initiator="agent",
        conversation={"focus": "review the Harbor Lantern Budget"},
    )
    assert packet["abstention"] == {"reason": "disabled"}
    assert packet["intent_resolution"] == {"status": "unavailable"}
    assert packet["trigger"] == "task_refresh" and packet["initiator"] == "agent"


def test_declared_initiator_changes_no_context_and_logs_no_human_or_task_text(
    cvault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "activation-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    arguments = {
        "turn": "review the Tidewater Grant", "trigger": "task_refresh", "client": "codex",
        "conversation": {"focus": "preferences for the Harbor Lantern Budget"},
    }
    first = commands.op_activate_context(cvault, **arguments, initiator="human")
    second = commands.op_activate_context(cvault, **arguments, initiator="agent")
    assert {key: first.get(key) for key in PARITY_BLOCKS} == {
        key: second.get(key) for key in PARITY_BLOCKS
    }
    rows = (logs / "activations.jsonl").read_text()
    assert arguments["turn"] not in rows
    assert arguments["conversation"]["focus"] not in rows
    assert all(row["client"] == "codex" for row in map(json.loads, rows.splitlines()))


def test_generated_mcp_cli_and_rest_share_refresh_semantics(
    cvault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = {
        "turn": "review the Tidewater Grant", "trigger": "task_refresh", "initiator": "agent",
        "conversation": {"focus": "preferences for the Harbor Lantern Budget"}, "max_chars": 2000,
    }
    expected = commands.op_activate_context(cvault, **arguments)
    mcp = _surface_server(monkeypatch)
    with library_scope():
        result = asyncio.run(mcp.call_tool("activate_context", arguments, run_middleware=False))
    response = TestClient(mcp.http_app()).post(
        "/api/activate_context", json=arguments, headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text
    code, output = _run_cli([
        "activate", arguments["turn"], "--trigger", "task_refresh", "--initiator", "agent",
        "--focus", arguments["conversation"]["focus"], "--max-chars", "2000", "--json",
    ], capsys)
    assert code == 0, output
    packets = (result.structured_content, response.json()["data"], json.loads(output)["data"])
    assert expected["abstained"] is False and expected["units"]
    for packet in packets:
        assert {key: packet.get(key) for key in (*PARITY_BLOCKS, "trigger", "intent_resolution", "initiator")} == {
            key: expected.get(key) for key in (*PARITY_BLOCKS, "trigger", "intent_resolution", "initiator")
        }
