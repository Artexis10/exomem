"""Next-turn delivery of the per-turn capture reminder (`deliver-capture-reminder-next-turn`).

Every case runs the real hook scripts as subprocesses, the way the clients do: the Stop
hook defers the reminder, and the UserPromptSubmit hook delivers it. The state home and
the config file are temporary, and `HOME` is too, so no credential or live service is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from exomem._hooks import exomem_retrieve_nudge as retrieve_hook

HOOKS = Path(__file__).resolve().parents[1] / "src" / "exomem" / "_hooks"
SESSION = "session-next-turn"
#: The stable start of the capture reminder, which is all these tests pin of its wording.
MARKER = "[Evermatter capture check]"
EPISODE_MARKER = "[Evermatter episode check]"
PROMPT = "Please look at how the depot ledger reconciles and tell me what changed."
SIX_HOURS = 6 * 3600


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if k.startswith(("PATH", "SYSTEMROOT", "LANG"))}
    return {
        **keep,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "EXOMEM_HOOK_HOME": str(tmp_path / "state"),
        "EXOMEM_HOOK_CLIENT": "claude",
        "EXOMEM_CONFIG_PATH": str(tmp_path / "config.json"),
        "EXOMEM_PROMINENCE": "balanced",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": "0",
        "EXOMEM_EPISODE_ASK_TURNS": "0",
        **extra,
    }


def _run(script: str, event: dict, env: dict[str, str], *args: str) -> str:
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
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _stop(tmp_path: Path, env: dict[str, str], *args: str, reply: str = "Pushed.", session: str = SESSION) -> str:
    """A Stop event whose turn pushed successfully: below `maximal` only a landing is nudged."""
    rows = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push"}}
                ],
            },
        },
    ]
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    event = {
        "session_id": session,
        "transcript_path": str(transcript),
        "last_assistant_message": reply,
    }
    return _run("exomem_capture_nudge.py", event, env, *args)


def _prompt(env: dict[str, str], prompt: str, *args: str, session: str = SESSION) -> str:
    out = _run("exomem_retrieve_nudge.py", {"session_id": session, "prompt": prompt}, env, *args)
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _pending(tmp_path: Path) -> list[Path]:
    """The reminders waiting in the state home; their names are a digest, so the test lists them."""
    return sorted((tmp_path / "state" / ".cache" / "exomem-nudge").glob("pending_*"))


def _log(tmp_path: Path) -> str:
    log = tmp_path / "state" / "exomem-capture-nudge.log"
    return log.read_text("utf-8") if log.exists() else ""


@pytest.mark.parametrize("source", ["env", "config"])
def test_a_deferred_reminder_arrives_once_on_the_next_prompt(tmp_path, source):
    """The failure: the reminder is lost, repeated, or held back by the retrieval gates.

    "yes" is below the retrieval hook's length gate, so that hook alone stays silent on it.
    """
    extra = {}
    if source == "env":
        extra["EXOMEM_CAPTURE_NUDGE_DELIVERY"] = "next-turn"
    else:
        (tmp_path / "config.json").write_text(
            json.dumps({"capture_nudge_delivery": "next-turn"}), encoding="utf-8"
        )
    env = _env(tmp_path, **extra)

    assert _stop(tmp_path, env) == ""  # the Stop is not blocked
    assert _pending(tmp_path)

    assert MARKER in _prompt(env, "yes")
    assert _prompt(env, "yes") == ""
    assert not _pending(tmp_path)
    assert [row.split(" ", 2)[2] for row in _log(tmp_path).splitlines()] == [
        "nudge deferred",
        "nudge delivered",
    ]


def test_a_settings_conflict_skips_the_turn_in_both_hooks(tmp_path):
    """The failure: a hook picks one of two different values for the same setting and nudges on a guess."""
    (tmp_path / "agreed").mkdir()
    agreed = _env(tmp_path / "agreed", EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    assert _stop(tmp_path / "agreed", agreed) == ""
    assert _pending(tmp_path / "agreed")
    assert retrieve_hook.REMINDER in _prompt(agreed, PROMPT)

    conflicted = _env(
        tmp_path,
        EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn",
        EVERMATTER_CAPTURE_NUDGE_DELIVERY="inline",
    )

    assert _stop(tmp_path, conflicted) == ""
    assert not _pending(tmp_path)
    assert _prompt(conflicted, PROMPT) == ""
    assert "EVERMATTER_CAPTURE_NUDGE_DELIVERY" in _log(tmp_path)


def test_a_hook_flag_wins_over_an_inherited_product_prefix_name_in_both_hooks(tmp_path):
    """The failure: the flag's value disagrees with the inherited EVERMATTER_ name, so the hook skips every turn."""
    env = _env(
        tmp_path,
        EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn",
        EVERMATTER_HOOK_HOME=str(tmp_path / "inherited-home"),
    )
    del env["EXOMEM_HOOK_HOME"]
    flag = ("--hook-home", str(tmp_path / "state"))

    assert _stop(tmp_path, env, *flag) == ""
    assert _pending(tmp_path)  # the capture hook ran in the flag's home
    assert MARKER in _prompt(env, "yes", *flag)  # and so did the retrieval hook
    assert "settings conflict" not in _log(tmp_path)


def test_the_deferred_reminder_shares_one_reply_with_the_retrieval_reminder(tmp_path):
    """The failure: two JSON objects on stdout, or one block overwriting the other."""
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    _stop(tmp_path, env)

    context = _prompt(env, PROMPT)

    assert retrieve_hook.REMINDER in context
    assert MARKER in context


def test_the_native_activation_path_delivers_the_deferred_reminder(tmp_path):
    """The failure: with native activation the prompt hook answers on its own path and never delivers."""
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    _stop(tmp_path, env, "--activation-mode", "mcp")
    event = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt": PROMPT}

    out = _run(
        "exomem_retrieve_nudge.py",
        event,
        env,
        "--activation-mode",
        "mcp",
        "--refresh-event",
        "UserPromptSubmit",
    )

    assert MARKER in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert not _pending(tmp_path)


def test_a_pending_reminder_older_than_six_hours_is_dropped_unread(tmp_path):
    """The failure: an old reminder describes a turn the agent no longer has in mind."""
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    _stop(tmp_path, env)
    stale = time.time() - SIX_HOURS - 60
    [marker] = _pending(tmp_path)
    os.utime(marker, (stale, stale))

    assert _prompt(env, "yes") == ""
    assert not _pending(tmp_path)


def test_the_episode_ask_still_blocks_in_next_turn_mode(tmp_path):
    """The failure: the episode ask is deferred along with the per-turn reminder."""
    env = _env(
        tmp_path,
        EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn",
        EXOMEM_EPISODE_ASK_TURNS="1",
        EXOMEM_EPISODE_ASK_COOLDOWN_SEC="0",
    )

    reply = json.loads(_stop(tmp_path, env, reply="We settled the finish. " + "x" * 400))

    assert reply["decision"] == "block"
    assert EPISODE_MARKER in reply["reason"]
    assert not _pending(tmp_path)


def test_an_unknown_delivery_value_keeps_the_stop_blocking(tmp_path):
    """The failure: a typo in the env var silently switches mode, or the config key wins over it."""
    (tmp_path / "config.json").write_text(
        json.dumps({"capture_nudge_delivery": "next-turn"}), encoding="utf-8"
    )
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next_turn")

    reply = json.loads(_stop(tmp_path, env))

    assert reply["decision"] == "block"
    assert MARKER in reply["reason"]
    assert not _pending(tmp_path)


def test_a_reminder_that_cannot_be_deferred_blocks_the_stop(tmp_path):
    """The failure: an unwritable state file drops the reminder without a word."""
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    nudge_cache = tmp_path / "state" / ".cache" / "exomem-nudge"
    nudge_cache.parent.mkdir(parents=True)
    nudge_cache.write_text("", encoding="utf-8")  # a file where the marker's directory must go

    reply = json.loads(_stop(tmp_path, env))

    assert reply["decision"] == "block"
    assert MARKER in reply["reason"]


def test_codex_defers_and_delivers_under_its_own_home(tmp_path):
    """The failure: the Stop and prompt hooks disagree on the state directory for Codex."""
    codex_home = tmp_path / "codex-home"
    env = _env(
        tmp_path,
        EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn",
        EXOMEM_PROMINENCE="maximal",
        CODEX_HOME=str(codex_home),
    )
    del env["EXOMEM_HOOK_HOME"], env["EXOMEM_HOOK_CLIENT"]
    # No transcript: Codex hands the final text over directly, and `maximal` gates on its length.
    stop = {"session_id": SESSION, "last_assistant_message": "Done. " + "x" * 200}

    assert _run("exomem_capture_nudge.py", stop, env, "--client", "codex") == ""
    assert list((codex_home / ".cache" / "exomem-nudge").glob("pending_*"))

    assert MARKER in _prompt(env, "yes", "--client", "codex")


def test_sessions_whose_ids_sanitise_alike_keep_their_own_reminders(tmp_path):
    """The failure: a sanitised or truncated id joins two sessions, and one takes the other's reminder."""
    env = _env(tmp_path, EXOMEM_CAPTURE_NUDGE_DELIVERY="next-turn")
    _stop(tmp_path, env, session="session/alpha")

    assert _prompt(env, "yes", session="session_alpha") == ""
    assert MARKER in _prompt(env, "yes", session="session/alpha")
