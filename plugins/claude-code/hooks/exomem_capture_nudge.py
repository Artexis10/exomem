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
cooldown caps frequency; every trigger is logged under the active client home for
tuning.

Tunables (env): EXOMEM_CAPTURE_NUDGE_DISABLE=1 (off), EXOMEM_CAPTURE_NUDGE_MIN_CHARS
(default 300 — lower it for a dense script like Japanese, which packs more meaning
per char), EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC (default 300). The legacy KB_CAPTURE_NUDGE_*
names are still accepted for back-compat (aliased to the EXOMEM_* names at startup).

Contract (Claude Code / Codex Stop hook): read the event JSON on stdin; print
`{"decision":"block","reason":...}` and exit 0 to block the stop and feed the
reminder to the agent; exit 0 with no output to allow the stop. Never raises — a
hook crash must not break the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# KB write tools — mixed tools include their operation selector so read-only
# discovery does not suppress the reminder. Legacy names remain recognized.
_KB_WRITE = re.compile(
    r"(?:exomem|knowledge[_-]?base).*(?:"
    r"note|add|edit|append|create_file|replace|remember|capture_source|"
    r"preserve_evidence|manage_memory_file|"
    r"connect_memory:(?:create-entity|accept-relation)"
    r")",
    re.I,
)

REMINDER = (
    "[Exomem capture check] This turn did substantial work. If your Exomem knowledge-base "
    "skill is available, check whether the turn reached a durable conclusion or a "
    "durable recurring entity recognized by the active entity registry, prioritizing "
    "the selected knowledge packs. For an entity, first call "
    'connect_memory(operation="resolve-entity", name=...). Update stable facts with '
    "edit_memory or add a governed relation when one "
    "active page matches. Only when none matches and the identity is stable, recurring, "
    "central, and useful beyond this source may you call "
    'connect_memory(operation="create-entity"). A single incidental mention, unresolved '
    "identity, or transient participant stays in source/note context. Capture conclusions "
    "as distilled compiled notes, not transcripts, then report Saved -> path. If neither "
    "case applies, or no Knowledge Base is configured, do nothing and stop."
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


def _hook_client() -> str:
    explicit = os.environ.get("EXOMEM_HOOK_CLIENT", "").strip().lower()
    if explicit in {"claude", "codex"}:
        return explicit
    try:
        parts = {p.lower() for p in Path(__file__).resolve().parts}
    except Exception:
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
        except Exception:
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
    operation = str(tool.get("operation") or "")
    selector = f"{name}:{operation}" if operation else name
    if not _KB_WRITE.search(selector):
        return False
    tool_input = tool.get("input")
    if (
        "edit_memory" in name.lower()
        and isinstance(tool_input, dict)
        and tool_input.get("validate_only") is True
    ):
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
    except Exception:
        pass


def _log(text: str) -> None:
    try:
        logp = _hook_home() / "exomem-capture-nudge.log"
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

    if data.get("stop_hook_active") or data.get("stopHookActive"):  # already blocked once
        return 0
    tpath = data.get("transcript_path") or data.get("transcriptPath")
    event_assistant_text = data.get("last_assistant_message") or data.get(
        "lastAssistantMessage"
    )
    if not isinstance(event_assistant_text, str):
        event_assistant_text = ""
    if not tpath and not event_assistant_text:
        return 0

    min_chars = _env_int("EXOMEM_CAPTURE_NUDGE_MIN_CHARS", 300)
    cooldown = _env_int("EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC", 300)

    # Codex exposes the final text directly because its transcript format is not
    # a stable hook API. Keep transcript parsing as the Claude/older-client
    # fallback and to retain best-effort successful-write detection.
    transcript_text, tools = _latest_turn(tpath) if tpath else ("", [])
    assistant_text = event_assistant_text or transcript_text
    if any(_successful_kb_write(tool) for tool in tools):  # already captured this turn
        return 0
    if re.search(r"Saved\s*(?:->|→|:)", assistant_text):
        return 0
    if len(assistant_text.strip()) < min_chars:  # trivial turn, not a landing
        return 0

    ok, stamp = _cooldown_ok(str(data.get("session_id") or data.get("sessionId") or ""), cooldown)
    if not ok:  # fired recently this session — keep cost bounded
        return 0

    _touch(stamp)
    _log(assistant_text)
    print(json.dumps({"decision": "block", "reason": REMINDER}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
