"""Copy one Kubernetes-projected authorization bundle into owner-only memory."""

from __future__ import annotations

import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .authorization_custody import HOSTED_CUSTODY_ROOT, MAX_CUSTODY_FILE_BYTES

SOURCE_ROOT = Path("/run/exomem/authorization-session-source")
_FILENAMES = ("control.json", "keyring.json", "serving-membership.json")
_GENERATION = re.compile(r"\.\.[A-Za-z0-9_.-]{1,255}\Z")
#: Kubelet refreshes a Secret volume about once a minute, so a shorter poll
#: only costs syscalls. The renewal budget that matters is the attestation
#: window, not this interval.
WATCH_INTERVAL_SECONDS = 15.0


class HostedCustodyMountUnavailable(RuntimeError):
    """Content-free refusal for an unsafe or incomplete projected bundle."""


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_CUSTODY_FILE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) != expected_size or len(result) > MAX_CUSTODY_FILE_BYTES:
        raise HostedCustodyMountUnavailable
    return result


def _projected_payloads(source: Path) -> dict[str, bytes]:
    try:
        root = os.lstat(source)
        if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
            raise HostedCustodyMountUnavailable
        generation = os.readlink(source / "..data")
        if _GENERATION.fullmatch(generation) is None:
            raise HostedCustodyMountUnavailable
        expected_entries = {"..data", generation, *_FILENAMES}
        if set(os.listdir(source)) != expected_entries:
            raise HostedCustodyMountUnavailable
        generation_info = os.lstat(source / generation)
        if not stat.S_ISDIR(generation_info.st_mode) or stat.S_ISLNK(
            generation_info.st_mode
        ):
            raise HostedCustodyMountUnavailable
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        generation_fd = os.open(source / generation, flags)
        try:
            payloads: dict[str, bytes] = {}
            for name in _FILENAMES:
                if os.readlink(source / name) != f"..data/{name}":
                    raise HostedCustodyMountUnavailable
                file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
                    os, "O_NOFOLLOW", 0
                )
                descriptor = os.open(name, file_flags, dir_fd=generation_fd)
                try:
                    before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or not 1 <= before.st_size <= MAX_CUSTODY_FILE_BYTES
                        or stat.S_IMODE(before.st_mode) not in {0o400, 0o440, 0o444}
                    ):
                        raise HostedCustodyMountUnavailable
                    payloads[name] = _read_exact(descriptor, before.st_size)
                    after = os.fstat(descriptor)
                    if (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise HostedCustodyMountUnavailable
                finally:
                    os.close(descriptor)
            if os.readlink(source / "..data") != generation or any(
                os.readlink(source / name) != f"..data/{name}"
                for name in _FILENAMES
            ):
                raise HostedCustodyMountUnavailable
            return payloads
        finally:
            os.close(generation_fd)
    except HostedCustodyMountUnavailable:
        raise
    except (OSError, TypeError, ValueError):
        raise HostedCustodyMountUnavailable from None


def copy_projected_custody(source: Path, destination: Path) -> None:
    """Create private custody and publish one projected generation into it."""

    source = Path(source)
    destination = Path(destination)
    staged: list[Path] = []
    created_destination = False
    try:
        payloads = _projected_payloads(source)
        parent_info = os.lstat(destination.parent)
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise HostedCustodyMountUnavailable
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd = os.open(destination.parent, parent_flags)
        try:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)
            created_destination = True
        finally:
            os.close(parent_fd)
        info = os.lstat(destination)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or mode != 0o700
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
            or any(destination.iterdir())
        ):
            raise HostedCustodyMountUnavailable
        for name in _FILENAMES:
            temporary = destination / f".{name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            staged.append(temporary)
            try:
                view = memoryview(payloads[name])
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise HostedCustodyMountUnavailable
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, destination / name)
            staged.remove(temporary)
        directory_fd = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except HostedCustodyMountUnavailable:
        if created_destination:
            for path in staged:
                try:
                    path.unlink()
                except OSError:
                    pass
            for name in _FILENAMES:
                try:
                    (destination / name).unlink()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
        raise
    except (OSError, TypeError, ValueError):
        if created_destination:
            for path in staged:
                try:
                    path.unlink()
                except OSError:
                    pass
            for name in _FILENAMES:
                try:
                    (destination / name).unlink()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
        raise HostedCustodyMountUnavailable from None


def republish_projected_custody(source: Path, destination: Path) -> None:
    """Replace a live custody generation with the current projected one.

    `copy_projected_custody` creates its destination and refuses a non-empty
    one, so it can only initialise. A renewed authorization bundle reaches the
    projected Secret while the pod runs, and the runtime re-reads custody on
    every admission check, so publishing the new generation in place is what
    makes renewal invisible instead of requiring the pod to restart.

    Every replacement file is written and fsynced before any of them is moved
    into place. A source that turns out to be torn or ambiguous therefore leaves
    the previous generation whole, rather than stranding the runtime on a
    directory holding one renewed file beside two stale ones -- which would fail
    cross-validation and refuse writes until something republished it correctly.
    """

    source = Path(source)
    destination = Path(destination)
    staged: list[Path] = []
    try:
        payloads = _projected_payloads(source)
        info = os.lstat(destination)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
        ):
            raise HostedCustodyMountUnavailable
        _refuse_authority_identity_change(destination, payloads)
        # The sidecar is the only writer and never republishes concurrently with
        # itself, so any staging file already present is an orphan from a crashed
        # attempt. Left alone they accumulate without bound in a 256 KiB tmpfs.
        for orphan in destination.glob(".*.tmp"):
            try:
                orphan.unlink()
            except OSError:
                pass
        replacements: list[tuple[Path, Path]] = []
        for name in _FILENAMES:
            temporary = destination / f".{name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            staged.append(temporary)
            try:
                view = memoryview(payloads[name])
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise HostedCustodyMountUnavailable
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            replacements.append((temporary, destination / name))
        for temporary, published in replacements:
            os.replace(temporary, published)
            staged.remove(temporary)
        directory_fd = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except HostedCustodyMountUnavailable:
        for path in staged:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    except (OSError, TypeError, ValueError):
        for path in staged:
            try:
                path.unlink()
            except OSError:
                pass
        raise HostedCustodyMountUnavailable from None


def _refuse_authority_identity_change(destination: Path, payloads: dict[str, bytes]) -> None:
    """Keep an activated private authority generation bound to its identity.

    The projected bundle remains read-only.  We inspect the fixed authority
    artifact patterns in its immediate control directory before parsing either
    custody generation; ordinary v1 renewals retain their copy-and-replace
    path.
    """
    try:
        from ..vocabulary_authority import authority_artifact_paths
        from . import authorization_custody

        old = {
            name: (destination / name).read_bytes()
            for name in ("keyring.json", "control.json")
        }
        old_keyring = authorization_custody.parse_keyring(old["keyring.json"])
        new_keyring = authorization_custody.parse_keyring(payloads["keyring.json"])
        new_control = authorization_custody.parse_control_record(
            payloads["control.json"], keyring=new_keyring, now=int(time.time())
        )
        try:
            old_control = authorization_custody.parse_control_record(
                old["control.json"], keyring=old_keyring, now=int(time.time())
            )
        except authorization_custody.AuthorizationCustodyUnavailable:
            # `control.json` is published before `keyring.json`.  A crash in
            # that interval is repairable only when the new control is the
            # exact authenticated projected successor, never merely a record
            # that happens to verify under the projected keyring.
            if old["control.json"] != payloads["control.json"]:
                raise
            old_control = new_control
        old_identity = (old_control.cell_id, old_control.logical_vault_id, old_control.keyring_id)
        new_identity = (new_control.cell_id, new_control.logical_vault_id, new_control.keyring_id)
        old_artifacts = authority_artifact_paths(
            destination / "control.json", old_control.logical_vault_id
        )
        new_artifacts = authority_artifact_paths(
            destination / "control.json", new_control.logical_vault_id
        )
    except Exception as exc:  # noqa: BLE001 - authority-present identity is authenticated
        raise HostedCustodyMountUnavailable from exc
    old_floor = getattr(old_control, "vocabulary_authority_floor", 1)
    new_floor = getattr(new_control, "vocabulary_authority_floor", 1)
    if old_floor not in {1, 2} or new_floor not in {1, 2}:
        raise HostedCustodyMountUnavailable
    if new_floor < old_floor:
        raise HostedCustodyMountUnavailable
    if (old_floor == 2 or new_floor == 2) and old_identity != new_identity:
        raise HostedCustodyMountUnavailable
    old_marker, old_database = old_artifacts
    new_marker, new_database = new_artifacts
    authority_artifacts = (old_marker, old_database, new_marker, new_database)
    authority_sidecars = tuple(
        database.with_name(f"{database.name}{suffix}")
        for database in (old_database, new_database)
        for suffix in ("-journal", "-wal", "-shm")
    )
    if old_identity != new_identity and any(
        os.path.lexists(path) for path in (*authority_artifacts, *authority_sidecars)
    ):
        raise HostedCustodyMountUnavailable


def _published_custody_is_current(source: Path, destination: Path) -> bool:
    """Answer whether every published file already matches the projected one.

    An unreadable source is treated as current: there is nothing to publish, and
    republishing from a torn projection would be worse than waiting. An
    unreadable or differing destination file is not current, which is what makes
    a crash-interrupted publish and a restarted sidecar both self-healing.
    """

    try:
        payloads = _projected_payloads(Path(source))
    except HostedCustodyMountUnavailable:
        return True
    for name in _FILENAMES:
        try:
            with open(Path(destination) / name, "rb") as handle:
                if handle.read(MAX_CUSTODY_FILE_BYTES + 1) != payloads[name]:
                    return False
        except OSError:
            return False
    return True


def watch_projected_custody(
    source: Path,
    destination: Path,
    *,
    interval_seconds: float = WATCH_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
    ticks: int | None = None,
) -> int:
    """Republish each time kubelet swaps the projected generation.

    The runtime re-reads custody on every admission check, so keeping the
    published generation current is the whole of seamless renewal: a renewed
    bundle reaches the Secret volume while the pod runs, and the pod picks it up
    without restarting.

    A republish that fails is retried on the next tick rather than ending the
    watch. The previous generation stays whole and serving, so the cost of
    waiting is bounded by the attestation window, while exiting would strand the
    pod on a generation nothing will ever refresh.

    The tick compares what is *published* against what is *projected*, rather
    than remembering which source generation was last seen. Remembering is
    edge-triggered and wrong twice over: a sidecar restart re-seeds the memory
    from the current source and never notices that the destination is stale, and
    a republish interrupted between file replacements leaves a mixed generation
    that no later tick would ever revisit. Comparing content is level-triggered,
    so both repair themselves within one interval.
    """

    remaining = ticks
    while remaining is None or remaining > 0:
        sleeper(interval_seconds)
        if remaining is not None:
            remaining -= 1
        if _published_custody_is_current(source, destination):
            continue
        try:
            republish_projected_custody(source, destination)
        except HostedCustodyMountUnavailable:
            continue
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    watch = "--watch" in arguments
    if watch:
        arguments.remove("--watch")
    if arguments:
        return 2
    if watch:
        # The init container has already published the first generation; a
        # second copy would refuse against its own non-empty destination.
        return watch_projected_custody(SOURCE_ROOT, HOSTED_CUSTODY_ROOT)
    try:
        copy_projected_custody(SOURCE_ROOT, HOSTED_CUSTODY_ROOT)
    except HostedCustodyMountUnavailable:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
