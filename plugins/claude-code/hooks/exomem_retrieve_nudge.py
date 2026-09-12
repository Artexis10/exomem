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
stubs (`ask_memory(detail="compact", mode="hybrid")`) via a short transport
ladder — REST first (`EXOMEM_REST_API_KEY` in this env, else read from the
managed install's `service.env`), then an opt-in CLI fallback
(`EXOMEM_RETRIEVE_INJECT_CLI`) — and appends them to the reminder. Any transport
failure (or the flag being off) falls straight through to the reminder-only
floor; the hook never blocks or raises past that point.

Hybrid, not keyword: keyword mode is an all-tokens-present gate over the raw
whitespace-split query, so a real prompt — a pasted ticket, a sentence with a
colon or a comma, a harness notification — never passes it, and the stub block
was silently empty on every substantive prompt (measured 2026-09-11: 0 hits for
a 3 KB ticket prompt and for "deploy notes: rollback"; 3 relevant hits in
hybrid mode for the same prompt, ~1.5 s over REST, ~2.4 s via the CLI). The log
line names the lane that answered and the hit count so that fall-through is
visible instead of indistinguishable from success.

A key this hook lifts out of the managed install's `service.env` travels only
to loopback: an `EXOMEM_HOST` that points anywhere else disables the REST rung
for a file-sourced key (a key the user exported into the environment keeps
today's behaviour, since they placed both). The stub block is cut by whole
lines, never inside a path. The two rungs share one wall-clock budget under the
registered hook timeout: the REST rung is joined against the remaining budget
and the CLI subprocess gets the remainder as its timeout, so a slow or
dribbling server cannot burn the reminder.

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
import threading
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
# Whole lines only: three readable-filename stubs run to ~140 chars each, and a
# path cut in the middle is a fabricated path presented as a retrieved one.
_STUB_BLOCK_MAX_CHARS = 600
_STUB_OMITTED_LINE = "- … {n} more not shown"

# Recall mode for the inject lane. See the module docstring for why this is not
# "keyword".
INJECT_MODE = "hybrid"
# Hybrid recall embeds the prompt server-side; the old 2 s keyword budget was
# measured too tight for it (1.5 s warm on a laptop CPU lane).
REST_TIMEOUT_SECONDS = 4.0
CLI_TIMEOUT_SECONDS = 5.0
# One wall-clock budget shared by both rungs, under the 10 s hook timeout that
# `install-hook` registers, leaving room for the wrapper and interpreter start.
# A rung that would start with less than `_MIN_RUNG_SECONDS` left is skipped.
INJECT_BUDGET_SECONDS = 8.0
_MIN_RUNG_SECONDS = 0.5
# A key read from disk is only ever sent to the local service.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

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


# Prominence presets. This hook is deployed as a STANDALONE copy into the client's
# hook directory, so it cannot import `exomem.prominence` — the table is duplicated
# here deliberately, and `tests/test_prominence.py` asserts the two stay identical.
# Values are (min_chars, control_max_chars, cooldown_sec, global_cooldown_sec), or
# None to disable. `control_max_chars` is not level-dependent: a short control prompt
# ("ok", "continue") is never a retrieval trigger at any level.
_PROMINENCE_PRESETS = {
    "off": None,
    "light": (80, 180, 900, 1800),
    "balanced": (20, 180, 300, 900),
    "maximal": (0, 180, 0, 0),
}
_PROMINENCE_ALIASES = {
    "none": "off",
    "silent": "off",
    "disabled": "off",
    "minimal": "light",
    "low": "light",
    "quiet": "light",
    "default": "balanced",
    "medium": "balanced",
    "normal": "balanced",
    "high": "maximal",
    "max": "maximal",
    "full": "maximal",
    "aggressive": "maximal",
}


def _config_path() -> Path:
    """Mirror of `exomem.mode.config_path` — same file the CLI writes."""
    override = os.environ.get("EXOMEM_CONFIG_PATH")
    if override:
        return Path(override)
    if os.name == "nt":
        base = (
            os.environ.get("PROGRAMDATA")
            or os.environ.get("ALLUSERSPROFILE")
            or "C:" + r"\ProgramData"
        )
        return Path(base) / "exomem" / "config.json"
    return Path.home() / ".exomem" / "config.json"


def _prominence() -> str:
    """Active level: `EXOMEM_PROMINENCE` env -> config file -> balanced.

    Defaults to `balanced` rather than auto-detecting a surface: if this hook is
    running at all, the client supports hooks, which is exactly the case that wants
    the hook-backed default.
    """

    def _canon(value):
        if not value or not isinstance(value, str):
            return None
        v = value.strip().lower()
        if v in _PROMINENCE_PRESETS:
            return v
        return _PROMINENCE_ALIASES.get(v)

    from_env = _canon(os.environ.get("EXOMEM_PROMINENCE"))
    if from_env:
        return from_env
    try:
        data = json.loads(_config_path().read_text("utf-8"))
        from_config = _canon(data.get("prominence")) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — missing/corrupt config must degrade to default
        from_config = None
    return from_config or "balanced"


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


def _log(prompt: str, lane: str = "off", hits: int = 0) -> None:
    """One line per fired nudge: which inject lane answered ("rest" / "cli",
    "none" for the reminder-only floor, "off" when inject mode is not on) and
    how many stubs it returned, then the prompt head. Never the key."""
    try:
        logp = _hook_home() / "exomem-retrieve-nudge.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        snippet = re.sub(r"\s+", " ", prompt)[:160]
        with open(logp, "a", encoding="utf-8") as f:
            f.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} nudge fired | "
                f"lane={lane} hits={hits} | {snippet}\n"
            )
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
        if payload.get("success") is not True:
            return None
        data = payload.get("data")
        if isinstance(data, list):
            return data
        # The REST facade wraps the hits in a dict whenever it attaches a marker
        # (`degraded`, `warming`, timings, a continuation, ...); otherwise `data`
        # is the bare list. Either shape may arrive in any mode, so both are read.
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return data["hits"]
        return None
    return None


def _fetch_via_rest(
    prompt: str, api_key: str, limit: int = 3, timeout: float = REST_TIMEOUT_SECONDS
) -> list[dict] | None:
    """One POST to the local REST facade's `/api/ask_memory` (hybrid mode, compact
    detail). Returns the compact hit list, or `None` on ANY failure — connection
    error, timeout, non-200, malformed JSON, `success: false` — never raises."""
    host = _rest_host()
    url = f"http://{host}:8765/api/ask_memory"
    body = json.dumps(
        {"query": prompt, "detail": "compact", "limit": limit, "mode": INJECT_MODE}
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


def _rest_host() -> str:
    return (os.environ.get("EXOMEM_HOST") or "").strip() or "127.0.0.1"


def _fetch_via_cli(
    prompt: str, limit: int = 3, timeout: float = CLI_TIMEOUT_SECONDS
) -> list[dict] | None:
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
                "--mode", INJECT_MODE,
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


def _service_env_path() -> Path | None:
    """The managed install's service EnvironmentFile, where `install-service.sh`
    persists `EXOMEM_REST_API_KEY` (mirrors its CONFIG_ROOT per platform).
    `EXOMEM_SERVICE_ENV` overrides the location; Windows has no service env."""
    explicit = os.environ.get("EXOMEM_SERVICE_ENV", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Exomem" / "service.env"
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "exomem" / "service.env"


def _resolve_rest_key() -> tuple[str, str]:
    """`(key, source)`: the key from this env (`source="env"`), else from the
    managed install's `service.env` (`source="file"`, #1142), else `("", "")`.
    On a managed install the key lives only in the service's EnvironmentFile,
    so the client shell never carries it and the REST rung never ran.

    The file read mirrors `scripts/_service-common.sh`'s
    `exomem_dotenv_file_value` — first `NAME=` line, one layer of matching
    quotes stripped — and additionally reverses the `systemd_quote` escaping
    `install-service.sh` writes inside double quotes (`\\` and `\"`). Never
    raises; the value is never logged."""
    from_env = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if from_env:
        return from_env, "env"
    path = _service_env_path()
    if path is None:
        return "", ""
    try:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        return "", ""
    for line in text.splitlines():
        match = re.match(r"^\s*EXOMEM_REST_API_KEY\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        value = value.strip()
        return (value, "file") if value else ("", "")
    return "", ""


def _rest_api_key() -> str:
    """The resolved key alone; see `_resolve_rest_key`."""
    return _resolve_rest_key()[0]


def _bounded(call, budget: float):
    """Run `call()` on a daemon thread and wait at most `budget` seconds for it.
    Returns its result, or `None` when it has not finished in time (the thread
    is left to end with the process; the hook exits right after printing)."""
    box: list = []

    def _run() -> None:
        try:
            box.append(call())
        except Exception:  # noqa: BLE001 - hook must never break prompt submission
            box.append(None)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(max(0.0, budget))
    return box[0] if box else None


def _gather_hits_with_lane(prompt: str) -> tuple[list[dict], str]:
    """Transport ladder decision, plus the name of the rung that answered:
    "rest", "cli", or "none" when the ladder fell through to the reminder-only
    floor. REST runs first when a key resolves; CLI only when REST wasn't
    attempted or failed AND `EXOMEM_RETRIEVE_INJECT_CLI` is truthy. A resolved
    transport that reports zero hits is still the answer (rendered as "nothing
    extra"), so CLI is never a second opinion on REST.

    A key read from `service.env` is bound to loopback: the hook must not become
    the primitive that posts a key it lifted off disk, plus the prompt, to a host
    named by `EXOMEM_HOST`. A key the user exported into the environment keeps
    today's behaviour — they placed both variables.

    Both rungs draw on one wall-clock budget (`INJECT_BUDGET_SECONDS`). The
    CLI rung's `subprocess.run(timeout=)` is a wall-clock bound already; the
    REST rung's `urlopen(timeout=)` is only a per-socket-operation timeout, so a
    server that dribbles a byte every few seconds would never trip it (measured
    52 s against an 8 s budget). The REST rung therefore runs on a daemon thread
    joined against the remaining budget: past the deadline the rung counts as
    failed and the ladder moves on, while the abandoned request dies with the
    hook process."""
    deadline = time.monotonic() + INJECT_BUDGET_SECONDS
    api_key, source = _resolve_rest_key()
    if api_key and (source == "env" or _rest_host() in _LOOPBACK_HOSTS):
        remaining = deadline - time.monotonic()
        if remaining >= _MIN_RUNG_SECONDS:
            hits = _bounded(
                lambda: _fetch_via_rest(prompt, api_key, timeout=min(REST_TIMEOUT_SECONDS, remaining)),
                remaining,
            )
            if hits is not None:  # REST reachable (even with 0 hits) -> CLI never tried
                return hits, "rest"
    if _env_flag("EXOMEM_RETRIEVE_INJECT_CLI"):
        remaining = deadline - time.monotonic()
        if remaining >= _MIN_RUNG_SECONDS:
            hits = _fetch_via_cli(prompt, timeout=min(CLI_TIMEOUT_SECONDS, remaining))
            if hits is not None:
                return hits, "cli"
    return [], "none"


def _format_inject_block(hits: list[dict]) -> str:
    """Render up to 3 compact hits as `- path (type, updated)` lines under a
    header, bounded to `_STUB_BLOCK_MAX_CHARS` by WHOLE lines: a line that would
    not fit is dropped and counted in a trailing `- … N more not shown` marker,
    never cut inside the path (a truncated path is a path that does not exist,
    presented as retrieved). `""` for no hits, and `""` when not even the first
    line fits — the caller must never inject an empty header or a "no results"
    placeholder. Only reads the `path`/`type`/`updated` compact-dict fields
    (never `excerpt`/`signals`)."""
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
    kept: list[str] = [_STUB_HEADER]
    used = len(_STUB_HEADER)
    omitted = 0
    for index, line in enumerate(lines):
        # Reserve room for the marker if anything after this line could be dropped.
        marker = _STUB_OMITTED_LINE.format(n=len(lines) - index) if index < len(lines) - 1 else ""
        reserve = len(marker) + 1 if marker else 0
        if used + 1 + len(line) + reserve > _STUB_BLOCK_MAX_CHARS:
            omitted = len(lines) - index
            break
        kept.append(line)
        used += 1 + len(line)
    if len(kept) == 1:
        return ""
    if omitted:
        kept.append(_STUB_OMITTED_LINE.format(n=omitted))
    return "\n".join(kept)


def main() -> int:
    _normalize_env_aliases()
    if os.environ.get("EXOMEM_RETRIEVE_NUDGE_DISABLE"):
        return 0
    preset = _PROMINENCE_PRESETS[_prominence()]
    if preset is None:  # prominence=off — the user asked for explicit invocation only
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 - hook must never break prompt submission
        return 0

    prompt = _prompt(data)
    if not prompt:
        return 0

    # Explicit env still wins; the prominence level only moves the default.
    min_chars = _env_int("EXOMEM_RETRIEVE_NUDGE_MIN_CHARS", preset[0])
    control_max_chars = _env_int("EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS", preset[1])
    cooldown = _env_int("EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC", preset[2])
    global_cooldown = _env_int("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", preset[3])

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

    additional_context = REMINDER
    lane, hit_count = "off", 0
    if _env_flag("EXOMEM_RETRIEVE_INJECT"):
        # Inject mode is a payload upgrade on this same gate, not a second
        # trigger — REST/CLI are only ever attempted past this point. Any
        # unanticipated failure here must still fall through to the
        # reminder-only floor, never raise past the hook.
        try:
            hits, lane = _gather_hits_with_lane(prompt)
            hit_count = len(hits)
            block = _format_inject_block(hits)
        except Exception:  # noqa: BLE001 - hook must never break prompt submission
            lane, hit_count, block = "none", 0, ""
        if block:
            additional_context = REMINDER + "\n\n" + block
    # Stamped after the transport ran: a hook the client kills mid-ladder must
    # not also burn the session's cooldown and the client-wide one.
    _touch(stamp)
    if global_cooldown > 0:
        _touch(global_stamp)
    _log(prompt, lane, hit_count)

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": additional_context,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
