"""A scripted stand-in for the agent CLI, for the adapter's dry run. No model.

Called the way the session executor calls a client launcher:
``python -m membench.utility.organisation_replay.standin CLIENT -- ARGS``.
It reads the same ``--mcp-config`` and ``--settings`` files the real client
reads, starts the tapped server and runs the hooks those settings wire at the
points the client runs them: ``SessionStart`` at startup, ``UserPromptSubmit``
before each turn, ``Stop`` after each answer and ``SessionEnd`` at exit. It
reports each hook's lifecycle but never acts on a hook's decision. Per user
turn it makes a fixed script of calls: one activation, as the shipped workflow
opens a turn, then one search, and on the second turn one governed note write.
It prints the stream-json events the executor parses.
Every answer it gives says it is a stand-in.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

VERSION = "0.0.0-standin"
SERVER_NAME = "exomem"


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class _Server:
    def __init__(self, entry: dict) -> None:
        import os

        self.proc = subprocess.Popen(  # noqa: S603 - the run's own tapped server entry
            [entry["command"], *entry["args"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, **(entry.get("env") or {})},
        )
        self._next = 0

    def ask(self, method: str, params: dict) -> dict:
        self._next += 1
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})
            + "\n"
        )
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed before answering {method}")
            message = json.loads(line)
            if message.get("id") == self._next and "method" not in message:
                return message

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=60)


def _hooks(settings: Path | None, event: str, subject: str | None) -> list[str]:
    if settings is None:
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in (data.get("hooks") or {}).get(event) or []
        # A matcher is the client's documented regular expression over the
        # event's subject (a session start's source); a group without one always runs.
        if not group.get("matcher") or (subject and re.fullmatch(group["matcher"], subject))
        for hook in group.get("hooks") or []
        if hook.get("type") == "command"
    ]


def _run_hooks(settings: Path | None, event: str, fields: dict, session: dict) -> None:
    subject = fields.get("source")
    for number, command in enumerate(_hooks(settings, event, subject)):
        hook_id = f"standin-hook-{uuid.uuid4().hex[:8]}-{number}"
        base = {
            "type": "system",
            "hook_id": hook_id,
            "hook_name": f"{event}:{subject}" if subject else event,
            "hook_event": event,
        }
        _emit({**base, "subtype": "hook_started"})
        payload = {**session, "cwd": str(Path.cwd()), "hook_event_name": event, **fields}
        done = subprocess.run(  # noqa: S602 - the hook command the run's own settings wire
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=120,
        )
        _emit(
            {
                **base,
                "subtype": "hook_response",
                "exit_code": done.returncode,
                "stdout": done.stdout,
                "stderr": done.stderr[-2000:],
                "outcome": "success" if done.returncode == 0 else "error",
            }
        )


def _call(server: _Server, doors, command: str, arguments: dict) -> dict:
    tool, spelled = doors.spell(command, arguments)
    reply = server.ask("tools/call", {"name": tool, "arguments": spelled})
    result = reply.get("result") or {}
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    text = "".join(item.get("text", "") for item in result.get("content") or [])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _turn(server: _Server, doors, index: int, prompt: str, seen: list[str]) -> str:
    _call(server, doors, "recall_context", {"turn": prompt})
    found = _call(server, doors, "ask_memory", {"query": prompt})
    hits = (
        (found.get("result") or {}).get("hits")
        if isinstance(found.get("result"), dict)
        else found.get("result")
    )
    seen.extend(str(h.get("title")) for h in hits or [] if isinstance(h, dict) and h.get("title"))
    said = f"Stand-in turn {index + 1}: the search returned {len(hits or [])} hits."
    if index == 1 and "create_note" in doors.served:
        content = (
            "## Decision\n\nThe stand-in client records one scripted decision for the dry run."
        )
        if seen:
            # One typed edge to the first page a search found, so the readback has an edge to read.
            content += f"\n\n## Relations\n\n- relates_to [[{seen[0]}]]"
        note = {"title": "Stand-in capture note", "content": content, "note_type": "pattern"}
        draft = _call(server, doors, "remember", {**note, "validate_only": True})
        commit = {
            **note,
            "draft_id": draft.get("draft_id"),
            "draft_hash": draft.get("draft_hash"),
            "draft_token": draft.get("draft_token"),
        }
        if draft.get("relation_review_hash"):
            commit |= {
                "relation_disposition": "reviewed_none",
                "relation_review_hash": draft["relation_review_hash"],
                "relation_review_reason": "stand-in note without a qualifying typed relation",
            }
        written = _call(server, doors, "remember", commit)
        said += f" It wrote one note: {json.dumps(written.get('path') or written.get('error'))}."
    return said


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    client_args = raw[raw.index("--") + 1 :] if "--" in raw else raw
    parser = argparse.ArgumentParser(prog="standin")
    parser.add_argument("--mcp-config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--settings")
    args, _rest = parser.parse_known_args(client_args)
    entry = json.loads(Path(args.mcp_config).read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    settings = Path(args.settings) if args.settings else None
    session_id = str(uuid.uuid4())
    # The hooks read the conversation from the transcript the client keeps.
    transcript = Path.cwd() / f"standin-{session_id}.jsonl"
    transcript.touch()
    session = {"session_id": session_id, "transcript_path": str(transcript)}
    server = _Server(entry)
    server.ask(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": VERSION},
        },
    )
    server.notify("notifications/initialized")
    tools = [tool["name"] for tool in server.ask("tools/list", {})["result"]["tools"]]
    from .scripted import Doors

    doors = Doors(frozenset(tools))
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "apiKeySource": "none",
            "model": args.model,
            "claude_code_version": VERSION,
            "cwd": str(Path.cwd()),
            "permissionMode": "default",
            "tools": [f"mcp__{SERVER_NAME}__{t}" for t in tools],
            "mcp_servers": [{"name": SERVER_NAME, "status": "connected"}],
        }
    )
    _run_hooks(settings, "SessionStart", {"source": "startup"}, session)
    seen: list[str] = []
    for index, line in enumerate(sys.stdin):
        message = json.loads(line)["message"]
        prompt = (
            message["content"]
            if isinstance(message["content"], str)
            else json.dumps(message["content"])
        )
        started = time.monotonic()
        _run_hooks(settings, "UserPromptSubmit", {"prompt": prompt}, session)
        answer = _turn(server, doors, index, prompt, seen)
        reply = {"role": "assistant", "content": [{"type": "text", "text": answer}]}
        with transcript.open("a", encoding="utf-8") as sink:
            for kind, said in (("user", {"role": "user", "content": prompt}), ("assistant", reply)):
                sink.write(
                    json.dumps({"type": kind, "session_id": session_id, "message": said}) + "\n"
                )
        _emit({"type": "assistant", "session_id": session_id, "message": reply})
        _run_hooks(settings, "Stop", {"stop_hook_active": False}, session)
        _emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "result": answer,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
    _run_hooks(settings, "SessionEnd", {"reason": "other"}, session)
    server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
