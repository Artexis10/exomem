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
    # 300 -> 332 (was 272 in use): the two rules the diet dropped (no repeat searches on a recurring
    # reminder, the KB is the source of truth) are restored for at most 60 bytes.
    "retrieval reminder": (retrieve.REMINDER, 332),
    "retrieval pointer": (retrieve.REMINDER_POINTER, 160),
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


def test_the_retrieval_reminder_keeps_the_two_rules_the_diet_once_dropped():
    """Both came from the pre-diet text and prevent a known failure: re-running the same
    search every time a recurring reminder appears, and reading a KB miss as absence."""
    text = retrieve.REMINDER
    assert "source of truth for prior conclusions" in text
    assert "reuse fresh KB context" in text
    assert "reminder recurs" in text and "repeat" in text


def test_the_maximal_pointer_keeps_the_already_covered_escape():
    pointer = retrieve.REMINDER_POINTER
    assert "already covers" in pointer and "skip" in pointer


def _run(script: str, event: dict, home: Path, **env: str) -> str:
    environment = {
        **os.environ,
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_HOOK_CLIENT": "claude",
        **env,
    }
    for name in (
        "EXOMEM_RETRIEVE_INJECT",
        "KB_RETRIEVE_INJECT",
        "EXOMEM_SURFACE",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
    ):
        environment.pop(name, None)
    # Env passed by the caller is authoritative, including a cooldown it sets.
    environment.update(env)
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

    assert first == retrieve.REMINDER
    assert second == ""


def test_the_client_wide_cooldown_still_suppresses_a_second_tab(tmp_path):
    """A fresh session opened inside the client-wide window is not told again what
    another session was just told; it stays eligible and is reminded after the window."""
    env = {"EXOMEM_PROMINENCE": "balanced"}
    _run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "tab-a"}, tmp_path, **env)
    quiet = _context(_run("exomem_retrieve_nudge.py", {"prompt": PROMPT, "session_id": "tab-b"}, tmp_path, **env))
    later = _context(
        _run(
            "exomem_retrieve_nudge.py",
            {"prompt": PROMPT, "session_id": "tab-b"},
            tmp_path,
            EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC="0",
            **env,
        )
    )

    assert quiet == ""
    assert later == retrieve.REMINDER


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

    # The two once-per-session stamps and the client-wide one go (a session that just
    # compacted is fresh again); the episode counter is not re-armed.
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


def _isolated_env(tmp_path: Path, client: str, **extra: str) -> dict[str, str]:
    """A hook environment with NO shared-home override: only the client's own
    config-dir variable (or none) decides where the hook keeps its state."""
    keep = {k: v for k, v in os.environ.items() if k.startswith(("PATH", "SYSTEMROOT", "LANG"))}
    return {
        **keep,
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "EXOMEM_HOOK_CLIENT": client,
        "EXOMEM_PROMINENCE": "balanced",
        **extra,
    }


def _spawn(script: str, args: list[str], event: dict, env: dict[str, str]) -> str:
    done = subprocess.run(
        [sys.executable, str(HOOKS / script), *args],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
        cwd=env["HOME"],
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("client", "config_var"),
    [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("claude", None), ("codex", None)],
)
def test_a_real_precompact_rearms_the_retrieval_reminder_under_a_relocated_config_dir(
    tmp_path, client, config_var
):
    """End to end: PreCompact through the checkpoint hook's own entry point, then the
    retrieve hook again. The checkpoint clears the stamps under `resolve_home`, which
    honours CLAUDE_CONFIG_DIR / CODEX_HOME; the nudge hooks must look in the same place
    or the compaction never re-arms them."""
    (tmp_path / "home").mkdir()
    extra = {}
    if config_var:
        config = tmp_path / "relocated-config"
        config.mkdir()
        extra[config_var] = str(config)
    env = _isolated_env(tmp_path, client, **extra)
    session = "s-relocated"
    prompt = {"prompt": PROMPT, "session_id": session}

    first = _context(_spawn("exomem_retrieve_nudge.py", [], prompt, env))
    quiet = _context(_spawn("exomem_retrieve_nudge.py", [], prompt, env))
    assert first == retrieve.REMINDER
    assert quiet == ""

    _spawn(
        "exomem_continuation_checkpoint.py",
        ["--client", client],
        {
            "hook_event_name": "PreCompact",
            "session_id": session,
            "trigger": "auto",
            "cwd": str(tmp_path / "home"),
            "transcript_path": str(tmp_path / "home" / "t.jsonl"),
        },
        env,
    )

    again = _context(_spawn("exomem_retrieve_nudge.py", [], prompt, env))
    assert again == retrieve.REMINDER


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"CLAUDE_CONFIG_DIR": "~/cfg-claude"},
        {"CODEX_HOME": "~/cfg-codex"},
        {"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": ""},
        {"EXOMEM_HOOK_HOME": "~/shared", "CLAUDE_CONFIG_DIR": "/x", "CODEX_HOME": "/y"},
    ],
)
def test_every_hook_resolves_its_home_the_way_the_checkpoint_does(
    tmp_path, monkeypatch, client, environ
):
    """The three standalone scripts cannot import each other, so the resolution is
    mirrored; this pins the mirrors to `resolve_home`, the one definition."""
    for name in ("EXOMEM_HOOK_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", client)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)

    expected = checkpoint.resolve_home(client)
    assert retrieve._hook_home() == expected
    assert capture._hook_home() == expected


def test_the_plugin_mirrors_stay_byte_identical():
    for name in (
        "exomem_capture_nudge.py",
        "exomem_retrieve_nudge.py",
        "exomem_continuation_checkpoint.py",
    ):
        assert (HOOKS / name).read_bytes() == (PLUGIN_HOOKS / name).read_bytes()
