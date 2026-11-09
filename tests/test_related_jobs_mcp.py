"""A door operation reaches the grouped command's own execution boundary: credential rule, lease, egress."""

import dataclasses

import pytest
import test_single_purpose_tools_mcp as pilot_mcp
import yaml
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
        return await _call(client, "inspect_memory", {"operation": "writer.status"})

    mcp = build()
    assert _session(mcp, scenario) == status
    assert seams.rules[-1][0] == "coordination_status"


def test_dataset_filters_keep_the_omitted_operator_and_do_not_contact_a_writer(vault, build, seams, monkeypatch):
    (vault / "metrics.csv").write_text("value,label\n1,first\n2,second\n")
    arguments = {"path": "metrics.csv", "filters": [{"column": "value", "value": "2"}]}

    async def scenario(client):
        return await _call(client, "inspect_memory", {"operation": "dataset.query", "arguments": arguments})

    mcp = build()

    def ensure_writer(*args, **kwargs):
        raise AssertionError("a dataset read must not acquire writer authority")

    monkeypatch.setattr(writer_lease.LeaseManager, "ensure_writer", ensure_writer)
    result = _session(mcp, scenario)
    assert result["rows"] == [{"value": "2", "label": "second"}]
    assert all(name == "query_dataset" for name, *_ in seams.rules)


@pytest.mark.parametrize("condition", ["room", "collision", "cap"])
def test_registry_preview_checks_the_live_overlay_without_replacing_it(vault, build, seams, monkeypatch, condition):
    """A singleton document can validate while its delta fails against existing custom categories."""
    from exomem import semantic_language_registry

    count = semantic_language_registry.CATEGORY_SPEC.cap if condition == "cap" else 2
    categories = {
        f"existing_category_{index}": {"description": f"Retained category number {index}."}
        for index in range(count)
    }
    categories["existing_category_0"]["aliases"] = ["shared_category_alias"]
    overlay = semantic_language_registry.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(yaml.safe_dump({"schema_version": 1, "categories": categories, "kinds": {}}))
    original = overlay.read_bytes()
    entry = {"description": "A distinct proposed category."}
    if condition == "collision":
        entry["aliases"] = ["shared_category_alias"]
    delta = {"upsert": {"proposed_category": entry}}

    def ensure_writer(*args, **kwargs):
        raise AssertionError("a registry preview must not acquire writer authority")

    monkeypatch.setattr(writer_lease.LeaseManager, "ensure_writer", ensure_writer)

    async def scenario(client):
        document = await _call(client, "inspect_memory", {"operation": "schema.validate_document", "arguments": {
            "subject": "categories",
            "proposal": {"schema_version": 1, "categories": {"proposed_category": entry}},
        }})
        malformed = await _call(client, "inspect_memory", {"operation": "schema.preview", "arguments": {
            "subject": "categories", "proposal": {"upsert": [{"key": "proposed_category", **entry}]},
        }})
        preview = await _call(client, "inspect_memory", {"operation": "schema.preview", "arguments": {
            "subject": "categories", "proposal": delta,
        }})
        return document, malformed, preview

    document, malformed, compact = _session(build(), scenario)
    assert document["valid"] is True
    # The door's arguments are an open object, so the leaf rejects the malformed delta shape.
    assert malformed["valid"] is False
    assert "INVALID_REGISTRY_DELTA" in {finding["code"] for finding in malformed["findings"]}
    assert compact["expected_hash"] != "none"
    if condition == "cap":
        assert compact["valid"] is False
        assert "registry_cap" in {finding["code"] for finding in compact["findings"]}
    elif condition == "collision":
        assert "existing_category_0" in {collision["key"] for collision in compact["collisions"]}
    else:
        assert compact["valid"] is True
        assert [item["key"] for item in compact["entries"]] == ["proposed_category"]
    assert overlay.read_bytes() == original
    assert {call["command"] for call in seams.dispatched} == {"schema_memory"}


def test_an_import_run_started_by_one_door_is_readable_through_the_other(build, seams):
    async def scenario(client):
        run = await _call(client, "manage_memory", {"operation": "imports.start", "arguments": {"path": "."}})
        run_id = run["run_id"]
        status = await _call(client, "inspect_memory", {"operation": "imports.status", "arguments": {"run_id": run_id}})
        return run_id, status

    run_id, status = _session(build(), scenario)
    assert status["run_id"] == run_id
    assert {call["command"] for call in seams.dispatched} == {"adoption_studio"}


def test_video_variant_retains_native_image_content(build, monkeypatch):
    native = ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")

    def video(vault_root, path: str | None = None, media: str = "video", paths: list[str] | None = None,
              max_frames: int = 8, start_sec: float | None = None, end_sec: float | None = None,
              max_side: int | None = None) -> ToolResult:
        return ToolResult(content=[native])

    monkeypatch.setattr(commands, "PRODUCT_COMMANDS", tuple(
        dataclasses.replace(command, leaf=video) if command.name == "read_media" else command
        for command in commands.PRODUCT_COMMANDS
    ))

    async def scenario(client):
        return await client.call_tool(
            "inspect_memory", {"operation": "media.video", "arguments": {"path": "clip.mp4"}}
        )

    result = _session(build(), scenario)
    assert result.content == [native]


def test_cloud_discovery_keeps_media_status_and_original_file_metadata(build, monkeypatch):
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "test-cell")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "x" * 40)

    async def scenario(client):
        return {tool.name: tool for tool in await client.list_tools()}

    tools = _session(build(), scenario)
    inspect_operations = set(tools["inspect_memory"].input_schema["properties"]["operation"]["enum"])
    assert {"media.status"} <= inspect_operations
    assert not {"media.video", "media.image"} & inspect_operations
    assert not {"upload_files", "download_files"} & tools.keys()
    assert {"observe_transfer", "transfer_memory"} <= tools.keys()
    evidence = tools["save_evidence"]
    assert "files" in evidence.input_schema["properties"]
    assert evidence.meta["openai/fileParams"] == ["files"]


def test_selected_session_refusal_reaches_the_original_credential_rule(build, seams):
    async def scenario(client):
        return await _call(client, "manage_memory", {"operation": "session.status"})

    result = _session(build(), scenario)
    assert "authorization session is unavailable" in result["text"]
    name, arguments, rule = seams.rules[-1]
    assert name == "govern_memory"
    assert arguments == {"operation": "session", "session_action": "status"}
    assert rule.value == "required"
    assert seams.dispatched == []


@pytest.mark.parametrize("door", ["inspect_memory", "manage_memory"])
def test_unknown_operation_is_a_public_refusal_without_an_authentication_target(build, seams, door):
    async def scenario(client):
        return await _call(client, door, {"operation": "media.unknown"})

    result = _session(build(), scenario)
    assert result["error"]["code"] == "INVALID_OPERATION"
    assert seams.rules == []
    assert seams.dispatched == []
