"""Durable credential and replay authority for one hosted Exomem cell."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .hosted_runtime import HostedBindingV2

CREDENTIAL_BUNDLE_PATH = Path("/run/exomem/credentials/credentials.json")
SECURITY_DATABASE_FILENAME = "hosted-security.sqlite"
SECURITY_SCHEMA_VERSION = 1
TRANSFER_SCHEMA_VERSION = 2
JTI_RETENTION_SECONDS = 86_400
DEFAULT_JTI_CAPACITY = 10_000
DEFAULT_BUSY_TIMEOUT_MS = 250
PROOF_MAX_AGE_SECONDS = 300
_BUNDLE_MAX_BYTES = 8192
_CREDENTIAL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_GENERATION = re.compile(r"^\.\.[A-Za-z0-9_.-]+$")


class HostedSecurityError(RuntimeError):
    """Content-free security-authority failure with a stable integration code."""

    code = "HOSTED_SECURITY_UNAVAILABLE"
    message = "hosted security state is unavailable"

    def __init__(self, code: str | None = None, message: str | None = None) -> None:
        self.code = code or type(self).code
        self.message = message or type(self).message
        super().__init__(f"{self.code}: {self.message}")


class HostedSecurityUnavailable(HostedSecurityError):
    code = "HOSTED_SECURITY_UNAVAILABLE"
    message = "hosted security state is temporarily unavailable"


class HostedSecurityStateInvalid(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_STATE_INVALID"
    message = "hosted credential state is invalid"


class HostedCredentialBundleInvalid(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_BUNDLE_INVALID"
    message = "hosted credential bundle is invalid"


class HostedCredentialWeak(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_WEAK"
    message = "hosted credential does not meet the machine-secret contract"


class HostedCredentialTransitionInvalid(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_TRANSITION_INVALID"
    message = "hosted credential transition is invalid"


class HostedCredentialRevisionConflict(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_REVISION_CONFLICT"
    message = "hosted credential revision conflicts with the request"


class HostedCredentialProofRequired(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_PROOF_REQUIRED"
    message = "hosted credential proof is required"


class HostedCredentialProofStale(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_PROOF_STALE"
    message = "hosted credential proof is stale"


class HostedOperationConflict(HostedSecurityError):
    code = "HOSTED_OPERATION_CONFLICT"
    message = "hosted operation identity conflicts with an earlier request"


class HostedJTIReplay(HostedSecurityError):
    code = "HOSTED_JTI_REPLAY"
    message = "hosted transfer grant was already consumed"


class HostedJTIExpired(HostedSecurityError):
    code = "HOSTED_JTI_EXPIRED"
    message = "hosted transfer grant is expired"


class HostedJTICapacity(HostedSecurityError):
    code = "HOSTED_JTI_CAPACITY"
    message = "hosted transfer replay capacity is exhausted"


class HostedCredentialRejected(HostedSecurityError):
    code = "HOSTED_CREDENTIAL_STATE_INVALID"
    message = "hosted credential is not currently accepted"


@dataclass(frozen=True, slots=True)
class CredentialBundle:
    """One complete Secret generation; its representation never includes values."""

    credentials: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        values = dict(self.credentials)
        if not 1 <= len(values) <= 2:
            raise HostedCredentialBundleInvalid()
        for version, credential in values.items():
            _validate_credential_version(version)
            _validate_credential_value(credential)
        object.__setattr__(self, "credentials", MappingProxyType(values))

    def __repr__(self) -> str:
        return f"CredentialBundle(versions={tuple(sorted(self.credentials))!r})"


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    phase: str
    revision: int
    active_version: str
    pending_version: str | None
    preferred_version: str
    rotation_id: str | None
    proof_valid_until: int | None


@dataclass(frozen=True, slots=True)
class AuthenticatedCredential:
    credential_version: str
    security_revision: int
    preferred: bool


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    credential_version: str
    security_revision: int
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProofPersistence:
    recorded: bool
    valid_until: int | None
    snapshot: SecuritySnapshot


class HostedAuthenticator(Protocol):
    """Narrow common seam shared by FastMCP and private HTTP route wiring."""

    def authenticate(self, presented: str | None) -> AuthenticatedCredential | None: ...


class TransferSecurityAuthority(Protocol):
    """Narrow transfer-lane integration contract."""

    def verify_transfer_signature(
        self, kid: str, ascii_payload: str | bytes, signature: bytes
    ) -> bool: ...

    def consume_transfer_jti(
        self,
        *,
        cell_id: str,
        schema_version: int,
        kid: str,
        jti: str,
        expires_at: int,
        consumed_at: int,
    ) -> None: ...


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostedCredentialBundleInvalid()
        result[key] = value
    return result


def _validate_credential_version(version: object) -> str:
    if not isinstance(version, str) or not _CREDENTIAL_VERSION.fullmatch(version):
        raise HostedCredentialBundleInvalid()
    return version


def _validate_credential_value(value: object) -> str:
    if not isinstance(value, str) or len(value) != 43 or "=" in value:
        raise HostedCredentialWeak()
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HostedCredentialWeak() from exc
    if len(decoded) != 32:
        raise HostedCredentialWeak()
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(canonical, value):
        raise HostedCredentialWeak()
    # Exact entropy cannot be measured from one value. Reject the obvious degenerate
    # shapes while the provisioner remains responsible for a CSPRNG-generated value.
    if len(set(decoded)) < 8:
        raise HostedCredentialWeak()
    return value


def _parse_bundle(raw: bytes) -> CredentialBundle:
    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = json.loads(text, object_pairs_hook=_duplicate_free_object)
    except HostedSecurityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HostedCredentialBundleInvalid() from exc
    if not isinstance(parsed, dict) or set(parsed) != {"schema_version", "credentials"}:
        raise HostedCredentialBundleInvalid()
    if parsed["schema_version"] != 1 or isinstance(parsed["schema_version"], bool):
        raise HostedCredentialBundleInvalid()
    credentials = parsed["credentials"]
    if not isinstance(credentials, dict):
        raise HostedCredentialBundleInvalid()
    return CredentialBundle(credentials)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
        value.st_gid,
        value.st_size,
    )


def _load_projected_credential_bundle_at(
    path: Path,
    *,
    expected_uid: int = 0,
    expected_gid: int = 0,
    after_open: Callable[[], None] | None = None,
    require_read_only_mount: bool = False,
) -> CredentialBundle:
    """Read one native AtomicWriter generation from a validated descriptor.

    This path-accepting leaf is deliberately private for filesystem contract tests.
    Production callers use :func:`load_credential_bundle`, whose path is fixed.
    """

    leaf = Path(path)
    mount = leaf.parent
    try:
        mount_stat = os.lstat(mount)
        leaf_stat = os.lstat(leaf)
        leaf_target = os.readlink(leaf)
        data_link = mount / "..data"
        data_stat = os.lstat(data_link)
        generation_name = os.readlink(data_link)
    except (OSError, ValueError) as exc:
        raise HostedCredentialBundleInvalid() from exc
    if leaf.name != "credentials.json" or not stat.S_ISDIR(mount_stat.st_mode):
        raise HostedCredentialBundleInvalid()
    if stat.S_ISLNK(mount_stat.st_mode) or not stat.S_ISLNK(leaf_stat.st_mode):
        raise HostedCredentialBundleInvalid()
    if require_read_only_mount:
        try:
            mount_flags = os.statvfs(mount).f_flag
        except OSError as exc:
            raise HostedCredentialBundleInvalid() from exc
        if not mount_flags & getattr(os, "ST_RDONLY", 1):
            raise HostedCredentialBundleInvalid()
    if leaf_target != "..data/credentials.json" or not stat.S_ISLNK(data_stat.st_mode):
        raise HostedCredentialBundleInvalid()
    if (
        not _GENERATION.fullmatch(generation_name)
        or generation_name in {"..", "..data"}
        or "/" in generation_name
        or "\\" in generation_name
    ):
        raise HostedCredentialBundleInvalid()
    generation = mount / generation_name
    projected = generation / "credentials.json"
    try:
        generation_stat = os.lstat(generation)
        mount_real = mount.resolve(strict=True)
        generation_real = generation.resolve(strict=True)
    except OSError as exc:
        raise HostedCredentialBundleInvalid() from exc
    if not stat.S_ISDIR(generation_stat.st_mode) or stat.S_ISLNK(generation_stat.st_mode):
        raise HostedCredentialBundleInvalid()
    if generation_real.parent != mount_real:
        raise HostedCredentialBundleInvalid()

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(projected, flags)
    except OSError as exc:
        raise HostedCredentialBundleInvalid() from exc
    try:
        before = os.fstat(descriptor)
        if after_open is not None:
            after_open()
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or mode != 0o444
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > _BUNDLE_MAX_BYTES
        ):
            raise HostedCredentialBundleInvalid()
        chunks: list[bytes] = []
        remaining = _BUNDLE_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > _BUNDLE_MAX_BYTES
            or len(raw) != before.st_size
            or _stat_identity(before) != _stat_identity(after)
            or stat.S_IMODE(after.st_mode) != 0o444
        ):
            raise HostedCredentialBundleInvalid()
    except OSError as exc:
        raise HostedCredentialBundleInvalid() from exc
    finally:
        os.close(descriptor)
    return _parse_bundle(raw)


def load_credential_bundle() -> CredentialBundle:
    """Load the fixed Kubernetes Secret projection; callers cannot select a path."""

    return _load_projected_credential_bundle_at(
        CREDENTIAL_BUNDLE_PATH,
        require_read_only_mount=True,
    )


def _validate_identifier(value: str, *, operation: bool = False) -> str:
    pattern = _OPERATION_ID if operation else _OPAQUE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HostedSecurityStateInvalid()
    return value


def _validate_request_digest(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise HostedOperationConflict()
    return value


def _credential_digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def _canonical_result(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _snapshot_payload(snapshot: SecuritySnapshot) -> dict[str, Any]:
    return asdict(snapshot)


def _snapshot_from_payload(payload: Mapping[str, Any]) -> SecuritySnapshot:
    fields = {
        "phase",
        "revision",
        "active_version",
        "pending_version",
        "preferred_version",
        "rotation_id",
        "proof_valid_until",
    }
    try:
        if set(payload) != fields:
            raise HostedSecurityStateInvalid()
        phase = payload["phase"]
        revision = payload["revision"]
        active = payload["active_version"]
        pending = payload["pending_version"]
        preferred = payload["preferred_version"]
        rotation = payload["rotation_id"]
        valid_until = payload["proof_valid_until"]
        if (
            phase not in {"stable", "staged", "promoted"}
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or not isinstance(active, str)
            or not _CREDENTIAL_VERSION.fullmatch(active)
            or not isinstance(preferred, str)
            or not _CREDENTIAL_VERSION.fullmatch(preferred)
            or (
                pending is not None
                and (
                    not isinstance(pending, str)
                    or not _CREDENTIAL_VERSION.fullmatch(pending)
                )
            )
            or (
                valid_until is not None
                and (not isinstance(valid_until, int) or isinstance(valid_until, bool))
            )
        ):
            raise HostedSecurityStateInvalid()
        if rotation is not None:
            parsed_rotation = uuid.UUID(rotation)
            if parsed_rotation.version != 4 or str(parsed_rotation) != rotation:
                raise HostedSecurityStateInvalid()
        if phase == "stable" and (
            pending is not None
            or rotation is not None
            or preferred != active
            or valid_until is not None
        ):
            raise HostedSecurityStateInvalid()
        if phase in {"staged", "promoted"} and (
            pending is None or rotation is None or pending == active
        ):
            raise HostedSecurityStateInvalid()
        if phase == "staged" and preferred != active:
            raise HostedSecurityStateInvalid()
        if phase == "promoted" and preferred != pending:
            raise HostedSecurityStateInvalid()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HostedSecurityStateInvalid() from exc
    return SecuritySnapshot(
        phase=phase,
        revision=revision,
        active_version=active,
        pending_version=pending,
        preferred_version=preferred,
        rotation_id=rotation,
        proof_valid_until=valid_until,
    )


def _converge_descriptor_owner(
    descriptor: int, *, expected_uid: int, expected_gid: int
) -> None:
    """Set a newly-created hosted file to its bound runtime owner and verify it."""

    try:
        descriptor_stat = os.fstat(descriptor)
        if descriptor_stat.st_uid != expected_uid or descriptor_stat.st_gid != expected_gid:
            if os.geteuid() != 0:
                raise HostedSecurityStateInvalid()
            os.fchown(descriptor, expected_uid, expected_gid)
            descriptor_stat = os.fstat(descriptor)
        if descriptor_stat.st_uid != expected_uid or descriptor_stat.st_gid != expected_gid:
            raise HostedSecurityStateInvalid()
    except HostedSecurityError:
        raise
    except OSError as exc:
        raise HostedSecurityUnavailable() from exc


class HostedSecurityAuthority:
    """Cell-bound SQLite authority for credentials, proof metadata, and JTIs."""

    def __init__(
        self,
        state_root: Path,
        *,
        cell_id: str,
        vault_id: str,
        bundle_loader: Callable[[], CredentialBundle] = load_credential_bundle,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        jti_capacity: int = DEFAULT_JTI_CAPACITY,
        transaction_hook: Callable[[str], None] | None = None,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> None:
        self.state_root = Path(state_root)
        if not self.state_root.is_absolute():
            raise HostedSecurityStateInvalid()
        self.cell_id = _validate_identifier(cell_id)
        self.vault_id = _validate_identifier(vault_id)
        if isinstance(busy_timeout_ms, bool) or not 1 <= busy_timeout_ms <= 5_000:
            raise ValueError("busy_timeout_ms must be between 1 and 5000")
        if isinstance(jti_capacity, bool) or not 1 <= jti_capacity <= DEFAULT_JTI_CAPACITY:
            raise ValueError("jti_capacity must be between 1 and 10000")
        resolved_uid = os.geteuid() if expected_uid is None else expected_uid
        resolved_gid = os.getegid() if expected_gid is None else expected_gid
        if (
            isinstance(resolved_uid, bool)
            or isinstance(resolved_gid, bool)
            or not isinstance(resolved_uid, int)
            or not isinstance(resolved_gid, int)
            or not 0 <= resolved_uid <= 2_147_483_647
            or not 0 <= resolved_gid <= 2_147_483_647
        ):
            raise ValueError("expected runtime owner is invalid")
        self.busy_timeout_ms = busy_timeout_ms
        self.jti_capacity = jti_capacity
        self.expected_uid = resolved_uid
        self.expected_gid = resolved_gid
        self._bundle_loader = bundle_loader
        self._transaction_hook = transaction_hook or (lambda _label: None)
        self.database_path = self.state_root / SECURITY_DATABASE_FILENAME
        self._initialize()

    def _prepare_database_file(self) -> None:
        try:
            if self.state_root.exists():
                root_stat = os.lstat(self.state_root)
                if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                    raise HostedSecurityStateInvalid()
                if (
                    stat.S_IMODE(root_stat.st_mode) & 0o077
                    or root_stat.st_uid != self.expected_uid
                    or root_stat.st_gid != self.expected_gid
                ):
                    raise HostedSecurityStateInvalid()
            else:
                self.state_root.mkdir(parents=True, mode=0o700)
            try:
                descriptor = os.open(
                    self.database_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
            except FileExistsError:
                descriptor = -1
            if descriptor >= 0:
                try:
                    _converge_descriptor_owner(
                        descriptor,
                        expected_uid=self.expected_uid,
                        expected_gid=self.expected_gid,
                    )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            database_stat = os.lstat(self.database_path)
        except HostedSecurityError:
            raise
        except OSError as exc:
            raise HostedSecurityUnavailable() from exc
        if (
            not stat.S_ISREG(database_stat.st_mode)
            or stat.S_ISLNK(database_stat.st_mode)
            or database_stat.st_nlink != 1
            or stat.S_IMODE(database_stat.st_mode) != 0o600
            or database_stat.st_uid != self.expected_uid
            or database_stat.st_gid != self.expected_gid
        ):
            raise HostedSecurityStateInvalid()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise HostedSecurityUnavailable() from exc

    def _initialize(self) -> None:
        self._prepare_database_file()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SECURITY_SCHEMA_VERSION}:
                raise HostedSecurityStateInvalid()
            schema_sql = """
                CREATE TABLE IF NOT EXISTS authority_binding (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    cell_id TEXT NOT NULL,
                    vault_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credential_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    row_revision INTEGER NOT NULL CHECK (row_revision >= 1),
                    phase TEXT NOT NULL CHECK (phase IN ('stable', 'staged', 'promoted')),
                    active_version TEXT NOT NULL,
                    active_digest BLOB NOT NULL,
                    pending_version TEXT,
                    pending_digest BLOB,
                    preferred_version TEXT NOT NULL,
                    rotation_id TEXT,
                    proof_operation_id TEXT,
                    proof_request_digest TEXT,
                    proof_request_id TEXT,
                    proof_revision INTEGER,
                    proof_rotation_id TEXT,
                    proof_credential_version TEXT,
                    proof_credential_digest BLOB,
                    proof_release TEXT,
                    proof_protocol TEXT,
                    proof_worker_policy_digest TEXT,
                    proof_readiness_digest TEXT,
                    proof_recorded_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consumed_jtis (
                    jti_digest BLOB PRIMARY KEY,
                    credential_version TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER NOT NULL,
                    retention_until INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS consumed_jtis_retention
                ON consumed_jtis(retention_until);
                """
            # sqlite3.executescript() commits an existing transaction. Execute each
            # fixed DDL statement ourselves so first-open schema + cell binding stay
            # inside this BEGIN IMMEDIATE transaction.
            for statement in schema_sql.split(";"):
                if statement.strip():
                    connection.execute(statement)
            if version == 0:
                connection.execute(f"PRAGMA user_version = {SECURITY_SCHEMA_VERSION}")
            binding = connection.execute(
                "SELECT schema_version, cell_id, vault_id FROM authority_binding WHERE singleton=1"
            ).fetchone()
            if binding is None:
                connection.execute(
                    "INSERT INTO authority_binding(singleton, schema_version, cell_id, vault_id) "
                    "VALUES(1, ?, ?, ?)",
                    (SECURITY_SCHEMA_VERSION, self.cell_id, self.vault_id),
                )
            elif (
                binding["schema_version"] != SECURITY_SCHEMA_VERSION
                or binding["cell_id"] != self.cell_id
                or binding["vault_id"] != self.vault_id
            ):
                raise HostedSecurityStateInvalid()
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise HostedSecurityStateInvalid()
            connection.commit()
        except HostedSecurityError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        finally:
            connection.close()

    @contextmanager
    def _write(self, action: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            self._transaction_hook(f"{action}:before_commit")
            connection.commit()
        except HostedSecurityError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM credential_state WHERE singleton=1").fetchone()
        if row is None:
            raise HostedSecurityStateInvalid()
        self._validate_state_row(row)
        return row

    @staticmethod
    def _validate_state_row(row: sqlite3.Row) -> None:
        try:
            active_digest = bytes(row["active_digest"])
            pending_digest = (
                None if row["pending_digest"] is None else bytes(row["pending_digest"])
            )
            snapshot = SecuritySnapshot(
                phase=row["phase"],
                revision=row["row_revision"],
                active_version=row["active_version"],
                pending_version=row["pending_version"],
                preferred_version=row["preferred_version"],
                rotation_id=row["rotation_id"],
                proof_valid_until=(
                    None
                    if row["proof_recorded_at"] is None
                    else int(row["proof_recorded_at"]) + PROOF_MAX_AGE_SECONDS
                ),
            )
            _snapshot_from_payload(_snapshot_payload(snapshot))
            if len(active_digest) != hashlib.sha256().digest_size:
                raise HostedSecurityStateInvalid()
            if snapshot.pending_version is not None:
                if (
                    pending_digest is None
                    or len(pending_digest) != hashlib.sha256().digest_size
                    or hmac.compare_digest(active_digest, pending_digest)
                ):
                    raise HostedSecurityStateInvalid()
            elif pending_digest is not None:
                raise HostedSecurityStateInvalid()
            proof_fields = (
                "proof_operation_id",
                "proof_request_digest",
                "proof_request_id",
                "proof_revision",
                "proof_rotation_id",
                "proof_credential_version",
                "proof_credential_digest",
                "proof_release",
                "proof_protocol",
                "proof_worker_policy_digest",
                "proof_readiness_digest",
                "proof_recorded_at",
            )
            proof_values = [row[field] for field in proof_fields]
            if all(value is None for value in proof_values):
                return
            if any(value is None for value in proof_values):
                raise HostedSecurityStateInvalid()
            request_uuid = uuid.UUID(row["proof_request_id"])
            if (
                snapshot.phase not in {"staged", "promoted"}
                or not _OPERATION_ID.fullmatch(row["proof_operation_id"])
                or not _SHA256.fullmatch(row["proof_request_digest"])
                or request_uuid.version != 4
                or str(request_uuid) != row["proof_request_id"]
                or row["proof_revision"] != snapshot.revision
                or row["proof_rotation_id"] != snapshot.rotation_id
                or row["proof_credential_version"] != snapshot.pending_version
                or not hmac.compare_digest(
                    bytes(row["proof_credential_digest"]), pending_digest or b""
                )
                or not isinstance(row["proof_release"], str)
                or not 1 <= len(row["proof_release"].encode("utf-8")) <= 64
                or not _PROTOCOL.fullmatch(row["proof_protocol"])
                or not _SHA256.fullmatch(row["proof_worker_policy_digest"])
                or not _SHA256.fullmatch(row["proof_readiness_digest"])
                or not isinstance(row["proof_recorded_at"], int)
                or row["proof_recorded_at"] < 0
            ):
                raise HostedSecurityStateInvalid()
        except HostedSecurityError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HostedSecurityStateInvalid() from exc

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SecuritySnapshot:
        proof_at = row["proof_recorded_at"]
        return SecuritySnapshot(
            phase=row["phase"],
            revision=row["row_revision"],
            active_version=row["active_version"],
            pending_version=row["pending_version"],
            preferred_version=row["preferred_version"],
            rotation_id=row["rotation_id"],
            proof_valid_until=(
                None if proof_at is None else int(proof_at) + PROOF_MAX_AGE_SECONDS
            ),
        )

    def snapshot(self) -> SecuritySnapshot:
        connection = self._connect()
        try:
            return self._snapshot(self._read_state(connection))
        except HostedSecurityError:
            raise
        except sqlite3.Error as exc:
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        finally:
            connection.close()

    def validate_ready(self) -> SecuritySnapshot:
        """Prove durable credential state matches the current Secret generation."""

        connection = self._connect()
        try:
            row = self._read_state(connection)
            self._validate_recorded_bundle(row, self._bundle())
            return self._snapshot(row)
        except HostedSecurityError:
            raise
        except sqlite3.Error as exc:
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        finally:
            connection.close()

    def _operation_replay(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        action: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        row = connection.execute(
            "SELECT action, request_digest, result_json FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or not hmac.compare_digest(
            row["request_digest"], request_digest
        ):
            raise HostedOperationConflict()
        try:
            result = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise HostedSecurityStateInvalid() from exc
        if not isinstance(result, dict):
            raise HostedSecurityStateInvalid()
        return result

    @staticmethod
    def _record_operation(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        action: str,
        request_digest: str,
        result: Mapping[str, Any],
        now: int | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO operations(operation_id, action, request_digest, result_json, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                operation_id,
                action,
                request_digest,
                _canonical_result(result),
                int(time.time()) if now is None else int(now),
            ),
        )

    def _bundle(self) -> CredentialBundle:
        try:
            bundle = self._bundle_loader()
        except HostedSecurityError:
            raise
        except Exception as exc:  # noqa: BLE001 - injected authority must fail closed
            raise HostedCredentialBundleInvalid() from exc
        if not isinstance(bundle, CredentialBundle):
            raise HostedCredentialBundleInvalid()
        return bundle

    def _validate_recorded_bundle(
        self, row: sqlite3.Row, bundle: CredentialBundle
    ) -> dict[str, str]:
        accepted: dict[str, str] = {}
        active = bundle.credentials.get(row["active_version"])
        if active is None or not hmac.compare_digest(
            _credential_digest(active), bytes(row["active_digest"])
        ):
            raise HostedSecurityStateInvalid()
        accepted[row["active_version"]] = active
        if row["pending_version"] is not None:
            pending = bundle.credentials.get(row["pending_version"])
            if pending is None or row["pending_digest"] is None or not hmac.compare_digest(
                _credential_digest(pending), bytes(row["pending_digest"])
            ):
                raise HostedSecurityStateInvalid()
            accepted[row["pending_version"]] = pending
        return accepted

    def bootstrap(
        self,
        *,
        active_version: str,
        operation_id: str,
        request_digest: str,
    ) -> SecuritySnapshot:
        _validate_credential_version(active_version)
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        bundle = self._bundle()
        if set(bundle.credentials) != {active_version}:
            raise HostedCredentialBundleInvalid()
        active_digest = _credential_digest(bundle.credentials[active_version])
        with self._write("bootstrap") as connection:
            replay = self._operation_replay(
                connection,
                operation_id=operation_id,
                action="bootstrap",
                request_digest=request_digest,
            )
            if replay is not None:
                return _snapshot_from_payload(replay["snapshot"])
            existing = connection.execute(
                "SELECT 1 FROM credential_state WHERE singleton=1"
            ).fetchone()
            if existing is not None:
                raise HostedCredentialTransitionInvalid()
            connection.execute(
                """
                INSERT INTO credential_state(
                    singleton, row_revision, phase, active_version, active_digest,
                    pending_version, pending_digest, preferred_version, rotation_id
                ) VALUES(1, 1, 'stable', ?, ?, NULL, NULL, ?, NULL)
                """,
                (active_version, active_digest, active_version),
            )
            snapshot = self._snapshot(self._read_state(connection))
            self._record_operation(
                connection,
                operation_id=operation_id,
                action="bootstrap",
                request_digest=request_digest,
                result={"snapshot": _snapshot_payload(snapshot)},
            )
            return snapshot

    def _transition_start(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
    ) -> tuple[sqlite3.Row | None, SecuritySnapshot | None]:
        replay = self._operation_replay(
            connection,
            operation_id=operation_id,
            action=action,
            request_digest=request_digest,
        )
        if replay is not None:
            return None, _snapshot_from_payload(replay["snapshot"])
        row = self._read_state(connection)
        if isinstance(expected_revision, bool) or row["row_revision"] != expected_revision:
            raise HostedCredentialRevisionConflict()
        return row, None

    def _transition_finish(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        operation_id: str,
        request_digest: str,
    ) -> SecuritySnapshot:
        snapshot = self._snapshot(self._read_state(connection))
        self._record_operation(
            connection,
            operation_id=operation_id,
            action=action,
            request_digest=request_digest,
            result={"snapshot": _snapshot_payload(snapshot)},
        )
        return snapshot

    def stage(
        self,
        *,
        pending_version: str,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
    ) -> SecuritySnapshot:
        _validate_credential_version(pending_version)
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        with self._write("stage") as connection:
            row, replay = self._transition_start(
                connection,
                action="stage",
                expected_revision=expected_revision,
                operation_id=operation_id,
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            assert row is not None
            if row["phase"] != "stable" or row["pending_version"] is not None:
                raise HostedCredentialTransitionInvalid()
            bundle = self._bundle()
            if set(bundle.credentials) != {row["active_version"], pending_version}:
                raise HostedCredentialBundleInvalid()
            active = bundle.credentials[row["active_version"]]
            pending = bundle.credentials[pending_version]
            active_digest = _credential_digest(active)
            pending_digest = _credential_digest(pending)
            if not hmac.compare_digest(active_digest, bytes(row["active_digest"])):
                raise HostedSecurityStateInvalid()
            if pending_version == row["active_version"] or hmac.compare_digest(
                pending_digest, active_digest
            ):
                raise HostedCredentialTransitionInvalid()
            rotation_id = str(uuid.uuid4())
            connection.execute(
                """
                UPDATE credential_state SET
                    row_revision=row_revision+1, phase='staged',
                    pending_version=?, pending_digest=?, preferred_version=active_version,
                    rotation_id=?, proof_operation_id=NULL, proof_request_digest=NULL,
                    proof_request_id=NULL, proof_revision=NULL, proof_rotation_id=NULL,
                    proof_credential_version=NULL, proof_credential_digest=NULL,
                    proof_release=NULL, proof_protocol=NULL,
                    proof_worker_policy_digest=NULL, proof_readiness_digest=NULL,
                    proof_recorded_at=NULL
                WHERE singleton=1 AND row_revision=?
                """,
                (pending_version, pending_digest, rotation_id, expected_revision),
            )
            return self._transition_finish(
                connection,
                action="stage",
                operation_id=operation_id,
                request_digest=request_digest,
            )

    def _require_fresh_proof(self, row: sqlite3.Row, *, now: int) -> None:
        if row["proof_recorded_at"] is None:
            raise HostedCredentialProofRequired()
        proof_age = int(now) - int(row["proof_recorded_at"])
        if proof_age < 0 or proof_age > PROOF_MAX_AGE_SECONDS:
            raise HostedCredentialProofStale()
        required = (
            row["proof_rotation_id"] == row["rotation_id"],
            row["proof_credential_version"] == row["pending_version"],
            row["proof_credential_digest"] is not None,
            row["pending_digest"] is not None,
            row["proof_credential_digest"] is not None
            and row["pending_digest"] is not None
            and hmac.compare_digest(
                bytes(row["proof_credential_digest"]), bytes(row["pending_digest"])
            ),
            row["proof_revision"] == row["row_revision"],
        )
        if not all(required):
            raise HostedCredentialProofRequired()

    def promote(
        self,
        *,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
        now: int | None = None,
    ) -> SecuritySnapshot:
        now_value = int(time.time()) if now is None else int(now)
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        with self._write("promote") as connection:
            row, replay = self._transition_start(
                connection,
                action="promote",
                expected_revision=expected_revision,
                operation_id=operation_id,
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            assert row is not None
            if row["phase"] != "staged" or row["pending_version"] is None:
                raise HostedCredentialTransitionInvalid()
            self._validate_recorded_bundle(row, self._bundle())
            self._require_fresh_proof(row, now=now_value)
            connection.execute(
                """
                UPDATE credential_state SET
                    row_revision=row_revision+1, phase='promoted',
                    preferred_version=pending_version, proof_revision=row_revision+1
                WHERE singleton=1 AND row_revision=?
                """,
                (expected_revision,),
            )
            return self._transition_finish(
                connection,
                action="promote",
                operation_id=operation_id,
                request_digest=request_digest,
            )

    def abort(
        self,
        *,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
    ) -> SecuritySnapshot:
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        with self._write("abort") as connection:
            row, replay = self._transition_start(
                connection,
                action="abort",
                expected_revision=expected_revision,
                operation_id=operation_id,
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            assert row is not None
            if row["phase"] not in {"staged", "promoted"}:
                raise HostedCredentialTransitionInvalid()
            bundle = self._bundle()
            active = bundle.credentials.get(row["active_version"])
            if active is None or not hmac.compare_digest(
                _credential_digest(active), bytes(row["active_digest"])
            ):
                raise HostedSecurityStateInvalid()
            connection.execute(
                """
                UPDATE credential_state SET
                    row_revision=row_revision+1, phase='stable',
                    pending_version=NULL, pending_digest=NULL,
                    preferred_version=active_version, rotation_id=NULL,
                    proof_operation_id=NULL, proof_request_digest=NULL,
                    proof_request_id=NULL, proof_revision=NULL, proof_rotation_id=NULL,
                    proof_credential_version=NULL, proof_credential_digest=NULL,
                    proof_release=NULL, proof_protocol=NULL,
                    proof_worker_policy_digest=NULL, proof_readiness_digest=NULL,
                    proof_recorded_at=NULL
                WHERE singleton=1 AND row_revision=?
                """,
                (expected_revision,),
            )
            return self._transition_finish(
                connection,
                action="abort",
                operation_id=operation_id,
                request_digest=request_digest,
            )

    def finalize(
        self,
        *,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
        now: int | None = None,
    ) -> SecuritySnapshot:
        now_value = int(time.time()) if now is None else int(now)
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        with self._write("finalize") as connection:
            row, replay = self._transition_start(
                connection,
                action="finalize",
                expected_revision=expected_revision,
                operation_id=operation_id,
                request_digest=request_digest,
            )
            if replay is not None:
                return replay
            assert row is not None
            if row["phase"] != "promoted" or row["pending_version"] is None:
                raise HostedCredentialTransitionInvalid()
            self._validate_recorded_bundle(row, self._bundle())
            self._require_fresh_proof(row, now=now_value)
            connection.execute(
                """
                UPDATE credential_state SET
                    row_revision=row_revision+1, phase='stable',
                    active_version=pending_version, active_digest=pending_digest,
                    pending_version=NULL, pending_digest=NULL,
                    preferred_version=pending_version, rotation_id=NULL,
                    proof_operation_id=NULL, proof_request_digest=NULL,
                    proof_request_id=NULL, proof_revision=NULL, proof_rotation_id=NULL,
                    proof_credential_version=NULL, proof_credential_digest=NULL,
                    proof_release=NULL, proof_protocol=NULL,
                    proof_worker_policy_digest=NULL, proof_readiness_digest=NULL,
                    proof_recorded_at=NULL
                WHERE singleton=1 AND row_revision=?
                """,
                (expected_revision,),
            )
            return self._transition_finish(
                connection,
                action="finalize",
                operation_id=operation_id,
                request_digest=request_digest,
            )

    def record_probe_proof(
        self,
        *,
        selected_version: str,
        expected_revision: int,
        operation_id: str,
        request_digest: str,
        request_id: str,
        release: str,
        protocol: str,
        worker_policy_digest: str,
        readiness_digest: str,
        now: int | None = None,
    ) -> ProofPersistence:
        _validate_credential_version(selected_version)
        operation_id = _validate_identifier(operation_id, operation=True)
        request_digest = _validate_request_digest(request_digest)
        if not _SHA256.fullmatch(worker_policy_digest) or not _SHA256.fullmatch(
            readiness_digest
        ):
            raise HostedCredentialTransitionInvalid()
        try:
            canonical_request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise HostedCredentialTransitionInvalid() from exc
        if canonical_request_id != request_id or uuid.UUID(request_id).version != 4:
            raise HostedCredentialTransitionInvalid()
        now_value = int(time.time()) if now is None else int(now)
        with self._write("probe") as connection:
            replay = self._operation_replay(
                connection,
                operation_id=operation_id,
                action="probe",
                request_digest=request_digest,
            )
            row = self._read_state(connection)
            if row["row_revision"] != expected_revision:
                raise HostedCredentialRevisionConflict()
            accepted = self._validate_recorded_bundle(row, self._bundle())
            if selected_version not in accepted:
                raise HostedCredentialTransitionInvalid()
            if replay is not None:
                if selected_version == row["pending_version"]:
                    if (
                        row["proof_revision"] != expected_revision
                        or row["proof_rotation_id"] != row["rotation_id"]
                        or row["proof_credential_version"] != selected_version
                        or row["proof_credential_digest"] is None
                        or row["pending_digest"] is None
                        or not hmac.compare_digest(
                            bytes(row["proof_credential_digest"]),
                            bytes(row["pending_digest"]),
                        )
                        or row["proof_release"] != release
                        or row["proof_protocol"] != protocol
                        or row["proof_worker_policy_digest"] != worker_policy_digest
                        or row["proof_readiness_digest"] != readiness_digest
                    ):
                        raise HostedCredentialTransitionInvalid()
                    snapshot = self._snapshot(row)
                    return ProofPersistence(True, snapshot.proof_valid_until, snapshot)
                snapshot = self._snapshot(row)
                return ProofPersistence(False, None, snapshot)
            if selected_version == row["active_version"]:
                snapshot = self._snapshot(row)
                result = ProofPersistence(False, None, snapshot)
                self._record_operation(
                    connection,
                    operation_id=operation_id,
                    action="probe",
                    request_digest=request_digest,
                    result={
                        "recorded": False,
                        "valid_until": None,
                        "snapshot": _snapshot_payload(snapshot),
                    },
                    now=now_value,
                )
                return result
            if (
                selected_version != row["pending_version"]
                or row["phase"] not in {"staged", "promoted"}
                or row["rotation_id"] is None
                or row["pending_digest"] is None
            ):
                raise HostedCredentialTransitionInvalid()
            connection.execute(
                """
                UPDATE credential_state SET
                    proof_operation_id=?, proof_request_digest=?, proof_request_id=?,
                    proof_revision=row_revision, proof_rotation_id=rotation_id,
                    proof_credential_version=pending_version,
                    proof_credential_digest=pending_digest,
                    proof_release=?, proof_protocol=?, proof_worker_policy_digest=?,
                    proof_readiness_digest=?, proof_recorded_at=?
                WHERE singleton=1 AND row_revision=?
                """,
                (
                    operation_id,
                    request_digest,
                    request_id,
                    release,
                    protocol,
                    worker_policy_digest,
                    readiness_digest,
                    now_value,
                    expected_revision,
                ),
            )
            snapshot = self._snapshot(self._read_state(connection))
            result = ProofPersistence(True, snapshot.proof_valid_until, snapshot)
            self._record_operation(
                connection,
                operation_id=operation_id,
                action="probe",
                request_digest=request_digest,
                result={
                    "recorded": True,
                    "valid_until": snapshot.proof_valid_until,
                    "snapshot": _snapshot_payload(snapshot),
                },
                now=now_value,
            )
            return result

    def authenticate(self, presented: str | None) -> AuthenticatedCredential | None:
        if not presented or not isinstance(presented, str):
            return None
        connection = self._connect()
        try:
            row = self._read_state(connection)
            accepted = self._validate_recorded_bundle(row, self._bundle())
            candidate = hashlib.sha256(presented.encode("utf-8")).digest()
            selected: str | None = None
            for version in sorted(accepted):
                stored_column = (
                    "active_digest" if version == row["active_version"] else "pending_digest"
                )
                stored = bytes(row[stored_column])
                if hmac.compare_digest(candidate, stored):
                    selected = version
            if selected is None:
                return None
            return AuthenticatedCredential(
                credential_version=selected,
                security_revision=row["row_revision"],
                preferred=selected == row["preferred_version"],
            )
        except HostedSecurityError:
            raise
        except sqlite3.Error as exc:
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        finally:
            connection.close()

    def credential_material(self, version: str) -> CredentialMaterial:
        _validate_credential_version(version)
        connection = self._connect()
        try:
            row = self._read_state(connection)
            accepted = self._validate_recorded_bundle(row, self._bundle())
            secret = accepted.get(version)
            if secret is None:
                raise HostedCredentialRejected()
            return CredentialMaterial(version, row["row_revision"], secret)
        except HostedSecurityError:
            raise
        except sqlite3.Error as exc:
            if _is_busy(exc):
                raise HostedSecurityUnavailable() from exc
            raise HostedSecurityStateInvalid() from exc
        finally:
            connection.close()

    def verify_transfer_signature(
        self, kid: str, ascii_payload: str | bytes, signature: bytes
    ) -> bool:
        try:
            material = self.credential_material(kid)
        except HostedCredentialRejected:
            return False
        if not isinstance(signature, bytes) or len(signature) != hashlib.sha256().digest_size:
            return False
        if isinstance(ascii_payload, str):
            try:
                payload = ascii_payload.encode("ascii")
            except UnicodeEncodeError:
                return False
        elif isinstance(ascii_payload, bytes):
            payload = ascii_payload
            try:
                payload.decode("ascii")
            except UnicodeDecodeError:
                return False
        else:
            return False
        expected = hmac.digest(material.secret.encode("ascii"), payload, "sha256")
        return hmac.compare_digest(expected, signature)

    def consume_transfer_jti(
        self,
        *,
        cell_id: str,
        schema_version: int,
        kid: str,
        jti: str,
        expires_at: int,
        consumed_at: int,
    ) -> None:
        if cell_id != self.cell_id or schema_version != TRANSFER_SCHEMA_VERSION:
            raise HostedSecurityStateInvalid()
        _validate_credential_version(kid)
        if (
            isinstance(expires_at, bool)
            or isinstance(consumed_at, bool)
            or not isinstance(expires_at, int)
            or not isinstance(consumed_at, int)
            or expires_at <= consumed_at
            or expires_at > 9_223_372_036_854_689_407
        ):
            raise HostedJTIExpired()
        try:
            parsed_jti = uuid.UUID(jti)
        except (ValueError, AttributeError) as exc:
            raise HostedSecurityStateInvalid() from exc
        if parsed_jti.version != 4 or str(parsed_jti) != jti:
            raise HostedSecurityStateInvalid()
        jti_digest = hashlib.sha256(
            f"{self.cell_id}\0{schema_version}\0{jti}".encode("ascii")
        ).digest()
        with self._write("jti-consume") as connection:
            row = self._read_state(connection)
            accepted = self._validate_recorded_bundle(row, self._bundle())
            if kid not in accepted:
                raise HostedCredentialRejected()
            self._cleanup_jtis(connection, now=consumed_at, limit=256)
            if connection.execute(
                "SELECT 1 FROM consumed_jtis WHERE jti_digest=?", (jti_digest,)
            ).fetchone() is not None:
                raise HostedJTIReplay()
            count = int(connection.execute("SELECT COUNT(*) FROM consumed_jtis").fetchone()[0])
            if count >= self.jti_capacity:
                raise HostedJTICapacity()
            try:
                connection.execute(
                    """
                    INSERT INTO consumed_jtis(
                        jti_digest, credential_version, expires_at,
                        consumed_at, retention_until
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        jti_digest,
                        kid,
                        expires_at,
                        consumed_at,
                        expires_at + JTI_RETENTION_SECONDS,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise HostedJTIReplay() from exc

    @staticmethod
    def _cleanup_jtis(
        connection: sqlite3.Connection, *, now: int, limit: int
    ) -> int:
        cursor = connection.execute(
            """
            DELETE FROM consumed_jtis
            WHERE jti_digest IN (
                SELECT jti_digest FROM consumed_jtis
                WHERE retention_until <= ?
                ORDER BY retention_until
                LIMIT ?
            )
            """,
            (now, limit),
        )
        return max(cursor.rowcount, 0)

    def cleanup_jtis(self, *, now: int, limit: int = 1000) -> int:
        if isinstance(now, bool) or not isinstance(now, int):
            raise HostedSecurityStateInvalid()
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("cleanup limit must be between 1 and 1000")
        with self._write("jti-cleanup") as connection:
            return self._cleanup_jtis(connection, now=now, limit=limit)


def _is_busy(error: sqlite3.Error) -> bool:
    message = str(error).casefold()
    return "locked" in message or "busy" in message


def bootstrap_hosted_security(
    *,
    binding: HostedBindingV2,
    active_credential_version: str,
    operation_id: str | None,
    request_digest: str | None,
) -> SecuritySnapshot:
    """Compatibility adapter used by hosted init and restore wiring."""

    if operation_id is None or request_digest is None:
        raise HostedSecurityStateInvalid()
    authority = HostedSecurityAuthority(
        binding.state_root,
        cell_id=binding.cell_id,
        vault_id=binding.vault_id,
        bundle_loader=load_credential_bundle,
        expected_uid=binding.runtime_uid,
        expected_gid=binding.runtime_gid,
    )
    return authority.bootstrap(
        active_version=active_credential_version,
        operation_id=operation_id,
        request_digest=request_digest,
    )


def _operator_authority(request: Mapping[str, Any]) -> HostedSecurityAuthority:
    return HostedSecurityAuthority(
        Path(request["state_root"]),
        cell_id=request["cell_id"],
        vault_id=request["vault_id"],
        bundle_loader=load_credential_bundle,
    )


def _operator_failure(code: str) -> Exception:
    from .hosted_operator import OperatorFailure

    return OperatorFailure(code)


def _proof_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _credential_operator_data(snapshot: SecuritySnapshot) -> dict[str, Any]:
    return {
        "phase": snapshot.phase,
        "revision": snapshot.revision,
        "active_version": snapshot.active_version,
        "pending_version": snapshot.pending_version,
        "preferred_version": snapshot.preferred_version,
        "rotation_id": snapshot.rotation_id,
        "proof_valid_until": _proof_timestamp(snapshot.proof_valid_until),
    }


def execute_credential_operator(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Adapt one validated operator request to the credential authority."""

    from .hosted_operator import canonical_request_digest

    try:
        authority = _operator_authority(request)
        common = {
            "expected_revision": request["expected_revision"],
            "operation_id": request["operation_id"],
            "request_digest": canonical_request_digest(request),
        }
        action = request["action"]
        if action == "stage":
            snapshot = authority.stage(
                pending_version=request["pending_version"],
                **common,
            )
        elif action == "promote":
            snapshot = authority.promote(**common)
        elif action == "abort":
            snapshot = authority.abort(**common)
        elif action == "finalize":
            snapshot = authority.finalize(**common)
        else:
            raise HostedCredentialTransitionInvalid()
    except HostedSecurityError as exc:
        raise _operator_failure(exc.code) from exc
    code = {
        "stage": "HOSTED_CREDENTIAL_STAGED",
        "promote": "HOSTED_CREDENTIAL_PROMOTED",
        "abort": "HOSTED_CREDENTIAL_ABORTED",
        "finalize": "HOSTED_CREDENTIAL_FINALIZED",
    }[action]
    return code, _credential_operator_data(snapshot)


def execute_probe_operator(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Synchronously adapt one validated operator request to the async probe."""

    from .hosted_operator import canonical_request_digest
    from .hosted_probe import HostedProbeError, HostedProbeRequest, run_hosted_probe

    try:
        authority = _operator_authority(request)
        probe_request = HostedProbeRequest(
            request_id=request["request_id"],
            operation_id=request["operation_id"],
            request_digest=canonical_request_digest(request),
            cell_id=request["cell_id"],
            vault_id=request["vault_id"],
            selected_credential_version=request["selected_credential_version"],
            expected_release=request["expected_release"],
            expected_protocol=request["expected_protocol"],
            expected_worker_policy_digest=request["expected_worker_policy_digest"],
            expected_revision=request["expected_revision"],
            port=request["port"],
        )
        result = asyncio.run(run_hosted_probe(probe_request, authority=authority))
    except HostedProbeError as exc:
        raise _operator_failure(exc.code) from exc
    except HostedSecurityError as exc:
        raise _operator_failure(exc.code) from exc
    return "HOSTED_PROBE_READY", result.as_data()


__all__ = [
    "AuthenticatedCredential",
    "CredentialBundle",
    "CredentialMaterial",
    "HostedAuthenticator",
    "HostedCredentialBundleInvalid",
    "HostedCredentialProofRequired",
    "HostedCredentialProofStale",
    "HostedCredentialRejected",
    "HostedCredentialRevisionConflict",
    "HostedCredentialTransitionInvalid",
    "HostedCredentialWeak",
    "HostedJTIExpired",
    "HostedJTICapacity",
    "HostedJTIReplay",
    "HostedOperationConflict",
    "HostedSecurityAuthority",
    "HostedSecurityError",
    "HostedSecurityStateInvalid",
    "HostedSecurityUnavailable",
    "ProofPersistence",
    "SecuritySnapshot",
    "TransferSecurityAuthority",
    "bootstrap_hosted_security",
    "execute_credential_operator",
    "execute_probe_operator",
    "load_credential_bundle",
]
