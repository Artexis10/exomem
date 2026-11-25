"""The one-upload Claude plugin that a self-hosted server serves for its own address."""

from __future__ import annotations

import io
import json
import zipfile

from fastmcp import FastMCP
from starlette.testclient import TestClient

from exomem import self_hosted_plugin
from exomem.cloud_plugins import _validate_claude_manifest
from exomem.server_assets import register_claude_plugin_route


def test_plugin_connects_to_the_server_and_keeps_the_edition_skills_and_hooks() -> None:
    # A wrong URL, a dropped hook or skill, or a manifest claude.ai rejects all
    # leave the user with an upload that does not work.
    built = zipfile.ZipFile(io.BytesIO(self_hosted_plugin.build("https://kb.example.com/")))
    edition = zipfile.ZipFile(self_hosted_plugin.edition_archive())

    assert json.loads(built.read(".mcp.json")) == {
        "mcpServers": {"evermatter": {"type": "http", "url": "https://kb.example.com/mcp"}}
    }
    manifest = json.loads(built.read(".claude-plugin/plugin.json"))
    _validate_claude_manifest(manifest)
    assert manifest["name"] == "evermatter-kb-example-com"
    assert manifest["displayName"] == "Evermatter"
    assert manifest["description"].startswith("For your own Evermatter server at kb.example.com:")
    readme = built.read("README.md").decode("utf-8")
    assert "Connectors tab" in readme and "## Data handling" in readme
    carried = [
        name
        for name in edition.namelist()
        if name.startswith(("hooks/", "skills/", "assets/")) or name == "LICENSE"
    ]
    assert any(name.startswith("hooks/") for name in carried)
    assert any(name.startswith("skills/") for name in carried)
    for name in carried:
        assert built.read(name) == edition.read(name), name


def test_plugin_name_stays_valid_for_a_port_and_a_long_host() -> None:
    assert self_hosted_plugin.plugin_name("http://127.0.0.1:8765") == "evermatter-127-0-0-1-8765"
    long_host = "https://" + "memory-" * 12 + "example.com"
    name = self_hosted_plugin.plugin_name(long_host)
    _validate_claude_manifest({"name": name, "description": ""})
    assert len(name) <= 64


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
