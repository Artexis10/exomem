from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, relation_queue, relation_registry, server, writer_lease
from exomem.__main__ import main
from exomem.governance import egress
from exomem.governance.principal import library_scope


def _product_command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _complete_relation_proposal() -> dict[str, object]:
    return {
        "requested_label": "applies_to",
        "parent": "relates_to",
        "description": "One reviewed thing applies to another.",
        "direction": "directed",
    }


def test_connect_relation_resolution_reaches_the_shared_vocabulary_leaf(
    tmp_path: Path,
) -> None:
    result = commands.op_connect_memory(
        tmp_path / "vault",
        operation="resolve-relation",
        query="a child belongs to its parent",
        requested_relation="part_of",
        path="Knowledge Base/Notes/source.md",
        target="Knowledge Base/Notes/target.md",
        limit=3,
        continuation=None,
    )

    assert result["exact_matches"][0]["canonical"] == "part_of"
    assert len(result["core_vocabulary"]) == len(relation_registry.core_registry().core)
    assert result["selected_relation"] is None
    assert result["proposed_relation"] is None


@pytest.mark.parametrize(
    "arguments, code",
    [
        ({}, "RELATION_QUERY_REQUIRED"),
        ({"query": "intent", "limit": 0}, "RELATION_LIMIT_INVALID"),
        ({"query": "intent", "limit": 65}, "RELATION_LIMIT_INVALID"),
        (
            {"query": "intent", "continuation": "not-a-continuation"},
            "RELATION_CONTINUATION_INVALID",
        ),
    ],
)
def test_connect_relation_resolution_has_one_invalid_argument_matrix(
    tmp_path: Path, arguments: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        commands.op_connect_memory(
            tmp_path / "vault", operation="resolve-relation", **arguments
        )


def test_relation_proposal_is_read_only_and_returns_a_complete_delta(tmp_path: Path) -> None:
    with library_scope():
        result = commands.op_schema_memory(
            tmp_path / "vault",
            subject="relations",
            operation="propose-relation",
            proposal=_complete_relation_proposal(),
            continuation=None,
            limit=4,
        )

    assert result["valid"] is True
    assert result["delta"] == {
        "upsert": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "One reviewed thing applies to another.",
                "direction": "directed",
                "aliases": ["applies_to"],
            }
        }
    }
    assert result["content_hash"]
    assert result["duplicate_evidence"]["extensions"]["returned"] == 0


@pytest.mark.parametrize(
    "operation, read_only",
    [
        ("propose-relation", True),
        ("save-relations", False),
    ],
)
def test_relation_schema_selectors_share_fail_closed_egress_classification(
    operation: str, read_only: bool
) -> None:
    command = _product_command("schema_memory")

    assert (
        commands.invocation_is_read_only(
            command, {"subject": "relations", "operation": operation}
        )
        is read_only
    )
    assert egress.assert_selector_covered("schema_memory", "operation", operation) == (
        "structure" if read_only else "mutation"
    )

    with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
        commands.invocation_is_read_only(
            command, {"subject": "relations", "operation": "future-relation-mode"}
        )


def test_relation_read_selectors_are_lease_free() -> None:
    connect = _product_command("connect_memory")
    schema = _product_command("schema_memory")

    assert commands.invocation_is_read_only(
        connect, {"operation": "resolve-relation"}
    )
    assert commands.invocation_is_read_only(
        schema, {"subject": "relations", "operation": "propose-relation"}
    )
    assert not commands.invocation_is_read_only(
        schema, {"subject": "relations", "operation": "save-relations"}
    )


def test_new_relation_parameters_are_generated_from_the_product_signatures() -> None:
    by_name = {command.name: command for command in commands.PRODUCT_COMMANDS}
    connect = {param.name for param in by_name["connect_memory"].params}
    schema = {param.name for param in by_name["schema_memory"].params}
    triage = {param.name for param in by_name["triage_memory"].params}

    assert {"requested_relation", "continuation"} <= connect
    assert {"date_from", "date_to", "continuation", "limit"} <= schema
    assert "source_path" in triage


@pytest.mark.parametrize(
    "arguments, cli_arguments, code",
    [
        (
            {"query": "intent", "limit": 1_000_000},
            ["--query", "intent", "--limit", "1000000"],
            "RELATION_LIMIT_INVALID",
        ),
        (
            {"query": "intent", "include_model_suggestions": True},
            ["--query", "intent", "--include-model-suggestions"],
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            {"query": "intent", "scope": "all"},
            ["--query", "intent", "--scope", "all"],
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            {"query": "intent", "depth": 999},
            ["--query", "intent", "--depth", "999"],
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            {"query": "intent", "name": "Unrelated entity"},
            ["--query", "intent", "--name", "Unrelated entity"],
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            {"query": "intent", "ref": "exomem://review/relation/example"},
            ["--query", "intent", "--ref", "exomem://review/relation/example"],
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            {"query": "intent", "why": "write-only reason"},
            ["--query", "intent", "--why", "write-only reason"],
            "INVALID_RELATION_ARGUMENT",
        ),
    ],
)
def test_real_mcp_rest_cli_resolver_rejects_unbounded_and_cross_mode_inputs(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: dict[str, object],
    cli_arguments: list[str],
    code: str,
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server.build_server(require_auth=False)
    request = {"operation": "resolve-relation", **arguments}

    mcp_result = asyncio.run(
        mcp.call_tool("connect_memory", request, run_middleware=True)
    )
    mcp_refusal = json.loads(mcp_result.content[0].text)
    assert mcp_refusal["success"] is False
    assert mcp_refusal["error"]["code"] == code

    rest_response = TestClient(mcp.http_app()).post(
        "/api/connect_memory",
        json=request,
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest_response.status_code == 400, rest_response.text
    assert rest_response.json()["error"]["code"] == code

    exit_code = main(
        [
            "connect_memory",
            "--operation",
            "resolve-relation",
            *cli_arguments,
            "--json",
        ]
    )
    cli_result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert exit_code == 1
    assert cli_result["error"]["code"] == code


def test_relation_review_source_hints_are_dispatched_to_lane_c_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, str | None]] = []

    def accept(_root: Path, **kwargs: object) -> dict[str, bool]:
        captured.append(("accept", kwargs.get("source_path")))  # type: ignore[arg-type]
        return {"accepted": True}

    def triage(_root: Path, **kwargs: object) -> dict[str, bool]:
        captured.append(("triage", kwargs.get("source_path")))  # type: ignore[arg-type]
        return {"triaged": True}

    monkeypatch.setattr(relation_queue, "accept", accept)
    monkeypatch.setattr(relation_queue, "triage", triage)
    monkeypatch.setattr(relation_queue, "is_relation_ref", lambda _ref: True)

    commands.op_connect_memory(
        tmp_path,
        operation="accept-relation",
        path="Knowledge Base/Notes/source.md",
        ref="exomem://review/relation/example",
        expected_hash="a" * 64,
        expected_fingerprint="b" * 64,
        why="reviewed",
    )
    commands.op_triage_memory(
        tmp_path,
        ref="exomem://review/relation/example",
        action="dismiss",
        source_path="Knowledge Base/Notes/source.md",
    )

    assert captured == [
        ("accept", "Knowledge Base/Notes/source.md"),
        ("triage", "Knowledge Base/Notes/source.md"),
    ]


@pytest.mark.parametrize(
    "operation, kwargs, code",
    [
        ("propose-relation", {}, "INCOMPLETE_RELATION_PROPOSAL"),
        (
            "propose-relation",
            {"proposal": _complete_relation_proposal(), "why": "not a write"},
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            "propose-relation",
            {"proposal": {"requested_label": "applies_to"}},
            "INCOMPLETE_RELATION_PROPOSAL",
        ),
        (
            "propose-relation",
            {"proposal": {**_complete_relation_proposal(), "unexpected": True}},
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            "propose-relation",
            {"proposal": {**_complete_relation_proposal(), "aliases": "not-a-list"}},
            "INVALID_RELATION_ARGUMENT",
        ),
        (
            "save-relations",
            {"proposal": {"upsert": {}}, "expected_hash": "a" * 64},
            "WHY_REQUIRED",
        ),
        (
            "save-relations",
            {"proposal": {"upsert": {}}, "why": "reviewed"},
            "EXPECTED_HASH_REQUIRED",
        ),
    ],
)
def test_relation_schema_selectors_reject_cross_mode_arguments(
    tmp_path: Path,
    operation: str,
    kwargs: dict[str, object],
    code: str,
) -> None:
    with library_scope(), pytest.raises(ValueError, match=code):
        commands.op_schema_memory(
            tmp_path / "vault", subject="relations", operation=operation, **kwargs
        )


@pytest.mark.parametrize(
    "name, arguments, cli_arguments",
    [
        (
            "connect_memory",
            {
                "operation": "resolve-relation",
                "query": "a child belongs to its parent",
                "requested_relation": "part_of",
                "path": "Knowledge Base/Notes/source.md",
                "target": "Knowledge Base/Notes/target.md",
                "limit": 3,
            },
            [
                "--operation",
                "resolve-relation",
                "--query",
                "a child belongs to its parent",
                "--requested-relation",
                "part_of",
                "--path",
                "Knowledge Base/Notes/source.md",
                "--target",
                "Knowledge Base/Notes/target.md",
                "--limit",
                "3",
            ],
        ),
        (
            "schema_memory",
            {
                "subject": "relations",
                "operation": "propose-relation",
                "proposal": _complete_relation_proposal(),
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
                "limit": 3,
            },
            [
                "--subject",
                "relations",
                "--operation",
                "propose-relation",
                "--proposal",
                json.dumps(_complete_relation_proposal()),
                "--date-from",
                "2026-01-01",
                "--date-to",
                "2026-12-31",
                "--limit",
                "3",
            ],
        ),
    ],
)
def test_real_mcp_rest_cli_facades_reach_the_same_relation_leaf(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    arguments: dict[str, object],
    cli_arguments: list[str],
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server.build_server(require_auth=False)
    expected = _product_command(name).leaf(vault, **arguments)

    mcp_result = asyncio.run(
        mcp.call_tool(name, arguments, run_middleware=True)
    ).structured_content
    if isinstance(mcp_result, dict) and set(mcp_result) == {"result"}:
        mcp_result = mcp_result["result"]
    rest_response = TestClient(mcp.http_app()).post(
        f"/api/{name}",
        json=arguments,
        headers={"Authorization": "Bearer sekret"},
    )
    exit_code = main([name, *cli_arguments, "--json"])
    cli_result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert mcp_result == expected
    assert rest_response.status_code == 200, rest_response.text
    assert rest_response.json() == {"success": True, "data": expected}
    assert exit_code == 0
    assert cli_result == {"success": True, "data": expected}


def test_mcp_and_openapi_contracts_share_relation_parameter_schemas(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server.build_server(require_auth=False)
    live_tools = {
        item.name: item.to_mcp_tool().model_dump(mode="json", by_alias=True)
        for item in asyncio.run(mcp.list_tools())
    }
    openapi = TestClient(mcp.http_app()).get("/api/openapi.json").json()
    selected = {
        "connect_memory": ("read_connections", "resolve-relation", {"requested_relation", "continuation", "limit"}),
        "schema_memory": ("read_schema", "propose-relation", {"date_from", "date_to", "continuation", "limit"}),
        "triage_memory": ("triage_memory", "decision", {"source_path"}),
    }

    for name, (public_name, kind, parameter_names) in selected.items():
        operation = live_tools[public_name]["inputSchema"]["properties"]["operation"]
        branch = next(branch for branch in operation["oneOf"] if branch["properties"]["kind"]["const"] == kind)
        mcp_properties = {**branch["properties"], **operation.get("properties", {})}
        rest_properties = openapi["paths"][f"/api/{name}"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]["properties"]
        for parameter_name in parameter_names:
            # Discovery compacts prose; both surfaces retain the same validation constraints.
            assert {
                key: value for key, value in rest_properties[parameter_name].items()
                if key != "description"
            } == {
                key: value for key, value in mcp_properties[parameter_name].items()
                if key != "description"
            }
        if "limit" in parameter_names:
            assert mcp_properties["limit"]["minimum"] == 1
            assert mcp_properties["limit"]["maximum"] == 64


def test_only_relation_delta_save_uses_the_invocation_specific_narrow_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease")
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []
    canonical = _product_command("schema_memory")
    command = replace(
        canonical,
        leaf=lambda _root, **_kwargs: events.append("leaf") or {"ok": True},
    )

    @contextmanager
    def authority_guard(**_kwargs):
        events.append("authority-enter")
        yield
        events.append("authority-exit")

    @contextmanager
    def full_guard(*_args, **_kwargs):
        events.append("full-enter")
        yield
        events.append("full-exit")

    monkeypatch.setattr(manager, "writer_authority_guard", authority_guard)
    monkeypatch.setattr(manager, "mutation_guard", full_guard)

    save = manager.invoke(
        command,
        (vault,),
        {
            "subject": "relations",
            "operation": "save-relations",
            "proposal": {"upsert": {}},
            "expected_hash": "a" * 64,
            "why": "reviewed",
        },
        read_only=False,
    )
    assert save == {"ok": True}
    assert events == ["authority-enter", "leaf", "authority-exit"]

    events.clear()
    legacy = manager.invoke(
        command,
        (vault,),
        {"subject": "relations", "operation": "infer", "save": True},
        read_only=False,
    )
    assert legacy == {"ok": True}
    assert events == ["full-enter", "leaf", "full-exit"]

    events.clear()
    proposal = manager.invoke(
        command,
        (vault,),
        {
            "subject": "relations",
            "operation": "propose-relation",
            "proposal": _complete_relation_proposal(),
        },
        read_only=True,
    )
    assert proposal == {"ok": True}
    assert events == ["leaf"]
