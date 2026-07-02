#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge a KB retrieval before Claude answers.

The read-side mirror of the capture hook. The skill says to consult the KB
proactively, but that prose is passive — Claude forgets, especially at the start
of a thread. This re-arms the read side: when the user submits a substantial
prompt, it injects a one-line reminder to run `find` first and fold prior
conclusions into the answer, so the KB actually functions as the source of truth.

Language-agnostic and cheap: gates on prompt length + a per-session cooldown, no
keywords. It injects only a *reminder* — Claude still runs the real (semantic)
find — so it never stalls the prompt. (UserPromptSubmit blocks model start until
the hook returns, so the hook must be fast: stdlib only, no search here.)

Tunables (env): KB_RETRIEVE_NUDGE_DISABLE=1 (off), KB_RETRIEVE_NUDGE_MIN_CHARS
(default 20 — short, since prompts are short and a dense script like Japanese
packs more per char), KB_RETRIEVE_NUDGE_COOLDOWN_SEC (default 300).

Contract (Claude Code UserPromptSubmit hook): read the event JSON on stdin (incl.
`prompt`); on exit 0, print
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}`
to add the reminder to context; print nothing to stay silent. Never raises — a
hook crash must not break the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REMINDER = (
    "[KB retrieval check] Before answering: if this prompt touches a topic the "
    "Knowledge Base might hold — a project, a past decision, a domain you've taken "
    "notes on, or a 'what did I conclude / have I looked at' question — run a quiet "
    "`find` FIRST and fold any hits into the answer (cite them). The KB is the "
    "source of truth for prior conclusions; a miss means 'not found in what I "
    "searched,' not 'doesn't exist.' If the prompt plainly has no KB bearing "
    "(chit-chat, or a fresh task with no prior notes), skip silently."
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def _cooldown_ok(session_id: str, cooldown: int) -> tuple[bool, Path]:
    """Per-session timestamp file (mtime-based). Namespaced so it never collides
    with the capture hook's cooldown stamp for the same session."""
    state_dir = Path.home() / ".claude" / ".cache" / "kb-nudge"
    key = "retrieve_" + re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")[:120]
    stamp = state_dir / key
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < cooldown:
            return False, stamp
    except OSError:
        pass
    return True, stamp


def _touch(stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _log(prompt: str) -> None:
    try:
        logp = Path.home() / ".claude" / "kb-retrieve-nudge.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        snippet = re.sub(r"\s+", " ", prompt)[:160]
        with open(logp, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} nudge fired | {snippet}\n")
    except Exception:
        pass


def main() -> int:
    if os.environ.get("KB_RETRIEVE_NUDGE_DISABLE"):
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return 0

    min_chars = _env_int("KB_RETRIEVE_NUDGE_MIN_CHARS", 20)
    cooldown = _env_int("KB_RETRIEVE_NUDGE_COOLDOWN_SEC", 300)

    if len(prompt.strip()) < min_chars:  # trivial prompt ("yes", "go", "thanks")
        return 0

    ok, stamp = _cooldown_ok(data.get("session_id", ""), cooldown)
    if not ok:  # already nudged recently this session — keep it quiet
        return 0

    _touch(stamp)
    _log(prompt)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": REMINDER,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
