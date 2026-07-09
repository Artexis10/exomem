from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, server
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
    assert "compute_policy" in out["server"]
    assert {
        "workflow",
        "workflow_skills",
        "tool_defaults",
        "performance_profiles",
        "memory_model",
        "knowledge_packs",
        "authoring_contract",
    } <= set(out)
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
    assert out["front_door_actions"]["save"]["selected_pack_guidance"][0]["pack_id"] == "personal-records"
    assert out["tool_defaults"]["adopt_existing_vault"]["tool"] == "adopt"
    authoring = out["authoring_contract"]
    assert "suggest_links" in " ".join(authoring["canonical_loop"])
    assert authoring["route_by_intent"]["new_durable_conclusion"] == "note"
    assert authoring["route_by_intent"]["small_correction"] == "edit"
    assert authoring["route_by_intent"]["substantial_rewrite"] == "replace"
    assert "near_duplicate_warnings" in authoring["preflight"]
    assert "write_feedback" in authoring["post_write"]
    assert "insight" in authoring["note_type_recipes"]
    assert any("write_feedback" in step for step in out["workflow"]["loop"])
    assert "adopt" in out["common_tools"]
    assert "search" in out["common_tools"]
    assert "fetch" in out["common_tools"]
    assert "find" in out["common_tools"]
    assert "get" in out["common_tools"]
    assert out["tool_defaults"]["normal_lookup"] == {"tool": "search", "args": {}}
    assert out["tool_defaults"]["read_bounded_page"]["tool"] == "fetch"
    serialized = json.dumps(out)
    assert str(vault) not in serialized
    assert "Progressive disclosure" not in serialized


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
    assert "adopt" in catalog["primary"]
    assert "search" in catalog["primary"]
    assert "fetch" in catalog["primary"]
    assert "find" in catalog["primary"]
    assert "get" in catalog["primary"]
    assert "preserve" in front_door["prove"]["primary_tools"]
    assert "audit" in front_door["review"]["primary_tools"]
    assert "create_file" in catalog["advanced"]
    assert "list_directory" in catalog["advanced"]
    assert "scan-only" in front_door["adopt"]["contract"]
    assert "proof" in front_door["prove"]["contract"]

    selected = {
        "packs": [
            {
                "id": "technical",
                "name": "Technical",
                "actions": ["save", "ask"],
                "agent_instructions": "Route technical work through governed notes.",
                "suggested_workflows": [{"title": "Save", "intent": "x", "route": "note", "example": "x"}],
            }
        ]
    }
    guided = commands.product_front_door_catalog(selected)
    assert guided["save"]["selected_pack_guidance"][0]["pack_id"] == "technical"
    assert "selected_pack_guidance" not in guided["prove"]

    actions = set(front_door)
    for command in commands.COMMANDS:
        assert command.product_surface in {"primary", "advanced"}
        assert set(command.product_actions) <= actions


def test_bootstrap_is_registry_generated_on_public_surfaces(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cmd = next(c for c in commands.COMMANDS if c.name == "bootstrap")
    assert cmd.read_only is True
    assert {"mcp", "rest", "cli"} <= set(cmd.surfaces)
    assert "bootstrap" not in commands.HAND_REGISTERED_EXCEPTIONS

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    mcp = server.build_server(require_auth=False)
    names = _tool_names(mcp)
    assert "bootstrap" in names
    assert "adopt" in names

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
