"""A stdio MCP tap between the agent CLI and the Exomem server under test.

The agent CLI starts this script as its MCP server; the script starts the real
server and pumps newline-delimited JSON-RPC both ways. It is the one place that
sees every Exomem call, so it does the three things the protocol asks of the
client side and that no CLI flag does:

- it applies the arm's tool filter to ``tools/list``, so the actor sees only
  the arm's read tools, with the shipped descriptions unchanged;
- it enforces the pull budget per user turn: the harness writes the current
  turn's index to ``--turn-file`` before it sends the turn, and a call past the
  budget is answered here with a refusal and never reaches the server;
- it records the server instructions, the filtered tool list, every call with
  its result, and the server's exit, so a crash of the server under test is
  told apart from a fault of the harness.

Stdlib only: the CLI runs it with the build's interpreter, before anything of
the harness is importable, and a proxy that imports the product could change
what it relays.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

#: The text an over-budget call receives. Part of every arm's hashed surface.
BUDGET_REFUSAL = (
    "Refused: the Exomem call budget for this user turn is spent. "
    "Answer now from what you already have."
)
#: The text a call to a tool outside the arm's filter receives.
FILTER_REFUSAL = "Refused: this tool is not available in this session."

#: The MCP handshakes whose answer carries the server instructions: the
#: session-based ``initialize`` and the stateless ``server/discover`` that a
#: client on the 2026-07-28 protocol sends instead. Protocol method names.
HANDSHAKE_METHODS = frozenset({"initialize", "server/discover"})


def process_start_ticks(pid: int) -> str | None:
    """The process's start time in clock ticks (``/proc/<pid>/stat`` field 22),
    or ``None`` off Linux or once the process is gone. A pid with the same start
    time is the same process, so a reused pid is never mistaken for it."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    # The command name, field 2, is in parentheses and may hold spaces.
    return stat.rpartition(")")[2].split()[19]


class TapLog:
    """Append-only JSONL log; one line per event, flushed as written."""

    def __init__(self, path: Path) -> None:
        self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def write(self, event: str, **fields: object) -> None:
        line = json.dumps({"t": round(time.monotonic() - self._start, 4), "event": event, **fields})
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()


class Budget:
    """Calls per user turn. Every call that reaches the tap counts, refused or not."""

    def __init__(self, turn_file: Path, limit: int) -> None:
        self._turn_file = turn_file
        self._limit = limit
        self._counts: dict[int, int] = {}

    def current_turn(self) -> int:
        try:
            return int(self._turn_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return -1

    def admit(self) -> tuple[int, int, bool]:
        """``(turn, ordinal of this call in the turn, within budget)``."""

        turn = self.current_turn()
        ordinal = self._counts.get(turn, 0) + 1
        self._counts[turn] = ordinal
        return turn, ordinal, ordinal <= self._limit


class Tap:
    def __init__(
        self, *, server: subprocess.Popen, log: TapLog, budget: Budget, allow: frozenset[str]
    ) -> None:
        self.server = server
        self.log = log
        self.budget = budget
        self.allow = allow
        self._out_lock = threading.Lock()
        self._pending: dict[object, tuple[str, dict]] = {}
        self._pending_lock = threading.Lock()

    def to_client(self, message: dict) -> None:
        with self._out_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def _refuse(self, request_id: object, text: str) -> None:
        self.to_client(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": True},
            }
        )

    def from_client(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.log.write("client_unparsed", raw=raw[:2000])
            self._forward(raw)
            return
        if not isinstance(message, dict):
            # MCP over stdio sends no batches; one would bypass the budget, so it
            # is relayed and logged for the chain check to fail on.
            self.log.write(
                "batch_unhandled", size=len(message) if isinstance(message, list) else None
            )
            self._forward(raw)
            return
        method = message.get("method")
        request_id = message.get("id")
        if method == "tools/call" and request_id is not None:
            params = message.get("params") or {}
            name = str(params.get("name", ""))
            turn, ordinal, within = self.budget.admit()
            call = {
                "turn": turn,
                "ordinal": ordinal,
                "name": name,
                "arguments": params.get("arguments"),
            }
            if name not in self.allow:
                self.log.write("call", outcome="refused_filtered", **call)
                self._refuse(request_id, FILTER_REFUSAL)
                return
            if not within:
                self.log.write("call", outcome="refused_budget", **call)
                self._refuse(request_id, BUDGET_REFUSAL)
                return
            with self._pending_lock:
                self._pending[request_id] = ("tools/call", {**call, "started": time.monotonic()})
        elif (method in HANDSHAKE_METHODS or method == "tools/list") and request_id is not None:
            with self._pending_lock:
                self._pending[request_id] = (method, {})
        self._forward(raw)

    def _forward(self, raw: str) -> None:
        try:
            self.server.stdin.write(raw + "\n")
            self.server.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            self.log.write("server_write_failed")

    def from_server(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.log.write("server_unparsed", raw=raw[:2000])
            self._write_raw(raw)
            return
        request_id = message.get("id") if isinstance(message, dict) else None
        with self._pending_lock:
            pending = (
                self._pending.pop(request_id, None)
                if request_id is not None and "method" not in message
                else None
            )
        if pending is not None:
            method, call = pending
            result = message.get("result")
            if method in HANDSHAKE_METHODS and isinstance(result, dict):
                meta = result.get("_meta") or {}
                self.log.write(
                    "handshake",
                    method=method,
                    instructions=result.get("instructions"),
                    server_info=result.get("serverInfo")
                    or meta.get("io.modelcontextprotocol/serverInfo"),
                    protocol_version=result.get("protocolVersion")
                    or result.get("supportedVersions"),
                )
            elif method == "tools/list" and isinstance(result, dict):
                tools = result.get("tools") or []
                kept = [tool for tool in tools if tool.get("name") in self.allow]
                withheld = sorted(
                    str(tool.get("name")) for tool in tools if tool.get("name") not in self.allow
                )
                message = {**message, "result": {**result, "tools": kept}}
                self.log.write("tools_list", tools=kept, withheld=withheld)
            elif method == "tools/call":
                started = call.pop("started")
                self.log.write(
                    "call",
                    outcome="served",
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                    result=result,
                    error=message.get("error"),
                    **call,
                )
        self.to_client(message)

    def _write_raw(self, raw: str) -> None:
        with self._out_lock:
            sys.stdout.write(raw + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--turn-file", required=True, type=Path)
    parser.add_argument("--budget", required=True, type=int)
    parser.add_argument(
        "--allow", required=True, help="comma-separated tool names the arm may see and call"
    )
    parser.add_argument("--server-stderr", required=True, type=Path)
    parser.add_argument("server", nargs=argparse.REMAINDER, help="-- then the server argv")
    args = parser.parse_args(argv)
    server_argv = args.server[1:] if args.server[:1] == ["--"] else args.server
    if not server_argv:
        parser.error("the server argv is required after --")

    log = TapLog(args.log)
    stderr = open(args.server_stderr, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
    server = subprocess.Popen(  # noqa: S603 - argv comes from the harness's own MCP config
        server_argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        bufsize=1,
        env=os.environ,
    )
    log.write(
        "server_started",
        pid=server.pid,
        start_ticks=process_start_ticks(server.pid),
        argv=server_argv,
    )
    tap = Tap(
        server=server,
        log=log,
        budget=Budget(args.turn_file, args.budget),
        allow=frozenset(args.allow.split(",")),
    )

    closing = threading.Event()

    def shut_down() -> None:
        closing.set()
        try:
            server.stdin.close()
        except OSError:
            pass
        try:
            returncode = server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            returncode = server.wait()
        log.write("server_exit", returncode=returncode, while_client_connected=False)

    def pump_server() -> None:
        for line in server.stdout:
            if line.strip():
                tap.from_server(line.rstrip("\n"))
        returncode = server.wait()
        if not closing.is_set():
            # The server went away while the client still held the connection:
            # the crash the protocol scores as an actor failure.
            log.write("server_exit", returncode=returncode, while_client_connected=True)
            sys.stdout.flush()
            os._exit(1)

    def on_terminate(signum: int, frame: object) -> None:
        shut_down()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_terminate)
    reader = threading.Thread(target=pump_server, daemon=True)
    reader.start()
    for line in sys.stdin:
        if line.strip():
            tap.from_client(line.rstrip("\n"))
    # The client closed its end: an orderly shutdown of the server under test.
    shut_down()
    reader.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
