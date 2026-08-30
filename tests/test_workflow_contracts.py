from __future__ import annotations

from pathlib import Path
from uuid import UUID


def _proposal(**overrides: object) -> dict[str, object]:
    proposal: dict[str, object] = {
        "type": "workflow-contract",
        "contract_id": "6f1c2ec5-7f14-4ce8-a54e-f94c8c95c378",
        "schema_version": 1,
        "key": "software-delivery",
        "title": "Software Delivery",
        "lifecycle": "active",
        "scope": {
            "projects": ["example-project"],
            "domains": ["software"],
            "activities": ["implementation"],
        },
        "planning": {"mode": "companion"},
        "companions": [
            {
                "key": "specification-tool",
                "name": "Specification Tool",
                "owns": ["software.acceptance-tasks", "software.requirements"],
            }
        ],
        "capture": {"durable_intent": "proactive", "observed_outcomes": "proactive"},
        "planning_transition": "propose-after-outcome",
    }
    proposal.update(overrides)
    return proposal


def test_workflow_contract_round_trips_to_a_deterministic_markdown_document(tmp_path: Path) -> None:
    from exomem import workflow_contracts

    contract = workflow_contracts.parse_proposal(_proposal())
    assert UUID(contract.contract_id).version == 4
    assert contract.fingerprint == workflow_contracts.parse_proposal(_proposal()).fingerprint

    saved = workflow_contracts.save_contract(tmp_path, contract, why="reviewed")
    path = (
        tmp_path / "Knowledge Base" / "_Schema" / "contracts" / "workflow" / "Software Delivery.md"
    )
    assert path.read_text(encoding="utf-8") == saved["content"]
    assert (
        workflow_contracts.inspect_contract(tmp_path, "software-delivery")["contract"]["key"]
        == "software-delivery"
    )


def test_unknown_scope_refuses_instead_of_falling_back(tmp_path: Path) -> None:
    from exomem import workflow_contracts

    workflow_contracts.save_contract(
        tmp_path, workflow_contracts.parse_proposal(_proposal()), why="reviewed"
    )

    result = workflow_contracts.resolve_contracts(tmp_path, {"project": "example-project"})

    assert result == {"resolved": False, "code": "WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE"}


def test_schema_memory_uses_the_workflow_contract_implementation(tmp_path: Path) -> None:
    from exomem import commands

    validation = commands.op_schema_memory(
        tmp_path,
        subject="workflow-contracts",
        operation="validate",
        proposal=_proposal(),
    )

    assert validation["valid"] is True
    saved = commands.op_schema_memory(
        tmp_path,
        subject="workflow-contracts",
        operation="save",
        proposal=_proposal(),
        why="reviewed",
    )
    assert saved["saved"]["key"] == "software-delivery"
    resolved = commands.op_schema_memory(
        tmp_path,
        subject="workflow-contracts",
        operation="resolve",
        context={"project": "example-project", "domain": "software", "activity": "implementation"},
    )
    assert resolved["source"] == "scoped"


def test_planning_execution_kind_keeps_external_companions_opaque() -> None:
    from exomem import planning

    planning._validate_execution([{"kind": "tracker.issue", "ref": "opaque://123"}])


def test_init_writes_a_durable_workflow_migration_marker(tmp_path: Path) -> None:
    from exomem.init import init_vault

    fresh = tmp_path / "fresh"
    init_vault(fresh)
    assert (fresh / "Knowledge Base" / "_Schema" / "workflow-contract-migration.yaml").read_text(
        encoding="utf-8"
    ) == "schema_version: 1\nreview_required: false\n"

    existing = tmp_path / "existing"
    (existing / "Knowledge Base" / "_Schema").mkdir(parents=True)
    init_vault(existing, force=True)
    assert (existing / "Knowledge Base" / "_Schema" / "workflow-contract-migration.yaml").read_text(
        encoding="utf-8"
    ) == "schema_version: 1\nreview_required: true\n"


def test_bootstrap_advertises_the_schema_memory_workflow_route(tmp_path: Path) -> None:
    from exomem.commands import op_bootstrap

    payload = op_bootstrap(tmp_path, profile="full")

    workflow = payload["workflow_contracts"]
    assert workflow["invariants"]["planning"] == "intended future state"
    assert workflow["resolution_available"] is True
    assert workflow["route"] == {
        "tool": "schema_memory",
        "subject": "workflow-contracts",
        "operation": "resolve",
    }


def test_review_required_marker_blocks_implicit_standalone_but_not_explicit(tmp_path: Path) -> None:
    from exomem import workflow_contracts

    workflow_contracts.ensure_migration_marker(tmp_path, review_required=True)

    assert (
        workflow_contracts.inventory_contracts(tmp_path)["status"]
        == "workflow_contract_migration_required"
    )
    assert workflow_contracts.resolve_contracts(tmp_path, {}) == {
        "resolved": False,
        "code": "WORKFLOW_CONTRACT_MIGRATION_REQUIRED",
    }
    assert (
        workflow_contracts.resolve_contracts(tmp_path, {}, name="@standalone")["resolved"] is True
    )
