"""Every MCP tool carries explicit behaviour annotations (readOnly / destructive /
open-world hints) so cautious clients render them correctly.

ChatGPT's tool-call panel badged the read-only `find` as WRITE / OPEN-WORLD /
DESTRUCTIVE because the tools shipped no MCP annotation hints, so the client
assumed the worst. The hints are derived from the command registry:
`readOnlyHint = not cli_writes`, `openWorldHint = True` for external artifact
downloads, and `destructiveHint` is True only for the small set of
overwrite/remove ops (`commands.DESTRUCTIVE_OPS`). This test pins that contract
against the live server so a new tool can't ship un-annotated or mis-classified.

Built against the repo fixture vault with the same deterministic env as
`tests/test_mcp_schema_fidelity.py` (embeddings/media/clip off, tier-2 ON).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline

from exomem import commands as commands_module
from exomem import server as server_module
from exomem.command_surface import Command

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


def _build_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    vault_root = tmp_path / "annotation-vault"
    shutil.copytree(FIXTURE_VAULT, vault_root)
    initialize_vault_state_offline(vault_root, source="MCP annotation fixture")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault_root))
    return server_module.build_server(require_auth=False)


def _live_annotations(mcp) -> dict[str, dict | None]:
    """The wire-level `annotations` object for every registered tool."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict | None] = {}
    for t in tools:
        mt = t.to_mcp_tool().model_dump(mode="json", by_alias=True)
        out[t.name] = mt.get("annotations")
    return out


def test_every_tool_is_annotated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ann = _live_annotations(_build_server(monkeypatch, tmp_path))
    missing = [name for name, a in ann.items() if not a]
    assert not missing, f"tools missing MCP annotations: {sorted(missing)}"


def test_open_world_hint_matches_external_download_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # URL provenance alone is closed-world; temporary artifact handles are fetched.
    ann = _live_annotations(_build_server(monkeypatch, tmp_path))
    open_world = {n for n, a in ann.items() if a and a.get("openWorldHint") is True}
    assert open_world == {"capture_source", "preserve_artifacts"}


def test_read_only_hint_matches_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ann = _live_annotations(_build_server(monkeypatch, tmp_path))
    for cmd in commands_module.product_commands_for("mcp"):
        if "mcp" not in cmd.surfaces:
            continue  # note: hand-registered for MCP, checked below
        if cmd.renderings:
            # Listed as its renderings; tests/test_single_purpose_tools_mcp.py pins their hints.
            assert cmd.public_name not in ann
            continue
        assert ann[cmd.public_name]["readOnlyHint"] is cmd.read_only, (
            f"{cmd.name}: readOnlyHint={ann[cmd.public_name]['readOnlyHint']} "
            f"but registry cli_writes={cmd.cli_writes}"
        )


def test_destructive_hint_only_for_overwrite_ops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ann = _live_annotations(_build_server(monkeypatch, tmp_path))
    # Composite workflows can execute replacement/supersession, not only append.
    assert ann["episode_memory"]["destructiveHint"] is True
    assert ann["adoption_studio"]["destructiveHint"] is True
    for cmd in commands_module.product_commands_for("mcp"):
        if "mcp" not in cmd.surfaces or cmd.read_only:
            continue
        if cmd.renderings:
            assert cmd.public_name not in ann
            continue
        expected = cmd.name in commands_module.DESTRUCTIVE_OPS
        assert ann[cmd.public_name]["destructiveHint"] is expected, (
            f"{cmd.name}: destructiveHint={ann[cmd.public_name]['destructiveHint']} expected {expected}"
        )


def test_ask_memory_is_a_safe_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The public recall command remains a safe read.
    a = _live_annotations(_build_server(monkeypatch, tmp_path))["search_memory"]
    assert a["readOnlyHint"] is True
    assert a["destructiveHint"] is False
    assert a["openWorldHint"] is False


def test_no_hand_registered_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ann = _live_annotations(_build_server(monkeypatch, tmp_path))
    assert set(commands_module.HAND_REGISTERED_EXCEPTIONS) == set()
    assert "mint_upload_token" not in ann
    assert "mint_download_token" not in ann
    assert "note" not in ann
    assert ann["transfer_artifact"]["readOnlyHint"] is False


def test_review_question_is_not_advertised_or_classified_as_a_read() -> None:
    command = next(c for c in commands_module.PRODUCT_COMMANDS if c.name == "review_memory")
    assert command.mcp_annotations.read_only_hint is False
    assert command.mcp_annotations.destructive_hint is False
    assert commands_module.invocation_is_read_only(command, {"mode": "attention"})
    assert commands_module.invocation_is_read_only(command, {"mode": "vocabulary"})
    assert not commands_module.invocation_is_read_only(
        command,
        {
            "mode": "vocabulary", "path": "Knowledge Base/Notes/example.md",
            "query": "What does this term mean?", "family": "relation-type/v1",
        },
    )


def test_product_titles_reach_both_mcp_title_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = asyncio.run(_build_server(monkeypatch, tmp_path).list_tools())
    wire = {tool.name: tool.to_mcp_tool() for tool in tools}
    for command in commands_module.product_commands_for("mcp"):
        if "mcp" not in command.surfaces:
            continue
        assert command.public_title
        if command.renderings:
            assert command.public_name not in wire
            for rendering in command.renderings:
                tool = wire[rendering.name]
                assert tool.title == tool.annotations.title == rendering.title
            continue
        tool = wire[command.public_name]
        assert tool.title == tool.annotations.title == command.public_title


def test_non_product_title_defaults_to_the_public_name() -> None:
    command = Command(
        name="internal_read", leaf=lambda: None, params=(), surfaces=frozenset(),
        preferred_name="public_read",
    )
    assert command.mcp_annotations.title == "Public Read"


def test_api_reference_contract_is_stable_across_python_docstring_indentation(monkeypatch) -> None:
    """3.13 dedents compiled docs; a 3.11 renderer must emit the same contract."""
    command = next(c for c in commands_module.PRODUCT_COMMANDS if c.name == "coordination_status")
    descriptions = []
    for doc in ("Summary.\n\n    Details.\n    ", "Summary.\n\nDetails.\n"):
        monkeypatch.setattr(command.leaf, "__doc__", doc)
        rebuilt = next(c for c in commands_module._build_product_commands() if c.name == command.name)
        descriptions.append(rebuilt.doc)
    assert descriptions[0] == descriptions[1]
