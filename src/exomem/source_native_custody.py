"""Private native allocation facts, without canonical ownership or publication authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from . import held_fs, reserved_paths, source_capture, source_custody, state_paths
from .held_fs import FileVersion, HeldFile, StableIdentity

OWNER = "source_native_custody"  # This native owner token addresses the closed copy-custody protocol.

# A connection belongs to the thread that opened it, so a batch session lives in that thread only.
_THREAD = threading.local()


class _Session:
    def __init__(self) -> None:
        # Keyed by the store object itself, so a recycled id can never reach another database.
        self.connections: dict[object, sqlite3.Connection] = {}
        self.busy: set[object] = set()


@contextmanager
def session() -> Iterator[None]:
    """Keep one connection per custody database open for this thread's writer batch.

    Every operation still commits its own transaction with a synced WAL commit; the session saves
    only the open, close and last-close checkpoint that a fresh connection pays per operation.
    A nested session joins the outer one.
    """
    if getattr(_THREAD, "session", None) is not None:
        yield
        return
    current = _THREAD.session = _Session()
    try:
        yield
    finally:
        _THREAD.session = None
        for connection in current.connections.values():
            connection.close()


# These closed states describe native allocation facts, not a content vocabulary.
class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    binding_id: str
    ledger_id: str
    physical_scope: str
    relative_path: str
    expected_sha256: str
    expected_size: int
    source_ref: str | None
    attempt_ticket: str | None
    image_id: str | None
    generated_copy_id: str | None
    publication_id: str | None
    destination_relative: str | None
    removable: bool
    root_identity: StableIdentity
    parent_identity: StableIdentity
    destination_parent_identity: StableIdentity | None
    state: Literal["reserved", "initializing", "verified", "installed"] = "reserved"
    identity: StableIdentity | None = None
    version: FileVersion | None = None
    bound_sha256: str | None = None
    outcome: Literal["not_written", "removed", "installed"] | None = None

    @model_validator(mode="after")
    def _facts(self):
        source_capture.validate_sha256(self.expected_sha256)
        if self.expected_size < 0:
            raise ValueError("native allocation size is invalid")
        if self.state == "reserved":
            if self.identity is not None or self.version is not None or self.bound_sha256 is not None:
                raise ValueError("reserved native allocation already has content")
            if self.outcome is not None and self.outcome != "not_written":
                raise ValueError("unbound native allocation cannot acknowledge a file")
        elif self.identity is None or self.identity.kind != "file" or self.identity.link_count != 1:
            raise ValueError("native allocation lacks its unique file identity")
        if self.version is not None and self.version.identity != self.identity:
            raise ValueError("native allocation version has a different identity")
        if self.bound_sha256 is not None:
            source_capture.validate_sha256(self.bound_sha256)
        if self.state in ("verified", "installed") and (self.version is None or self.version.size != self.expected_size
                                                        or self.bound_sha256 != self.expected_sha256):
            raise ValueError("verified native allocation lacks its complete producer image")
        if self.state == "installed" and (self.destination_relative is None or self.destination_parent_identity is None):
            raise ValueError("installed native allocation lacks its destination")
        if (self.state != "reserved" and self.outcome == "not_written"
                or self.outcome == "installed" and self.state != "verified"):
            raise ValueError("native acknowledgement has no matching allocation fact")
        return self


@dataclass(frozen=True)
class NativeReservation:
    """An internal handle sealed to one owner and one immutable row allocation."""

    _binding: _Binding = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    @property
    def binding_id(self) -> str:
        return self._binding.binding_id

    @property
    def identity(self) -> StableIdentity | None:
        return self._binding.identity

    @property
    def version(self) -> FileVersion | None:
        return self._binding.version

    @property
    def relative_path(self) -> str:
        return self._binding.relative_path

    @property
    def source_ref(self) -> str | None:
        return self._binding.source_ref

    @property
    def attempt_ticket(self) -> str | None:
        return self._binding.attempt_ticket

    @property
    def image_id(self) -> str | None:
        return self._binding.image_id

    @property
    def generated_copy_id(self) -> str | None:
        return self._binding.generated_copy_id

    @property
    def publication_id(self) -> str | None:
        return self._binding.publication_id

    @property
    def destination_relative(self) -> str | None:
        return self._binding.destination_relative


@dataclass(frozen=True)
class NativeObservation:
    # The closed protocol preserves unknown observations separately from acknowledged closure.
    state: Literal["reserved", "initializing", "verified", "installed", "closed", "unknown"]
    binding_id: str
    identity: StableIdentity | None = None
    version: FileVersion | None = None
    matching: bool | None = None
    outcome: Literal["not_written", "removed", "installed"] | None = None
    reservation: NativeReservation | None = None


@dataclass(frozen=True)
class NativePage:
    # Page states distinguish a complete indexed read from truncation and unavailable proof.
    state: Literal["ready", "partial", "unknown"]
    reservations: tuple[NativeReservation, ...] = ()
    cursor: str | None = None


class NativeCustodyUnavailable(RuntimeError):
    """The private owner cannot establish the database or physical binding."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _parts(relative: str) -> tuple[str, str]:
    # The held protocol fixes these path separators and traversal tokens.
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\0" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("native allocation requires an anchor-relative file path")
    path = Path(relative)
    if path.is_absolute() or path.drive:
        raise ValueError("native allocation escapes its anchor")
    return path.parent.as_posix(), path.name


def _opaque(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise ValueError("native metadata requires a bounded opaque identifier")
    return value


def _id(value: str) -> str:
    if not isinstance(value, str) or uuid.UUID(value).hex != value:
        raise ValueError("native binding requires a hexadecimal UUID")
    return value


def _same(first: StableIdentity, second: StableIdentity) -> bool:
    return held_fs._same_file_identity(first, second)


def _allocation(binding: _Binding) -> dict:
    # These fields alone change as the closed allocation protocol advances.
    return binding.model_dump(mode="json", exclude={"state", "identity", "version", "bound_sha256", "outcome"})


class NativeCustody:
    """Own exact private allocations under one current canonical writer boundary."""

    def __init__(self, root: Path, *, anchor: Path | None = None):
        from .writer_lease import active_manager

        self.root = _absolute(root)
        source_custody._boundary(self.root)
        self._manager = active_manager()
        self._store = self._manager.idempotency
        self._ledger_id = source_capture.validate_sha256(self._store.capture_attempts.ledger_id)
        self.anchor = self.root if anchor is None else _absolute(anchor)
        # These three roots are the closed placement protocol; callers cannot nominate another root.
        if self.anchor not in (self.root, _absolute(state_paths.vault_state_dir(self.root)),
                               _absolute(self._store.state_dir)):
            raise ValueError("native custody anchor is not a current owner root")
        self._seal = object()
        # Borrowed handles preserve allocation continuity only while their caller keeps them open.
        self._live_allocations: dict[str, HeldFile] = {}
        self._vault_identity = self._root_identity(self.root)
        self._anchor_identity = self._root_identity(self.anchor)
        self._database_identity = self._database_file_identity()
        scope = (self._ledger_id, self._anchor_identity.device, self._anchor_identity.inode,
                 self._anchor_identity.kind)
        self._physical_scope = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS source_native_bindings ("
                         "binding_id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, physical_scope TEXT NOT NULL,"
                         "source_ref TEXT, attempt_ticket TEXT, outcome TEXT, data TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS source_native_open ON source_native_bindings "
                         "(physical_scope,binding_id) WHERE outcome IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS source_native_source ON source_native_bindings "
                         "(physical_scope,source_ref,binding_id) WHERE outcome IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS source_native_attempt ON source_native_bindings "
                         "(physical_scope,attempt_ticket,binding_id) WHERE outcome IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS source_native_source_attempt ON source_native_bindings "
                         "(physical_scope,source_ref,attempt_ticket,binding_id) WHERE outcome IS NULL")

    def _boundary(self) -> None:
        from .writer_lease import active_manager

        source_custody._boundary(self.root)
        if active_manager() is not self._manager or self._manager.idempotency is not self._store:
            raise NativeCustodyUnavailable("native custody belongs to another active manager")
        if self.anchor != self.root and self.anchor not in (
                _absolute(state_paths.vault_state_dir(self.root)), _absolute(self._store.state_dir)):
            raise NativeCustodyUnavailable("native custody anchor is no longer a current owner root")

    @property
    def ledger_id(self) -> str:
        with self._connect():
            return self._ledger_id

    @property
    def physical_scope(self) -> str:
        with self._connect():
            return self._physical_scope

    @staticmethod
    def _root_identity(root: Path) -> StableIdentity:
        with held_fs.acquire(root).require() as filesystem, filesystem.parent(".").require() as parent:
            filesystem.validate_directory(parent, require_name=False).require()
            return parent.identity

    def _database_file_identity(self) -> tuple[StableIdentity, StableIdentity]:
        with held_fs.acquire(self._store.state_dir).require() as filesystem:
            with filesystem.parent(".").require() as parent:
                filesystem.validate_directory(parent, require_name=False).require()
                with filesystem.file(parent, self._store.path.name, access="identity").require() as file:
                    return parent.identity, file.identity

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._boundary()
        database_parent, database_file = self._database_file_identity()
        if (not _same(self._root_identity(self.root), self._vault_identity)
                or not _same(self._root_identity(self.anchor), self._anchor_identity)
                or not _same(database_parent, self._database_identity[0])
                or database_file != self._database_identity[1]):
            raise NativeCustodyUnavailable("native custody physical owner changed")
        current = getattr(_THREAD, "session", None)
        key = self._store
        if current is None or key in current.busy:
            # Outside a batch, or nested in another operation, a separate connection keeps transactions apart.
            with self._open() as conn:
                yield from self._checked(conn)
            return
        conn = current.connections.get(key)
        if conn is None:
            conn = self._open()
            try:
                # The session skips the last-close checkpoint, so each commit must sync the WAL itself.
                conn.execute("PRAGMA synchronous=FULL")
            except BaseException:
                conn.close()
                raise
            current.connections[key] = conn
        current.busy.add(key)
        try:
            yield from self._checked(conn)
            conn.commit()
        except BaseException:
            # A failed operation retires the shared connection; the next one opens afresh.
            current.connections.pop(key, None)
            try:
                conn.rollback()
            finally:
                conn.close()
            raise
        finally:
            current.busy.discard(key)

    def _open(self) -> sqlite3.Connection:
        try:
            return self._store._connect()
        except RuntimeError as error:
            raise NativeCustodyUnavailable("native custody database is unavailable") from error

    def _checked(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        row = conn.execute("SELECT ledger_id FROM capture_attempt_ledger WHERE singleton=1").fetchone()
        if row is None or row[0] != self._ledger_id:
            raise NativeCustodyUnavailable("native custody ledger changed")
        yield conn
        database_parent, database_file = self._database_file_identity()
        if (not _same(database_parent, self._database_identity[0])
                or database_file != self._database_identity[1]):
            raise NativeCustodyUnavailable("native custody database changed during access")

    @contextmanager
    def _held(self, binding: _Binding | None = None, *, destination: bool = False, flush: bool = False):
        self._boundary()
        with held_fs.acquire(self.anchor).require() as filesystem:
            with filesystem.parent(".").require() as anchor:
                filesystem.validate_directory(anchor, require_name=False).require()
                if not _same(anchor.identity, self._anchor_identity):
                    raise NativeCustodyUnavailable("native custody anchor changed")
            if binding is None:
                yield filesystem
                return
            relative = binding.destination_relative if destination else binding.relative_path
            if relative is None:
                raise NativeCustodyUnavailable("native allocation has no intended destination")
            parent_path, leaf = _parts(relative)
            with filesystem.parent(parent_path, **({"access": "flush"} if flush else {})).require() as parent:
                filesystem.validate_directory(parent, require_name=parent_path != ".").require()
                expected = binding.destination_parent_identity if destination else binding.parent_identity
                if expected is None or not _same(parent.identity, expected):
                    raise NativeCustodyUnavailable("native allocation parent changed")
                yield filesystem, parent, leaf
                filesystem.validate_directory(parent, require_name=parent_path != ".").require()

    def _decode(self, row) -> _Binding:
        if row is None:
            raise NativeCustodyUnavailable("native binding is unavailable")
        try:
            value = _Binding.model_validate_json(row[6])
        except ValidationError as error:
            raise NativeCustodyUnavailable("native binding is unreadable") from error
        if ((value.binding_id, value.ledger_id, value.physical_scope, value.source_ref,
             value.attempt_ticket, value.outcome) != row[:6] or value.ledger_id != self._ledger_id
                or value.physical_scope != self._physical_scope
                or not _same(value.root_identity, self._anchor_identity)):
            raise NativeCustodyUnavailable("native binding belongs to another physical owner")
        return value

    def _row(self, conn: sqlite3.Connection, binding_id: str) -> _Binding:
        return self._decode(conn.execute("SELECT binding_id,ledger_id,physical_scope,source_ref,attempt_ticket,outcome,data "
                                        "FROM source_native_bindings WHERE binding_id=?", (_id(binding_id),)).fetchone())

    def _handle(self, value: _Binding) -> NativeReservation:
        return NativeReservation(value, self._seal)

    def get(self, binding_id: str) -> NativeReservation:
        with self._connect() as conn:
            return self._handle(self._row(conn, binding_id))

    def reopen(self, binding_id: str) -> NativeReservation:
        return self.get(binding_id)

    def _reservation(self, conn: sqlite3.Connection, reservation: NativeReservation) -> _Binding:
        if not isinstance(reservation, NativeReservation) or reservation._seal is not self._seal:
            raise ValueError("native reservation belongs to another owner")
        value = self._row(conn, reservation.binding_id)
        if _allocation(value) != _allocation(reservation._binding):
            raise ValueError("native reservation allocation changed")
        return value

    @staticmethod
    def _save(conn: sqlite3.Connection, value: _Binding) -> None:
        conn.execute("UPDATE source_native_bindings SET outcome=?,data=? WHERE binding_id=?",
                     (value.outcome, value.model_dump_json(), value.binding_id))

    def reserve(self, *, relative_path: str, expected_sha256: str, expected_size: int,
                source_ref: str | None = None, attempt_ticket: str | None = None,
                image_id: str | None = None, generated_copy_id: str | None = None,
                publication_id: str | None = None, destination_relative: str | None = None,
                removable: bool = True, binding_id: str | None = None) -> NativeReservation:
        self._boundary()
        parent_path, leaf = _parts(relative_path)
        source_capture.validate_sha256(expected_sha256)
        if type(expected_size) is not int or expected_size < 0 or type(removable) is not bool:
            raise ValueError("native allocation requires a nonnegative size and removal policy")
        for value in (source_ref, attempt_ticket, image_id, generated_copy_id, publication_id):
            if value is not None:
                _opaque(value)
        with self._held() as filesystem:
            with filesystem.parent(parent_path).require() as parent:
                filesystem.validate_directory(parent, require_name=parent_path != ".").require()
                existing = filesystem.file(parent, leaf, access="identity")
                if existing.ok:
                    existing.require().close()
                    raise held_fs.HeldFsError("ALREADY_EXISTS", "native allocation name is occupied")
                if existing.error.code != "MISSING":
                    raise existing.error
                parent_identity = parent.identity
            destination_parent = None
            if destination_relative is not None:
                destination_path, _ = _parts(destination_relative)
                with filesystem.parent(destination_path).require() as parent:
                    filesystem.validate_directory(parent, require_name=destination_path != ".").require()
                    destination_parent = parent.identity
            value = _Binding(binding_id=_id(binding_id) if binding_id is not None else uuid.uuid4().hex,
                             ledger_id=self._ledger_id, physical_scope=self._physical_scope,
                             relative_path=relative_path, expected_sha256=expected_sha256, expected_size=expected_size,
                             source_ref=source_ref, attempt_ticket=attempt_ticket, image_id=image_id,
                             generated_copy_id=generated_copy_id, publication_id=publication_id,
                             destination_relative=destination_relative, removable=removable,
                             root_identity=self._anchor_identity, parent_identity=parent_identity,
                             destination_parent_identity=destination_parent)
            with self._connect() as conn:
                conn.execute("INSERT INTO source_native_bindings VALUES (?,?,?,?,?,?,?)",
                             (value.binding_id, value.ledger_id, value.physical_scope, source_ref,
                              attempt_ticket, None, value.model_dump_json()))
        return self._handle(value)

    def _named_version(self, binding: _Binding, file: HeldFile) -> FileVersion:
        if (not isinstance(file, HeldFile) or not getattr(file, "named", False)
                or getattr(file, "name", None) != _parts(binding.relative_path)[1]
                or not _same(file.filesystem.root_identity, binding.root_identity)
                or not _same(file.parent_identity, binding.parent_identity)):
            raise ValueError("native allocation handle does not name its reserved file")
        before = file.version()
        with self._held(binding) as (filesystem, parent, leaf):
            with filesystem.file(parent, leaf, access="identity").require() as named:
                if named.identity != before.identity or before.identity.link_count != 1:
                    raise ValueError("native allocation identity changed")
        if file.version() != before:
            raise ValueError("native allocation version changed")
        return before

    def bind(self, reservation: NativeReservation, file: HeldFile) -> None:
        with self._connect() as conn:
            value = self._reservation(conn, reservation)
            version = self._named_version(value, file)
            if value.outcome is not None or value.state != "reserved" or version.size != 0:
                raise ValueError("native binding requires an empty reserved allocation")
            self._save(conn, value.model_copy(update={"state": "initializing", "identity": version.identity}))
        self._live_allocations[value.binding_id] = file

    def verified(self, reservation: NativeReservation, file: HeldFile, *, sha256: str) -> None:
        source_capture.validate_sha256(sha256)
        with self._connect() as conn:
            value = self._reservation(conn, reservation)
            version = self._named_version(value, file)
            if (value.outcome is not None or value.state != "initializing" or value.identity != version.identity
                    or value.expected_sha256 != sha256 or version.size != value.expected_size):
                raise ValueError("native verification does not match its allocation")
            self._save(conn, value.model_copy(update={"state": "verified", "version": version,
                                                      "bound_sha256": sha256}))
        self._live_allocations.pop(value.binding_id, None)

    def record_installation(self, reservation: NativeReservation, witness: held_fs.InstallationWitness) -> None:
        """Persist the exact destination version while the native publisher retains its allocation."""
        if not isinstance(witness, held_fs.InstallationWitness) or not witness.authentic():
            raise ValueError("native installation requires a held publisher witness")
        with self._connect() as conn:
            value = self._reservation(conn, reservation)
            if (value.outcome is not None or value.state not in ("verified", "installed")
                    or value.destination_relative is None
                    or not _same(witness.root_identity, value.root_identity)
                    or value.destination_parent_identity is None
                    or not _same(witness.parent_identity, value.destination_parent_identity)
                    or witness.leaf != _parts(value.destination_relative)[1]
                    or not _same(witness.after.identity, value.identity)
                    or witness.after.identity.link_count != 1
                    or (witness.before.size, witness.before.mtime_ns) != (witness.after.size, witness.after.mtime_ns)
                    or (value.state == "verified" and witness.before != value.version)
                    or (value.state == "installed" and witness.after != value.version)):
                raise ValueError("native installation differs from its verified allocation")
            with self._held(value, destination=True) as (filesystem, parent, leaf):
                with filesystem.file(parent, leaf).require() as installed:
                    if installed.version() != witness.after:
                        raise ValueError("native installation destination changed")
            self._save(conn, value.model_copy(update={"state": "installed", "version": witness.after}))

    def acknowledge(self, reservation: NativeReservation, identity: StableIdentity | None,
                    outcome: Literal["not_written", "removed", "installed"]) -> None:
        # Acknowledgements are the closed native release protocol, never canonical Copy acknowledgement.
        if outcome not in ("not_written", "removed", "installed"):
            raise ValueError("invalid native acknowledgement")
        with self._connect() as conn:
            value = self._reservation(conn, reservation)
            if value.outcome is not None:
                if value.outcome == outcome and value.identity == identity:
                    return
                raise ValueError("native acknowledgement conflicts with an earlier fact")
            if (value.state == "reserved" and (outcome != "not_written" or identity is not None)
                    or value.state != "reserved" and (outcome == "not_written" or identity != value.identity)
                    or outcome == "installed" and value.state != "verified"):
                raise ValueError("native acknowledgement does not match its allocation")
            self._save(conn, value.model_copy(update={"outcome": outcome}))
        self._live_allocations.pop(value.binding_id, None)

    def record_retirement(self, reservation: NativeReservation, witness: held_fs.RetirementWitness) -> None:
        """Acknowledge the old installed allocation after its publisher closes the last named version."""
        if not isinstance(witness, held_fs.RetirementWitness) or not witness.authentic():
            raise ValueError("native retirement requires a held publisher witness")
        with self._connect() as conn:
            value = self._reservation(conn, reservation)
            if (value.state != "installed" or value.outcome not in (None, "removed")
                    or value.destination_relative is None or value.version != witness.before
                    or not _same(witness.root_identity, value.root_identity)
                    or value.destination_parent_identity is None
                    or not _same(witness.parent_identity, value.destination_parent_identity)
                    or witness.leaf != _parts(value.destination_relative)[1]
                    or not _same(witness.observed.identity, value.identity)
                    or witness.observed.identity.link_count != 0 or witness.observed.delete_pending is False):
                raise ValueError("native retirement differs from its installed allocation")
            self._save(conn, value.model_copy(update={"outcome": "removed"}))

    def _live_version(self, value: _Binding) -> FileVersion | None:
        file = self._live_allocations.get(value.binding_id)
        if file is None:
            return None
        try:
            return file.version()
        except (OSError, held_fs.HeldFsError):
            return None

    def _observe(self, value: _Binding, *, destination: bool = False) -> NativeObservation:
        handle = self._handle(value)
        if value.outcome is not None and not destination:
            return NativeObservation("installed" if value.outcome == "installed" else "closed",
                                     value.binding_id, outcome=value.outcome, reservation=handle)
        with self._held(value, destination=destination) as (filesystem, parent, leaf):
            result = filesystem.file(parent, leaf)
            if not result.ok:
                if result.error.code == "MISSING" and value.state == "reserved" and not destination:
                    return NativeObservation("reserved", value.binding_id, reservation=handle)
                raise result.error
            with result.require() as file:
                version = file.version()
                matching = value.identity == version.identity
                if value.state == "reserved" and not destination:
                    return NativeObservation("reserved", value.binding_id, version.identity, version,
                                             reservation=handle)
                if destination:
                    # Equal bytes cannot reconnect an allocation after its inode was recycled.
                    matching = matching and value.state == "installed" and version == value.version
                elif value.state == "initializing":
                    matching = matching and self._live_version(value) == version
                elif value.state == "verified":
                    matching = version == value.version and value.bound_sha256 == value.expected_sha256
                elif value.state == "installed":
                    matching = False
                state = "installed" if value.outcome == "installed" else value.state
                return NativeObservation(state if matching else "unknown", value.binding_id,
                                         version.identity, version, matching, value.outcome, handle)

    def observe(self, binding_id: str, *, destination: bool = False) -> NativeObservation:
        self._boundary()
        try:
            with self._connect() as conn:
                return self._observe(self._row(conn, binding_id), destination=destination)
        except (NativeCustodyUnavailable, sqlite3.Error, OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
            return NativeObservation("unknown", binding_id)

    def cleanup(self, binding_id: str) -> NativeObservation:
        self._boundary()
        try:
            with self._connect() as conn:
                value = self._row(conn, binding_id)
                if value.outcome is not None:
                    return self._observe(value)
                if not value.removable or value.state == "installed":
                    return NativeObservation("unknown", binding_id, reservation=self._handle(value))
                if value.state == "reserved":
                    self._save(conn, value.model_copy(update={"outcome": "not_written"}))
                else:
                    with self._held(value, flush=True) as (filesystem, parent, leaf):
                        with filesystem.file(parent, leaf, access="mutate").require() as file:
                            before = file.version()
                            if before.identity != value.identity:
                                return NativeObservation("unknown", binding_id, before.identity, before, False)
                            if value.state == "verified" and (before != value.version
                                                               or value.bound_sha256 != value.expected_sha256):
                                return NativeObservation("unknown", binding_id, before.identity, before, False)
                            if value.state == "initializing":
                                # Without the original live handle, restart cannot prove this is still our allocation.
                                if self._live_version(value) != before:
                                    return NativeObservation("unknown", binding_id, before.identity, before, False)
                            if file.version() != before:
                                return NativeObservation("unknown", binding_id, before.identity, before, False)
                            filesystem.unlink(file).require()
                            filesystem.flush_directory(parent).require()
                    self._save(conn, value.model_copy(update={"outcome": "removed"}))
                self._live_allocations.pop(value.binding_id, None)
                return self._observe(value.model_copy(update={"outcome": "not_written" if value.state == "reserved" else "removed"}))
        except (NativeCustodyUnavailable, sqlite3.Error, OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
            return NativeObservation("unknown", binding_id)

    def lookup(self, *, source_ref: str | None = None, attempt_ticket: str | None = None,
               limit: int = 32, cursor: str | None = None) -> NativePage:
        self._boundary()
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("native lookup limit must be between 1 and 128")
        if cursor is not None:
            _id(cursor)
        clauses, arguments = ["physical_scope=?", "outcome IS NULL", "binding_id>?"] , [self._physical_scope, cursor or ""]
        for name, value in (("source_ref", source_ref), ("attempt_ticket", attempt_ticket)):
            if value is not None:
                clauses.append(f"{name}=?")
                arguments.append(_opaque(value))
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT binding_id,ledger_id,physical_scope,source_ref,attempt_ticket,outcome,data "
                                    "FROM source_native_bindings WHERE " + " AND ".join(clauses)
                                    + " ORDER BY binding_id LIMIT ?", (*arguments, limit + 1)).fetchall()
                reservations = tuple(self._handle(self._decode(row)) for row in rows[:limit])
                more = len(rows) > limit
                return NativePage("partial" if more else "ready", reservations,
                                  reservations[-1].binding_id if more else None)
        except (NativeCustodyUnavailable, sqlite3.Error, OSError, held_fs.HeldFsError):
            return NativePage("unknown")
