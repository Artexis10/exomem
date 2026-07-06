from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands
from exomem import server
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
    assert {"workflow", "tool_defaults", "performance_profiles"} <= set(out)
    assert out["tool_defaults"]["normal_lookup"]["args"] == {
        "detail": "compact",
        "rerank": False,
    }
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
    assert "bootstrap" in _tool_names(mcp)

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
