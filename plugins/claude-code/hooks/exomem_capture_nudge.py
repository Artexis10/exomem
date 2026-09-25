#!/usr/bin/env python3
"""Stop hook: nudge a Knowledge Base capture when a turn looks like a landing.

The KB skill already says to auto-capture at stepping-stones, but skill prose is
*passive* — over a long thread the model forgets to check, so "auto-save" quietly
never fires. This hook re-arms the check: when an agent finishes a substantial turn
that hasn't already written to the KB, it blocks the stop with a one-line reminder
so the agent evaluates whether a capture is warranted before ending. A substantial
turn alone does not require a write.

LANGUAGE-AGNOSTIC by design. It does NOT gate on English keywords — that would
miss Japanese and every other language. The gate is structural: a turn is a
candidate if the assistant's reply is substantial (>= a char threshold) and the
KB wasn't already written this turn. A per-session cooldown bounds how often it
can fire, so cost stays low while the agent — which judges "is this really a
stepping-stone?" well in any language — makes the actual call (the reminder tells
it to do nothing if it isn't one).

Cheap and safe: the script itself is free (stdlib only), but reminder context also
consumes tokens. Self-disarms via `stop_hook_active` (no loops); the
cooldown caps frequency; every trigger is logged under the active client home for
tuning.

Tunables (env): EXOMEM_CAPTURE_NUDGE_DISABLE=1 (off), EXOMEM_CAPTURE_NUDGE_MIN_CHARS
(default 300 — lower it for a dense script like Japanese, which packs more meaning
per char), EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC (default 300). The legacy KB_CAPTURE_NUDGE_*
names are still accepted for back-compat (aliased to the EXOMEM_* names at startup).

Episode ask. Every K substantive turns (K and a cooldown by prominence,
`_EPISODE_ASK_PRESETS`; EXOMEM_EPISODE_ASK_TURNS / EXOMEM_EPISODE_ASK_COOLDOWN_SEC
override) the hook asks for one `episode_memory` record under a key derived from
the client and session id alone (`episode_key`), so the key survives compaction
without the hook reading a transcript record. Only a SUCCESSFUL record resets the
count: an unrelated write, a `Saved ->` marker or a failed record leaves the
episode pending. When the ask is due it takes that Stop; on every other Stop the
per-turn capture reminder behaves exactly as before. The hook never records
anything itself — hooks trigger, agents author.

Before an ask actually fires, the hook makes one read-only, bounded REST call —
`episode_memory(action="inspect", episode=<the derived key>)` against the local
door, using the same host/port/key resolution and bounded-timeout pattern the
retrieve hook's REST rung uses — and compares the revision count it reports
against the count last seen (`last_seen_revisions` in the per-session state
file). A higher count means a recap was recorded through some other door (a
client whose tool list predates `episode_memory`, the REST facade directly, a
door this hook doesn't otherwise watch), which the transcript-only
`_successful_episode_record` check can never see; the substantive-turn count
resets and the ask is skipped. Any failure of that call — the door
unconfigured, a timeout, any error, or the episode simply not found yet —
falls straight through to the transcript-only behaviour above.

Contract (Claude Code / Codex Stop hook): read the event JSON on stdin; print
`{"decision":"block","reason":...}` and exit 0 to block the stop and feed the
reminder to the agent; exit 0 with no output to allow the stop. Never raises — a
hook crash must not break the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# KB write tools — mixed tools include their operation selector so read-only
# discovery does not suppress the reminder. Legacy names remain recognized.
_KB_WRITE = re.compile(
    r"(?:exomem|knowledge[_-]?base).*(?:"
    r"note|add|edit|append|create_file|replace|remember|capture_source|"
    r"preserve_evidence|preserve_artifacts|manage_memory_file|"
    r"connect_memory:(?:create-entity|accept-relation)|"
    # Structured-collection lifecycle writes. A turn that filed the record or
    # moved the plan item did exactly what the capture contract asks for, and
    # was nudged anyway because the detector knew only page and entity writes.
    # Spelled out per action so `inspect`/`query`/`describe`/`validate` stay
    # read-only discovery that leaves the check armed.
    r"record_memory:(?:create|append|update|revise|rebaseline)|"
    r"plan_memory:(?:create|add|update|triage)|"
    r"observe_memory:(?:add|update|remove)|"
    r"episode_memory:record"
    r")",
    re.I,
)

REMINDER = (
    "[Exomem capture check] Reuse evidence; skip transient code/test/CI. "
    "Capture durable outcomes per live policy. "
    "Decompose before routing; open notes have no priority. Keep hypotheses attributed/uncertain. "
    "Check coverage once/episode. Stable preference/recurring routine/historical baseline/"
    "durable affiliation needs stability or recurrence plus reusable comparison/interpretation/decision value; "
    "fleeting/one-off/incidental/trivial/tentative events: quiet. At balanced/maximal, "
    "after primary work, before the final response, "
    'review_memory(mode="attention", categories=["entity_recurrence"], limit=3) once per session; '
    "no local scan; no model. Active agent uses active entity registry and selected knowledge packs: "
    'connect_memory(operation="resolve-entity"); stop on ambiguity; single incidental mention stays '
    "in context. Uniquely resolved Entity: narrow additive entity facet, else concise compiled "
    "observation/proactive_capture; "
    "affiliation relation/link_acceptance; compatible Records only. Hydrate: edit_memory first; else "
    'connect_memory(operation="create-entity") only for stable recurring identity beyond source. '
    "Entity creation/substantial curation: confirmed "
    "restructure_execution. Recheck on confirmed batch terminal receipt; closure-only eighth recheck. "
    "Distil; no transcripts. replace_memory supersedes contradicted "
    "conclusions, not corrections beside them. Stated intent -> "
    "Planning/plan_memory; observed outcome -> Records/record_memory. Generated draft stays "
    "ephemeral; selected is not write consent: proactive_capture keeps exact Source/Evidence bytes "
    "by role, not MIME. No handle: non-committing handoff; delivery needs Evidence receipt/Record; "
    "no remote byte inference. No schema: "
    "structural_suggestions/restructure_execution; relations: link_acceptance. Else/no "
    "Knowledge Base: stop."
)


#: The episode ask. Its own constant, so `REMINDER`'s bytes (and every pin on
#: them) stay exactly as they were. `{key}` is the session's episode key.
EPISODE_ASK = (
    "[Exomem episode check] Several substantive turns have passed since this "
    "session's last episode record. If the conversation reached a decision or a "
    'stopping point, call episode_memory once with action="record", '
    'episode="{key}", a one-line subject and summary, and short worked_on, decided '
    "and open items; add said only for a user statement worth keeping verbatim. "
    "Distil; no transcript. If nothing durable happened, do nothing."
)
#: The label the server derives the same key from (`episode_capture.hook_key`).
_EPISODE_CLIENT_LABELS = {"claude": "claude-code", "codex": "codex"}
_EPISODE_KEY_LABEL = "exomem-episode-key-v1"


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


# Prominence presets. This hook is deployed as a STANDALONE copy into the client's
# hook directory, so it cannot import `exomem.prominence` — the table is duplicated
# here deliberately, and `tests/test_prominence.py` asserts the two stay identical.
# Keys are the level; values are (min_chars, cooldown_sec) or None to disable.
_PROMINENCE_PRESETS = {
    "off": None,
    "light": (800, 900),
    "balanced": (300, 300),
    "maximal": (120, 60),
}
# Episode-ask cadence per level: (substantive turns since the last record,
# seconds between asks) or None. Duplicated from `exomem.prominence` for the
# same standalone reason; `tests/test_capture_nudge_episode.py` pins the two.
_EPISODE_ASK_PRESETS = {
    "off": None,
    "light": (12, 3600),
    "balanced": (6, 1200),
    "maximal": (3, 600),
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
    except Exception:  # noqa: BLE001 — path resolution must not break the hook
        parts = set()
    if ".codex" in parts:
        return "codex"
    return "claude"


def _hook_home() -> Path:
    explicit = os.environ.get("EXOMEM_HOOK_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / (".codex" if _hook_client() == "codex" else ".claude")


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
                if b.get("type") in {"input_text", "output_text"}:
                    out.append({**b, "type": "text"})
                else:
                    out.append(b)
            elif isinstance(b, str):
                out.append({"type": "text", "text": b})
        return out
    return []


def _codex_call_output_succeeded(output: object) -> bool:
    """Confirm a Codex function call completed without an MCP error.

    Codex 0.144.x stores connector output as a timing prelude followed by an
    ``Output:`` JSON object. There is no positive status field, so malformed or
    missing output stays unconfirmed and must not suppress the capture check.
    """
    if not isinstance(output, str):
        return False
    match = re.search(r"(?:^|\n)Output:\s*", output)
    if not match:
        return False
    try:
        result = json.loads(output[match.end() :].strip())
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(result, dict):
        return False
    if result.get("error") or result.get("error_code"):
        return False
    error_data = result.get("error_data")
    if isinstance(error_data, dict):
        if error_data.get("type") == "mcp_tool_execution_error":
            return False
        nested = error_data.get("result")
        if isinstance(nested, dict) and nested.get("isError") is True:
            return False
    return True


def _latest_turn(path: str, max_bytes: int = 262_144) -> tuple[str, list[dict]]:
    """Return (assistant_text, tool calls) for the latest turn from the JSONL
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
    tools: list[dict] = []
    failed_tool_ids: set[str] = set()
    completed_codex_tool_ids: set[str] = set()
    for line in reversed([ln for ln in raw.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 — malformed transcript rows are ignored
            continue
        payload = obj.get("payload")
        record = (
            payload
            if obj.get("type") == "response_item" and isinstance(payload, dict)
            else obj
        )
        msg = (
            record.get("message")
            if isinstance(record.get("message"), dict)
            else record
        )
        role = msg.get("role") if isinstance(msg, dict) else None
        typ = record.get("type") if isinstance(record, dict) else None
        if typ == "function_call_output":
            call_id = str(record.get("call_id") or "")
            if call_id and _codex_call_output_succeeded(record.get("output")):
                completed_codex_tool_ids.add(call_id)
            elif call_id:
                failed_tool_ids.add(call_id)
        elif typ == "function_call":
            arguments = record.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            namespace = str(record.get("namespace") or "")
            name = str(record.get("name") or "")
            operation = str(arguments.get("operation") or "")
            tools.append(
                {
                    "id": str(record.get("call_id") or ""),
                    "name": f"{namespace}{name}",
                    "operation": operation,
                    "input": arguments,
                    "requires_confirmed_output": True,
                }
            )
        elif role == "assistant" or typ in {"assistant", "agent_message"}:
            for b in _content_blocks(msg):
                if b.get("type") == "text":
                    chunks.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    name = str(b.get("name", ""))
                    tool_input = b.get("input")
                    operation = (
                        str(tool_input.get("operation", ""))
                        if isinstance(tool_input, dict)
                        else ""
                    )
                    tools.append(
                        {
                            "id": str(b.get("id") or ""),
                            "name": name,
                            "operation": operation,
                            "input": tool_input if isinstance(tool_input, dict) else {},
                        }
                    )
        elif role == "user" or typ == "user":
            blocks = _content_blocks(msg)
            for b in blocks:
                if b.get("type") == "tool_result" and b.get("is_error") is True:
                    failed_tool_ids.add(str(b.get("tool_use_id") or ""))
            if any(b.get("type") == "text" for b in blocks) and not any(
                b.get("type") == "tool_result" for b in blocks
            ):
                break  # reached the human prompt that began this turn
    for tool in tools:
        tool_id = tool["id"]
        tool["failed"] = bool(
            tool_id
            and (
                tool_id in failed_tool_ids
                or (
                    tool.get("requires_confirmed_output")
                    and tool_id not in completed_codex_tool_ids
                )
            )
        )
    return "".join(reversed(chunks)), tools


def _successful_kb_write(tool: dict) -> bool:
    """Whether one observed tool call completed a real KB mutation."""
    if tool.get("failed"):
        return False
    name = str(tool.get("name") or "")
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    # `operation` for the mixed tools that use it; `action` for the two
    # structured-collection commands, whose selector is spelled `action`. Without
    # the second name every Records and Planning call looked like a bare tool
    # name, so a mutation and a read were indistinguishable here.
    operation = str(tool.get("operation") or "") or str(tool_input.get("action") or "")
    selector = f"{name}:{operation}" if operation else name
    if not _KB_WRITE.search(selector):
        return False
    if "edit_memory" in name.lower() and tool_input.get("validate_only") is True:
        return False
    return True


#: Written by `install-hook`, cleared once an exomem MCP tool is observed. The
#: hooks take effect for the CURRENT session, but the MCP server they reference
#: is not loaded until the client restarts — so on a fresh install the very
#: first thing the product does is ask for `remember` / `edit_memory` /
#: `connect_memory`, none of which exist yet, and there is no CLI fallback for
#: the write side. The nudge's own escape hatch ("no Knowledge Base configured,
#: do nothing") cannot detect this case.
PENDING_RESTART_MARKER = "pending-restart"

#: Upper bound on the silence. The marker normally clears the moment an exomem
#: tool appears, but a user who installs and never calls one would otherwise be
#: muted forever — and the nudge is what prompts the first capture. After this,
#: assume the restart happened.
_PENDING_RESTART_MAX_AGE_SEC = 24 * 3600


def episode_key(client: str, session_id: str) -> str:
    """The session's episode key: a pure function of client and session id.

    Mirror of `exomem.episode_capture.hook_key`, which this standalone script
    cannot import.
    """
    material = f"{_EPISODE_KEY_LABEL}\0{client}\0{session_id}".encode("utf-8", "surrogatepass")
    return "ep-" + hashlib.sha256(material).hexdigest()[:32]


def _successful_episode_record(tool: dict) -> bool:
    """A completed `episode_memory` record — the only thing that covers an episode."""
    if tool.get("failed"):
        return False
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    return (
        bool(re.search(r"(?:exomem|knowledge[_-]?base).*episode_memory", str(tool.get("name") or ""), re.I))
        and str(tool_input.get("action") or "") == "record"
    )


def _episode_state_path(session_id: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:120]
    return _hook_home() / ".cache" / "exomem-nudge" / f"episode_{key}"


_EPISODE_STATE_DEFAULT = {
    "substantive_since_record": 0,
    "last_ask_ts": 0.0,
    "last_seen_revisions": 0,
}
#: `last_seen_revisions` after a record this hook saw itself: the door's count
#: now includes that record, so the next read re-bases instead of comparing.
_REVISIONS_UNKNOWN = -1


def _read_episode_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt state starts the count over
        return dict(_EPISODE_STATE_DEFAULT)
    if not isinstance(data, dict):
        return dict(_EPISODE_STATE_DEFAULT)
    try:
        return {
            "substantive_since_record": max(0, int(data.get("substantive_since_record") or 0)),
            "last_ask_ts": float(data.get("last_ask_ts") or 0.0),
            "last_seen_revisions": max(-1, int(data.get("last_seen_revisions") or 0)),
        }
    except (TypeError, ValueError):
        return dict(_EPISODE_STATE_DEFAULT)


def _write_episode_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except Exception:  # noqa: BLE001 — the counter is strictly best-effort
        pass


# --- episode door check: a revision recorded through any door still counts ---

#: A key read from `service.env` is bound to loopback, mirroring the retrieve
#: hook's own ladder: the hook must not become the primitive that posts a key
#: it lifted off disk, plus a derived episode key, to a host named by
#: `EXOMEM_HOST`. A key the user exported into the environment keeps today's
#: behaviour — they placed both variables.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: One bounded call, only on a Stop where the ask is otherwise about to fire —
#: this is the Stop hook's own time budget, spent alongside the transcript
#: work above it.
_EPISODE_DOOR_TIMEOUT_SECONDS = 2.0


def _rest_host() -> str:
    """Mirror of the retrieve hook's `_rest_host` — this standalone script
    cannot import it."""
    return (os.environ.get("EXOMEM_HOST") or "").strip() or "127.0.0.1"


def _rest_port() -> int | None:
    """Mirror of the retrieve hook's `_rest_port`."""
    value = os.environ.get("EXOMEM_REST_PORT", "").strip()
    if not value:
        return 8765
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _service_env_path() -> Path | None:
    """Mirror of the retrieve hook's `_service_env_path`: the managed
    install's service EnvironmentFile, where `install-service.sh` persists
    `EXOMEM_REST_API_KEY`. `EXOMEM_SERVICE_ENV` overrides the location;
    Windows has no service env."""
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
    """Mirror of the retrieve hook's `_resolve_rest_key`: `(key, source)` —
    from this env (`source="env"`), else the managed install's `service.env`
    (`source="file"`), else `("", "")`. Never raises; the value is never
    logged."""
    from_env = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if from_env:
        return from_env, "env"
    path = _service_env_path()
    if path is None:
        return "", ""
    try:
        text = path.read_text(encoding="utf-8").lstrip("﻿")
    except Exception:  # noqa: BLE001 — hook must never break a stop hook
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


def _bounded(call, budget: float):
    """Mirror of the retrieve hook's `_bounded`: run `call()` on a daemon
    thread and wait at most `budget` seconds for it. Returns its result, or
    `None` when it has not finished in time (the thread is left to end with
    the process; the hook exits right after)."""
    box: list = []

    def _run() -> None:
        try:
            box.append(call())
        except Exception:  # noqa: BLE001 — hook must never break a stop hook
            box.append(None)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(max(0.0, budget))
    return box[0] if box else None


def _episode_revision_count(key: str) -> int | None:
    """This episode's revision count, via one bounded, read-only REST call —
    `episode_memory(action="inspect", episode=key)` against the local door.

    `None` on ANY failure: the door unconfigured (no key resolves), a bad or
    unresolved port, a timeout, a non-200, a malformed body, or the episode
    simply not found yet (a session's first ask, before anything was ever
    recorded). The caller treats `None` exactly like "no new revision" —
    today's ask-cadence behaviour, unchanged. Never raises.

    Reads `_EPISODE_DOOR_TIMEOUT_SECONDS` fresh on every call rather than
    binding it as a default parameter, so a test (or a future tuning knob)
    that reassigns the module constant actually changes the bound used here.
    """
    api_key, source = _resolve_rest_key()
    if not api_key or (source == "file" and _rest_host() not in _LOOPBACK_HOSTS):
        return None
    port = _rest_port()
    if port is None:
        return None
    timeout = _EPISODE_DOOR_TIMEOUT_SECONDS

    def _call() -> int | None:
        body = json.dumps({"action": "inspect", "episode": key}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{_rest_host()}:{port}/api/episode_memory",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — hook must never break a stop hook
            return None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None
        data = payload.get("data")
        revisions = data.get("revisions") if isinstance(data, dict) else None
        return len(revisions) if isinstance(revisions, list) else None

    return _bounded(_call, timeout)


def _episode_ask(
    session_id: str, tools: list[dict], substantive: bool, level: str
) -> str | None:
    """Update this session's episode coverage and return the ask when it is due.

    Coverage is hook-local: covered through the last successful record,
    pending while substantive turns accrue after it. Asking never resets the
    count — only a record does — so an ignored ask repeats after its cooldown.

    Before an ask that is otherwise due actually fires, one bounded REST
    `inspect` call (`_episode_revision_count`) checks whether some other door
    recorded a revision this hook hasn't seen. If so, the count resets there
    too and the ask is skipped — module docstring has the full rationale.
    """
    preset = _EPISODE_ASK_PRESETS.get(level)
    if preset is None or not session_id:
        return None
    turns = _env_int("EXOMEM_EPISODE_ASK_TURNS", preset[0])
    cooldown = _env_int("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", preset[1])
    path = _episode_state_path(session_id)
    state = _read_episode_state(path)
    if any(_successful_episode_record(tool) for tool in tools):
        state["substantive_since_record"] = 0
        state["last_seen_revisions"] = _REVISIONS_UNKNOWN
    elif substantive:
        state["substantive_since_record"] += 1
    now = time.time()
    client = _EPISODE_CLIENT_LABELS.get(_hook_client(), _hook_client())
    key = episode_key(client, session_id)
    about_due = (
        turns > 0
        and state["substantive_since_record"] >= turns
        and now - state["last_ask_ts"] >= cooldown
    )
    if about_due:
        revisions = _episode_revision_count(key)
        if revisions is not None:
            # Only a count above a known baseline is another door's record.
            # An unknown baseline follows a record seen here, which the door
            # now reports too; re-base on it rather than suppress.
            baseline = state["last_seen_revisions"]
            if baseline != _REVISIONS_UNKNOWN and revisions > baseline:
                state["substantive_since_record"] = 0
                about_due = False
            state["last_seen_revisions"] = revisions
    due = about_due
    if due:
        state["last_ask_ts"] = now
    _write_episode_state(path, state)
    if not due:
        return None
    return EPISODE_ASK.replace("{key}", key)


def _note_continuation_record(session_id: str, tools: list[dict]) -> None:
    """Count a record made in a `stop_hook_active` continuation. Never asks.

    The ask blocks a Stop, and the agent answers it in the continuation that
    follows, which Stops again with `stop_hook_active`. That record is the
    coverage the ask asked for; dropping it would repeat the ask every cooldown.
    """
    if not session_id or not any(_successful_episode_record(tool) for tool in tools):
        return
    path = _episode_state_path(session_id)
    state = _read_episode_state(path)
    state["substantive_since_record"] = 0
    state["last_seen_revisions"] = _REVISIONS_UNKNOWN
    _write_episode_state(path, state)


def _pending_restart_marker() -> Path:
    return _hook_home() / ".cache" / "exomem-nudge" / PENDING_RESTART_MARKER


def _exomem_tool_seen(tools: list[dict]) -> bool:
    """Whether any exomem MCP tool appears this turn — read-only counts.

    A read proves the server is loaded just as well as a write does, and this is
    only ever used to answer "did the client restart yet".
    """
    return any(
        re.search(r"exomem|knowledge[_-]?base", str(tool.get("name") or ""), re.I)
        for tool in tools
    )


def _restart_pending(tools: list[dict]) -> bool:
    """True while the MCP write tools the reminder asks for cannot exist yet."""
    marker = _pending_restart_marker()
    try:
        if not marker.exists():
            return False
        if _exomem_tool_seen(tools) or (
            time.time() - marker.stat().st_mtime > _PENDING_RESTART_MAX_AGE_SEC
        ):
            marker.unlink(missing_ok=True)
            return False
    except OSError:
        return False
    return True


def _cooldown_ok(session_id: str, cooldown: int) -> tuple[bool, Path]:
    """Per-session timestamp file (mtime-based, so we never parse content)."""
    state_dir = _hook_home() / ".cache" / "exomem-nudge"
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
    except Exception:  # noqa: BLE001 — the advisory marker is strictly best-effort
        pass


def _log(text: str) -> None:
    try:
        logp = _hook_home() / "exomem-capture-nudge.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        snippet = re.sub(r"\s+", " ", text)[-160:]
        with open(logp, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} nudge fired | {snippet}\n")
    except Exception:  # noqa: BLE001 — logging must never break a stop hook
        pass


def main() -> int:
    _normalize_env_aliases()
    if os.environ.get("EXOMEM_CAPTURE_NUDGE_DISABLE"):
        return 0
    level = _prominence()
    preset = _PROMINENCE_PRESETS[level]
    if preset is None:  # prominence=off — the user asked for explicit invocation only
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — malformed hook input must fail soft
        return 0

    if data.get("stop_hook_active") or data.get("stopHookActive"):  # already blocked once
        tpath = data.get("transcript_path") or data.get("transcriptPath")
        if tpath and _EPISODE_ASK_PRESETS.get(level) is not None:
            _note_continuation_record(
                str(data.get("session_id") or data.get("sessionId") or ""),
                _latest_turn(tpath)[1],
            )
        return 0
    tpath = data.get("transcript_path") or data.get("transcriptPath")
    event_assistant_text = data.get("last_assistant_message") or data.get(
        "lastAssistantMessage"
    )
    if not isinstance(event_assistant_text, str):
        event_assistant_text = ""
    if not tpath and not event_assistant_text:
        return 0

    # Explicit env still wins; the prominence level only moves the default.
    min_chars = _env_int("EXOMEM_CAPTURE_NUDGE_MIN_CHARS", preset[0])
    cooldown = _env_int("EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC", preset[1])

    # Codex exposes the final text directly because its transcript format is not
    # a stable hook API. Keep transcript parsing as the Claude/older-client
    # fallback and to retain best-effort successful-write detection.
    transcript_text, tools = _latest_turn(tpath) if tpath else ("", [])
    assistant_text = event_assistant_text or transcript_text
    session_id = str(data.get("session_id") or data.get("sessionId") or "")
    if _restart_pending(tools):  # hooks are live but the MCP server is not loaded yet
        return 0
    # Before every write/Saved shortcut below: those silence the per-turn
    # reminder, but they never cover the episode (task 4.1).
    ask = _episode_ask(
        session_id, tools, len(assistant_text.strip()) >= min_chars, level
    )
    if ask is not None:
        _log(assistant_text)
        print(json.dumps({"decision": "block", "reason": ask}))
        return 0
    if any(_successful_kb_write(tool) for tool in tools):  # already captured this turn
        return 0
    if re.search(r"Saved\s*(?:->|→|:)", assistant_text):
        return 0
    if len(assistant_text.strip()) < min_chars:  # trivial turn, not a landing
        return 0

    ok, stamp = _cooldown_ok(session_id, cooldown)
    if not ok:  # fired recently this session — keep cost bounded
        return 0

    _touch(stamp)
    _log(assistant_text)
    print(json.dumps({"decision": "block", "reason": REMINDER}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
