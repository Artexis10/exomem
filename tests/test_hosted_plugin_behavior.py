from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_hosted_behavior_fixture_covers_the_local_automatic_memory_contract() -> None:
    fixture = json.loads((REPO_ROOT / "plugins/hosted/behavior-fixtures-v1.json").read_text())
    ids = {scenario["id"] for scenario in fixture["scenarios"]}
    assert ids == {
        "quiet-retrieval", "automatic-decision-capture", "automatic-failure-capture",
        "automatic-pattern-capture", "automatic-research-capture", "fresh-chat-continuation",
        "avoid-redundant-trivial-writes", "no-transcript-dump",
    }


def test_hosted_core_skill_teaches_the_fixture_behavior_without_save_prompt_or_transcript() -> None:
    text = (REPO_ROOT / "plugins/hosted/skills/exomem/SKILL.md").read_text(encoding="utf-8").lower()
    for phrase in ("quietly use `ask_memory`", "cite a useful retrieved note", "clear reusable decision", "raw conversation transcripts"):
        assert phrase in text
