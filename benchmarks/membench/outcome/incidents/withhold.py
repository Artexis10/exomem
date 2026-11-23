"""Withhold one write's acknowledgement at the transport, for the cases that test recovery from an uncertain write.

The product answers ``MUTATION_ACKNOWLEDGEMENT_PENDING`` when an accepted write outlives its deadline: the work
commits, and the caller is told that its outcome is not known. No product setting makes a healthy write do that
without also degrading every other call of the session, so the unit puts a transport between the actor and the
arm's server, and the product is left as it is. The transport forwards every message unchanged except one: the
result of the session's first write that the arm commits. The write reaches the arm and commits; the real result
is kept in the unit's wire log; and the actor is handed the arm's own pending result (``arm_side.py withhold``)
instead. From then on the transport forwards everything unchanged.

Two carriers share one decision (``Withholder``): a loopback HTTP proxy for the Claude Code actor, and a stdio relay
for the OpenAI actor's server, which that actor starts as a child process. The wire log is every tool call the actor
made while the transport was armed, in order, with its arguments and its replay identity; a check reads recovery
from it, so it never depends on how a client reports its own calls.

This file imports only the standard library, because the relay runs it as a script under ``python -I``.
"""

from __future__ import annotations

import http.client
import json
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

#: A tool that runs longer than this on the arm is the arm's fault, not the transport's.
UPSTREAM_SECONDS = 600
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
    }
)


@dataclass
class Call:
    """One ``tools/call`` of the actor, as the wire carried it."""

    request_id: object
    seq: int
    tool: str | None
    arguments: dict
    #: The replay identity the client sent in the call's metadata, or None when it sent none.
    identity: str | None
    #: A write that the transport may still withhold: none has been yet, and the arm lists the tool as a write.
    candidate: bool
    withheld: bool = False
    real_result: dict | None = field(default=None)

    def entry(self) -> dict:
        kept = {
            "seq": self.seq,
            "tool": self.tool,
            "arguments": self.arguments,
            "identity": self.identity,
        }
        return {
            **kept,
            "withheld": self.withheld,
            **({"real_result": self.real_result} if self.withheld else {}),
        }


def refused(message: dict) -> bool:
    """Whether the arm answered the call with a refusal, which committed nothing.

    A protocol error, an ``isError`` result, and the compact ``{"error": {...}}`` result that the arm's agent-facing
    refusal contract fixes (``agent_refusal.py``). A write that was refused is never the one withheld, because the
    actor would then be told that a write whose outcome is unknown had in fact been rejected.
    """
    result = message.get("result")
    if not isinstance(result, dict):
        return True
    structured = result.get("structuredContent")
    return bool(result.get("isError")) or (isinstance(structured, dict) and "error" in structured)


class Withholder:
    """Decides, for each message that passes, whether it is the one result to swap; keeps the wire log.

    ``settings`` is ``arm_side.py withhold`` (``writers``, ``result``, ``idempotency_key``). Every call is logged
    until ``disarm`` ends the main session's part of a unit, and the first committed write is swapped, once.
    """

    def __init__(self, settings: dict, log: Path) -> None:
        self.writers = frozenset(settings["writers"])
        self.result = settings["result"]
        self.key = settings["idempotency_key"]
        self.log = log
        self.logging = True
        self.owed = True
        self.calls: list[Call] = []
        self.lock = threading.Lock()
        self.flush()

    def begin(self, message: object) -> Call | None:
        """The call a client request makes, when it is a tool call of the main session; None otherwise."""
        if (
            not isinstance(message, dict)
            or message.get("method") != "tools/call"
            or "id" not in message
        ):
            return None
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        with self.lock:
            if not self.logging:
                return None
            call = Call(
                request_id=message["id"],
                seq=len(self.calls),
                tool=params.get("name"),
                arguments=params.get("arguments") or {},
                identity=meta.get(self.key),
                candidate=self.owed and params.get("name") in self.writers,
            )
            self.calls.append(call)
            self.flush()
        return call

    def finish(self, call: Call, message: dict) -> dict:
        """The response the client is handed for ``call``: the arm's own, except for the one write that is withheld."""
        with self.lock:
            if self.owed and call.candidate and not refused(message):
                call.withheld, call.real_result, self.owed = True, message["result"], False
                message = {
                    "jsonrpc": message.get("jsonrpc", "2.0"),
                    "id": message["id"],
                    "result": self.result,
                }
            self.flush()
        return message

    def disarm(self) -> None:
        """Stop logging and withholding: the main session is over, and a follow-up session is not interrupted."""
        with self.lock:
            self.logging = self.owed = False

    def flush(self) -> None:
        self.log.write_text(
            json.dumps([call.entry() for call in self.calls], indent=1, default=str)
        )


def answered(call_by_id: dict, message: object) -> Call | None:
    """The call that a server message answers, when it is a response (a result or an error) to one of ``call_by_id``."""
    if isinstance(message, dict) and ("result" in message or "error" in message):
        return call_by_id.get(message.get("id"))
    return None


def rewrite_event(event: str, swap) -> str:
    """One server-sent event, with the JSON-RPC message of its ``data`` replaced by ``swap(message)``."""
    lines = event.split("\n")
    data = [line.partition(":")[2].removeprefix(" ") for line in lines if line.startswith("data:")]
    try:
        message = json.loads("\n".join(data))
    except ValueError:
        return event
    swapped = swap(message)
    if swapped is message:
        return event
    return "\n".join(
        [*(line for line in lines if not line.startswith("data:")), "data: " + json.dumps(swapped)]
    )


def rewrite(body: bytes, content_type: str, swap) -> bytes:
    """A buffered server reply with ``swap`` applied to each JSON-RPC message in it, in the reply's own framing."""
    if content_type.startswith("text/event-stream"):
        events = body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
        return "\n\n".join(rewrite_event(event, swap) for event in events).encode("utf-8")
    message = json.loads(body)
    swapped = [swap(item) for item in message] if isinstance(message, list) else swap(message)
    return json.dumps(swapped).encode("utf-8")


def proxy_handler(withholder: Withholder, host: str, port: int) -> type[BaseHTTPRequestHandler]:
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            """The unit keeps the server's log and the wire log; a proxy line per request would only add noise."""

        def forward(self) -> None:
            length = self.headers.get("Content-Length")
            if length is None and self.headers.get("Transfer-Encoding"):
                self.send_error(411)
                return
            body = self.rfile.read(int(length)) if length else b""
            calls: dict = {}
            if self.command == "POST":
                try:
                    sent = json.loads(body)
                except ValueError:
                    sent = None
                for message in sent if isinstance(sent, list) else [sent]:
                    if (call := withholder.begin(message)) is not None:
                        calls[call.request_id] = call
            # The reply is read and rewritten as text, so the arm is asked for it uncompressed.
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP | {"host", "accept-encoding"}
            }
            upstream = http.client.HTTPConnection(host, port, timeout=UPSTREAM_SECONDS)
            try:
                upstream.request(
                    self.command, self.path, body or None, {**headers, "Host": f"{host}:{port}"}
                )
                reply = upstream.getresponse()
                hold = any(call.candidate for call in calls.values())

                def swap(message: object) -> object:
                    call = answered(calls, message)
                    return message if call is None else withholder.finish(call, message)

                # A write that may be withheld is buffered, because its result is the message to change.
                payload = (
                    rewrite(reply.read(), reply.getheader("Content-Type", ""), swap)
                    if hold
                    else None
                )
                self.send_response(reply.status)
                for name, value in reply.getheaders():
                    if name.lower() not in HOP_BY_HOP | {"content-length", "date", "server"}:
                        self.send_header(name, value)
                if payload is not None:
                    self.send_header("Content-Length", str(len(payload)))
                elif reply.getheader("Content-Length") is not None:
                    self.send_header("Content-Length", reply.getheader("Content-Length"))
                # A stream has no length to promise; the connection's end is its end.
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if payload is not None:
                    self.wfile.write(payload)
                else:
                    while chunk := reply.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (OSError, http.client.HTTPException):
                self.close_connection = True
                if not self.wfile.closed:
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
            finally:
                upstream.close()

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_HEAD = forward

    return Proxy


@contextmanager
def proxied(entry: dict, withholder: Withholder) -> Iterator[dict]:
    """Serve the HTTP MCP server of ``entry`` through the withholding proxy on loopback; yield the entry for the proxy.

    The proxy serves while the block runs and stops with it. The entry keeps its headers, so the actor's sign-in
    reaches the arm's server unchanged.
    """
    target = urlsplit(entry["url"])
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), proxy_handler(withholder, target.hostname, target.port)
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {**entry, "url": f"http://127.0.0.1:{server.server_address[1]}{target.path}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def relayed(spec: dict, settings: dict, root: Path) -> dict:
    """The stdio server entry ``spec`` as an entry that runs it behind the relay; ``settings`` are the withholder's.

    The relay's settings are written to a file of the unit, and its wire log is the unit's ``wire.json``.
    """
    config = root / "withhold.json"
    config.write_text(json.dumps({**settings, "log": str(root / "wire.json")}))
    return {
        **spec,
        "command": sys.executable,
        "args": [
            "-I",
            str(Path(__file__).resolve()),
            "relay",
            str(config),
            "--",
            spec["command"],
            *spec["args"],
        ],
    }


def relay(config: Path, command: list[str]) -> int:
    """Run ``command`` as a stdio MCP server whose messages pass through a ``Withholder``; its exit code.

    MCP over stdio is one JSON message per line each way, so the relay forwards lines and parses them only to find
    the tool calls and their results.
    """
    settings = json.loads(config.read_text())
    withholder = Withholder(settings, Path(settings["log"]))
    child = subprocess.Popen(  # noqa: S603 - the argv is the unit's own server command
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    pending: dict = {}

    def pump_requests() -> None:
        for line in sys.stdin:
            try:
                call = withholder.begin(json.loads(line))
            except ValueError:
                call = None
            if call is not None:
                pending[call.request_id] = call
            child.stdin.write(line)
            child.stdin.flush()
        child.stdin.close()

    threading.Thread(target=pump_requests, daemon=True).start()
    for line in child.stdout:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if (call := answered(pending, message)) is not None:
            swapped = withholder.finish(call, message)
            line = line if swapped is message else json.dumps(swapped) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()
    return child.wait()


if __name__ == "__main__":
    if sys.argv[1] != "relay" or sys.argv[3] != "--":
        raise SystemExit("usage: withhold.py relay CONFIG -- COMMAND [ARGUMENT...]")
    raise SystemExit(relay(Path(sys.argv[2]), sys.argv[4:]))
