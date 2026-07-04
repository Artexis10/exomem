#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge a KB retrieval before Claude answers.

The read-side mirror of the capture hook. The skill says to consult the KB
proactively, but that prose is passive — Claude forgets, especially at the start
of a thread. This re-arms the read side: when the user submits a substantial
prompt, it injects a one-line reminder to run `find` first and fold prior
conclusions into the answer, so the KB actually functions as the source of truth.

Language-agnostic and cheap: gates on prompt length + a per-session cooldown, no
keywords. By default it injects only a *reminder* — Claude still runs the real
(semantic) find — so it never stalls the prompt. (UserPromptSubmit blocks model
start until the hook returns, so the hook must be fast: stdlib only, no search
here by default.)

Opt-in **inject mode** (`KB_RETRIEVE_INJECT`) upgrades the reminder to real
retrieved content: on the same gated prompt, it fetches the top compact routing
stubs (`find(detail="compact")`, keyword mode only — no embeddings, no GPU) via a
short transport ladder — REST first (`EXOMEM_REST_API_KEY` in this env), then an
opt-in CLI fallback (`KB_RETRIEVE_INJECT_CLI`) — and appends them to the
reminder. Any transport failure (or the flag being off) falls straight through to
the reminder-only floor; the hook never blocks or raises past that point.

Tunables (env): KB_RETRIEVE_NUDGE_DISABLE=1 (off), KB_RETRIEVE_NUDGE_MIN_CHARS
(default 20 — short, since prompts are short and a dense script like Japanese
packs more per char), KB_RETRIEVE_NUDGE_COOLDOWN_SEC (default 300),
KB_RETRIEVE_INJECT (opt-in, default off — truthy-parsed: unset/""/"0"/"false"/
"no"/"off", any case, count as off) to turn on retrieve-and-inject, and
KB_RETRIEVE_INJECT_CLI (opt-in, same truthy parse) to additionally allow the
slower CLI transport when REST isn't configured or fails.

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
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REMINDER = (
    "[KB retrieval check] Before answering: if this prompt touches a topic your "
    "Exomem knowledge base might hold — a project, a past decision, a domain you've taken "
    "notes on, or a 'what did I conclude / have I looked at' question — run a quiet "
    "`find` FIRST and fold any hits into the answer (cite them). The KB is the "
    "source of truth for prior conclusions; a miss means 'not found in what I "
    "searched,' not 'doesn't exist.' If the prompt plainly has no KB bearing "
    "(chit-chat, or a fresh task with no prior notes), skip silently."
)

# Inject-mode routing-stub block: header + up to 3 `- path (type, updated)` lines,
# capped to keep the worst case (long titles/paths) small and predictable.
_STUB_HEADER = "KB routing stubs — verify with `get` before relying on these:"
_STUB_BLOCK_MAX_CHARS = 400

# Local mirror of extract.py::_env_flag's truthy-parse convention. This script
# deliberately never imports exomem (see module docstring), so the helper is
# duplicated rather than shared.
_FALSY_ENV = {"", "0", "false", "no", "off"}


def _env_flag(name: str) -> bool:
    """Truthy opt-in parse: unset, '', '0', 'false', 'no', 'off' (any case) → False.

    A bare presence/truthiness check would read `KB_RETRIEVE_INJECT=0` as opted in —
    the same bug class fixed elsewhere in this repo (extract.py::_env_flag).
    """
    return os.environ.get(name, "").strip().lower() not in _FALSY_ENV


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


# --- inject mode: transport ladder (REST -> CLI -> nudge-only floor) -------------


def _parse_hits(payload) -> list[dict] | None:
    """Extract a compact-hit list from a parsed JSON payload. Accepts either the
    shared {"success", "data"} envelope (what both REST and `--json` CLI actually
    print) or a bare list, defensively. `None` means "not usable" (caller falls
    through); never raises."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("success") is True and isinstance(payload.get("data"), list):
            return payload["data"]
        return None
    return None


def _fetch_via_rest(
    prompt: str, api_key: str, limit: int = 3, timeout: float = 2.0
) -> list[dict] | None:
    """One POST to the local REST facade's `/api/find` (keyword mode, compact
    detail). Returns the compact hit list, or `None` on ANY failure — connection
    error, timeout, non-200, malformed JSON, `success: false` — never raises."""
    host = os.environ.get("EXOMEM_HOST") or "127.0.0.1"
    url = f"http://{host}:8765/api/find"
    body = json.dumps(
        {"query": prompt, "detail": "compact", "limit": limit, "mode": "keyword"}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
        if status != 200:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return _parse_hits(payload)


def _fetch_via_cli(prompt: str, limit: int = 3, timeout: float = 5.0) -> list[dict] | None:
    """Locate the installed `exomem`/`kb` console script and run its `find`
    subcommand. Returns the compact hit list, or `None` on ANY failure — script
    not found, non-zero exit, malformed JSON, timeout — never raises. Never falls
    back to `sys.executable -m exomem`: this hook's interpreter is not assumed to
    have `exomem` importable."""
    script = shutil.which("exomem") or shutil.which("kb")
    if not script:
        return None
    try:
        proc = subprocess.run(
            [
                script, "find",
                "--detail", "compact",
                "--limit", str(limit),
                "--mode", "keyword",
                "--json", prompt,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout)
    except Exception:
        return None
    return _parse_hits(payload)


def _gather_hits(prompt: str) -> list[dict]:
    """Transport ladder decision: REST first (only when `EXOMEM_REST_API_KEY` is
    set), CLI only when REST wasn't attempted/failed AND `KB_RETRIEVE_INJECT_CLI`
    is truthy. Returns [] when neither rung is usable, or when the resolved
    transport itself reports zero hits (caller renders that as "nothing extra")."""
    api_key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if api_key:
        hits = _fetch_via_rest(prompt, api_key)
        if hits is not None:  # REST reachable (even with 0 hits) -> CLI never tried
            return hits
    if _env_flag("KB_RETRIEVE_INJECT_CLI"):
        hits = _fetch_via_cli(prompt)
        if hits is not None:
            return hits
    return []


def _format_inject_block(hits: list[dict]) -> str:
    """Render up to 3 compact hits as `- path (type, updated)` lines under a
    header, truncated to ~400 chars total. `""` for no hits — the caller must
    never inject an empty header or a "no results" placeholder. Only reads the
    `path`/`type`/`updated` compact-dict fields (never `excerpt`/`signals`)."""
    lines: list[str] = []
    for hit in hits[:3]:
        if not isinstance(hit, dict):
            continue
        path = hit.get("path")
        if not path:
            continue
        meta = ", ".join(str(v) for v in (hit.get("type"), hit.get("updated")) if v)
        lines.append(f"- {path} ({meta})" if meta else f"- {path}")
    if not lines:
        return ""
    block = "\n".join([_STUB_HEADER, *lines])
    if len(block) > _STUB_BLOCK_MAX_CHARS:
        block = block[: _STUB_BLOCK_MAX_CHARS - 1].rstrip() + "…"
    return block


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

    additional_context = REMINDER
    if _env_flag("KB_RETRIEVE_INJECT"):
        # Inject mode is a payload upgrade on this same gate, not a second
        # trigger — REST/CLI are only ever attempted past this point. Any
        # unanticipated failure here must still fall through to the
        # reminder-only floor, never raise past the hook.
        try:
            block = _format_inject_block(_gather_hits(prompt))
        except Exception:
            block = ""
        if block:
            additional_context = REMINDER + "\n\n" + block

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": additional_context,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
