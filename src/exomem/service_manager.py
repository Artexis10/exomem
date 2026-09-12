"""Linux managed-service lifecycle; public requests are forwarded by the ingress.

This module owns processes and deployment records, never vault or OAuth stores.
The supported daemon runs as a verified systemd user-unit MainPID.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def private_directory(path: Path) -> Path:
    """Create an owner-only directory without following its final symlink."""
    path = Path(os.path.abspath(path))
    if path.is_symlink():
        raise ValueError("managed directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("managed directory must be private to its owner (0700)")
    return path


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ReleaseRecords:
    """Atomic, content-free deployment records outside the vault."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.directory / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                raise ValueError("deployment record must be private and bounded")
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("invalid deployment record")
        return value

    def _write(self, name: str, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / name)
            _sync_directory(self.directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def active(self) -> dict[str, Any] | None:
        return self._read("active.json")

    def pending(self) -> dict[str, Any] | None:
        return self._read("transition.json")

    def begin(
        self, target: dict[str, Any], *, worker_pid: int, identity: dict[str, Any] | None = None
    ) -> None:
        if self.pending() is not None:
            raise RuntimeError("an unfinished transition already exists; resume it")
        self._write(
            "transition.json",
            {
                "target": target,
                "worker_pid": worker_pid,
                "phase": "drained",
                "identity": identity or {},
            },
        )

    def phase(self, phase: str, **fields: Any) -> None:
        pending = self.pending()
        if pending is None:
            raise RuntimeError("no transition to update")
        self._write("transition.json", {**pending, **fields, "phase": phase})

    def accept(self, target: dict[str, Any]) -> None:
        # Active is published first. A crash before receipt removal requires
        # explicit roll-forward; it never silently selects the prior release.
        self._write("active.json", target)
        (self.directory / "transition.json").unlink(missing_ok=True)
        _sync_directory(self.directory)


@contextmanager
def deployment_lock(directory: Path) -> Iterator[None]:
    import fcntl

    descriptor = os.open(directory / "manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("deployment lock must be owner-only")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("a managed supervisor already holds the deployment lock") from error
        yield
    finally:
        os.close(descriptor)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("deadline must be a positive finite number")
        self.clock = clock
        self.ends = clock() + seconds

    def remaining(self, maximum: float) -> float:
        remaining = min(maximum, self.ends - self.clock())
        if remaining <= 0:
            raise TimeoutError("managed upgrade deadline expired")
        return remaining


def _descendants() -> dict[int, tuple[int, str]]:
    """Read the current descendant tree, including subreaper-adopted children."""
    processes: dict[int, tuple[int, str]] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            processes[int(path.name)] = (int(fields[1]), fields[0])
        except (FileNotFoundError, ProcessLookupError):
            continue
    owned: dict[int, tuple[int, str]] = {}
    parents = {os.getpid()}
    while True:
        new = {
            pid for pid, (parent, _) in processes.items() if parent in parents and pid not in owned
        }
        if not new:
            return owned
        owned.update({pid: processes[pid] for pid in new})
        parents = new


async def stop_owned_process(
    child: asyncio.subprocess.Process,
    *,
    timeout: float,
    include_adopted: bool = False,
) -> None:
    """Stop an owned process tree and require observed exit before returning.

    ``include_adopted`` is only for the single-purpose daemon after enabling
    Linux subreaper mode. A caller must never use it in a shared application.
    """
    deadline = Deadline(timeout)
    signalled: set[int] = set()
    if child.returncode is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    while True:
        owned = _descendants() if include_adopted else {}
        for pid, (_, state) in owned.items():
            if pid == child.pid:
                continue  # asyncio owns reaping this direct child
            if state == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            elif pid not in signalled:
                try:
                    os.kill(pid, signal.SIGTERM)
                    signalled.add(pid)
                except ProcessLookupError:
                    pass
        group_alive = False
        try:
            os.killpg(child.pid, 0)
            group_alive = True
        except ProcessLookupError:
            pass
        if (
            child.returncode is not None
            and not group_alive
            and not _live_descendants(include_adopted)
        ):
            return
        await asyncio.sleep(min(0.025, deadline.remaining(0.025)))


def _live_descendants(include_adopted: bool) -> bool:
    return bool(_descendants()) if include_adopted else False


class Supervisor:
    """Serialize replacement while the ingress retains client connections."""

    def __init__(
        self,
        directory: Path,
        *,
        initial_target: dict[str, Any],
        ingress: Any,
        runtime: Any,
        identity: dict[str, Any],
        transition_timeout: float = 40,
    ):
        self.records = ReleaseRecords(directory)
        self.initial_target = initial_target
        self.ingress = ingress
        self.runtime = runtime
        self.identity = identity
        self.transition_timeout = transition_timeout
        self.lock = asyncio.Lock()
        self.phase = "unavailable"
        self.transition_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.records.pending() is not None:
            self.phase = "recovery-required"
            self.ingress.unavailable()
            return
        target = self.records.active() or self.initial_target
        target = await self.runtime.inspect(target)
        client = await self.runtime.start(target, timeout=120)
        self.records.accept(target)
        self.ingress.resume(client)
        self.phase = "ready"

    def status(self) -> dict[str, Any]:
        import sys

        return {
            "ok": True,
            "phase": self.phase,
            "active": self.records.active(),
            "pending": self.records.pending(),
            "worker_pid": self.runtime.pid,
            "launcher_python": sys.executable,
            "unit": self.identity.get("unit"),
            "ingress": self.ingress.stats,
            "port": getattr(self.runtime, "port", None),
        }

    async def upgrade(
        self, target: dict[str, Any] | None, *, resume: bool = False
    ) -> dict[str, Any]:
        if self.lock.locked():
            return {"ok": False, "error": "an upgrade is already in progress"}
        async with self.lock:
            pending = self.records.pending()
            if bool(pending) != resume:
                return {
                    "ok": False,
                    "error": "resume the recorded transition"
                    if pending
                    else "no transition to resume",
                }
            if resume:
                target = pending["target"]
            try:
                target = await self.runtime.inspect(target)
            except Exception:  # noqa: BLE001 - reject candidate failures before changing admission
                return {
                    "ok": False,
                    "error": "target interpreter verification failed; current admission unchanged",
                }
            deadline = Deadline(self.transition_timeout)
            self.phase = "upgrading"
            self.ingress.pause()
            if not resume:
                try:
                    drained = await self.ingress.drain(deadline.remaining(30))
                except TimeoutError:
                    drained = False
                if not drained:
                    self.ingress.resume()
                    self.phase = "ready"
                    return {
                        "ok": False,
                        "error": "active requests exceeded the drain budget; current worker is still serving",
                    }
                try:
                    self.records.begin(target, worker_pid=self.runtime.pid, identity=self.identity)
                except OSError:
                    self.ingress.resume()
                    self.phase = "ready"
                    return {
                        "ok": False,
                        "error": "could not record the transition; current worker is still serving",
                    }
            try:
                async with asyncio.timeout(deadline.remaining(40)):
                    await self.ingress.detach_streams()
                    # Resume always repeats the stop proof, including a timed-out
                    # migrator or failed candidate retained by this supervisor.
                    self.records.phase("stopping")
                    await self.runtime.stop(timeout=deadline.remaining(10))
                    self.records.phase("migrating", worker_pid=0)
                    await self.runtime.migrate(target, timeout=deadline.remaining(15))
                    self.records.phase("starting", worker_pid=0)
                    client = await self.runtime.start(target, timeout=deadline.remaining(30))
                    self.records.phase("ready", worker_pid=self.runtime.pid)
                    self.records.accept(target)
                    self.ingress.resume(client)
                    self.phase = "ready"
                    return {
                        "ok": True,
                        "phase": "ready",
                        "active": target,
                        "worker_pid": self.runtime.pid,
                    }
            except Exception:  # noqa: BLE001 - every post-stop failure must retain recovery state
                self.phase = "recovery-required"
                self.ingress.unavailable()
                # Error text from subprocesses can contain configuration or
                # vault content. Retain phase and identity, not arbitrary text.
                self.records.phase("failed", worker_pid=self.runtime.pid)
                try:
                    await self.runtime.stop(timeout=5)
                except Exception:  # noqa: BLE001 - never claim an unproven process exit
                    return {
                        "ok": False,
                        "error": "upgrade failed; owned process exit is unproven; resume required",
                    }
                self.records.phase("failed", worker_pid=0)
                return {
                    "ok": False,
                    "error": "upgrade failed after shutdown; worker is stopped; resume required",
                }


def verify_systemd_identity(
    unit: str,
    *,
    properties: dict[str, str] | None = None,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, str]:
    """Require systemd's cleanup boundary and an empty prior invocation."""
    import re
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit):
        raise ValueError("a systemd user service unit name is required")
    if properties is None:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=MainPID,ControlGroup,InvocationID,KillMode,SendSIGKILL,TimeoutStopUSec",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("MainPID") != str(os.getpid()):
        raise RuntimeError("managed serve must be the selected systemd unit MainPID")
    if (
        properties.get("KillMode") != "control-group"
        or properties.get("SendSIGKILL") != "yes"
        or properties.get("TimeoutStopUSec", "") in {"", "infinity", "0"}
    ):
        raise RuntimeError("systemd must enforce bounded control-group cleanup")
    invocation = properties.get("InvocationID", "")
    if not re.fullmatch("[0-9a-f]{32}", invocation) or invocation != os.environ.get(
        "INVOCATION_ID"
    ):
        raise RuntimeError("systemd invocation identity does not match this supervisor")
    group = properties.get("ControlGroup", "")
    if not group.startswith("/") or ".." in Path(group).parts or group == "/":
        raise RuntimeError("invalid service control group")
    own_groups = (proc_root / "self/cgroup").read_text().splitlines()
    if f"0::{group}" not in own_groups:
        raise RuntimeError("supervisor does not belong to the selected service cgroup")
    directory = cgroup_root / group.lstrip("/")
    if not (directory / "cgroup.procs").is_file():
        raise RuntimeError("service cgroup cannot be inspected")
    members: set[int] = set()
    for path in directory.rglob("cgroup.procs"):
        members.update(int(pid) for pid in path.read_text().split())
    if members != {os.getpid()}:
        raise RuntimeError("service cgroup has residual processes; prior cleanup is unproven")
    return {
        "unit": unit,
        "invocation": invocation,
        "cgroup": group,
        "boot": (proc_root / "sys/kernel/random/boot_id").read_text().strip(),
    }


def enable_subreaper() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "could not establish worker child ownership")


WORKER_PROTOCOL = 1


def remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("managed socket path contains an unexpected file")
    path.unlink()


class WorkerRuntime:
    """Own every state-touching child through stop, migration and readiness."""

    def __init__(self, socket_path: Path, *, host: str, port: int):
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self.child: asyncio.subprocess.Process | None = None
        self.client: Any = None

    @property
    def pid(self) -> int:
        return self.child.pid if self.child is not None and self.child.returncode is None else 0

    async def inspect(self, target: dict[str, Any]) -> dict[str, str]:
        if not isinstance(target, dict) or set(target) != {"python", "version"}:
            raise ValueError("target must identify an interpreter and exact release")
        interpreter, version = target["python"], target["version"]
        if (
            not isinstance(interpreter, str)
            or not Path(interpreter).is_absolute()
            or not os.access(interpreter, os.X_OK)
            or not isinstance(version, str)
            or not version
            or len(version) > 80
        ):
            raise ValueError("invalid target interpreter or version")
        code = (
            "import json; from importlib.metadata import version; "
            "from exomem.service_manager import WORKER_PROTOCOL; "
            'print(json.dumps({"version":version("exomem"),"protocol":WORKER_PROTOCOL}))'
        )
        probe = await asyncio.create_subprocess_exec(
            interpreter,
            "-I",
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), 10)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
                await probe.wait()
            raise
        if probe.returncode != 0 or len(output) > 4096:
            raise ValueError("target cannot run the managed worker protocol")
        actual = json.loads(output)
        if actual != {"version": version, "protocol": WORKER_PROTOCOL}:
            raise ValueError("target version or managed worker protocol differs")
        return {"python": interpreter, "version": version}

    async def _spawn(self, command: list[str]) -> None:
        if self.child is not None or _descendants():
            raise RuntimeError("previous owned process exit has not been proven")
        pending = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                start_new_session=True,
            )
        )
        try:
            self.child = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Retain ownership even if cancellation arrives between fork and
            # subprocess construction. The failure path must stop this tree.
            self.child = await asyncio.shield(pending)
            raise

    async def stop(self, timeout: float) -> None:
        if self.child is not None:
            await stop_owned_process(self.child, timeout=timeout, include_adopted=True)
            self.child = None
        elif _descendants():
            raise RuntimeError("untracked owned children require a service-manager cleanup")
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        remove_stale_socket(self.socket_path)

    async def migrate(self, target: dict[str, str], timeout: float) -> None:
        vault = os.environ.get("EXOMEM_VAULT_PATH", "")
        if not vault or not Path(vault).is_absolute():
            raise ValueError("managed service requires its installed absolute vault binding")
        deadline = Deadline(timeout)
        await self._spawn(
            [
                target["python"],
                "-I",
                "-m",
                "exomem",
                "maintain",
                "--vault",
                vault,
                "--migrate-state",
                "--offline",
                "--json",
            ]
        )
        await asyncio.wait_for(self.child.wait(), deadline.remaining(timeout))
        if self.child.returncode != 0:
            raise RuntimeError("offline target preparation failed")
        # A completed migrator is not proof its children have stopped.
        await self.stop(timeout=deadline.remaining(timeout))

    async def start(self, target: dict[str, str], timeout: float):
        import httpx

        deadline = Deadline(timeout)
        remove_stale_socket(self.socket_path)
        await self._spawn(
            [
                target["python"],
                "-I",
                "-m",
                "exomem.service_manager",
                "worker",
                "--socket",
                str(self.socket_path),
                "--host",
                self.host,
                "--port",
                str(self.port),
            ]
        )
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path), retries=0),
            base_url="http://localhost",
            trust_env=False,
            follow_redirects=False,
            timeout=None,
        )
        while True:
            if self.child.returncode is not None:
                raise RuntimeError("candidate worker exited before readiness")
            try:
                async with asyncio.timeout(deadline.remaining(2)):
                    health = await self.client.get("/health")
                    ready = await self.client.get("/health/ready")
                if health.status_code == 200 and health.json().get("version") != target["version"]:
                    raise RuntimeError("candidate is serving a different release")
                if (
                    health.status_code == 200
                    and ready.status_code == 200
                    and ready.json().get("status") == "ready"
                ):
                    return self.client
            except (httpx.HTTPError, TimeoutError):
                pass
            await asyncio.sleep(min(0.1, deadline.remaining(0.1)))


async def control_server(path: Path, supervisor: Supervisor) -> asyncio.Server:
    """Serve a bounded local-only JSON protocol; no public ASGI control route."""
    import socket
    import struct

    remove_stale_socket(path)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise ValueError("control peer is not the service owner")
            line = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid control request")
            command = request.get("command")
            if command == "status":
                result = supervisor.status()
            elif command in {"upgrade", "resume"}:
                if supervisor.transition_task is not None and not supervisor.transition_task.done():
                    result = {"ok": False, "error": "an upgrade is already in progress"}
                else:
                    supervisor.transition_task = asyncio.create_task(
                        supervisor.upgrade(
                            request.get("target"),
                            resume=command == "resume",
                        )
                    )
                    result = await asyncio.shield(supervisor.transition_task)
            else:
                result = {"ok": False, "error": "unknown control command"}
        except (ValueError, TimeoutError, ConnectionError):
            result = {"ok": False, "error": "invalid or incomplete control request"}
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_unix_server(handle, path=str(path), limit=65536)
    os.chmod(path, 0o600)
    return listener


async def serve(supervisor: Supervisor, *, host: str, port: int) -> None:
    """Keep ingress and control alive until the owning unit stops this daemon."""
    import uvicorn

    async with await control_server(supervisor.records.directory / "control.sock", supervisor):
        await supervisor.start()
        server = uvicorn.Server(
            uvicorn.Config(
                supervisor.ingress,
                host=host,
                port=port,
                lifespan="off",
                access_log=False,
                timeout_graceful_shutdown=10,
            )
        )

        async def monitor() -> None:
            while True:
                await asyncio.sleep(0.25)
                if supervisor.phase == "ready" and not supervisor.runtime.pid:
                    # A crash is not a managed handoff. Stop the supervisor so
                    # the unit cleans the whole cgroup and applies Restart=.
                    supervisor.ingress.unavailable()
                    server.should_exit = True
                    return

        monitoring = asyncio.create_task(monitor())
        try:
            await server.serve()
        finally:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
            if supervisor.transition_task is not None and not supervisor.transition_task.done():
                supervisor.transition_task.cancel()
                await asyncio.gather(supervisor.transition_task, return_exceptions=True)
            try:
                await asyncio.wait_for(supervisor.ingress.aclose(), 10)
            except TimeoutError:
                pass
            await supervisor.runtime.stop(timeout=5)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from importlib.metadata import version

    parser = argparse.ArgumentParser(prog="python -m exomem.service_manager")
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("serve", help="run the installed systemd managed service")
    daemon.add_argument("--runtime-dir", type=Path, required=True)
    daemon.add_argument("--worker-python", required=True)
    daemon.add_argument("--unit-name", required=True)
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=8765)
    worker = commands.add_parser("worker", help="run the supervisor-owned private HTTP worker")
    worker.add_argument("--socket", type=Path, required=True)
    worker.add_argument("--host", required=True)
    worker.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("managed services currently require Linux systemd user units")
    if args.command == "worker":
        from . import server

        private_directory(args.socket.parent)
        server.run(
            transport="streamable-http", host=args.host, port=args.port, worker_socket=args.socket
        )
        return 0
    try:
        directory = private_directory(args.runtime_dir)
        if len(os.fsencode(directory / "control.sock")) >= 104:
            raise ValueError("managed runtime directory is too long for Unix sockets")
        if not 0 < args.port < 65536:
            raise ValueError("port must be between 1 and 65535")
        with deployment_lock(directory):
            identity = verify_systemd_identity(args.unit_name)
            enable_subreaper()
            from .service_ingress import ServiceIngress

            host = os.environ.get("EXOMEM_HOST") or args.host
            runtime = WorkerRuntime(directory / "worker.sock", host=host, port=args.port)
            supervisor = Supervisor(
                directory,
                initial_target={"python": args.worker_python, "version": version("exomem")},
                ingress=ServiceIngress(),
                runtime=runtime,
                identity=identity,
            )
            asyncio.run(serve(supervisor, host=host, port=args.port))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"managed service: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
