from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_hosted_definition_is_a_fixed_production_release() -> None:
    definition = hosted_plugins.load_definition(REPO_ROOT)

    assert definition.plugin_id == "exomem-hosted"
    assert definition.version == "0.1.0"
    assert definition.endpoint == "https://substratesystems.io/api/exomem/mcp/v1"
    assert definition.profile == "hosted-alpha-agent-v1"
    assert definition.channel == "production"
    assert definition.support_url.startswith("https://")
    assert definition.privacy_url.startswith("https://")
    assert definition.terms_url.startswith("https://")


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"endpoint": "http://localhost:8765/mcp"}, "HTTPS"),
        ({"endpoint": "https://staging.example/mcp/v1", "channel": "production"}, "production"),
        ({"version": "0.1"}, "semantic version"),
        ({"tenant": "tenant-123"}, "unsupported"),
        ({"token": "secret"}, "unsupported"),
        ({"command": "uvx"}, "unsupported"),
    ],
)
def test_definition_rejects_nonportable_or_invalid_fields(
    tmp_path: Path, patch: dict[str, str], message: str
) -> None:
    raw = json.loads((REPO_ROOT / "plugins/hosted/definition.json").read_text(encoding="utf-8"))
    raw.update(patch)
    candidate = tmp_path / "definition.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        hosted_plugins.load_definition_file(candidate)


def test_compatibility_manifest_uses_the_exact_ordered_alpha_contract() -> None:
    manifest = hosted_plugins.compatibility_manifest(REPO_ROOT)

    assert manifest["profile"] == "hosted-alpha-agent-v1"
    # The descriptor identifies the contract surface, not the build: it carries
    # no Exomem release, so a version bump leaves it untouched. Covered in
    # tests/test_hosted_plugin_release_identity.py.
    assert "source_release" not in manifest
    assert "exomem_release" not in manifest["agent_contract"]
    assert manifest["endpoint"] == "https://substratesystems.io/api/exomem/mcp/v1"
    assert tuple(manifest["commands"]) == (
        "bootstrap",
        "ask_memory",
        "read_memory",
        "browse_memory",
        "remember",
        "observe_memory",
        "capture_source",
        "compile_source",
        "preserve_evidence",
        "review_memory",
        "review_item_context",
        "triage_memory",
        "connect_memory",
    )
    assert manifest["schema_contract_sha256"]
    assert manifest["command_surface_sha256"]
    assert manifest["definition_sha256"]
    assert manifest["skills_sha256"]
    assert manifest["oauth_discovery_sha256"]
    assert manifest["compatibility_sha256"]


def test_compatibility_manifest_exports_exact_mcp_contract_and_standard_oauth_overlay() -> None:
    manifest = hosted_plugins.compatibility_manifest(REPO_ROOT)

    assert tuple(entry["name"] for entry in manifest["agent_contract"]["commands"]) == tuple(
        manifest["commands"]
    )
    assert all({"name", "description", "inputSchema", "annotations"} <= set(entry["mcp_tool"])
               for entry in manifest["agent_contract"]["commands"])
    assert manifest["oauth_discovery"]["resource"] == manifest["endpoint"]
    assert all(
        overlay["securitySchemes"][0]["type"] == "oauth2"
        for overlay in manifest["oauth_discovery"]["tools"].values()
    )
    oauth = manifest["oauth_discovery"]
    assert oauth["issuer"] == "https://substratesystems.io/api/exomem/oauth"
    assert oauth["authorization_server_metadata"] == (
        "https://substratesystems.io/.well-known/oauth-authorization-server/api/exomem/oauth"
    )
    assert oauth["authorize_url"] == "https://substratesystems.io/api/exomem/oauth/authorize"
    assert oauth["token_url"] == "https://substratesystems.io/api/exomem/oauth/token"
    assert oauth["revoke_url"] == "https://substratesystems.io/api/exomem/oauth/revoke"
