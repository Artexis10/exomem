"""The immovable gate: the live product MCP tools must match a committed baseline
byte-for-byte.

This is the safety net for the unified-command-surface migration. Claude reads
every tool's `name`, `description`, and `inputSchema` (the parsed Google-style
docstring Args become per-property descriptions). If the migration to a product registry
+ `bind_vault` generation changes ANY of those, Claude's view of the tools would
silently drift. This test captures the product-command schemas as
`tests/fixtures/mcp_tool_schemas.json` and asserts the live server reproduces
them exactly, so the refactor provably cannot change what Claude sees.

Determinism: the server is built against the repo fixture vault with the same
env the fixture was generated under (embeddings/media/clip off, tier-2 ON,
dotenv neutralized). The fixture vault has no `project-keys.yaml`, so `note`'s
project-key hint resolves to the deterministic built-in fallback.

Any tool that genuinely cannot be registry-generated stays hand-registered and
is named in `commands.HAND_REGISTERED_EXCEPTIONS`; this test asserts that list is
explicit (every live tool is either registry-generated or a named exception, with
no overlap and no silent gap).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import project_keys as project_keys_module
from exomem import server as server_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"
TOOL_SURFACE_CONTRACT_PATH = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"
DISCOVERY_FIELDS = (
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "icons",
    "annotations",
    "meta",
    "execution",
)


def _build_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build the server exactly as the fixture was captured (see module docstring)."""
    vault_root = tmp_path / "schema_vault"
    shutil.copytree(FIXTURE_VAULT, vault_root)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease")
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault_root))
    return server_module.build_server(require_auth=False)


def _live_schemas(mcp) -> dict[str, dict]:
    """The wire-level {name: {description, inputSchema}} for every registered tool."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict] = {}
    for t in tools:
        mt = t.to_mcp_tool().model_dump(mode="json")
        out[t.name] = {"description": mt["description"], "inputSchema": mt["inputSchema"]}
    return out


def _canonical(entry: dict) -> str:
    """Order-preserving canonical JSON so the comparison is truly byte-for-byte
    (property order in `inputSchema` mirrors the signature and is load-bearing)."""
    return json.dumps(entry, ensure_ascii=False, sort_keys=False, indent=2)


def _tool_surface_sha256(mcp) -> tuple[str, int]:
    tools = asyncio.run(mcp.list_tools())
    surface = []
    for tool in sorted(tools, key=lambda item: item.name):
        wire = tool.to_mcp_tool().model_dump(mode="json")
        assert tuple(wire) == DISCOVERY_FIELDS, (
            "FastMCP discovery fields changed; deliberately review and fingerprint "
            f"the new wire surface: {tuple(wire)}"
        )
        surface.append({field: wire[field] for field in DISCOVERY_FIELDS})
    canonical = json.dumps(
        surface,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), len(surface)


def test_live_tools_match_committed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = _live_schemas(_build_server(monkeypatch, tmp_path))

    # Same set of tools — nothing added, nothing dropped.
    assert set(live) == set(baseline), (
        f"tool set drifted: added={sorted(set(live) - set(baseline))} "
        f"removed={sorted(set(baseline) - set(live))}"
    )

    # Each tool's description + inputSchema byte-for-byte (order included).
    mismatches = [
        name for name in sorted(baseline) if _canonical(live[name]) != _canonical(baseline[name])
    ]
    assert not mismatches, (
        "MCP schema drift (Claude's tool view changed) for: "
        + ", ".join(mismatches)
        + "\nFirst diff:\n"
        + next(
            (
                f"--- baseline[{n}]\n{_canonical(baseline[n])}\n+++ live[{n}]\n{_canonical(live[n])}"
                for n in mismatches
            ),
            "",
        )
    )


def test_full_mcp_discovery_surface_matches_packaged_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert TOOL_SURFACE_CONTRACT_PATH.is_file(), (
        "missing packaged tool-surface contract; run scripts/dump-tool-schemas.py"
    )
    contract = json.loads(TOOL_SURFACE_CONTRACT_PATH.read_text(encoding="utf-8"))
    digest, tool_count = _tool_surface_sha256(_build_server(monkeypatch, tmp_path))

    assert contract["fields"] == list(DISCOVERY_FIELDS)
    assert contract["tool_count"] == tool_count
    assert contract["sha256"] == digest, (
        "full MCP discovery surface drifted; run scripts/dump-tool-schemas.py, "
        "then refresh and verify every registered external connector"
    )


def test_remember_discovery_schema_is_vault_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_before = _build_server(monkeypatch, tmp_path)
    before = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")
        for tool in asyncio.run(mcp_before.list_tools())
    }["remember"]

    vault_root = tmp_path / "schema_vault"
    project_keys_module.register_project_key(vault_root, "new-project-key")
    mcp_after = server_module.build_server(require_auth=False)
    after = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")
        for tool in asyncio.run(mcp_after.list_tools())
    }["remember"]

    assert after == before, (
        "remember discovery changed when the vault gained a project key; dynamic "
        "tool schemas silently invalidate hosted connector registrations"
    )


def test_readiness_fingerprint_matches_actual_registered_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    mcp = _build_server(monkeypatch, tmp_path)
    actual_digest, _ = _tool_surface_sha256(mcp)

    response = TestClient(mcp.http_app()).get("/health/ready")

    assert response.status_code == 200
    assert response.json()["mcp_tool_surface_sha256"] == actual_digest


def test_hand_registered_exceptions_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every live tool is either registry-generated or a named hand-registered
    exception — no silent gaps, no overlap. Skips cleanly until the registry exists
    (pre-refactor), so this stays green at every step of the migration."""
    try:
        from exomem import commands as commands_module
    except ImportError:
        pytest.skip("registry (commands.py) not introduced yet")

    mcp = _build_server(monkeypatch, tmp_path)
    live = set(_live_schemas(mcp))

    generated = {c.name for c in commands_module.PRODUCT_COMMANDS if "mcp" in c.surfaces}
    exceptions = set(commands_module.HAND_REGISTERED_EXCEPTIONS)

    # No tool is both generated and a hand-registered exception.
    assert not (generated & exceptions), (
        f"tools both generated and excepted: {sorted(generated & exceptions)}"
    )
    # Every live tool is accounted for explicitly.
    unaccounted = live - generated - exceptions
    assert not unaccounted, (
        f"live tools neither registry-generated nor in HAND_REGISTERED_EXCEPTIONS: "
        f"{sorted(unaccounted)}"
    )
    # Every named exception (that is exposed under the test env) is actually live.
    assert exceptions <= live, (
        f"HAND_REGISTERED_EXCEPTIONS names a tool that isn't registered: "
        f"{sorted(exceptions - live)}"
    )


def test_process_media_mcp_schema_annotations_and_leaf_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands as commands_module
    from exomem import media_jobs

    mcp = _build_server(monkeypatch, tmp_path)
    vault = tmp_path / "schema_vault"
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools["process_media"].to_mcp_tool().model_dump(mode="json")
    schema = tool["inputSchema"]
    [command] = [cmd for cmd in commands_module.PRODUCT_COMMANDS if cmd.name == "process_media"]
    operation_param = next(param for param in command.params if param.name == "operation")
    assert set(schema["properties"]) == {"path", "operation"}
    assert schema.get("required", []) == []
    assert schema["properties"]["operation"]["enum"] == list(operation_param.choices)
    assert "governed" in schema["properties"]["path"]["description"].lower()
    assert "process" in schema["properties"]["operation"]["description"].lower()
    assert tool["annotations"] == {
        "title": "process_media",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }

    result = asyncio.run(
        mcp.call_tool("process_media", {"operation": "status"}, run_middleware=False)
    )
    structured = result.structured_content
    assert structured["operation"] == "status"
    assert structured["counts"] == media_jobs.status(vault)["counts"]
