"""Protect the experiment from channel leaks and accidental source-field ablations."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from import_guidance.review_dimensions import ReviewDimensionProjection, project_listing
from mcp.types import TextContent


@pytest.mark.reads_repository_docs
def test_actor_stages_both_contexts_with_server_configuration_outside_its_working_directory(tmp_path):
    """Dropping one configured context silently turns a composite task into an easier single-context task."""
    from import_guidance.review_dimensions import stage_actor

    servers = {
        "primary": {"command": sys.executable, "args": ["-m", "fixture_server"]},
        "bounded": {"type": "http", "url": "http://127.0.0.1:8765/mcp",
                    "headers": {"Authorization": "Bearer synthetic-fixture-token"}},
    }
    staged = stage_actor(
        Path(__file__).resolve().parents[1], tmp_path, {}, arm="candidate",
        executable=Path(sys.executable), interpreter=Path(sys.executable), model="fixture-model",
        effort="high", budget_usd=0.2, mcp_servers=servers,
    )

    assert json.loads(staged["mcp"].read_text())["mcpServers"] == servers
    assert not staged["mcp"].is_relative_to(staged["cwd"])
    if os.name == "posix":
        assert stat.S_IMODE(staged["mcp"].stat().st_mode) == 0o600
    assert json.loads(staged["settings"].read_text())["permissions"]["blockReadsOutsideWorkingDirectories"]
    allowed = staged["argv"][staged["argv"].index("--allowedTools") + 1].split(",")
    assert {"mcp__primary__*", "mcp__bounded__*"} <= set(allowed)


def test_projection_preserves_nested_source_dimensions_coverage_and_every_item_mirror():
    item = {
        "ref": "exomem://review/example", "fingerprint": "source-hash", "reason": "Read the source.",
        "dimensions": ["uncertainty"], "dimension_status": "known",
        "dimension_diagnostics": {"dependency": "selected_structure"},
        "source": {"dimensions": ["attribution"], "dimension_status": "known"},
    }
    coverage = {"annotation_unknown": 1, "complete": False, "unsearched": None}
    raw = {"items": [item], "groups": [{"items": [copy.deepcopy(item)]}],
           "runs": [{"items": [copy.deepcopy(item)]}], "approvals": [copy.deepcopy(item)],
           "dimension_coverage": coverage, "diagnostics": {"dimensions": ["kind"]}}
    original = copy.deepcopy(raw)
    projected = project_listing(raw)
    expected = {"ref": item["ref"], "fingerprint": "source-hash", "reason": item["reason"],
                "source": item["source"]}
    assert projected["items"] == projected["groups"][0]["items"] == [expected]
    assert projected["runs"][0]["items"] == projected["approvals"] == [expected]
    assert projected["dimension_coverage"] == coverage
    assert projected["diagnostics"] == raw["diagnostics"]
    assert raw == original


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
def test_real_mcp_dispatch_binds_arguments_and_projects_both_channels_only_on_listing(tmp_path, arm):
    mcp = FastMCP("Annotation boundary")
    log = tmp_path / "calls.jsonl"
    mcp.middleware.insert(0, ReviewDimensionProjection(arm, log))
    raw = {"items": [{"ref": "review-one", "dimensions": [], "dimension_status": "known"}],
           "dimension_coverage": {"annotation_known": 1, "filter_complete": False}}

    @mcp.tool
    def list_review_items(filters: dict) -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text=json.dumps(raw))],
                          structured_content=raw, meta={"boundary": "retained"})

    @mcp.tool
    def read_page(page: str) -> dict:
        return {"page": page, "dimensions": ["uncertainty"], "dimension_status": "known"}

    filters = {"$not": {"unit.dimension": {"$eq": "uncertainty"}}}
    listing = asyncio.run(mcp.call_tool("list_review_items", {"filters": filters}))
    expected = project_listing(raw) if arm == "baseline" else raw
    assert listing.structured_content == json.loads(listing.content[0].text) == expected
    wire = listing.to_mcp_result()
    assert wire.structured_content == json.loads(wire.content[0].text) == expected
    assert wire.meta == {"boundary": "retained"}
    source = asyncio.run(mcp.call_tool("read_page", {"page": "subject"}))
    assert source.structured_content["dimensions"] == ["uncertainty"]
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["request"]["arguments"] == {"filters": filters}
    assert rows[0]["raw"]["structured_content"] == raw
    assert rows[0]["served"]["structured_content"] == expected
    assert rows[1]["raw"] == rows[1]["served"]


def test_disagreeing_channels_stop_the_experiment_and_keep_the_raw_failure(tmp_path):
    mcp = FastMCP("Channel mismatch")
    log = tmp_path / "calls.jsonl"
    mcp.middleware.insert(0, ReviewDimensionProjection("baseline", log))

    @mcp.tool
    def list_review_items() -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text='{"items": []}')],
                          structured_content={"items": [{"dimension_status": "unknown"}]})

    with pytest.raises(ValueError, match="channels disagree"):
        asyncio.run(mcp.call_tool("list_review_items", {}))
    [row] = [json.loads(line) for line in log.read_text().splitlines()]
    assert row["raw"]["structured_content"]["items"][0]["dimension_status"] == "unknown"
    assert "served" not in row and row["error"]["type"] == "ValueError"


def test_graph_preparation_exit_records_failed_readiness_instead_of_silent_timeout(tmp_path, monkeypatch):
    from import_guidance import session_seed
    from import_guidance.review_dimensions import observe_readiness

    from exomem import readiness

    def unavailable_graph(_root):
        raise SystemExit("graph snapshot unavailable")

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(readiness, "wait", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(session_seed, "prepare_recall", lambda _root: None)
    monkeypatch.setattr(session_seed, "wait_for_graph", unavailable_graph)
    receipt = tmp_path / "readiness.json"
    observe_readiness(receipt)

    result = json.loads(receipt.read_text())
    assert result["status"] == "failed"
    assert result["error"] == {"type": "SystemExit", "message": "graph snapshot unavailable"}
