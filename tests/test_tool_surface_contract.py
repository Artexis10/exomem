from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from exomem import commands, semantic_authoring, semantic_index
from exomem import server as server_module
from exomem.__main__ import main

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORING_TOOLS = (
    "remember",
    "replace_memory",
    "observe_memory",
    "edit_memory",
    "manage_memory_file",
)


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _param(command, name: str):
    return next(param for param in command.params if param.name == name)


def _build_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tier2: bool = True
):
    vault = tmp_path / "empty-vault"
    shutil.copytree(
        REPO_ROOT / "src" / "exomem" / "_scaffold" / "_Schema",
        vault / "Knowledge Base" / "_Schema",
    )
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-test-key")
    if tier2:
        monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    return server_module.build_server(require_auth=False)


def _mcp_tools(mcp) -> dict[str, dict]:
    return {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")
        for tool in asyncio.run(mcp.list_tools())
    }


def _call_bootstrap(mcp) -> dict:
    result = asyncio.run(mcp.call_tool("bootstrap", {}, run_middleware=False))
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


def _call_tool(mcp, name: str, arguments: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments, run_middleware=False))
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


def test_registry_projects_one_canonical_identity_into_every_authoring_tool() -> None:
    contract = semantic_authoring.get_semantic_authoring_contract()
    identity = f"{contract.contract_id}:v{contract.version} {contract.content_digest}"

    for name in AUTHORING_TOOLS:
        assert identity in _command(name).description


def test_registry_content_and_operation_guidance_covers_every_write_shape() -> None:
    compact = "- [category] content #tags (context) ^anchor"

    for name in ("remember", "replace_memory"):
        content_help = _param(_command(name), "content").help
        assert "## Observations" in content_help
        assert compact in content_help
        assert "open-vocabulary category" in content_help
        assert "at least one valid, non-empty semantic unit" in content_help
        assert "rich" in content_help.lower()

    observe = _command("observe_memory")
    assert compact in observe.description
    observe_content = _param(observe, "content").help
    assert compact not in observe_content
    assert "substantive" in observe_content
    assert "one Markdown line" in observe_content
    assert "sibling `category`, `tags`, and `context` fields" in observe_content
    assert "final valid semantic unit" in observe.description

    edit = _command("edit_memory")
    edit_help = _param(edit, "operation").help
    assert "final valid semantic unit" in edit_help
    assert "inactive-to-active" in edit_help

    manage = _command("manage_memory_file")
    tier2_help = " ".join(
        (_param(manage, "operation").help, _param(manage, "content").help)
    )
    assert "create, overwrite, and append" in tier2_help
    assert "same semantic precommit contract" in tier2_help
    assert "remember" in tier2_help
    assert "replace_memory" in tier2_help


def test_observe_schema_field_split_renders_one_compact_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)
    tools = _mcp_tools(mcp)
    content_help = tools["observe_memory"]["inputSchema"]["properties"]["content"][
        "description"
    ]
    assert "substantive" in content_help
    assert "sibling `category`, `tags`, and `context` fields" in content_help
    assert "- [category] content #tags (context) ^anchor" not in content_help

    relative = "Knowledge Base/Notes/Insights/field-split.md"
    page = tmp_path / "empty-vault" / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 00000000-0000-4000-8000-000000000401\n"
        "status: active\n"
        "created: 2026-07-21\n"
        "updated: 2026-07-21\n"
        "tags: []\n"
        "---\n\n"
        "# Field split\n\n"
        "## Observations\n\n"
        "- [baseline] Existing valid unit ^baseline\n",
        encoding="utf-8",
    )
    fields = {
        "path": relative,
        "category": "operating constraint",
        "content": "Keep retries bounded",
        "tags": ["reliability"],
        "context": "edge path",
    }
    validation = _call_tool(
        mcp, "observe_memory", {**fields, "operation": "validate"}
    )
    semantic = validation["semantic"]
    committed = _call_tool(
        mcp,
        "observe_memory",
        {
            **fields,
            "operation": "add",
            "transition_token": semantic["transition_token"],
            "relation_disposition": "reviewed_none",
            "relation_review_hash": semantic["transition_hash"],
            "relation_review_reason": "No honest relation exists in this synthetic page.",
        },
    )

    assert committed["mutated"] is True
    authored = page.read_text(encoding="utf-8")
    assert "- [operating_constraint] Keep retries bounded #reliability (edge path) ^" in authored
    assert "- [operating_constraint] - [" not in authored


def test_mcp_rest_openapi_and_cli_help_inherit_registry_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)
    tools = _mcp_tools(mcp)
    compact = "- [category] content #tags (context) ^anchor"
    identity = semantic_authoring.contract_identity()
    for name in AUTHORING_TOOLS:
        assert identity in tools[name]["description"]
    assert compact in tools["remember"]["inputSchema"]["properties"]["content"][
        "description"
    ]
    assert "inactive-to-active" in tools["edit_memory"]["inputSchema"]["properties"][
        "operation"
    ]["description"]
    assert "same semantic precommit contract" in tools["manage_memory_file"][
        "inputSchema"
    ]["properties"]["operation"]["description"]

    client = TestClient(mcp.http_app())
    openapi = client.get(
        "/api/openapi.json",
        headers={"Authorization": "Bearer synthetic-test-key"},
    ).json()
    remember_schema = openapi["paths"]["/api/remember"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert compact in remember_schema["properties"]["content"]["description"]
    edit_schema = openapi["paths"]["/api/edit_memory"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "final valid semantic unit" in edit_schema["properties"]["operation"][
        "description"
    ]
    assert "inactive-to-active" in edit_schema["properties"]["operation"][
        "description"
    ]
    assert semantic_authoring.AUTHORING_CONTRACT.content_digest in json.dumps(openapi)

    with pytest.raises(SystemExit) as exit_info:
        main(["remember", "--help"])
    assert exit_info.value.code == 0
    cli_help = capsys.readouterr().out
    assert "## Observations" in cli_help
    assert "[category] content #tags (context) ^anchor" in cli_help


def test_generated_capability_document_projects_registry_contract() -> None:
    script_path = REPO_ROOT / "scripts" / "generate-capabilities.py"
    spec = importlib.util.spec_from_file_location("generate_capabilities", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    generated = module.build_capabilities_markdown()
    contract = semantic_authoring.get_semantic_authoring_contract()

    assert contract.content_digest in generated
    assert "- [category] content #tags (context) ^anchor" in generated
    for name in AUTHORING_TOOLS:
        assert f"### `{name}`" in generated
        assert semantic_authoring.render_tool_guidance(name) in generated


def test_normative_mutation_changes_identity_and_every_projection() -> None:
    original = semantic_authoring.get_semantic_authoring_contract()
    normative = original.normative_dict()
    normative["version"] += 1
    normative["minimum_semantic_unit"]["rule"] = (
        "Every applicable active compiled result needs two valid, non-empty semantic units."
    )
    normative["minimum_semantic_unit"]["minimum_count"] = 2
    normative["findings"]["missing_semantic_unit"]["compact_remediation"] = (
        "Add two valid compact observations under `## Observations`."
    )
    mutated = semantic_authoring.contract_from_normative(normative)

    assert mutated.content_digest != original.content_digest
    assert semantic_authoring.contract_identity(mutated) != semantic_authoring.contract_identity(
        original
    )
    assert semantic_authoring.bootstrap_projection(mutated) != semantic_authoring.bootstrap_projection(
        original
    )
    assert semantic_authoring.render_concise(mutated) != semantic_authoring.render_concise(
        original
    )
    assert semantic_authoring.render_expanded(mutated) != semantic_authoring.render_expanded(
        original
    )
    for name in AUTHORING_TOOLS:
        before = semantic_authoring.render_tool_guidance(name, original)
        after = semantic_authoring.render_tool_guidance(name, mutated)
        assert after != before
        assert normative["minimum_semantic_unit"]["rule"] in after
        assert (
            normative["findings"]["missing_semantic_unit"]["compact_remediation"]
            in after
        )


def test_mcp_only_empty_environment_is_sufficient_to_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)
    tools = _mcp_tools(mcp)
    bootstrap = _call_bootstrap(mcp)
    contract = bootstrap["semantic_authoring"]
    remember_schema = tools["remember"]["inputSchema"]
    assert contract["semantic_roles"]["category"] != contract["semantic_roles"]["tag"]
    assert contract["semantic_roles"]["kind"] != contract["semantic_roles"]["category"]
    assert "content" in remember_schema["properties"]

    invalid = {
        "note_type": "insight",
        "title": "MCP-only repair",
        "content": "# MCP-only repair\n\nOrdinary prose only.\n",
    }
    refusal = _call_tool(mcp, "remember", invalid)
    assert refusal["success"] is False
    assert refusal["error"]["code"] == "missing_semantic_unit"
    assert refusal["mutated"] is False
    assert refusal["validation_state"] == "rejected"
    assert (
        refusal["error"]["compact_remediation"]
        == contract["findings"]["missing_semantic_unit"]["compact_remediation"]
    )
    assert (
        refusal["error"]["rich_remediation"]
        == contract["findings"]["missing_semantic_unit"]["rich_remediation"]
    )

    repaired_content = (
        "# MCP-only repair\n\n"
        f"{contract['compact']['canonical_section']}\n\n"
        "- [operating constraint] Keep retries bounded #reliability\n"
    )
    validation = _call_tool(
        mcp,
        "remember",
        {**invalid, "content": repaired_content, "validate_only": True},
    )
    assert validation["mutated"] is False
    assert validation["contract_result"]["compact_unit_count"] == 1
    committed = _call_tool(
        mcp,
        "remember",
        {
            **invalid,
            "content": repaired_content,
            "draft_id": validation["draft_id"],
            "draft_hash": validation["draft_hash"],
            "draft_token": validation["draft_token"],
        },
    )
    assert committed["mutated"] is True
    assert (tmp_path / "empty-vault" / committed["path"]).exists()


def test_missing_unit_failure_envelope_matches_mcp_rest_and_cli_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)
    invalid = {
        "title": "Facade refusal",
        "content": "# Facade refusal\n\nOrdinary prose only.\n",
    }

    mcp_payload = _call_tool(mcp, "remember", invalid)
    client = TestClient(mcp.http_app())
    response = client.post(
        "/api/remember",
        json=invalid,
        headers={"Authorization": "Bearer synthetic-test-key"},
    )
    assert response.status_code == 400
    rest_payload = response.json()
    cli_code = main(
        [
            "remember",
            "--title",
            invalid["title"],
            "--content",
            invalid["content"],
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert cli_code == 1
    assert mcp_payload == rest_payload == cli_payload
    assert mcp_payload["success"] is False
    assert mcp_payload["validation_state"] == "rejected"
    assert mcp_payload["mutated"] is False
    error = mcp_payload["error"]
    finding = semantic_authoring.AUTHORING_CONTRACT.findings["missing_semantic_unit"]
    assert error["code"] == "missing_semantic_unit"
    assert error["compact_remediation"] == finding["compact_remediation"]
    assert error["rich_remediation"] == finding["rich_remediation"]
    assert {item["code"] for item in error["findings"]} >= {
        "missing_semantic_unit"
    }


def test_observe_final_unit_removal_envelope_matches_all_facades_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)
    vault = tmp_path / "empty-vault"
    relative = "Knowledge Base/Notes/Insights/final-unit.md"
    page = vault / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 00000000-0000-4000-8000-000000000402\n"
        "status: active\n"
        "created: 2026-07-21\n"
        "updated: 2026-07-21\n"
        "tags: []\n"
        "---\n\n"
        "# Final unit\n\n"
        "## Observations\n\n"
        "- [operating constraint] Keep retries bounded #reliability ^only-unit\n",
        encoding="utf-8",
    )
    state = semantic_index.current_parent_index_state(vault, relative)
    unit = state.document.units[0]
    assert unit.unit_ref is not None
    removal = {
        "path": relative,
        "operation": "remove",
        "unit_ref": unit.unit_ref,
        "expected_fingerprint": unit.fingerprint,
        "expected_hash": state.parent_source_hash,
    }
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    mcp_payload = _call_tool(mcp, "observe_memory", removal)
    client = TestClient(mcp.http_app())
    response = client.post(
        "/api/observe_memory",
        json=removal,
        headers={"Authorization": "Bearer synthetic-test-key"},
    )
    assert response.status_code == 400
    rest_payload = response.json()
    cli_code = main(
        [
            "observe_memory",
            relative,
            "--operation",
            "remove",
            "--unit-ref",
            unit.unit_ref,
            "--expected-fingerprint",
            unit.fingerprint,
            "--expected-hash",
            state.parent_source_hash,
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert cli_code == 1
    assert mcp_payload == rest_payload == cli_payload
    assert mcp_payload["success"] is False
    assert mcp_payload["validation_state"] == "rejected"
    assert mcp_payload["mutated"] is False
    error = mcp_payload["error"]
    finding = semantic_authoring.AUTHORING_CONTRACT.findings["missing_semantic_unit"]
    assert error["code"] == "missing_semantic_unit"
    assert error["compact_remediation"] == finding["compact_remediation"]
    assert error["rich_remediation"] == finding["rich_remediation"]
    assert {item["code"] for item in error["findings"]} >= {
        "missing_semantic_unit"
    }
    after = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }
    final_state = semantic_index.current_parent_index_state(vault, relative)
    assert after == before
    assert final_state.parent_source_hash == state.parent_source_hash
    assert final_state.document.units[0].unit_ref == unit.unit_ref


def test_observe_unrelated_errors_keep_the_native_mcp_error_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _build_server(tmp_path, monkeypatch)

    with pytest.raises(ToolError, match="UNIT_REFERENCE_REQUIRED"):
        _call_tool(
            mcp,
            "observe_memory",
            {
                "path": "Knowledge Base/Notes/Insights/unrelated.md",
                "operation": "remove",
            },
        )
