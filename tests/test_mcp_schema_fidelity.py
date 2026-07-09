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
import json
from pathlib import Path

import pytest

from exomem import server as server_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


def _build_server(monkeypatch: pytest.MonkeyPatch):
    """Build the server exactly as the fixture was captured (see module docstring)."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(FIXTURE_VAULT))
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


def test_live_tools_match_committed_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = _live_schemas(_build_server(monkeypatch))

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


def test_hand_registered_exceptions_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every live tool is either registry-generated or a named hand-registered
    exception — no silent gaps, no overlap. Skips cleanly until the registry exists
    (pre-refactor), so this stays green at every step of the migration."""
    try:
        from exomem import commands as commands_module
    except ImportError:
        pytest.skip("registry (commands.py) not introduced yet")

    mcp = _build_server(monkeypatch)
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
