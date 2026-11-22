#!/usr/bin/env python
"""Rehearse store mode on a copy of a vault: answers, a write stream, hard kills and rollback.

OpenSpec change make-vault-store-canonical, task 7.1. One command takes a vault copy, a scratch
root and two case files, and runs the product code (never a prototype) through these steps:

1. Preflight. The run refuses unless every path, port and environment setting that it uses is
   isolated from the running installation (see "Isolation").
2. A coordinator of its own, the collection-store enrolment and the sidecars a service needs.
3. File mode: serve the copy, run the answer cases, stop. These answers are the baseline.
4. ``store-mode migrate --dry-run`` and ``--apply``, with times and the store's size.
5. Store mode on the same bytes: the same answer cases, compared case by case with the baseline.
6. A write stream through the served door, then hard kills (SIGKILL). Each trial waits for its
   first write to commit. Kills alternate between a random moment of the stream and a random
   moment just after a write's canonical commit starts. A missed trigger fails the trial.
   After each kill the run restarts the service, waits for the projection queue to drain,
   and checks the three things the design
   names: nothing held that the kill created, no differing row in a whole-table diff against
   what the files derive, and every projection equal to the store's bytes.
7. The answer cases again, then ``store-mode rollback --dry-run`` and ``--apply --export-to``.
8. File mode again: the answer cases equal the ones from step 7.

The run reaches the service through the owner's local stdio door, so it has no service port: the
remote door has no owner identity without an OAuth login.

Answer cases are two files. Recall canary cases use the format that ``python -m
exomem.recall_canary --cases`` reads. Query cases hold one query per line; each runs with the
graph lane on and off, and the run compares the top ten results in order and each hit's graph
flag. Every difference is reported, none is averaged away.

Isolation. A live service and a live lease coordinator run on a machine that rehearses, and an
unset ``EXOMEM_STATE_ROOT`` falls back to the live state root. So the run refuses unless:

* the vault, the scratch root, the report directory and every path the run writes neither equal,
  sit inside nor contain a ``--never`` path (after symlinks resolve), and the scratch root is
  new or empty;
* it gives every child process its own state root, log directory, config path, lease state
  directory, temporary directory, home and working directory, all inside the scratch root, and
  strips every inherited ``EXOMEM_*`` variable;
* an interpreter started as the children are started reports, from inside, the same settings
  (a ``.pth`` file or ``sitecustomize`` that sets one is caught there);
* its coordinator listens on a free port that is not 8765 or 8770, and no served process
  listens on any port (the run reads the sockets of each one and refuses a live port);
* every child sets ``FASTMCP_CHECK_FOR_UPDATES=off``, so no child asks PyPI for a newer release,
  and ``PYTHONDONTWRITEBYTECODE=1``, so none writes bytecode into the interpreter's install.

The driver runs on Linux only: it finds and stops its processes through /proc.

Use an installed build that supports store mode. The driver runs its migration and serving checks as shipped.

The JSON report may hold page paths of the vault it ran on; it goes only where ``--out`` says.
The Markdown summary opens with counts only, safe to quote.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import random
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_product_loop as loop  # noqa: E402
import synth_vault  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent

#: The product's default ports for the served process and the lease coordinator, where a live
#: installation listens. The run never binds, nor talks to, either.
LIVE_PORTS = (8765, 8770)
#: The environment that a child may inherit: lookup of executables and the locale, nothing else.
PASS_THROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
#: What the in-interpreter check compares with the environment that the run passed.
GUARDED_PREFIXES = ("EXOMEM_", "KB_MCP_", "XDG_")
GUARDED_NAMES = ("HOME", "TMPDIR", "PYTHONPATH")
LEASE_VAULT_ID = "store-rehearsal"
LEASE_REPLICA_ID = "rehearsal-service"
#: The run's lease term: a service's default (`writer_lease.LeaseConfig`), not the journey's short
#: test lease, which one operation on a real vault under load outlasts. A copy, because this
#: process builds the run's environment before it may import product code (see "Isolation").
LEASE_TTL = 30.0


class Refusal(Exception):
    """The run would not be isolated, or an input is unusable; the message says what to change."""


# --- Isolation ------------------------------------------------------------------------------


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def folds_case(path: Path) -> bool:
    """Whether the volume under ``path`` reads two names that differ in case as one name.

    Asks the filesystem, as the product does (``vault._probe_casefolds``): the nearest existing
    ancestor whose name re-cases to a sibling of the same length must give the same file back.
    No such ancestor gives False, which compares the paths exactly, as before.
    """
    for ancestor in (path, *path.parents):
        swapped = ancestor.name.swapcase()
        if not ancestor.exists() or swapped == ancestor.name or len(swapped) != len(ancestor.name):
            continue
        sibling = ancestor.parent / swapped
        return sibling.exists() and os.path.samefile(ancestor, sibling)
    return False


def refuse_overlaps(paths: Mapping[str, Path], never: Sequence[Path]) -> None:
    """Refuse a path that equals, sits inside or contains a path the run must never touch."""
    guarded = [(_resolved(path), folds_case(_resolved(path))) for path in never]
    for label, path in paths.items():
        resolved = _resolved(path)
        for protected, folds in guarded:
            same = (
                _overlap(Path(str(resolved).casefold()), Path(str(protected).casefold()))
                if folds
                else _overlap(resolved, protected)
            )
            if same:
                raise Refusal(
                    f"{label} ({resolved}) equals, sits inside or contains the --never path "
                    f"{protected}. Choose a {label} outside it."
                )


def refuse_shared_files(vault: Path) -> None:
    """Refuse a vault that shares file data with another tree, as a hard-linked copy does."""
    shared = 0
    for directory, _folders, names in os.walk(vault):
        shared += sum(os.lstat(Path(directory, name)).st_nlink > 1 for name in names)
    if shared:
        raise Refusal(
            f"{shared} files of the vault have more than one hard link, so a write could reach "
            "the tree they are linked to. Copy the vault with `cp -a` or rsync, not `cp -al`."
        )


def is_listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def check_port(port: int, label: str) -> int:
    if port in LIVE_PORTS:
        raise Refusal(f"{label} port {port} is where a live installation listens by default.")
    if is_listening(port):
        raise Refusal(f"{label} port {port} already has a listener. Choose a free port.")
    return port


def free_port() -> int:
    """A free loopback port, checked by the same rule as an explicit one."""
    (reservation,) = loop._reserve_port_reservations(1)
    with reservation:
        return check_port(int(reservation.getsockname()[1]), "chosen")


@dataclasses.dataclass(frozen=True)
class Layout:
    """Everything the run writes, all under the scratch root except the vault copy and ``out``."""

    vault: Path
    scratch: Path
    out: Path

    @property
    def home(self) -> Path:
        return self.scratch / "home"

    @property
    def state(self) -> Path:
        return self.scratch / "state"

    @property
    def logs(self) -> Path:
        return self.scratch / "logs"

    @property
    def config(self) -> Path:
        return self.home / "exomem-config.json"

    @property
    def lease(self) -> Path:
        return self.scratch / "lease"

    @property
    def tmp(self) -> Path:
        return self.scratch / "tmp"

    @property
    def work(self) -> Path:
        return self.scratch / "work"

    @property
    def coordinator_db(self) -> Path:
        return self.scratch / "coordinator" / "leases.sqlite"

    @property
    def export(self) -> Path:
        return self.scratch / "export"

    def writes(self) -> dict[str, Path]:
        """Each path the run writes, by the name a refusal gives it."""
        return {
            "vault": self.vault,
            "scratch root": self.scratch,
            "report directory": self.out,
        }


@dataclasses.dataclass(frozen=True)
class Settings:
    """The run's own coordinator port and credentials. The tokens are new for each run."""

    coordinator_port: int
    writer_token: str
    operator_token: str

    @property
    def coordinator_url(self) -> str:
        return f"http://127.0.0.1:{self.coordinator_port}"


def child_env(layout: Layout, settings: Settings, *, file_watcher: bool = True) -> dict[str, str]:
    """The whole environment of every child: a closed list, never a copy of the caller's."""
    env = {name: os.environ[name] for name in PASS_THROUGH if name in os.environ}
    env.update(
        {
            "HOME": str(layout.home),
            "TMPDIR": str(layout.tmp),
            "XDG_STATE_HOME": str(layout.state / "xdg"),
            "XDG_CACHE_HOME": str(layout.home / "cache"),
            "XDG_CONFIG_HOME": str(layout.home / "config"),
            "XDG_DATA_HOME": str(layout.home / "data"),
            "FASTMCP_HOME": str(layout.home / "fastmcp"),
            "PYTHONUTF8": "1",
            # No bytecode cache written into the interpreter's own install, which may be shared.
            "PYTHONDONTWRITEBYTECODE": "1",
            # FastMCP's banner asks PyPI for a newer release; the run makes no outbound request.
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            # The writer lease, on this run's coordinator. `single-host` takes a loopback origin.
            "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
            "EXOMEM_WRITER_LEASE_URL": settings.coordinator_url,
            "EXOMEM_WRITER_LEASE_VAULT_ID": LEASE_VAULT_ID,
            "EXOMEM_WRITER_LEASE_REPLICA_ID": LEASE_REPLICA_ID,
            "EXOMEM_WRITER_LEASE_TOKEN": settings.writer_token,
            "EXOMEM_WRITER_LEASE_TTL": str(LEASE_TTL),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(layout.lease),
            "EXOMEM_VAULT_PATH": str(layout.vault),
            "EXOMEM_STATE_ROOT": str(layout.state),
            "EXOMEM_LOG_DIR": str(layout.logs),
            "EXOMEM_CONFIG_PATH": str(layout.config),
            # Lexical answers only, as the design's offline runs; no model or media extraction.
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
        }
    )
    if not file_watcher:
        env["EXOMEM_DISABLE_FILE_WATCHER"] = "1"
    return env


def _guarded(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environment.items()
        if name.startswith(GUARDED_PREFIXES) or name in GUARDED_NAMES
    }


#: Run by the children's interpreter, so that the answer comes from inside it, after every `.pth`
#: file and `sitecustomize` has run. It prints what the product would resolve.
_INTERPRETER_PROBE = """
import json, os, sys, tempfile
from pathlib import Path
from exomem import logging_config, mode, state_paths, writer_lease
lease = writer_lease.LeaseConfig.from_env()
print(json.dumps({
    "environment": {k: v for k, v in os.environ.items()},
    "state_root": str(state_paths.state_store_root()),
    "vault_state_dir": str(state_paths.vault_state_dir(Path(sys.argv[1]))),
    "log_dir": str(logging_config.resolve_log_dir()),
    "config_path": str(mode.config_path()),
    "lease_url": lease.url,
    "lease_state_dir": str(lease.state_dir),
    "temp_dir": tempfile.gettempdir(),
    "home": str(Path.home()),
    "cwd": os.getcwd(),
}))
"""


def check_interpreter(
    python: str, env: Mapping[str, str], layout: Layout, settings: Settings
) -> dict[str, Any]:
    """Start an interpreter as the children start, and refuse unless it resolves only to scratch.

    Returns what the interpreter reported, for the report.
    """
    done = subprocess.run(
        [python, "-c", _INTERPRETER_PROBE, str(layout.vault)],
        env=dict(env),
        cwd=layout.work,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        raise Refusal(
            f"the isolated interpreter could not resolve its settings:\n{done.stderr[-1500:]}"
        )
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    arrived = _guarded(seen.pop("environment"))
    passed = _guarded(env)
    if arrived != passed:
        names = sorted(
            name for name in arrived.keys() | passed.keys() if arrived.get(name) != passed.get(name)
        )
        raise Refusal(
            f"the interpreter's environment differs from the one that the run passed for {names}. "
            "A `.pth` file or `sitecustomize` in its environment sets them. "
            "Use another interpreter."
        )
    where = {
        "state root": seen["state_root"],
        "vault state directory": seen["vault_state_dir"],
        "log directory": seen["log_dir"],
        "config path": seen["config_path"],
        "lease state directory": seen["lease_state_dir"],
        "temporary directory": seen["temp_dir"],
        "home": seen["home"],
        "working directory": seen["cwd"],
    }
    scratch = _resolved(layout.scratch)
    for label, value in where.items():
        if not _resolved(value).is_relative_to(scratch):
            raise Refusal(
                f"the interpreter resolves its {label} to {value}, outside the scratch root."
            )
    if seen["lease_url"] != settings.coordinator_url:
        raise Refusal(
            f"the interpreter's lease coordinator is {seen['lease_url']}, not the run's own."
        )
    return seen


def preflight(
    layout: Layout,
    never: Sequence[Path],
    settings: Settings,
    *,
    python: str = sys.executable,
) -> dict[str, Any]:
    """Refuse unless the run is isolated; create the scratch tree; return what was proved."""
    if not never:
        raise Refusal(
            "pass at least one --never path, such as the live vault and the live state root."
        )
    refuse_overlaps(layout.writes(), never)
    if not layout.vault.is_dir():
        raise Refusal("the vault copy is not a directory.")
    for label, path in (("scratch root", layout.scratch), ("report directory", layout.out)):
        if _overlap(_resolved(path), _resolved(layout.vault)):
            raise Refusal(f"the {label} and the vault copy must not contain each other.")
    if layout.scratch.exists() and any(layout.scratch.iterdir()):
        raise Refusal("the scratch root is not empty. Every run starts from fresh state.")
    refuse_shared_files(layout.vault)
    check_port(settings.coordinator_port, "coordinator")
    for directory in (
        layout.home,
        layout.state,
        layout.logs,
        layout.lease,
        layout.tmp,
        layout.work,
        layout.coordinator_db.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seen = check_interpreter(python, child_env(layout, settings), layout, settings)
    return {"interpreter": seen, "never_paths": len(never)}


# --- The run's own processes ------------------------------------------------------------------

#: How long a served process may take to answer its first request, and an ordinary request.
START_TIMEOUT = 240.0
REQUEST_TIMEOUT = 180.0
#: A step that reads or rebuilds every page of a real vault: a sidecar build, a migrate, a fresh
#: derivation, a pass of cold finds. Each took about 20 minutes on a 15,000-page copy under load,
#: and over an hour on a loaded host; `--long-step-seconds` raises it for such a host.
LONG_STEP_SECONDS = 3600.0


def _process_table() -> dict[int, int]:
    """Parent of every process that can be read, from /proc."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError, IndexError, ValueError):
                parents[int(entry.name)] = int(
                    (entry / "stat").read_text().rsplit(")", 1)[1].split()[1]
                )
    return parents


def descendants(pid: int) -> list[int]:
    """Every process below ``pid``."""
    parents = _process_table()
    found: list[int] = []
    frontier = [pid]
    while frontier:
        frontier = [c for c, parent in parents.items() if parent in frontier and c not in found]
        found.extend(frontier)
    return found


def listening_ports(pids: Sequence[int]) -> list[int]:
    """TCP ports that any of ``pids`` listens on, read from /proc."""
    inodes: set[str] = set()
    for pid in pids:
        with contextlib.suppress(OSError):
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                with contextlib.suppress(OSError):
                    target = os.readlink(fd)
                    if target.startswith("socket:["):
                        inodes.add(target[8:-1])
    ports: set[int] = set()
    for table in ("tcp", "tcp6"):
        with contextlib.suppress(OSError):
            for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
                columns = line.split()
                # State 0A is LISTEN; column 9 is the socket inode.
                if columns[3] == "0A" and columns[9] in inodes:
                    ports.add(int(columns[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


class Rig:
    """The coordinator and the owner's offline commands, in the isolated environment."""

    def __init__(
        self,
        layout: Layout,
        settings: Settings,
        python: str = sys.executable,
        *,
        file_watcher: bool = True,
    ) -> None:
        self.layout = layout
        self.settings = settings
        self.python = python
        self.env = child_env(layout, settings, file_watcher=file_watcher)
        self.coordinator: subprocess.Popen[str] | None = None
        self.serves = 0

    # The coordinator runs for the whole rehearsal and is never restarted.
    def start_coordinator(self, timeout: float = 60.0) -> None:
        env = dict(self.env)
        env["EXOMEM_LEASE_COORDINATOR_DB"] = str(self.layout.coordinator_db)
        env["EXOMEM_LEASE_COORDINATOR_TOKEN"] = self.settings.writer_token
        env["EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"] = self.settings.operator_token
        log_path = self.layout.logs / "coordinator.log"
        with log_path.open("w", encoding="utf-8") as log:
            self.coordinator = subprocess.Popen(
                [
                    self.python,
                    "-m",
                    "exomem.lease_coordinator",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.settings.coordinator_port),
                    "--database",
                    str(self.layout.coordinator_db),
                ],
                cwd=self.layout.work,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        url = f"{self.settings.coordinator_url}/v1/vaults/{LEASE_VAULT_ID}/lease"
        loop._wait_http_ready(
            lambda: loop._http_json(url, token=self.settings.writer_token, timeout=1.0)[0] == 200,
            proc=self.coordinator,
            deadline=time.monotonic() + timeout,
            log=log_path,
            label="the run's lease coordinator",
        )

    def cli(
        self,
        *arguments: str,
        operator: bool = False,
        wait_for_lease: bool = False,
        timeout: float = 600.0,
    ) -> subprocess.CompletedProcess[str]:
        """An offline owner command; ``operator`` adds the run's operator credential to it alone."""
        env = dict(self.env)
        if operator:
            env["EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"] = self.settings.operator_token
        return loop._owner_cli(
            Path(self.python),
            env,
            self.layout.work,
            "-m",
            "exomem",
            *arguments,
            timeout=timeout,
            wait_for_lease=wait_for_lease,
        )

    def cli_json(self, what: str, *arguments: str, **options: Any) -> dict[str, Any]:
        return loop._owner_json(self.cli(*arguments, **options), what)

    def store_json(self, name: str, *arguments: str, **options: Any) -> dict[str, Any]:
        """A `store-mode` command and its JSON answer; a refusal is an answer, so it is returned.

        The exit status is under ``_exit``; an answer that is not JSON is under ``unparsed``, and
        a refusal's code, which the command prints as a ``VAULT_STORE_CODE: message`` stderr line,
        under ``code``.
        """
        done = self.cli(
            "store-mode", name, *arguments, "--vault", str(self.layout.vault), **options
        )
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            data = json.loads(lines[-1])
        except (IndexError, ValueError):
            data = {"unparsed": (done.stdout[-300:] + done.stderr[-300:]).strip()}
            for line in done.stderr.splitlines():
                # An earlier stderr line, such as a log warning, must not hide the refusal's own line.
                head, colon, _ = line.partition(":")
                if colon and head.startswith("VAULT_STORE_") and not any(c.isspace() for c in head):
                    data["code"] = head
                    break
        if not isinstance(data, dict):
            data = {"unparsed": str(data)[:300]}
        data["_exit"] = done.returncode
        return data

    def child(self, program: str, payload: Any = None, timeout: float = 900.0) -> Any:
        """Run ``program`` in an interpreter set up as the served process is; return its JSON."""
        done = subprocess.run(
            [self.python, "-c", program, str(self.layout.vault)],
            input=json.dumps(payload),
            env=dict(self.env),
            cwd=self.layout.work,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"a helper process failed ({done.returncode}): {done.stderr[-1500:]}"
            )
        return json.loads(done.stdout.strip().splitlines()[-1])

    def close(self) -> None:
        """Take the coordinator down; the run leaves nothing listening."""
        coordinator, self.coordinator = self.coordinator, None
        if coordinator is not None and coordinator.poll() is None:
            os.killpg(coordinator.pid, signal.SIGKILL)
            coordinator.wait(timeout=30)


class Served:
    """One served process, reached through the owner's local stdio door.

    The remote door needs an OAuth identity, so a rehearsal cannot use it. The stdio door runs the
    same server and lifecycle code with the owner as caller, and it listens on no port at all.
    """

    def __init__(self, rig: Rig, label: str) -> None:
        self.rig = rig
        rig.serves += 1
        self.label = f"{rig.serves:03d}-{label}"
        self.client: Any = None
        self.pid = 0
        self.listening: list[int] = []
        self.start_seconds = 0.0

    async def __aenter__(self) -> Served:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=self.rig.python,
            args=["-m", "exomem", "--transport", "stdio"],
            env=self.rig.env,
            cwd=str(self.rig.layout.work),
            keep_alive=False,
            log_file=self.rig.layout.logs / f"served-{self.label}.log",
        )
        self.client = Client(transport, timeout=REQUEST_TIMEOUT, init_timeout=START_TIMEOUT)
        started = time.monotonic()
        await asyncio.wait_for(self.client.__aenter__(), START_TIMEOUT + 30)
        self.start_seconds = round(time.monotonic() - started, 1)
        mine = [
            pid
            for pid in descendants(os.getpid())
            if b"--transport" in Path(f"/proc/{pid}/cmdline").read_bytes()
        ]
        if len(mine) != 1:
            raise RuntimeError(f"expected one served process, found {len(mine)}")
        self.pid = mine[0]
        self.listening = listening_ports([self.pid, *descendants(self.pid)])
        if set(self.listening) & set(LIVE_PORTS):
            raise Refusal(f"the served process listens on {self.listening}, a live port.")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.pid and Path(f"/proc/{self.pid}").exists():
            await self.stop()

    def alive(self) -> bool:
        return bool(self.pid) and Path(f"/proc/{self.pid}").exists()

    async def stop(self) -> dict[str, Any]:
        """Close the session as a client does, and say whether the process left in time."""
        started = time.monotonic()
        peak_mb = self.peak_rss_mb()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.client.__aexit__(None, None, None), 90)
        deadline = time.monotonic() + 30
        while self.alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        clean = not self.alive()
        if not clean:
            self.kill()
        return {
            "clean_exit": clean,
            "seconds": round(time.monotonic() - started, 1),
            "peak_rss_mb": peak_mb,
        }

    def peak_rss_mb(self) -> int | None:
        """The served process's resident-memory high-water mark so far, from the kernel."""
        with contextlib.suppress(OSError, ValueError, IndexError):
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) // 1024
        return None

    def kill(self) -> None:
        """SIGKILL the served process and everything below it, as `kill -9` of the unit does."""
        for pid in (*descendants(self.pid), self.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    async def discard(self) -> None:
        """Drop the client of a process that was killed."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.client.__aexit__(None, None, None), 30)


def body(result: Any) -> Any:
    """What a tool call returned, without the transport wrapper."""
    data = result.data if isinstance(result.data, dict) else result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


async def call(client: Any, name: str, arguments: dict[str, Any], *, key: str | None = None) -> Any:
    """One tool call. ``key`` is the call's replay identity, as a client that retries sends it."""
    meta = None
    if key is not None:
        from exomem.command_surface import MCP_IDEMPOTENCY_META_KEY

        meta = {MCP_IDEMPOTENCY_META_KEY: key}
    result = await asyncio.wait_for(
        client.call_tool(name, arguments, raise_on_error=False, meta=meta), REQUEST_TIMEOUT
    )
    return body(result)


#: The writer's own answer for an accepted write that is still executing. It carries no `retry`,
#: and the documented client step is to repeat the same call under the same identity.
PENDING_CODE = "MUTATION_ACKNOWLEDGEMENT_PENDING"
#: How long one write may be repeated for, and the pause when the refusal names none.
RETRY_BOUND_SECONDS = 900.0
RETRY_PAUSE = 2.0


def retry_pause(data: Any) -> float | None:
    """Seconds to wait before repeating an unchanged call, or None when no repeat should follow.

    A refusal names its own next step: `retry` holds the tool and what to change, and `after_s`
    is the wait it advises. Only a repeat that changes nothing keeps the call's identity.
    """
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    retry = error.get("retry")
    if isinstance(retry, dict) and not retry.get("arguments"):
        return min(float(retry.get("after_s") or RETRY_PAUSE), 30.0)
    return RETRY_PAUSE if error.get("code") == PENDING_CODE else None


async def call_write(client: Any, op: dict[str, Any]) -> tuple[Any, int]:
    """A write, repeated under its own identity for as long as the refusal asks for a repeat."""
    repeats = 0
    deadline = time.monotonic() + RETRY_BOUND_SECONDS
    while True:
        data = await call(client, op["tool"], op["arguments"], key=op["key"])
        pause = retry_pause(data)
        if pause is None or time.monotonic() > deadline:
            return data, repeats
        repeats += 1
        await asyncio.sleep(pause)


def refusal_code(data: Any) -> str | None:
    """The code of a refused call. The error shape is the product's own, not a word list."""
    if isinstance(data, dict):
        error = data.get("error") or (data.get("diagnostics") or {}).get("error")
        if isinstance(error, dict):
            return str(error.get("code"))
    return None


def describe(differences: Sequence[dict[str, Any]]) -> list[str]:
    return [
        f"table {d['table']}: {len(d['keys'])} key(s) differ, e.g. {', '.join(d['keys'][:5])}"
        for d in differences
    ]


def refusal_message(data: Any) -> str | None:
    """What a refused call said, so that the report shows the product's own words."""
    error = data.get("error") if isinstance(data, dict) else None
    return str(error.get("message"))[:240] if isinstance(error, dict) else None


def is_committed(data: Any) -> bool:
    operational = ((data or {}).get("diagnostics") or {}).get("operational") or {}
    return operational.get("state") == "committed" and operational.get("terminal") is True


async def await_warm(client: Any, bound: float = 240.0) -> float:
    """Wait until recall answers; a service that has just started refuses it for a while."""
    started = time.monotonic()
    while True:
        data = await call(client, "search_memory", {"query": "warm", "limit": 1})
        # RETRIEVAL_INDEX_WARMING is the product's own retry token (it names `retry`).
        if refusal_code(data) != "RETRIEVAL_INDEX_WARMING":
            return round(time.monotonic() - started, 1)
        if time.monotonic() - started > bound:
            raise TimeoutError(f"recall was still warming after {bound:.0f} s")
        await asyncio.sleep(1.0)


# --- Helper programs: run in an interpreter that is set up as the served process is -----------

#: The product's own search command with the graph lane on and off (`--graph`, `--no-graph`). The
#: served door does not expose the graph switch, so these run offline, in one process, through the
#: command line's entry point. A bare `commands.op_find` call on the same vault returned no hit at
#: all in a probe, so it would have compared two empty answers.
_QUERY_CHILD = """
import contextlib, io, json, sys
from exomem.__main__ import main
payload = json.loads(sys.stdin.read())
rows = []
for query in payload["queries"]:
    for graph in (True, False):
        row = {"query": query, "graph": graph}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["search_memory", query, "--limit", str(payload["limit"]),
                  "--graph" if graph else "--no-graph", "--detail", "full", "--json"])
        envelope = json.loads(buffer.getvalue().strip().splitlines()[-1])
        if envelope.get("success"):
            row["hits"] = [
                {"path": hit.get("path"), "signals": hit.get("signals")}
                for hit in envelope["data"].get("hits") or []
            ]
        else:
            row["error"] = str(envelope.get("error"))[:300]
        rows.append(row)
print(json.dumps(rows))
"""

#: The migrate verification again, on the live store: whole-table diff against what the files
#: derive, SQLite's own integrity check, and the store's size. A projection writes a file after the
#: commit that stored its page, so the file's modification time is never the stored one. The derived
#: copy takes the stored time first and the run counts the pages where the two differ.
_VERIFY_CHILD = """
import json, os, sqlite3, sys, tempfile
from pathlib import Path
from exomem.collection_store import connection
from exomem.vault_store import verify
verify.KEYS_REPORTED = 1_000_000  # every differing key, not the first five
vault = Path(sys.argv[1])
store = connection.store_path(vault)
with tempfile.TemporaryDirectory() as scratch:
    expected = Path(scratch) / "expected.sqlite"
    verify.derive_expected(vault, expected)
    # The product's reader registers the SQL functions that a collection's expression indexes call,
    # so the integrity check can evaluate them.
    conn = connection.open_reader(store)
    drift = 0
    try:
        stored = dict(conn.execute("SELECT path, mtime_ns FROM pages"))
        derived = sqlite3.connect(expected)
        for path, mtime in derived.execute("SELECT path, mtime_ns FROM pages").fetchall():
            if path in stored and stored[path] != mtime:
                drift += 1
                derived.execute(
                    "UPDATE pages SET mtime_ns = ? WHERE path = ?", (stored[path], path)
                )
        derived.commit()
        derived.close()
        comparison = verify.compare_tables(conn, expected)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        pages = conn.execute("SELECT COUNT(*) FROM store_pages").fetchone()[0]
    finally:
        conn.close()
size = sum(
    os.path.getsize(f"{store}{suffix}")
    for suffix in ("", "-wal") if os.path.exists(f"{store}{suffix}")
)
differences = [{"table": found.table, "keys": list(found.keys)} for found in comparison.differences]
print(json.dumps({
    "tables": len(comparison.tables), "differences": differences,
    "integrity": integrity, "pages": pages, "store_bytes": size, "mtime_differs_on": drift,
}))
"""

#: Every stored page and the hash of its stored bytes, to compare with the files after rollback.
_PAGES_CHILD = """
import json, sqlite3, sys
from pathlib import Path
from exomem.collection_store import connection
store = connection.store_path(Path(sys.argv[1]))
conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
try:
    rows = conn.execute("SELECT path, sha256 FROM store_pages ORDER BY path").fetchall()
finally:
    conn.close()
print(json.dumps(rows))
"""


# --- Answer cases ---------------------------------------------------------------------------

#: Fields that a rerun on the same bytes changes by design: a file's modification time and a
#: request's identity. A closed set of the product's own output fields, not a word list.
_VOLATILE = frozenset({"mtime", "request_id"})


def stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(stable(value), sort_keys=True, default=str).encode()
    ).hexdigest()


async def run_canary(client: Any, cases: Sequence[Any]) -> list[dict[str, Any]]:
    """Each canary case through the served door: the canary's verdict and a digest of the answer."""
    from exomem import recall_canary

    rows = []
    for case in cases:
        measured = await recall_canary.measure_call(client, case, REQUEST_TIMEOUT)
        answer = await call(client, case.tool, case.arguments)
        rows.append(
            {
                "case": case.id,
                "tool": case.tool,
                "outcome": measured.outcome,
                "failure": measured.failure,
                "ms": round(measured.elapsed_ms),
                "answer_sha256": digest(answer),
                "answer": stable(answer),
            }
        )
    return rows


def field_diff(first: Any, second: Any, path: str = "", limit: int = 6) -> list[dict[str, str]]:
    """The fields at which two answers differ, with short values."""
    found: list[dict[str, str]] = []
    if isinstance(first, dict) and isinstance(second, dict):
        for key in sorted(set(first) | set(second)):
            found += field_diff(first.get(key), second.get(key), f"{path}.{key}".lstrip("."), limit)
    elif isinstance(first, list) and isinstance(second, list) and len(first) == len(second):
        for index, (one, two) in enumerate(zip(first, second, strict=True)):
            found += field_diff(one, two, f"{path}[{index}]", limit)
    elif isinstance(first, str) and isinstance(second, str):
        if first != second:
            at = next(
                (i for i, (a, b) in enumerate(zip(first, second, strict=False)) if a != b),
                min(len(first), len(second)),
            )
            found.append(
                {
                    "field": path,
                    "first_differs_at": at,
                    "lengths": [len(first), len(second)],
                    "first": first[max(0, at - 30) : at + 50],
                    "second": second[max(0, at - 30) : at + 50],
                }
            )
    elif first != second:
        found.append({"field": path, "first": str(first)[:120], "second": str(second)[:120]})
    return found[:limit]


def slim(answers: dict[str, Any]) -> dict[str, Any]:
    """The answers without the kept bodies: the report holds digests; the comparison, fields."""
    return {
        **answers,
        **{
            name: [{k: v for k, v in row.items() if k != "answer"} for row in answers[name]]
            for name in ("canary", "canary_settled")
        },
    }


def compare_canary(first: Sequence[dict[str, Any]], second: Sequence[dict[str, Any]]) -> list[dict]:
    """Every case whose verdict or answer differs between two runs, with the fields that differ."""
    return [
        {
            "case": a["case"],
            "first_outcome": a["outcome"],
            "second_outcome": b["outcome"],
            "fields": field_diff(a["answer"], b["answer"]),
        }
        for a, b in zip(first, second, strict=True)
        if {k: v for k, v in a.items() if k != "ms"} != {k: v for k, v in b.items() if k != "ms"}
    ]


def compare_queries(
    first: Sequence[dict[str, Any]], second: Sequence[dict[str, Any]]
) -> list[dict]:
    """Every query whose top hits, their order or their signals differ between two runs."""
    found = []
    for a, b in zip(first, second, strict=True):
        if a == b:
            continue
        one, two = a.get("hits") or [], b.get("hits") or []
        paths = ([h["path"] for h in one], [h["path"] for h in two])
        if "error" in a or "error" in b:
            kind = "error"
        elif paths[0] != paths[1]:
            kind = "hits or order"
        else:
            kind = "signals"
        found.append(
            {
                "query": a["query"],
                "graph": a["graph"],
                "kind": kind,
                "first_paths": paths[0][:10],
                "second_paths": paths[1][:10],
                "first_error": a.get("error"),
                "second_error": b.get("error"),
            }
        )
    return found


# --- The write stream -------------------------------------------------------------------------


@dataclasses.dataclass
class Page:
    """A page that the stream made: where it is, which revision it holds, where its trash is."""

    path: str
    slug: str
    revision: int = 0
    trash: str | None = None


def first_paragraph(text: str) -> str:
    """The text of the first top-level paragraph of a page, read by the product's own parsers."""
    from markdown_it import MarkdownIt

    from exomem.vault import parse_frontmatter

    _, body, _ = parse_frontmatter(text)
    tokens = MarkdownIt("commonmark").parse(body)
    for index, token in enumerate(tokens):
        if token.type == "paragraph_open" and token.level == 0:
            return " ".join(tokens[index + 1].content.split())
    return ""


class Stream:
    """A seeded write stream through governed commands, with a model of what it acknowledged."""

    #: How often each kind of write comes up when the pages allow it. These are the driver's own
    #: operation kinds, not a vocabulary of the vault.
    WEIGHTS = (("create", 3), ("edit", 4), ("move", 1), ("trash", 2), ("recover", 2))

    def __init__(self, vault: Path, seed: int) -> None:
        self.vault = vault
        self.seed = seed
        self.planned = 0
        self.rng = random.Random(seed)
        knowledge = vault / "Knowledge Base"
        # Pages of the copy are the source of bodies and link targets; `_` folders
        # hold the product's own state.
        self.samples = sorted(
            str(path.relative_to(vault))
            for path in knowledge.rglob("*.md")
            if not any(part.startswith("_") for part in path.relative_to(knowledge).parts)
        )[:5000]
        self.pages: list[Page] = []
        self.made = 0

    def _sample(self) -> tuple[str, str]:
        """A paragraph and a link target taken from a page of the copy."""
        rel = self.rng.choice(self.samples)
        text = (self.vault / rel).read_text(encoding="utf-8", errors="replace")
        return first_paragraph(text)[:400], Path(rel).stem

    def plan(self) -> dict[str, Any]:
        live = [page for page in self.pages if page.trash is None]
        trashed = [page for page in self.pages if page.trash is not None]
        feasible = [
            (kind, weight)
            for kind, weight in self.WEIGHTS
            if kind == "create" or (kind == "recover" and trashed) or (kind != "recover" and live)
        ]
        kinds, weights = zip(*feasible, strict=True)
        kind = self.rng.choices(kinds, weights)[0]
        self.planned += 1
        op: dict[str, Any] = {
            "kind": kind,
            "page": None,
            "key": f"rehearsal-{self.seed}-{self.planned}",
        }
        if kind == "create":
            self.made += 1
            slug = f"rehearsal-{self.made:05d}"
            prose, stem = self._sample()
            content = (
                f"# Rehearsal page {self.made:05d}\n\n{prose}\n\n## Claim\n\n"
                f"Revision 0 of {slug}.\n\n## Related\n\n- see [[{stem}]]\n"
            )
            op["page"] = Page(path="", slug=slug)
            op["tool"], op["arguments"] = (
                "create_note",
                {
                    "title": f"Rehearsal page {self.made:05d}",
                    "content": content,
                    "unlinked_reason": "a page of the rehearsal stream",
                    "detail": "full",
                },
            )
        elif kind == "edit":
            page = self.rng.choice(live)
            op["page"] = page
            op["tool"], op["arguments"] = (
                "edit_note",
                {
                    "note": page.path,
                    "why": "rehearsal edit",
                    "action": "replace_text",
                    "old_text": f"Revision {page.revision} of {page.slug}.",
                    "new_text": f"Revision {page.revision + 1} of {page.slug}.",
                    "detail": "full",
                },
            )
        elif kind == "move":
            page = self.rng.choice(live)
            op["page"] = page
            # A compiled note keeps its valid destination when the stream renames it.
            op["new_path"] = str(Path(page.path).with_name(f"{page.slug}-moved-{self.planned}.md"))
            op["tool"], op["arguments"] = (
                "manage_memory",
                {
                    "operation": "files.move",
                    "detail": "full",
                    "arguments": {"old_path": page.path, "new_path": op["new_path"]},
                },
            )
        elif kind == "trash":
            page = self.rng.choice(live)
            op["page"] = page
            op["tool"], op["arguments"] = (
                "manage_memory",
                {
                    "operation": "files.delete",
                    "detail": "full",
                    "arguments": {"path": page.path, "confirm": True},
                },
            )
        else:
            page = self.rng.choice(trashed)
            op["page"] = page
            op["tool"], op["arguments"] = (
                "manage_memory",
                {
                    "operation": "files.recover",
                    "detail": "full",
                    "arguments": {"trash_path": page.trash},
                },
            )
        return op

    def settle(self, op: dict[str, Any], data: Any) -> str:
        """Record an answer in the model; return the outcome: committed, refused:CODE or unclear."""
        code = refusal_code(data)
        if code is not None:
            return f"refused:{code}"
        if not is_committed(data):
            return "unclear"
        diagnostics = data.get("diagnostics") or {}
        page: Page = op["page"]
        if op["kind"] == "create":
            page.path = str(diagnostics.get("path") or data.get("path") or "")
            self.pages.append(page)
        elif op["kind"] == "edit":
            page.revision += 1
        elif op["kind"] == "move":
            page.path = op["new_path"]
        elif op["kind"] == "trash":
            page.trash = diagnostics.get("trash_path")
        else:
            page.trash = None
        return "committed"

    def check(self, in_flight: dict[str, Any] | None) -> list[str]:
        """Compare the files with the model; the one write that the kill cut may sit either side.

        It adopts the observed state of that page, so the next trial starts from what is true.
        """
        cut = in_flight["page"] if in_flight else None
        wrong = []
        for page in list(self.pages):
            on_disk = (self.vault / page.path).is_file()
            text = (self.vault / page.path).read_text(encoding="utf-8") if on_disk else ""
            if page is cut:
                self._adopt(in_flight, on_disk, text)
                continue
            if page.trash is not None:
                if on_disk:
                    wrong.append(f"{page.path}: trashed page is still a file")
                elif not (self.vault / page.trash).is_file():
                    wrong.append(f"{page.trash}: trashed page is not in the trash")
            elif not on_disk:
                wrong.append(f"{page.path}: acknowledged page is missing")
            elif f"Revision {page.revision} of {page.slug}." not in text:
                wrong.append(
                    f"{page.path}: acknowledged revision {page.revision} is not in the file"
                )
        return wrong

    def _adopt(self, op: dict[str, Any] | None, on_disk: bool, text: str) -> None:
        page: Page = op["page"]
        if op["kind"] == "edit" and f"Revision {page.revision + 1} of {page.slug}." in text:
            page.revision += 1
        elif op["kind"] == "move" and (self.vault / op["new_path"]).is_file():
            page.path = op["new_path"]
        elif op["kind"] == "trash" and not on_disk:
            # The trash path of a cut delete is unknown; stop tracking the page.
            self.pages.remove(page)
        elif op["kind"] == "recover" and on_disk:
            page.trash = None


# --- The rehearsal ----------------------------------------------------------------------------

#: The refusal that the design records for the owner's recursive folder delete through the served
#: door in store mode. The run records it as a named expected skip and does not work around it.
EXPECTED_SKIP_CODE = "AUDIENCE_RESTRICTED"
#: The served log line for the start of a canonical commit (`log_active_mutation_phase`).
COMMIT_STARTED = b"phase=canonical_commit_started"
#: A commit-triggered kill lands this many seconds at most after that line.
COMMIT_OFFSET_WINDOW = 1.5


class Abort(Exception):
    """A step that the rest depends on failed; the remaining steps would mean nothing."""


def count_of(value: Any) -> int:
    """A count from a field that is a number, a list, or a mapping that holds a ``count``."""
    if isinstance(value, dict):
        return int(value.get("count") or 0)
    if isinstance(value, list):
        return len(value)
    return int(value or 0)


def vault_stats(vault: Path) -> dict[str, int]:
    files = size = 0
    for directory, _folders, names in os.walk(vault):
        for name in names:
            files += 1
            size += os.lstat(Path(directory, name)).st_size
    return {"files": files, "bytes": size}


def reap_served() -> int:
    """Kill any served process of this run that a failed start left behind."""
    killed = 0
    for pid in descendants(os.getpid()):
        with contextlib.suppress(OSError):
            if b"--transport" in Path(f"/proc/{pid}/cmdline").read_bytes():
                os.kill(pid, signal.SIGKILL)
                killed += 1
    return killed


class Rehearsal:
    def __init__(self, args: argparse.Namespace, rig: Rig, isolation: dict[str, Any]) -> None:
        from exomem import recall_canary

        self.args = args
        self.rig = rig
        self.layout = rig.layout
        self.cases = recall_canary.load_cases(Path(args.canary_cases).read_bytes())
        self.queries = [
            line
            for line in Path(args.query_cases).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.stream = Stream(self.layout.vault, args.seed)
        self.kill_rng = random.Random(args.seed + 1)
        self.steps: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "isolation": isolation,
            "options": {
                "kills": args.kills,
                "ops": args.ops,
                "seed": args.seed,
                "kill_window_seconds": args.kill_window,
                "drain_seconds": args.drain_seconds,
                "file_watcher": not args.no_file_watcher,
                "enrol": not args.skip_enrol and not args.adopt_replica,
                "adopt_replica": args.adopt_replica,
                "canary_cases": len(self.cases),
                "queries": len(self.queries),
            },
            "steps": self.steps,
        }

    @contextlib.contextmanager
    def step(self, name: str, *, fatal: bool = False) -> Iterator[dict[str, Any]]:
        entry: dict[str, Any] = {"step": name, "ok": None, "seconds": None}
        self.steps.append(entry)
        started = time.monotonic()
        try:
            yield entry
        except Exception as error:  # noqa: BLE001 - a failed step is a result; the run goes on
            entry["ok"] = False
            entry["error"] = f"{type(error).__name__}: {str(error)[:600]}"
            print(f"FAILED {name}: {entry['error']}", flush=True)
            if fatal:
                raise Abort(name) from error
        else:
            entry["ok"] = entry["ok"] if entry["ok"] is not None else True
        finally:
            entry["seconds"] = round(time.monotonic() - started, 1)
            print(f"step {name}: {entry['ok']} in {entry['seconds']} s", flush=True)

    async def answers(self, label: str) -> dict[str, Any]:
        """The canary cases through a served process, then the query cases (graph on and off)."""
        async with Served(self.rig, label) as served:
            warm = await await_warm(served.client)
            canary = await run_canary(served.client, self.cases)
            # The same cases again after a pause: an answer that differs only on the first pass
            # after a start belongs to a service that is not yet settled, not to the mode.
            await asyncio.sleep(self.args.settle_seconds)
            settled = await run_canary(served.client, self.cases)
            stop = await served.stop()
        started = time.monotonic()
        queries = await asyncio.to_thread(
            self.rig.child,
            _QUERY_CHILD,
            {"queries": self.queries, "limit": 10},
            self.args.long_step_seconds,
        )
        queries_seconds = round(time.monotonic() - started, 1)
        return {
            "start_seconds": served.start_seconds,
            "warm_seconds": warm,
            "served_listening_ports": served.listening,
            "stop": stop,
            "canary": canary,
            "canary_settled": settled,
            "queries": queries,
            "queries_seconds": queries_seconds,
        }

    def wait_drained(self) -> dict[str, Any]:
        """Poll the offline status until no projection is queued, within the stated bound."""
        started = time.monotonic()
        while True:
            status = self.rig.store_json("status")
            queued = status.get("queued_projections")
            elapsed = round(time.monotonic() - started, 1)
            if queued == 0:
                return {"drained": True, "seconds": elapsed, "status": status}
            if time.monotonic() - started > self.args.drain_seconds:
                return {"drained": False, "seconds": elapsed, "status": status}
            time.sleep(2.0)

    def wait_registry(self) -> dict[str, Any]:
        """Poll the offline status until the registry has applied every revision it targets."""
        started = time.monotonic()
        while True:
            status = self.rig.store_json("status")
            applied, target = (
                status.get("registry_revision"),
                status.get("registry_target_revision"),
            )
            elapsed = round(time.monotonic() - started, 1)
            settled = applied is not None and applied == target
            if settled or time.monotonic() - started > self.args.drain_seconds:
                return {
                    "settled": settled,
                    "seconds": elapsed,
                    "registry": status.get("registry"),
                    "applied_revision": applied,
                    "target_revision": target,
                }
            time.sleep(2.0)

    async def folder_trash(self, client: Any) -> dict[str, Any]:
        """Trash a folder of two pages through the door, and recover it if the door lets it."""
        folder = "Knowledge Base/Scratch/Rehearsal Folder"

        async def door(name: str, arguments: dict[str, Any]) -> Any:
            op = {
                "tool": "manage_memory",
                "key": f"rehearsal-folder-{name}",
                "arguments": {"detail": "full", **arguments},
            }
            return (await call_write(client, op))[0]

        for name in ("one", "two"):
            made = await door(
                f"create-{name}",
                {
                    "operation": "files.create",
                    "arguments": {
                        "path": f"{folder}/{name}.md",
                        "content": f"# {name}\n\nA page.\n",
                    },
                },
            )
            if not is_committed(made):
                return {"outcome": "setup failed", "code": refusal_code(made)}
        answer = await door(
            "delete",
            {
                "operation": "files.delete",
                "arguments": {"path": folder, "confirm": True, "recursive": True},
            },
        )
        code = refusal_code(answer)
        if code is not None:
            return {
                "outcome": "expected skip" if code == EXPECTED_SKIP_CODE else "refused",
                "code": code,
            }
        trash = (answer.get("diagnostics") or {}).get("trash_path")
        recovered = await door(
            "recover",
            {
                "operation": "files.recover",
                "arguments": {"trash_path": trash},
            },
        )
        return {"outcome": "trashed", "recovered": is_committed(recovered)}

    async def write_stream(self) -> dict[str, Any]:
        """The seeded stream through one served process, and a clean stop."""
        outcomes: dict[str, int] = {}
        ops = []
        async with Served(self.rig, "stream") as served:
            await await_warm(served.client)
            folder = await self.folder_trash(served.client)
            for number in range(1, self.args.ops + 1):
                op = self.stream.plan()
                started = time.monotonic()
                data, repeats = await call_write(served.client, op)
                outcome = self.stream.settle(op, data)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                ops.append(
                    {
                        "n": number,
                        "kind": op["kind"],
                        "outcome": outcome,
                        "repeats": repeats,
                        "seconds": round(time.monotonic() - started, 2),
                        "message": refusal_message(data),
                    }
                )
            stop = await served.stop()
        # The next start drains what a stop leaves queued, so this is read once and never waited
        # for. The files are compared with the model only when nothing is queued; otherwise the
        # first kill trial does it, after its restart has drained the queue.
        after = self.rig.store_json("status")
        queued = after.get("queued_projections")
        return {
            "ops": ops,
            "outcomes": outcomes,
            "folder_trash": folder,
            "stop": stop,
            "queued_after_clean_stop": queued,
            "oldest_projection_age_seconds": after.get("oldest_projection_age_seconds"),
            "registry_after_stream": {
                key: after.get(key)
                for key in ("registry", "registry_revision", "registry_target_revision")
            },
            "model_mismatches": (
                self.stream.check(None) if queued == 0 else "deferred: queue not empty"
            ),
        }

    def kill_window(self) -> float:
        """The span for a kill moment: the option, or one and a half median writes of the stream."""
        if self.args.kill_window is not None:
            return float(self.args.kill_window)
        seconds = sorted(op["seconds"] for op in self.report.get("stream", {}).get("ops", []))
        return max(3.0, 1.5 * seconds[len(seconds) // 2]) if seconds else 12.0

    async def _commit_started(self, mark: int, bound: float) -> bool:
        """Wait until a canonical commit starts in the served log after byte ``mark``."""
        log = self.layout.logs / "exomem.log"
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            if log.exists() and log.stat().st_size > mark:
                with log.open("rb") as handle:
                    handle.seek(mark)
                    # The product's own log token for the start of a canonical commit; a fixed
                    # protocol token, not a reading of anyone's words.
                    if COMMIT_STARTED in handle.read():
                        return True
            await asyncio.sleep(0.05)
        return False

    async def kill_trial(self, number: int, held_before: int) -> dict[str, Any]:
        """Write, SIGKILL the served process mid-stream, restart, drain, stop, then check."""
        window = self.kill_window()
        delay = self.kill_rng.uniform(1.0, max(1.5, window))
        offset = self.kill_rng.uniform(0.0, COMMIT_OFFSET_WINDOW)
        by_commit = number % 2 == 0
        trial: dict[str, Any] = {
            "trial": number,
            "trigger": "after a commit starts" if by_commit else "timer",
            "kill_after_seconds": round(offset if by_commit else delay, 2),
            "ops": [],
        }
        in_flight: dict[str, Any] | None = None
        served = await Served(self.rig, f"kill{number:02d}-write").__aenter__()
        timer: asyncio.Task[None] | None = None
        fired = False

        async def kill_later(mark: int) -> None:
            nonlocal fired
            if by_commit:
                trial["commit_seen"] = await self._commit_started(mark, 2 * window + 60)
                if not trial["commit_seen"]:
                    return
            await asyncio.sleep(offset if by_commit else delay)
            served.kill()
            fired = True

        try:
            await await_warm(served.client)
            for _ in range(self.args.max_ops_per_kill):
                op = self.stream.plan()
                try:
                    data, repeats = await call_write(served.client, op)
                except Exception:  # noqa: BLE001 - the process died under the call
                    in_flight = op
                    trial["in_flight"] = op["kind"]
                    break
                outcome = self.stream.settle(op, data)
                trial["ops"].append(
                    {
                        "kind": op["kind"],
                        "outcome": outcome,
                        "repeats": repeats,
                        "message": refusal_message(data),
                    }
                )
                if timer is not None and timer.done():
                    break
                # Recall can answer while the writer is warming. Arm after a committed write.
                if timer is None and outcome == "committed":
                    log = self.layout.logs / "exomem.log"
                    mark = log.stat().st_size if log.exists() else 0
                    timer = asyncio.create_task(kill_later(mark))
        finally:
            try:
                if timer is not None:
                    timer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await timer
            finally:
                served.kill()
                await served.discard()
        trial["trigger_fired"] = fired
        trial["acknowledged_before_kill"] = len(trial["ops"])
        recovered = await Served(self.rig, f"kill{number:02d}-recover").__aenter__()
        try:
            trial["restart_seconds"] = recovered.start_seconds
            trial["warm_seconds"] = await await_warm(recovered.client)
            trial["drain"] = await asyncio.to_thread(self.wait_drained)
        finally:
            trial["stop"] = await recovered.stop()
        status = self.rig.store_json("status")
        dry_run = self.rig.store_json("rollback", "--dry-run")
        verified = await asyncio.to_thread(
            self.rig.child, _VERIFY_CHILD, None, self.args.long_step_seconds
        )
        trial["status"] = {
            key: status.get(key)
            for key in (
                "mode",
                "pages",
                "revision",
                "queued_projections",
                "held",
                "held_by_kind",
                "unreadable",
                "registry",
                "registry_revision",
                "registry_target_revision",
                "registry_waiting_on_paths",
            )
        }
        trial["rollback_dry_run"] = {
            key: dry_run.get(key)
            for key in (
                "queued_projections",
                "held",
                "edited_on_disk",
                "deleted_on_disk",
                "refusal",
                "needs_export_held",
                "_exit",
                "unparsed",
            )
        }
        trial["verify"] = verified
        work_left = []
        if not trial["drain"]["drained"]:
            work_left.append("the projection queue did not drain within the bound")
        if count_of(status.get("held")) > held_before:
            work_left.append("the kill created a held page")
        if status.get("unreadable"):
            work_left.append("unreadable rows")
        if status.get("registry_waiting_on_paths"):
            work_left.append("the registry waits on paths")
        if dry_run.get("needs_export_held"):
            work_left.append("a rollback would need an export of held pages")
        wrong = describe(verified["differences"])
        if verified["integrity"] != "ok":
            wrong.append(f"integrity check: {verified['integrity']}")
        if count_of(dry_run.get("edited_on_disk")) or count_of(dry_run.get("deleted_on_disk")):
            wrong.append("a projection differs from the store's bytes")
        wrong += self.stream.check(in_flight)
        trial["left_work_for_a_person"] = work_left
        trial["wrong_after_recovery"] = wrong
        trial["held_after"] = count_of(status.get("held"))
        return trial

    async def run(self) -> None:
        args, rig, layout = self.args, self.rig, self.layout
        vault = ("--vault", str(layout.vault))
        with self.step("the run's lease coordinator starts", fatal=True):
            rig.start_coordinator()
        if args.adopt_replica:
            # A copy of an enrolled vault carries its store's replica; a new host adopts it.
            with self.step("adopt the collection store replica", fatal=True) as entry:
                why = ("--why", "adopt the vault copy's collection store replica for a rehearsal")
                preview = rig.cli_json(
                    "collections adopt-local --dry-run",
                    "collections",
                    "adopt-local",
                    "--dry-run",
                    *why,
                    *vault,
                    operator=True,
                )
                done = rig.cli_json(
                    "collections adopt-local",
                    "collections",
                    "adopt-local",
                    "--preview-id",
                    preview["preview_id"],
                    *why,
                    *vault,
                    operator=True,
                    wait_for_lease=True,
                )
                entry["detail"] = {"preview": preview.get("step"), "status": done.get("status")}
            if args.release_claim:
                # A copy of a vault in store mode names the holder's claim, and that holder is not
                # this run; the product's own remedy releases it as the offline owner.
                with self.step("release the copy's standing store claim", fatal=True) as entry:
                    named = rig.store_json("release", "--dry-run", operator=True)
                    claim = named.get("diagnostics", {}).get("claim")
                    if named.get("code") == "VAULT_STORE_RELEASE_NOTHING_HELD":
                        # A copy of a file-mode vault, or of a released one, holds no claim.
                        entry["detail"] = {"claim": None, "status": "nothing held"}
                    else:
                        if named["_exit"] != 0 or not claim:
                            raise RuntimeError(f"the release dry run named no claim: {named}")
                        released = rig.store_json(
                            "release",
                            "--apply",
                            "--claim",
                            claim,
                            "--why",
                            "release a vault copy's store claim for a rehearsal",
                            operator=True,
                            wait_for_lease=True,
                        )
                        if released["_exit"] != 0:
                            raise RuntimeError(f"the release refused: {released}")
                        entry["detail"] = {"claim": claim, "status": released.get("status")}
        elif not args.skip_enrol:
            with self.step("enrol the collection store", fatal=True) as entry:
                manifest = layout.work / "store-enrolment.md"
                manifest.write_text(loop._STORE_ENROLMENT_MANIFEST, encoding="utf-8")
                done = rig.cli_json(
                    "collections create",
                    "collections",
                    "create",
                    "--manifest-path",
                    loop._STORE_ENROLMENT_PATH,
                    "--manifest-file",
                    str(manifest),
                    "--why",
                    "enrol the collection store for a rehearsal",
                    *vault,
                    operator=True,
                    wait_for_lease=True,
                )
                entry["detail"] = done.get("status")
        with self.step("build the sidecars: exomem maintain --reconcile", fatal=True):
            rig.cli_json(
                "maintain --reconcile",
                "maintain",
                "--reconcile",
                "--json",
                timeout=self.args.long_step_seconds,
            )
        self.report["vault"] = vault_stats(layout.vault)
        with self.step("store-mode status before the switch", fatal=True) as entry:
            status = rig.store_json("status")
            entry["detail"] = {k: status.get(k) for k in ("mode", "pages", "binaries")}
        with self.step("file mode: serve, run the answer cases, stop", fatal=True) as entry:
            baseline = await self.answers("file-baseline")
            self.report["file_baseline"] = slim(baseline)
            entry["detail"] = {
                "canary": len(baseline["canary"]),
                "queries": len(baseline["queries"]),
            }
        with self.step("store-mode migrate --dry-run", fatal=True) as entry:
            dry = rig.store_json("migrate", "--dry-run")
            entry["detail"] = dry
            self.report["pages_before"] = dry.get("pages")
            if dry.get("_exit") != 0:
                raise RuntimeError(f"the dry run refused: {dry}")
        with self.step("store-mode migrate --apply", fatal=True) as entry:
            applied = rig.store_json(
                "migrate",
                "--apply",
                "--why",
                "rehearse the switch on a vault copy",
                operator=True,
                wait_for_lease=True,
                timeout=self.args.long_step_seconds,
            )
            entry["detail"] = applied
            if applied.get("status") != "migrated":
                raise RuntimeError(f"migrate did not report migrated: {applied}")
        with self.step(
            "store mode: the whole-table diff and the store's size", fatal=True
        ) as entry:
            checked = self.report["after_migrate"] = await asyncio.to_thread(
                rig.child, _VERIFY_CHILD, None, self.args.long_step_seconds
            )
            entry["detail"] = checked
            entry["ok"] = not checked["differences"] and checked["integrity"] == "ok"
        with self.step("store mode: serve, run the answer cases, stop", fatal=True) as entry:
            migrated = await self.answers("store-after-migrate")
            self.report["store_after_migrate"] = slim(migrated)
            self.report["file_vs_store"] = {
                "canary": compare_canary(baseline["canary"], migrated["canary"]),
                "canary_settled": compare_canary(
                    baseline["canary_settled"], migrated["canary_settled"]
                ),
                "queries": compare_queries(baseline["queries"], migrated["queries"]),
            }
            entry["detail"] = {k: len(v) for k, v in self.report["file_vs_store"].items()}
        with self.step(f"write stream of {args.ops} operations through the served door") as entry:
            self.report["stream"] = await self.write_stream()
            entry["detail"] = self.report["stream"]["outcomes"]
        trials: list[dict[str, Any]] = []
        self.report["kills"] = {"requested": args.kills, "trials": trials}
        held = count_of(rig.store_json("status").get("held"))
        for number in range(1, args.kills + 1):
            with self.step(f"kill {number} of {args.kills}") as entry:
                trial = await self.kill_trial(number, held)
                trials.append(trial)
                held = trial["held_after"]
                entry["detail"] = {
                    "left_work": len(trial["left_work_for_a_person"]),
                    "wrong": len(trial["wrong_after_recovery"]),
                }
                if not trial["trigger_fired"]:
                    entry["error"] = f"the {trial['trigger']} trigger did not fire"
                entry["ok"] = trial["trigger_fired"] and not (
                    trial["left_work_for_a_person"] or trial["wrong_after_recovery"]
                )
            reap_served()
        with self.step("the registry catches up while a service runs") as entry:
            async with Served(rig, "registry") as served:
                await await_warm(served.client)
                self.report["registry"] = entry["detail"] = await asyncio.to_thread(
                    self.wait_registry
                )
                await served.stop()
            reap_served()
        with self.step("store mode after the stream: serve, run the answer cases, stop") as entry:
            after = await self.answers("store-after-stream")
            self.report["store_after_stream"] = slim(after)
            entry["detail"] = {"canary": len(after["canary"]), "queries": len(after["queries"])}
        with self.step("rollback --dry-run") as entry:
            self.report["rollback_dry_run"] = entry["detail"] = rig.store_json(
                "rollback", "--dry-run"
            )
        with self.step("rollback --apply --export-to", fatal=True) as entry:
            stored = await asyncio.to_thread(rig.child, _PAGES_CHILD)
            applied = rig.store_json(
                "rollback",
                "--apply",
                "--export-to",
                str(layout.export),
                "--why",
                "rehearse the way back on a vault copy",
                operator=True,
                wait_for_lease=True,
                timeout=self.args.long_step_seconds,
            )
            entry["detail"] = applied
            self.report["rollback_apply"] = applied
            if applied.get("_exit") != 0:
                raise RuntimeError(f"rollback refused: {applied}")
            differing = [
                path
                for path, sha in stored
                if not (layout.vault / path).is_file()
                or hashlib.sha256((layout.vault / path).read_bytes()).hexdigest() != sha
            ]
            self.report["rollback_pages"] = {
                "stored": len(stored),
                "files_differing": len(differing),
            }
            entry["ok"] = not differing
        with self.step("file mode again: serve, run the answer cases, stop") as entry:
            again = await self.answers("file-after-rollback")
            self.report["file_after_rollback"] = slim(again)
            self.report["store_vs_file_again"] = {
                "canary": compare_canary(after["canary"], again["canary"]),
                "canary_settled": compare_canary(after["canary_settled"], again["canary_settled"]),
                "queries": compare_queries(after["queries"], again["queries"]),
            }
            entry["detail"] = {k: len(v) for k, v in self.report["store_vs_file_again"].items()}

    def coverage(self) -> dict[str, int]:
        """How much the answer comparison covered, so that a comparison of nothing shows."""
        baseline = self.report.get("file_baseline", {})
        canary, queries = baseline.get("canary", []), baseline.get("queries", [])
        return {
            "canary_cases": len(canary),
            "canary_cases_meeting_their_expectation_in_file_mode": sum(
                row["failure"] is None for row in canary
            ),
            "query_runs": len(queries),
            "queries_with_hits": sum(bool(row.get("hits")) for row in queries),
            "queries_that_errored": sum("error" in row for row in queries),
        }

    def conclude(self) -> dict[str, Any]:
        """Totals for the summary, computed from the report and never typed in."""
        trials = self.report.get("kills", {}).get("trials", [])
        differences = [
            len(self.report.get(name, {}).get(kind, []))
            for name in ("file_vs_store", "store_vs_file_again")
            for kind in ("canary", "canary_settled", "queries")
        ]
        failed = [s["step"] for s in self.steps if s["ok"] is False]
        totals = {
            "steps": len(self.steps),
            "steps_failed": len(failed),
            "failed_steps": failed,
            "kills_requested": self.args.kills,
            "kills_run": len(trials),
            "kills_that_left_work": sum(bool(t["left_work_for_a_person"]) for t in trials),
            "kills_with_a_wrong_state": sum(bool(t["wrong_after_recovery"]) for t in trials),
            "answer_differences": sum(differences),
            **self.coverage(),
            "registry_settled": self.report.get("registry", {}).get("settled"),
        }
        totals["verdict"] = (
            "clean"
            if not failed
            and not totals["kills_that_left_work"]
            and not totals["kills_with_a_wrong_state"]
            and not totals["answer_differences"]
            and totals["queries_with_hits"]  # equal empty answers prove nothing
            and totals["registry_settled"]
            and totals["kills_run"] == self.args.kills
            else "findings"
        )
        return totals


# --- Report -----------------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else None


def summary_markdown(report: dict[str, Any], totals: dict[str, Any]) -> str:
    """The summary. Its opening section holds counts only, so it is safe to quote."""
    migrate = next((s for s in report["steps"] if s["step"] == "store-mode migrate --apply"), {})
    lines = [
        "# Store-mode rehearsal",
        "",
        "## Counts (safe to quote)",
        "",
        f"- Verdict: {totals['verdict']}",
        f"- Pages in the vault copy before the switch: {report.get('pages_before')}",
        f"- Steps run: {totals['steps']}; failed: {totals['steps_failed']}",
        f"- Kills: {totals['kills_run']} run of {totals['kills_requested']} requested",
        f"- Kills that left work for a person: {totals['kills_that_left_work']}",
        f"- Kills that left a state that is still wrong: {totals['kills_with_a_wrong_state']}",
        f"- Answer differences between modes on the same bytes: {totals['answer_differences']}",
        f"- Answers compared: {totals['canary_cases']} canary cases (read twice; "
        f"{totals['canary_cases_meeting_their_expectation_in_file_mode']} meet their own "
        f"expectation in file mode) and {totals['query_runs']} query runs "
        f"({totals['queries_with_hits']} with hits, {totals['queries_that_errored']} refused)",
        f"- Writes in the first stream: {len(report.get('stream', {}).get('ops', []))}; "
        f"outcomes {report.get('stream', {}).get('outcomes')}; median seconds per write "
        f"{_median([o['seconds'] for o in report.get('stream', {}).get('ops', [])])}",
        f"- Projections queued after the stream's clean stop: "
        f"{report.get('stream', {}).get('queued_after_clean_stop')}",
        f"- Registry applied every targeted revision while a service ran: "
        f"{report.get('registry', {}).get('settled')} "
        f"(applied {report.get('registry', {}).get('applied_revision')} of "
        f"{report.get('registry', {}).get('target_revision')}, "
        f"{report.get('registry', {}).get('seconds')} s)",
        f"- Vault copy: {report.get('vault', {}).get('files')} files, "
        f"{report.get('vault', {}).get('bytes')} bytes",
        f"- Store after migration: {report.get('after_migrate', {}).get('store_bytes')} bytes",
        f"- `migrate --apply`: {migrate.get('seconds')} s",
        "",
        "## Steps",
        "",
        *_table(
            ("step", "ok", "seconds"),
            [(s["step"], s["ok"], s["seconds"]) for s in report["steps"]],
        ),
        "",
        "## Kills",
        "",
        *_table(
            (
                "kill",
                "trigger",
                "trigger fired",
                "after (s)",
                "acknowledged",
                "cut write",
                "restart (s)",
                "drain (s)",
                "left work",
                "wrong",
            ),
            [
                (
                    t["trial"],
                    t["trigger"],
                    t["trigger_fired"],
                    t["kill_after_seconds"],
                    t["acknowledged_before_kill"],
                    t.get("in_flight", "none"),
                    t.get("restart_seconds"),
                    t.get("drain", {}).get("seconds"),
                    len(t["left_work_for_a_person"]),
                    len(t["wrong_after_recovery"]),
                )
                for t in report.get("kills", {}).get("trials", [])
            ],
        ),
        "",
        "## What the run found (may hold page paths of the vault copy)",
        "",
    ]
    found = False
    for name in ("file_vs_store", "store_vs_file_again"):
        for kind, rows in report.get(name, {}).items():
            for row in rows:
                found = True
                lines.append(f"- {name} {kind}: {json.dumps(row)[:400]}")
    for t in report.get("kills", {}).get("trials", []):
        for item in (*t["left_work_for_a_person"], *t["wrong_after_recovery"]):
            found = True
            lines.append(f"- kill {t['trial']}: {item}")
    for entry in report["steps"]:
        if entry["ok"] is False and "error" in entry:
            found = True
            lines.append(f"- step failed, {entry['step']}: {entry['error']}")
    folder = report.get("stream", {}).get("folder_trash")
    if folder:
        lines.append(f"- folder trash through the door: {json.dumps(folder)}")
    if not found:
        lines.append("- nothing")
    return "\n".join(lines) + "\n"


# --- Synthetic vault --------------------------------------------------------------------------


def _words(rng: random.Random, count: int) -> list[str]:
    consonants, vowels = "bcdfghjklmnprstvz", "aeiou"
    return [
        "".join(rng.choice(consonants) + rng.choice(vowels) for _ in range(3)) for _ in range(count)
    ]


def synthesise(args: argparse.Namespace) -> int:
    """Write a vault of invented pages and the two case files that fit it."""
    layout = Layout(
        vault=_resolved(args.vault), scratch=_resolved(args.scratch), out=_resolved(args.out)
    )
    never = [Path(p) for p in args.never]
    refuse_overlaps(layout.writes(), never)
    layout.vault.mkdir(parents=True, exist_ok=True)
    settings = Settings(free_port(), secrets.token_hex(16), secrets.token_hex(16))
    preflight(layout, never, settings, python=args.python)
    layout.out.mkdir(parents=True, exist_ok=True)
    rig = Rig(layout, settings, python=args.python)
    adopt_isolation(rig)
    from markdown_it import MarkdownIt

    from exomem.vault import parse_frontmatter

    made = rig.cli("init", "--vault", str(layout.vault))
    if made.returncode != 0:
        raise Refusal(f"`exomem init` failed: {made.stderr[-500:]}")
    rng = random.Random(args.seed)
    topics = [_words(rng, 30) for _ in range(25)]
    shared = _words(rng, 200)
    rels = synth_vault.gen_dense_vault(
        layout.vault,
        args.pages,
        links_per_note=5,
        seed=args.seed,
        sources=4,
        relations=True,
        ids=True,
    )
    facts = []
    for index, rel in enumerate(rels):
        path = layout.vault / rel
        words = rng.sample(topics[index % len(topics)], 14) + rng.sample(shared, 6)
        rng.shuffle(words)
        text = path.read_text(encoding="utf-8")
        front, body, _ = parse_frontmatter(text)
        # The generator's one top-level paragraph is the filler that the topic words replace.
        paragraph = next(
            token
            for token in MarkdownIt("commonmark").parse(body)
            if token.type == "paragraph_open" and token.level == 0
        )
        start, end = paragraph.map
        body_lines = body.splitlines()
        body_lines[start:end] = [" ".join(words) + "."]
        path.write_text(
            text[: len(text) - len(body)] + "\n".join(body_lines) + "\n", encoding="utf-8"
        )
        facts.append((rel, front["title"], front["exomem_id"]))
    cases = []
    for number, (rel, title, ident) in enumerate(rng.sample(facts, 8), start=1):
        cases.append(
            {
                "id": f"search-{number}",
                "tool": "search_memory",
                "arguments": {"query": title, "limit": 5, "options": {"mode": "keyword"}},
                "expected": {
                    "state_path": ["hits", 0, "title"],
                    "state": title,
                    "outcome": "ok",
                    "references": [
                        {"path": ["hits", 0, "ref"], "value": f"exomem://memory/{ident}"}
                    ],
                    "material_paths": [["hits", 0, "excerpt"]],
                },
            }
        )
        cases.append(
            {
                "id": f"read-{number}",
                "tool": "read_page",
                "arguments": {"page": rel},
                "expected": {
                    "state_path": ["frontmatter", "title"],
                    "state": title,
                    "outcome": "ok",
                    "references": [{"path": ["path"], "value": rel}],
                    "material_paths": [["body"]],
                },
            }
        )
    queries = [" ".join(rng.sample(rng.choice(topics), rng.choice((2, 3)))) for _ in range(40)]
    queries += [
        " ".join((*rng.sample(topics[a], 1), *rng.sample(topics[b], 1)))
        for a, b in (rng.sample(range(len(topics)), 2) for _ in range(10))
    ]
    (layout.out / "canary-cases.json").write_text(json.dumps(cases, indent=1), encoding="utf-8")
    (layout.out / "queries.txt").write_text("\n".join(queries) + "\n", encoding="utf-8")
    print(f"wrote {len(rels)} pages, {len(cases)} canary cases, {len(queries)} queries")
    return 0


# --- Command line -----------------------------------------------------------------------------


def adopt_isolation(rig: Rig) -> None:
    """Give this process the children's environment, before it imports any product code.

    What it imports then resolves nothing outside the scratch root, and writes no bytecode into
    the interpreter's own install (the children get ``PYTHONDONTWRITEBYTECODE`` the same way).
    """
    os.environ.clear()
    os.environ.update(rig.env)
    sys.dont_write_bytecode = True


def run_command(args: argparse.Namespace) -> int:
    if args.release_claim and not args.adopt_replica:
        raise SystemExit(
            "--release-claim releases the claim in an adopted replica: add --adopt-replica"
        )
    layout = Layout(
        vault=_resolved(args.vault), scratch=_resolved(args.scratch), out=_resolved(args.out)
    )
    never = [Path(p) for p in args.never]
    refuse_overlaps(
        {
            **layout.writes(),
            "canary cases": Path(args.canary_cases),
            "query cases": Path(args.query_cases),
        },
        never,
    )
    settings = Settings(
        args.coordinator_port or free_port(), secrets.token_hex(16), secrets.token_hex(16)
    )
    isolation = preflight(layout, never, settings, python=args.python)
    layout.out.mkdir(parents=True, exist_ok=True)
    rig = Rig(layout, settings, python=args.python, file_watcher=not args.no_file_watcher)
    adopt_isolation(rig)
    rehearsal = Rehearsal(args, rig, isolation)
    try:
        asyncio.run(rehearsal.run())
    except Abort as stopped:
        print(f"stopped at: {stopped}", flush=True)
    finally:
        reap_served()
        rig.close()
        totals = rehearsal.conclude()
        rehearsal.report["totals"] = totals
        (layout.out / "report.json").write_text(
            json.dumps(rehearsal.report, indent=1, default=str), encoding="utf-8"
        )
        (layout.out / "summary.md").write_text(
            summary_markdown(rehearsal.report, totals), encoding="utf-8"
        )
    print(json.dumps(totals))
    return 0 if totals["verdict"] == "clean" else 1


#: `synth` samples eight pages for its answer cases, so fewer pages cannot make them.
MIN_PAGES = 8


def at_least(floor: int) -> Callable[[str], int]:
    """An argument type: a whole number of at least ``floor``, else a refusal that says so."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if value < floor:
            raise argparse.ArgumentTypeError(f"{value} is below the least allowed, {floor}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--vault", required=True, help="the vault copy (never the original)")
        command.add_argument("--scratch", required=True, help="a new or empty directory")
        command.add_argument("--out", required=True, help="where the report goes")
        command.add_argument(
            "--never",
            action="append",
            default=[],
            metavar="PATH",
            help="a path the run must never touch; repeatable; at least one is required",
        )
        command.add_argument(
            "--python", default=sys.executable, help="interpreter that holds exomem"
        )

    run = sub.add_parser("run", help="rehearse the switch on the vault copy")
    common(run)
    run.add_argument("--canary-cases", required=True, help="a recall canary --cases file")
    run.add_argument("--query-cases", required=True, help="one query per line")
    run.add_argument(
        "--kills", type=at_least(1), default=20, help="SIGKILL trials, at least 1 (default 20)"
    )
    run.add_argument("--ops", type=int, default=40, help="writes in the first stream (default 40)")
    run.add_argument("--seed", type=int, default=7, help="seeds the stream and the kill moments")
    run.add_argument(
        "--long-step-seconds",
        type=at_least(60),
        default=LONG_STEP_SECONDS,
        metavar="SECONDS",
        help="the bound on a step that reads or rebuilds the whole vault (default "
        f"{LONG_STEP_SECONDS:.0f})",
    )
    run.add_argument(
        "--kill-window",
        type=float,
        default=None,
        metavar="SECONDS",
        help="a timer kill lands within this many seconds (default: one and a half "
        "median writes of the stream, at least 3)",
    )
    run.add_argument(
        "--settle-seconds",
        type=float,
        default=20.0,
        help="pause before the second pass of the canary cases (default 20)",
    )
    run.add_argument("--max-ops-per-kill", type=int, default=60)
    run.add_argument(
        "--drain-seconds",
        type=float,
        default=120.0,
        help="how long a restart may take to drain the projection queue (default 120)",
    )
    run.add_argument("--coordinator-port", type=int, default=0, help="default: a free port")
    run.add_argument(
        "--no-file-watcher",
        action="store_true",
        help="serve with the file watcher off, as the installed-wheel journey does",
    )
    run.add_argument(
        "--skip-enrol",
        action="store_true",
        help="the run's state root already holds the vault's collection store",
    )
    run.add_argument(
        "--adopt-replica",
        action="store_true",
        help="the copy is of a vault whose collection store lives outside it; adopt the replica "
        "the copy carries, as a new host does, instead of enrolling a new store",
    )
    run.add_argument(
        "--release-claim",
        action="store_true",
        help="with --adopt-replica: the copy's replica names a store-mode claim whose holder is "
        "not this run (a copy of a vault in store mode, or an earlier run that stopped after "
        "its migrate); release it with `store-mode release` before the file-mode baseline",
    )
    run.set_defaults(handler=run_command)

    synth = sub.add_parser("synth", help="write a vault of invented pages and case files")
    common(synth)
    synth.add_argument(
        "--pages", type=at_least(MIN_PAGES), default=1000, help=f"at least {MIN_PAGES}"
    )
    synth.add_argument("--seed", type=int, default=7)
    synth.set_defaults(handler=synthesise)
    return parser


def require_linux() -> None:
    """The driver finds, counts and stops its processes through /proc, which only Linux has."""
    if sys.platform != "linux":
        raise Refusal(
            f"the driver reads /proc to find and stop its processes, so it runs on Linux only "
            f"(this platform is {sys.platform}). Run it on Linux against a copy of the vault."
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_linux()
        return int(args.handler(args))
    except Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
