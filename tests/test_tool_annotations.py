"""Every MCP tool carries explicit behaviour annotations (readOnly / destructive /
open-world hints) so cautious clients render them correctly.

ChatGPT's tool-call panel badged the read-only `find` as WRITE / OPEN-WORLD /
DESTRUCTIVE because the tools shipped no MCP annotation hints, so the client
assumed the worst. The hints are derived from the command registry:
`readOnlyHint = not cli_writes`, `openWorldHint = False` for every tool (exomem is
a closed local vault), and `destructiveHint` is True only for the small set of
overwrite/remove ops (`commands.DESTRUCTIVE_OPS`). This test pins that contract
against the live server so a new tool can't ship un-annotated or mis-classified.

Built against the repo fixture vault with the same deterministic env as
`tests/test_mcp_schema_fidelity.py` (embeddings/media/clip off, tier-2 ON).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kb_mcp import commands as commands_module
from kb_mcp import server as server_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


def _build_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("KB_MCP_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("KB_MCP_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")
    monkeypatch.delenv("KB_MCP_DISABLE_TIER2", raising=False)
    monkeypatch.setenv("KB_MCP_VAULT_PATH", str(FIXTURE_VAULT))
    return server_module.build_server(require_auth=False)


def _live_annotations(mcp) -> dict[str, dict | None]:
    """The wire-level `annotations` object for every registered tool."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict | None] = {}
    for t in tools:
        mt = t.to_mcp_tool().model_dump(mode="json")
        out[t.name] = mt.get("annotations")
    return out


def test_every_tool_is_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    ann = _live_annotations(_build_server(monkeypatch))
    missing = [name for name, a in ann.items() if not a]
    assert not missing, f"tools missing MCP annotations: {sorted(missing)}"


def test_open_world_hint_false_for_all(monkeypatch: pytest.MonkeyPatch) -> None:
    # exomem operates on a closed local vault and reaches no external systems.
    ann = _live_annotations(_build_server(monkeypatch))
    open_world = [n for n, a in ann.items() if not a or a.get("openWorldHint") is not False]
    assert not open_world, f"tools not marked closed-world: {sorted(open_world)}"


def test_read_only_hint_matches_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    ann = _live_annotations(_build_server(monkeypatch))
    for cmd in commands_module.COMMANDS:
        if "mcp" not in cmd.surfaces:
            continue  # note: hand-registered for MCP, checked below
        assert ann[cmd.name]["readOnlyHint"] is cmd.read_only, (
            f"{cmd.name}: readOnlyHint={ann[cmd.name]['readOnlyHint']} "
            f"but registry cli_writes={cmd.cli_writes}"
        )


def test_destructive_hint_only_for_overwrite_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    ann = _live_annotations(_build_server(monkeypatch))
    for cmd in commands_module.COMMANDS:
        if "mcp" not in cmd.surfaces or cmd.read_only:
            continue
        expected = cmd.name in commands_module.DESTRUCTIVE_OPS
        assert ann[cmd.name]["destructiveHint"] is expected, (
            f"{cmd.name}: destructiveHint={ann[cmd.name]['destructiveHint']} expected {expected}"
        )


def test_find_is_a_safe_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact regression ChatGPT surfaced.
    a = _live_annotations(_build_server(monkeypatch))["find"]
    assert a["readOnlyHint"] is True
    assert a["destructiveHint"] is False
    assert a["openWorldHint"] is False


def test_hand_registered_tools_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    ann = _live_annotations(_build_server(monkeypatch))
    # note is an additive write (creates/updates a note, never overwrites blindly).
    assert ann["note"]["readOnlyHint"] is False
    assert ann["note"]["destructiveHint"] is False
    # mint_upload_token issues a write-capable credential; mint_download_token is read-only.
    assert ann["mint_upload_token"]["readOnlyHint"] is False
    assert ann["mint_download_token"]["readOnlyHint"] is True
