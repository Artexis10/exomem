from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from exomem import commands, hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_hosted_profile_commands_remain_hashable_with_mcp_metadata() -> None:
    selected = commands.product_commands_for_profile(commands.HOSTED_ALPHA_AGENT_PROFILE, "rest")

    assert len(set(selected)) == len(selected)
    artifact_command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "preserve_artifacts"
    )
    metadata_variant = replace(artifact_command, mcp_meta=MappingProxyType({}))
    assert artifact_command != metadata_variant
    assert len({artifact_command, metadata_variant}) == 2
    assert artifact_command.mcp_meta == {"openai/fileParams": ("files",)}
    assert {key: list(value) for key, value in artifact_command.mcp_meta.items()} == {
        "openai/fileParams": ["files"]
    }


def test_every_hosted_skill_declares_and_uses_only_alpha_profile_tools() -> None:
    dependencies = hosted_plugins.skill_dependencies(REPO_ROOT)

    assert tuple(dependencies) == hosted_plugins.SKILL_NAMES
    assert set(dependencies["exomem"]) == {
        "ask_memory",
        "read_memory",
        "remember",
        "observe_memory",
    }


def test_hosted_skills_do_not_depend_on_local_plugin_mechanisms() -> None:
    prose = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "plugins/hosted/skills").glob("*/SKILL.md")
    ).lower()

    for forbidden in (
        "uvx",
        "exomem_vault_path",
        "hooks",
        "transfer",
        "media",
        "adopt",
        "maintain",
        "edit_memory",
        "replace_memory",
    ):
        assert not re.search(rf"\b{re.escape(forbidden)}\b", prose)


def test_tool_reference_scanner_rejects_legacy_and_undeclared_callable_names() -> None:
    with pytest.raises(ValueError, match="unavailable Hosted tools"):
        hosted_plugins.validate_skill_text(
            "---\nrequired_tools: [ask_memory]\n---\nUse `ask_memory` and `edit_memory`.",
            Path("malicious.md"),
        )

    with pytest.raises(ValueError, match="unavailable Hosted tools"):
        hosted_plugins.validate_skill_text(
            "---\nrequired_tools: [ask_memory]\n---\nUse ask_memory, then use edit_memory.",
            Path("ordinary-prose.md"),
        )

    for prose in (
        "Invoke edit_memory after ask_memory.",
        "Query via edit_memory after ask_memory.",
    ):
        with pytest.raises(ValueError, match="unavailable Hosted tools"):
            hosted_plugins.validate_skill_text(
                f"---\nrequired_tools: [ask_memory]\n---\nCall ask_memory. {prose}",
                Path("ordinary-prose.md"),
            )


def test_hosted_public_inputs_pass_the_hosted_no_leak_gate() -> None:
    hosted_plugins.validate_hosted_public_inputs(REPO_ROOT)
