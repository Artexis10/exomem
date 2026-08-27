"""Per-machine governance sidecar and its monotonic schema owner.

`.governance.sqlite` is a derived convenience, never the enforcement
authority (design decision D6): rebuildable at any time from
`_Governance/**.yaml`, never synced, and never consulted by
`policy.load`/`membership.evaluate`/`decisions.decide` — those run entirely
in-process off the parsed YAML. Only an explicit `compile.write_snapshot`
call opens this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .. import index_paths, reserved_paths, sidecar_store

if TYPE_CHECKING:
    from .schema_v4 import MigrationSeed, VerifiedActiveGovernanceState

SCHEMA_USER_VERSION = 3
DATA_TABLE = "compiled_policy"
_INITIALIZED_SIDECARS_MAX = 64
_INITIALIZED_SIDECARS: OrderedDict[
    Path, tuple[int, int, int, int, int, str]
] = OrderedDict()
_INITIALIZED_SIDECARS_LOCK = threading.Lock()


class UnsupportedGovernanceSchema(RuntimeError):
    """The authoring core cannot safely interpret this sidecar version."""


def _reset_initialized_sidecars_after_fork() -> None:
    global _INITIALIZED_SIDECARS, _INITIALIZED_SIDECARS_LOCK
    _INITIALIZED_SIDECARS = OrderedDict()
    _INITIALIZED_SIDECARS_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_initialized_sidecars_after_fork)


def sidecar_path(vault_root: Path) -> Path:
    return index_paths.governance_sidecar_path(Path(vault_root))


def open_readonly_connection(vault_root: Path) -> sqlite3.Connection | None:
    """Open an existing supported sidecar without migration, DDL, or creation."""

    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("governance-store",),
            identity_may_change=False,
        ):
            return _open_readonly_connection_owned(vault_root)


def _open_readonly_connection_owned(vault_root: Path) -> sqlite3.Connection | None:
    path = sidecar_path(vault_root)
    try:
        with reserved_paths._sqlite_owner_target_scope(
            vault_root,
            path,
            "governance-store",
            create=False,
        ) as retained_path:
            conn = sqlite3.connect(f"{retained_path.as_uri()}?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                if (
                    int(conn.execute("PRAGMA user_version").fetchone()[0])
                    != SCHEMA_USER_VERSION
                ):
                    conn.close()
                    return None
                reserved_paths._publish_sqlite_owner_family(
                    vault_root,
                    path,
                    "governance-store",
                    conn,
                )
                return conn
            except BaseException:
                conn.close()
                raise
    except (FileNotFoundError, RuntimeError, sqlite3.Error, OSError):
        return None


def open_connection(
    vault_root: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open (creating if absent) the governance sidecar with its schema in place."""

    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("governance-store",),
        ):
            return _open_connection_owned(
                vault_root,
                check_same_thread=check_same_thread,
            )


def open_authorization_session_connection(
    vault_root: Path,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open an existing exact-v4 store for session lifecycle DML only.

    This opener never creates or migrates a sidecar. Ordinary v3 openers remain
    unchanged; only the explicit offline coordinator may perform the v3-to-v4
    transition before this boundary becomes available.
    """
    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("governance-store",),
        ):
            path = sidecar_path(vault_root)
            with reserved_paths._sqlite_owner_target_scope(
                vault_root,
                path,
                "governance-store",
                create=False,
            ) as retained_path:
                connection = sqlite3.connect(
                    f"{retained_path.as_uri()}?mode=rw",
                    uri=True,
                    check_same_thread=check_same_thread,
                )
                try:
                    from . import schema_v4

                    version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if version != schema_v4.SCHEMA_USER_VERSION:
                        raise UnsupportedGovernanceSchema(
                            "authorization sessions require an existing exact-v4 store"
                        )
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("PRAGMA busy_timeout=5000")
                    reserved_paths._publish_sqlite_owner_family(
                        vault_root,
                        path,
                        "governance-store",
                        connection,
                    )
                    return connection
                except BaseException:
                    connection.close()
                    raise


def open_active_governance_read_connection(vault_root: Path) -> sqlite3.Connection:
    """Open one existing exact-v4 activation store as a pinned read source.

    The caller starts an explicit SQLite read transaction before sampling the
    active tuple.  Legacy stores are reported distinctly so the staged rollout
    can retain the current v3 reader until the external schema fence is active.
    """

    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("governance-store",),
            identity_may_change=False,
        ):
            path = sidecar_path(vault_root)
            try:
                target = reserved_paths._sqlite_owner_target_scope(
                    vault_root,
                    path,
                    "governance-store",
                    create=False,
                )
                with target as retained_path:
                    connection = sqlite3.connect(
                        f"{retained_path.as_uri()}?mode=ro",
                        uri=True,
                    )
                    try:
                        from . import schema_v4

                        version = int(
                            connection.execute("PRAGMA user_version").fetchone()[0]
                        )
                        if version != schema_v4.SCHEMA_USER_VERSION:
                            raise UnsupportedGovernanceSchema(
                                "active governance requires an existing exact-v4 store"
                            )
                        connection.execute("PRAGMA query_only=ON")
                        connection.execute("PRAGMA busy_timeout=50")
                        reserved_paths._publish_sqlite_owner_family(
                            vault_root,
                            path,
                            "governance-store",
                            connection,
                        )
                        return connection
                    except BaseException:
                        connection.close()
                        raise
            except FileNotFoundError as exc:
                raise UnsupportedGovernanceSchema(
                    "active governance store is absent"
                ) from exc


def authorization_session_schema_version(vault_root: Path) -> int | None:
    """Return exact v3/v4 for an existing protected store, never creating one."""

    connection: sqlite3.Connection | None = None
    try:
        connection = open_authorization_session_connection(vault_root)
    except UnsupportedGovernanceSchema:
        legacy = open_readonly_connection(vault_root)
        if legacy is None:
            return None
        legacy.close()
        return SCHEMA_USER_VERSION
    except (FileNotFoundError, OSError, sqlite3.Error):
        return None
    else:
        from . import schema_v4

        return schema_v4.SCHEMA_USER_VERSION
    finally:
        if connection is not None:
            connection.close()


def _schema_migration_barrier(point: str) -> None:
    """Test seam after the only durable database effect."""

    del point


def _v3_snapshot_digest(connection: sqlite3.Connection) -> str:
    snapshot = sqlite3.connect(":memory:")
    try:
        connection.backup(snapshot)
        snapshot.execute("VACUUM")
        serialized = snapshot.serialize()
    finally:
        snapshot.close()
    domain = b"exomem.governance-v3-snapshot.v1"
    return hashlib.sha256(
        domain + len(serialized).to_bytes(8, "big") + serialized
    ).hexdigest()


def migrate_enrolled_v3_store(
    vault_root: Path,
    *,
    seed: MigrationSeed,
    expected_source_store_digest: str,
    now: int,
    source_recheck: Callable[[], None] | None = None,
) -> VerifiedActiveGovernanceState:
    """Commit one pre-enrolled exact-v3 store to its exact v4 target.

    This is the filesystem-backed offline migration coordinator.  It verifies
    the authenticated serving membership is wholly drained at schema v3 before
    the first database effect, then advances the external membership to serving
    v4 only after the exact store target commits.  The cooperative identity guard
    keeps current Exomem writers out while the exact policy workspace is
    rechecked; external direct OS mutation remains outside that guarantee and is
    detected by the post-commit observation.
    """

    from .. import writer_lease
    from . import authorization_custody, policy, schema_v4

    root = Path(vault_root)
    path = sidecar_path(root)
    if (
        not isinstance(expected_source_store_digest, str)
        or len(expected_source_store_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_source_store_digest
        )
    ):
        raise schema_v4.SchemaV4Error("migration source store digest is invalid")
    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=("governance-store",),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                path,
                "governance-store",
                create=False,
            ) as retained_path:
                connection = sqlite3.connect(
                    f"{retained_path.as_uri()}?mode=rw",
                    uri=True,
                )
                try:
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("PRAGMA busy_timeout=0")
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        connection,
                    )
                    version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if version == SCHEMA_USER_VERSION:
                        schema_v4.require_exact_v3_connection(connection)
                    elif version == schema_v4.SCHEMA_USER_VERSION:
                        schema_v4.require_exact_v4_connection(connection)
                    else:
                        raise UnsupportedGovernanceSchema(
                            "explicit governance migration requires exact schema v3 or v4"
                        )

                    target = schema_v4.migration_target(seed)
                    custody = authorization_custody.load_authorization_custody(
                        root,
                        now=now,
                    )
                    control = custody.control
                    if (
                        not control.governance_enrolled
                        or control.activation_store_id != target.activation_store_id
                        or control.activation_epoch != target.activation_epoch
                        or control.activation_state_digest
                        != target.activation_state_digest
                        or control.logical_vault_id != target.logical_vault_id
                    ):
                        raise authorization_custody.AuthorizationCustodyUnavailable
                    if version == SCHEMA_USER_VERSION and (
                        custody.serving_membership is None
                        or custody.local_replica_id is None
                        or not custody.serving_membership.replicas
                        or any(
                            replica.state != "DRAINING"
                            or replica.schema_version != SCHEMA_USER_VERSION
                            or not replica.issuance_stopped
                            or not replica.no_in_flight
                            for replica in custody.serving_membership.replicas
                        )
                    ):
                        raise authorization_custody.AuthorizationCustodyUnavailable
                    if version == SCHEMA_USER_VERSION and (
                        _v3_snapshot_digest(connection) != expected_source_store_digest
                    ):
                        raise schema_v4.SchemaV4Error(
                            "migration source store changed after owner review"
                        )

                    before = None
                    if version == SCHEMA_USER_VERSION:
                        before = policy.observe_authoring_snapshot(root)
                        if (
                            before is None
                            or before.documents != seed.policy.source_documents
                            or before.source_fingerprint
                            != seed.policy.source_fingerprint
                            or before.conflict_set_digest
                            != seed.policy.conflict_digest
                        ):
                            raise schema_v4.SchemaV4Error(
                                "migration policy workspace does not match the reviewed seed"
                            )

                    try:
                        fenced = writer_lease.advance_configured_schema_fence(
                            source_schema_version=SCHEMA_USER_VERSION,
                            target_schema_version=schema_v4.SCHEMA_USER_VERSION,
                        )
                    except writer_lease.OpError:
                        raise authorization_custody.AuthorizationCustodyUnavailable from None
                    if fenced is not None:
                        _schema_migration_barrier("after_schema_fence")

                    result = schema_v4.migrate_v3_connection(connection, seed)
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        connection,
                    )
                    _schema_migration_barrier("after_store_commit")
                    if fenced is not None:
                        try:
                            confirmed_fence = (
                                writer_lease.require_configured_schema_fence(
                                    schema_v4.SCHEMA_USER_VERSION
                                )
                            )
                        except writer_lease.OpError:
                            raise authorization_custody.AuthorizationCustodyUnavailable from None
                        if confirmed_fence != fenced:
                            raise authorization_custody.AuthorizationCustodyUnavailable
                    active = schema_v4.load_active_policy(
                        connection,
                        expected_logical_vault_id=target.logical_vault_id,
                        expected_activation_store_id=target.activation_store_id,
                        expected_activation_epoch=target.activation_epoch,
                        expected_activation_state_digest=(
                            target.activation_state_digest
                        ),
                    ).active
                    if (
                        result.schema_version != schema_v4.SCHEMA_USER_VERSION
                        or result.activation_store_id != target.activation_store_id
                        or result.activation_state_digest
                        != target.activation_state_digest
                        or active != target
                    ):
                        raise schema_v4.SchemaV4Error(
                            "migration result does not match the enrolled target"
                        )
                    if before is not None:
                        after = policy.observe_authoring_snapshot(root)
                        if after != before:
                            raise schema_v4.SchemaV4Error(
                                "migration policy workspace changed during commit"
                            )
                    if source_recheck is not None:
                        source_recheck()
                finally:
                    connection.close()

            verified = authorization_custody.complete_standalone_v4_migration(
                root,
                target=target,
                now=now,
            )
            if (
                verified.keyring != custody.keyring
                or verified.control.logical_vault_id != target.logical_vault_id
                or verified.control.activation_store_id != target.activation_store_id
                or verified.control.activation_epoch != target.activation_epoch
                or verified.control.activation_state_digest
                != target.activation_state_digest
                or verified.control.serving_membership_epoch != 2
            ):
                raise authorization_custody.AuthorizationCustodyUnavailable
    return active


def _open_connection_owned(
    vault_root: Path, *, check_same_thread: bool
) -> sqlite3.Connection:
    path = sidecar_path(vault_root)
    with reserved_paths._sqlite_owner_target_scope(
        vault_root,
        path,
        "governance-store",
        create=True,
    ) as retained_path:
        return _open_connection_retained(
            vault_root,
            retained_path,
            check_same_thread=check_same_thread,
        )


def _open_connection_retained(
    vault_root: Path,
    path: Path,
    *,
    check_same_thread: bool,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    try:
        with _INITIALIZED_SIDECARS_LOCK:
            state = _connection_state(conn, path)
            cached = _INITIALIZED_SIDECARS.get(path)
            if cached == state:
                # WAL mode persists in the database. These two pragmas are
                # connection-local and cheap, so every new handle still gets
                # the production timeout/durability settings without rerunning
                # journal negotiation and idempotent DDL on every receipt.
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=5000")
                _INITIALIZED_SIDECARS.move_to_end(path)
            else:
                sidecar_store.apply_sidecar_pragmas(conn)
                conn.execute("PRAGMA synchronous=FULL")
                _migrate(conn)
                if int(conn.execute("PRAGMA user_version").fetchone()[0]) <= SCHEMA_USER_VERSION:
                    sidecar_store.ensure_meta_table(conn, DATA_TABLE, "governance")
                    conn.commit()
                _INITIALIZED_SIDECARS[path] = _connection_state(conn, path)
                _INITIALIZED_SIDECARS.move_to_end(path)
                while len(_INITIALIZED_SIDECARS) > _INITIALIZED_SIDECARS_MAX:
                    _INITIALIZED_SIDECARS.popitem(last=False)
        reserved_paths._publish_sqlite_owner_family(
            vault_root,
            path,
            "governance-store",
            conn,
        )
        return conn
    except BaseException:
        conn.close()
        raise


def _connection_state(
    conn: sqlite3.Connection, path: Path
) -> tuple[int, int, int, int, int, str]:
    """Identity + live schema state; DML does not invalidate this fast path."""
    stat_result = path.stat()
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return (
        os.getpid(),
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        user_version,
        schema_version,
        journal_mode,
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply known sidecar migrations without ever lowering a newer version."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {DATA_TABLE} ("
            "fingerprint TEXT PRIMARY KEY, snapshot TEXT NOT NULL, compiled_at REAL NOT NULL)"
        )
        version = 1
    if version < 2:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_instance "
            "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), instance_id TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipts_head ("
            "instance_id TEXT PRIMARY KEY, durable_seq INTEGER NOT NULL, durable_hash TEXT NOT NULL, "
            "observed_seq INTEGER NOT NULL, observed_hash TEXT NOT NULL, "
            "path TEXT NOT NULL DEFAULT '', byte_offset INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_secrets ("
            "name TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        version = 2
    if version == 2:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {DATA_TABLE} ("
            "fingerprint TEXT PRIMARY KEY, snapshot TEXT NOT NULL, compiled_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_instance "
            "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), instance_id TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipts_head ("
            "instance_id TEXT PRIMARY KEY, durable_seq INTEGER NOT NULL, durable_hash TEXT NOT NULL, "
            "observed_seq INTEGER NOT NULL, observed_hash TEXT NOT NULL, "
            "path TEXT NOT NULL DEFAULT '', byte_offset INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_secrets ("
            "name TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(receipts_head)")}
        if "path" not in columns:
            conn.execute("ALTER TABLE receipts_head ADD COLUMN path TEXT NOT NULL DEFAULT ''")
        if "byte_offset" not in columns:
            conn.execute(
                "ALTER TABLE receipts_head ADD COLUMN byte_offset INTEGER NOT NULL DEFAULT 0"
            )
    if version < 3:
        _migrate_v3(conn)
        version = 3
    elif version == 3:
        _migrate_v3(conn)
        _add_column(
            conn,
            "governance_session_grants",
            "membership_manifest TEXT NOT NULL DEFAULT '[]'",
        )
        _add_column(
            conn,
            "governance_session_grants",
            "policy_fingerprint TEXT NOT NULL DEFAULT ''",
        )
    if version <= SCHEMA_USER_VERSION:
        conn.execute(f"PRAGMA user_version = {version}")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    name = declaration.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Install the complete governance-authoring schema in one owned step."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS withhold_tokens ("
        "jti TEXT PRIMARY KEY, audience TEXT NOT NULL, max_level INTEGER NOT NULL, "
        "fingerprints TEXT NOT NULL, paths TEXT NOT NULL, expires_at INTEGER NOT NULL, "
        "minted_at REAL NOT NULL, consumed_at REAL)"
    )
    _add_column(conn, "withhold_tokens", "authorization_session TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "withhold_tokens", "purpose TEXT")
    _add_column(conn, "withhold_tokens", "org_ceiling INTEGER NOT NULL DEFAULT 6")
    _add_column(conn, "withhold_tokens", "status TEXT NOT NULL DEFAULT 'active'")
    _add_column(conn, "withhold_tokens", "prepared_event_id TEXT")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_proposals ("
        "proposal_id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "proposal_json TEXT NOT NULL, fingerprint_at_propose TEXT NOT NULL, "
        "membership_manifest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "reserved_event_id TEXT, attempt_no INTEGER NOT NULL DEFAULT 0, "
        "attempt_nonce TEXT, spent_at REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_operation_journals ("
        "event_id TEXT PRIMARY KEY, operation TEXT NOT NULL, causation_id TEXT NOT NULL, "
        "authorization_session TEXT, principal_id TEXT NOT NULL, phase TEXT NOT NULL, "
        "direction TEXT NOT NULL, prior_digest TEXT NOT NULL, prepared_digest TEXT NOT NULL, "
        "final_digest TEXT NOT NULL, affected_ids TEXT NOT NULL, "
        "required_child_intents TEXT NOT NULL, required_child_terminals TEXT NOT NULL, "
        "proposal_id TEXT, attempt_no INTEGER, marker_required INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, blocked_reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_operation_components ("
        "event_id TEXT NOT NULL, phase TEXT NOT NULL, ordinal INTEGER NOT NULL, "
        "component_kind TEXT NOT NULL, component_key TEXT NOT NULL, "
        "value_json TEXT NOT NULL, value_hash TEXT NOT NULL, status TEXT NOT NULL, "
        "PRIMARY KEY(event_id, phase, ordinal), "
        "FOREIGN KEY(event_id) REFERENCES governance_operation_journals(event_id))"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS governance_components_no_update "
        "BEFORE UPDATE ON governance_operation_components BEGIN "
        "SELECT RAISE(ABORT, 'governance operation components are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS governance_components_no_delete "
        "BEFORE DELETE ON governance_operation_components BEGIN "
        "SELECT RAISE(ABORT, 'governance operation components are immutable'); END"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_session_grants ("
        "grant_id TEXT PRIMARY KEY, authorization_session TEXT NOT NULL, "
        "audience TEXT NOT NULL, purpose TEXT, ceiling INTEGER NOT NULL, "
        "paths TEXT NOT NULL, fingerprints TEXT NOT NULL, token_jti TEXT NOT NULL, "
        "status TEXT NOT NULL, prepared_event_id TEXT, created_at REAL NOT NULL, "
        "expires_at REAL NOT NULL, revoked_at REAL, "
        "membership_manifest TEXT NOT NULL DEFAULT '[]', "
        "policy_fingerprint TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_session_purpose ("
        "authorization_session TEXT PRIMARY KEY, principal_id TEXT NOT NULL, "
        "purpose TEXT NOT NULL, status TEXT NOT NULL, prepared_event_id TEXT, "
        "created_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_session_purpose_staging ("
        "event_id TEXT PRIMARY KEY, authorization_session TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, purpose TEXT NOT NULL, created_at REAL NOT NULL, "
        "expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS governance_policy_archives ("
        "archive_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, path TEXT NOT NULL, "
        "prior_bytes BLOB, prior_hash TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS governance_journals_phase "
        "ON governance_operation_journals(phase)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS governance_grants_session "
        "ON governance_session_grants(authorization_session, audience, status)"
    )


def require_authoring_schema(
    vault_root: Path,
    *,
    supported_versions: tuple[int, ...] = (SCHEMA_USER_VERSION,),
) -> None:
    """Refuse authoring on a schema this release cannot interpret."""
    if (
        type(supported_versions) is not tuple
        or not supported_versions
        or any(type(version) is not int or version < 1 for version in supported_versions)
    ):
        raise ValueError("supported authoring schema versions are invalid")
    conn = open_connection(vault_root)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    if version not in supported_versions:
        raise UnsupportedGovernanceSchema(
            "governance authoring requires schema "
            f"{','.join(f'v{item}' for item in supported_versions)}, found v{version}"
        )


def pinned_component_keys(
    vault_root: Path, *, conn: sqlite3.Connection | None = None
) -> dict[str, frozenset[str]]:
    """Rows referenced by a live recovery journal and therefore not GC-able."""
    path = sidecar_path(vault_root)
    if not path.exists():
        return {}
    owns_connection = conn is None
    active = open_connection(vault_root) if conn is None else conn
    try:
        rows = active.execute(
            "SELECT DISTINCT c.component_kind, c.component_key "
            "FROM governance_operation_components c "
            "JOIN governance_operation_journals j ON j.event_id=c.event_id "
            "WHERE j.phase IN ('allocating', 'pending')"
        ).fetchall()
    finally:
        if owns_connection:
            active.close()
    grouped: dict[str, set[str]] = {}
    for kind, key in rows:
        grouped.setdefault(str(kind), set()).add(str(key))
    return {kind: frozenset(keys) for kind, keys in grouped.items()}


def _delete_expired_except(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    expiry_column: str,
    now: float,
    pinned: frozenset[str],
) -> int:
    if pinned:
        placeholders = ",".join("?" for _ in pinned)
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {expiry_column} < ? "
            f"AND {key_column} NOT IN ({placeholders})",
            (now, *sorted(pinned)),
        )
    else:
        cursor = conn.execute(f"DELETE FROM {table} WHERE {expiry_column} < ?", (now,))
    return int(cursor.rowcount or 0)


def sweep_authoring_state(vault_root: Path, *, now: float | None = None) -> int:
    """Physically retire expired tool state while pinning live composites."""
    if not sidecar_path(vault_root).exists():
        return 0
    moment = __import__("time").time() if now is None else float(now)
    conn = open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        pinned = pinned_component_keys(vault_root, conn=conn)
        removed = _delete_expired_except(
            conn,
            table="governance_proposals",
            key_column="proposal_id",
            expiry_column="expires_at",
            now=moment,
            pinned=pinned.get("proposal", frozenset()),
        )
        grant_pins = pinned.get("grant", frozenset()) | pinned.get(
            "dependent_grant", frozenset()
        )
        removed += _delete_expired_except(
            conn,
            table="governance_session_grants",
            key_column="grant_id",
            expiry_column="expires_at",
            now=moment,
            pinned=grant_pins,
        )
        removed += _delete_expired_except(
            conn,
            table="governance_session_purpose",
            key_column="authorization_session",
            expiry_column="expires_at",
            now=moment,
            pinned=pinned.get("purpose", frozenset()),
        )
        cursor = conn.execute(
            "DELETE FROM governance_session_purpose_staging WHERE expires_at < ? "
            "AND event_id NOT IN (SELECT event_id FROM governance_operation_journals "
            "WHERE phase IN ('allocating', 'pending'))",
            (moment,),
        )
        removed += int(cursor.rowcount or 0)
        conn.commit()
        return removed
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def active_session_grants(
    vault_root: Path,
    *,
    audience: str,
    authorization_session: str | None,
    rel_path: str,
    purpose: str | None,
    now: float | None = None,
) -> tuple[list[dict[str, object]], str]:
    """Return exact unchanged active grants and their decision identity."""
    if not authorization_session or not sidecar_path(vault_root).exists():
        return [], "no-session-grants"
    # Schema-v3 rows bind paths and a membership snapshot, but do not persist
    # the exact reviewed scope IDs. Re-evaluating current item membership and
    # treating it as a grant binding would let a legacy row authorise sibling
    # scopes it never reviewed. v4 will store that proof; v3 grants are inert.
    del audience, rel_path, purpose, now
    return [], "v3-session-grants-unscoped"


def active_session_purpose(
    vault_root: Path,
    *,
    audience: str,
    authorization_session: str | None,
    now: float | None = None,
) -> str | None:
    """Resolve the live purpose default for one explicit authorization session."""
    if not authorization_session or not sidecar_path(vault_root).exists():
        return None
    moment = __import__("time").time() if now is None else float(now)
    conn = open_readonly_connection(vault_root)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT purpose FROM governance_session_purpose WHERE authorization_session=? "
            "AND principal_id=? AND status='active' AND expires_at>=?",
            (authorization_session, audience, moment),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


_V3_GUARD_TABLES = frozenset(
    {
        "governance_operation_journals",
        "governance_operation_components",
        "governance_proposals",
        "governance_session_grants",
        "governance_session_purpose",
        "governance_session_purpose_staging",
        "governance_policy_archives",
        "withhold_tokens",
    }
)

_V4_GUARD_TABLES = _V3_GUARD_TABLES | frozenset(
    {
        "governance_authorization_sessions",
        "compiled_policy_generations",
        "catalog_generation_descriptors",
        "governance_projection_namespaces",
        "active_governance_tuple",
        "governance_activation_store",
        "governance_schema_migrations",
        "governance_tuple_publications",
        "governance_legacy_authority",
    }
)


def guard_generation_probe(vault_root: Path) -> dict[str, object]:
    """Non-creating read-only seqlock probe for the policy loader."""
    path = sidecar_path(vault_root)
    if not path.exists():
        return {"state": "clear", "generation": "absent", "event_ids": ()}
    try:
        with reserved_paths._subsystem_authority_scope("governance.store"):
            with reserved_paths._identity_coordination_scope(
                vault_root,
                descriptor_ids=("governance-store",),
                identity_may_change=False,
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    vault_root,
                    path,
                    "governance-store",
                    create=False,
                ) as retained_path:
                    return _guard_generation_probe_retained(
                        vault_root,
                        path,
                        retained_path,
                    )
    except (RuntimeError, sqlite3.Error, OSError) as exc:
        return {
            "state": "blocked",
            "generation": f"unreadable:{type(exc).__name__}",
            "event_ids": (),
        }


def _guard_generation_probe_retained(
    vault_root: Path,
    path: Path,
    retained_path: Path,
) -> dict[str, object]:
    conn = sqlite3.connect(
        f"{retained_path.as_uri()}?mode=ro",
        uri=True,
        timeout=0.05,
    )
    try:
        reserved_paths._publish_sqlite_owner_family(
            vault_root,
            path,
            "governance-store",
            conn,
        )
        conn.execute("PRAGMA query_only=ON")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_USER_VERSION:
            return {
                "state": "clear",
                "generation": f"legacy:{version}",
                "event_ids": (),
            }
        if version not in {SCHEMA_USER_VERSION, 4}:
            return {
                "state": "blocked",
                "generation": f"unsupported:{version}",
                "event_ids": (),
            }
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required_tables = (
            _V4_GUARD_TABLES
            if version == 4
            else _V3_GUARD_TABLES - {"governance_session_purpose_staging"}
        )
        if not required_tables <= tables:
            return {
                "state": "blocked",
                "generation": "structurally-unknown",
                "event_ids": (),
            }
        pending = [
            tuple(row)
            for row in conn.execute(
                "SELECT event_id, operation, prior_digest, prepared_digest, final_digest, "
                "affected_ids, required_child_intents, required_child_terminals, "
                "marker_required, updated_at FROM governance_operation_journals "
                "WHERE phase='pending' ORDER BY event_id"
            )
        ]
        if (
            version == SCHEMA_USER_VERSION
            and "governance_session_purpose_staging" not in tables
            and pending
        ):
            return {
                "state": "blocked",
                "generation": "legacy-open-protocol",
                "event_ids": tuple(str(row[0]) for row in pending),
            }
        schema_generation = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        active = (
            conn.execute(
                "SELECT policy_generation_id, policy_fingerprint, "
                "projector_schema_version, catalog_generation "
                "FROM active_governance_tuple WHERE singleton=1"
            ).fetchone()
            if version == 4
            else None
        )
        activation = (
            conn.execute(
                "SELECT activation_store_id, logical_vault_id, activation_epoch, "
                "activation_state_digest FROM governance_activation_store "
                "WHERE singleton=1"
            ).fetchone()
            if version == 4
            else None
        )
        if version == 4 and (active is None or activation is None):
            return {
                "state": "blocked",
                "generation": "activation-incomplete",
                "event_ids": tuple(str(row[0]) for row in pending),
            }
    finally:
        conn.close()
    generation = hashlib.sha256(
        json.dumps(
            [version, schema_generation, pending, active, activation],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "state": "pending" if pending else "clear",
        "generation": generation,
        "event_ids": tuple(str(row[0]) for row in pending),
    }
