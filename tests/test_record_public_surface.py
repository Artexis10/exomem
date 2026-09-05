from __future__ import annotations

import json
import tempfile
from pathlib import Path

from record_fixtures import copy_x3_fixture

from exomem import commands, mutation_terminal, record_formats, record_memory, records
from exomem import graph_sync
from exomem import hosted_gateway as gateway
from exomem import structured_collections as collections
from exomem.writer_lease import LeaseConfig, LeaseManager

RECORD_ACTIONS = (
    "describe",
    "validate",
    "inspect",
    "query",
    "create",
    "append",
    "update",
    "revise",
    "rebaseline",
)


def test_record_command_exposes_the_lifecycle_arguments_and_selector_routing() -> None:
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")

    assert tuple(sorted(record_memory.ACTIONS)) == tuple(sorted(RECORD_ACTIONS))
    assert {param.name for param in command.params} >= {
        "action",
        "collection",
        "manifest_path",
        "manifest_text",
        "expected_manifest_hash",
        "expected_container_hash",
        "acknowledged_gap_codes",
        "why",
    }
    assert record_memory._ACTION_FIELDS["revise"] == frozenset(
        {
            "collection",
            "manifest_text",
            "expected_manifest_hash",
            "expected_container_hash",
            "why",
        }
    )
    assert record_memory._ACTION_FIELDS["rebaseline"] == frozenset(
        {
            "collection",
            "expected_manifest_hash",
            "expected_container_hash",
            "acknowledged_gap_codes",
            "why",
        }
    )
    assert all(
        commands.invocation_is_read_only(command, {"action": action})
        for action in ("describe", "validate", "inspect", "query")
    )
    assert all(
        not commands.invocation_is_read_only(command, {"action": action})
        for action in ("create", "append", "update", "revise", "rebaseline")
    )


def test_record_descriptions_teach_observed_state_and_proposal_before_creation() -> None:
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")
    params = {param.name: param.help for param in command.params}

    assert "observed" in command.description.lower()
    assert "Planning" in command.description
    assert "propose" in command.description.lower()
    assert "describe" in params["action"]
    assert "rebaseline" in params["action"]
    assert "revise" in params["manifest_text"]
    assert "revise" in params["expected_manifest_hash"]
    assert "rebaseline" in params["acknowledged_gap_codes"]


def test_compact_bootstrap_puts_record_route_before_semantic_authoring() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    payload = commands.op_bootstrap(root, profile="compact")
    serialized = json.dumps(payload, ensure_ascii=False).encode()

    assert "record" in payload["simple_actions"]
    assert "record" in payload["front_door_actions"]
    assert payload["simple_actions"]["record"]["route"] == {
        "tool": "record_memory",
        "args": {"action": "inspect"},
    }
    assert payload["front_door_actions"]["record"]["primary_tools"] == ["record_memory"]
    # An absolute offset is a PROXY for "reachable early", and the real ordering
    # property is the relative assertion on the next line. The proxy is re-cut
    # rather than nudged: the delegation envelope put 1,796 bytes of authority
    # contract inside `engagement`, which precedes the action catalogue by
    # design, and 8,192 had no margin left for anything that legitimately sits
    # in front. 10 KiB is still the first sixth of a ~63 KB payload.
    assert serialized.find(b'"record"') < 10_240
    assert serialized.find(b'"record"') < serialized.find(b'"semantic_authoring"')
    # The compact payload's SIZE budget is not asserted here. It lives in
    # `tests/test_bootstrap_compact_budget.py::COMPACT_BYTE_CEILING`, which owns
    # the constraint and records why the number is what it is. This test's
    # subject is placement -- that `record` is reachable early and ahead of
    # `semantic_authoring` -- and the `< 8192` offsets above are what pin that.
    #
    # A duplicate ceiling used to sit on this line, undocumented and 656 bytes
    # lower than the real one. The lower number silently became the gate, so
    # growth that the owning test had deliberately pre-authorised failed here
    # instead, in a test that says nothing about budgets and offers no rationale
    # to weigh the failure against. One budget, in the file that explains it.


def test_hosted_records_v2_is_additive_and_v1_remains_unchanged() -> None:
    v1 = commands.product_commands_for_profile("hosted-alpha-agent-v1", "rest")
    v2 = commands.product_commands_for_profile("hosted-alpha-agent-v2", "rest")

    assert tuple(command.name for command in v2) == (*tuple(command.name for command in v1), "record_memory")
    assert "record_memory" not in {command.name for command in v1}
    descriptor = gateway.hosted_agent_surface_descriptor("hosted-alpha-agent-v2")
    assert descriptor.profile == "hosted-alpha-agent-v2"
    assert descriptor.product_commands == tuple(command.name for command in v2)
    contract = gateway.build_agent_gateway_contract(profile="hosted-alpha-agent-v2")
    record_tool = next(entry["mcp_tool"] for entry in contract["commands"] if entry["name"] == "record_memory")
    assert record_tool["inputSchema"]["properties"]["action"]["enum"] == list(RECORD_ACTIONS)


def test_public_revise_keeps_the_closed_receipt_through_graph_handoff_and_replay(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item={
            "occurred_on": "2026-08-03",
            "title": "Pull",
            "status": "completed",
            "movements": [],
        },
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record a session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")
    manager = LeaseManager(LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "state")}))
    kwargs = {
        "action": "revise",
        "collection": current.path,
        "manifest_text": (tmp_path / current.path).read_text(encoding="utf-8").replace(
            "title:", "title: Revised", 1
        ),
        "expected_manifest_hash": current.manifest_version.hash,
        "expected_container_hash": records.lifecycle_guards(current, current_snapshot)[
            "expected_container_hash"
        ],
        "why": "clarify collection title",
        "response_detail": "compact",
    }

    first = manager.invoke(
        command,
        (tmp_path,),
        kwargs,
        idempotency_key="lifecycle-revise-graph-handoff",
        public_idempotency_key="lifecycle-revise-graph-handoff",
    )
    receipt = {
        key: first[key]
        for key in (
            "_record_receipt",
            "receipt_version",
            "operation",
            "collection_id",
            "item_key",
            "before_item_hash",
            "after_item_hash",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_container_hash",
            "after_container_hash",
            "affected_paths",
            "payload_hash",
            "outcome",
            "audit_correlation",
            "continuity",
            "acknowledged_gap_codes",
            "gap_fingerprint",
            "checkpoint_snapshot_hash",
            "minimum_reader_version",
        )
    }
    assert mutation_terminal.valid_record_receipt(receipt)
    # The write no longer joins its own graph rebuild (#576/#588), so its
    # terminal reports what was true at commit -- `pending` while the rebuild is
    # still in flight, `completed` if it had already landed. Which of the two is
    # a race, so asserting either here would be flaky. What is not a race, and
    # is what "graph handoff" in this test's name actually means, is that the
    # handoff converges: join the flight and require the graph to be current.
    assert first["graph_sync"] != "failed"
    graph_sync.await_active_rebuild(tmp_path, state_root=tmp_path / "state")
    assert graph_sync.status(tmp_path)["state"] == "current"
    history_before_replay = records.agent_audit_history(tmp_path, current.path)
    lifecycle_event = history_before_replay["events"][0]
    assert {
        key: lifecycle_event[key]
        for key in (
            "operation",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_container_hash",
            "after_container_hash",
        )
    } == {
        key: receipt[key]
        for key in (
            "operation",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_container_hash",
            "after_container_hash",
        )
    }

    replay = manager.invoke(
        command,
        (tmp_path,),
        kwargs,
        idempotency_key="lifecycle-revise-graph-handoff",
        public_idempotency_key="lifecycle-revise-graph-handoff",
    )

    assert replay["status"] == "replayed"
    assert replay["mutated"] is False
    assert records.agent_audit_history(tmp_path, current.path) == history_before_replay
    assert {key: replay[key] for key in receipt} == receipt
