"""Private, additive v2 authority records for canonical vocabulary effects.

This module deliberately owns only durable authority state and its transaction
ordering.  The trusted control surface supplies sealed decisions, while the
canonical mutation leaf supplies the exact byte-image classification.  Neither
ordinary request fields nor an agent-held credential can construct an approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import mutation_lock
from .cli_ops import OpError
from .governance import authorization_custody
from .governance.principal import RequestPrincipal
from .vocabulary_effects import CanonicalWriteImage, Effect
from .vocabulary_placement import (
    DATABASE_SUFFIX as _DATABASE_SUFFIX,
)
from .vocabulary_placement import (
    ENV_VOCABULARY_AUTHORITY_DIR,
    AuthorityArtifactPaths,
    VocabularyAuthorityPlacementUnavailable,
    contains_authority_artifacts,
    resolve_authority_artifact_paths,
    validated_authority_directory,
)
from .vocabulary_placement import (
    MARKER_SUFFIX as _MARKER_SUFFIX,
)

_SCHEMA_VERSION = 3
RUNTIME_SUPPORTED_CONTRACT = 2
RUNTIME_FLOOR = "vocabulary-authority/v2"
_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})
_OWNER_SEAL = object()
_FLOOR_SEAL = object()
_RECEIPT_SEAL = object()
_MARKER_VERSION = 2


def authority_artifact_paths(
    control_path: Path,
    logical_vault_id: str,
    *,
    vault_root: Path | None = None,
) -> AuthorityArtifactPaths:
    """Return the immutable marker and SQLite paths for one custody identity."""

    try:
        return resolve_authority_artifact_paths(
            Path(control_path), _text(logical_vault_id), vault_root=vault_root
        )
    except VocabularyAuthorityPlacementUnavailable:
        raise VocabularyAuthorityUnavailable from None


def transition_status(
    vault_root: Path, *, now: int
) -> Literal["v1", "v2", "unavailable"]:
    """Read one custody-bound transition state for a transition guard."""

    return VocabularyAuthority(vault_root).transition_status(now=now)


class _AuthorityError(OpError, RuntimeError):
    def __init__(self, message: str = "vocabulary authority is unavailable"):
        super().__init__(self.code, message)


class VocabularyAuthorityUnavailable(_AuthorityError):
    """The activated authority state or its external custody cannot be read."""

    code = "VOCABULARY_AUTHORITY_UNAVAILABLE"


class VocabularyAuthorityDenied(_AuthorityError):
    """No current additive authority covers the complete canonical operation."""

    code = "VOCABULARY_AUTHORITY_DENIED"


class VocabularyAuthorityConflict(_AuthorityError):
    """An operation identity or authority transition is already bound."""

    code = "VOCABULARY_AUTHORITY_CONFLICT"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _owner_binding(
    action: str,
    principal: RequestPrincipal,
    *,
    request_id: str | None = None,
    operation: Mapping[str, Any] | None = None,
    grant: Mapping[str, Any] | None = None,
    authority_id: str | None = None,
) -> str:
    context = principal.verified_authorization_session
    return _digest(
        {
            "action": action,
            "audience": principal.audience_id,
            "issuer": principal.issuer_family,
            "session": None if context is None else context.session_id,
            "cell": None if context is None else context.cell_id,
            "logical_vault": None if context is None else context.logical_vault_id,
            "keyring": None if context is None else context.keyring_id,
            "generation": None if context is None else context.credential_generation,
            "request_id": request_id,
            "operation": operation,
            "grant": grant,
            "authority_id": authority_id,
        }
    )


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("VOCABULARY_AUTHORITY_INVALID")
    return value


def _timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (1 << 63) - 1:
        raise ValueError("VOCABULARY_AUTHORITY_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class TrustedOwnerDecision:
    """A capability created only by the authenticated owner-control adapter."""

    owner_id: str
    ceremony_id: str
    binding_digest: str | None = None
    expires_at: int | None = None
    _seal: object | None = field(default=None, repr=False, compare=False)


def _trusted_owner_decision_for_adapter(
    *,
    owner_id: str,
    ceremony_id: str,
    binding_digest: str | None = None,
    expires_at: int | None = None,
) -> TrustedOwnerDecision:
    """Adapter-only construction seam; never accept this shape from a request."""

    if binding_digest is not None:
        binding_digest = _text(binding_digest, maximum=64)
    if expires_at is not None:
        expires_at = _timestamp(expires_at)
    return TrustedOwnerDecision(
        _text(owner_id), _text(ceremony_id), binding_digest, expires_at, _OWNER_SEAL
    )


@dataclass(frozen=True, slots=True)
class DeploymentFloorProof:
    """A sealed admission proof from the runtime/deployment controller."""

    runtime: str
    generation: int
    _seal: object = field(repr=False, compare=False)


def _deployment_floor_for_adapter(*, runtime: str, generation: int) -> DeploymentFloorProof:
    return DeploymentFloorProof(_text(runtime), _timestamp(generation), _FLOOR_SEAL)


@dataclass(frozen=True, slots=True)
class CanonicalReceiptEvidence:
    """Writer-only proof that one previously reserved operation committed."""

    operation_id: str
    command_digest: str
    effect_digest: str
    receipt_id: str
    _seal: object = field(repr=False, compare=False)


def _canonical_receipt_for_writer(
    *, operation: CanonicalOperation, receipt_id: str
) -> CanonicalReceiptEvidence:
    """Writer integration seam; request payloads can never create this proof."""

    return CanonicalReceiptEvidence(
        operation.operation_id,
        operation.command_digest,
        operation.effect_digest,
        _text(receipt_id),
        _RECEIPT_SEAL,
    )


@dataclass(frozen=True, slots=True)
class AuthorityScope:
    """Closed scope grammar for the initial additive action family."""

    kind: str
    project_ref: str | None = None
    endpoints: tuple[str, str] | None = None
    membership_digest: str | None = None

    @classmethod
    def vault_wide(cls) -> AuthorityScope:
        return cls("vault")

    @classmethod
    def project_edge(
        cls,
        *,
        project_ref: str,
        source_ref: str,
        target_ref: str,
        membership_digest: str,
    ) -> AuthorityScope:
        return cls(
            "project-edge",
            _text(project_ref),
            tuple(sorted((_text(source_ref), _text(target_ref)))),
            _text(membership_digest, maximum=64),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project_ref": self.project_ref,
            "endpoints": list(self.endpoints) if self.endpoints else None,
            "membership_digest": self.membership_digest,
        }


@dataclass(frozen=True, slots=True)
class CanonicalOperation:
    """One immutable, complete structural result presented to the authority gate."""

    operation_id: str
    command_digest: str
    effect_digest: str
    effects: tuple[Effect, ...]
    image_digest: str
    registry_digests: tuple[tuple[str, str], ...]
    target_digests: tuple[tuple[str, str], ...]
    scope_proofs: tuple[AuthorityScope, ...] = ()

    @classmethod
    def from_effects(
        cls,
        *,
        operation_id: str,
        command_digest: str,
        effects: Iterable[Effect],
        image_digest: str,
        registry_digests: Mapping[str, str],
        target_digests: Mapping[str, str],
        scope_proofs: Iterable[AuthorityScope] = (),
    ) -> CanonicalOperation:
        normalized = tuple(sorted(tuple(effects), key=lambda item: (item.action, item.path, item.key or "")))
        if not normalized or not all(isinstance(item, Effect) for item in normalized):
            raise ValueError("VOCABULARY_AUTHORITY_INVALID")
        if any(item.action not in _ACTIONS for item in normalized):
            raise ValueError("VOCABULARY_AUTHORITY_INVALID")
        proofs = tuple(scope_proofs)
        if not all(isinstance(item, AuthorityScope) for item in proofs):
            raise ValueError("VOCABULARY_AUTHORITY_INVALID")
        image = _text(image_digest, maximum=64)
        registries = _digest_pairs(registry_digests)
        targets = _digest_pairs(target_digests)
        return cls(
            _text(operation_id),
            _text(command_digest, maximum=64),
            _digest(
                {
                    "effects": [item.as_dict() for item in normalized],
                    "image_digest": image,
                    "registry_digests": registries,
                    "target_digests": targets,
                    "scope_proofs": [proof.as_dict() for proof in proofs],
                }
            ),
            normalized,
            image,
            registries,
            targets,
            proofs,
        )

    def effect_manifest(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.effects]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "command_digest": self.command_digest,
            "effects": self.effect_manifest(),
            "image_digest": self.image_digest,
            "registry_digests": dict(self.registry_digests),
            "target_digests": dict(self.target_digests),
            "scope_proofs": [proof.as_dict() for proof in self.scope_proofs],
        }

    @classmethod
    def _from_record(cls, value: Mapping[str, Any]) -> CanonicalOperation:
        effects = tuple(
            Effect(item["action"], item["path"], item.get("key"), item.get("details", {}))
            for item in value["effects"]
        )
        proofs = tuple(
            AuthorityScope(
                item["kind"],
                item.get("project_ref"),
                tuple(item["endpoints"]) if item.get("endpoints") else None,
                item.get("membership_digest"),
            )
            for item in value.get("scope_proofs", ())
        )
        return cls.from_effects(
            operation_id=value["operation_id"],
            command_digest=value["command_digest"],
            effects=effects,
            image_digest=value["image_digest"],
            registry_digests=value["registry_digests"],
            target_digests=value["target_digests"],
            scope_proofs=proofs,
        )


def _digest_pairs(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    normalized = tuple(sorted((_text(key), _text(value, maximum=64)) for key, value in values.items()))
    return normalized


@dataclass(frozen=True, slots=True)
class AuthorityStatus:
    mode: str
    generation: int | None
    active_grants: int


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    operation_id: str
    authority_ids: tuple[str, ...]
    generation: int
    state: str


@dataclass(frozen=True, slots=True)
class AuthorityRequestStatus:
    request_id: str
    state: str
    expires_at: int
    display_effects: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class OwnerRequestPreview:
    operation: CanonicalOperation
    images: tuple[CanonicalWriteImage, ...]


class _CommitGuard(AbstractContextManager["_CommitGuard"]):
    def __init__(self, store: VocabularyAuthority, reservation: Reservation, principal: RequestPrincipal, now: int | None):
        self._store = store
        self.reservation = reservation
        self._principal = principal
        self._now = now
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    def __enter__(self) -> _CommitGuard:
        initial = self._store._validate_custody(self._principal, now=self._store._now(self._now))
        connection = self._store._connect(initial, create=False)
        if connection is None:
            raise VocabularyAuthorityUnavailable
        self._connection = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._store._now(self._now)
            operation = self._store._operation_for_reservation(connection, self.reservation)
            current_custody = self._store._validate_custody(self._principal, now=now)
            if self._store._database_path(current_custody) != self._store._database_path(initial):
                raise VocabularyAuthorityUnavailable
            self._store._validate_active(connection, operation, self._principal, now=now, require_reservation=True)
            return self
        except Exception:
            connection.rollback()
            connection.close()
            self._connection = None
            raise

    def mark_committed(self, evidence: CanonicalReceiptEvidence) -> None:
        if self._connection is None or self._closed:
            raise VocabularyAuthorityConflict("commit guard is closed")
        if (
            not isinstance(evidence, CanonicalReceiptEvidence)
            or evidence._seal is not _RECEIPT_SEAL
            or (evidence.operation_id, evidence.command_digest, evidence.effect_digest)
            != self._connection.execute(
                "SELECT operation_id, command_digest, effect_digest FROM reservations WHERE reservation_id = ?",
                (self.reservation.reservation_id,),
            ).fetchone()
        ):
            raise VocabularyAuthorityDenied("canonical receipt evidence required")
        updated = self._connection.execute(
            "UPDATE reservations SET state = 'committed', receipt_id = ? "
            "WHERE reservation_id = ? AND state = 'reserved'",
            (evidence.receipt_id, self.reservation.reservation_id),
        )
        if updated.rowcount != 1:
            raise VocabularyAuthorityConflict("reservation is no longer current")
        self._connection.execute(
            "UPDATE authorities SET status = 'spent' WHERE authority_id IN "
            "(SELECT authority_id FROM reservation_authorities WHERE reservation_id = ?) "
            "AND kind = 'exact'",
            (self.reservation.reservation_id,),
        )
        self._connection.commit()
        self._closed = True
        self._connection.close()
        self._connection = None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._connection is not None:
            self._connection.rollback()
            self._connection.close()
            self._connection = None
        self._closed = True
        return False


class VocabularyAuthority:
    """The private v2 additive authority store for one vault.

    A missing file intentionally means v1.  Once a file exists, malformed or
    unavailable state is a v2 refusal; callers must never treat it as absence.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        custody_loader: Callable[..., object] = authorization_custody.load_authorization_custody,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        self._custody_loader = custody_loader
        self._clock = clock or __import__("time").time

    def _custody_leaf(self, custody: object, suffix: str) -> Path:
        paths = authority_artifact_paths(
            Path(custody.control_path),
            custody.control.logical_vault_id,
            vault_root=self.vault_root,
        )
        if suffix == _MARKER_SUFFIX:
            return paths.marker_path
        if suffix == _DATABASE_SUFFIX:
            return paths.database_path
        raise ValueError("unknown vocabulary authority artifact")

    def _marker_path(self, custody: object) -> Path:
        return self._custody_leaf(custody, _MARKER_SUFFIX)

    def _database_path(self, custody: object) -> Path:
        return self._custody_leaf(custody, _DATABASE_SUFFIX)

    def _now(self, supplied: int | None) -> int:
        return _timestamp(int(self._clock()) if supplied is None else supplied)

    @staticmethod
    def _artifact_exists(path: Path) -> bool:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise VocabularyAuthorityUnavailable from None
        return True

    def _artifacts_are_absent(self, custody: object) -> bool:
        database = self._database_path(custody)
        return not any(
            self._artifact_exists(path)
            for path in (
                self._marker_path(custody),
                database,
                *(database.with_name(f"{database.name}{suffix}") for suffix in ("-journal", "-wal", "-shm")),
            )
        )

    @staticmethod
    def _custody_floor(custody: object) -> int:
        floor = getattr(custody.control, "vocabulary_authority_floor", 1)
        if isinstance(floor, bool) or floor not in {1, 2}:
            raise VocabularyAuthorityUnavailable
        return floor

    def _marker(self, custody: object) -> dict[str, Any] | None:
        path = self._marker_path(custody)
        if self._artifacts_are_absent(custody):
            if self._custody_floor(custody) == 1:
                return None
            raise VocabularyAuthorityUnavailable
        if not authorization_custody._private_parent_is_safe(path.parent):  # noqa: SLF001
            raise VocabularyAuthorityUnavailable
        if not self._artifact_exists(path):
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not 1 <= info.st_size <= 2048
                    or (
                        os.name != "nt"
                        and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600})
                    )
                ):
                    raise ValueError
                raw = os.read(descriptor, 2049)
            finally:
                os.close(descriptor)
            if not 1 <= len(raw) <= 2048:
                raise ValueError
            value = json.loads(raw)
            required = {
                "version", "store_id", "cell_id", "logical_vault_id", "keyring_id",
                "min_runtime", "activated_at", "authorizer",
            }
            if not isinstance(value, dict) or set(value) != required:
                raise ValueError
            if (
                value["version"] != _MARKER_VERSION
                or value["cell_id"] != custody.control.cell_id
                or value["logical_vault_id"] != custody.control.logical_vault_id
                or value["keyring_id"] != custody.keyring.keyring_id
                or value["min_runtime"] != RUNTIME_FLOOR
            ):
                raise ValueError
            _text(value["store_id"])
            _text(value["authorizer"])
            _timestamp(value["activated_at"])
            return value
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise VocabularyAuthorityUnavailable from None

    def _create_marker(
        self, custody: object, *, floor: DeploymentFloorProof, owner: TrustedOwnerDecision, now: int
    ) -> dict[str, Any]:
        existing = None if self._artifacts_are_absent(custody) else self._marker(custody)
        if existing is not None:
            return existing
        path = self._marker_path(custody)
        if not authorization_custody._private_parent_is_safe(path.parent):  # noqa: SLF001
            raise VocabularyAuthorityUnavailable
        if self._database_path(custody).exists():
            raise VocabularyAuthorityUnavailable
        marker = {
            "version": _MARKER_VERSION,
            "store_id": f"vocab-authority-{uuid.uuid4()}",
            "cell_id": custody.control.cell_id,
            "logical_vault_id": custody.control.logical_vault_id,
            "keyring_id": custody.keyring.keyring_id,
            "min_runtime": floor.runtime,
            "activated_at": now,
            "authorizer": owner.owner_id,
        }
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("short marker write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if os.name != "nt":
                parent = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            return marker
        except FileExistsError:
            return self._marker(custody) or (_ for _ in ()).throw(VocabularyAuthorityUnavailable())
        except OSError:
            raise VocabularyAuthorityUnavailable from None

    def _connect(self, custody: object, *, create: bool, marker: Mapping[str, Any] | None = None) -> sqlite3.Connection | None:
        current_marker = dict(marker) if marker is not None else self._marker(custody)
        if current_marker is None:
            return None
        if self._custody_floor(custody) != 2:
            raise VocabularyAuthorityUnavailable
        path = self._database_path(custody)
        if not authorization_custody._private_parent_is_safe(path.parent):  # noqa: SLF001
            raise VocabularyAuthorityUnavailable
        if not path.exists() and not create:
            raise VocabularyAuthorityUnavailable
        try:
            if create and not path.exists():
                descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._validate_sqlite_sidecars(path)
            retained = mutation_lock.retain_regular_file(path)
            try:
                info = os.fstat(retained.fd)
                if not authorization_custody._file_is_owner_protected(retained.fd, info):  # noqa: SLF001
                    raise VocabularyAuthorityUnavailable
                connection = sqlite3.connect(path, timeout=5, isolation_level=None)
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                self._ensure_schema(connection, create=create, marker=current_marker)
                self._validate_sqlite_sidecars(path)
                if not mutation_lock._same_file_entry(retained.directory, path.name, retained.fd):  # noqa: SLF001
                    connection.close()
                    raise VocabularyAuthorityUnavailable
            finally:
                retained.close()
            return connection
        except (OSError, sqlite3.Error, ValueError):
            try:
                connection.close()
            except UnboundLocalError:
                pass
            raise VocabularyAuthorityUnavailable from None

    @staticmethod
    def _validate_sqlite_sidecars(path: Path) -> None:
        """Reject pre-existing SQLite journals outside the custody protection rule."""

        for suffix in ("-journal", "-wal", "-shm"):
            candidate = path.with_name(f"{path.name}{suffix}")
            try:
                os.lstat(candidate)
            except FileNotFoundError:
                continue
            retained = mutation_lock.retain_regular_file(candidate)
            try:
                info = os.fstat(retained.fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not authorization_custody._file_is_owner_protected(retained.fd, info)  # noqa: SLF001
                    or not mutation_lock._same_file_entry(  # noqa: SLF001
                        retained.directory, candidate.name, retained.fd
                    )
                ):
                    raise VocabularyAuthorityUnavailable
            finally:
                retained.close()

    def _ensure_schema(self, connection: sqlite3.Connection, *, create: bool, marker: Mapping[str, Any]) -> None:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not tables:
            if not create:
                raise VocabularyAuthorityUnavailable
            connection.executescript(
                """
                CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE activation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    contract_version INTEGER NOT NULL, generation INTEGER NOT NULL,
                    cell_id TEXT NOT NULL, logical_vault_id TEXT NOT NULL,
                    keyring_id TEXT NOT NULL, store_id TEXT NOT NULL,
                    min_runtime TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE authorities (
                    authority_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    audience_id TEXT NOT NULL, issuer_family TEXT NOT NULL, cell_id TEXT NOT NULL,
                    logical_vault_id TEXT NOT NULL, keyring_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    actions_json TEXT NOT NULL, scope_json TEXT NOT NULL, command_digest TEXT,
                    effect_digest TEXT, operation_id TEXT, expires_at INTEGER NOT NULL,
                    owner_id TEXT NOT NULL, ceremony_id TEXT NOT NULL
                );
                CREATE TABLE reservations (
                    reservation_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                    audience_id TEXT NOT NULL, command_digest TEXT NOT NULL, effect_digest TEXT NOT NULL,
                    effects_json TEXT NOT NULL, proofs_json TEXT NOT NULL, generation INTEGER NOT NULL,
                    operation_json TEXT NOT NULL, state TEXT NOT NULL, receipt_id TEXT
                );
                CREATE TABLE reservation_authorities (
                    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
                    authority_id TEXT NOT NULL REFERENCES authorities(authority_id),
                    PRIMARY KEY (reservation_id, authority_id)
                );
                CREATE TABLE authority_uses (
                    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
                    effect_index INTEGER NOT NULL,
                    authority_id TEXT NOT NULL REFERENCES authorities(authority_id),
                    action TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    PRIMARY KEY (reservation_id, effect_index)
                );
                CREATE TABLE authority_requests (
                    request_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                    audience_id TEXT NOT NULL, issuer_family TEXT NOT NULL,
                    cell_id TEXT NOT NULL, logical_vault_id TEXT NOT NULL,
                    keyring_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    operation_json TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL, authority_id TEXT REFERENCES authorities(authority_id)
                );
                CREATE TABLE owner_decisions (
                    ceremony_id TEXT PRIMARY KEY, binding_digest TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, action TEXT NOT NULL
                );
                INSERT INTO metadata(name, value) VALUES ('schema_version', '3');
                """
            )
            return
        required = {
            "metadata", "activation", "authorities", "reservations",
            "reservation_authorities", "authority_uses", "authority_requests", "owner_decisions",
        }
        if tables != required:
            raise VocabularyAuthorityUnavailable
        row = connection.execute("SELECT value FROM metadata WHERE name = 'schema_version'").fetchone()
        if row != (str(_SCHEMA_VERSION),):
            raise VocabularyAuthorityUnavailable

    def _validate_custody(self, principal: RequestPrincipal, *, now: int) -> object:
        context = principal.verified_authorization_session
        if (
            context is None
            or not principal.resolved
            or principal.audience_id != context.principal_id
            or principal.authorization_session_id != context.session_id
            or principal.issuer_family != context.issuer_family
            or context.expires_at < now
        ):
            raise VocabularyAuthorityUnavailable
        try:
            custody = self._custody_loader(self.vault_root, now=now)
            control = custody.control
            keyring = custody.keyring
            if (
                context.cell_id != control.cell_id
                or context.logical_vault_id != control.logical_vault_id
                or context.keyring_id != keyring.keyring_id
            ):
                raise VocabularyAuthorityUnavailable
            from .governance import authorization_session_lifecycle, store

            connection = store.open_authorization_session_connection(self.vault_root)
            try:
                authorization_session_lifecycle.status_verified_session(
                    connection, custody=custody, context=context, now=now
                )
            finally:
                connection.close()
            return custody
        except VocabularyAuthorityUnavailable:
            raise
        except Exception:  # noqa: BLE001 - custody parsing is a fail-closed boundary
            raise VocabularyAuthorityUnavailable from None

    def _revalidate_after_lock(
        self,
        principal: RequestPrincipal,
        *,
        supplied_now: int | None,
        decision: TrustedOwnerDecision | None = None,
        binding: str | None = None,
    ) -> tuple[int, object, TrustedOwnerDecision | None]:
        """Refresh time and session custody after waiting for SQLite's writer lock."""

        current = self._now(supplied_now)
        custody = self._validate_custody(principal, now=current)
        owner = None
        if decision is not None:
            if binding is None:
                raise VocabularyAuthorityDenied("trusted owner decision does not match this operation")
            owner = self._require_owner(decision, binding=binding, now=current)
        return current, custody, owner

    @staticmethod
    def _require_owner(
        decision: TrustedOwnerDecision, *, binding: str, now: int
    ) -> TrustedOwnerDecision:
        if (
            not isinstance(decision, TrustedOwnerDecision)
            or decision._seal is not _OWNER_SEAL
            or decision.binding_digest != binding
            or decision.expires_at is None
            or decision.expires_at < now
        ):
            raise VocabularyAuthorityDenied("trusted owner decision required")
        return decision

    @staticmethod
    def _consume_owner_decision(
        connection: sqlite3.Connection,
        decision: TrustedOwnerDecision,
        *,
        binding: str,
        action: str,
    ) -> None:
        try:
            connection.execute(
                "INSERT INTO owner_decisions VALUES (?, ?, ?, ?)",
                (decision.ceremony_id, binding, decision.expires_at, action),
            )
        except sqlite3.IntegrityError:
            raise VocabularyAuthorityDenied("trusted owner decision was already consumed") from None

    @staticmethod
    def _require_binding(binding: str, expected: str) -> str:
        if binding != expected:
            raise VocabularyAuthorityDenied("trusted owner decision does not match this operation")
        return binding

    @staticmethod
    def _require_floor(proof: DeploymentFloorProof) -> DeploymentFloorProof:
        if not isinstance(proof, DeploymentFloorProof) or proof._seal is not _FLOOR_SEAL:
            raise VocabularyAuthorityUnavailable
        return proof

    def status(self, principal: RequestPrincipal, *, now: int | None = None) -> AuthorityStatus:
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            return AuthorityStatus("v1", None, 0)
        try:
            activation = connection.execute("SELECT generation, cell_id, logical_vault_id, keyring_id, store_id, status FROM activation WHERE singleton = 1").fetchone()
            if activation is None:
                raise VocabularyAuthorityUnavailable
            self._activation_matches(activation, custody, self._marker(custody))
            if activation[5] != "active":
                raise VocabularyAuthorityUnavailable
            grants = connection.execute(
                "SELECT COUNT(*) FROM authorities WHERE kind = 'grant' AND status = 'active' "
                "AND audience_id = ? AND expires_at >= ?",
                (principal.audience_id, current),
            ).fetchone()[0]
            return AuthorityStatus("v2", int(activation[0]), int(grants))
        finally:
            connection.close()

    def runtime_status(self, *, now: int | None = None) -> AuthorityStatus:
        """Startup admission check, deliberately independent of request sessions."""

        if self._custody_loader is authorization_custody.load_authorization_custody and (
            os.environ.get(authorization_custody.KEYRING_FILE_ENV) is None
            and os.environ.get(authorization_custody.CONTROL_FILE_ENV) is None
        ):
            if os.environ.get(ENV_VOCABULARY_AUTHORITY_DIR) is not None:
                try:
                    directory = validated_authority_directory(self.vault_root)
                    if contains_authority_artifacts(directory):
                        return AuthorityStatus("unavailable", None, 0)
                except VocabularyAuthorityPlacementUnavailable:
                    return AuthorityStatus("unavailable", None, 0)
            return AuthorityStatus("v1", None, 0)
        try:
            current = self._now(now)
            custody = self._custody_loader(self.vault_root, now=current)
            connection = self._connect(custody, create=False)
        except (
            VocabularyAuthorityUnavailable,
            authorization_custody.AuthorizationCustodyUnavailable,
            OSError,
            TypeError,
            ValueError,
        ):
            return AuthorityStatus("unavailable", None, 0)
        if connection is None:
            return AuthorityStatus("v1", None, 0)
        try:
            try:
                row = connection.execute(
                    "SELECT contract_version, generation, cell_id, logical_vault_id, keyring_id, store_id, min_runtime, status "
                    "FROM activation WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    # The marker is durable before schema initialization and
                    # activation.  Returning v1 here would turn that crash
                    # window into a silent authority downgrade.
                    return AuthorityStatus("unavailable", None, 0)
                if (
                    int(row[0]) != RUNTIME_SUPPORTED_CONTRACT
                    or row[6] != RUNTIME_FLOOR
                    or row[7] != "active"
                ):
                    return AuthorityStatus("unavailable", None, 0)
                self._activation_matches((row[1], row[2], row[3], row[4], row[5]), custody, self._marker(custody))
                return AuthorityStatus("v2", int(row[1]), 0)
            except Exception:  # noqa: BLE001 - startup inspection is fail-closed
                return AuthorityStatus("unavailable", None, 0)
        finally:
            connection.close()

    def transition_status(self, *, now: int | None = None) -> str:
        """Return the authenticated v1/v2/unavailable transition state."""

        return self.runtime_status(now=now).mode

    def activate(self, *, principal: RequestPrincipal, decision: TrustedOwnerDecision, deployment_floor: DeploymentFloorProof, binding: str, now: int | None = None) -> AuthorityStatus:
        current = self._now(now)
        floor = self._require_floor(deployment_floor)
        binding = self._require_binding(
            binding,
            _owner_binding(
                "activate",
                principal,
                operation={"runtime": floor.runtime, "generation": floor.generation},
            ),
        )
        owner = self._require_owner(decision, binding=binding, now=current)
        custody = self._validate_custody(principal, now=current)
        if self._custody_floor(custody) != 2:
            raise VocabularyAuthorityUnavailable
        if floor.generation != custody.control.activation_epoch or floor.runtime != RUNTIME_FLOOR:
            raise VocabularyAuthorityUnavailable
        marker = self._create_marker(custody, floor=floor, owner=owner, now=current)
        connection = self._connect(custody, create=True, marker=marker)
        assert connection is not None
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, owner = self._revalidate_after_lock(
                principal,
                supplied_now=now,
                decision=decision,
                binding=binding,
            )
            assert owner is not None
            if floor.generation != custody.control.activation_epoch or floor.runtime != RUNTIME_FLOOR:
                raise VocabularyAuthorityUnavailable
            if self._marker(custody) != marker:
                raise VocabularyAuthorityUnavailable
            row = connection.execute("SELECT generation, cell_id, logical_vault_id, keyring_id, store_id, min_runtime, status FROM activation WHERE singleton = 1").fetchone()
            if row is None:
                self._consume_owner_decision(connection, owner, binding=binding, action="activate")
                connection.execute(
                    "INSERT INTO activation VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (RUNTIME_SUPPORTED_CONTRACT, custody.control.activation_epoch, custody.control.cell_id, custody.control.logical_vault_id, custody.keyring.keyring_id, marker["store_id"], floor.runtime),
                )
            else:
                self._activation_matches((row[0], row[1], row[2], row[3], row[4]), custody, marker)
                if row[5] != floor.runtime or row[6] != "active":
                    raise VocabularyAuthorityConflict("activated authority differs from deployment floor")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.status(principal, now=current)

    @staticmethod
    def _activation_matches(row: tuple[object, ...], custody: object, marker: Mapping[str, Any] | None) -> None:
        if (
            int(row[0]) != custody.control.activation_epoch
            or row[1] != custody.control.cell_id
            or row[2] != custody.control.logical_vault_id
            or row[3] != custody.keyring.keyring_id
            or marker is None
            or row[4] != marker.get("store_id")
        ):
            raise VocabularyAuthorityUnavailable

    def grant(self, *, principal: RequestPrincipal, decision: TrustedOwnerDecision, audience: RequestPrincipal, actions: Iterable[str], scope: AuthorityScope, expires_at: int, binding: str, now: int | None = None) -> str:
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        self._validate_custody(audience, now=current)
        actions_tuple = tuple(sorted(set(actions)))
        if not actions_tuple or any(action not in _ACTIONS for action in actions_tuple) or expires_at <= current:
            raise VocabularyAuthorityDenied("invalid additive grant")
        self._validate_scope(actions_tuple, scope)
        binding = self._require_binding(
            binding,
            _owner_binding(
                "grant",
                principal,
                grant={
                    "actions": list(actions_tuple),
                    "scope": scope.as_dict(),
                    "expires_at": expires_at,
                },
            ),
        )
        owner = self._require_owner(decision, binding=binding, now=current)
        authority_id = f"vocab-auth-{uuid.uuid4()}"
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, owner = self._revalidate_after_lock(
                principal,
                supplied_now=now,
                decision=decision,
                binding=binding,
            )
            assert owner is not None
            if expires_at <= current:
                raise VocabularyAuthorityDenied("invalid additive grant")
            self._require_active(connection, custody)
            self._consume_owner_decision(connection, owner, binding=binding, action="grant")
            connection.execute(
                "INSERT INTO authorities VALUES (?, 'grant', 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
                (authority_id, audience.audience_id, audience.issuer_family, custody.control.cell_id, custody.control.logical_vault_id, custody.keyring.keyring_id, custody.control.activation_epoch, json.dumps(actions_tuple), json.dumps(scope.as_dict(), sort_keys=True), _timestamp(expires_at), owner.owner_id, owner.ceremony_id),
            )
            connection.commit()
            return authority_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve_exact(self, *, principal: RequestPrincipal, decision: TrustedOwnerDecision, audience: RequestPrincipal, operation: CanonicalOperation, expires_at: int, binding: str, now: int | None = None) -> str:
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        self._validate_custody(audience, now=current)
        if expires_at <= current:
            raise VocabularyAuthorityDenied("expired exact approval")
        binding = self._require_binding(
            binding,
            _owner_binding("approve-exact", principal, operation=operation.as_dict()),
        )
        owner = self._require_owner(decision, binding=binding, now=current)
        authority_id = f"vocab-auth-{uuid.uuid4()}"
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, owner = self._revalidate_after_lock(
                principal,
                supplied_now=now,
                decision=decision,
                binding=binding,
            )
            assert owner is not None
            if expires_at <= current:
                raise VocabularyAuthorityDenied("expired exact approval")
            self._require_active(connection, custody)
            self._consume_owner_decision(connection, owner, binding=binding, action="approve-exact")
            authority_id = self._insert_exact(
                connection,
                operation=operation,
                audience_id=audience.audience_id,
                issuer_family=str(audience.issuer_family),
                custody=custody,
                expires_at=_timestamp(expires_at),
                owner=owner,
                authority_id=authority_id,
            )
            connection.commit()
            return authority_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_exact(
        connection: sqlite3.Connection,
        *,
        operation: CanonicalOperation,
        audience_id: str,
        issuer_family: str,
        custody: object,
        expires_at: int,
        owner: TrustedOwnerDecision,
        authority_id: str | None = None,
    ) -> str:
        identifier = authority_id or f"vocab-auth-{uuid.uuid4()}"
        connection.execute(
            "INSERT INTO authorities VALUES (?, 'exact', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                audience_id,
                issuer_family,
                custody.control.cell_id,
                custody.control.logical_vault_id,
                custody.keyring.keyring_id,
                custody.control.activation_epoch,
                json.dumps(sorted({effect.action for effect in operation.effects})),
                json.dumps({"kind": "exact"}),
                operation.command_digest,
                operation.effect_digest,
                operation.operation_id,
                expires_at,
                owner.owner_id,
                owner.ceremony_id,
            ),
        )
        return identifier

    @staticmethod
    def _validate_scope(actions: tuple[str, ...], scope: AuthorityScope) -> None:
        if scope.kind == "vault" and set(actions) <= {"entity.create", "entity_type.add", "relation_type.add"}:
            return
        if scope.kind == "project-edge" and set(actions) == {"edge.add"} and scope.project_ref and scope.endpoints and scope.membership_digest:
            return
        raise VocabularyAuthorityDenied("unsupported additive authority scope")

    def _require_active(self, connection: sqlite3.Connection, custody: object) -> None:
        row = connection.execute("SELECT generation, cell_id, logical_vault_id, keyring_id, store_id, status FROM activation WHERE singleton = 1").fetchone()
        if row is None or row[5] != "active":
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        self._activation_matches(row, custody, self._marker(custody))

    def request(
        self,
        operation: CanonicalOperation,
        *,
        principal: RequestPrincipal,
        expires_at: int,
        images: Iterable[CanonicalWriteImage] | None = None,
        now: int | None = None,
    ) -> AuthorityRequestStatus:
        """Persist a pending exact request; it is never an authority grant."""

        from .vocabulary_preview import encode_preview

        # The existing operation record is extensible. Its optional preview is
        # bound by image_digest, not part of the canonical operation identity.
        record = operation.as_dict()
        if images is not None:
            record["write_preview"] = encode_preview(images, operation)
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        if _timestamp(expires_at) <= current:
            raise VocabularyAuthorityDenied("expired authority request")
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, _ = self._revalidate_after_lock(
                principal, supplied_now=now
            )
            if _timestamp(expires_at) <= current:
                raise VocabularyAuthorityDenied("expired authority request")
            self._require_active(connection, custody)
            row = connection.execute(
                "SELECT request_id, audience_id, issuer_family, operation_json, expires_at, state "
                "FROM authority_requests WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if row is not None:
                try:
                    stored_operation = CanonicalOperation._from_record(json.loads(str(row[3])))
                except (KeyError, TypeError, ValueError):
                    raise VocabularyAuthorityUnavailable from None
                if row[1:3] != (
                    principal.audience_id,
                    principal.issuer_family,
                ) or stored_operation != operation:
                    raise VocabularyAuthorityConflict("operation identity is already requested")
                connection.commit()
                return self._request_status_row(row, principal, current)
            request_id = f"vocab-request-{uuid.uuid4()}"
            operation_json = json.dumps(record, sort_keys=True)
            connection.execute(
                "INSERT INTO authority_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)",
                (
                    request_id,
                    operation.operation_id,
                    principal.audience_id,
                    principal.issuer_family,
                    custody.control.cell_id,
                    custody.control.logical_vault_id,
                    custody.keyring.keyring_id,
                    custody.control.activation_epoch,
                    operation_json,
                    _timestamp(expires_at),
                ),
            )
            connection.commit()
            return AuthorityRequestStatus(
                request_id, "pending", _timestamp(expires_at), self._display_effects(operation)
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _display_effects(operation: CanonicalOperation) -> tuple[dict[str, str], ...]:
        if len(operation.effects) > 8:
            raise VocabularyAuthorityDenied("authority request effect display exceeds bound")
        return tuple(
            {
                "action": effect.action,
                "path": effect.path,
                "key": effect.key or "",
            }
            for effect in operation.effects
        )

    def _request_status_row(
        self, row: tuple[object, ...], principal: RequestPrincipal, now: int
    ) -> AuthorityRequestStatus:
        if row[1] != principal.audience_id or row[2] != principal.issuer_family:
            raise VocabularyAuthorityDenied("authority request is not disclosed to this principal")
        try:
            operation = CanonicalOperation._from_record(json.loads(str(row[3])))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise VocabularyAuthorityUnavailable from None
        state = str(row[5])
        if int(row[4]) < now and state == "pending":
            state = "expired"
        return AuthorityRequestStatus(str(row[0]), state, int(row[4]), self._display_effects(operation))

    def request_status(
        self, request_id: str, *, principal: RequestPrincipal, now: int | None = None
    ) -> AuthorityRequestStatus:
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            row = connection.execute(
                "SELECT request_id, audience_id, issuer_family, operation_json, expires_at, state "
                "FROM authority_requests WHERE request_id = ?",
                (_text(request_id),),
            ).fetchone()
            if row is None:
                raise VocabularyAuthorityDenied("authority request is unavailable")
            return self._request_status_row(row, principal, current)
        finally:
            connection.close()

    def inspect_request_for_owner(
        self, request_id: str, *, principal: RequestPrincipal, now: int | None = None
    ) -> CanonicalOperation:
        """Return the complete stored request only to the current request audience."""

        record = self._inspect_request_record(request_id, principal=principal, now=now)
        try:
            return CanonicalOperation._from_record(record)
        except (KeyError, TypeError, ValueError):
            raise VocabularyAuthorityUnavailable from None

    @staticmethod
    def _request_preview(record: Mapping[str, Any]) -> OwnerRequestPreview:
        from .vocabulary_preview import decode_preview

        try:
            operation = CanonicalOperation._from_record(record)
            images = decode_preview(record["write_preview"], operation)
            return OwnerRequestPreview(operation, images)
        except (KeyError, TypeError, ValueError):
            raise VocabularyAuthorityUnavailable("exact request preview is unavailable") from None

    def inspect_request_preview_for_owner(
        self, request_id: str, *, principal: RequestPrincipal, now: int | None = None
    ) -> OwnerRequestPreview:
        """Read exact proposed bytes with the same audience and currency checks."""

        return self._request_preview(
            self._inspect_request_record(request_id, principal=principal, now=now)
        )

    def _inspect_request_record(
        self, request_id: str, *, principal: RequestPrincipal, now: int | None = None
    ) -> dict[str, Any]:

        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            row = connection.execute(
                "SELECT audience_id, issuer_family, cell_id, logical_vault_id, keyring_id, generation, operation_json, expires_at, state "
                "FROM authority_requests WHERE request_id = ?",
                (_text(request_id),),
            ).fetchone()
            if (
                row is None
                or row[0] != principal.audience_id
                or row[1] != principal.issuer_family
                or int(row[7]) < current
                or row[8] != "pending"
                or tuple(row[2:6])
                != (
                    custody.control.cell_id,
                    custody.control.logical_vault_id,
                    custody.keyring.keyring_id,
                    custody.control.activation_epoch,
                )
            ):
                raise VocabularyAuthorityDenied("authority request is unavailable")
            try:
                record = json.loads(str(row[6]))
                if not isinstance(record, dict):
                    raise ValueError
                return record
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise VocabularyAuthorityUnavailable from None
        finally:
            connection.close()

    def deny_request(
        self,
        request_id: str,
        *,
        principal: RequestPrincipal,
        decision: TrustedOwnerDecision,
        binding: str,
        now: int | None = None,
    ) -> None:
        """Durably close a displayed pending request without granting authority."""

        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, _ = self._revalidate_after_lock(
                principal, supplied_now=now
            )
            self._require_active(connection, custody)
            row = connection.execute(
                "SELECT operation_json FROM authority_requests WHERE request_id = ? "
                "AND audience_id = ? AND issuer_family = ? AND state = 'pending' AND expires_at >= ?",
                (_text(request_id), principal.audience_id, principal.issuer_family, current),
            ).fetchone()
            if row is None:
                raise VocabularyAuthorityDenied("authority request is unavailable")
            try:
                operation = CanonicalOperation._from_record(json.loads(str(row[0])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise VocabularyAuthorityUnavailable from None
            binding = self._require_binding(
                binding,
                _owner_binding(
                    "deny-request",
                    principal,
                    request_id=request_id,
                    operation=operation.as_dict(),
                ),
            )
            owner = self._require_owner(decision, binding=binding, now=current)
            self._consume_owner_decision(connection, owner, binding=binding, action="deny-request")
            changed = connection.execute(
                "UPDATE authority_requests SET state = 'denied' WHERE request_id = ? "
                "AND audience_id = ? AND issuer_family = ? AND state = 'pending' AND expires_at >= ?",
                (_text(request_id), principal.audience_id, principal.issuer_family, current),
            )
            if changed.rowcount != 1:
                raise VocabularyAuthorityDenied("authority request is unavailable")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve_request(
        self,
        request_id: str,
        *,
        principal: RequestPrincipal,
        decision: TrustedOwnerDecision,
        binding: str,
        now: int | None = None,
    ) -> str:
        """Control-only transition from stored pending operation to exact approval."""

        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, _ = self._revalidate_after_lock(
                principal, supplied_now=now
            )
            self._require_active(connection, custody)
            row = connection.execute(
                "SELECT audience_id, issuer_family, cell_id, logical_vault_id, keyring_id, generation, operation_json, expires_at, state, authority_id "
                "FROM authority_requests WHERE request_id = ?",
                (_text(request_id),),
            ).fetchone()
            if row is None or row[8] not in {"pending", "approved"}:
                raise VocabularyAuthorityDenied("authority request is not pending")
            if row[0:2] != (principal.audience_id, principal.issuer_family):
                raise VocabularyAuthorityDenied("authority request is unavailable")
            if int(row[7]) < current or tuple(row[2:6]) != (
                custody.control.cell_id,
                custody.control.logical_vault_id,
                custody.keyring.keyring_id,
                custody.control.activation_epoch,
            ):
                raise VocabularyAuthorityDenied("authority request is stale")
            try:
                operation = self._request_preview(json.loads(str(row[6]))).operation
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise VocabularyAuthorityUnavailable from None
            binding = self._require_binding(
                binding,
                _owner_binding(
                    "approve-request",
                    principal,
                    request_id=request_id,
                    operation=operation.as_dict(),
                ),
            )
            owner = self._require_owner(decision, binding=binding, now=current)
            if row[8] == "approved":
                connection.commit()
                return str(row[9])
            self._consume_owner_decision(connection, owner, binding=binding, action="approve-request")
            authority_id = self._insert_exact(
                connection,
                operation=operation,
                audience_id=str(row[0]),
                issuer_family=str(row[1]),
                custody=custody,
                expires_at=int(row[7]),
                owner=owner,
            )
            connection.execute(
                "UPDATE authority_requests SET state = 'approved', authority_id = ? WHERE request_id = ?",
                (authority_id, _text(request_id)),
            )
            connection.commit()
            return authority_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(self, operation: CanonicalOperation, *, principal: RequestPrincipal, now: int | None = None) -> Reservation:
        current = self._now(now)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, _ = self._revalidate_after_lock(
                principal, supplied_now=now
            )
            self._require_active(connection, custody)
            existing = connection.execute(
                "SELECT reservation_id, audience_id, command_digest, effect_digest, generation, state FROM reservations WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing[1:4] != (principal.audience_id, operation.command_digest, operation.effect_digest):
                    raise VocabularyAuthorityConflict("operation identity is already bound")
                ids = tuple(str(row[0]) for row in connection.execute("SELECT authority_id FROM reservation_authorities WHERE reservation_id = ? ORDER BY authority_id", (existing[0],)))
                connection.commit()
                return Reservation(str(existing[0]), operation.operation_id, ids, int(existing[4]), str(existing[5]))
            authorities = self._matching_authorities(connection, operation, principal, custody, now=current)
            if authorities is None:
                raise VocabularyAuthorityDenied("complete effect set lacks current authority")
            reservation_id = f"vocab-reservation-{uuid.uuid4()}"
            generation = custody.control.activation_epoch
            connection.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL)",
                (reservation_id, operation.operation_id, principal.audience_id, operation.command_digest, operation.effect_digest, json.dumps(operation.effect_manifest(), sort_keys=True), json.dumps([proof.as_dict() for proof in operation.scope_proofs], sort_keys=True), generation, json.dumps(operation.as_dict(), sort_keys=True)),
            )
            connection.executemany("INSERT INTO reservation_authorities VALUES (?, ?)", ((reservation_id, authority_id) for authority_id in authorities))
            authority_for_effect = self._authority_for_each_effect(
                connection, operation, principal, custody, now=current, candidates=authorities
            )
            connection.executemany(
                "INSERT INTO authority_uses VALUES (?, ?, ?, ?, ?)",
                (
                    (reservation_id, index, authority_id, effect.action, generation)
                    for index, (effect, authority_id) in enumerate(
                        zip(operation.effects, authority_for_effect, strict=True)
                    )
                ),
            )
            placeholders = ",".join("?" for _ in authorities)
            connection.execute(
                "UPDATE authorities SET status = 'reserved' "
                f"WHERE authority_id IN ({placeholders}) AND kind = 'exact'",
                authorities,
            )
            connection.commit()
            return Reservation(reservation_id, operation.operation_id, tuple(authorities), generation, "reserved")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _matching_authorities(self, connection: sqlite3.Connection, operation: CanonicalOperation, principal: RequestPrincipal, custody: object, *, now: int) -> list[str] | None:
        rows = connection.execute(
            "SELECT authority_id, kind, status, actions_json, scope_json, command_digest, effect_digest, operation_id, expires_at, generation "
            "FROM authorities WHERE audience_id = ? AND issuer_family = ? AND cell_id = ? AND logical_vault_id = ? AND keyring_id = ?",
            (principal.audience_id, principal.issuer_family, custody.control.cell_id, custody.control.logical_vault_id, custody.keyring.keyring_id),
        ).fetchall()
        matched: list[str] = []
        for effect in operation.effects:
            candidate = None
            for row in rows:
                if row[2] not in {"active", "reserved"} or int(row[8]) < now or int(row[9]) != custody.control.activation_epoch:
                    continue
                actions = tuple(json.loads(row[3]))
                if effect.action not in actions:
                    continue
                if row[1] == "exact":
                    if row[5:8] != (operation.command_digest, operation.effect_digest, operation.operation_id):
                        continue
                elif not self._scope_matches(json.loads(row[4]), effect, operation.scope_proofs):
                    continue
                candidate = str(row[0])
                break
            if candidate is None:
                return None
            matched.append(candidate)
        return sorted(set(matched))

    def _authority_for_each_effect(
        self,
        connection: sqlite3.Connection,
        operation: CanonicalOperation,
        principal: RequestPrincipal,
        custody: object,
        *,
        now: int,
        candidates: Iterable[str],
    ) -> tuple[str, ...]:
        allowed = set(candidates)
        rows = connection.execute(
            "SELECT authority_id, kind, status, actions_json, scope_json, command_digest, effect_digest, operation_id, expires_at, generation "
            "FROM authorities WHERE audience_id = ? AND issuer_family = ? AND cell_id = ? AND logical_vault_id = ? AND keyring_id = ?",
            (principal.audience_id, principal.issuer_family, custody.control.cell_id, custody.control.logical_vault_id, custody.keyring.keyring_id),
        ).fetchall()
        bound: list[str] = []
        for effect in operation.effects:
            for row in rows:
                if str(row[0]) not in allowed or row[2] not in {"active", "reserved"} or int(row[8]) < now or int(row[9]) != custody.control.activation_epoch:
                    continue
                if effect.action not in tuple(json.loads(row[3])):
                    continue
                if row[1] == "exact":
                    if row[5:8] != (operation.command_digest, operation.effect_digest, operation.operation_id):
                        continue
                elif not self._scope_matches(json.loads(row[4]), effect, operation.scope_proofs):
                    continue
                bound.append(str(row[0]))
                break
            else:
                raise VocabularyAuthorityDenied("complete effect set lacks current authority")
        return tuple(bound)

    @staticmethod
    def _scope_matches(scope: Mapping[str, Any], effect: Effect, proofs: tuple[AuthorityScope, ...]) -> bool:
        if scope.get("kind") == "vault":
            return effect.action in {"entity.create", "entity_type.add", "relation_type.add"}
        if scope.get("kind") != "project-edge" or effect.action != "edge.add":
            return False
        details = effect.details
        endpoints = tuple(sorted((str(details.get("source", "")), str(details.get("target", "")))))
        return any(
            proof.kind == "project-edge"
            and proof.project_ref == scope.get("project_ref")
            and proof.endpoints == endpoints == tuple(scope.get("endpoints") or ())
            # A standing grant identifies a stable project and canonical
            # endpoints.  Each classified operation must still carry a fresh
            # well-formed membership proof, but a changed project body must
            # not invalidate the standing grant merely because its old digest
            # differs from the current proof.
            and isinstance(proof.membership_digest, str)
            and len(proof.membership_digest) == 64
            for proof in proofs
        )

    def _operation_for_reservation(self, connection: sqlite3.Connection, reservation: Reservation) -> CanonicalOperation:
        row = connection.execute("SELECT operation_json FROM reservations WHERE reservation_id = ?", (reservation.reservation_id,)).fetchone()
        if row is None:
            raise VocabularyAuthorityConflict("reservation is missing")
        try:
            return CanonicalOperation._from_record(json.loads(row[0]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise VocabularyAuthorityUnavailable from None

    def _validate_active(self, connection: sqlite3.Connection, operation: CanonicalOperation, principal: RequestPrincipal, *, now: int, require_reservation: bool) -> None:
        custody = self._validate_custody(principal, now=now)
        self._require_active(connection, custody)
        reservation = connection.execute("SELECT state, generation FROM reservations WHERE operation_id = ?", (operation.operation_id,)).fetchone()
        if not reservation or reservation[0] != "reserved" or int(reservation[1]) != custody.control.activation_epoch:
            raise VocabularyAuthorityDenied("reservation is not commit eligible")
        matches = self._matching_authorities(connection, operation, principal, custody, now=now)
        if matches is None:
            raise VocabularyAuthorityDenied("authority changed before commit")

    def commit_guard(self, reservation: Reservation, *, principal: RequestPrincipal, now: int | None = None) -> _CommitGuard:
        if not isinstance(reservation, Reservation):
            raise VocabularyAuthorityDenied("canonical reservation required")
        return _CommitGuard(self, reservation, principal, now)

    def reconcile_committed(
        self, reservation: Reservation, evidence: CanonicalReceiptEvidence
    ) -> Reservation:
        """Recover a writer-sealed committed receipt after a process crash.

        Recovery never invokes a writer.  It merely records the exact receipt
        already sealed by the canonical mutation boundary and leaves unknown
        or differently bound reservations unavailable.
        """

        if (
            not isinstance(reservation, Reservation)
            or not isinstance(evidence, CanonicalReceiptEvidence)
            or evidence._seal is not _RECEIPT_SEAL
            or evidence.operation_id != reservation.operation_id
        ):
            raise VocabularyAuthorityDenied("canonical receipt evidence required")
        try:
            custody = self._custody_loader(self.vault_root, now=self._now(None))
        except Exception:  # noqa: BLE001 - writer evidence custody is fail-closed
            raise VocabularyAuthorityUnavailable from None
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityUnavailable
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT command_digest, effect_digest, generation, state, receipt_id FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None or row[0:2] != (evidence.command_digest, evidence.effect_digest):
                raise VocabularyAuthorityConflict("receipt does not bind this reservation")
            if row[3] == "committed":
                if row[4] != evidence.receipt_id:
                    raise VocabularyAuthorityConflict("reservation already has a different receipt")
                connection.commit()
                return Reservation(
                    reservation.reservation_id,
                    reservation.operation_id,
                    reservation.authority_ids,
                    int(row[2]),
                    "committed",
                )
            if row[3] != "reserved":
                raise VocabularyAuthorityConflict("reservation cannot be reconciled")
            connection.execute(
                "UPDATE reservations SET state = 'committed', receipt_id = ? WHERE reservation_id = ?",
                (evidence.receipt_id, reservation.reservation_id),
            )
            connection.execute(
                "UPDATE authorities SET status = 'spent' WHERE authority_id IN "
                "(SELECT authority_id FROM reservation_authorities WHERE reservation_id = ?) "
                "AND kind = 'exact'",
                (reservation.reservation_id,),
            )
            connection.commit()
            return Reservation(
                reservation.reservation_id,
                reservation.operation_id,
                reservation.authority_ids,
                int(row[2]),
                "committed",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def authority_use_manifest(self, reservation: Reservation) -> tuple[dict[str, object], ...]:
        """Return receipt-safe per-effect authority evidence without credentials."""

        if not isinstance(reservation, Reservation):
            raise VocabularyAuthorityDenied("canonical reservation required")
        try:
            custody = self._custody_loader(self.vault_root, now=self._now(None))
            connection = self._connect(custody, create=False)
        except Exception:  # noqa: BLE001 - receipt metadata custody is fail-closed
            raise VocabularyAuthorityUnavailable from None
        if connection is None:
            raise VocabularyAuthorityUnavailable
        try:
            rows = connection.execute(
                "SELECT effect_index, authority_id, action, generation FROM authority_uses "
                "WHERE reservation_id = ? ORDER BY effect_index",
                (reservation.reservation_id,),
            ).fetchall()
            if not rows:
                raise VocabularyAuthorityUnavailable
            return tuple(
                {
                    "effect_index": int(row[0]),
                    "authority_id": str(row[1]),
                    "action": str(row[2]),
                    "generation": int(row[3]),
                }
                for row in rows
            )
        finally:
            connection.close()

    def revoke(self, *, principal: RequestPrincipal, decision: TrustedOwnerDecision, authority_id: str, binding: str, now: int | None = None) -> None:
        current = self._now(now)
        binding = self._require_binding(
            binding, _owner_binding("revoke", principal, authority_id=authority_id)
        )
        owner = self._require_owner(decision, binding=binding, now=current)
        custody = self._validate_custody(principal, now=current)
        connection = self._connect(custody, create=False)
        if connection is None:
            raise VocabularyAuthorityDenied("v2 authority is not activated")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current, custody, owner = self._revalidate_after_lock(
                principal,
                supplied_now=now,
                decision=decision,
                binding=binding,
            )
            assert owner is not None
            self._require_active(connection, custody)
            self._consume_owner_decision(connection, owner, binding=binding, action="revoke")
            changed = connection.execute("UPDATE authorities SET status = 'revoked' WHERE authority_id = ? AND status IN ('active', 'reserved')", (_text(authority_id),))
            if changed.rowcount != 1:
                raise VocabularyAuthorityConflict("authority is not revocable")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
