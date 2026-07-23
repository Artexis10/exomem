from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, entity_types, semantic_authoring, server
from exomem.__main__ import main


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN"):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_bootstrap_compact_contract_is_public_safe(vault: Path) -> None:
    out = commands.op_bootstrap(vault)

    assert out["contract_version"]
    assert out["profile"] == "compact"
    assert out["server"]["name"] == "exomem"
    assert out["server"]["content_included"] is False
    assert out["server"]["pure_substrate"] is True
    assert re.fullmatch(
        r"[0-9a-f]{64}", out["server"]["published_mcp_tool_surface_sha256"]
    )
    assert "compute_policy" in out["server"]
    assert {
        "workflow",
        "workflow_skills",
        "tool_defaults",
        "product_commands",
        "performance_profiles",
        "memory_model",
        "knowledge_packs",
        "entity_registry",
        "authoring_contract",
    } <= set(out)
    assert set(out["common_actions"]) == set(commands.simple_action_names())
    assert out["simple_actions"]["ask"]["route"]["tool"] == "ask_memory"
    assert out["simple_actions"]["remember"]["route"]["tool"] == "remember"
    assert out["simple_actions"]["capture"]["evidence_route"]["tool"] == "preserve_evidence"
    assert "durable governed knowledge" in out["memory_model"]["exomem"]
    assert [s["name"] for s in out["workflow_skills"]] == [
        "exomem-continue",
        "exomem-capture",
        "exomem-ingest",
        "exomem-research",
        "exomem-reflect",
        "exomem-curate",
        "exomem-defrag",
        "exomem-review",
        "exomem-media",
    ]
    assert out["workflow_skills"][0]["path"].startswith("Knowledge Base/_Schema/")
    assert out["knowledge_packs"]["selected"]["selected_pack_ids"] == ["personal-records"]
    assert out["knowledge_packs"]["available"][0]["beginner_description"]
    assert [item["id"] for item in out["entity_registry"]["types"]] == list(
        entity_types.ENTITY_TYPE_IDS
    )
    assert out["entity_registry"]["types"][0]["aliases"] == list(
        entity_types.ENTITY_TYPE_REGISTRY[0].aliases
    )
    assert out["entity_registry"]["candidate_route"] == (
        "connect_memory(operation='resolve-entity')"
    )
    organization = next(
        item
        for item in out["entity_registry"]["types"]
        if item["id"] == "organization"
    )
    assert "company" in organization["aliases"]
    assert (
        out["authoring_contract"]["route_by_intent"]["stable_named_entity"]
        == "connect_memory(operation='create-entity')"
    )
    assert "operation='entity'" not in repr(out)
    assert out["front_door_actions"]["save"]["selected_pack_guidance"][0]["pack_id"] == "personal-records"
    assert out["tool_defaults"]["adopt_existing_vault"]["tool"] == "adopt_vault"
    authoring = out["authoring_contract"]
    assert "connect_memory" in " ".join(authoring["canonical_loop"])
    assert authoring["route_by_intent"]["new_durable_conclusion"] == "remember"
    assert authoring["route_by_intent"]["small_correction"] == "edit_memory"
    assert authoring["route_by_intent"]["semantic_unit_mutation"] == "observe_memory"
    assert authoring["route_by_intent"]["substantial_rewrite"] == "replace_memory"
    assert "near_duplicate_warnings" in authoring["preflight"]
    assert "write_feedback" in authoring["post_write"]
    assert "insight" in authoring["note_type_recipes"]
    assert any("write_feedback" in step for step in out["workflow"]["loop"])
    assert "adopt_vault" in out["common_tools"]
    assert "ask_memory" in out["common_tools"]
    assert "read_memory" in out["common_tools"]
    assert "remember" in out["common_tools"]
    assert "observe_memory" in out["common_tools"]
    assert out["tool_defaults"]["normal_lookup"]["tool"] == "ask_memory"
    assert out["tool_defaults"]["normal_lookup"]["args"] == {
        "detail": "compact",
        "rerank": False,
    }
    assert out["tool_defaults"]["read_full_page"]["tool"] == "read_memory"
    assert out["tool_defaults"]["mutate_semantic_unit"]["tool"] == "observe_memory"
    unit_contract = authoring["semantic_units"]
    assert unit_contract["compact_syntax"].startswith("- [category]")
    assert unit_contract["compact_kind"] == "observation"
    assert unit_contract["rich_relation_rule"]
    reviewed_creation = authoring["reviewed_creation"]
    assert {"validate_only", "commit", "reviewed_none", "adoption_handoff"} <= set(
        reviewed_creation
    )
    assert "draft_id" in reviewed_creation["validate_only"]
    assert "draft_hash" in reviewed_creation["commit"]
    assert "never fabricate" in reviewed_creation["reviewed_none"]
    assert "remember()" in reviewed_creation["adoption_handoff"]
    semantic_recall = out["search_guidance"]["semantic_recall"]
    assert semantic_recall["result_levels"] == ["page", "unit", "mixed"]
    assert "empty query" in semantic_recall["filter_only"]
    assert "filters" in semantic_recall["structured_filters"]
    assert "explain=true" in semantic_recall["explanation"]
    score_guidance = semantic_recall["score_interpretation"]
    assert all(
        metric in score_guidance
        for metric in ("bm25", "cosine", "rrf", "reranker", "final_rank")
    )
    assert "confidence" in score_guidance["rule"]
    serialized = json.dumps(out)
    assert str(vault) not in serialized
    assert "Progressive disclosure" not in serialized


def test_bootstrap_profiles_project_profile_aware_semantic_authoring_contract(
    vault: Path,
) -> None:
    full = commands.op_bootstrap(vault, profile="full")["semantic_authoring"]
    compact = commands.op_bootstrap(vault, profile="compact")["semantic_authoring"]
    diagnostics = commands.op_bootstrap(vault, profile="diagnostics")[
        "semantic_authoring"
    ]

    # Each surface projects exactly the deterministic profile projection. Profile
    # is keyword-only so it can never be mistaken for a positionally-passed
    # contract (the existing positional-contract API is preserved).
    assert full == semantic_authoring.bootstrap_projection(profile="full")
    assert compact == semantic_authoring.bootstrap_projection(profile="compact")
    assert diagnostics == semantic_authoring.bootstrap_projection(profile="diagnostics")

    # Full carries every example; compact/diagnostics intentionally omit ONLY the
    # rich example while keeping the complete core keys, aliases, open rule,
    # selection guidance, and both compact (role and domain) examples.
    full_examples = full["portable_categories"]["examples"]
    compact_examples = compact["portable_categories"]["examples"]
    assert set(full_examples) == {"role", "domain", "rich"}
    assert set(compact_examples) == {"role", "domain"}
    assert compact == diagnostics
    assert compact["portable_categories"]["core_keys"] == (
        full["portable_categories"]["core_keys"]
    )
    assert compact["portable_categories"]["aliases"] == (
        full["portable_categories"]["aliases"]
    )
    assert len(full["portable_categories"]["core_keys"]) == 16

    # The compact projection differs from full only by the dropped rich example.
    reconstructed = json.loads(json.dumps(compact))
    reconstructed["portable_categories"]["examples"]["rich"] = full_examples["rich"]
    assert reconstructed == full

    for projection in (full, compact, diagnostics):
        serialized = json.dumps(projection, ensure_ascii=False)
        for required in (
            "## Observations",
            "- [category] content #tags (context) ^anchor",
            "open",
            "missing_semantic_unit",
            "empty_rich_unit",
            "remember",
            "replace_memory",
            "manage_memory_file create, overwrite, and append",
            "- [constraint] Keep retry windows bounded #code ^retry-windows",
            "- [design] Keep the public adapter stateless #api ^public-adapter",
        ):
            assert required in serialized


def test_bootstrap_compact_is_compact_through_the_entire_payload(vault: Path) -> None:
    rich_example = semantic_authoring.get_semantic_authoring_contract().portable_categories[
        "examples"
    ]["rich"]

    def contains_exact(value: object, needle: str) -> bool:
        if isinstance(value, str):
            return value == needle
        if isinstance(value, dict):
            return any(contains_exact(item, needle) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_exact(item, needle) for item in value)
        return False

    # A compact (or diagnostics) bootstrap must stay compact through the WHOLE
    # payload: the rich example may not appear anywhere, including the nested
    # authoring_contract.semantic_units.contract projection.
    for profile in ("compact", "diagnostics"):
        payload = commands.op_bootstrap(vault, profile=profile)
        assert not contains_exact(payload, rich_example)
        nested = payload["authoring_contract"]["semantic_units"]["contract"]
        assert nested == semantic_authoring.bootstrap_projection(profile=profile)
        assert "rich" not in nested["portable_categories"]["examples"]

    # The full profile carries the rich example in both the top projection and
    # the nested authoring_contract projection.
    full_payload = commands.op_bootstrap(vault, profile="full")
    assert contains_exact(full_payload, rich_example)
    full_nested = full_payload["authoring_contract"]["semantic_units"]["contract"]
    assert full_nested == semantic_authoring.bootstrap_projection(profile="full")
    assert full_nested["portable_categories"]["examples"]["rich"] == rich_example


def test_bootstrap_semantic_authoring_projection_is_vault_blind(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, sentinel in ((first, "Synthetic Alpha"), (second, "Synthetic Beta")):
        note = root / "Knowledge Base" / "Notes" / "Research" / "private-note.md"
        note.parent.mkdir(parents=True)
        note.write_text(f"# {sentinel}\n\nDo not project this body.\n", encoding="utf-8")

    left = commands.op_bootstrap(first)["semantic_authoring"]
    right = commands.op_bootstrap(second)["semantic_authoring"]

    assert left == right
    serialized = json.dumps(left, ensure_ascii=False)
    assert "Synthetic Alpha" not in serialized
    assert "Synthetic Beta" not in serialized
    assert str(first) not in serialized
    assert str(second) not in serialized


def test_bootstrap_teaches_human_readable_memory_citations(vault: Path) -> None:
    out = commands.op_bootstrap(vault)
    guidance = json.dumps(out["workflow"]).lower()

    assert out["contract_version"] == "2026-07-19.1"
    for required in (
        "show the note title by default",
        "normal user-facing prose",
        "do not expose the raw canonical ref by default",
        "current vault-relative path",
        "clarity or disambiguation",
        "path or file name as the visible fallback",
        "tool arguments",
        "durable machine state",
        "machine-readable automation",
        "user explicitly asks",
        "identifier itself is being inspected or debugged",
        "do not embed the canonical ref as a markdown link target",
        "plain title-first citation",
    ):
        assert required in guidance
    assert "exomem://memory/<uuid>" in guidance


def test_bootstrap_profiles_and_validation(vault: Path) -> None:
    full = commands.op_bootstrap(vault, profile="full", workflow="research")
    assert full["workflow"]["requested"] == "research"
    assert "examples" in full

    diagnostics = commands.op_bootstrap(vault, profile="diagnostics")
    assert "diagnostics" in diagnostics
    assert "compute_modes" in diagnostics["diagnostics"]

    with pytest.raises(ValueError, match="compact.*full.*diagnostics"):
        commands.op_bootstrap(vault, profile="verbose")


def test_product_front_door_metadata_is_registry_derived() -> None:
    catalog = commands.product_tool_catalog()
    front_door = commands.product_front_door_catalog()

    assert {"save", "adopt", "ask", "prove", "review", "update", "connect"} <= set(front_door)
    assert "adopt_vault" in catalog["primary"]
    assert "ask_memory" in catalog["primary"]
    assert "preserve_evidence" in front_door["prove"]["primary_tools"]
    assert "review_memory" in front_door["review"]["primary_tools"]
    assert "manage_memory_file" in catalog["advanced"]
    assert "query_dataset" in catalog["advanced"]
    assert "scan-only" in front_door["adopt"]["contract"]
    assert "proof" in front_door["prove"]["contract"]

    selected = {
        "packs": [
            {
                "id": "technical",
                "name": "Technical",
                "actions": ["save", "ask"],
                "agent_instructions": "Route technical work through governed notes.",
                "suggested_workflows": [{"title": "Save", "intent": "x", "route": "remember", "example": "x"}],
            }
        ]
    }
    guided = commands.product_front_door_catalog(selected)
    assert guided["save"]["selected_pack_guidance"][0]["pack_id"] == "technical"
    assert "selected_pack_guidance" not in guided["prove"]

    actions = set(front_door)
    for command in commands.PRODUCT_COMMANDS:
        assert command.product_surface in {"primary", "advanced"}
        assert set(command.product_actions) <= actions


def test_simple_action_catalog_is_registry_routed() -> None:
    catalog = commands.simple_action_catalog()

    assert set(catalog) == {
        "ask",
        "remember",
        "capture",
        "review",
        "connect",
        "adopt",
        "maintain",
    }
    assert catalog["ask"]["route"] == {
        "tool": "ask_memory",
        "args": {"detail": "compact", "rerank": False},
    }
    assert catalog["ask"]["deep_route"]["args"]["deep"] is True
    assert catalog["remember"]["route"]["tool"] == "remember"
    assert catalog["capture"]["route"]["tool"] == "capture_source"
    assert catalog["capture"]["evidence_route"]["tool"] == "preserve_evidence"
    assert catalog["review"]["route"]["tool"] == "review_memory"
    assert catalog["connect"]["relations_route"]["tool"] == "connect_memory"
    assert catalog["adopt"]["route"] == {"tool": "adopt_vault", "args": {"mode": "scan-only"}}
    assert catalog["maintain"]["fix_route"]["tool"] == "maintain_memory"

    known = {command.name for command in commands.PRODUCT_COMMANDS} | {"doctor"}
    for action, item in catalog.items():
        routes = [item["route"]]
        routes.extend(
            value for key, value in item.items()
            if key.endswith("_route") and isinstance(value, dict)
        )
        for route in routes:
            assert route["tool"] in known, (action, route)
        for tool in item["advanced"]:
            assert tool in known, (action, tool)

    selected = {
        "packs": [
            {
                "id": "legal-warranty",
                "name": "Legal and warranty",
                "actions": ["save", "prove", "review"],
                "agent_instructions": "Preserve proof before compiling claims.",
                "suggested_workflows": [],
            }
        ]
    }
    guided = commands.simple_action_catalog(selected)
    assert guided["remember"]["selected_pack_guidance"][0]["pack_id"] == "legal-warranty"
    assert guided["capture"]["selected_pack_guidance"][0]["pack_id"] == "legal-warranty"
    assert guided["review"]["selected_pack_guidance"][0]["pack_id"] == "legal-warranty"
    assert "selected_pack_guidance" not in guided["connect"]


def test_bootstrap_is_registry_generated_on_public_surfaces(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cmd = next(c for c in commands.PRODUCT_COMMANDS if c.name == "bootstrap")
    assert cmd.read_only is True
    assert {"mcp", "rest", "cli"} <= set(cmd.surfaces)
    assert "bootstrap" not in commands.HAND_REGISTERED_EXCEPTIONS

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    mcp = server.build_server(require_auth=False)
    names = _tool_names(mcp)
    assert "bootstrap" in names
    assert "adopt_vault" in names
    assert "adopt" not in names

    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/bootstrap",
        json={"profile": "diagnostics"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["profile"] == "diagnostics"
    openapi = client.get("/api/openapi.json")
    assert "/api/bootstrap" in openapi.json()["paths"]

    code = main(["bootstrap", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["contract_version"]
