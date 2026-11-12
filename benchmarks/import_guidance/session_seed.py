"""Runs inside one arm's environment (`PYTHONPATH=<arm checkout>/src`): lays the arm's own starter scaffold in an empty
vault, copies a case's seeded pages into it, and readies the vault's external state.

    python session_seed.py <seed dir> <vault> <product root> [--ready-graph]

The caller pins every state root and the vault path in the environment (`session_close.pinned_env`), so this process
reaches no real state. The scaffold comes from the arm's `init_vault`, so each arm's vault carries that arm's own
`_Schema` and registries; the pages are the same files for both arms.

A seeded vault has no relation graph, and the product marks nothing from relations while the graph is unknown. A live
vault has one, because its writes build it. `--ready-graph` brings a seeded vault to that state before anything reads it:
the arm's own `exomem maintain --reconcile` (the reconcile that `maintain_memory` runs) rewrites no page of the seed, and
then the arm's own graph index proves its state is `available`, independently of the activation packet.
`record_worker.py` runs the same two steps (`reconcile`, then `wait_for_graph`). The process prints one JSON line: the graph state
it saw and the seconds it took.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

#: How long the arm's reconcile and the wait for its graph may take, each.
READY_SECONDS = 120.0
READY_INTERVAL = 2.0


def main(seed: Path, vault: Path, product_root: Path, ready_graph: bool = False) -> dict | None:
    import exomem

    if not Path(exomem.__file__).resolve().is_relative_to(product_root / "src"):
        raise SystemExit(f"imported exomem from {exomem.__file__}, not from {product_root}/src")
    from exomem.init import init_vault
    from exomem.kbdir import kb_dirname

    init_vault(vault)
    shutil.copytree(seed, vault / kb_dirname(), dirs_exist_ok=True)
    if not ready_graph:
        return None
    started = time.monotonic()
    reconcile()
    state = wait_for_graph(vault)
    return {"graph": state, "seconds": round(time.monotonic() - started, 1)}


def reconcile() -> None:
    """The arm's own reconcile, in a process of its own, on the vault that the environment pins.

    It runs before any server of this process is built, and returns once the arm has finished its derived-state work.
    """
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "exomem", "maintain", "--reconcile", "--json"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=READY_SECONDS,
    )
    try:
        succeeded = json.loads(done.stdout).get("success") is True
    except ValueError:
        succeeded = False
    if done.returncode != 0 or not succeeded:
        tail = (done.stdout.strip() + " " + done.stderr.strip()).strip()[-400:]
        raise SystemExit(f"the arm's reconcile failed: {tail}")


def open_server(vault: Path) -> Callable[[str, dict], dict]:
    """The arm's own tool server, built in this process on the pinned vault; returns `call(tool, arguments)`."""
    import asyncio

    from exomem import server as server_module
    from exomem import state_migration
    from exomem.governance import principal as principal_module

    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.require_vault_state_ready(vault)
    server_module.load_dotenv = lambda *args, **kwargs: None
    # The server is built before the first write: a write before it fails on the graph rebuild's lifetime.
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
            result = asyncio.run(mcp.call_tool(tool, arguments, run_middleware=True))
        return json.loads(result.content[0].text)

    return call


def wait_for_graph(
    vault: Path,
    seconds: float = READY_SECONDS,
    interval: float = READY_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    *,
    availability: Callable[[], str] | None = None,
) -> str:
    """Prove the arm's graph available, independently of packet shape; stop naming the state last seen."""
    from exomem.context_catalogue import EDGES_AVAILABLE
    from exomem.epistemic_graph import EpistemicGraphIndex

    if availability is None:
        availability = partial(EpistemicGraphIndex(vault).availability_state, prove=True)
    deadline = time.monotonic() + seconds
    while True:
        state = availability()
        if state == EDGES_AVAILABLE:
            return state
        if time.monotonic() >= deadline:
            raise SystemExit(f"the relation graph still reported {state!r} after {seconds:g}s: the vault is not ready")
        sleep(interval)


if __name__ == "__main__":
    ready = "--ready-graph" in sys.argv[1:]
    arguments = [item for item in sys.argv[1:] if item != "--ready-graph"]
    outcome = main(Path(arguments[0]), Path(arguments[1]), Path(arguments[2]).resolve(), ready)
    if outcome is not None:
        print(json.dumps(outcome))
