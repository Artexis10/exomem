"""Free canonical-state checks that prevent a tool-surface comparison from hiding writes."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from membench.utility.switch_eval.state import Slot
from membench.utility.tool_selection.cases import load_cases
from membench.utility.tool_selection.executor import arm_spec, seed_case, seeding_env, snapshot_slot
from membench.utility.tool_selection.recorder import CanonicalLog
from membench.utility.tool_selection.resolve import product_resolver
from membench.utility.tool_selection.score import compact_calls, score_session
from membench.utility.tool_selection.vault import read_end_state, seed_vault

CASES = Path(__file__).parents[1] / "benchmarks/membench/utility/tool_selection/round_two_cases.json"


def source_case(ident):
    return next(case for case in load_cases(CASES) if case.case_id == ident)


def session(before, after, *, answer=None):
    return {"arm": "grouped", "repeat": 1, "outcome": "ok", "answer": answer,
            "facts": {"end_state_before": before, "end_state_after": after}}


def test_preference_clear_scores_only_the_requested_context_and_retains_raw_seed_bytes(tmp_path):
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case("r2-d03")
    vault = tmp_path / "vault"
    seed_vault(vault, case.seed_steps())
    before = read_end_state(vault)
    with request_scope(owner_principal(surface="mcp")):
        commands.op_configure_memory(vault, action="clear", context="conversation",
                                     expected_revision=before["preferences"]["revision"])
    after = read_end_state(vault)

    scored = score_session(case, session(before, after), [], product_resolver())
    assert scored.success, scored.reasons
    assert before["evidence"]["machine"]
    collateral = json.loads(json.dumps(after))
    collateral["preferences"]["contexts"].pop("coding")
    assert not score_session(case, session(before, collateral), [], product_resolver()).success


def test_response_recorder_catches_a_write_then_rollback_despite_an_error_code(tmp_path, monkeypatch):
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case("r2-d03")
    vault = tmp_path / "vault"
    seed_vault(vault, case.seed_steps())
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    log_path = tmp_path / "tap.jsonl"
    recorder = CanonicalLog(log_path)
    recorder.write("ready_finished", outcome="ready")
    before = read_end_state(vault)
    with request_scope(owner_principal(surface="mcp")):
        commands.op_configure_memory(vault, action="clear", context="conversation",
                                     expected_revision=before["preferences"]["revision"])
    recorder.write("call", name="configure_memory", ordinal=1,
                   arguments={"action": "clear", "context": "conversation"}, outcome="served",
                   result={"structuredContent": {"code": "AFTER_COMMIT_FAILURE", "_terminal": "exomem.mutation-terminal",
                                                 "version": 1, "state": "committed", "mutated": True}})
    # Restore the exact canonical seed bytes to expose the final-snapshot blind spot.
    import base64

    from exomem import prominence_preferences, state_paths

    for relative, raw in before["evidence"]["machine"].items():
        path = state_paths.vault_state_dir(vault) / relative
        if path.is_relative_to(prominence_preferences._preference_path(vault, "owner").parent):
            path.write_bytes(base64.b64decode(raw["base64"]))
    after = read_end_state(vault)
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    read_case = replace(case, params={**case.params, "kind": "read", "expected": {
        "target_operations": [], "canonical_end_state": {"answer": {"mode": "text", "value": "ok"}},
    }})
    scored = score_session(read_case, session(events[0]["canonical_state"], after,
                                             answer='{"answer":"ok"}'), compact_calls(events), product_resolver())

    assert not scored.state_changed
    assert scored.committed_effects == scored.unexpected_mutations == 1
    assert scored.write_calls_served == 1
    assert scored.wrong_write and not scored.success


def test_seed_machine_state_is_restorable_only_with_its_stable_vault_path_and_principal(tmp_path):
    case = source_case("r2-d03")
    run = tmp_path / "run"
    run.mkdir()
    spec = arm_spec("grouped", ("*",))
    seeded = seed_case(run, case, parent_env=dict(os.environ), spec=spec)
    slot = Slot(run / "slot")
    source = run / "seeded" / case.case_id
    shutil.copytree(source / "vault", slot.vault)
    shutil.copytree(source / "state", slot.state)
    env = seeding_env(dict(os.environ), slot, spec)

    restored = snapshot_slot(slot, env, tmp_path / "restored.json")
    admitted = json.loads((source / "admitted-state.json").read_text())

    assert seeded["principal"] == "owner"
    assert restored["preferences"] == admitted["preferences"]
    assert restored["review"] == admitted["review"]
    assert restored["evidence"]["machine"] == admitted["evidence"]["machine"]


def test_an_unresolved_error_effect_is_reported_unknown_instead_of_zero():
    case = source_case("r2-d03")
    call = {"name": "configure_memory", "arguments": {"action": "clear", "context": "conversation"},
            "ordinal": 1, "outcome": "served", "errored": True, "codes": ["AFTER_COMMIT_FAILURE"]}
    state = {"collections": {}}

    result = score_session(case, session(state, state), [call], product_resolver())

    assert result.effect_unknown_calls == 1
    assert not result.scored


def test_known_read_task_write_survives_an_unreadable_snapshot():
    case = source_case("r2-d05")
    unavailable = {"collections": {}, "files": {}, "unknown": [{"domain": "media", "error": "unavailable"}]}
    call = {"name": "configure_memory", "arguments": {"action": "clear", "context": "conversation"},
            "ordinal": 1, "outcome": "served", "errored": False, "codes": [],
            "terminals": [{"_terminal": "exomem.mutation-terminal", "version": 1,
                           "state": "committed", "mutated": True}]}

    result = score_session(case, session(unavailable, unavailable), [call], product_resolver())

    assert not result.scored
    assert result.state_changed is None
    assert result.write_calls_served == result.committed_effects == result.unexpected_mutations == 1
    assert result.wrong_write


@pytest.mark.parametrize("ident,commit", [("r2-d01", False), ("r2-d02", True)])
def test_observation_key_accepts_only_the_declared_unit_change_and_preserves_the_sibling(tmp_path, ident, commit):
    from exomem import commands, memory_refs, semantic_units
    from exomem import vault as product_vault
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case(ident)
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    path = case.params["fixture"]["path"]
    assert before["files"][path]["frontmatter"]["status"] == "active"
    assert before["files"][path]["units"] == case.params["fixture"]["units"]
    source = (root / path).read_text()
    document = semantic_units.parse_semantic_units(
        source, path=path,
        parent_ref=memory_refs.memory_ref(before["files"][path]["frontmatter"]["exomem_id"]),
    )
    current = next(unit for unit in document.units if unit.anchor == "cache-window")
    request = {**case.params["fixture"]["units"][0], "content": "The cache window is eight minutes.",
               "path": path, "unit_ref": current.unit_ref, "expected_fingerprint": current.fingerprint,
               "expected_hash": product_vault.content_hash(source)}
    with request_scope(owner_principal(surface="mcp")):
        preview = commands.op_observe_memory(root, operation="validate", **request)
        if commit:
            commands.op_observe_memory(root, operation="update", **request,
                                       transition_token=preview["semantic"]["transition_token"],
                                       relation_disposition="reviewed_none",
                                       relation_review_hash=preview["semantic"]["transition_hash"],
                                       relation_review_reason="The fixture asserts no relation to another page.")
    after = read_end_state(root)
    answer = None if commit else '{"answer":"validated"}'

    calls = compact_calls([{"event": "call", "name": "observe_memory", "ordinal": 1,
                            "arguments": {"operation": "validate", **request}, "outcome": "served",
                            "result": {"structuredContent": preview}}])
    result = score_session(case, session(before, after, answer=answer), calls, product_resolver())

    assert result.success, result.reasons
    if commit:
        collateral = json.loads(json.dumps(after))
        collateral["files"][path]["units"][1]["content"] = "An unrelated change."
        assert not score_session(case, session(before, collateral), [], product_resolver()).success
    else:
        wrong = {**request, "tags": ["different"], "context": "a different context"}
        with request_scope(owner_principal(surface="mcp")):
            wrong_preview = commands.op_observe_memory(root, operation="validate", **wrong)
        wrong_calls = compact_calls([{"event": "call", "name": "observe_memory", "ordinal": 1,
                                      "arguments": {"operation": "validate", **wrong}, "outcome": "served",
                                      "result": {"structuredContent": wrong_preview}}])
        assert not score_session(case, session(before, after, answer=answer), wrong_calls, product_resolver()).success


@pytest.mark.parametrize("ident,commit", [("r2-d06", False), ("r2-d07", True)])
def test_file_key_checks_preview_and_once_only_append_on_real_markdown(tmp_path, ident, commit):
    import base64

    from exomem import artifact_role_state, commands, due_state, state_paths
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case(ident)
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    if commit:
        # Match common readiness: post-commit hygiene updates an already warmed projection, never a cold full scan.
        with request_scope(owner_principal(surface="mcp")):
            due_state.reconcile(root)
    before = read_end_state(root)
    request = {"path": case.params["fixture"]["path"], "content": "\n" + case.params["fixture"]["append"] + "\n"}
    with request_scope(owner_principal(surface="mcp")):
        preview = commands.op_manage_memory_file(root, operation="append", validate_only=True, **request)
        if commit:
            commands.op_manage_memory_file(root, operation="append", **request,
                                           semantic_transition_token=preview["transition_token"],
                                           relation_disposition="reviewed_none",
                                           relation_review_hash=preview["transition_hash"],
                                           relation_review_reason="The fixture asserts no relation to another page.")
    after = read_end_state(root)
    answer = None if commit else '{"answer":"validated"}'

    calls = compact_calls([{"event": "call", "name": "manage_memory_file", "ordinal": 1,
                            "arguments": {"operation": "append", "validate_only": True, **request}, "outcome": "served",
                            "result": {"structuredContent": preview}}])
    result = score_session(case, session(before, after, answer=answer), calls, product_resolver())

    assert result.success, result.reasons
    if commit:
        state_root = state_paths.vault_state_dir(root)
        for owned in (artifact_role_state.store_path(root), due_state.emission_path(root)):
            relative = owned.relative_to(state_root).as_posix()
            assert relative not in after["machine"]
            assert base64.b64decode(after["evidence"]["machine"][relative]["base64"]) == owned.read_bytes()
        due = due_state.state_path(root).relative_to(state_root).as_posix()
        assert due in after["machine"]
        collateral = json.loads(json.dumps(after))
        collateral["machine"][due]["value"]["writes"] = "an unrelated canonical change"
        assert not score_session(case, session(before, collateral), [], product_resolver()).success


def test_meaning_question_key_binds_page_evidence_and_rejects_a_second_question(tmp_path):
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case("r2-d04")
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    question = case.key.question
    with request_scope(owner_principal(surface="mcp")):
        commands.op_review_memory(root, mode="vocabulary", path=question["anchor_path"],
                                  query=question["question"], family=question["family"])
    after = read_end_state(root)

    result = score_session(case, session(before, after), [], product_resolver())

    assert result.success, result.reasons
    collateral = json.loads(json.dumps(after))
    items = collateral["review"]["vocabulary"]["items"]
    items["another-question"] = next(iter(items.values()))
    assert not score_session(case, session(before, collateral), [], product_resolver()).success


@pytest.mark.parametrize("ident,commit", [("r2-d08", False), ("r2-d09", True)])
def test_registry_key_checks_the_exact_delta_and_retained_history(tmp_path, ident, commit):
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    case = source_case(ident)
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    expected = case.params["expected"]["canonical_end_state"]
    delta = expected["registry"]["delta"] if commit else expected["preview_delta"]
    with request_scope(owner_principal(surface="mcp")):
        preview = commands.op_schema_memory(root, operation="propose", subject=case.params["fixture"]["subject"], proposal=delta)
        assert preview["valid"] is True
        if commit:
            commands.op_schema_memory(root, operation="save", subject=case.params["fixture"]["subject"],
                                      proposal=delta, expected_hash=preview["expected_hash"], why=case.key.registry["reason"])
    after = read_end_state(root)
    calls = compact_calls([{"event": "call", "name": "schema_memory", "ordinal": 1,
                            "arguments": {"operation": "propose", "subject": case.params["fixture"]["subject"], "proposal": delta},
                            "outcome": "served", "result": {"structuredContent": preview}}])

    result = score_session(case, session(before, after, answer=None if commit else '{"answer":"valid"}'), calls, product_resolver())

    assert result.success, result.reasons


def test_media_status_uses_source_empty_ledger_facts_and_creates_no_ledger(tmp_path):
    from exomem import commands

    case = source_case("r2-d05")
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    result = commands.op_process_media(root, operation="status")
    after = read_end_state(root)

    assert before["media"] == after["media"] == {"status": "absent", "tables": {}}
    assert len(result["jobs"]) == case.key.answer["jobs"]
    assert result["reconciliation_required_count"] == case.key.answer["reconciliation_required"]
    scored = score_session(case, session(before, after, answer=json.dumps({"answer": case.key.answer})), [], product_resolver())
    assert scored.success, scored.reasons


def test_failed_canonical_parse_stays_unknown_instead_of_becoming_an_empty_vault(tmp_path):
    case = source_case("r2-d06")
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    (root / case.params["fixture"]["path"]).write_text("---\ntitle: [invalid\n---\n")

    after = read_end_state(root)
    result = score_session(case, session(before, after, answer='{"answer":"validated"}'), [], product_resolver())

    assert after["unknown"]
    assert not result.scored
    assert result.state_changed is None


def test_derived_handoff_bytes_are_evidence_without_becoming_authored_state(tmp_path):
    from exomem import state_paths

    case = source_case("r2-d03")
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    state = state_paths.vault_state_dir(root)
    (state / ".graph-sync.json").write_bytes(b"an incomplete derived handoff")

    projected = read_end_state(root)

    assert not projected["unknown"]
    assert ".graph-sync.json" not in projected["machine"]
    assert projected["evidence"]["machine"][".graph-sync.json"]["bytes"] > 0


def test_run_refuses_changed_case_payload_before_overwriting_saved_cases(tmp_path, monkeypatch):
    from epistemic.journeys.f27_replay import (
        AgentEnvelope,
        JourneySetupError,
        refuse_unsafe_out_dir,
    )
    from membench.utility.switch_eval.state import StateError
    from membench.utility.tool_selection import __main__ as driver
    from membench.utility.tool_selection import executor
    from membench.utility.tool_selection.cases import dev_cases

    try:
        refuse_unsafe_out_dir(tmp_path)
    except JourneySetupError:
        pytest.skip("requires a marker-free run root; the actor isolation guard remains unchanged")
    monkeypatch.setattr(driver, "_envelope", lambda args: AgentEnvelope(executable=Path("unused"), version="fixture"))

    def stop_before_actors(*args, **kwargs):
        raise StateError("fixture intentionally stops after frozen input capture")

    monkeypatch.setattr(executor, "seed_case", stop_before_actors)
    case = dev_cases()[0].as_dict()
    source = tmp_path / "case-input.json"
    source.write_text(json.dumps([case]))
    run = tmp_path / "run"
    argv = ["run", "--cases", str(source), "--run", str(run), "--actor-model", "fixture-model"]
    assert driver.main(argv) == driver.EXIT_REFUSED
    original = (run / "cases.json").read_bytes()
    case["params"]["target"]["value"] += 1
    source.write_text(json.dumps([case]))

    with pytest.raises(SystemExit, match="holds other inputs"):
        driver.main(argv)

    assert (run / "cases.json").read_bytes() == original


def test_adoption_plan_scores_independent_items_and_detects_original_changes_and_extra_runs(tmp_path, monkeypatch):
    import datetime as dt

    from membench.utility.tool_selection.development import admit_fixture
    from membench.utility.tool_selection.vault import adoption_runs, markdown_facts

    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.kbdir import kb_dirname, kb_prefix

    case = source_case("r2-d10")
    # Capture this attempt's local date before any product output, as the source reference requires.
    case = replace(case, params={**case.params, "fixture": {
        **case.params["fixture"], "case_date": dt.date.today().isoformat(),
    }})
    root = tmp_path / "vault"
    seed_vault(root, case.seed_steps())
    before = read_end_state(root)
    admit_fixture(case.params, before)
    run_id = next(iter(adoption_runs(before).values()))["run_id"]
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    log_path = tmp_path / "adoption-tap.jsonl"
    recorder = CanonicalLog(log_path)
    recorder.write("ready_finished", outcome="ready")
    with request_scope(owner_principal(surface="mcp")):
        status = commands.op_adoption_studio(root, action="status", run_id=run_id)
        recorder.write("call", name="adoption_studio", ordinal=1, arguments={"action": "status", "run_id": run_id},
                       outcome="served", result={"structuredContent": status})
        planned = commands.op_adoption_studio(root, action="plan", run_id=run_id)
        recorder.write("call", name="adoption_studio", ordinal=2, arguments={"action": "plan", "run_id": run_id},
                       outcome="served", result={"structuredContent": planned})
    after = read_end_state(root)
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    result = score_session(case, session(events[0]["canonical_state"], after), compact_calls(events), product_resolver())

    assert planned["phase"] == "planned" and not planned.get("continuation")
    assert result.success, result.reasons
    assert result.committed_effects == 1 and result.unexpected_mutations == 0
    run_path = next(iter(adoption_runs(after)))
    extra_log = json.loads(json.dumps(after))
    log = extra_log["files"][f"{kb_dirname()}/log.md"]
    prefix_end = next((index for index, token in enumerate(log["body"])
                       if token["type"] == "heading_open" and token["tag"] == "h2"), len(log["body"]))
    entry = markdown_facts(f"## 2026-10-25 | {run_path.removeprefix(kb_prefix())}\n\nUnexpected audit text.\n")
    log["body"] = log["body"][:prefix_end] + entry + log["body"][prefix_end:]
    log["remainder"] = log["body"]
    assert not score_session(case, session(before, extra_log), [], product_resolver()).success
    wrong = json.loads(json.dumps(after))
    wrong["files"][run_path]["value"]["plan"]["items"][0]["frontmatter"]["source_type"] = "wrong-kind"
    assert not score_session(case, session(before, wrong), [], product_resolver()).success
    imported = json.loads(json.dumps(after))
    imported["files"][case.key.adoption["plan"]["items"][0]["target_path"]] = {"body": "an unauthorized import"}
    assert not score_session(case, session(before, imported), [], product_resolver()).success
    original = root / next(iter(case.params["fixture"]["originals"]))
    original.write_bytes(original.read_bytes() + b"\n")
    assert not score_session(case, session(before, read_end_state(root)), [], product_resolver()).success

    with request_scope(owner_principal(surface="mcp")):
        status = commands.op_adoption_studio(root, action="status", run_id=run_id)
    recorder.write("call", name="adoption_studio", ordinal=3, arguments={"action": "status", "run_id": run_id},
                   outcome="served", result={"structuredContent": status})
    original.write_bytes(case.params["fixture"]["originals"][original.relative_to(root).as_posix()].encode())
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    rolled_back = score_session(case, session(before, read_end_state(root)), compact_calls(events), product_resolver())
    assert rolled_back.unexpected_mutations == 1 and not rolled_back.success
    with request_scope(owner_principal(surface="mcp")):
        commands.op_adoption_studio(root, action="start", path=case.params["fixture"]["source_root"])
    assert not score_session(case, session(before, read_end_state(root)), [], product_resolver()).success


def test_real_dispatcher_read_audit_and_checkpoint_are_separate_from_plan_and_privileged_changes(tmp_path):
    import asyncio
    import base64
    import datetime as dt
    import subprocess
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from membench.utility.switch_eval.state import mcp_server_entry

    case = source_case("r2-d10")
    case = replace(case, params={**case.params, "fixture": {
        **case.params["fixture"], "case_date": dt.date.today().isoformat(),
    }})
    run = tmp_path / "run"
    spec = arm_spec("grouped", ("*",))
    seed_case(run, case, parent_env=dict(os.environ), spec=spec)
    slot = Slot(run / "slot")
    source = run / "seeded" / case.case_id
    shutil.copytree(source / "vault", slot.vault)
    shutil.copytree(source / "state", slot.state)
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True, exist_ok=True)
    env = seeding_env(dict(os.environ), slot, spec)
    env["EXOMEM_MCP_TOOL_SHAPE"] = "grouped"
    slot.turn_file.write_text("0")
    # Persist an existing synthetic credential sample, so this boundary proof survives unrelated URI repairs.
    injected = subprocess.run([sys.executable, "-c", """
from pathlib import Path
from exomem.governance import scrubber
from exomem.adoption_run import AdoptionRunStore
import os
owner = AdoptionRunStore(Path(os.environ['EXOMEM_VAULT_PATH']))
run = owner.load(owner.list_runs()[0]['run_id'])
run['errors'] = [{'reason': scrubber.CREDENTIAL_PATTERNS[0].samples[0]}]
owner.save(run)
"""], env=env, capture_output=True, text=True, timeout=20)
    assert injected.returncode == 0, injected.stderr
    admitted = snapshot_slot(slot, env, tmp_path / "fixture.json")
    from membench.utility.tool_selection.vault import adoption_runs

    run_id = next(iter(adoption_runs(admitted).values()))["run_id"]
    log = slot.runtime / "tap.jsonl"
    entry = mcp_server_entry(slot, spec, log=log, server_stderr=slot.runtime / "server.stderr", warm_search=True, budget=8)
    args = ["-m", "membench.utility.tool_selection.recorder", *entry["args"][1:]]

    async def workflow():
        parameters = StdioServerParameters(command=entry["command"], args=args, env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=45) as client:
                await client.initialize()
                await client.list_tools()
                await client.call_tool("adoption_studio", {"action": "status"})
                await client.call_tool("adoption_studio", {"action": "status", "run_id": run_id})
                await client.call_tool("adoption_studio", {"action": "plan", "run_id": run_id})

    # Keep the derived owner's connection open so shutdown deterministically leaves a WAL to checkpoint.
    index_owner = subprocess.Popen([sys.executable, "-c", """
from pathlib import Path
from exomem.working_set_index import WorkingSetIndex
import os, sys
owner = WorkingSetIndex(Path(os.environ['EXOMEM_VAULT_PATH']))
try:
    assert owner.readable()
    print('ready', flush=True)
    sys.stdin.read()
finally:
    owner.close()
"""], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert index_owner.stdout.readline().strip() == "ready"
        asyncio.run(workflow())
    finally:
        try:
            _, owner_error = index_owner.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            index_owner.kill()
            index_owner.communicate()
            raise
    assert index_owner.returncode == 0, owner_error
    events = [json.loads(line) for line in log.read_text().splitlines()]
    ready = next(event for event in events if event["event"] == "ready_finished")
    assert ready["outcome"] == "ready"
    before = ready["canonical_state"]
    after = snapshot_slot(slot, env, tmp_path / "after-shutdown.json")
    result = score_session(case, session(before, after), compact_calls(events), product_resolver())
    assert result.success, result.reasons
    assert result.read_audit_receipts == result.committed_effects == 1
    assert result.unexpected_mutations == 0
    assert after["governance"]["records"][0]["event_type"] == "credential_block"
    from exomem import working_set_index

    index = working_set_index.sidecar_path(slot.vault).name
    assert index + "-wal" in before["evidence"]["machine"]
    assert before["evidence"]["machine"][index + "-wal"]["bytes"] > 0
    assert index + "-wal" not in after["evidence"]["machine"]
    assert index not in after["machine"] and index in after["evidence"]["machine"]

    changed = subprocess.run([sys.executable, "-c", """
from pathlib import Path
from exomem.governance import store
import os
connection = store.open_connection(Path(os.environ['EXOMEM_VAULT_PATH']))
connection.execute('INSERT INTO compiled_policy (fingerprint, snapshot, compiled_at) VALUES (?, ?, ?)', ('fixture-policy', '{}', 1))
connection.commit()
connection.close()
"""], env=env, capture_output=True, text=True, timeout=20)
    assert changed.returncode == 0, changed.stderr
    altered = snapshot_slot(slot, env, tmp_path / "policy-row.json")
    rejected = score_session(case, session(after, altered), [], product_resolver())
    assert rejected.scored and not rejected.success and rejected.unexpected_mutations == 1

    event_path = next(iter(after["governance"]["event_paths"]))
    original = base64.b64decode(after["evidence"]["files"][event_path]["base64"])
    target = slot.vault / event_path
    target.write_bytes(original.replace(b'"credential_block"', b'"not-an-event"'))
    broken = snapshot_slot(slot, env, tmp_path / "altered-prior-receipt.json")
    assert broken["unknown"] and not score_session(case, session(after, broken), [], product_resolver()).scored
    target.unlink()
    missing = snapshot_slot(slot, env, tmp_path / "deleted-prior-receipt.json")
    assert missing["unknown"] and not score_session(case, session(after, missing), [], product_resolver()).scored
    target.write_bytes(original)
    changed = subprocess.run([sys.executable, "-c", """
from pathlib import Path
from exomem.governance import store
import os, sqlite3
connection = sqlite3.connect(store.sidecar_path(Path(os.environ['EXOMEM_VAULT_PATH'])))
connection.execute('PRAGMA user_version=99')
connection.close()
"""], env=env, capture_output=True, text=True, timeout=20)
    assert changed.returncode == 0, changed.stderr
    unsupported = snapshot_slot(slot, env, tmp_path / "unsupported-governance-schema.json")
    assert unsupported["unknown"] and not score_session(case, session(after, unsupported), [], product_resolver()).scored


def test_nested_operation_argument_envelope_refusal_is_counted_without_serving_a_write():
    from exomem.cli_ops import INVALID_OPERATION_ARGUMENTS, envelope

    case = source_case("r2-d06")
    state = {"collections": {}, "files": {}, "unknown": []}
    calls = compact_calls([{"event": "call", "name": "change_files", "ordinal": 1,
                            "arguments": {"operation": {"kind": "append"}, "validate_only": True},
                            "outcome": "served", "result": {"structuredContent": envelope(
                                False, error={"code": INVALID_OPERATION_ARGUMENTS, "message": "invalid arguments"},
                            )}}])
    result = score_session(case, {**session(state, state), "arm": "nested"}, calls, product_resolver())
    assert result.argument_refusals == 1
    assert result.write_calls_served == result.committed_effects == 0


@pytest.mark.parametrize("shape", ["relabeled-v3", "missing-v4-table", "extended-v4", "valid-v4"])
def test_governance_projection_rejects_false_v4_authority_and_retains_real_authorization_rows(tmp_path, shape):
    import base64

    from membench.utility.tool_selection.vault import governance_facts
    from test_governance_store_v4 import _insert_legacy_authority, _seed

    from exomem.governance import schema_v4, store
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    connection = store.open_connection(vault)
    try:
        if shape == "relabeled-v3":
            connection.execute("PRAGMA user_version=4")
        else:
            _insert_legacy_authority(connection)
            schema_v4.migrate_v3_connection(connection, _seed())
            connection.execute(
                "INSERT INTO governance_authorization_sessions "
                "(session_id, locator_digest, verifier, verifier_key_id, credential_generation, "
                "principal_id, issuer_family, cell_id, logical_vault_id, keyring_id, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("fixture-session", b"l" * 32, b"v" * 32, "fixture-key", 1, "fixture-principal", "rest-oauth",
                 "fixture-cell", "logical-vault-7", "fixture-keyring", "active", 1, 2),
            )
            if shape == "missing-v4-table":
                connection.execute("DROP TABLE governance_authorization_sessions")
            elif shape == "extended-v4":
                connection.execute("CREATE TABLE unexpected_authority (value TEXT)")
        connection.commit()
    finally:
        connection.close()

    facts = governance_facts(vault)

    if shape == "valid-v4":
        assert facts["status"] == "known"
        assert facts["tables"]["governance_authorization_sessions"][0]["session_id"] == "fixture-session"
        assert facts["tables"]["governance_authorization_sessions"][0]["verifier"] == {
            "blob_base64": base64.b64encode(b"v" * 32).decode("ascii"),
        }
        archived = next(row for row in facts["tables"]["governance_legacy_authority"]
                        if row["source_table"] == "governance_session_grants")
        assert json.loads(base64.b64decode(archived["row_json"]["blob_base64"]))["grant_id"] == "grant-v3"
    else:
        assert facts["status"] == "unreadable"


def test_retained_failed_media_fixture_survives_recorded_status_without_starting_a_worker(tmp_path):
    import asyncio
    import subprocess
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from membench.utility.switch_eval.state import mcp_server_entry
    from membench.utility.tool_selection.development import admit_fixture

    case = source_case("r2-d05")
    fixture = {"seed_kind": "media-ledger", "jobs": [{
        "binary_path": f"Knowledge Base/Evidence/Images/retained-{index}.png",
        "sidecar_path": f"Knowledge Base/Evidence/Images/retained-{index}.md",
        "media_type": "image", "do_ocr": True, "do_clip": False, "do_reembed": False,
        "state": "failed", "error": "ExtractionError: the retained image header was invalid.",
    } for index in (1, 2)]}
    case = replace(case, params={**case.params, "fixture": fixture})
    run = tmp_path / "run"
    spec = arm_spec("grouped", ("*",))
    seed_case(run, case, parent_env=dict(os.environ), spec=spec)
    slot = Slot(run / "slot")
    source = run / "seeded" / case.case_id
    shutil.copytree(source / "vault", slot.vault)
    shutil.copytree(source / "state", slot.state)
    for path in (slot.runtime, slot.cwd, slot.hook_home):
        path.mkdir(parents=True, exist_ok=True)
    env = seeding_env(dict(os.environ), slot, spec)
    env["EXOMEM_MCP_TOOL_SHAPE"] = "grouped"
    slot.turn_file.write_text("0")
    before = snapshot_slot(slot, env, tmp_path / "before.json")
    admit_fixture(case.params, before)
    log = slot.runtime / "tap.jsonl"
    entry = mcp_server_entry(slot, spec, log=log, server_stderr=slot.runtime / "server.stderr",
                             warm_search=True, budget=2)

    async def workflow():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "membench.utility.tool_selection.recorder", *entry["args"][1:]], env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=45) as client:
                await client.initialize()
                result = await client.call_tool("process_media", {"operation": "status"})
                diagnostic = subprocess.run([sys.executable, "-c", """
from pathlib import Path
from exomem.media_jobs import MediaJobStore, status
import os
vault = Path(os.environ['EXOMEM_VAULT_PATH'])
owner = MediaJobStore(vault, create=False)
assert not owner.needs_worker() and owner.worker_pid() is None
assert not status(vault, diagnostic_snapshot=True)['worker_active']
"""], env=env, capture_output=True, text=True, timeout=15)
                assert diagnostic.returncode == 0, diagnostic.stderr
                return result

    result = asyncio.run(workflow())
    after = snapshot_slot(slot, env, tmp_path / "after.json")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert not result.is_error
    assert len(result.structured_content["jobs"]) == 2
    assert result.structured_content["reconciliation_required_count"] == 0
    assert before["media"] == after["media"]
    assert len(after["media"]["tables"]["jobs"]) == 2
    assert after["media"]["tables"]["media_job_results"] == []
    assert any(event["event"] == "ready_finished" and event.get("canonical_state") for event in events)
    calls = [event for event in events if event["event"] == "call"]
    assert len(calls) == 1 and calls[0]["canonical_state"]["media"] == before["media"]
    broken = json.loads(json.dumps(after))
    broken["media"]["tables"]["jobs"][0]["last_error"] = "a different retained failure"
    with pytest.raises(ValueError, match="declared jobs"):
        admit_fixture(case.params, broken)


def test_source_manifest_key_checks_typed_composite_collection_creation_and_collateral(tmp_path):
    from membench.utility.tool_selection.development import admit_fixture
    from membench.utility.tool_selection.score import check_end_state
    from membench.utility.tool_selection.templates import _manifest_text

    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope

    declared = {
        "manifest": "Knowledge Base/Records/Calibration/_collection.md",
        "profile": "records", "title": "Calibration ledger",
        "natural_key": ["measured_on", "sample"],
        "fields": {"measured_on": ["date"], "sample": ["string"], "replicates": ["integer"],
                   "reading": ["number"], "accepted": ["boolean"]},
        "required": ["measured_on", "sample", "replicates", "reading", "accepted"],
    }
    case = source_case("r2-d05")
    case = replace(case, params={**case.params, "kind": "write", "fixture": {"seed_kind": "empty-records"},
        "expected": {"target_operations": [{"command": "record_memory", "value": "create"}],
                     "canonical_end_state": {"manifests": [declared]}}})
    vault = tmp_path / "vault"
    seed_vault(vault, case.seed_steps())
    before = read_end_state(vault)
    admit_fixture(case.params, before)
    key = case.key
    assert check_end_state(key, before, before)
    text = _manifest_text(title=declared["title"], profile=declared["profile"], source="Entries",
        natural_key=declared["natural_key"], fields={
            name: {"type": types[0], "required": True} for name, types in declared["fields"].items()})
    with request_scope(owner_principal(surface="mcp")):
        commands.op_record_memory(vault, action="create", manifest_path=declared["manifest"], manifest_text=text,
                                  why="Create the invented calibration ledger.")
    after = read_end_state(vault)

    assert check_end_state(key, before, after) == []
    assert after["collections"][declared["manifest"]]["rows"] == []
    malformed = json.loads(json.dumps(after))
    malformed["collections"][declared["manifest"]]["fields"]["accepted"]["type"] = "string"
    assert check_end_state(key, before, malformed)
    collateral = json.loads(json.dumps(after))
    collateral["preferences"]["stored"] = "suppressed"
    assert check_end_state(key, before, collateral)
    with pytest.raises(ValueError, match="without collections"):
        admit_fixture(case.params, after)
