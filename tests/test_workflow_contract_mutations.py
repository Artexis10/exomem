from __future__ import annotations

from pathlib import Path


def _proposal() -> dict[str, object]:
    return {
        "type": "workflow-contract",
        "contract_id": "6f1c2ec5-7f14-4ce8-a54e-f94c8c95c378",
        "schema_version": 1,
        "key": "software-delivery",
        "title": "Software Delivery",
        "lifecycle": "active",
        "scope": {"projects": [], "domains": [], "activities": []},
        "planning": {"mode": "standalone"},
        "companions": [],
        "capture": {"durable_intent": "explicit", "observed_outcomes": "explicit"},
        "planning_transition": "explicit-only",
    }


def test_workflow_save_and_refresh_use_the_writer_lease_mutation_envelope(tmp_path: Path) -> None:
    from exomem import commands
    from exomem.writer_lease import invoke_command

    command = next(item for item in commands.COMMANDS if item.name == "schema_memory")
    saved = invoke_command(
        command,
        tmp_path,
        subject="workflow-contracts",
        operation="save",
        proposal=_proposal(),
        why="reviewed",
        idempotency_key="workflow-contract-save",
    )

    assert saved["state"] == "committed"
    assert saved["receipt_id"]
    current_hash = commands.op_schema_memory(
        tmp_path,
        subject="workflow-contracts",
        operation="inspect",
        name="software-delivery",
    )["content_hash"]
    refreshed = invoke_command(
        command,
        tmp_path,
        subject="workflow-contracts",
        operation="refresh",
        name="software-delivery",
        expected_hash=current_hash,
        why="refresh presentation",
        idempotency_key="workflow-contract-refresh",
    )
    assert refreshed["state"] == "committed"
    assert refreshed["receipt_id"]
