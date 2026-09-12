"""Stage a managed service release and request its private worker handoff."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

MAX_CONTROL_BYTES = 64 * 1024
PROFILES = {
    "lean": "",
    "onnx": "embeddings-onnx",
    "hybrid": "embeddings",
    "standard": "embeddings,media",
    "media": "embeddings,media,vision,diarization",
}


def _check_runtime_dir(runtime_dir: Path) -> None:
    if not runtime_dir.is_absolute():
        raise RuntimeError("managed runtime directory must be absolute")
    directory = runtime_dir.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise RuntimeError("managed runtime directory is not owner-only")


def control(runtime_dir: Path, command: dict[str, Any]) -> dict[str, Any]:
    _check_runtime_dir(runtime_dir)
    socket_path = runtime_dir / "control.sock"
    if len(os.fsencode(socket_path)) >= 108:
        raise RuntimeError("managed control socket path is too long")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(60 if command["command"] in {"upgrade", "resume"} else 5)
        client.connect(str(socket_path))
        _, peer_uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid != os.getuid():
            raise RuntimeError("managed control peer is not the service owner")
        client.sendall((json.dumps(command, separators=(",", ":")) + "\n").encode())
        with client.makefile("rb") as stream:
            line = stream.readline(MAX_CONTROL_BYTES + 1)
    if not line or len(line) > MAX_CONTROL_BYTES or not line.endswith(b"\n"):
        raise RuntimeError("invalid managed control response")
    response = json.loads(line)
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise RuntimeError("invalid managed control response")
    if not response["ok"]:
        raise RuntimeError(str(response.get("error", "managed service rejected command")))
    return response


def _installed_version(python: Path) -> str:
    result = subprocess.run(
        [str(python), "-I", "-c", "import importlib.metadata as m; print(m.version('exomem'))"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    return result.stdout.strip()


def _uv() -> str:
    local = Path.cwd() / ".uvbin" / "uv"
    executable = os.environ.get("EXOMEM_UV") or (str(local) if local.is_file() else shutil.which("uv"))
    if not executable:
        raise RuntimeError("uv is required to stage a managed release")
    return executable


def stage(runtime_dir: Path, launcher_python: str, profile: str, package_version: str) -> dict[str, str]:
    launcher = Path(launcher_python)
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError("manager reported an invalid launcher interpreter")
    releases = runtime_dir.parent / "releases"
    if releases.is_symlink():
        raise RuntimeError("managed releases directory must not be a symlink")
    releases.mkdir(mode=0o700, exist_ok=True)
    if releases.stat().st_uid != os.getuid() or releases.stat().st_mode & 0o077:
        raise RuntimeError("managed releases directory is not owner-only")
    target_dir = releases / uuid.uuid4().hex
    target_python = target_dir / "bin" / "python"
    requirement = f"exomem[{PROFILES[profile]}]" if PROFILES[profile] else "exomem"
    if package_version:
        requirement += f"=={package_version}"
    uv = _uv()
    for command in (
        [uv, "venv", "--python", str(launcher), str(target_dir)],
        [uv, "pip", "install", "--refresh-package", "exomem", "--python", str(target_python), requirement],
    ):
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        if result.returncode:
            raise RuntimeError(f"release staging failed (uv exit {result.returncode})")
    version = _installed_version(target_python)
    if not version or (package_version and version != package_version):
        raise RuntimeError("staged release version does not match requested version")
    return {"python": str(target_python), "version": version}


def _wait_for_target(runtime_dir: Path, target: dict[str, str], initial: dict[str, Any]) -> dict[str, Any]:
    result = initial
    deadline = time.monotonic() + 180
    while result.get("phase") == "upgrading" and time.monotonic() < deadline:
        time.sleep(1)
        result = control(runtime_dir, {"command": "status"})
    if result.get("phase") != "ready" or result.get("active") != target:
        raise RuntimeError("managed upgrade did not reach the staged release; inspect --status")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="show the managed service state")
    action.add_argument("--resume", action="store_true", help="roll forward a recorded failed transition")
    parser.add_argument("--package-version", default="", help="pin the staged PyPI release")
    parser.add_argument("--profile", choices=PROFILES, default="standard")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux":
            raise RuntimeError("managed service upgrades are available only on Linux/WSL")
        if args.status:
            result = control(args.runtime_dir, {"command": "status"})
        elif args.resume:
            result = control(args.runtime_dir, {"command": "resume"})
        else:
            import fcntl

            _check_runtime_dir(args.runtime_dir)
            lock_path = args.runtime_dir / "operator.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "r+b") as lock:
                info = os.fstat(lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise RuntimeError("managed operator lock is not owner-only")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = control(args.runtime_dir, {"command": "status"})
                if current.get("phase") != "ready":
                    raise RuntimeError("managed service is not ready; inspect --status or use --resume")
                target = stage(
                    args.runtime_dir,
                    str(current.get("launcher_python", "")),
                    args.profile,
                    args.package_version,
                )
                result = control(args.runtime_dir, {"command": "upgrade", "target": target})
                result = _wait_for_target(args.runtime_dir, target, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"managed upgrade: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
