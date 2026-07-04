#!/usr/bin/env python3
"""Stop hook: nudge a Knowledge Base capture when a turn looks like a landing.

The KB skill already says to auto-capture at stepping-stones, but skill prose is
*passive* — over a long thread the model forgets to check, so "auto-save" quietly
never fires. This hook re-arms the check: when Claude finishes a substantial turn
that hasn't already written to the KB, it blocks the stop with a one-line reminder
so Claude evaluates a capture before ending.

LANGUAGE-AGNOSTIC by design. It does NOT gate on English keywords — that would
miss Japanese and every other language. The gate is structural: a turn is a
candidate if the assistant's reply is substantial (>= a char threshold) and the
KB wasn't already written this turn. A per-session cooldown bounds how often it
can fire, so cost stays low while Claude — which judges "is this really a
stepping-stone?" well in any language — makes the actual call (the reminder tells
it to do nothing if it isn't one).

Cheap and safe: the script itself is free (stdlib only); the only token cost is a
real capture (the feature). Self-disarms via `stop_hook_active` (no loops); the
cooldown caps frequency; every trigger is logged to ~/.claude/exomem-capture-nudge.log
for tuning.

Tunables (env): EXOMEM_CAPTURE_NUDGE_DISABLE=1 (off), EXOMEM_CAPTURE_NUDGE_MIN_CHARS
(default 300 — lower it for a dense script like Japanese, which packs more meaning
per char), EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC (default 300). The legacy KB_CAPTURE_NUDGE_*
names are still accepted for back-compat (aliased to the EXOMEM_* names at startup).

Contract (Claude Code Stop hook): read the event JSON on stdin; print
`{"decision":"block","reason":...}` and exit 0 to block the stop and feed the
reminder to Claude; exit 0 with no output to allow the stop. Never raises — a hook
crash must not break the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# KB write tools — if an Exomem write tool ran this turn (e.g. mcp__…_Exomem__note/add;
# legacy `knowledge_base` names still matched for back-compat), the capture already happened.
_KB_WRITE = re.compile(r"(?:exomem|knowledge[_-]?base).*(note|add|edit|append|create_file|replace)", re.I)

REMINDER = (
    "[Exomem capture check] This turn did substantial work. If your Exomem knowledge-base "
    "skill is available and the turn reached a durable conclusion — a decision, a "
    "solved problem, a diagnosed failure, or a recognized pattern, in whatever "
    "language you were working in — capture it now as a *compiled note* (the "
    "distilled conclusion plus links, NOT a transcript) under the standing waiver, "
    "then report one line (Saved -> path). Ask only if the note's type/scope is "
    "genuinely ambiguous. If it is NOT a stepping-stone, or no Knowledge Base is "
    "configured here, do nothing and stop — that is the common case, and skipping "
    "is correct and free."
)


# Back-compat: the tunables were renamed KB_CAPTURE_NUDGE_* -> EXOMEM_CAPTURE_NUDGE_*
# with the knowledge-base -> exomem rename. The OLD names still work — normalized to
# the new names at startup so the rest of the hook reads only EXOMEM_* everywhere.
_ENV_ALIASES = (
    ("EXOMEM_CAPTURE_NUDGE_DISABLE", "KB_CAPTURE_NUDGE_DISABLE"),
    ("EXOMEM_CAPTURE_NUDGE_MIN_CHARS", "KB_CAPTURE_NUDGE_MIN_CHARS"),
    ("EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC", "KB_CAPTURE_NUDGE_COOLDOWN_SEC"),
)


def _normalize_env_aliases() -> None:
    """Map any legacy KB_* tunable onto its EXOMEM_* name (new wins if both set)."""
    for new, old in _ENV_ALIASES:
        if new not in os.environ and old in os.environ:
            os.environ[new] = os.environ[old]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def _content_blocks(msg: dict) -> list[dict]:
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        out: list[dict] = []
        for b in c:
            if isinstance(b, dict):
                out.append(b)
            elif isinstance(b, str):
                out.append({"type": "text", "text": b})
        return out
    return []


def _latest_turn(path: str, max_bytes: int = 262_144) -> tuple[str, list[str]]:
    """Return (assistant_text, tool_names) for the latest turn from the JSONL
    transcript. Walks backward, stopping at the human message that began the turn
    (a user message with real text and no tool_result block)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop a partial first line
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return "", []
    chunks: list[str] = []
    tools: list[str] = []
    for line in reversed([ln for ln in raw.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        role = msg.get("role") if isinstance(msg, dict) else None
        typ = obj.get("type")
        if role == "assistant" or typ == "assistant":
            for b in _content_blocks(msg):
                if b.get("type") == "text":
                    chunks.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tools.append(b.get("name", ""))
        elif role == "user" or typ == "user":
            blocks = _content_blocks(msg)
            if any(b.get("type") == "text" for b in blocks) and not any(
                b.get("type") == "tool_result" for b in blocks
            ):
                break  # reached the human prompt that began this turn
    return "".join(reversed(chunks)), tools


def _cooldown_ok(session_id: str, cooldown: int) -> tuple[bool, Path]:
    """Per-session timestamp file (mtime-based, so we never parse content)."""
    state_dir = Path.home() / ".claude" / ".cache" / "exomem-nudge"
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")[:128]
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


def _log(text: str) -> None:
    try:
        logp = Path.home() / ".claude" / "exomem-capture-nudge.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        snippet = re.sub(r"\s+", " ", text)[-160:]
        with open(logp, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} nudge fired | {snippet}\n")
    except Exception:
        pass


def main() -> int:
    _normalize_env_aliases()
    if os.environ.get("EXOMEM_CAPTURE_NUDGE_DISABLE"):
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    if data.get("stop_hook_active"):  # we already blocked once — stand down
        return 0
    tpath = data.get("transcript_path")
    if not tpath:
        return 0

    min_chars = _env_int("EXOMEM_CAPTURE_NUDGE_MIN_CHARS", 300)
    cooldown = _env_int("EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC", 300)

    assistant_text, tools = _latest_turn(tpath)
    if any(_KB_WRITE.search(t) for t in tools):  # already captured this turn
        return 0
    if re.search(r"Saved\s*(?:->|→|:)", assistant_text):
        return 0
    if len(assistant_text.strip()) < min_chars:  # trivial turn, not a landing
        return 0

    ok, stamp = _cooldown_ok(data.get("session_id", ""), cooldown)
    if not ok:  # fired recently this session — keep cost bounded
        return 0

    _touch(stamp)
    _log(assistant_text)
    print(json.dumps({"decision": "block", "reason": REMINDER}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
