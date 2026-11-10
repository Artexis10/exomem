"""The context-replay suite: recording on a case seed, case-directory scenarios, answer checks, the replay report and
the blind judge batch. Seeds, cases and keys here are tiny invented fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from import_guidance import recording
from import_guidance.recording import Arm
from import_guidance.scripted_replies import load_scenario

ROOT = Path(__file__).resolve().parents[1]
FERRY_PAGE = """---
exomem_id: 6f0f7c52-3a1b-5c1e-9a55-0d2f4c7e8b10
type: research-note
project: harbour-ferry
status: active
created: 2026-01-05
updated: 2026-02-01
sources: []
tags: [ferry]
---

# Ferry timetable

## Claim
- id: first-sailing

The first sailing leaves the harbour at 07:15 on weekdays.
"""
TURN = "When does the first ferry sail on weekdays?"


def ferry_seed(root: Path) -> Path:
    page = root / "seed" / "Notes" / "Research" / "harbour-ferry" / "timetable.md"
    page.parent.mkdir(parents=True)
    page.write_text(FERRY_PAGE, encoding="utf-8")
    return root / "seed"


def replay_scenario(folder: Path, *, seed: str, steps: list[dict] | None = None) -> Path:
    path = folder / "ferry.json"
    path.write_text(
        json.dumps(
            {
                "id": "ferry",
                "title": "t",
                "suite": "context-replay",
                "prompt": TURN,
                "served_tools": ["recall_context"],
                "replies": {"recall_context": {"$shape": "context"}},
                "default_reply": {"error": "unscripted"},
                "record": {
                    "seed": seed,
                    "steps": steps
                    or [
                        {
                            "as": "context",
                            "tool": "recall_context",
                            "arguments": {"turn": TURN},
                            "warm": {"seconds": 3, "ignore": ["served_context_id"]},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_recording_on_a_case_seed_serves_that_seeds_page_and_no_throwaway_page(tmp_path: Path) -> None:
    """Catches a recording made on the fixture vault or on a settled one: the served context would hold a fixture note
    or a throwaway page, and a cold first read would hold nothing at all."""
    ferry_seed(tmp_path)
    scenario = load_scenario(replay_scenario(tmp_path, seed="seed"))
    target = tmp_path / "shapes.json"
    assert recording.record(scenario, Arm("test", ROOT), target, tmp_path / "scratch") == []
    served = json.dumps(recording.load_recording(target)["shapes"]["context"])
    assert "Ferry timetable" in served
    assert "Settle probe" not in served and "feature-flag" not in served


def test_a_seeded_recording_refuses_a_step_that_writes_throwaway_pages(tmp_path: Path) -> None:
    """Catches a scenario that settles the graph on a seeded vault: its throwaway pages are ones `recall_context`
    could serve to the agent."""
    ferry_seed(tmp_path)
    scenario = load_scenario(replay_scenario(tmp_path, seed="seed", steps=[{"settle_graph": True}]))
    problems = recording.record(scenario, Arm("test", ROOT), tmp_path / "shapes.json", tmp_path / "scratch")
    assert problems and "settle_graph" in problems[0]


def test_a_seed_that_is_not_a_folder_fails_the_recording_by_name(tmp_path: Path) -> None:
    scenario = load_scenario(replay_scenario(tmp_path, seed="no-such-seed"))
    problems = recording.record(scenario, Arm("test", ROOT), tmp_path / "shapes.json", tmp_path / "scratch")
    assert problems and "no-such-seed" in problems[0]
