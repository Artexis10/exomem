"""Stage a managed service release and request its private worker handoff."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from .governance.scrubber import NOTICE, scrub_text

MAX_CONTROL_BYTES = 64 * 1024
#: Bound on the uv stderr tail surfaced in a staging failure: at most this
#: many trailing lines, further clipped to at most this many trailing bytes.
_UV_STDERR_TAIL_MAX_LINES = 20
_UV_STDERR_TAIL_MAX_BYTES = 4 * 1024
#: How much trailing stderr is scrubbed before the tail is clipped. Scrubbing
#: runs on whole lines inside this window, never on a clipped fragment.
_UV_STDERR_READ_WINDOW_BYTES = 64 * 1024
#: The whole userinfo segment of a URL (`user:pass` or a bare token before
#: `@`). Not a shape the shared egress scrubber recognizes on its own, but
#: exactly what a leaked package-index or git-source URL carries.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/\s@]+(?=@)")
#: The value of a labelled secret assignment such as `UV_INDEX_PASSWORD=...`,
#: whose value may be too low-entropy for the shared scrubber to recognize.
_LABELLED_SECRET_RE = re.compile(r"(?i)(password|token|secret)(\s*[=:]\s*)\S+")
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
        # Every command is answered promptly now: a transition is acknowledged
        # and polled through `status`, never awaited on this connection.
        client.settimeout(15 if command["command"] in {"upgrade", "resume"} else 5)
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


# A target that predates the state-migration declaration has no
# `declared_descriptor_ids`. Staging it must still work -- that is the rollback
# path -- so the probe degrades to an empty set exactly as `WorkerRuntime.inspect`
# does, and an empty declaration makes the supervisor run the migrator.
_TARGET_PROBE = (
    "import json; import importlib.metadata as m\n"
    "try:\n"
    "    from exomem.state_migration import declared_descriptor_ids\n"
    "    descriptors = list(declared_descriptor_ids())\n"
    "except Exception:\n"
    "    descriptors = []\n"
    'print(json.dumps({"version": m.version("exomem"), "state_descriptors": descriptors}))'
)

_WHEEL_TARGET_PROBE = (
    "import base64; import csv; import hashlib; import importlib.metadata as m; import json\n"
    "try:\n"
    "    from exomem.state_migration import declared_descriptor_ids\n"
    "    descriptors = list(declared_descriptor_ids())\n"
    "except Exception:\n"
    "    descriptors = []\n"
    "try:\n"
    '    distribution = m.distribution("exomem")\n'
    '    name = distribution.metadata["Name"]\n'
    '    raw_direct_url = distribution.read_text("direct_url.json")\n'
    "    direct_url = json.loads(raw_direct_url) if raw_direct_url else None\n"
    "    record_hashes = {}\n"
    '    for row in csv.reader((distribution.read_text("RECORD") or "").splitlines()):\n'
    "        algorithm, separator, encoded = row[1].partition(\"=\") if len(row) == 3 else (\"\", \"\", \"\")\n"
    '        if algorithm in hashlib.algorithms_guaranteed and separator == "=" and encoded:\n'
    "            content = hashlib.new(algorithm)\n"
    "            if content.digest_size < hashlib.sha256().digest_size:\n"
    "                continue\n"
    '            with distribution.locate_file(row[0]).open("rb") as installed:\n'
    "                while chunk := installed.read(1024 * 1024):\n"
    "                    content.update(chunk)\n"
    "            record_hashes[row[0]] = algorithm + \"=\" + base64.urlsafe_b64encode(content.digest()).decode().rstrip(\"=\")\n"
    "except Exception:\n"
    "    name = None\n"
    "    direct_url = None\n"
    "    record_hashes = None\n"
    'print(json.dumps({"version": m.version("exomem"), "name": name, '
    '"direct_url": direct_url, "record_hashes": record_hashes, "state_descriptors": descriptors}))'
)


def _staged_identity(python: Path, *, wheel: bool = False) -> dict[str, object]:
    """Read the staged release's version and the state descriptors it requires.

    The descriptor set is the target's migration declaration: the supervisor
    compares it with the vault's state manifest at cutover and runs the offline
    migrator only when they differ (`seamless-managed-worker-handoff` D8).
    """
    result = subprocess.run(
        [str(python), "-I", "-c", _WHEEL_TARGET_PROBE if wheel else _TARGET_PROBE],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    identity = json.loads(result.stdout)
    if not isinstance(identity, dict) or not isinstance(identity.get("version"), str):
        raise RuntimeError("staged release did not report a usable identity")
    descriptors = identity.get("state_descriptors")
    if not isinstance(descriptors, list) or not all(
        isinstance(entry, str) and entry for entry in descriptors
    ):
        raise RuntimeError("staged release did not declare its state descriptors")
    # An empty list is a legitimate declaration from a release that predates the
    # descriptor probe; the supervisor treats it as "declares nothing" and runs
    # the offline migrator.
    return {
        "version": identity["version"].strip(),
        "name": identity.get("name"),
        "direct_url": identity.get("direct_url"),
        "record_hashes": identity.get("record_hashes"),
        "state_descriptors": descriptors,
    }


def _uv() -> str:
    local = Path.cwd() / ".uvbin" / "uv"
    executable = os.environ.get("EXOMEM_UV") or (str(local) if local.is_file() else shutil.which("uv"))
    if not executable:
        raise RuntimeError("uv is required to stage a managed release")
    return executable


def _regular_file(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("candidate wheel must be a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sha256_file(path: Path) -> str:
    before = _regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise RuntimeError("candidate wheel changed while staging")
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if _regular_file(path) != before:
        raise RuntimeError("candidate wheel changed while staging")
    return digest.hexdigest()


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    if wheel.suffix != ".whl":
        raise RuntimeError("candidate artifact must be a wheel")
    _regular_file(wheel)
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1 or "exomem/__init__.py" not in archive.namelist():
                raise RuntimeError("candidate artifact is not an Exomem wheel")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("candidate artifact is not a readable wheel") from exc
    name = metadata.get("Name", "").strip()
    version = metadata.get("Version", "").strip()
    if re.sub(r"[-_.]+", "-", name).lower() != "exomem" or not version:
        raise RuntimeError("candidate artifact is not an Exomem wheel")
    if not wheel.name.startswith(f"exomem-{version}-"):
        raise RuntimeError("candidate wheel filename does not match its package metadata")
    return name, version


def _secure_record_algorithm(algorithm: str) -> bool:
    if algorithm not in hashlib.algorithms_guaranteed:
        return False
    return hashlib.new(algorithm).digest_size >= hashlib.sha256().digest_size


def _wheel_record_hashes(wheel: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
            if len(record_names) != 1:
                raise RuntimeError("candidate wheel does not have a complete install record")
            expected: dict[str, str] = {}
            for row in csv.reader(io.TextIOWrapper(archive.open(record_names[0]), encoding="utf-8")):
                if len(row) != 3 or row[0] == record_names[0]:
                    continue
                algorithm, separator, encoded = row[1].partition("=")
                if separator != "=" or not _secure_record_algorithm(algorithm) or not encoded:
                    continue
                expected[row[0]] = row[1]
            record_directory = record_names[0].removesuffix("RECORD")
            excluded = {record_names[0], f"{record_directory}RECORD.jws", f"{record_directory}RECORD.p7s"}
            names = {name for name in archive.namelist() if not name.endswith("/")} - excluded
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("candidate artifact is not a readable wheel") from exc
    if set(expected) != names:
        raise RuntimeError("candidate wheel does not have a complete install record")
    return expected


def _snapshot_wheel(wheel: Path, target_dir: Path) -> tuple[Path, str, str]:
    source_identity = _regular_file(wheel)
    name, version = _wheel_metadata(wheel)
    if _regular_file(wheel) != source_identity:
        raise RuntimeError("candidate wheel changed while staging")
    digest = _sha256_file(wheel)
    if _regular_file(wheel) != source_identity:
        raise RuntimeError("candidate wheel changed while staging")
    snapshot = target_dir / wheel.name
    temporary = target_dir / f".{uuid.uuid4().hex}.wheel"
    try:
        with wheel.open("rb") as source, os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
        os.replace(temporary, snapshot)
    except OSError as exc:
        raise RuntimeError("candidate wheel could not be snapshotted") from exc
    if _regular_file(wheel) != source_identity or _sha256_file(wheel) != digest or _sha256_file(snapshot) != digest:
        raise RuntimeError("candidate wheel changed while staging")
    _wheel_metadata(snapshot)
    return snapshot, name, version


def _verify_wheel_install(identity: dict[str, object], snapshot: Path, digest: str, version: str) -> None:
    name = identity.get("name")
    direct_url = identity.get("direct_url")
    if re.sub(r"[-_.]+", "-", str(name)).lower() != "exomem" or identity["version"] != version:
        raise RuntimeError("staged candidate package identity does not match its wheel")
    if not isinstance(direct_url, dict):
        raise RuntimeError("staged candidate did not record wheel provenance")
    archive_info = direct_url.get("archive_info")
    recorded_url = direct_url.get("url")
    if not isinstance(archive_info, dict) or not isinstance(recorded_url, str):
        raise RuntimeError("staged candidate did not record its wheel source")
    source_url, _, fragment = recorded_url.partition("#")
    if source_url != snapshot.as_uri():
        raise RuntimeError("staged candidate did not record its wheel source")
    recorded_digests: list[str] = []
    deprecated_hash = archive_info.get("hash")
    if deprecated_hash is not None:
        if not isinstance(deprecated_hash, str):
            raise RuntimeError("staged candidate wheel digest does not match its installed provenance")
        recorded_digests.append(deprecated_hash)
    hashes = archive_info.get("hashes")
    if hashes is not None:
        if not isinstance(hashes, dict):
            raise RuntimeError("staged candidate wheel digest does not match its installed provenance")
        sha256 = hashes.get("sha256")
        if sha256 is not None:
            if not isinstance(sha256, str):
                raise RuntimeError("staged candidate wheel digest does not match its installed provenance")
            recorded_digests.append(f"sha256={sha256}")
    if fragment:
        recorded_digests.append(fragment)
    if not recorded_digests or any(recorded != f"sha256={digest}" for recorded in recorded_digests):
        raise RuntimeError("staged candidate wheel digest does not match its installed provenance")
    record_hashes = identity.get("record_hashes")
    expected_hashes = _wheel_record_hashes(snapshot)
    if not isinstance(record_hashes, dict) or any(
        record_hashes.get(path) != digest for path, digest in expected_hashes.items()
    ):
        raise RuntimeError("staged candidate files do not match its wheel")


def _uv_stderr_tail(data: bytes) -> str:
    """Bounded, credential-scrubbed tail of a failed uv command's stderr.

    Scrubbing runs before any clipping: a clip that lands inside a URL would
    drop the `://` the userinfo rule anchors on, and a replacement notice is
    longer than what it replaces, so clipping first neither hides credentials
    nor bounds the result.
    """
    if len(data) > _UV_STDERR_READ_WINDOW_BYTES:
        data = data[-_UV_STDERR_READ_WINDOW_BYTES:]
        # The first line in the window started before it, so it may be the
        # remainder of a credential whose prefix was cut. Never emit it.
        data = data.partition(b"\n")[2]
    text = _URL_USERINFO_RE.sub(NOTICE, data.decode("utf-8", errors="replace"))
    text = _LABELLED_SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + NOTICE, text)
    text, _ = scrub_text(text)
    tail = "\n".join(text.splitlines()[-_UV_STDERR_TAIL_MAX_LINES:]).strip()
    tail_bytes = tail.encode("utf-8")
    if len(tail_bytes) > _UV_STDERR_TAIL_MAX_BYTES:
        # Already scrubbed as whole lines, so a clipped fragment of it
        # carries nothing the whole line would not.
        tail = tail_bytes[-_UV_STDERR_TAIL_MAX_BYTES:].decode("utf-8", errors="ignore")
    return tail


def _write_provenance(path: Path, provenance: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(provenance, stream, sort_keys=True)
        stream.write("\n")


def stage(
    runtime_dir: Path,
    launcher_python: str,
    profile: str,
    package_version: str,
    wheel: Path | None = None,
    source_revision: str = "",
) -> dict[str, Any]:
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
    target_dir.mkdir(mode=0o700)
    if target_dir.stat().st_uid != os.getuid() or target_dir.stat().st_mode & 0o077:
        raise RuntimeError("managed release directory is not owner-only")
    snapshot: Path | None = None
    wheel_name = ""
    wheel_version = ""
    digest = ""
    if wheel is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", source_revision):
            raise RuntimeError("candidate wheel requires a full hexadecimal source revision")
        wheel_path = wheel.expanduser()
        _regular_file(wheel_path)
        snapshot, _, wheel_version = _snapshot_wheel(wheel_path.resolve(), target_dir)
        wheel_name = wheel_path.name
        digest = _sha256_file(snapshot)
    environment = target_dir / "venv" if snapshot else target_dir
    target_python = environment / "bin" / "python"
    requirement = f"exomem[{PROFILES[profile]}]" if PROFILES[profile] else "exomem"
    if snapshot:
        requirement = f"{requirement} @ {snapshot.as_uri()}#sha256={digest}"
    elif package_version:
        requirement += f"=={package_version}"
    uv = _uv()
    for command in (
        [uv, "venv", "--python", str(launcher), str(environment)],
        [uv, "pip", "install", "--refresh-package", "exomem", "--python", str(target_python), requirement],
    ):
        # stderr goes to a file, not a pipe, so a long run is never held in
        # memory; only the bounded window the tail needs is read back. One
        # byte past the window tells the tail that its first line was cut.
        with tempfile.TemporaryFile() as stderr:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr, timeout=900)
            if result.returncode:
                stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - _UV_STDERR_READ_WINDOW_BYTES - 1))
                tail = _uv_stderr_tail(stderr.read())
                detail = f"; uv stderr: {tail}" if tail else ""
                raise RuntimeError(f"release staging failed (uv exit {result.returncode}){detail}")
    identity = _staged_identity(target_python, wheel=snapshot is not None)
    version = identity["version"]
    if not version or (package_version and version != package_version):
        raise RuntimeError("staged release version does not match requested version")
    if snapshot:
        _verify_wheel_install(identity, snapshot, digest, wheel_version)
        if _sha256_file(snapshot) != digest:
            raise RuntimeError("candidate wheel snapshot changed while staging")
        _write_provenance(
            target_dir / "provenance.json",
            {
                "artifact_sha256": digest,
                "source_revision": source_revision,
                "original_artifact_name": wheel_name,
                "installed_name": str(identity["name"]),
                "installed_version": version,
                "installed_python": str(target_python),
            },
        )
    return {
        "python": str(target_python),
        "version": version,
        "state_descriptors": identity["state_descriptors"],
    }


def _transition_budget() -> float:
    """How long an accepted transition may take: warm, cutover, cold start.

    The supervisor warms the candidate beside the serving worker before it
    pauses anything, and once it has stopped the old worker it waits out the
    cold-start window for the replacement to report ready. Reporting failure
    while the supervisor is still legitimately waiting is what sends an
    operator to `--resume` in the middle of a handoff that was going to
    succeed, so this covers both budgets, with room for a busy host.
    """
    from .service_manager import cold_start_window, standby_warm_budget

    return standby_warm_budget() + cold_start_window() + 120.0


def _wait_for_target(
    runtime_dir: Path,
    target: dict[str, Any],
    initial: dict[str, Any],
    *,
    budget: float | None = None,
    interval: float = 1.0,
) -> dict[str, Any]:
    """Poll an accepted transition to its outcome.

    The supervisor acknowledges `upgrade` immediately and runs the transition in
    the background, so this is where the operator waits. It ends on the recorded
    outcome for this transition, on a ready supervisor serving the staged
    target, or on the budget.
    """
    transition = initial.get("transition") if initial.get("accepted") else None
    result = initial if not transition else control(runtime_dir, {"command": "status"})
    deadline = time.monotonic() + (_transition_budget() if budget is None else budget)
    while time.monotonic() < deadline:
        recorded = result.get("last_transition")
        if isinstance(recorded, dict) and (
            transition is None or recorded.get("transition") == transition
        ):
            if not recorded.get("ok"):
                raise RuntimeError(
                    str(recorded.get("error", "managed upgrade failed; inspect --status"))
                )
            if result.get("phase") == "ready" and result.get("active") == target:
                return result
        elif result.get("phase") not in {"upgrading", "unavailable"} and not transition:
            break
        time.sleep(interval)
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--package-version", default="", help="pin the staged PyPI release")
    source.add_argument("--wheel", type=Path, help="stage an immutable local Exomem wheel")
    parser.add_argument("--source-revision", default="", help="full source revision for a local wheel")
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
            if args.wheel is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", args.source_revision):
                parser.error("--wheel requires a full hexadecimal source revision")
            if args.source_revision and args.wheel is None:
                parser.error("--source-revision requires --wheel")
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
                    args.wheel,
                    args.source_revision,
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
