"""The one-upload Claude plugin that a self-hosted server serves for its own address."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from exomem import package_skills, self_hosted_plugin
from exomem.cloud_plugins import _validate_claude_manifest, server_plugin_name
from exomem.server_assets import register_claude_plugin_route

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_plugin_connects_to_the_server_and_carries_the_hooked_edition() -> None:
    # A wrong URL, a leftover placeholder, a dropped hook or skill, or a manifest
    # claude.ai rejects all leave the user with an upload that does not work.
    built = zipfile.ZipFile(io.BytesIO(self_hosted_plugin.build("https://kb.example.com/")))
    hooked = zipfile.ZipFile(REPO_ROOT / "plugins/cloud/generated/claude-hooks.zip")

    assert json.loads(built.read(".mcp.json")) == {
        "mcpServers": {"evermatter": {"type": "http", "url": "https://kb.example.com/mcp"}}
    }
    manifest = json.loads(built.read(".claude-plugin/plugin.json"))
    _validate_claude_manifest(manifest)
    assert manifest["name"] == "evermatter-kb-example-com"
    assert manifest["displayName"] == "Evermatter"
    assert manifest["description"].startswith("For your own Evermatter server at kb.example.com:")
    readme = built.read("README.md").decode("utf-8")
    assert "kb.example.com" in readme and "Connectors tab" in readme
    assert manifest["version"] == package_skills.release_version()
    for name in built.namelist():
        assert b".invalid" not in built.read(name), name
        assert package_skills.RELEASE_PLACEHOLDER.encode() not in built.read(name), name
    carried = [name for name in hooked.namelist() if name.startswith(("hooks/", "skills/"))]
    assert any(name.startswith("hooks/") for name in carried)
    assert any(name.startswith("skills/") for name in carried)
    for name in carried:
        assert built.read(name) == hooked.read(name), name


def test_plugin_name_stays_valid_and_distinct_for_long_hosts() -> None:
    assert server_plugin_name("http://127.0.0.1:8765") == "evermatter-127-0-0-1-8765"
    first = server_plugin_name("https://" + "memory-" * 12 + "one.example.com")
    second = server_plugin_name("https://" + "memory-" * 12 + "two.example.com")
    for name in (first, second):
        _validate_claude_manifest({"name": name, "description": ""})
    assert first != second


@pytest.mark.parametrize(
    "base_url", ["kb.example.com", "https://kb.example.com/?a=b", "https://u:p@kb.example.com"]
)
def test_plugin_refuses_what_is_not_a_public_origin(base_url: str) -> None:
    with pytest.raises(ValueError):
        self_hosted_plugin.build(base_url)


def test_server_serves_the_plugin_for_its_public_origin_only() -> None:
    served = FastMCP("served")
    register_claude_plugin_route(served, base_url="https://kb.example.com")
    with TestClient(served.http_app()) as client:
        response = client.get("/claude-plugin.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="evermatter-kb-example-com.zip"' in response.headers["content-disposition"]
    assert ".mcp.json" in zipfile.ZipFile(io.BytesIO(response.content)).namelist()

    private = FastMCP("private")
    register_claude_plugin_route(private, base_url="")
    with TestClient(private.http_app()) as client:
        assert client.get("/claude-plugin.zip").status_code == 404

    # An origin too long for claude.ai's description limit says where the cause is.
    oversized = FastMCP("oversized")
    register_claude_plugin_route(oversized, base_url="https://" + "a" * 200 + ".example.com")
    with TestClient(oversized.http_app()) as client:
        response = client.get("/claude-plugin.zip")
    assert response.status_code == 503
    assert "log" in response.json()["error"]
