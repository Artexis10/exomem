from __future__ import annotations

from pathlib import Path


def test_compact_full_and_selected_knowledge_pack_share_workflow_contract_projection(
    tmp_path: Path,
) -> None:
    from exomem import commands, knowledge_packs, workflow_contracts

    compact = commands.op_bootstrap(tmp_path, profile="compact")
    full = commands.op_bootstrap(tmp_path, profile="full")
    portable = workflow_contracts.portable_projection()
    identity = {key: portable[key] for key in ("family", "schema_version", "digest")}

    assert compact["workflow_contracts"]["portable"] == identity
    assert compact["workflow_contracts"]["invariants"] == portable["invariants"]
    assert compact["workflow_contracts"]["builtin_fallback"] == portable["builtin_fallback"]
    assert compact["workflow_contracts"]["resolution_available"] is True
    assert compact["workflow_contracts"]["route"] == {
        "tool": "schema_memory",
        "subject": "workflow-contracts",
        "operation": "resolve",
    }
    assert "proactive_routing_available" not in compact["workflow_contracts"]
    assert full["workflow_contracts"]["portable"] == portable
    assert "workflow_contract" not in compact["knowledge_packs"]["selected"]
    assert full["knowledge_packs"]["selected"]["workflow_contract"] == portable
    assert knowledge_packs.workflow_contract_projection() == portable
