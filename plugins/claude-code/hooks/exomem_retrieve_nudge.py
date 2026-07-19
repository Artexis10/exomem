#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge a KB retrieval before the agent answers.

The read-side mirror of the capture hook. The skill says to consult the KB
proactively, but that prose is passive — Claude forgets, especially at the start
of a thread. This re-arms the read side: when the user submits a substantial
prompt, it injects a one-line reminder to run `ask_memory` first and fold prior
conclusions into the answer, so the KB actually functions as the source of truth.

Cheap by default: gates on prompt length, an obvious-control-prompt filter, a
per-session cooldown, and a client-wide cooldown. By default it injects only a
*reminder* — Claude still runs the real recall only when the prompt actually
needs prior KB
context — so it never stalls the prompt. (UserPromptSubmit blocks model start
until the hook returns, so the hook must be fast: stdlib only, no search here by
default.)

Opt-in **inject mode** (`EXOMEM_RETRIEVE_INJECT`) upgrades the reminder to real
retrieved content: on the same gated prompt, it fetches the top compact routing
stubs (`ask_memory(detail="compact")`, keyword mode only — no embeddings, no GPU) via a
short transport ladder — REST first (`EXOMEM_REST_API_KEY` in this env), then an
opt-in CLI fallback (`EXOMEM_RETRIEVE_INJECT_CLI`) — and appends them to the
reminder. Any transport failure (or the flag being off) falls straight through to
the reminder-only floor; the hook never blocks or raises past that point.

Tunables (env): EXOMEM_RETRIEVE_NUDGE_DISABLE=1 (off),
EXOMEM_RETRIEVE_NUDGE_MIN_CHARS (default 20 — short, since prompts are short and
a dense script like Japanese packs more per char),
EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS (default 180 — only prompts at or below
this length are eligible for the obvious-control-prompt skip gate),
EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC (default 300),
EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC (default 900; set 0 to disable),
EXOMEM_RETRIEVE_INJECT (opt-in, default off — truthy-parsed:
unset/""/"0"/"false"/"no"/"off", any case, count as off) to turn on
retrieve-and-inject, and EXOMEM_RETRIEVE_INJECT_CLI (opt-in, same truthy parse)
to additionally allow the slower CLI transport when REST isn't configured or
fails. The legacy KB_RETRIEVE_* names (including KB_RETRIEVE_INJECT /
KB_RETRIEVE_INJECT_CLI) are still accepted for back-compat, aliased to the
EXOMEM_* names at startup.

Contract (Claude Code / Codex UserPromptSubmit hook): read the event JSON on
stdin (incl. `prompt`); on exit 0, print
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
    "[Exomem retrieval check] Before answering: if this prompt touches a topic your "
    "Exomem knowledge base might hold — a project, a past decision, a domain you've taken "
    "notes on, or a 'what did I conclude / have I looked at' question — run a quiet "
    "`ask_memory` only if recent conversation context does not already cover it, then fold "
    "any hits into the answer (cite them). Do not repeat a KB search just because this "
    "reminder appears again; reuse fresh KB context until the topic changes or the "
    "answer needs more evidence. The KB is the source of truth for prior conclusions; "
    "a miss means 'not found in what I searched,' not 'doesn't exist.' If the prompt "
    "plainly has no KB bearing (chit-chat, status/control messages, or a fresh task "
    "with no prior notes), skip silently."
)

# Inject-mode routing-stub block: header + up to 3 `- path (type, updated)` lines,
# capped to keep the worst case (long titles/paths) small and predictable.
_STUB_HEADER = "KB routing stubs — verify with `read_memory` before relying on these:"
_STUB_BLOCK_MAX_CHARS = 400

# Local mirror of extract.py::_env_flag's truthy-parse convention. This script
# deliberately never imports exomem (see module docstring), so the helper is
# duplicated rather than shared.
_FALSY_ENV = {"", "0", "false", "no", "off"}

_KB_BEARING_RE = re.compile(
    r"\b("
    r"kb|knowledge\s+base|exomem|note|notes|remember|save|capture|"
    r"conclud(?:e|ed|ion|ions)?|decision|decisions|"
    r"prior|previous|earlier|history|looked\s+at|have\s+i|did\s+i"
    r")\b",
    re.IGNORECASE,
)

_CONTROL_PROMPT_RE = re.compile(
    r"""
    ^\s*
    (?:
        y(?:es|ep|eah)?|ok(?:ay)?|no(?:pe)?|thanks?|thank\s+you|thx|
        good\s+job|gj|perfect|cool|nice|great|done|
        perfect\s+merge(?:\s+\w+){0,8}|
        (?:cool|ok(?:ay)?|nice|great)\s+(?:did|are|is|merge|continue|go)\b.*|
        so\s+everything\s+done\??|
        continue|carry\s+on|go\s+on|go\s+ahead|proceed|do\s+it|do\s+that|
        merge(?:\s+(?:it|then|to|into|main))*|ship\s+it|
        open(?:\s+(?:the\s+)?)?pr|put(?:\s+it)?\s+to\s+pr|
        restart(?:\s+the)?\s+server|cut(?:\s+the)?\s+release|
        status|stuck|are\s+you\s+done|done\s+yet|why\s+is\s+it\s+taking
    )
    [\s\.,!?:;\-]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _env_flag(name: str) -> bool:
    """Truthy opt-in parse: unset, '', '0', 'false', 'no', 'off' (any case) → False.

    A bare presence/truthiness check would read `EXOMEM_RETRIEVE_INJECT=0` as opted in —
    the same bug class fixed elsewhere in this repo (extract.py::_env_flag).
    """
    return os.environ.get(name, "").strip().lower() not in _FALSY_ENV


# Back-compat: the tunables were renamed KB_RETRIEVE_* -> EXOMEM_RETRIEVE_* with the
# knowledge-base -> exomem rename. The OLD names still work — normalized to the new
# names at startup so the rest of the hook reads only EXOMEM_* everywhere.
_ENV_ALIASES = (
    ("EXOMEM_RETRIEVE_NUDGE_DISABLE", "KB_RETRIEVE_NUDGE_DISABLE"),
    ("EXOMEM_RETRIEVE_NUDGE_MIN_CHARS", "KB_RETRIEVE_NUDGE_MIN_CHARS"),
    ("EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS", "KB_RETRIEVE_NUDGE_CONTROL_MAX_CHARS"),
    ("EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC", "KB_RETRIEVE_NUDGE_COOLDOWN_SEC"),
    ("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", "KB_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC"),
    ("EXOMEM_RETRIEVE_INJECT", "KB_RETRIEVE_INJECT"),
    ("EXOMEM_RETRIEVE_INJECT_CLI", "KB_RETRIEVE_INJECT_CLI"),
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


def _hook_client() -> str:
    explicit = os.environ.get("EXOMEM_HOOK_CLIENT", "").strip().lower()
    if explicit in {"claude", "codex"}:
        return explicit
    try:
        parts = {p.lower() for p in Path(__file__).resolve().parts}
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        parts = set()
    if ".codex" in parts:
        return "codex"
    return "claude"


def _hook_home() -> Path:
    explicit = os.environ.get("EXOMEM_HOOK_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / (".codex" if _hook_client() == "codex" else ".claude")


def _prompt(data: dict) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "input"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _is_obvious_control_prompt(prompt: str, max_chars: int) -> bool:
    """Skip short command/ack/status prompts that do not need prior KB context.

    This is deliberately a narrow English-only fast path. Non-English prompts
    and substantive English prompts still flow through the length+cooldown gate,
    preserving the hook's language-agnostic default for real questions while
    suppressing the common churn cases ("continue", "merge it", "done?").
    """
    text = re.sub(r"\s+", " ", prompt).strip()
    if not text or len(text) > max_chars:
        return False
    if _KB_BEARING_RE.search(text):
        return False
    return bool(_CONTROL_PROMPT_RE.match(text))


def _cooldown_stamp_ok(key: str, cooldown: int) -> tuple[bool, Path]:
    """Timestamp-file cooldown (mtime-based)."""
    state_dir = _hook_home() / ".cache" / "exomem-nudge"
    stamp = state_dir / key
    if cooldown <= 0:
        return True, stamp
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < cooldown:
            return False, stamp
    except OSError:
        pass
    return True, stamp


def _cooldown_ok(session_id: str, cooldown: int) -> tuple[bool, Path]:
    """Per-session timestamp file. Namespaced so it never collides with the
    capture hook's cooldown stamp for the same session."""
    key = "retrieve_" + re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")[:120]
    return _cooldown_stamp_ok(key, cooldown)


def _global_cooldown_ok(cooldown: int) -> tuple[bool, Path]:
    """Client-wide retrieval nudge cooldown. This keeps multi-tab Codex/Claude
    setups from seeing the same reminder in every fresh session."""
    return _cooldown_stamp_ok("retrieve_global", cooldown)


def _touch(stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding="utf-8")
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        pass


def _log(prompt: str) -> None:
    try:
        logp = _hook_home() / "exomem-retrieve-nudge.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        snippet = re.sub(r"\s+", " ", prompt)[:160]
        with open(logp, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} nudge fired | {snippet}\n")
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
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
    """One POST to the local REST facade's `/api/ask_memory` (keyword mode, compact
    detail). Returns the compact hit list, or `None` on ANY failure — connection
    error, timeout, non-200, malformed JSON, `success: false` — never raises."""
    host = os.environ.get("EXOMEM_HOST") or "127.0.0.1"
    url = f"http://{host}:8765/api/ask_memory"
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
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        return None
    return _parse_hits(payload)


def _fetch_via_cli(prompt: str, limit: int = 3, timeout: float = 5.0) -> list[dict] | None:
    """Locate the installed `exomem`/`kb` console script and run its `ask_memory`
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
                script, "ask_memory",
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
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        return None
    return _parse_hits(payload)


def _gather_hits(prompt: str) -> list[dict]:
    """Transport ladder decision: REST first (only when `EXOMEM_REST_API_KEY` is
    set), CLI only when REST wasn't attempted/failed AND `EXOMEM_RETRIEVE_INJECT_CLI`
    is truthy. Returns [] when neither rung is usable, or when the resolved
    transport itself reports zero hits (caller renders that as "nothing extra")."""
    api_key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if api_key:
        hits = _fetch_via_rest(prompt, api_key)
        if hits is not None:  # REST reachable (even with 0 hits) -> CLI never tried
            return hits
    if _env_flag("EXOMEM_RETRIEVE_INJECT_CLI"):
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
    _normalize_env_aliases()
    if os.environ.get("EXOMEM_RETRIEVE_NUDGE_DISABLE"):
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        return 0

    prompt = _prompt(data)
    if not prompt:
        return 0

    min_chars = _env_int("EXOMEM_RETRIEVE_NUDGE_MIN_CHARS", 20)
    control_max_chars = _env_int("EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS", 180)
    cooldown = _env_int("EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC", 300)
    global_cooldown = _env_int("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", 900)

    if len(prompt.strip()) < min_chars:  # trivial prompt ("yes", "go", "thanks")
        return 0

    if _is_obvious_control_prompt(prompt, control_max_chars):
        return 0

    ok, stamp = _cooldown_ok(str(data.get("session_id") or data.get("sessionId") or ""), cooldown)
    if not ok:  # already nudged recently this session — keep it quiet
        return 0

    global_ok, global_stamp = _global_cooldown_ok(global_cooldown)
    if not global_ok:  # another tab/session already got the reminder recently
        return 0

    _touch(stamp)
    if global_cooldown > 0:
        _touch(global_stamp)
    _log(prompt)

    additional_context = REMINDER
    if _env_flag("EXOMEM_RETRIEVE_INJECT"):
        # Inject mode is a payload upgrade on this same gate, not a second
        # trigger — REST/CLI are only ever attempted past this point. Any
        # unanticipated failure here must still fall through to the
        # reminder-only floor, never raise past the hook.
        try:
            block = _format_inject_block(_gather_hits(prompt))
        except Exception:  # noqa: BLE001 - hook must never break prompt submission
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
