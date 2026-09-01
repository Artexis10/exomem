from __future__ import annotations

from pathlib import Path

import pytest


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
    from exomem.init import init_vault
    from exomem.writer_lease import invoke_command

    init_vault(tmp_path)
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


def test_schema_memory_entity_type_save_dispatches_as_a_mutation_while_inventory_stays_lease_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands
    from exomem.init import init_vault
    from exomem.writer_lease import invoke_command, reset_managers_for_tests

    init_vault(tmp_path)
    command = next(item for item in commands.COMMANDS if item.name == "schema_memory")
    with monkeypatch.context() as isolated:
        isolated.setenv("EXOMEM_WRITER_LEASE_URL", "http://127.0.0.1:1")
        isolated.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
        isolated.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "desktop")
        isolated.setenv("EXOMEM_WRITER_LEASE_TIMEOUT", "0.05")
        isolated.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
        try:
            inventory = invoke_command(
                command,
                tmp_path,
                subject="workflow-contracts",
                operation="inventory",
            )
            assert inventory["subject"] == "workflow-contracts"
        finally:
            reset_managers_for_tests()

    saved = invoke_command(
        command,
        tmp_path,
        operation="save-entity-types",
        proposal={"schema_version": 1, "entity_types": {}},
        why="Exercise the registered mutation selector.",
        idempotency_key="schema-memory-entity-types",
    )

    assert saved["state"] == "committed"
    assert (tmp_path / "Knowledge Base" / "_Schema" / "entity-types.yaml").is_file()
