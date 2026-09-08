"""Public adapter coverage for the bounded vocabulary-review workflow."""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

from starlette.testclient import TestClient


def _command(name: str):
    from exomem import commands

    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def test_vocabulary_variants_are_declared_on_canonical_product_commands() -> None:
    review = _command("review_memory")
    context = _command("review_item_context")
    triage = _command("triage_memory")
    schema = _command("schema_memory")

    assert {parameter.name for parameter in review.params} >= {"mode", "limit", "continuation"}
    assert {parameter.name for parameter in context.params} >= {
        "ref",
        "expected_fingerprint",
        "max_body_chars",
        "max_related_pages",
        "continuation",
    }
    decision = next(parameter for parameter in triage.params if parameter.name == "decision")
    assert decision.schema == {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "item_ref",
                    "fingerprint",
                    "family",
                    "registry_hashes",
                    "target_versions",
                    "outcome",
                    "rationale",
                    "choice",
                ],
                "properties": {
                    "item_ref": {"type": "string"},
                    "fingerprint": {"type": "string"},
                    "family": {
                        "enum": ["entity-instance/v1", "entity-type/v1", "relation-type/v1"]
                    },
                    "registry_hashes": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "target_versions": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "outcome": {
                        "enum": ["reuse", "enrich", "propose-new", "generic", "no-edge", "defer"]
                    },
                    "rationale": {"type": "string", "minLength": 1},
                    "choice": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["canonical"],
                                "properties": {"canonical": {"type": "string"}},
                            },
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["canonical", "definition"],
                                "properties": {
                                    "canonical": {"type": "string"},
                                    "definition": {"type": "object"},
                                },
                            },
                        ]
                    },
                },
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "family": {"const": "entity-instance/v1"},
                                "outcome": {"const": "propose-new"},
                            },
                            "required": ["family", "outcome"],
                        },
                        "then": {
                            "properties": {
                                "choice": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["canonical", "definition"],
                                    "properties": {
                                        "canonical": {
                                            "type": "string",
                                            "description": "Must equal definition.name.",
                                        },
                                        "definition": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["entity_type", "name", "summary"],
                                            "properties": {
                                                "entity_type": {
                                                    "type": "string",
                                                    "pattern": "[a-z][a-z0-9-]*",
                                                },
                                                "name": {"type": "string", "minLength": 1},
                                                "summary": {
                                                    "type": "string",
                                                    "minLength": 1,
                                                },
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    }
                ],
            },
            {"type": "null"},
        ]
    }
    assert {parameter.name for parameter in schema.params} >= {
        "subject",
        "operation",
        "query",
        "requested_type",
        "continuation",
        "limit",
    }


def test_vocabulary_selectors_have_explicit_read_and_write_classification() -> None:
    from exomem import commands
    from exomem.governance import egress

    schema = _command("schema_memory")
    triage = _command("triage_memory")

    assert commands.invocation_is_read_only(
        schema,
        {"subject": "entity-types", "operation": "resolve-entity-type"},
    )
    assert not commands.invocation_is_read_only(
        triage,
        {"ref": "exomem://review/vocabulary/item", "action": "decide-vocabulary"},
    )
    assert (
        egress.assert_selector_covered("schema_memory", "operation", "resolve-entity-type")
        == "structure"
    )


def test_vocabulary_adapter_dispatches_only_its_bounded_arguments(monkeypatch) -> None:
    import exomem
    from exomem import commands

    calls: list[tuple[str, dict]] = []

    def record(name: str):
        def leaf(vault_root, **kwargs):
            calls.append((name, {"vault_root": vault_root, **kwargs}))
            return {"leaf": name}

        return leaf

    adapter = types.SimpleNamespace(
        review=record("review"),
        context=record("context"),
        decide=record("decide"),
    )
    monkeypatch.setattr(exomem, "vocabulary_review", adapter, raising=False)
    vault = Path("/tmp/vocabulary-tool-surface")
    ref = "exomem://review/vocabulary/item"
    decision = {
        "item_ref": ref,
        "fingerprint": "fingerprint",
        "family": "relation-type/v1",
        "registry_hashes": {"entity-types": "hash"},
        "target_versions": {"entity": "version"},
        "outcome": "reuse",
        "rationale": "The existing type already fits.",
        "choice": {"canonical": "relates_to"},
    }

    assert commands.op_review_memory(vault, mode="vocabulary") == {"leaf": "review"}
    assert calls.pop() == ("review", {"vault_root": vault, "limit": 4, "continuation": None})
    assert commands.op_review_memory(vault, mode="vocabulary", limit=25) == {"leaf": "review"}
    assert calls.pop() == ("review", {"vault_root": vault, "limit": 25, "continuation": None})
    assert commands.op_review_memory(vault, mode="vocabulary", state="all") == {"leaf": "review"}
    assert calls.pop() == (
        "review", {"vault_root": vault, "limit": 4, "continuation": None, "state": "all"}
    )
    assert commands.op_review_item_context(vault, ref=ref) == {"leaf": "context"}
    assert calls.pop() == (
        "context",
        {
            "vault_root": vault,
            "ref": ref,
            "expected_fingerprint": None,
            "max_body_chars": 4000,
            "max_related_pages": 8,
            "continuation": None,
        },
    )
    assert commands.op_triage_memory(
        vault, ref=ref, action="decide-vocabulary", decision=decision
    ) == {"leaf": "decide"}
    assert calls.pop() == ("decide", {"vault_root": vault, "ref": ref, "decision": decision})

    import pytest

    with pytest.raises(ValueError, match="INVALID_VOCABULARY_REVIEW_ARGUMENTS"):
        commands.op_review_memory(vault, mode="vocabulary", categories=["ignored"])
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_CONTEXT_ARGUMENTS"):
        commands.op_review_item_context(vault, ref=ref, max_history=1)
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_DECISION"):
        commands.op_triage_memory(vault, ref=ref, action="dismiss", decision=decision)
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_DECISION"):
        commands.op_triage_memory(
            vault, ref="exomem://review/ordinary", action="decide-vocabulary", decision=decision
        )


def test_entity_type_resolution_refuses_unrelated_schema_arguments(tmp_path: Path) -> None:
    import pytest

    from exomem import commands

    with pytest.raises(ValueError, match="ENTITY_TYPE_QUERY_REQUIRED"):
        commands.op_schema_memory(
            tmp_path,
            subject="entity-types",
            operation="resolve-entity-type",
        )
    with pytest.raises(ValueError, match="INVALID_ENTITY_TYPE_ARGUMENTS"):
        commands.op_schema_memory(
            tmp_path,
            subject="entity-types",
            operation="resolve-entity-type",
            query="organization",
            proposal={},
        )


def test_vocabulary_review_context_and_decision_reach_the_real_leaves(
    tmp_path: Path,
) -> None:
    import hashlib

    from exomem import commands, vocabulary_review
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vocabulary_state import VocabularyState
    from exomem.vocabulary_workflow import Evidence, make_item

    target = "Knowledge Base/Entities/Organizations/acme.md"
    evidence = "Knowledge Base/Sources/Articles/acme.md"
    for path in (target, evidence):
        page = tmp_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntype: organization\n---\nAcme facts.\n", encoding="utf-8")
    target_hash = hashlib.sha256((tmp_path / target).read_bytes()).hexdigest()
    evidence_hash = hashlib.sha256((tmp_path / evidence).read_bytes()).hexdigest()
    item = make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={target: target_hash},
        evidence=[Evidence(evidence, evidence_hash, "origin:one")],
        registry_hashes=vocabulary_review.registry_hashes(tmp_path),
        projection_status="current",
    )
    VocabularyState(tmp_path).observe(item)

    assert commands.op_review_memory(tmp_path, mode="vocabulary") == vocabulary_review.review(
        tmp_path
    )
    context = commands.op_review_item_context(tmp_path, ref=item.ref)
    decision = {
        "item_ref": context["item"]["ref"],
        "fingerprint": context["item"]["fingerprint"],
        "family": context["item"]["family"],
        "registry_hashes": context["item"]["registry_hashes"],
        "target_versions": context["item"]["target_versions"],
        "outcome": "generic",
        "rationale": "No narrower meaning is supported.",
        "choice": None,
    }
    with request_scope(owner_principal(surface="library")):
        result = commands.op_triage_memory(
            tmp_path,
            ref=item.ref,
            action="decide-vocabulary",
            decision=decision,
        )

    assert context["definitions"]["selected_relation"] is None
    assert result["state"] == "resolved_without_mutation"


def test_public_vocabulary_contract_teaches_and_accepts_entity_instance_proposals(
    tmp_path: Path,
) -> None:
    import hashlib

    from exomem import commands, vocabulary_review
    from exomem.governance.principal import library_scope
    from exomem.vocabulary_state import VocabularyState
    from exomem.vocabulary_workflow import Evidence, make_item

    source = "Knowledge Base/Sources/Articles/example-institution.md"
    page = tmp_path / source
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: source\n---\nExample Institution is active.\n", encoding="utf-8")
    version = hashlib.sha256(page.read_bytes()).hexdigest()
    item = make_item(
        family="entity-instance/v1",
        signal="agent-meaning-question",
        targets={source: version},
        evidence=[Evidence(source, version, "origin:one")],
        registry_hashes=vocabulary_review.registry_hashes(tmp_path),
        projection_status="current",
    )
    VocabularyState(tmp_path).observe(item)

    bootstrap = commands.op_bootstrap(tmp_path)["vocabulary_workflow"]
    contract = bootstrap["decision"]["choice_contracts"]["entity-instance/v1"]
    definition = contract["propose-new"]["definition"]
    assert definition["required"] == ["entity_type", "name", "summary"]
    assert definition["additional_properties"] is False
    assert definition["properties"]["entity_type"]["pattern"] == "[a-z][a-z0-9-]*"
    assert contract["propose-new"]["canonical"] == "equals definition.name"

    context = commands.op_review_item_context(tmp_path, ref=item.ref)
    assert context["choice_contract"] == contract
    decision = {
        "item_ref": context["item"]["ref"],
        "fingerprint": context["item"]["fingerprint"],
        "family": context["item"]["family"],
        "registry_hashes": context["item"]["registry_hashes"],
        "target_versions": context["item"]["target_versions"],
        "outcome": "propose-new",
        "rationale": "The source identifies a recurring institution.",
        "choice": {
            "canonical": "Example institution",
            "definition": {
                "entity_type": "organization",
                "name": "Example institution",
                "summary": "An institution providing technical education.",
            },
        },
    }
    with library_scope():
        result = commands.op_triage_memory(
            tmp_path,
            ref=item.ref,
            action="decide-vocabulary",
            decision=decision,
        )

    assert result["state"] == "proposed"


def test_entity_type_resolution_schema_and_dispatch_match_across_surfaces(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from conftest import initialize_vault_state_offline

    from exomem import commands
    from exomem import server as server_module
    from exomem.__main__ import main
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="vocabulary tool surface")
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
    request = {
        "subject": "entity-types",
        "operation": "resolve-entity-type",
        "requested_type": "organization",
        "limit": 20,
    }
    direct = commands.op_schema_memory(vault, **request)
    mcp = server_module.build_server(require_auth=False)
    mcp_result = asyncio.run(mcp.call_tool("schema_memory", request, run_middleware=True))
    rest = TestClient(mcp.http_app()).post(
        "/api/schema_memory",
        json=request,
        headers={"Authorization": "Bearer sekret"},
    )

    assert mcp_result.structured_content == direct
    assert rest.status_code == 200, rest.text
    assert rest.json() == {"success": True, "data": direct}
    assert (
        main(
            [
                "schema_memory",
                "--subject",
                "entity-types",
                "--operation",
                "resolve-entity-type",
                "--requested-type",
                "organization",
                "--limit",
                "20",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"success": True, "data": direct}

    triage_schema = next(
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True)["inputSchema"]
        for tool in asyncio.run(mcp.list_tools())
        if tool.name == "triage_memory"
    )
    decision_schema = triage_schema["properties"]["decision"]
    assert decision_schema["anyOf"][0]["additionalProperties"] is False
    assert set(decision_schema["anyOf"][0]["required"]) == {
        "item_ref",
        "fingerprint",
        "family",
        "registry_hashes",
        "target_versions",
        "outcome",
        "rationale",
        "choice",
    }
    rest_schema = (
        TestClient(mcp.http_app())
        .get("/api/openapi.json")
        .json()["paths"]["/api/triage_memory"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["decision"]
    )
    assert rest_schema == decision_schema


def test_reconcile_repairs_vocabulary_projection_only_outside_dry_run(tmp_path, monkeypatch):
    from exomem import commands, due_state, review_state, writer_lease
    from exomem.vocabulary_state import VocabularyState

    monkeypatch.setattr(
        commands.reconcile_module, "reconcile",
        lambda *args, **kwargs: types.SimpleNamespace(as_dict=lambda: {"dry_run": kwargs["dry_run"]}),
    )
    monkeypatch.setattr(writer_lease, "active_mutation_request_id", lambda: "test-request")
    monkeypatch.setattr(due_state, "reconcile", lambda *args, **kwargs: {})
    monkeypatch.setattr(review_state.ReviewStateStore, "compact", lambda *args, **kwargs: {})
    calls = []

    def rebuild(owner):
        calls.append(owner.store.vault_root)
        return {"state": "current", "rows": 3, "notifications": {"imported": 2}}

    monkeypatch.setattr(VocabularyState, "rebuild_review_projection", rebuild)
    assert "vocabulary_review_projection" not in commands.op_reconcile(tmp_path, dry_run=True)
    assert calls == []
    result = commands.op_reconcile(tmp_path)
    assert calls == [tmp_path]
    assert result["vocabulary_review_projection"] == {
        "state": "current", "rows": 3, "notifications": {"imported": 2}
    }


def test_reconcile_reports_projection_failure_without_exposing_exception_text(tmp_path, monkeypatch):
    from exomem import commands, due_state, review_state, writer_lease
    from exomem.vocabulary_state import VocabularyState

    monkeypatch.setattr(
        commands.reconcile_module, "reconcile",
        lambda *args, **kwargs: types.SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(writer_lease, "active_mutation_request_id", lambda: "test-request")
    monkeypatch.setattr(due_state, "reconcile", lambda *args, **kwargs: {})
    monkeypatch.setattr(review_state.ReviewStateStore, "compact", lambda *args, **kwargs: {})

    def unavailable(owner):
        raise OSError("private-path-and-database-details")

    monkeypatch.setattr(VocabularyState, "rebuild_review_projection", unavailable)
    result = commands.op_reconcile(tmp_path)
    assert result["vocabulary_review_projection"] == {
        "state": "unavailable", "error": "VOCABULARY_PROJECTION_UNAVAILABLE"
    }
    assert "private-path-and-database-details" not in json.dumps(result)
