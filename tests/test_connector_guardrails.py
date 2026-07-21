from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_SURFACE_CONTRACT = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
CHATGPT_PLUGIN_CONTRACT = (
    REPO_ROOT / "deploy" / "chatgpt" / "personal-plugin-contract.json"
)


def test_chatgpt_personal_plugin_tracks_current_tool_surface_rollout() -> None:
    assert TOOL_SURFACE_CONTRACT.is_file(), "missing packaged MCP tool-surface contract"
    assert CHATGPT_PLUGIN_CONTRACT.is_file(), (
        "missing ChatGPT Personal Plugin attestation; refresh/recreate the plugin "
        "before shipping a changed MCP surface"
    )

    surface = json.loads(TOOL_SURFACE_CONTRACT.read_text(encoding="utf-8"))
    plugin = json.loads(CHATGPT_PLUGIN_CONTRACT.read_text(encoding="utf-8"))

    assert plugin["mcp_url"] == "https://exomem.substratesystems.io/mcp"
    assert plugin["authentication"] == "oauth"
    assert plugin["client_registration"] == "cimd"
    assert plugin["oidc_enabled"] is False
    assert plugin["default_scopes"] == []
    assert plugin["base_scopes"] == ["offline_access"]
    registered = plugin["registered_tool_surface_sha256"]
    pending = plugin["pending_tool_surface_sha256"]
    if registered == surface["sha256"]:
        assert plugin["refresh_required"] is False
        assert pending is None
        assert plugin["rollout_state"] == "registered"
        assert plugin["last_verified_tool_surface_sha256"] == registered
    else:
        assert plugin["refresh_required"] is True, (
            "MCP tool surface changed without a rollout acknowledgement. Record the "
            "new digest as pending before release; do not claim it is registered yet."
        )
        assert pending == surface["sha256"]
        assert plugin["rollout_state"] == "awaiting-post-deploy-refresh"
    if registered is not None:
        assert re.fullmatch(r"[0-9a-f]{64}", registered)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", plugin["last_verified_at"])
    assert "bootstrap" in plugin["verification"]
    assert "ask_memory" in plugin["verification"]


def test_operator_connector_host_is_canonical() -> None:
    instructions = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    connector_section = instructions.split('## Connector triage', 1)[1].split('\n## ', 1)[0]

    assert "exomem.substratesystems.io" in connector_section
    assert "kb.substratesystems.io" not in connector_section


def test_remote_quickstart_documents_chatgpt_oauth_only_setup() -> None:
    guide = (REPO_ROOT / "docs" / "remote-quickstart.md").read_text(
        encoding="utf-8"
    ).lower()

    assert "chatgpt personal plugin" in guide
    assert "https://<host>/mcp" in guide
    assert "oidc" in guide and "off" in guide
    assert "default scopes" in guide and "blank" in guide
    assert "base scopes" in guide and "offline_access" in guide
    assert "fresh conversation" in guide
