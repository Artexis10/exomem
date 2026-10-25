"""Shared discovery reaches each original MCP execution boundary."""

import dataclasses

import pytest
import test_single_purpose_tools_mcp as pilot_mcp
from fastmcp.tools import ToolResult
from mcp.types import ImageContent
from test_single_purpose_tools_mcp import _call, _session

from exomem import commands, writer_lease

build = pilot_mcp.build
seams = pilot_mcp.seams


def test_writer_status_does_not_build_bootstrap_during_an_outage(build, monkeypatch, seams):
    status = {"writer": "unavailable", "source": "test-coordinator"}

    def read_status(vault_root):
        return status

    def unavailable_bootstrap(vault_root, profile="compact", workflow=None, skill_contract=None, section=None):
        raise AssertionError("writer status must not build instructions")

    monkeypatch.setattr(commands, "PRODUCT_COMMANDS", tuple(
        dataclasses.replace(command, leaf=read_status) if command.name == "coordination_status"
        else dataclasses.replace(command, leaf=unavailable_bootstrap) if command.name == "bootstrap"
        else command for command in commands.PRODUCT_COMMANDS
    ))

    async def scenario(client):
        return await _call(client, "bootstrap", {"operation": {"kind": "writer-status"}})

    mcp = build()
    assert _session(mcp, scenario) == status
    assert seams.rules[-1][0] == "coordination_status"


def test_dataset_filters_keep_the_omitted_operator_and_do_not_contact_a_writer(vault, build, seams, monkeypatch):
    (vault / "metrics.csv").write_text("value,label\n1,first\n2,second\n")
    arguments = {"path": "metrics.csv", "filters": [{"column": "value", "value": "2"}]}

    async def scenario(client):
        nested = await _call(client, "read_memory", {"operation": {"kind": "dataset", **arguments}})
        legacy = await _call(client, "query_dataset", arguments)
        return nested, legacy

    mcp = build()

    def ensure_writer(*args, **kwargs):
        raise AssertionError("a dataset read must not acquire writer authority")

    monkeypatch.setattr(writer_lease.LeaseManager, "ensure_writer", ensure_writer)
    nested, legacy = _session(mcp, scenario)
    assert nested == legacy
    assert nested["rows"] == [{"value": "2", "label": "second"}]
    assert all(name == "query_dataset" for name, *_ in seams.rules)


def test_video_variant_retains_native_image_content(build, monkeypatch):
    native = ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")

    def video(vault_root, path: str, max_frames: int = 8, start_sec: float | None = None,
              end_sec: float | None = None) -> ToolResult:
        return ToolResult(content=[native])

    monkeypatch.setattr(commands, "PRODUCT_COMMANDS", tuple(
        dataclasses.replace(command, leaf=video) if command.name == "read_media" else command
        for command in commands.PRODUCT_COMMANDS
    ))

    async def scenario(client):
        return await client.call_tool("read_media", {"operation": {"kind": "video", "path": "clip.mp4"}})

    result = _session(build(), scenario)
    assert result.content == [native]


def test_cloud_discovery_keeps_media_status_and_original_file_metadata(build, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "test-cell")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "x" * 40)

    async def scenario(client):
        return {tool.name: tool for tool in await client.list_tools()}

    tools = _session(build(), scenario)
    kinds = {branch["properties"]["kind"]["const"]
             for branch in tools["read_media"].input_schema["properties"]["operation"]["oneOf"]}
    assert kinds == {"status"}
    assert "transfer_artifact" not in tools
    assert {"observe_transfer", "transfer_memory"} <= tools.keys()
    evidence = tools["preserve_artifacts"]
    assert "files" in evidence.input_schema["required"]
    assert evidence.meta["openai/fileParams"] == ["files"]


def test_selected_session_refusal_reaches_the_original_credential_rule(build, seams):
    async def scenario(client):
        return await _call(client, "manage_authorization_session", {"operation": {
            "kind": "status", "documents": {},
        }})

    result = _session(build(), scenario)
    assert "authorization session is unavailable" in result["text"]
    name, arguments, rule = seams.rules[-1]
    assert name == "govern_memory"
    assert arguments == {"operation": "session", "session_action": "status"}
    assert rule.value == "required"
    assert seams.dispatched == []


@pytest.mark.parametrize("tool,code", [("read_media", "INVALID_OPERATION"), ("read_records", "INVALID_RECORD_ARGUMENTS")])
def test_unknown_kind_is_a_public_refusal_without_an_authentication_target(build, seams, tool, code):
    async def scenario(client):
        return await _call(client, tool, {"operation": {"kind": "unknown"}})

    result = _session(build(), scenario)
    assert result["error"]["code"] == code
    assert seams.rules == []
    assert seams.dispatched == []
