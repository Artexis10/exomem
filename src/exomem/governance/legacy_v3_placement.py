"""Deterministic publication and reconciliation of the one v3 rollback copy."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .. import held_fs
from ..kbdir import kb_dirname
from . import receipts, schema_v4, store

_DESTINATION_LEAF = ".governance.sqlite"
_STAGE_PREFIX = ".governance-v3-rollback-"
_STAGE_SUFFIX = ".sqlite"
_EVENT_ID = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_TABLES = ("receipt_instance", "receipt_secrets", "receipts_head")


class LegacyV3PublicationUnavailable(RuntimeError):
    """The v3 rollback database cannot be proved, published, or aligned safely."""


def legacy_v3_path(vault_root: Path) -> Path:
    """The exact historical v0.57 database location, never caller-selected."""

    return Path(vault_root) / kb_dirname() / _DESTINATION_LEAF


def rollback_stage_leaf(event_id: str) -> str:
    """Return the only permitted direct-child durable stage name for *event_id*."""

    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise LegacyV3PublicationUnavailable
    return f"{_STAGE_PREFIX}{event_id}{_STAGE_SUFFIX}"


def _valid_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LegacyV3PublicationUnavailable
    return value


def _digest(connection: sqlite3.Connection) -> str:
    schema_v4.require_exact_v3_connection(connection)
    return store._v3_snapshot_digest(connection)  # noqa: SLF001 - canonical v3 digest seam


def _digest_bytes(snapshot_bytes: bytes) -> str:
    if not isinstance(snapshot_bytes, bytes) or not snapshot_bytes:
        raise LegacyV3PublicationUnavailable
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(snapshot_bytes)
        return _digest(connection)
    except (sqlite3.Error, schema_v4.SchemaV4Error, ValueError):
        raise LegacyV3PublicationUnavailable from None
    finally:
        connection.close()


def _require(result: held_fs.HeldResult[object]) -> object:
    try:
        return result.require()
    except held_fs.HeldFsError:
        raise LegacyV3PublicationUnavailable from None


def _read_exact(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
    leaf: str,
    *,
    links: int | None,
) -> tuple[held_fs.StableIdentity, bytes] | None:
    opened = filesystem.file(parent, leaf)
    if not opened.ok:
        if opened.error is not None and opened.error.code == "MISSING":
            return None
        _require(opened)
    with _require(opened) as file:  # type: ignore[union-attr]
        if (
            file.identity.kind != "file"
            or (links is not None and file.identity.link_count != links)
        ):
            raise LegacyV3PublicationUnavailable
        data = _require(filesystem.read(file))
        if not isinstance(data, bytes):  # pragma: no cover - held contract
            raise LegacyV3PublicationUnavailable
        return file.identity, data


def _same_identity(first: held_fs.StableIdentity, second: held_fs.StableIdentity) -> bool:
    return (
        first.device == second.device
        and first.inode == second.inode
        and first.kind == second.kind == "file"
    )


def _revalidate_legacy_binding(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
    retained: held_fs.HeldFile,
) -> None:
    """Prove the v0.57 pathname still resolves to the retained single-link file."""

    if retained.identity.kind != "file" or retained.identity.link_count != 1:
        raise LegacyV3PublicationUnavailable
    current = filesystem.file(parent, _DESTINATION_LEAF)
    with _require(current) as observed:  # type: ignore[union-attr]
        if (
            observed.identity.kind != "file"
            or observed.identity.link_count != 1
            or not _same_identity(retained.identity, observed.identity)
        ):
            raise LegacyV3PublicationUnavailable


@contextmanager
def _retained_legacy_binding(vault_root: Path, *, mutate: bool):
    """Hold the historical file and its KB parent across a SQLite operation."""

    acquired = held_fs.acquire(Path(vault_root))
    with _require(acquired) as filesystem:  # type: ignore[union-attr]
        parent = filesystem.parent(
            kb_dirname(),
            access="mutate" if mutate else "read",
        )
        with _require(parent) as retained_parent:  # type: ignore[union-attr]
            opened = filesystem.file(retained_parent, _DESTINATION_LEAF)
            with _require(opened) as retained_file:  # type: ignore[union-attr]
                _revalidate_legacy_binding(filesystem, retained_parent, retained_file)
                yield filesystem, retained_parent, retained_file


def _barrier(barrier: Callable[[str], None] | None, point: str) -> None:
    if barrier is not None:
        barrier(point)


def publish_exact_v3_bytes(
    filesystem: held_fs.HeldFilesystem,
    parent: held_fs.HeldDirectory,
    *,
    event_id: str,
    snapshot_bytes: bytes,
    expected_digest: str,
    barrier: Callable[[str], None] | None = None,
) -> str:
    """Reconcile the deterministic D0 stage/destination state through held handles."""

    expected = _valid_digest(expected_digest)
    stage = rollback_stage_leaf(event_id)
    if _digest_bytes(snapshot_bytes) != expected:
        raise LegacyV3PublicationUnavailable
    _require(filesystem.validate_directory(parent))

    stage_state = _read_exact(filesystem, parent, stage, links=None)
    destination_state = _read_exact(filesystem, parent, _DESTINATION_LEAF, links=None)
    if stage_state is None and destination_state is not None:
        if (
            destination_state[0].link_count != 1
            or _digest_bytes(destination_state[1]) != expected
        ):
            raise LegacyV3PublicationUnavailable
        return expected
    if stage_state is not None and destination_state is not None:
        stage_two = _read_exact(filesystem, parent, stage, links=2)
        destination_two = _read_exact(filesystem, parent, _DESTINATION_LEAF, links=2)
        if (
            stage_two is None
            or destination_two is None
            or not _same_identity(stage_two[0], destination_two[0])
            or _digest_bytes(stage_two[1]) != expected
            or _digest_bytes(destination_two[1]) != expected
        ):
            raise LegacyV3PublicationUnavailable
        # A crash before the original link's directory flush leaves both names
        # reachable but not yet durably named.  Make that exact two-link state
        # durable before removing either name; otherwise recovery can turn an
        # ambiguous link into a destination-only claim without proving it.
        _require(filesystem.flush_directory(parent))
        _barrier(barrier, "after_recovery_link_flush")
        opened = filesystem.file(parent, stage, access="mutate")
        with _require(opened) as residue:  # type: ignore[union-attr]
            if not _same_identity(residue.identity, stage_two[0]) or residue.identity.link_count != 2:
                raise LegacyV3PublicationUnavailable
            _require(filesystem.unlink(residue))
        _require(filesystem.flush_directory(parent))
        _barrier(barrier, "after_stage_unlink")
        destination = _read_exact(filesystem, parent, _DESTINATION_LEAF, links=1)
        if destination is None or _digest_bytes(destination[1]) != expected:
            raise LegacyV3PublicationUnavailable
        return expected

    if stage_state is None:
        created = filesystem.file(parent, stage, access="write", create=True, exclusive=True)
        with _require(created) as staged:  # type: ignore[union-attr]
            _require(filesystem.write(staged, snapshot_bytes))
        _require(filesystem.flush_directory(parent))
        _barrier(barrier, "after_stage_write")
    elif stage_state[0].link_count != 1 or _digest_bytes(stage_state[1]) != expected:
        raise LegacyV3PublicationUnavailable

    staged = filesystem.file(parent, stage, access="mutate")
    with _require(staged) as source:  # type: ignore[union-attr]
        if source.identity.kind != "file" or source.identity.link_count != 1:
            raise LegacyV3PublicationUnavailable
        contents = _require(filesystem.read(source))
        if not isinstance(contents, bytes) or _digest_bytes(contents) != expected:
            raise LegacyV3PublicationUnavailable
        _require(filesystem.link(source, parent, _DESTINATION_LEAF))
    _require(filesystem.flush_directory(parent))
    _barrier(barrier, "after_destination_link")

    stage_two = _read_exact(filesystem, parent, stage, links=2)
    destination_two = _read_exact(filesystem, parent, _DESTINATION_LEAF, links=2)
    if (
        stage_two is None
        or destination_two is None
        or not _same_identity(stage_two[0], destination_two[0])
        or _digest_bytes(stage_two[1]) != expected
    ):
        raise LegacyV3PublicationUnavailable
    staged = filesystem.file(parent, stage, access="mutate")
    with _require(staged) as source:  # type: ignore[union-attr]
        if not _same_identity(source.identity, stage_two[0]) or source.identity.link_count != 2:
            raise LegacyV3PublicationUnavailable
        _require(filesystem.unlink(source))
    _require(filesystem.flush_directory(parent))
    _barrier(barrier, "after_stage_unlink")
    destination = _read_exact(filesystem, parent, _DESTINATION_LEAF, links=1)
    if destination is None or _digest_bytes(destination[1]) != expected:
        raise LegacyV3PublicationUnavailable
    return expected


def _external_snapshot_bytes(vault_root: Path, expected_digest: str) -> bytes:
    source = sqlite3.connect(f"{store.sidecar_path(Path(vault_root)).as_uri()}?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        if _digest(source) != expected_digest:
            raise LegacyV3PublicationUnavailable
        source.backup(snapshot)
        snapshot.execute("VACUUM")
        data = snapshot.serialize()
        if _digest_bytes(data) != expected_digest:
            raise LegacyV3PublicationUnavailable
        return data
    except (sqlite3.Error, schema_v4.SchemaV4Error, ValueError):
        raise LegacyV3PublicationUnavailable from None
    finally:
        snapshot.close()
        source.close()


def exact_external_v3_digest(vault_root: Path) -> str:
    path = store.sidecar_path(Path(vault_root))
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            return _digest(connection)
        finally:
            connection.close()
    except (sqlite3.Error, schema_v4.SchemaV4Error, OSError):
        raise LegacyV3PublicationUnavailable from None


def publish_exact_v3_snapshot(
    vault_root: Path,
    *,
    expected_digest: str,
    event_id: str | None = None,
    barrier: Callable[[str], None] | None = None,
) -> str:
    """Compatibility wrapper that proves the external D0 then uses held publication."""

    expected = _valid_digest(expected_digest)
    root = Path(vault_root)
    snapshot_bytes = _external_snapshot_bytes(root, expected)
    acquired = held_fs.acquire(root)
    with _require(acquired) as filesystem:  # type: ignore[union-attr]
        parent = filesystem.parent(kb_dirname(), create=True, access="mutate")
        with _require(parent) as retained_parent:  # type: ignore[union-attr]
            return publish_exact_v3_bytes(
                filesystem,
                retained_parent,
                event_id=event_id or f"compat-{expected}",
                snapshot_bytes=snapshot_bytes,
                expected_digest=expected,
                barrier=barrier,
            )


def _receipt_schema(connection: sqlite3.Connection) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    return tuple(
        (table, tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")))
        for table in _RECEIPT_TABLES
    )


def _rows(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))


def _active_head(connection: sqlite3.Connection) -> tuple[str, tuple[object, ...]]:
    instances = _rows(connection, "receipt_instance")
    if len(instances) != 1 or instances[0][0] != 1 or not isinstance(instances[0][1], str):
        raise LegacyV3PublicationUnavailable
    instance_id = instances[0][1]
    heads = _rows(connection, "receipts_head")
    active = [row for row in heads if row[0] == instance_id]
    if len(active) != 1 or len(active[0]) != 7:
        raise LegacyV3PublicationUnavailable
    return instance_id, active[0]


def _head_values(row: tuple[object, ...]) -> tuple[object, ...]:
    if len(row) != 7:
        raise LegacyV3PublicationUnavailable
    return row[1:]


def _records_for_active(vault_root: Path, instance_id: str) -> list[dict[str, object]]:
    try:
        directory = receipts._instance_dir(Path(vault_root), instance_id)  # noqa: SLF001
        records, issues = receipts._chain_state(directory)  # noqa: SLF001
    except receipts.ReceiptError:
        raise LegacyV3PublicationUnavailable from None
    if issues:
        raise LegacyV3PublicationUnavailable
    return records


def _prove_receipt_transition(
    vault_root: Path,
    *,
    instance_id: str,
    d0_head: tuple[object, ...],
    d1_head: tuple[object, ...],
    event_id: str,
    expected_outcome: str,
) -> None:
    if receipts.verify_chain(vault_root).get("valid") is not True:
        raise LegacyV3PublicationUnavailable
    records = _records_for_active(vault_root, instance_id)
    intent = [record for record in records if record.get("event_id") == event_id and record.get("phase") == "intent"]
    terminals = [record for record in records if record.get("causation_id") == event_id and record.get("phase") == "committed"]
    competitors = [record for record in records if record.get("causation_id") == event_id and record.get("phase") in {"committed", "aborted"}]
    if len(intent) != 1 or len(terminals) != 1 or len(competitors) != 1:
        raise LegacyV3PublicationUnavailable
    intent_record, terminal = intent[0], terminals[0]
    if (
        terminal.get("outcome") != expected_outcome
        or terminal.get("durable") is not True
        or terminal.get("seq") != intent_record.get("seq", 0) + 1
        or terminal.get("prev") != intent_record.get("hash")
    ):
        raise LegacyV3PublicationUnavailable
    d0 = _head_values(d0_head)
    d1 = _head_values(d1_head)
    if (
        d0[:4] != (intent_record.get("seq"), intent_record.get("hash"), intent_record.get("seq"), intent_record.get("hash"))
        or d1[:4] != (terminal.get("seq"), terminal.get("hash"), terminal.get("seq"), terminal.get("hash"))
        or d1[4] != receipts._relative_locator(Path(vault_root), Path(str(terminal.get("_path", ""))))  # noqa: SLF001
        or d1[5] != terminal.get("_offset")
    ):
        raise LegacyV3PublicationUnavailable


def _normalized_d1_digest(connection: sqlite3.Connection, d0_head: tuple[object, ...]) -> str:
    clone = sqlite3.connect(":memory:")
    try:
        connection.backup(clone)
        clone.execute(
            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=?, path=?, byte_offset=? WHERE instance_id=?",
            (*_head_values(d0_head), str(d0_head[0])),
        )
        clone.commit()
        return _digest(clone)
    except (sqlite3.Error, schema_v4.SchemaV4Error):
        raise LegacyV3PublicationUnavailable from None
    finally:
        clone.close()


def _prove_d1_connections(
    vault_root: Path,
    legacy: sqlite3.Connection,
    external: sqlite3.Connection,
    *,
    event_id: str,
    d0_digest: str,
    expected_outcome: str,
    allow_aligned_legacy: bool,
) -> str:
    expected_d0 = _valid_digest(d0_digest)
    d1_digest = _digest(external)
    legacy_digest = _digest(legacy)
    allowed = {expected_d0, d1_digest} if allow_aligned_legacy else {expected_d0}
    if legacy_digest not in allowed or _receipt_schema(legacy) != _receipt_schema(external):
        raise LegacyV3PublicationUnavailable
    if _rows(legacy, "receipt_instance") != _rows(external, "receipt_instance") or _rows(legacy, "receipt_secrets") != _rows(external, "receipt_secrets"):
        raise LegacyV3PublicationUnavailable
    instance_id, d0_head = _active_head(legacy)
    d1_instance, d1_head = _active_head(external)
    legacy_heads = {row[0]: row for row in _rows(legacy, "receipts_head")}
    external_heads = {row[0]: row for row in _rows(external, "receipts_head")}
    if (
        instance_id != d1_instance
        or set(legacy_heads) != set(external_heads)
        or any(legacy_heads[key] != external_heads[key] for key in legacy_heads if key != instance_id)
    ):
        raise LegacyV3PublicationUnavailable
    if legacy_digest == d1_digest:
        records = _records_for_active(vault_root, instance_id)
        intents = [
            record
            for record in records
            if record.get("event_id") == event_id and record.get("phase") == "intent"
        ]
        if len(intents) != 1 or d0_head != d1_head:
            raise LegacyV3PublicationUnavailable
        intent = intents[0]
        d0_head = (
            instance_id,
            intent.get("seq"),
            intent.get("hash"),
            intent.get("seq"),
            intent.get("hash"),
            receipts._relative_locator(Path(vault_root), Path(str(intent.get("_path", "")))),  # noqa: SLF001
            intent.get("_offset"),
        )
    _prove_receipt_transition(vault_root, instance_id=instance_id, d0_head=d0_head, d1_head=d1_head, event_id=event_id, expected_outcome=expected_outcome)
    if _normalized_d1_digest(external, d0_head) != expected_d0:
        raise LegacyV3PublicationUnavailable
    return d1_digest


def prove_d1_against_legacy(
    vault_root: Path,
    *,
    event_id: str,
    d0_digest: str,
    expected_outcome: str = "schema-v3-restored",
) -> str:
    """Require that D1 differs from immutable D0 only by one exact receipt head."""

    root = Path(vault_root)
    try:
        with _retained_legacy_binding(root, mutate=False) as (
            filesystem,
            parent,
            retained,
        ):
            legacy = sqlite3.connect(f"{legacy_v3_path(root).as_uri()}?mode=ro", uri=True)
            try:
                _revalidate_legacy_binding(filesystem, parent, retained)
                external = sqlite3.connect(f"{store.sidecar_path(root).as_uri()}?mode=ro", uri=True)
                try:
                    result = _prove_d1_connections(
                        root,
                        legacy,
                        external,
                        event_id=event_id,
                        d0_digest=d0_digest,
                        expected_outcome=expected_outcome,
                        allow_aligned_legacy=False,
                    )
                    _revalidate_legacy_binding(filesystem, parent, retained)
                    return result
                finally:
                    external.close()
            finally:
                legacy.close()
    except (OSError, sqlite3.Error, schema_v4.SchemaV4Error):
        raise LegacyV3PublicationUnavailable from None


def _align_legacy_connection(
    vault_root: Path,
    legacy: sqlite3.Connection,
    external: sqlite3.Connection,
    *,
    event_id: str,
    d0_digest: str,
    expected_outcome: str,
    revalidate: Callable[[], None] | None = None,
) -> str:
    try:
        # Opening and reading the schema forces SQLite's hot-journal recovery
        # before the proof observes a D0/D1 endpoint.  Full synchronous mode
        # remains in force through the one permitted legacy mutation.
        legacy.execute("PRAGMA synchronous=FULL")
        legacy.execute("PRAGMA busy_timeout=0")
        legacy.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error:
        raise LegacyV3PublicationUnavailable from None
    d1_digest = _prove_d1_connections(
        vault_root,
        legacy,
        external,
        event_id=event_id,
        d0_digest=d0_digest,
        expected_outcome=expected_outcome,
        allow_aligned_legacy=True,
    )
    if revalidate is not None:
        revalidate()
    if _digest(legacy) == d1_digest:
        if revalidate is not None:
            revalidate()
        return d1_digest
    instance_id, d0_head = _active_head(legacy)
    _d1_instance, d1_head = _active_head(external)
    try:
        legacy.execute("BEGIN IMMEDIATE")
        updated = legacy.execute(
            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=?, path=?, byte_offset=? "
            "WHERE instance_id=? AND durable_seq=? AND durable_hash=? AND observed_seq=? "
            "AND observed_hash=? AND path=? AND byte_offset=?",
            (*_head_values(d1_head), instance_id, *_head_values(d0_head)),
        )
        if updated.rowcount != 1:
            raise LegacyV3PublicationUnavailable
        legacy.commit()
    except (LegacyV3PublicationUnavailable, sqlite3.Error):
        if legacy.in_transaction:
            legacy.rollback()
        raise LegacyV3PublicationUnavailable from None
    if revalidate is not None:
        revalidate()
    if _digest(legacy) != d1_digest:
        raise LegacyV3PublicationUnavailable
    if revalidate is not None:
        revalidate()
    return d1_digest


def align_legacy_to_d1(
    vault_root: Path,
    *,
    event_id: str,
    d0_digest: str,
    expected_outcome: str = "schema-v3-restored",
) -> str:
    """Advance the rollback copy only through the proven six-field receipt endpoint."""

    root = Path(vault_root)
    try:
        with _retained_legacy_binding(root, mutate=True) as (
            filesystem,
            parent,
            retained,
        ):
            legacy = sqlite3.connect(f"{legacy_v3_path(root).as_uri()}?mode=rw", uri=True)
            try:
                _revalidate_legacy_binding(filesystem, parent, retained)
                external = sqlite3.connect(f"{store.sidecar_path(root).as_uri()}?mode=ro", uri=True)
                try:
                    return _align_legacy_connection(
                        root,
                        legacy,
                        external,
                        event_id=event_id,
                        d0_digest=d0_digest,
                        expected_outcome=expected_outcome,
                        revalidate=lambda: _revalidate_legacy_binding(
                            filesystem,
                            parent,
                            retained,
                        ),
                    )
                finally:
                    external.close()
            finally:
                legacy.close()
    except (OSError, sqlite3.Error, schema_v4.SchemaV4Error):
        raise LegacyV3PublicationUnavailable from None
