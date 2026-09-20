"""Serialize and recover delivery of already authenticated custody generations.

The private delivery journal contains signed custody, never canonical mutation
receipts. It can complete an interrupted copy but cannot authorize a mutation.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from .governance import authorization_hosted_mount as mount
from .hosted_activation_delivery import (
    FILENAMES,
    CustodyDeliveryUnavailable,
    VerifiedDeliveryBundle,
    generation_relation,
    parse_delivery_bundle,
    public_response_bundle,
)

_JOURNAL = ".activation-ack-pending.json"
_JOURNAL_LIMIT = 2 * 3 * 87384 + 4096
_ORPHAN = re.compile(r"\.\.activation-ack-pending\.json\.[0-9a-f]{32}\.tmp\Z")


def _integrity_stat(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _closed_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CustodyDeliveryUnavailable
        result[key] = value
    return result


class CustodyPublisher:
    """One in-process writer; the hosting service owns the process lock."""

    def __init__(
        self,
        source: Path,
        destination: Path,
        *,
        now: Callable[[], float] = time.time,
        fetch_current: Callable[[float], Mapping[str, object]] | None = None,
    ) -> None:
        self.source = Path(source)
        self.destination = Path(destination)
        self.now = now
        self.fetch_current = fetch_current
        self._lock = threading.RLock()

    def _verify_directory(self) -> None:
        directory = self.destination.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise CustodyDeliveryUnavailable

    def _read(self, name: str, limit: int = 65536) -> bytes:
        self._verify_directory()
        descriptor = os.open(self.destination / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 1 <= info.st_size <= limit
            ):
                raise CustodyDeliveryUnavailable
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                raw = handle.read(limit + 1)
            if len(raw) != info.st_size or _integrity_stat(os.fstat(descriptor)) != _integrity_stat(
                info
            ):
                raise CustodyDeliveryUnavailable
            return raw
        finally:
            os.close(descriptor)

    def _files(self) -> dict[str, bytes]:
        return {name: self._read(name) for name in FILENAMES}

    def current(self) -> VerifiedDeliveryBundle:
        with self._lock:
            return parse_delivery_bundle(self._files(), now=int(self.now()))

    def _sync_directory(self) -> None:
        descriptor = os.open(self.destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_pending(self, before: VerifiedDeliveryBundle, after: VerifiedDeliveryBundle) -> None:
        def encoded(bundle):
            return {
                name: base64.b64encode(raw).decode("ascii") for name, raw in bundle.files.items()
            }

        raw = json.dumps(
            {"version": 1, "before": encoded(before), "after": encoded(after)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(raw) > _JOURNAL_LIMIT:
            raise CustodyDeliveryUnavailable
        temporary = self.destination / f".{_JOURNAL}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.destination / _JOURNAL)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _finish(self, candidate: VerifiedDeliveryBundle, *, historical: bool = False) -> None:
        mount.publish_custody_payloads(
            dict(candidate.files),
            self.destination,
            now=candidate.control.issued_at if historical else int(self.now()),
        )
        if self._files() != candidate.files:
            raise CustodyDeliveryUnavailable
        (self.destination / _JOURNAL).unlink()
        self._sync_directory()

    def recover_pending(self) -> bool:
        with self._lock:
            self._verify_directory()
            # The hosting service holds the process lock throughout its life.
            # Clean only this publisher's interrupted allocations before any
            # new allocation, including when no durable journal was reached.
            for path in self.destination.iterdir():
                if _ORPHAN.fullmatch(path.name):
                    info = path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_nlink != 1
                    ):
                        raise CustodyDeliveryUnavailable
                    path.unlink()
            try:
                raw = self._read(_JOURNAL, _JOURNAL_LIMIT)
            except FileNotFoundError:
                return False
            try:
                record = json.loads(raw, object_pairs_hook=_closed_object)
                if (
                    set(record) != {"version", "before", "after"}
                    or type(record["version"]) is not int
                    or record["version"] != 1
                ):
                    raise CustodyDeliveryUnavailable

                def decoded(value):
                    if not isinstance(value, dict) or set(value) != FILENAMES:
                        raise CustodyDeliveryUnavailable
                    files = {}
                    for name, encoded in value.items():
                        if not isinstance(encoded, str) or len(encoded) > 87384:
                            raise CustodyDeliveryUnavailable
                        data = base64.b64decode(encoded, validate=True)
                        if base64.b64encode(data).decode("ascii") != encoded:
                            raise CustodyDeliveryUnavailable
                        files[name] = data
                    return parse_delivery_bundle(files, now=int(self.now()), historical=True)

                before, after = decoded(record["before"]), decoded(record["after"])
                if generation_relation(before, after) not in {"same", "advance"}:
                    raise CustodyDeliveryUnavailable
                installed = self._files()
                if any(
                    installed[name] not in (before.files[name], after.files[name])
                    for name in FILENAMES
                ):
                    raise CustodyDeliveryUnavailable
                self._finish(after, historical=True)
                return True
            except (ValueError, TypeError, KeyError, OSError, mount.HostedCustodyMountUnavailable):
                raise CustodyDeliveryUnavailable from None

    def _install(self, candidate: VerifiedDeliveryBundle) -> bool:
        before = parse_delivery_bundle(self._files(), now=int(self.now()), historical=True)
        relation = generation_relation(before, candidate)
        if relation in {"same", "stale"}:
            return False
        if relation != "advance":
            raise CustodyDeliveryUnavailable
        mount._refuse_authority_identity_change(
            self.destination, dict(candidate.files), now=int(self.now())
        )
        self._write_pending(before, candidate)
        self._finish(candidate)
        return True

    def _response(self, response: Mapping[str, object]) -> VerifiedDeliveryBundle:
        keyrings = [self._read("keyring.json")]
        try:
            keyrings.append(mount._projected_payloads(self.source)["keyring.json"])
        except (OSError, mount.HostedCustodyMountUnavailable):
            pass
        return public_response_bundle(response, keyrings=keyrings, now=int(self.now()))

    def install_response(self, response: Mapping[str, object]) -> bool:
        with self._lock:
            self.recover_pending()
            return self._install(self._response(response))

    def refresh(self, *, deadline: float | None = None) -> bool:
        with self._lock:
            self.recover_pending()
            candidate = parse_delivery_bundle(
                mount._projected_payloads(self.source), now=int(self.now())
            )
            installed = parse_delivery_bundle(self._files(), now=int(self.now()), historical=True)
            if generation_relation(installed, candidate) == "incomparable":
                if self.fetch_current is None:
                    raise CustodyDeliveryUnavailable
                authoritative = self._response(self.fetch_current(deadline or time.monotonic() + 5))
                if generation_relation(candidate, authoritative) not in {"same", "advance"}:
                    raise CustodyDeliveryUnavailable
                candidate = authoritative
            return self._install(candidate)
