from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_SURFACE_CONTRACT = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
CHATGPT_PLUGIN_CONTRACT = (
    REPO_ROOT / "deploy" / "chatgpt" / "personal-plugin-contract.json"
)
OPENAI_PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "hosted" / "generated" / "openai" / ".app.json"

_REGISTERED_V1_ATTESTATION = {
    "registered_tool_surface_sha256": "3fb189ee7d9e48183c404d5b5d36df3c36d5e8df995b7e4cd78ad8c763672ae6",
    "last_verified_release": "0.25.5",
    "last_verified_at": "2026-07-20",
    "last_verified_tool_surface_sha256": "3fb189ee7d9e48183c404d5b5d36df3c36d5e8df995b7e4cd78ad8c763672ae6",
    "verification": (
        "Connector recreated against 0.25.5 and confirmed returning note content in a fresh "
        "ChatGPT conversation, which is the check that matters: the active failure note "
        "chatgpt-app-blocks-exomem-content-returning-mcp-reads-despite-safe-annotations "
        "records that bootstrap and frontmatter-only reads can succeed while content-bearing "
        "reads stay blocked, so connectivity alone is not evidence. bootstrap and ask_memory "
        "were additionally verified over the live connector, with bootstrap reporting "
        "published_mcp_tool_surface_sha256 equal to the promoted digest. Re-verify content "
        "reads, not just connection, after any tool-surface change."
    ),
}
_REGISTERED_OPENAI_APP_ID = "plugin_asdk_app_6a5e3d26f2b08191a04424d1c1b33fc0"


def _assert_records_pending_acceptance(plugin: dict[str, object], surface: dict[str, object]) -> None:
    pending = plugin.get("records_pending_acceptance")
    assert isinstance(pending, dict)
    assert set(pending) == {
        "profile",
        "minimum_records_reader_version",
        "pending_tool_surface_sha256",
        "state",
        "evidence",
    }
    assert pending == {
        "profile": "hosted-alpha-agent-v2",
        "minimum_records_reader_version": 2,
        "pending_tool_surface_sha256": surface["sha256"],
        "state": "awaiting-post-deploy-v2-acceptance",
        "evidence": None,
    }


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


def test_chatgpt_personal_plugin_keeps_v2_records_acceptance_pending() -> None:
    surface = json.loads(TOOL_SURFACE_CONTRACT.read_text(encoding="utf-8"))
    plugin = json.loads(CHATGPT_PLUGIN_CONTRACT.read_text(encoding="utf-8"))
    app = json.loads(OPENAI_PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert {key: plugin[key] for key in _REGISTERED_V1_ATTESTATION} == _REGISTERED_V1_ATTESTATION
    assert app["apps"]["exomem"]["id"] == _REGISTERED_OPENAI_APP_ID
    assert plugin["rollout_state"] == "awaiting-post-deploy-refresh"
    _assert_records_pending_acceptance(plugin, surface)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda pending: pending.pop("evidence"),
        lambda pending: pending.__setitem__("unexpected", True),
        lambda pending: pending.__setitem__("profile", "hosted-alpha-agent-v1"),
        lambda pending: pending.__setitem__("minimum_records_reader_version", 1),
        lambda pending: pending.__setitem__("pending_tool_surface_sha256", "0" * 64),
        lambda pending: pending.__setitem__("state", "registered"),
        lambda pending: pending.__setitem__("evidence", {"fabricated": True}),
    ),
)
def test_chatgpt_records_pending_guardrail_rejects_invalid_pending_blocks(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    surface = json.loads(TOOL_SURFACE_CONTRACT.read_text(encoding="utf-8"))
    plugin = json.loads(CHATGPT_PLUGIN_CONTRACT.read_text(encoding="utf-8"))
    pending = plugin["records_pending_acceptance"]
    assert isinstance(pending, dict)
    mutate(pending)

    with pytest.raises(AssertionError):
        _assert_records_pending_acceptance(plugin, surface)


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
