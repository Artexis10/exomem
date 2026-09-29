"""The hook nudges send their full text once and stay short after (`shrink-bootstrap`).

The Stop capture check and the retrieval reminder used to repeat their full doctrine
on every fire, dozens of times in one session, against a contract the agent had
already read. Now each sends the full text on the session's first fire (and again
after a compaction, which rewrites the context it lived in) and a short line after;
the retrieval reminder is silent between at `balanced`. The short forms keep the rule
by name, and every text has a byte ceiling.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "src" / "exomem" / "_hooks"
PLUGIN_HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "claude-code" / "hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = _load("exomem_capture_nudge")
retrieve = _load("exomem_retrieve_nudge")
checkpoint = _load("exomem_continuation_checkpoint")

#: Byte ceilings for what a hook injects (served-JSON is a few bytes larger).
CEILINGS = {
    "capture short": (capture.REMINDER_SHORT, 320),
    "episode ask": (capture.EPISODE_ASK, 340),
    "retrieval reminder": (retrieve.REMINDER, 300),
    "retrieval pointer": (retrieve.REMINDER_POINTER, 120),
}


@pytest.mark.parametrize("name", CEILINGS)
def test_each_short_nudge_stays_under_its_ceiling(name):
    text, ceiling = CEILINGS[name]
    assert len(text.encode("utf-8")) <= ceiling
    assert text.isascii()


def test_the_checkpoint_context_is_capped_at_two_kib():
    assert checkpoint.MAX_CONTEXT_BYTES == 2048


def test_the_short_capture_check_keeps_the_incident_rules_by_name():
    text = capture.REMINDER_SHORT
    assert text.startswith("[Exomem capture check]")
    for rule in (
        "live policy",
        "no transcripts",
        "replace_memory supersedes",
        "Planning/plan_memory",
        "Records/record_memory",
        "transient code/test/CI",
    ):
        assert rule in text


def test_the_episode_ask_keeps_the_record_call_the_key_and_the_escape():
    text = capture.EPISODE_ASK
    assert 'action="record"' in text and "episode_memory" in text
    assert "{key}" in text
    assert "Otherwise do nothing." in text


def test_the_retrieval_texts_keep_recall_the_miss_reading_and_the_skip():
    text = retrieve.REMINDER
    assert "ask_memory" in text and "cite" in text
    assert "not found in that scope" in text
    assert "skip" in text
    assert "activate_context" in retrieve.REMINDER_POINTER


def _run(script: str, event: dict, home: Path, **env: str) -> str:
    environment = {
        **os.environ,
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_HOOK_CLIENT": "claude",
        **env,
    }
    for name in ("EXOMEM_RETRIEVE_INJECT", "KB_RETRIEVE_INJECT", "EXOMEM_SURFACE"):
        environment.pop(name, None)
    done = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    return done.stdout.strip()


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


PROMPT = "Please look at how the depot ledger reconciles and tell me what changed."


def test_the_capture_check_is_full_once_then_short(tmp_path):
    event = {"session_id": "s-cap", "last_assistant_message": "x" * 400}
    env = {
        "EXOMEM_PROMINENCE": "balanced",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": "0",
        "EXOMEM_EPISODE_ASK_TURNS": "0",
    }
    replies = [json.loads(_run("exomem_capture_nudge.py", event, tmp_path, **env)) for _ in range(3)]

    assert [r["reason"] for r in replies] == [
        capture.REMINDER,
        capture.REMINDER_SHORT,
        capture.REMINDER_SHORT,
    ]


def test_the_retrieval_reminder_is_once_per_session_at_balanced(tmp_path):
    env = {"EXOMEM_PROMINENCE": "balanced"}
    first = _context(_run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "s-r"}, tmp_path, **env))
    second = _context(_run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "s-r"}, tmp_path, **env))
    other = _context(_run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "s-other"}, tmp_path, **env))

    assert first == retrieve.REMINDER
    assert second == ""
    assert other == retrieve.REMINDER


def test_maximal_follows_the_full_reminder_with_a_pointer_on_every_prompt(tmp_path):
    env = {"EXOMEM_PROMINENCE": "maximal"}
    texts = [
        _context(_run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "s-m"}, tmp_path, **env))
        for _ in range(3)
    ]

    assert texts == [retrieve.REMINDER, retrieve.REMINDER_POINTER, retrieve.REMINDER_POINTER]


def test_a_lifecycle_event_rearms_both_full_texts(tmp_path):
    session = "s-compact"
    _run(
        "exomem_retrieve_nudge.py",
        {"prompt": PROMPT, "session_id": session},
        tmp_path,
        EXOMEM_PROMINENCE="balanced",
    )
    _run(
        "exomem_capture_nudge.py",
        {"session_id": session, "last_assistant_message": "x" * 400},
        tmp_path,
        EXOMEM_PROMINENCE="balanced",
        EXOMEM_EPISODE_ASK_TURNS="0",
    )
    state = tmp_path / ".cache" / "exomem-nudge"
    assert {f"retrieve_{session}", session} <= {p.name for p in state.iterdir()}

    checkpoint._rearm_nudges(tmp_path, session)

    # Only the two once-per-session stamps go; the episode counter is not re-armed.
    assert {p.name for p in state.iterdir()} == {f"episode_{session}"}
    again = _context(
        _run(
            "exomem_retrieve_nudge.py",
            {"prompt": PROMPT, "session_id": session},
            tmp_path,
            EXOMEM_PROMINENCE="balanced",
        )
    )
    assert again == retrieve.REMINDER


def test_the_plugin_mirrors_stay_byte_identical():
    for name in (
        "exomem_capture_nudge.py",
        "exomem_retrieve_nudge.py",
        "exomem_continuation_checkpoint.py",
    ):
        assert (HOOKS / name).read_bytes() == (PLUGIN_HOOKS / name).read_bytes()
