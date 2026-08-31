"""Public workflow-contract command and bootstrap contract coverage."""

from __future__ import annotations

from pathlib import Path

import pytest


def _proposal(**overrides: object) -> dict[str, object]:
    proposal: dict[str, object] = {
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
    proposal.update(overrides)
    return proposal


def _schema(vault: Path, operation: str, **kwargs: object) -> dict:
    from exomem import commands

    return commands.op_schema_memory(
        vault, subject="workflow-contracts", operation=operation, **kwargs
    )


def test_preview_refuses_create_collision_with_the_same_code_as_save(tmp_path: Path) -> None:
    from exomem.init import init_vault

    init_vault(tmp_path)
    assert _schema(tmp_path, "save", proposal=_proposal(), why="reviewed")["saved"]["key"] == (
        "software-delivery"
    )
    colliding = _proposal(
        contract_id="123e4567-e89b-12d3-a456-426614174000",
        key="other-delivery",
    )

    preview = _schema(tmp_path, "preview", proposal=colliding)
    saved = _schema(tmp_path, "save", proposal=colliding, why="reviewed")

    assert preview == {"resolved": False, "code": "WORKFLOW_CONTRACT_PATH_CONFLICT"}
    assert saved == preview


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        (_proposal(contract_id="123e4567-e89b-12d3-a456-426614174000"), "immutable identity"),
        (_proposal(key="other-delivery"), "immutable key"),
    ],
)
def test_preview_rejects_an_update_that_save_would_refuse(
    tmp_path: Path, proposal: dict[str, object], expected: str
) -> None:
    from exomem.init import init_vault

    init_vault(tmp_path)
    saved = _schema(tmp_path, "save", proposal=_proposal(), why="reviewed")["saved"]
    preview = _schema(tmp_path, "preview", name="software-delivery", proposal=proposal)
    result = _schema(
        tmp_path,
        "save",
        name="software-delivery",
        proposal=proposal,
        expected_hash=saved["content_hash"],
        why="reviewed",
    )

    assert preview == {"resolved": False, "code": "WORKFLOW_CONTRACT_INVALID"}
    assert result == preview, expected


@pytest.mark.parametrize("operation", ("inspect", "validate", "preview", "save", "refresh"))
def test_saved_key_operations_refuse_non_key_names(tmp_path: Path, operation: str) -> None:
    kwargs: dict[str, object] = {"name": "not a saved key"}
    if operation in {"validate", "preview", "save"}:
        kwargs["proposal"] = _proposal()
    if operation in {"save", "refresh"}:
        kwargs["why"] = "reviewed"
        kwargs["expected_hash"] = "0" * 64

    result = _schema(tmp_path, operation, **kwargs)

    assert result == {"resolved": False, "code": "WORKFLOW_CONTRACT_INVALID_ARGUMENTS"}


def test_schema_memory_help_names_workflow_contracts_as_a_supported_subject() -> None:
    from exomem import commands

    assert "workflow-contracts" in (commands.op_schema_memory.__doc__ or "")


def test_bootstrap_is_bounded_sorted_and_omits_route_when_schema_is_unavailable(
    tmp_path: Path,
) -> None:
    from exomem import commands, workflow_contracts
    from exomem.capabilities import ActiveSurfaceDescriptor, active_surface
    from exomem.init import init_vault

    init_vault(tmp_path)
    for number in range(10):
        proposal = _proposal(
            contract_id=f"123e4567-e89b-12d3-a456-{number:012d}",
            key=f"scope-{number:02d}",
            title=f"Scope {number:02d}",
            scope={"projects": [f"project-{number:02d}"], "domains": [], "activities": []},
        )
        workflow_contracts.save_contract(
            tmp_path, workflow_contracts.parse_proposal(proposal), why="reviewed"
        )
    default = _proposal(
        contract_id="223e4567-e89b-12d3-a456-426614174000",
        key="default",
        title="Default",
    )
    workflow_contracts.save_contract(
        tmp_path, workflow_contracts.parse_proposal(default), why="reviewed"
    )

    full = commands.op_bootstrap(tmp_path, profile="full")["workflow_contracts"]
    assert len(full["default"]) == 1
    assert full["default"][0]["key"] == "default"
    assert [item["key"] for item in full["scoped"]] == [f"scope-{number:02d}" for number in range(8)]
    assert full["total"] == 11
    assert full["truncated"] is True

    descriptor = ActiveSurfaceDescriptor(
        surface="test", profile="reduced", tier2_enabled=False, product_commands=("bootstrap",)
    )
    with active_surface(descriptor):
        reduced = commands.op_bootstrap(tmp_path, profile="full")["workflow_contracts"]
    assert reduced["resolution_available"] is False
    assert "route" not in reduced
    assert reduced["status"] == "workflow_resolution_unavailable"


def test_compact_reduced_bootstrap_reports_honest_fallback_or_unavailability(tmp_path: Path) -> None:
    from exomem import commands, workflow_contracts
    from exomem.capabilities import ActiveSurfaceDescriptor, active_surface
    from exomem.governance import egress

    descriptor = ActiveSurfaceDescriptor(
        surface="test", profile="reduced", tier2_enabled=False, product_commands=("bootstrap",)
    )
    with active_surface(descriptor):
        empty = commands.op_bootstrap(tmp_path, profile="compact")["workflow_contracts"]
    assert empty["resolution_available"] is False
    assert empty["proactive_routing_available"] is False
    assert empty["status"] == "builtin_standalone"
    assert "route" not in empty

    workflow_contracts.contract_directory(tmp_path).mkdir(parents=True)
    (workflow_contracts.contract_directory(tmp_path) / "bad.md").write_text(
        "---\ntype: workflow-contract\nkey: bad\n---\n", encoding="utf-8"
    )
    egress.clear_decision_memo()
    with active_surface(descriptor):
        unavailable = commands.op_bootstrap(tmp_path, profile="compact")["workflow_contracts"]
    assert unavailable["resolution_available"] is False
    assert unavailable["proactive_routing_available"] is False
    assert unavailable["status"] == "workflow_resolution_unavailable"
    assert "route" not in unavailable


def test_bootstrap_never_invents_a_total_after_an_incomplete_contract_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, workflow_contracts

    workflow_contracts.contract_directory(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(workflow_contracts, "MAX_FILES", 0)
    (workflow_contracts.contract_directory(tmp_path) / "one.md").write_text(
        "not a workflow contract\n", encoding="utf-8"
    )

    projection = commands.op_bootstrap(tmp_path, profile="compact")["workflow_contracts"]

    assert projection["status"] == "workflow_resolution_unavailable"
    assert projection["findings"] == [{"code": "WORKFLOW_CONTRACT_SCAN_LIMIT", "detail": "scan bound exceeded"}]
    assert {"default", "scoped", "total", "truncated"}.isdisjoint(projection)
    assert projection["proactive_routing_available"] is False


def test_validate_uses_argument_presence_and_returns_repair_findings_for_saved_malformed_file(
    tmp_path: Path,
) -> None:
    from exomem import workflow_contracts

    mixed = _schema(
        tmp_path, "validate", name="scope-00", proposal={}
    )
    assert mixed == {"resolved": False, "code": "WORKFLOW_CONTRACT_INVALID_ARGUMENTS"}

    root = workflow_contracts.contract_directory(tmp_path)
    root.mkdir(parents=True)
    (root / "broken.md").write_text(
        "---\ntype: workflow-contract\nkey: broken\n---\n", encoding="utf-8"
    )
    repaired = _schema(tmp_path, "validate", name="broken")
    assert repaired["subject"] == "workflow-contracts"
    assert repaired["valid"] is False
    assert repaired["findings"]
    assert "resolved" not in repaired


def test_context_is_exact_at_runtime_and_in_the_published_schema() -> None:
    import json

    invalid = _schema(Path("/tmp"), "resolve", context={"unexpected": "value"})
    assert invalid == {
        "resolved": False,
        "code": "WORKFLOW_CONTRACT_INVALID_ARGUMENTS",
    }

    schema = json.loads(
        Path("tests/fixtures/mcp_tool_schemas.json").read_text(encoding="utf-8")
    )["schema_memory"]["inputSchema"]["properties"]["context"]
    context_schema = next(item for item in schema["anyOf"] if item.get("type") == "object")
    assert context_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "domain": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "activity": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
