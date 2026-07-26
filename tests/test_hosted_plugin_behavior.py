from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_hosted_behavior_fixture_covers_the_local_automatic_memory_contract() -> None:
    fixture = json.loads((REPO_ROOT / "plugins/hosted/behavior-fixtures-v1.json").read_text())
    ids = {scenario["id"] for scenario in fixture["scenarios"]}
    assert ids == {
        "quiet-retrieval",
        "automatic-decision-capture",
        "automatic-failure-capture",
        "automatic-pattern-capture",
        "automatic-research-capture",
        "fresh-chat-continuation",
        "avoid-redundant-trivial-writes",
        "no-transcript-dump",
    }
    allowed = {
        command.name
        for command in commands.product_commands_for_profile("hosted-alpha-agent-v1", "rest")
    }
    for scenario in fixture["scenarios"]:
        assert isinstance(scenario["turn"], str) and scenario["turn"]
        assert isinstance(scenario["starting_context"], str) and scenario["starting_context"]
        assert type(scenario["fresh_chat"]) is bool and type(scenario["no_write"]) is bool
        assert set(scenario["expected_tools"]) <= allowed
        assert scenario["citation"] is (
            scenario["id"]
            in {"quiet-retrieval", "automatic-research-capture", "fresh-chat-continuation"}
        )
        if scenario["capture"] is not None:
            assert scenario["capture"]["distilled"] is True
            assert scenario["capture"]["max_words"] <= 160


def test_hosted_core_skill_teaches_the_fixture_behavior_without_save_prompt_or_transcript() -> None:
    text = (REPO_ROOT / "plugins/hosted/skills/exomem/SKILL.md").read_text(encoding="utf-8").lower()
    for phrase in (
        "quietly use `ask_memory`",
        "cite a useful retrieved note",
        "clear reusable decision",
        "raw conversation transcripts",
    ):
        assert phrase in text


def test_behavior_fixture_is_executable_against_client_trace_observations() -> None:
    fixture = json.loads((REPO_ROOT / "plugins/hosted/behavior-fixtures-v1.json").read_text())
    scenario = next(
        item for item in fixture["scenarios"] if item["id"] == "automatic-decision-capture"
    )
    observation = {
        "tools": ["ask_memory", "remember"],
        "fresh_chat": False,
        "citation": None,
        "write_count": 1,
        "capture": {
            "kind": "decision",
            "text": "Use the versioned resource for future client connections.",
            "distilled": True,
            "transcript_dump": False,
        },
    }

    hosted_plugins.validate_behavior_observation(scenario, observation)
    with pytest.raises(ValueError, match="tool sequence"):
        hosted_plugins.validate_behavior_observation(scenario, {**observation, "tools": []})
    with pytest.raises(ValueError, match="distilled payload"):
        hosted_plugins.validate_behavior_observation(
            scenario,
            {**observation, "capture": {**observation["capture"], "transcript_dump": True}},
        )
