"""The measured sessions' MCP server: the shipped stdio server, held until recall is warm.

The tap starts this module in place of ``python -m exomem --transport stdio``.
It runs that same server in this process, and holds the client's first
``tools/list`` or ``tools/call`` until the server's own warm-up is over.

The tap's ready gate cannot do this for a measured session: its probe is a
recall call through the due-state carrier, and the probe would settle the
prior agent's promotion before the actor sees it. This holder makes no MCP
call. It waits until the product's startup has finished, then repeats the
product's own boot warm turn until that turn stops abstaining as warming. That
turn writes no log, carries no session and is not the due-state carrier.

It writes the tap's own gate events, ``ready_started`` and ``ready_finished``,
to the tap log. The session executor therefore keeps the held time out of the
turn clock, and a hold that ends in any outcome but ``ready`` is a harness
fault, exactly as for the tap's own gate.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
from pathlib import Path

from membench.utility.switch_eval.tap import READY_TIMEOUT_SECONDS, warming_refusal

#: The MCP methods the hold starts at: discovery and calls. The tap's gate holds
#: the same two, so ``initialize`` still answers while the server warms.
HELD_METHODS = frozenset({"tools/list", "tools/call"})
#: Seconds between two warm turns while the server still warms, the tap's own probe cadence.
PROBE_INTERVAL_SECONDS = 1.0
#: Names this holder in the gate events it writes beside the tap's.
SOURCE = "organisation_replay.ready_server"

_BOOT = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from membench.utility.organisation_replay.ready_server import main; sys.exit(main())"
)


def entry_args(tap_args: list[str], log: Path) -> list[str]:
    """The tap's argv with this holder as the server after ``--``, on the same interpreter.

    The client's environment carries no ``PYTHONPATH``, so the server argv names
    the benchmarks root, as the stand-in launcher does.
    """

    split = tap_args.index("--")
    benchmarks = Path(__file__).resolve().parents[3]
    return [
        *tap_args[: split + 1],
        tap_args[split + 1],
        "-c",
        _BOOT,
        str(benchmarks),
        "--log",
        str(log),
    ]


def _log(path: Path, event: str, **fields: object) -> None:
    # One append of one whole line; the tap appends to the same file.
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps({"event": event, "source": SOURCE, **fields}) + "\n")


class _Warm:
    """Whether the server's warm-up is over, decided once."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.done = threading.Event()
        self.outcome = "timeout"
        self.probes = 0
        self.activated = threading.Event()
        self.vault_root: Path | None = None

    def observe_activation(self) -> None:
        # Before the product's entry point runs its native-thread bootstrap; this
        # module imports no native runtime, so that bootstrap still comes first.
        from exomem import server_runtime

        activate = server_runtime.LocalRuntimeActivation._activate

        def observed(owner) -> None:
            self.vault_root = owner.vault_root
            try:
                activate(owner)
            finally:
                self.activated.set()

        # No product signal says startup finished; this observer changes no activation work.
        server_runtime.LocalRuntimeActivation._activate = observed

    def watch(self) -> None:
        try:
            if not self.activated.wait(max(0.0, self.deadline - time.monotonic())):
                return
            from exomem import commands, server_runtime, working_set

            while True:
                self.probes += 1
                # The product's boot warm turn, called as `server_runtime._warm_request_path`
                # calls it: no log, no session, no heat, and outside `op_activate_context`,
                # the wrapper that serves the due-state carrier.
                packet = commands._op_activate_context_body(  # noqa: SLF001
                    self.vault_root,
                    server_runtime._REQUEST_PATH_WARM_TURN,  # noqa: SLF001
                    working_set.DEFAULT_BUDGET_CHARS,
                    purpose=None,
                    continuity=None,
                    anchor=None,
                    include_timings=False,
                    client=None,
                    session=None,
                    workspace=None,
                )
                if not warming_refusal(packet):
                    self.outcome = "ready"
                    return
                if time.monotonic() + PROBE_INTERVAL_SECONDS >= self.deadline:
                    return
                time.sleep(PROBE_INTERVAL_SECONDS)
        except Exception as error:  # noqa: BLE001 - any failure is a gate outcome, never a crash
            self.outcome = f"probe_error: {type(error).__name__}"
        finally:
            self.done.set()


def _method(line: bytes) -> object:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message.get("method") if isinstance(message, dict) else None


def _relay(client: int, server: int, warm: _Warm, log: Path) -> None:
    """Forward the client's lines to the server; hold from the first held method until warm."""

    lines: queue.Queue[bytes | None] = queue.Queue()
    closed = threading.Event()

    def read() -> None:
        with open(client, "rb") as source:
            for line in source:
                lines.put(line)
        closed.set()
        lines.put(None)

    threading.Thread(target=read, name="ready-server-read", daemon=True).start()
    held = False
    try:
        with open(server, "wb") as sink:
            while (line := lines.get()) is not None:
                if not held and _method(line) in HELD_METHODS:
                    held = True
                    started = time.monotonic()
                    _log(
                        log,
                        "ready_started",
                        monotonic=started,
                        timeout_seconds=round(max(0.0, warm.deadline - started), 3),
                    )
                    # A client that leaves during the hold ends the server with it.
                    while not warm.done.wait(0.5) and not closed.is_set():
                        pass
                    finished = time.monotonic()
                    _log(
                        log,
                        "ready_finished",
                        monotonic=finished,
                        outcome=warm.outcome if warm.done.is_set() else "client_closed",
                        wait_seconds=round(finished - started, 4),
                        probes=warm.probes,
                    )
                sink.write(line)
                sink.flush()
    except BrokenPipeError:
        # The server stopped reading; it is shutting down.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, type=Path, help="the tap log the gate events join")
    args = parser.parse_args(argv)
    warm = _Warm(time.monotonic() + READY_TIMEOUT_SECONDS)
    # The server reads fd 0; point it at a pipe this process feeds.
    client = os.dup(0)
    read_end, write_end = os.pipe()
    os.dup2(read_end, 0)
    os.close(read_end)
    warm.observe_activation()
    threading.Thread(target=warm.watch, name="ready-server-watch", daemon=True).start()
    threading.Thread(
        target=_relay,
        args=(client, write_end, warm, args.log),
        name="ready-server-relay",
        daemon=True,
    ).start()
    from exomem.__main__ import main as serve

    return serve(["--transport", "stdio"])
