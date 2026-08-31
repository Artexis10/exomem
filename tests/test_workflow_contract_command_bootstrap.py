"""Public workflow-contract command and bootstrap contract coverage."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient


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


def test_live_workflow_context_refusals_and_schema_match_across_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from conftest import initialize_vault_state_offline
    from fastmcp.exceptions import ValidationError

    from exomem import server as server_module
    from exomem.__main__ import main
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="workflow context wire test")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    mcp = server_module.build_server(require_auth=False)

    invalid_request = {
        "subject": "workflow-contracts",
        "operation": "resolve",
        "context": {"unexpected": "value"},
    }

    def cli_result(context: object) -> dict:
        assert main(
            [
                "schema_memory",
                "--subject",
                "workflow-contracts",
                "--operation",
                "resolve",
                "--context",
                json.dumps(context),
                "--json",
            ]
        ) == 0
        return json.loads(capsys.readouterr().out)

    invalid = asyncio.run(
        mcp.call_tool(
            "schema_memory",
            invalid_request,
            run_middleware=True,
        )
    )
    valid = asyncio.run(
        mcp.call_tool(
            "schema_memory",
            {
                "subject": "workflow-contracts",
                "operation": "resolve",
                "context": {"domain": None, "activity": "implementation"},
            },
            run_middleware=True,
        )
    )

    assert valid.structured_content["resolved"] is True
    assert valid.structured_content["context"] == {
        "project": ["unknown", None],
        "domain": ["absent", None],
        "activity": ["known", "implementation"],
    }

    direct = _schema(vault, "resolve", context=invalid_request["context"])
    assert invalid.structured_content == direct
    rest = TestClient(mcp.http_app()).post(
        "/api/schema_memory",
        json=invalid_request,
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
    assert rest.json() == {"success": True, "data": direct}
    assert cli_result(invalid_request["context"]) == {"success": True, "data": direct}

    null_context = {**invalid_request, "context": None}
    null_expected = _schema(vault, "resolve", context=None)
    assert null_expected == {
        "resolved": False,
        "code": "WORKFLOW_CONTRACT_INVALID_ARGUMENTS",
    }
    assert asyncio.run(
        mcp.call_tool("schema_memory", null_context, run_middleware=True)
    ).structured_content == null_expected
    null_rest = TestClient(mcp.http_app()).post(
        "/api/schema_memory",
        json=null_context,
        headers={"Authorization": "Bearer sekret"},
    )
    assert null_rest.status_code == 200, null_rest.text
    assert null_rest.json() == {"success": True, "data": null_expected}
    null_cli = subprocess.run(
        [
            str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "kb"),
            "schema_memory",
            "--subject",
            "workflow-contracts",
            "--operation",
            "resolve",
            "--context",
            "null",
            "--json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert null_cli.returncode == 0, null_cli.stderr
    assert json.loads(null_cli.stdout) == {"success": True, "data": null_expected}

    for malformed in (1, [], {}):
        request = {**invalid_request, "context": {"project": malformed}}
        expected = _schema(vault, "resolve", context=request["context"])
        assert expected == {"resolved": False, "code": "WORKFLOW_CONTRACT_INVALID"}
        assert asyncio.run(
            mcp.call_tool("schema_memory", request, run_middleware=True)
        ).structured_content == expected
        malformed_rest = TestClient(mcp.http_app()).post(
            "/api/schema_memory",
            json=request,
            headers={"Authorization": "Bearer sekret"},
        )
        assert malformed_rest.status_code == 200, malformed_rest.text
        assert malformed_rest.json() == {"success": True, "data": expected}
        assert cli_result(request["context"]) == {"success": True, "data": expected}

    non_mapping = {**invalid_request, "context": []}
    for malformed_context in ([], "text", 1, False):
        assert _schema(vault, "resolve", context=malformed_context) == {
            "resolved": False,
            "code": "WORKFLOW_CONTRACT_INVALID_ARGUMENTS",
        }
        with pytest.raises(ValidationError, match="context"):
            asyncio.run(
                mcp.call_tool(
                    "schema_memory",
                    {**invalid_request, "context": malformed_context},
                    run_middleware=True,
                )
            )
    non_mapping_rest = TestClient(mcp.http_app()).post(
        "/api/schema_memory",
        json=non_mapping,
        headers={"Authorization": "Bearer sekret"},
    )
    assert non_mapping_rest.status_code == 400
    assert non_mapping_rest.json()["error"]["code"] == "BAD_JSON"
    assert main(
        [
            "schema_memory",
            "--subject",
            "workflow-contracts",
            "--operation",
            "resolve",
            "--context",
            "[]",
            "--json",
        ]
    ) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "BAD_JSON"

    mcp_schema = next(
        tool.to_mcp_tool().model_dump(mode="json")["inputSchema"]["properties"]["context"]
        for tool in asyncio.run(mcp.list_tools())
        if tool.name == "schema_memory"
    )
    rest_schema = TestClient(mcp.http_app()).get("/api/openapi.json").json()["paths"][
        "/api/schema_memory"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][
        "context"
    ]
    assert rest_schema == mcp_schema
