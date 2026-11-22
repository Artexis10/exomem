"""Replica custody in the media ledger: where each original's replicas are, and how protected it is.

The sidecar binding is the record: `stored_object` and `replicas` (`originals.REPLICAS`) list what the vault holds.
The tables here are derived from those bindings and rebuild from them, so a lost ledger loses no custody fact. The
exceptions are the owner's key-export confirmations, which a lost ledger turns back into "not exported", and the
copy queue, which the owner starts again with `replicate`.

Status reads transactionally maintained counts and the first times in indexed ranges. The census exposes
a rebuild in progress as unknown and records bindings the walk could not read.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import foreground_priority, held_fs, originals, reserved_paths
from .media_jobs import MediaJobStore

if TYPE_CHECKING:
    from . import media_store

log = logging.getLogger(__name__)

#: A replica the vault published after the store confirmed its ciphertext. No later check has changed it.
UPLOAD_VERIFIED = "verified-at-upload"

#: A replica that a check found absent or damaged. Nothing trusts it, and the worker copies the original again.
QUARANTINED = "quarantined"

#: Why a replica's plaintext was quarantined although its ciphertext matched its record: the one finding that a later
#: sample can release (a replica whose ciphertext differs, or is absent, stays until it is copied again).
PLAINTEXT_MISMATCH = "plaintext_mismatch"
#: What an attempt on a replica that is not quarantined found. None is damage, and none quarantines.
UNREADABLE = "binding_unreadable"  # its binding cannot be read now
UNLISTED = "binding_lists_no_replica"  # its binding no longer lists a replica in the store; the next rebuild drops the row
RECORD_INVALID = "record_invalid"  # its binding names a version that the store cannot address
READ_FAILED = "store_read_failed"  # the store did not answer for this replica
#: A key refusal (`media_store.KEY_REFUSED`) is the fifth: a download's ciphertext matched, and the identity refused it.

#: What a check proved, strongest last. Protocol tokens of the owner status, not vault vocabulary.
PRESENCE = "presence"  # the version exists with the recorded size and the digest the store declares
CONTENT = "content"  # the store recomputed the digest of the stored ciphertext
DECRYPTED = "decrypted"  # a download decrypted and its plaintext hashed to the binding

PENDING = "pending"
DONE = "done"
REFUSED = "refused"
#: What a queue row asks for: a first replica in a store, or a new version for a quarantined one.
REPLICATE = "replicate"
RECOPY = "recopy"

#: The least time between two rebuilds of the table from the bindings.
REBUILD_SECONDS = 86400.0
#: Originals one drain takes before it looks at the queue again, and the longest it runs.
_DRAIN_BATCH = 16
_DRAIN_SECONDS = 60.0
#: Failed attempts after which a transient failure is reported as refused; a new `replicate` starts it again.
_MAX_ATTEMPTS = 5
_RETRY_SECONDS = 30.0
_IDLE_SECONDS = 30.0
#: The least time between two observations of whether each store answers as itself.
_OBSERVE_SECONDS = 60.0
# Let a cooperative filesystem hash verify its held witness and return.
_HEAD_RETURN_SECONDS = 0.05
#: A scan writes its queue rows this many at a time.
_SCAN_BATCH = 500
DEFAULT_CHECK_CYCLE_DAYS = 30.0
DEFAULT_CHECK_READ_MIB_PER_SECOND = 32.0
#: The least time between two check rounds of a vault, and the replicas one pick takes. A round takes each store's share
#: of the cycle, a pick at a time (`check`): `replicas x interval / cycle` rows, floored at `_CHECK_FLOOR_ROWS` so a
#: small vault still finishes its cycle, and for a content check `bytes x interval / cycle` bytes.
#: One read cap is shared by every store of a round, so a round runs `_CHECK_ROUND_FACTOR` times the seconds that the cap
#: needs for the byte shares of all its content stores together, never less than `_CHECK_ROUND_SECONDS`. At the sizing
#: target (500,000 replicas and 25 TB in a store, the default 30-day cycle and 32 MiB/s cap) one store's round holds 116
#: replicas and 5.4 GiB: 173 s of reading, so a 216 s round, and 4,320 rounds in the cycle check 501,120 replicas and
#: 25 TB. Two such stores read 10.8 GiB per round: 346 s, so a 432 s round. The sample that follows a round has
#: `_CHECK_ROUND_SECONDS` of its own, and the two fit one interval (the next round starts an interval after this one
#: began), so a round never runs past `_CHECK_ROUND_MAX`: the interval less the sample's share, which reads 15 GiB at the
#: cap. Three such stores need 16.2 GiB: their cycle stretches to about 32 days, and the raised cap that the status's
#: oldest check calls for ends it. The cap is an average over the round: one object is read at the store's speed and the
#: wait for its share follows it, so no read is paced in chunks.
_CHECK_INTERVAL = 600.0
_CHECK_ROUND_SECONDS = 120.0
_CHECK_ROUND_MAX = _CHECK_INTERVAL - _CHECK_ROUND_SECONDS
_CHECK_ROUND_FACTOR = 1.25
_CHECK_BATCH = 64
_CHECK_FLOOR_ROWS = 16
#: How long after a failed attempt on a replica its next turn comes, however long the cycle: a failure that a day does not
#: clear is shown in the status, and one that it does is cleared by that turn.
_FAILED_RETRY = 86400.0
#: Consecutive replicas in one store whose read failed that end the store's round: the store is the likelier cause of
#: the third, and a row that fails alone is recorded and skipped, never retried in the round.
_STORE_FAILURES = 3


#: Columns that a table made by an earlier release lacks. `0` in a time column means never.
_ADDED_COLUMNS = (
    ("size", "INTEGER NOT NULL DEFAULT 0"),  # ciphertext bytes, which the check and sample budgets spend
    ("checked_at", "REAL NOT NULL DEFAULT 0"),  # the last scheduled check, passed or failed
    ("level", "TEXT"),  # what that check proved, or the level that failed for a quarantined row
    ("decrypted_at", "REAL NOT NULL DEFAULT 0"),  # the last sample whose plaintext hashed to the binding
    ("failure", "TEXT"),  # why a quarantined row failed, or what the last attempt on a row that is not quarantined found
    ("sample_month", "INTEGER NOT NULL DEFAULT 0"),  # the UTC month (YYYYMM) in which a sample skipped this row for size
    # The instant from which each schedule counts a replica's cycle: a passed attempt sets it to its time, a failed one to
    # a time that brings the next turn after `_FAILED_RETRY`. It is the order of each schedule, which a proof time cannot be,
    # because a failed attempt proves nothing and must not read as a proof.
    ("check_from", "REAL NOT NULL DEFAULT 0"),
    ("sample_from", "REAL NOT NULL DEFAULT 0"),
    # The digest of the stored ciphertext, so the copies of bytes that decrypt wrong are found by the rows themselves.
    # An earlier release's row has none until a rebuild fills it.
    ("ciphertext_sha256", "TEXT"),
    # Upload plaintext evidence is separate from transfer time. A legacy upload has no such receipt.
    ("plaintext_proved", "INTEGER"),
)
#: A sample cannot read this row because the binding lists no exact plaintext digest or cannot be read.
_UNSAMPLEABLE = -1

# The projection depends on these ledger tables. A dropped source table must never leave readable stale counts.
_STATUS_SOURCES = ("media_replicas", "media_replica_queue", "media_key_exports")
_DECRYPT_PROOFS = "media_replica_decrypt_proofs"
_STATUS_TABLES = ("media_replica_counts", "media_replica_buckets", _DECRYPT_PROOFS)
_STATUS_TRIGGER_EVENTS = ("INSERT", "UPDATE", "DELETE")
_REPLICA_COUNT_TRIGGER = "media_replicas_counts"
_EXPORT_COUNT_TRIGGER = "media_key_exports_count"
_QUARANTINED_VERSION_INDEX = "media_replicas_reproof_version"
_REPLICA_STATUS_TRIGGERS = {
    f"{prefix}_{event.lower()}"
    for prefix in (_REPLICA_COUNT_TRIGGER, "media_replicas_buckets")
    for event in _STATUS_TRIGGER_EVENTS
}
_QUEUE_STATUS_TRIGGERS = {f"media_replica_queue_buckets_{event.lower()}" for event in _STATUS_TRIGGER_EVENTS}
_EXPORT_STATUS_TRIGGERS = {f"{_EXPORT_COUNT_TRIGGER}_{event.lower()}" for event in _STATUS_TRIGGER_EVENTS}
_DECRYPT_COUNT_TRIGGER = "media_replica_decrypt_counts"
_STATUS_REQUIRED_SCHEMA = {
    *_STATUS_SOURCES, *_STATUS_TABLES, *_REPLICA_STATUS_TRIGGERS, *_QUEUE_STATUS_TRIGGERS, *_EXPORT_STATUS_TRIGGERS,
    _DECRYPT_COUNT_TRIGGER,
}


def _status_schema(conn: sqlite3.Connection) -> set[str]:
    # Recreating a lost source leaves its former triggers absent. Check those before trusting its old counts.
    tables = (*_STATUS_SOURCES, *_STATUS_TABLES)
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type IN ('table', 'trigger') "
        f"AND tbl_name IN ({', '.join('?' for _ in tables)})", tables,
    )}


_EXPORT_COVERAGE_KEY = "replica.key_export_coverage"
_EXPORT_EVIDENCE_UNAVAILABLE = "key export evidence unavailable"
_DAMAGE_EVIDENCE_UNAVAILABLE = "damage evidence unavailable"


def _initialize_export_coverage(conn: sqlite3.Connection, existing: set[str]) -> None:
    recorded = conn.execute("SELECT value FROM meta WHERE key = ?", (_EXPORT_COVERAGE_KEY,)).fetchone()
    current = (recorded is not None and recorded[0] == "complete" and "media_key_exports" in existing
               and _EXPORT_STATUS_TRIGGERS.issubset(existing))
    # An intact legacy ledger has receipts. A blank ledger cannot rule out exports, even with no bindings.
    legacy = recorded is None and existing == set(_STATUS_SOURCES)
    coverage = "complete" if current or legacy else "incomplete"
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                 "WHERE meta.value <> excluded.value",
                 (_EXPORT_COVERAGE_KEY, coverage))


def _export_coverage_complete(conn: sqlite3.Connection, schema: set[str]) -> bool:
    if "media_key_exports" not in schema or not _EXPORT_STATUS_TRIGGERS.issubset(schema):
        return False
    coverage = conn.execute("SELECT value FROM meta WHERE key = ?", (_EXPORT_COVERAGE_KEY,)).fetchone()
    return coverage is not None and coverage[0] == "complete"


def _proof_level(row: str = "") -> str:
    return (f"CASE WHEN {row}state = '{QUARANTINED}' THEN NULL "
            f"WHEN {row}decrypted_at > 0 AND {row}decrypted_at >= {row}checked_at THEN '{DECRYPTED}' "
            f"WHEN {row}checked_at > 0 THEN {row}level END")


_PROOF_AT = "MAX(checked_at, decrypted_at)"


def _upload_proof(row: str = "") -> str:
    return f"COALESCE({row}plaintext_proved, 0)"


def _unproved_health(row: str = "") -> str:
    # The counts add exact decrypt proof separately, without updating the filings that benefit from it.
    return f"({row}state <> '{QUARANTINED}' AND {_upload_proof(row)} = 0)"


# These are the ledger fields that identify the exact stored ciphertext and the identity that can decrypt it.
_HEALTH_FIELDS = ("object", "version", "store", "key_version", "size", "ciphertext_sha256")
_HEALTH_COLUMNS = ", ".join(_HEALTH_FIELDS)
_HEALTH_PARAMETERS = "?, ?, ?, ?, ?, ?"
_EXACT_UPLOAD_INDEX = "media_replicas_exact_upload"
_PUBLICATION_TIME_INDEX = "media_replicas_publication_time"
_KNOWN_EXACT_UPLOAD = (f"SELECT 1 FROM media_replicas INDEXED BY {_EXACT_UPLOAD_INDEX} "
                       f"WHERE state <> '{QUARANTINED}' AND ciphertext_sha256 IS NOT NULL AND {_upload_proof()} = 1")


def _health_columns(row: str) -> str:
    return ", ".join(f"{row}{column}" for column in _HEALTH_FIELDS)


def _health_key(replica: originals.StoredObject) -> tuple[str, str, str, str, int, str]:
    return (replica.object, replica.version, replica.store, replica.key_version,
            replica.ciphertext_size, replica.ciphertext_sha256)


# A reused version need not have a timestamp on every filing. Read the latest exact time in index order.
_HEALTH_JSON_COLUMNS = tuple(f"json_extract(wanted.value, '$[{index}]')" for index in range(len(_HEALTH_FIELDS)))
_EVICTION_EVIDENCE = (
    "SELECT MAX(exports.key_version IS NULL), "
    f"MAX(((SELECT verified_at FROM media_replicas INDEXED BY {_PUBLICATION_TIME_INDEX} "
    f"WHERE ({_HEALTH_COLUMNS}) = ({', '.join(_HEALTH_JSON_COLUMNS)}) AND verified_at IS NOT NULL "
    "AND verified_at > 0 ORDER BY verified_at DESC LIMIT 1) <= :settled_before) IS NOT TRUE) "
    "FROM json_each(:replicas) AS wanted LEFT JOIN media_key_exports AS exports "
    f"ON exports.key_version = {_HEALTH_JSON_COLUMNS[_HEALTH_FIELDS.index('key_version')]}"
)
KEY_NOT_EXPORTED = "key_not_exported"
SETTLING = "settling"


def eviction_custody_wait(
    conn: sqlite3.Connection, references: Iterable[originals.StoredObject], *, settled_before: float,
) -> str | None:
    """Return the wait reason if any recovery version lacks its key export or settled publication."""
    missing_key, settling = conn.execute(_EVICTION_EVIDENCE, {
        "replicas": json.dumps([_health_key(reference) for reference in references]),
        "settled_before": settled_before,
    }).fetchone()
    if missing_key:
        return KEY_NOT_EXPORTED
    return SETTLING if settling else None


_PROVED_EXACT_UPLOAD = _KNOWN_EXACT_UPLOAD + f" AND ({_HEALTH_COLUMNS}) = ({_HEALTH_PARAMETERS}) LIMIT 1"
_PROVED_EXACT_DECRYPT = (f"SELECT 1 FROM {_DECRYPT_PROOFS} WHERE sampled = 1 "
                         f"AND ({_HEALTH_COLUMNS}) = ({_HEALTH_PARAMETERS})")
_RECORD_EXACT_DECRYPT = (f"INSERT INTO {_DECRYPT_PROOFS}({_HEALTH_COLUMNS}, n, sampled) "
                         f"VALUES ({_HEALTH_PARAMETERS}, 0, 1) ON CONFLICT({_HEALTH_COLUMNS}) "
                         "DO UPDATE SET sampled = 1 WHERE sampled = 0")


def _restore_upload_proofs(conn: sqlite3.Connection) -> None:
    # Rebuild can recover a second filing of a version whose upload proof survived in a different filing.
    if conn.execute(_KNOWN_EXACT_UPLOAD + " LIMIT 1").fetchone() is not None:
        conn.execute("UPDATE media_replicas AS alias SET plaintext_proved = 1 "
                     f"WHERE {_unproved_health()} AND ciphertext_sha256 IS NOT NULL "
                     f"AND EXISTS ({_KNOWN_EXACT_UPLOAD} "
                     f"AND ({_HEALTH_COLUMNS}) = ({_health_columns('alias.')}))")


def _store_contributions(row: str) -> dict[str, str]:
    """The additive columns of the store count, shared by trigger deltas and the upgrade backfill."""
    return {
        "replicas": "1",
        "bytes": f"{row}size",
        "timed": f"({row}verified_at IS NOT NULL)",
        "verified": f"({row}state = '{UPLOAD_VERIFIED}' AND {_upload_proof(row)} = 1)",
        "quarantined": f"({row}state = '{QUARANTINED}')",
        "unproved": _unproved_health(row),
        **{f"proved_{level}": f"COALESCE(({_proof_level(row)}) = '{level}', 0)"
           for level in (PRESENCE, CONTENT, DECRYPTED)},
        "exported": (f"({row}state <> '{QUARANTINED}' AND NOT {_unproved_health(row)} AND EXISTS ("
                     f"SELECT 1 FROM media_key_exports WHERE key_version = {row}key_version))"),
    }


def _replica_buckets(row: str) -> dict[str, tuple[str, str, str]]:
    """Each sparse count is keyed by a fixed ledger dimension, a key version where relevant, and a token."""
    healthy = f"{row}state <> '{QUARANTINED}'"
    return {
        "key": (f"{row}key_version", "''", healthy),
        "unproved": (f"{row}key_version", "''", _unproved_health(row)),
        "sample": (f"{row}key_version", f"CAST(MAX({row}sample_month, {_UNSAMPLEABLE}) AS TEXT)",
                   f"{healthy} AND {row}sample_month <> 0"),
        "failure": ("''", f"{row}failure", f"{healthy} AND {row}failure IS NOT NULL"),
    }


def _copy_member(row: str = "") -> str:
    # A legacy row gets its membership from its original job kind until a write explicitly preserves membership.
    return f"COALESCE({row}copy_member, {row}kind = '{REPLICATE}')"


def _queue_buckets(row: str) -> dict[str, tuple[str, str, str]]:
    return {"copy": ("''", f"{row}state", _copy_member(row))}


def _bucket_triggers(conn: sqlite3.Connection, table: str,
                     buckets: Callable[[str], dict[str, tuple[str, str, str]]]) -> None:
    """Change only the buckets whose membership changed. Delete empty buckets on the same write."""
    for event in _STATUS_TRIGGER_EVENTS:
        statements = []
        for kind, (key, token, member) in buckets("old.").items():
            new_key, new_token, new_member = buckets("new.")[kind]
            unchanged = (f"old.store = new.store AND {key} IS {new_key} AND {token} IS {new_token} "
                         f"AND ({member}) = ({new_member})")
            if event != "INSERT":
                where = f"store = old.store AND kind = '{kind}' AND key_version = {key} AND token = {token} AND {member}"
                if event == "UPDATE":
                    where += f" AND NOT ({unchanged})"
                statements += [f"UPDATE media_replica_buckets SET n = n - 1 WHERE {where}",
                               f"DELETE FROM media_replica_buckets WHERE {where} AND n = 0"]
            if event != "DELETE":
                where = new_member
                if event == "UPDATE":
                    where += f" AND NOT ({unchanged})"
                statements.append(
                    "INSERT INTO media_replica_buckets(store, kind, key_version, token, n) "
                    f"SELECT new.store, '{kind}', {new_key}, {new_token}, 1 WHERE {where} "
                    "ON CONFLICT(store, kind, key_version, token) DO UPDATE SET n = n + 1"
                )
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_buckets_{event.lower()} AFTER {event} ON {table} "
                     f"BEGIN {'; '.join(statements)}; END")


def _decrypt_membership(row: str) -> str:
    return f"({_unproved_health(row)} AND {row}ciphertext_sha256 IS NOT NULL)"


def _initialize_decrypt_membership(conn: sqlite3.Connection, existing: set[str]) -> None:
    """Count the exact healthy filings that can gain health from a single decrypt receipt."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_DECRYPT_PROOFS} ("
                 "object TEXT NOT NULL, version TEXT NOT NULL, store TEXT NOT NULL, key_version TEXT NOT NULL, "
                 "size INTEGER NOT NULL, ciphertext_sha256 TEXT NOT NULL, n INTEGER NOT NULL, "
                 f"sampled INTEGER NOT NULL DEFAULT 0, PRIMARY KEY ({_HEALTH_COLUMNS})) WITHOUT ROWID")
    if {_DECRYPT_PROOFS, _DECRYPT_COUNT_TRIGGER, *_REPLICA_STATUS_TRIGGERS} <= existing:
        return
    # A lost replica table cannot attest its former membership. A surviving exact proof can serve rows recovered later.
    conn.execute(f"UPDATE {_DECRYPT_PROOFS} SET n = 0")
    conn.execute(f"DELETE FROM {_DECRYPT_PROOFS} WHERE sampled = 0")
    conn.execute(f"INSERT INTO {_DECRYPT_PROOFS}({_HEALTH_COLUMNS}, n, sampled) "
                 f"SELECT {_HEALTH_COLUMNS}, SUM({_decrypt_membership('')}), MAX(decrypted_at > 0) FROM media_replicas "
                 f"WHERE state <> '{QUARANTINED}' AND ciphertext_sha256 IS NOT NULL "
                 f"AND ({_upload_proof()} = 0 OR decrypted_at > 0) GROUP BY {_HEALTH_COLUMNS} "
                 f"ON CONFLICT({_HEALTH_COLUMNS}) DO UPDATE SET n = excluded.n, sampled = MAX(sampled, excluded.sampled)")


def _decrypt_membership_delta(event: str, changed: str) -> list[str]:
    restrict = f" AND ({changed})" if event == "UPDATE" else ""
    statements = []
    if event != "INSERT":
        where = f"({_HEALTH_COLUMNS}) = ({_health_columns('old.')}) AND {_decrypt_membership('old.')}{restrict}"
        statements += [f"UPDATE {_DECRYPT_PROOFS} SET n = n - 1 WHERE {where}",
                       f"DELETE FROM {_DECRYPT_PROOFS} WHERE {where} AND n = 0 AND sampled = 0"]
    if event != "DELETE":
        statements.append(f"INSERT INTO {_DECRYPT_PROOFS}({_HEALTH_COLUMNS}, n) "
                          f"SELECT {_health_columns('new.')}, 1 WHERE {_decrypt_membership('new.')}{restrict} "
                          f"ON CONFLICT({_HEALTH_COLUMNS}) DO UPDATE SET n = n + 1")
    return statements


def _initialize_status(conn: sqlite3.Connection, existing: set[str]) -> None:
    """Install the status projection and backfill once, within ledger schema initialization."""
    current = _STATUS_REQUIRED_SCHEMA <= existing
    if _DECRYPT_COUNT_TRIGGER not in existing:
        for trigger in _REPLICA_STATUS_TRIGGERS | _EXPORT_STATUS_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    if not current:
        conn.execute(f"DROP TRIGGER IF EXISTS {_DECRYPT_COUNT_TRIGGER}")
    old = _store_contributions("old.")
    new = _store_contributions("new.")
    columns = ", ".join(new)
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_counts (store TEXT PRIMARY KEY, "
                 + ", ".join(f"{column} INTEGER NOT NULL" for column in new) + ") WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_buckets (store TEXT NOT NULL, kind TEXT NOT NULL, "
                 "key_version TEXT NOT NULL, token TEXT NOT NULL, n INTEGER NOT NULL, "
                 "PRIMARY KEY (store, kind, key_version, token)) WITHOUT ROWID")
    _initialize_decrypt_membership(conn, existing)
    # Move one exact group's effective health by its current membership. Row counters record upload proof alone.
    delta = "(new.n * new.sampled - old.n * old.sampled)"
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_DECRYPT_COUNT_TRIGGER} AFTER UPDATE ON {_DECRYPT_PROOFS} "
                 f"WHEN {delta} <> 0 BEGIN "
                 f"UPDATE media_replica_counts SET unproved = unproved - {delta}, exported = exported + {delta} * "
                 "EXISTS (SELECT 1 FROM media_key_exports WHERE key_version = new.key_version) WHERE store = new.store; "
                 "INSERT INTO media_replica_buckets(store, kind, key_version, token, n) "
                 f"VALUES (new.store, 'decrypted', new.key_version, '', {delta}) "
                 "ON CONFLICT(store, kind, key_version, token) DO UPDATE SET n = n + excluded.n; "
                 "DELETE FROM media_replica_buckets WHERE (store, kind, key_version, token) = "
                 "(new.store, 'decrypted', new.key_version, '') AND n = 0; END")
    # A key-export receipt affects only the stores that hold healthy replicas of that key.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replica_bucket_keys ON media_replica_buckets(key_version, store) "
                 "WHERE kind = 'key'")
    conn.execute("DROP INDEX IF EXISTS media_replicas_status")
    conn.execute("DROP INDEX IF EXISTS media_replicas_failing")
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_upload_time ON media_replicas(store, verified_at) "
                 "WHERE verified_at IS NOT NULL")
    proof = _proof_level()
    conn.execute(f"CREATE INDEX IF NOT EXISTS media_replicas_proof_time ON media_replicas(store, {proof}, {_PROOF_AT}) "
                 f"WHERE ({proof}) IS NOT NULL")
    old_member, new_member = _decrypt_membership("old."), _decrypt_membership("new.")
    membership_changed = (f"{old_member} <> {new_member} OR "
                          f"({old_member} AND ({_health_columns('old.')}) IS NOT ({_health_columns('new.')}))")
    for event in _STATUS_TRIGGER_EVENTS:
        # Check the projected values first. A rebuild's unchanged row and a retry's schedule-only write do no work here.
        changed = " OR ".join(f"{old[column]} <> {new[column]}" for column in new)
        when = f"WHEN old.store <> new.store OR {changed} OR {membership_changed} " if event == "UPDATE" else ""
        statements = []
        if event != "INSERT":
            subtract = ", ".join(f"{column} = {column} - {value}" for column, value in old.items())
            statements.append(f"UPDATE media_replica_counts SET {subtract} WHERE store = old.store")
        if event != "DELETE":
            add = ", ".join(f"{column} = {column} + excluded.{column}" for column in new)
            statements.append(
                f"INSERT INTO media_replica_counts(store, {columns}) VALUES (new.store, {', '.join(new.values())}) "
                f"ON CONFLICT(store) DO UPDATE SET {add}"
            )
        # The first restored filing can join a surviving decrypt receipt. Its store count must already exist.
        statements.extend(_decrypt_membership_delta(event, membership_changed))
        if event != "INSERT":
            statements.append("DELETE FROM media_replica_counts WHERE store = old.store AND replicas = 0")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_REPLICA_COUNT_TRIGGER}_{event.lower()} AFTER {event} ON media_replicas "
                     f"{when}BEGIN {'; '.join(statements)}; END")
    _bucket_triggers(conn, "media_replicas", _replica_buckets)
    _bucket_triggers(conn, "media_replica_queue", _queue_buckets)

    def receipt_delta(row: str, sign: str) -> str:
        return (f"UPDATE media_replica_counts SET exported = exported {sign} (b.n - COALESCE(u.n, 0) + COALESCE(d.n, 0)) "
                "FROM media_replica_buckets AS b LEFT JOIN media_replica_buckets AS u "
                "ON (u.store, u.kind, u.key_version, u.token) = (b.store, 'unproved', b.key_version, '') "
                "LEFT JOIN media_replica_buckets AS d "
                "ON (d.store, d.kind, d.key_version, d.token) = (b.store, 'decrypted', b.key_version, '') "
                f"WHERE media_replica_counts.store = b.store AND b.kind = 'key' AND b.key_version = {row}.key_version")

    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_EXPORT_COUNT_TRIGGER}_insert AFTER INSERT ON media_key_exports "
                 f"BEGIN {receipt_delta('new', '+')}; END")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_EXPORT_COUNT_TRIGGER}_delete AFTER DELETE ON media_key_exports "
                 f"BEGIN {receipt_delta('old', '-')}; END")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_EXPORT_COUNT_TRIGGER}_update AFTER UPDATE OF key_version "
                 f"ON media_key_exports WHEN old.key_version <> new.key_version BEGIN "
                 f"{receipt_delta('old', '-')}; {receipt_delta('new', '+')}; END")

    if current:
        return
    conn.execute("DELETE FROM media_replica_counts")
    conn.execute("DELETE FROM media_replica_buckets")
    sums = ", ".join(f"SUM({value})" for value in _store_contributions("r.").values())
    conn.execute(f"INSERT INTO media_replica_counts(store, {columns}) "
                 f"SELECT r.store, {sums} FROM media_replicas AS r GROUP BY r.store")
    for table, buckets in (("media_replicas", _replica_buckets("r.")), ("media_replica_queue", _queue_buckets("r."))):
        for kind, (key, token, member) in buckets.items():
            conn.execute("INSERT INTO media_replica_buckets(store, kind, key_version, token, n) "
                         f"SELECT r.store, '{kind}', {key}, {token}, COUNT(*) FROM {table} AS r WHERE {member} "
                         "GROUP BY 1, 3, 4")
    conn.execute("INSERT INTO media_replica_buckets(store, kind, key_version, token, n) "
                 f"SELECT store, 'decrypted', key_version, '', SUM(n) FROM {_DECRYPT_PROOFS} "
                 "WHERE sampled = 1 AND n <> 0 GROUP BY store, key_version")
    conn.execute("UPDATE media_replica_counts SET unproved = unproved - proved.n, exported = exported + proved.exported_n "
                 "FROM (SELECT b.store, SUM(b.n) AS n, SUM(b.n * (e.key_version IS NOT NULL)) AS exported_n "
                 "FROM media_replica_buckets AS b LEFT JOIN media_key_exports AS e ON e.key_version = b.key_version "
                 "WHERE b.kind = 'decrypted' GROUP BY b.store) AS proved WHERE media_replica_counts.store = proved.store")


def initialize(conn: sqlite3.Connection) -> None:
    """Create the replica tables. A table created fresh clears the census, so its first rows read as unknown."""
    existing = _status_schema(conn)
    recreated = bool(existing.intersection(_STATUS_TABLES)) and not _REPLICA_STATUS_TRIGGERS.issubset(existing)
    _initialize_export_coverage(conn, existing)
    conn.execute("CREATE TABLE IF NOT EXISTS media_replicas ("
                 "binary_rel TEXT NOT NULL, store TEXT NOT NULL, object TEXT NOT NULL, version TEXT NOT NULL, "
                 "key_version TEXT NOT NULL, verified_at REAL, state TEXT NOT NULL, seen INTEGER NOT NULL, "
                 + ", ".join(f"{name} {kind}" for name, kind in _ADDED_COLUMNS)
                 + ", PRIMARY KEY (binary_rel, store)) WITHOUT ROWID")
    have = {row[1] for row in conn.execute("PRAGMA table_info(media_replicas)")}
    healed = [(name, kind) for name, kind in _ADDED_COLUMNS if name not in have]
    for name, kind in healed:
        conn.execute(f"ALTER TABLE media_replicas ADD COLUMN {name} {kind}")
    # A check round takes the replicas checked longest ago first, and a sample those proved by decryption longest ago
    # first, each as one range read in index order with no sort. The literal in the partial index is the one the
    # queries below repeat: SQLite uses a partial index only when the query states the same condition.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_due ON media_replicas(store, check_from) "
                 f"WHERE state <> '{QUARANTINED}'")
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_sample ON media_replicas("
                 "store, key_version, sample_month, sample_from) "
                 f"WHERE state <> '{QUARANTINED}'")
    # The quarantined rows that a sample may release: the plaintext differed from the binding while the ciphertext
    # matched its record, which a later download can find was a false alarm. Oldest finding first, per key version.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_reproof ON media_replicas(store, key_version, sample_from) "
                 f"WHERE state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}'")
    # The quarantined rows, which the findings list reads in path order.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_findings ON media_replicas(binary_rel, store) "
                 f"WHERE state = '{QUARANTINED}'")
    # The filings that list one stored version and are not quarantined, which a finding quarantines together
    # (`_quarantine_listing`). A quarantined filing is not in it, so a version that every filing already holds as damaged
    # costs nothing to find again.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_held ON media_replicas(object, version) "
                 f"WHERE state <> '{QUARANTINED}'")
    conn.execute("DROP INDEX IF EXISTS media_replicas_exact_health")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_EXACT_UPLOAD_INDEX} ON media_replicas({_HEALTH_COLUMNS}) "
                 f"WHERE state <> '{QUARANTINED}' AND ciphertext_sha256 IS NOT NULL AND {_upload_proof()} = 1")
    # One seek to a timed witness, even when many aliases record the same version with no publication time.
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_PUBLICATION_TIME_INDEX} "
                 f"ON media_replicas({_HEALTH_COLUMNS}, verified_at) WHERE verified_at IS NOT NULL")
    # The filings of one ciphertext, under any name, that are not quarantined: the ones a wrong plaintext of it
    # quarantines together (`_inherit`, `_quarantine_listing`). A finding that repeats reads none that it handled before,
    # however many filings hold the ciphertext. A release finds the quarantined ones through the records (`_released`).
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_digest ON media_replicas(ciphertext_sha256) "
                 f"WHERE state <> '{QUARANTINED}'")
    # The quarantined filings of one stored version, which a change of the version's cause moves together
    # (`_record_damage`) and a release reads for the filings that a wrong plaintext holds (`_released`). Each statement
    # states the cause it wants: a version has few filings, so it reads only them, however many other replicas are
    # quarantined.
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_QUARANTINED_VERSION_INDEX} ON media_replicas(object, version, store) "
                 f"WHERE state = '{QUARANTINED}'")
    # A stored version that a check, a sample or eviction found damaged, whichever filings list it: what eviction asks
    # before it drops an original's local bytes. Damage is a fact of the version, so it outlives any filing's row: a row
    # that a rebuild writes late, or that a lost write never made, is gated by it and quarantined by `rebuild`.
    # Filings' rows and requests are projections of this record.
    # `ciphertext_sha256` is set when a finding is a wrong plaintext: those bytes decrypt wrong under any name, so each
    # copy of them is as wrong, and the digest finds the copies. A later finding of another cause on the version leaves
    # it, and only the release that proves the bytes good clears it.
    conn.execute("CREATE TABLE IF NOT EXISTS media_damaged_versions ("
                 "store TEXT NOT NULL, object TEXT NOT NULL, version TEXT NOT NULL, level TEXT NOT NULL, "
                 "failure TEXT NOT NULL, repair_via TEXT, ciphertext_sha256 TEXT, "
                 "PRIMARY KEY (store, object, version)) WITHOUT ROWID")
    conn.execute("CREATE INDEX IF NOT EXISTS media_damaged_versions_ciphertext "
                 "ON media_damaged_versions(ciphertext_sha256)")
    # A release proves a ciphertext and so acts on every filing and record of it, under any name: its authority is the
    # ciphertext's, not one version's. Every finding of a wrong plaintext, on any version of the digest, renews this
    # generation, so a release that read an earlier one commits nothing. A row exists while a record carries the digest.
    conn.execute("CREATE TABLE IF NOT EXISTS media_damaged_digests ("
                 "ciphertext_sha256 TEXT PRIMARY KEY, generation INTEGER NOT NULL) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_census ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), rebuilt_at REAL NOT NULL, unread INTEGER NOT NULL)")
    # Every rebuild takes the next number. A row carries the number of the rebuild that last confirmed it, or of the
    # one running when a publication wrote it, so "stale" never depends on a wall clock that can step backwards.
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_generation ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO media_replica_generation(id, value) VALUES (1, 0)")
    needs_rebuild = "media_replicas" not in existing or any(name != "plaintext_proved" for name, _ in healed) or recreated
    if needs_rebuild:
        # Lost rows or newly added fields need a fresh walk. A walk that started before the reset cannot certify it.
        conn.execute("DELETE FROM media_replica_census")
        conn.execute("UPDATE media_replica_generation SET value = value + 1 WHERE id = 1")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_demands ("
                 "store TEXT PRIMARY KEY, requested_at REAL NOT NULL, scanned_at REAL, scan_token INTEGER NOT NULL DEFAULT 0)")
    # A lost queue invalidates earlier scans, including a scan whose final batch is already in memory.
    if "scan_token" not in {row[1] for row in conn.execute("PRAGMA table_info(media_replica_demands)")}:
        conn.execute("ALTER TABLE media_replica_demands ADD COLUMN scan_token INTEGER NOT NULL DEFAULT 0")
    queue_lost = ("media_replica_queue" not in existing
                  or bool(existing.intersection(_STATUS_TABLES)) and not _QUEUE_STATUS_TRIGGERS.issubset(existing))
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_queue ("
                 "store TEXT NOT NULL, binary_rel TEXT NOT NULL, state TEXT NOT NULL, code TEXT, "
                 "attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, "
                 f"kind TEXT NOT NULL DEFAULT '{REPLICATE}', token INTEGER NOT NULL DEFAULT 0, copy_member INTEGER, "
                 "PRIMARY KEY (store, binary_rel)) WITHOUT ROWID")
    queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(media_replica_queue)")}
    if "kind" not in queue_columns:
        conn.execute(f"ALTER TABLE media_replica_queue ADD COLUMN kind TEXT NOT NULL DEFAULT '{REPLICATE}'")
    # Every write that makes a request pending gives it a new random `token`: a settlement matches the token it
    # picked, so it never closes or overwrites a request made after it, even one that replaced a deleted row.
    if "token" not in queue_columns:
        conn.execute("ALTER TABLE media_replica_queue ADD COLUMN token INTEGER NOT NULL DEFAULT 0")
    if "copy_member" not in queue_columns:
        # The legacy counts already describe the same membership. Replace their triggers during the schema transaction.
        conn.execute("ALTER TABLE media_replica_queue ADD COLUMN copy_member INTEGER")
        for trigger in _QUEUE_STATUS_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    # The drain's pick is `ORDER BY store, binary_rel` among the pending rows, so this order reads it with no sort;
    # `retry_at` only filters.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replica_queue_pick ON media_replica_queue(state, store, binary_rel)")
    # What the worker last saw of each store that has a cheap check, so a status never reads a store.
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_reach ("
                 "store TEXT PRIMARY KEY, reachable INTEGER NOT NULL, observed_at REAL NOT NULL)")
    # The ciphertext bytes that sampled checks downloaded from each store in each UTC calendar month (YYYYMM).
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_samples ("
                 "store TEXT NOT NULL, month INTEGER NOT NULL, bytes INTEGER NOT NULL, "
                 "PRIMARY KEY (store, month)) WITHOUT ROWID")
    # The random id that the owner's declaration recorded for each filesystem store's marker.
    conn.execute("CREATE TABLE IF NOT EXISTS media_store_ids (store TEXT PRIMARY KEY, store_id TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_key_exports ("
                 "key_version TEXT PRIMARY KEY, confirmed_at REAL NOT NULL)")
    _initialize_status(conn, existing)
    if _restore_damage(conn):
        conn.execute(_INHERIT_ALL)
        _quarantine_listing(conn, time.time())
    if queue_lost:
        # A surviving demand can safely rescan current bindings. Lost rows cannot say who belonged to its former scan.
        conn.execute("UPDATE media_replica_demands SET scanned_at = NULL, scan_token = random()")
        conn.execute("INSERT INTO media_replica_queue(store, binary_rel, state, kind, token) "
                     f"SELECT store, binary_rel, '{PENDING}', '{RECOPY}', random() FROM media_replicas "
                     f"WHERE state = '{QUARANTINED}' ON CONFLICT(store, binary_rel) DO UPDATE SET {_RENEW} "
                     f"WHERE media_replica_queue.kind <> '{RECOPY}'")


def _restore_damage(conn: sqlite3.Connection) -> bool:
    """Recover missing authority from surviving quarantines, without changing any surviving version record."""
    # A lost table may already have been recreated by another runtime. Use the partial index of quarantines on
    # schema initialization to recover missing rows in either case. No healthy filing participates in recovery.
    linked = conn.execute(
        "UPDATE media_damaged_versions AS d SET ciphertext_sha256 = links.digest FROM ("
        "SELECT r.store, r.object, r.version, MIN(r.ciphertext_sha256) AS digest FROM media_replicas AS r "
        "JOIN media_damaged_versions AS missing ON (r.store, r.object, r.version) = "
        "(missing.store, missing.object, missing.version) "
        f"WHERE r.state = '{QUARANTINED}' AND r.failure = '{PLAINTEXT_MISMATCH}' "
        f"AND missing.failure = '{PLAINTEXT_MISMATCH}' AND missing.ciphertext_sha256 IS NULL "
        "GROUP BY r.store, r.object, r.version HAVING COUNT(DISTINCT r.ciphertext_sha256) = 1"
        ") AS links WHERE (d.store, d.object, d.version) = (links.store, links.object, links.version)"
    ).rowcount
    digests = conn.execute(
        "INSERT INTO media_damaged_digests(ciphertext_sha256, generation) SELECT digest, random() FROM ("
        "SELECT ciphertext_sha256 AS digest FROM media_damaged_versions WHERE ciphertext_sha256 IS NOT NULL UNION "
        f"SELECT ciphertext_sha256 FROM media_replicas WHERE state = '{QUARANTINED}' "
        f"AND failure = '{PLAINTEXT_MISMATCH}' AND ciphertext_sha256 IS NOT NULL) WHERE true "
        "ON CONFLICT(ciphertext_sha256) DO NOTHING"
    ).rowcount
    versions = conn.execute(
        "INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256) "
        "SELECT store, object, version, level, failure, proved_digest FROM ("
        "SELECT r.store, r.object, r.version, r.level, r.failure, d.ciphertext_sha256 AS proved_digest, "
        "ROW_NUMBER() OVER (PARTITION BY r.store, r.object, r.version "
        f"ORDER BY (r.failure = '{PLAINTEXT_MISMATCH}'), r.checked_at DESC, r.binary_rel) AS rank "
        "FROM media_replicas AS r LEFT JOIN media_damaged_digests AS d ON d.ciphertext_sha256 = r.ciphertext_sha256 "
        f"WHERE r.state = '{QUARANTINED}' AND r.level IS NOT NULL AND r.failure IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM media_damaged_versions AS old "
        "WHERE (old.store, old.object, old.version) = (r.store, r.object, r.version))) WHERE rank = 1"
    ).rowcount
    if versions:
        # A sample can clear plaintext evidence. The record of an absent or differing version must still gate its filings.
        conn.execute(
            "UPDATE media_replicas AS r SET (level, failure) = (d.level, d.failure) FROM media_damaged_versions AS d "
            f"WHERE r.state = '{QUARANTINED}' AND (d.store, d.object, d.version) = (r.store, r.object, r.version) "
            "AND (r.level, r.failure) IS NOT (d.level, d.failure)"
        )
    return bool(linked or digests or versions)


def _iso(value: float | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


# --- what a publication records ---------------------------------------------------------------------


def _upsert(seen: str, plaintext_proved: str) -> str:
    """The statement that writes one row; `seen` is the SQL that gives the row's generation.

    A row for the same object and version keeps its upload time and its check results, a quarantine included. A new
    version is a new replica: it starts unchecked and unquarantined.
    """
    unchanged = ("media_replicas.object = excluded.object AND media_replicas.version = excluded.version "
                 "AND media_replicas.key_version = excluded.key_version "
                 "AND (media_replicas.size = 0 OR media_replicas.size = excluded.size) "
                 "AND (media_replicas.ciphertext_sha256 IS NULL "
                 "OR media_replicas.ciphertext_sha256 = excluded.ciphertext_sha256)")

    def keep(column: str, fresh: str) -> str:
        return f"{column} = CASE WHEN {unchanged} THEN media_replicas.{column} ELSE {fresh} END"

    return ("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, state, seen, "
            f"ciphertext_sha256, plaintext_proved) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {seen}, ?, {plaintext_proved}) "
            "ON CONFLICT(binary_rel, store) DO UPDATE SET "
            "object = excluded.object, version = excluded.version, key_version = excluded.key_version, "
            "size = excluded.size, seen = excluded.seen, ciphertext_sha256 = excluded.ciphertext_sha256, "
            f"verified_at = CASE WHEN {unchanged} THEN COALESCE(excluded.verified_at, media_replicas.verified_at) "
            "ELSE excluded.verified_at END, "
            f"plaintext_proved = CASE WHEN {unchanged} THEN MAX(COALESCE(excluded.plaintext_proved, 0), "
            f"{_upload_proof('media_replicas.')}) "
            "ELSE excluded.plaintext_proved END, "
            + keep("state", "excluded.state") + ", " + keep("checked_at", "0") + ", " + keep("level", "NULL") + ", "
            + keep("decrypted_at", "0") + ", " + keep("failure", "NULL") + ", " + keep("sample_month", "0") + ", "
            + keep("check_from", "0") + ", " + keep("sample_from", "0"))


#: A publication reads the generation in the same statement as its write, so a rebuild that starts in between cannot
#: strand the row below its own generation.
_UPSERT_PUBLISHED = _upsert("(SELECT value FROM media_replica_generation WHERE id = 1)", "?")
_UPSERT_REBUILT = _upsert("?", "NULL") + " WHERE media_replicas.seen < excluded.seen"


def record(vault: Path, binary_rel: str, replica: originals.StoredObject, *, verified_at: float | None,
           copied_from: originals.StoredObject | None = None) -> None:
    """Note a replica that a binding now lists. `verified_at` is its upload time; None when this write does not know it.

    A ciphertext upload records its exact source in `copied_from`, so its upload time only proves the new transfer.
    A version that a finding recorded as damaged stays damaged for a filing that lists it later: the row is written
    quarantined with its request, in the same transaction, so a publication that raced the finding is never trusted.
    So is a copy of ciphertext that a finding recorded as decrypting wrong, under whatever name it was written
    (`_inherit`): a finding that lands before this transaction reaches the new version here, and one after it reaches
    the row through its digest (`quarantine_all`).
    """
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = _record_replica(conn, binary_rel, replica, verified_at=verified_at, copied_from=copied_from)
    if changed:
        wake()


def _record_replica(conn: sqlite3.Connection, binary_rel: str, replica: originals.StoredObject, *,
                    verified_at: float | None, copied_from: originals.StoredObject | None = None) -> bool:
    # Only a fresh encryption checked the local plaintext. A ciphertext copy carries its exact source's evidence.
    exact_source = _health_key(copied_from or replica)
    proved = ((verified_at is not None and copied_from is None)
              or conn.execute(_PROVED_EXACT_UPLOAD, exact_source).fetchone() is not None
              or (copied_from is not None and conn.execute(_PROVED_EXACT_DECRYPT, exact_source).fetchone() is not None))
    conn.execute(_UPSERT_PUBLISHED, (binary_rel, replica.store, replica.object, replica.version,
                                     replica.key_version, replica.ciphertext_size, verified_at, UPLOAD_VERIFIED,
                                     replica.ciphertext_sha256, proved))
    conn.execute(_INHERIT_OF, {"digest": replica.ciphertext_sha256})
    return _quarantine_listing(conn, time.time(), replica, binary_rel=binary_rel)


def quarantine(vault: Path, binary_rel: str, replica: originals.StoredObject, *, level: str, failure: str) -> None:
    """Mark `replica` quarantined: the `level` of the check that failed, and why. Nothing trusts it from now on."""
    quarantine_all(vault, [binary_rel], replica, level=level, failure=failure)


#: A request that is made again: pending, with no attempt spent and a new token.
_RENEW = (f"copy_member = ({_copy_member('media_replica_queue.')} AND media_replica_queue.state = '{PENDING}'), "
          "state = excluded.state, kind = excluded.kind, code = NULL, attempts = 0, retry_at = 0, token = excluded.token")


def _record_damage(conn: sqlite3.Connection, replica: originals.StoredObject, level: str,
                   failure: str) -> tuple[str, str]:
    """Record the stored version as damaged, or renew its record.

    A confirmed version failure survives a finding about the plaintext. The digest still records a plaintext
    finding and renews its generation. The caller holds the write lock while the evidence is read and updated.
    """
    before = conn.execute("SELECT level, failure, ciphertext_sha256 FROM media_damaged_versions WHERE store = ? "
                          "AND object = ? AND version = ?", (replica.store, replica.object, replica.version)).fetchone()
    digest = replica.ciphertext_sha256 if failure == PLAINTEXT_MISMATCH else None
    cause = (level, failure)
    if digest is not None and before is not None and before[1] != PLAINTEXT_MISMATCH:
        cause = tuple(before[:2])
    conn.execute("INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(store, object, version) DO UPDATE SET "
                 "level = excluded.level, failure = excluded.failure, "
                 "ciphertext_sha256 = COALESCE(excluded.ciphertext_sha256, media_damaged_versions.ciphertext_sha256)",
                 (replica.store, replica.object, replica.version, *cause, digest))
    if before is None or tuple(before[:2]) != cause or (digest is not None and before[2] is None):
        conn.execute("UPDATE media_replicas SET level = ?, failure = ?, "
                     "ciphertext_sha256 = COALESCE(ciphertext_sha256, ?) WHERE object = ? AND version = ? AND store = ? "
                     f"AND state = '{QUARANTINED}' AND (level IS NOT ? OR failure IS NOT ? "
                     "OR (ciphertext_sha256 IS NULL AND ? IS NOT NULL))",
                     (*cause, digest, replica.object, replica.version, replica.store, *cause, digest))
    if digest is not None:
        conn.execute("INSERT INTO media_damaged_digests(ciphertext_sha256, generation) VALUES (?, random()) "
                     "ON CONFLICT(ciphertext_sha256) DO UPDATE SET generation = excluded.generation", (digest,))
    return cause


def quarantine_all(vault: Path, binary_rels: Sequence[str], replica: originals.StoredObject, *, level: str,
                   failure: str) -> None:
    """Mark `replica` quarantined for each of these originals, in one transaction, and record the version as damaged.

    The finding is about the stored version, so it also quarantines every other filing that lists it: filings of equal
    bytes share one version, and each needs its own copy again. A row is written first when the table does not know
    the store's replica (a publication whose write was lost), so a finding is never dropped for want of a row. A row that
    names another version is left alone: it is a newer replica, not the one that failed. The same write queues the new
    copy that the worker makes.
    """
    now, changed = time.time(), False
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")  # a read does not begin a transaction: `_record_damage` reads, then writes, under it
        cause = _record_damage(conn, replica, level, failure)
        digest = replica.ciphertext_sha256 if failure == PLAINTEXT_MISMATCH else None
        for binary_rel in binary_rels:
            conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, "
                         "state, seen, ciphertext_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, (SELECT value FROM "
                         "media_replica_generation WHERE id = 1), ?) ON CONFLICT(binary_rel, store) DO NOTHING",
                         (binary_rel, replica.store, replica.object, replica.version, replica.key_version,
                          replica.ciphertext_size, UPLOAD_VERIFIED, replica.ciphertext_sha256))
            if digest is not None and cause != (level, failure):
                conn.execute("UPDATE media_replicas SET ciphertext_sha256 = ? WHERE binary_rel = ? AND store = ? "
                             "AND object = ? AND version = ? AND ciphertext_sha256 IS NULL",
                             (digest, binary_rel, replica.store, replica.object, replica.version))
            if conn.execute(
                    "UPDATE media_replicas SET state = ?, level = ?, failure = ?, checked_at = ?, sample_from = ?, "
                    "ciphertext_sha256 = COALESCE(ciphertext_sha256, ?) "
                    "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                    "AND (state <> ? OR ?)",
                    (QUARANTINED, *cause, now, now, digest, binary_rel, replica.store, replica.object,
                     replica.version, QUARANTINED, cause == (level, failure))).rowcount:
                conn.execute("INSERT INTO media_replica_queue(store, binary_rel, state, kind, token) "
                             "VALUES (?, ?, ?, ?, random()) "
                             f"ON CONFLICT(store, binary_rel) DO UPDATE SET {_RENEW}",
                             (replica.store, binary_rel, PENDING, RECOPY))
                changed = True
        if failure == PLAINTEXT_MISMATCH:
            conn.execute(_INHERIT_OF, {"digest": replica.ciphertext_sha256})
            changed |= _quarantine_listing(conn, now, digest=replica.ciphertext_sha256)
        # The filings of the version, whatever cause: a row of an earlier release has no digest for the fan-out above.
        changed |= _quarantine_listing(conn, now, replica)
    if changed:
        wake()


def _quarantine_listing(conn: sqlite3.Connection, now: float, only: originals.StoredObject | None = None, *,
                        binary_rel: str | None = None, digest: str | None = None) -> bool:
    """Quarantine each filing that lists a damaged version and is not quarantined yet, and queue its copy again.

    The version is `only`, the filings of the ciphertext `digest`, or any version in `media_damaged_versions`;
    `binary_rel` narrows `only` to one filing's row, which a publication reads by its primary key. Two statements whose
    reads are indexed: the filings of one version in `media_replicas_held`, those of one ciphertext in
    `media_replicas_digest` (each states its index's condition, so a filing that is quarantined is never read), and, for
    every version, the damaged table. A filing that is already quarantined keeps its own request. Returns whether a
    filing changed.
    """
    of_version = ("FROM media_damaged_versions AS d WHERE d.object = media_replicas.object "
                  "AND d.version = media_replicas.version AND d.store = media_replicas.store")

    def found(column: str) -> str:
        return f"(SELECT d.{column} {of_version})"

    parameters: tuple[str, ...] = ()
    if digest is not None:
        damaged, parameters = f"ciphertext_sha256 = ? AND EXISTS (SELECT 1 {of_version})", (digest,)
    else:
        version, row = "", ""
        if only is not None:
            version, parameters = " WHERE object = ? AND version = ? AND store = ?", (only.object, only.version, only.store)
            if binary_rel is not None:
                row, parameters = " AND binary_rel = ? AND store = ?", (*parameters, binary_rel, only.store)
        damaged = f"(object, version, store) IN (SELECT object, version, store FROM media_damaged_versions{version}){row}"

    conn.execute("INSERT INTO media_replica_queue(store, binary_rel, state, kind, token) "
                 f"SELECT store, binary_rel, ?, ?, random() FROM media_replicas WHERE state <> '{QUARANTINED}' "
                 f"AND {damaged} ON CONFLICT(store, binary_rel) DO UPDATE SET {_RENEW}", (PENDING, RECOPY, *parameters))
    return bool(conn.execute(
        f"UPDATE media_replicas SET state = '{QUARANTINED}', level = {found('level')}, failure = {found('failure')}, "
        f"checked_at = ?, sample_from = ?, ciphertext_sha256 = COALESCE(ciphertext_sha256, {found('ciphertext_sha256')}) "
        f"WHERE state <> '{QUARANTINED}' AND {damaged}",
        (now, now, *parameters)).rowcount)


def note_repair(vault: Path, owner: str, replicas: Iterable[originals.StoredObject]) -> None:
    """Name `owner` as the original that published a new version for each of these damaged versions."""
    triples = json.dumps(sorted({(replica.store, replica.object, replica.version) for replica in replicas}))
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_damaged_versions SET repair_via = ? WHERE (store, object, version) IN "
                     "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]') "
                     "FROM json_each(?))", (owner, triples))


def repaired_via(vault: Path, replica: originals.StoredObject) -> str | None:
    """The original that last published a new version for the damaged `replica`, or None when none is named."""
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        row = conn.execute("SELECT repair_via FROM media_damaged_versions WHERE store = ? AND object = ? AND version = ?",
                           (replica.store, replica.object, replica.version)).fetchone()
    return None if row is None else row[0]


#: Whether any of the (store, object, version, ciphertext digest) quadruples of one JSON array is damaged: a primary-key
#: probe each of the version and ciphertext records. The quadruples travel as one value, so the statement is the
#: same for one and for thousands.
_RECORDED = ("FROM json_each(?) AS wanted WHERE EXISTS (SELECT 1 FROM media_damaged_versions "
             "WHERE store = json_extract(wanted.value, '$[0]') AND object = json_extract(wanted.value, '$[1]') "
             "AND version = json_extract(wanted.value, '$[2]')) OR EXISTS (SELECT 1 FROM media_damaged_digests "
             "WHERE ciphertext_sha256 = json_extract(wanted.value, '$[3]'))")


def _inherit(recorded: str) -> str:
    """The statement that records, for each filing of ciphertext that a finding recorded as decrypting wrong, its version.

    A filing of such bytes is damaged under its own name, whatever it is called: `_quarantine_listing` then quarantines
    it, so its row says so and the status, the findings and the recoverability read the row. `recorded` selects the
    digests, from the table that holds them; a version the table already holds keeps its record. A filing that is
    quarantined is never read (`media_replicas_digest`), so a repeated finding costs what it costs for one filing.
    """
    return ("INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256) "
            f"SELECT filing.store, filing.object, filing.version, '{DECRYPTED}', '{PLAINTEXT_MISMATCH}', "
            f"filing.ciphertext_sha256 FROM ({recorded}) AS wrong CROSS JOIN media_replicas AS filing "
            f"ON filing.ciphertext_sha256 = wrong.digest WHERE filing.state <> '{QUARANTINED}' "
            "ON CONFLICT(store, object, version) DO NOTHING")


_INHERIT_OF = _inherit(
    "SELECT :digest AS digest WHERE EXISTS (SELECT 1 FROM media_damaged_digests WHERE ciphertext_sha256 = :digest)")
_INHERIT_ALL = _inherit("SELECT ciphertext_sha256 AS digest FROM media_damaged_digests")
_DAMAGED = f"SELECT 1 {_RECORDED} LIMIT 1"
_DAMAGED_AMONG = ("SELECT json_extract(wanted.value, '$[0]'), json_extract(wanted.value, '$[1]'), "
                  f"json_extract(wanted.value, '$[2]') {_RECORDED}")


def _quadruples(references: Iterable[originals.StoredObject]) -> str:
    """The versions and ciphertext digests of these stored objects, as the one JSON value that `_RECORDED` reads."""
    return json.dumps(sorted({(reference.store, reference.object, reference.version, reference.ciphertext_sha256)
                              for reference in references}))


def quarantined_in(conn: sqlite3.Connection, references: Iterable[originals.StoredObject]) -> bool:
    """Whether the table holds any of these stored versions as damaged, read on `conn`'s own transaction.

    A version also counts when its ciphertext is recorded as decrypting wrong: a copy of such bytes is no better.

    One statement however many versions: the pointers that a pooled object collects grow with its filings and its
    re-copies, and a lookup for each would run inside eviction's write transaction.
    """
    wanted = _quadruples(references)
    return wanted != "[]" and conn.execute(_DAMAGED, (wanted,)).fetchone() is not None


def damaged_among(vault: Path, references: Iterable[originals.StoredObject]) -> frozenset[tuple[str, str, str]]:
    """The (store, object, version) of each of these stored versions that the table holds as damaged, by version or by digest.

    One statement over one connection however many versions: offload asks it for every version of equal bytes at once, and
    a lookup for each would open the ledger once for each.
    """
    wanted = _quadruples(references)
    if wanted == "[]":
        return frozenset()
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        return frozenset(tuple(row) for row in conn.execute(_DAMAGED_AMONG, (wanted,)))


def has_quarantined(vault: Path, references: Iterable[originals.StoredObject]) -> bool:
    """Whether the table holds any of these stored versions as damaged: what eviction asks before it trusts a pointer."""
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        return quarantined_in(conn, references)


def _replicas_of(snapshot: originals.OriginalSnapshot) -> Iterator[originals.StoredObject]:
    """Every replica a binding lists, the eviction store's pointer first."""
    if snapshot.reference is not None:
        yield snapshot.reference
    yield from snapshot.replicas


def rebuild(vault: Path) -> int:
    """Fill the table from the bindings. Return the count walked, or zero when another rebuild takes over.

    A row that a publication recorded while the walk ran carries this rebuild's generation and stays. The census
    is set only after the whole walk, so a walk that stops leaves the status unknown. A binding whose state cannot
    be read is skipped and counted, and the status reports that count. An upload time is kept for a replica the
    table already knew; a rebuilt row has none, because the binding does not record one.
    """
    from . import media_store

    vault = Path(vault).resolve()
    ledger = MediaJobStore(vault)
    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replica_generation SET value = value + 1 WHERE id = 1")
        generation = conn.execute("SELECT value FROM media_replica_generation WHERE id = 1").fetchone()[0]
        conn.execute("DELETE FROM media_replica_census")
    unread = 0

    def skip(error: originals.RetirementUnconfirmed) -> None:
        nonlocal unread
        unread += 1
        media_store.skip_unknown_original(error)

    written = 0
    batch: list[tuple] = []

    def still_current(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN IMMEDIATE")
        return conn.execute("SELECT value FROM media_replica_generation WHERE id = 1").fetchone()[0] == generation

    def flush() -> bool:
        with closing(ledger._connect()) as conn, conn:
            if not still_current(conn):
                return False
            conn.executemany(_UPSERT_REBUILT, batch)
        batch.clear()
        return True

    for snapshot in foreground_priority.yielding(media_store.bound_originals(vault, unconfirmed=skip)):
        relative = snapshot.path.relative_to(vault).as_posix()
        for replica in _replicas_of(snapshot):
            batch.append((relative, replica.store, replica.object, replica.version, replica.key_version,
                          replica.ciphertext_size, None, UPLOAD_VERIFIED, generation, replica.ciphertext_sha256))
            written += 1
        if len(batch) >= _SCAN_BATCH and not flush():
            return 0
    if not flush():
        return 0
    with closing(ledger._connect()) as conn, conn:
        if not still_current(conn):
            return 0
        conn.execute("DELETE FROM media_replicas WHERE seen < ?", (generation,))
        # A filing that the walk wrote as sound but lists a damaged version is quarantined with it, and so is one that lists
        # a copy of ciphertext that decrypts wrong. The record of a damaged version is never forgotten here: the walk skips a
        # binding whose Trash retirement is unconfirmed and never enters the Trash, so a version that no row lists may still
        # be listed by a filing, and eviction trusts the record.
        _restore_damage(conn)
        conn.execute(_INHERIT_ALL)
        _quarantine_listing(conn, time.time())
        _restore_upload_proofs(conn)
        if not unread:
            # Zero decrypt-only members can still mean that all live filings have upload proof.
            conn.execute(f"DELETE FROM {_DECRYPT_PROOFS} AS receipt WHERE n = 0 AND NOT EXISTS ({_KNOWN_EXACT_UPLOAD} "
                         f"AND ({_HEALTH_COLUMNS}) = ({_health_columns('receipt.')}))")
        # A request that had no verified source, a copy again or a first replica, may have one now: the original came
        # back, another replica did, or a pointer that was found wrong has been repaired.
        conn.execute("UPDATE media_replica_queue SET state = ?, attempts = 0, retry_at = 0, code = NULL, "
                     "token = random() WHERE state = ? AND (kind = ? OR code = ?)",
                     (PENDING, REFUSED, RECOPY, media_store.NO_VERIFIED_SOURCE))
        conn.execute("INSERT INTO media_replica_census(id, rebuilt_at, unread) VALUES (1, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET rebuilt_at = excluded.rebuilt_at, unread = excluded.unread",
                     (time.time(), unread))
    return written


def rebuild_due(vault: Path) -> bool:
    """Whether no rebuild has completed lately. A vault with no media store keeps no replica state."""
    if originals.configuration(vault) is None:
        return False
    ledger = MediaJobStore(vault)  # Bootstrap the status projection on upgrade, even without a store observation.
    with closing(ledger._connect(readonly=True)) as conn:
        row = _census(conn)
    return row is None or time.time() - row["rebuilt_at"] >= REBUILD_SECONDS


def _census(conn: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT rebuilt_at, unread FROM media_replica_census WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None  # The table is missing: the next connection that initializes the ledger creates it.


# --- the owner's key-export confirmation -----------------------------------------------------------


def confirm_key_export(vault: Path, key_version: str | None = None) -> str:
    """Record that the owner exported the decryption identity of `key_version` outside this machine.

    The default is the vault's current key version. Nothing here checks the export: the owner's statement is the
    record, and the status says so. Returns the key version that it recorded.
    """
    config = originals.configuration(vault)
    if key_version is None:
        if config is None or "key_version" not in config:
            raise ValueError("the vault configuration names no key version; pass one")
        key_version = str(config["key_version"])
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_key_exports(key_version, confirmed_at) VALUES (?, ?) "
                     "ON CONFLICT(key_version) DO UPDATE SET confirmed_at = excluded.confirmed_at",
                     (key_version, time.time()))
    return key_version


def _exports(conn: sqlite3.Connection) -> dict[str, float]:
    return dict(conn.execute("SELECT key_version, confirmed_at FROM media_key_exports"))


def recoverability(config: dict[str, object], key_version: str, exports: dict[str, float], *,
                   quarantined: bool = False, exports_complete: bool = True) -> tuple[bool | None, str | None]:
    """Whether a replica made under `key_version` survives the loss of this machine, and the reason when it does not.

    A quarantined replica does not: its bytes are damaged or absent, whoever holds the key. Otherwise it does when a
    platform holds the identity (the configuration says so) or the owner confirmed an export. A vault that lists its
    own stores is a personal install, where the owner's export is the only way. An unconfirmed key reads as
    not recoverable if the export history is complete. A vault with neither source of custody evidence reports unknown.
    """
    if quarantined:
        return False, QUARANTINED
    if key_version in exports:
        return True, None
    return _unexported_key_recoverability(config, exports_complete)


def _unexported_key_recoverability(config: dict[str, object], exports_complete: bool) -> tuple[bool | None, str | None]:
    if config.get(originals.KEY_CUSTODY) == originals.PLATFORM_CUSTODY:
        return True, None
    if not exports_complete:
        return None, _EXPORT_EVIDENCE_UNAVAILABLE
    if originals.STORES in config:
        return False, "key not exported"
    return None, "key custody unknown"


# --- what the worker observed of each store ---------------------------------------------------------


_reach: dict[tuple[Path, str], bool] = {}


def _note_reach(vault: Path, store: str, answer: bool, *, force: bool = False) -> None:
    """Write what the worker saw of `store`; an unchanged answer is written again only when `force` says so."""
    if not force and _reach.get((vault, store)) is answer:
        return
    _reach[(vault, store)] = answer
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_replica_reach(store, reachable, observed_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(store) DO UPDATE SET reachable = excluded.reachable, "
                     "observed_at = excluded.observed_at", (store, int(answer), time.time()))


def _reachable_now(vault: Path, name: str, *, deadline: float | None = None, force: bool = False) -> bool:
    """Wait briefly for an identity probe, leaving busy slots and other stores alone."""
    from . import media_store

    timeout = media_store.STORE_PROBE_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    try:
        answer = media_store.wait_store_read(vault, name, lambda: media_store.store_reachable(vault, name), timeout=timeout)
    except media_store.StoreReadPending:
        return False
    except media_store.StoreReadTimedOut:
        answer = False
    if answer is not None:
        _note_reach(vault, name, answer, force=force)
    return answer is not False


def observe(vault: Path, *, deadline: float | None = None) -> None:
    """Record whether each store that has a cheap check answers as itself now.

    Only the worker calls it (and the drain, when a copy meets a store that does not answer), so a status reports an
    observation with its time and never reads a store itself. A store with no cheap check (a remote one) is not
    probed and keeps no record.
    """
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return
    for name in originals.store_entries(config):
        _reachable_now(vault, name, deadline=deadline, force=True)


# --- the id that the owner's declaration recorded for a store's marker ------------------------------


def declared_store_id(vault: Path, store: str) -> str | None:
    """The marker id recorded when the owner declared `store`; None when there is none on this machine.

    Without a record the marker's name alone identifies the store, as before a declaration; the owner declares the
    store again to record the id that its root carries, after a lost ledger as well.
    """
    return declared_store_ids(vault, (store,)).get(store)


def declared_store_ids(vault: Path, stores: Iterable[str]) -> dict[str, str]:
    """Read the recorded marker IDs for a group of candidate stores in one query."""
    from . import media_store

    names = tuple(dict.fromkeys(stores))
    ledger = MediaJobStore(vault, create=False)
    if not names or not ledger.path.exists():
        return {}
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_store_ids'").fetchone() is None:
                return {}
            return dict(conn.execute("SELECT store, store_id FROM media_store_ids "
                                     "WHERE store IN (SELECT value FROM json_each(?))", (json.dumps(names),)))
    except (sqlite3.Error, OSError) as error:
        # Not knowing the recorded id is not the same as there being none: the store is not served on a guess.
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE",
                                          f"the store's recorded identity cannot be read: {type(error).__name__}") from error


def record_store_id(vault: Path, store: str, store_id: str) -> None:
    from .media_store import MediaStoreError

    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        same = conn.execute(
            "INSERT INTO media_store_ids(store, store_id) VALUES (?, ?) "
            "ON CONFLICT(store) DO UPDATE SET store_id = excluded.store_id "
            "WHERE media_store_ids.store_id = excluded.store_id RETURNING store_id", (store, store_id),
        ).fetchone()
        if same is None:
            raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED", "the declared store identity changed")


# --- status -----------------------------------------------------------------------------------------


# The owner read touches one row per store, point lookups for the current sampling key/month, and ranges of
# distinct failed-attempt and copy states. The time queries read the first entry in each matching index range.
_STATUS = f"""
WITH stores(name) AS (
    SELECT value FROM json_each(:stores)
    UNION SELECT store FROM media_replica_counts
)
SELECT stores.name, counts.*, reach.reachable, reach.observed_at, samples.bytes AS spent,
       current_key.n AS current_key, skipped.n AS skipped, no_digest.n AS no_digest,
       (SELECT verified_at FROM media_replicas
        WHERE store = stores.name AND verified_at IS NOT NULL
        ORDER BY verified_at LIMIT 1) AS oldest_upload,
       {", ".join(
           f"(SELECT {_PROOF_AT} FROM media_replicas WHERE store = stores.name "
           f"AND ({_proof_level()}) IS NOT NULL AND ({_proof_level()}) = '{level}' "
           f"ORDER BY {_PROOF_AT} LIMIT 1) AS oldest_{level}"
           for level in (PRESENCE, CONTENT, DECRYPTED)
       )},
       (SELECT json_group_object(token, n) FROM media_replica_buckets
        WHERE store = stores.name AND kind = 'failure' AND key_version = '') AS failures,
       (SELECT json_group_object(token, n) FROM media_replica_buckets
        WHERE store = stores.name AND kind = 'copy' AND key_version = '') AS copies
FROM stores
LEFT JOIN media_replica_counts AS counts ON counts.store = stores.name
LEFT JOIN media_replica_reach AS reach ON reach.store = stores.name
LEFT JOIN media_replica_samples AS samples ON samples.store = stores.name AND samples.month = :month
LEFT JOIN media_replica_buckets AS current_key
    ON current_key.store = stores.name AND current_key.kind = 'key'
    AND current_key.key_version = :key AND current_key.token = ''
LEFT JOIN media_replica_buckets AS skipped
    ON skipped.store = stores.name AND skipped.kind = 'sample' AND skipped.key_version = :key AND skipped.token = :month
LEFT JOIN media_replica_buckets AS no_digest
    ON no_digest.store = stores.name AND no_digest.kind = 'sample' AND no_digest.key_version = :key AND no_digest.token = '{_UNSAMPLEABLE}'
ORDER BY stores.name
"""

#: Why a replica is not sampled, beyond the reasons `media_store.unreadable_reason` gives.
SAMPLING_OFF = "sampling_off"
NO_EXACT_DIGEST = "no_exact_digest"  # its binding lists no plaintext digest to hash against
STORE_NOT_DECLARED = "store_not_declared"
#: Why a store's replicas get no scheduled check.
HEAD_NOT_DECLARED = "head_recomputes_not_declared"
STORE_UNAVAILABLE = "store_unavailable"


def _unknown(reason: str, **extra: object) -> dict[str, object]:
    return {"state": "unknown", "reason": reason, "as_of": None, "stores": None, "totals": None, **extra}


def _check_counts() -> dict[str, object]:
    """The check values of one store, or of all: each level counted apart, so a presence check is never content."""
    return {
        "quarantined": 0, "never_checked": 0, "checks_not_scheduled": None, "failed_attempts": [],
        "checks": {level: {"replicas": 0, "oldest_at": None} for level in (PRESENCE, CONTENT, DECRYPTED)},
    }


def _sample_budget(entry: originals.StoreEntry | None, spent: int, month: int) -> dict[str, object]:
    """One store's sampled-check budget this UTC month; the budget is None for a store that is not declared."""
    return {"month": f"{month // 100}-{month % 100:02d}", "budget_bytes":
            None if entry is None else max(0, entry.sample_bytes_per_month), "spent_bytes": spent,
            "skipped_for_size": 0, "not_sampled": []}


def _sample_totals(stores: list[dict[str, object]]) -> dict[str, object]:
    budgets = [store["budget_bytes"] for store in stores]
    return {"budget_bytes": None if None in budgets else sum(budgets),
            "spent_bytes": sum(store["spent_bytes"] for store in stores),
            "skipped_for_size": sum(store["skipped_for_size"] for store in stores),
            "not_sampled": [{"reason": reason, "replicas": count} for reason, count in sorted(
                _sum_reasons(store["not_sampled"] for store in stores).items())]}


def _merged(first: list[dict[str, object]], second: list[dict[str, object]]) -> list[dict[str, object]]:
    """Two lists of `{failure, replicas}` as one, a failure's replicas summed, in failure order."""
    summed: dict[str, int] = {}
    for item in (*first, *second):
        summed[item["failure"]] = summed.get(item["failure"], 0) + item["replicas"]
    return [{"failure": failure, "replicas": count} for failure, count in sorted(summed.items())]


def _sum_reasons(lists: Iterable[list[dict[str, object]]]) -> dict[str, int]:
    summed: dict[str, int] = {}
    for found in lists:
        for item in found:
            summed[item["reason"]] = summed.get(item["reason"], 0) + item["replicas"]
    return summed


def _unscheduled_reason(entry: originals.StoreEntry | None) -> str | None:
    """Why a store's replicas get no scheduled check, or None when they do: a descriptor value or the store's state."""
    if entry is None:
        return STORE_NOT_DECLARED
    if scheduled_level(entry) is not None:
        return None
    return HEAD_NOT_DECLARED if entry.head_recomputes is None else STORE_UNAVAILABLE


def _count_checks(stats: dict[str, Any], entry: originals.StoreEntry | None, row: sqlite3.Row) -> None:
    for level, proof in stats["checks"].items():
        proof["replicas"] = row[f"proved_{level}"] or 0
        proof["oldest_at"] = row[f"oldest_{level}"]
    unchecked = stats["replicas"] - stats["quarantined"] - sum(proof["replicas"] for proof in stats["checks"].values())
    if unchecked and (why := _unscheduled_reason(entry)) is not None:
        stats["checks_not_scheduled"] = {"replicas": unchecked, "reasons": [why]}
    else:
        stats["never_checked"] = unchecked
    stats["failed_attempts"] = [{"failure": failure, "replicas": count}
                                for failure, count in sorted(json.loads(row["failures"]).items())]


def _count_samples(config: dict[str, object], stats: dict[str, Any], entry: originals.StoreEntry | None,
                   row: sqlite3.Row, key: str) -> None:
    from . import media_store

    healthy = stats["replicas"] - stats["quarantined"]
    if not healthy:
        return
    reason = _unscheduled_reason(entry)
    if reason is None:
        reason = (SAMPLING_OFF if entry.sample_bytes_per_month <= 0
                  else media_store.replica_readability(config, entry, key))
    if reason is not None:
        stats["sample"]["not_sampled"] = [{"reason": reason, "replicas": healthy}]
        return
    stats["sample"]["skipped_for_size"] = row["skipped"] or 0
    counts = {NO_EXACT_DIGEST: row["no_digest"] or 0,
              media_store.IDENTITY_UNREADABLE: healthy - (row["current_key"] or 0)}
    stats["sample"]["not_sampled"] = [{"reason": reason, "replicas": count}
                                      for reason, count in sorted(counts.items()) if count]


def _count_recoverability(config: dict[str, object], stats: dict[str, Any], exported: int, unproved: int,
                          exports_complete: bool) -> None:
    unexported = stats["replicas"] - stats["quarantined"] - exported - unproved
    recoverable, reason = _unexported_key_recoverability(config, exports_complete)
    stats["recoverable"] = exported
    stats["not_recoverable"] = stats["quarantined"]
    stats["recoverability_unknown"] = unproved
    if unproved:
        stats["recoverability_unknown_reasons"].append(_DAMAGE_EVIDENCE_UNAVAILABLE)
    if stats["quarantined"]:
        stats["not_recoverable_reasons"].append(QUARANTINED)
    if unexported:
        if recoverable is True:
            stats["recoverable"] += unexported
        elif recoverable is False:
            stats["not_recoverable"] += unexported
            stats["not_recoverable_reasons"].append(reason)
        else:
            stats["recoverability_unknown"] += unexported
            stats["recoverability_unknown_reasons"].append(reason)


def _add_checks(totals: dict[str, Any], stats: dict[str, Any]) -> None:
    """Fold one store's check values into the aggregate."""
    totals["quarantined"] += stats["quarantined"]
    totals["never_checked"] += stats["never_checked"]
    totals["failed_attempts"] = _merged(totals["failed_attempts"], stats["failed_attempts"])
    if (found := stats["checks_not_scheduled"]) is not None:
        waiting = totals["checks_not_scheduled"] or {"replicas": 0, "reasons": []}
        totals["checks_not_scheduled"] = {"replicas": waiting["replicas"] + found["replicas"],
                                          "reasons": sorted({*waiting["reasons"], *found["reasons"]})}
    for level, proof in stats["checks"].items():
        folded = totals["checks"][level]
        folded["replicas"] += proof["replicas"]
        if proof["oldest_at"] is not None and (folded["oldest_at"] is None or proof["oldest_at"] < folded["oldest_at"]):
            folded["oldest_at"] = proof["oldest_at"]


def status(vault: Path) -> dict[str, object]:
    """Read the owner status from one ledger snapshot, bounded by the stores and returned dimensions."""
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return {"state": "unavailable", "reason": "no media store is configured", "as_of": None, "stores": None,
                "totals": None}
    entries = originals.store_entries(config)
    key = str(config.get("key_version"))
    month = _month(time.time())
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:  # never recreates a table
            conn.execute("BEGIN")
            schema = _status_schema(conn)
            if not _STATUS_REQUIRED_SCHEMA.issubset(schema):
                return _unknown("the replica status schema is incomplete")
            exports_complete = _export_coverage_complete(conn, schema)
            census = _census(conn)
            if census is None:
                return _unknown("the replica table is being rebuilt from the bindings")
            rows = {row["name"]: row for row in conn.execute(_STATUS, {"stores": json.dumps(list(entries)),
                                                                      "key": key, "month": month})}
            eviction = media_store.personal_eviction_status(conn, config)
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}")
    names = (*entries, *(name for name in rows if name not in entries))
    stores: dict[str, dict[str, Any]] = {}
    for name in names:
        row = rows[name]
        entry = entries.get(name)
        refusal = media_store.connection_unusable(entry) if entry is not None else None
        replicas = row["replicas"] or 0
        stats = stores[name] = {
            "name": name,
            "configured": entry is not None,
            "available": entry.available and refusal is None if entry is not None else False,
            "unavailable_reason": refusal,
            "missing_fields": list(entry.missing) if entry is not None else [],
            "invalid_fields": list(entry.invalid) if entry is not None else [],
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "head_recomputes": entry.head_recomputes if entry is not None else None,
            "serves_eviction": bool(entry and entry.serves_eviction),
            # The worker's last observation, never a read of the store.
            "reachable": None if row["reachable"] is None else bool(row["reachable"]),
            "reachable_observed_at": _iso(row["observed_at"]),
            "replicas": replicas, "verified_at_upload": row["verified"] or 0,
            "recoverable": 0, "not_recoverable": 0,
            "recoverability_unknown": 0, "not_recoverable_reasons": [], "recoverability_unknown_reasons": [],
            "oldest_verified_at": row["oldest_upload"], "verification_time_unknown": replicas - (row["timed"] or 0),
            **_check_counts(), "sample": _sample_budget(entry, row["spent"] or 0, month),
        }
        stats["quarantined"] = row["quarantined"] or 0
        _count_checks(stats, entry, row)
        _count_samples(config, stats, entry, row, key)
        _count_recoverability(config, stats, row["exported"] or 0, row["unproved"] or 0, exports_complete)
        if counts := json.loads(row["copies"]):
            stats["copy"] = _copy_counts(counts)
    holding = [stats for stats in stores.values() if stats["replicas"]]
    domains = {stats["failure_domain"] for stats in holding if stats["failure_domain"] is not None}
    undeclared = sum(1 for stats in holding if stats["failure_domain"] is None)
    times = [stats["oldest_verified_at"] for stats in holding if stats["oldest_verified_at"] is not None]
    totals = {
        "replicas": sum(stats["replicas"] for stats in holding),
        "verified_at_upload": sum(stats["verified_at_upload"] for stats in holding),
        "recoverable": sum(stats["recoverable"] for stats in holding),
        "not_recoverable": sum(stats["not_recoverable"] for stats in holding),
        "recoverability_unknown": sum(stats["recoverability_unknown"] for stats in holding),
        # Unknown, not a lower bound: a store that holds replicas and declares no domain may share one with another.
        "distinct_failure_domains": None if undeclared else len(domains),
        "stores_without_failure_domain": undeclared,
        "oldest_verified_at": min(times) if times else None,
        "verification_time_unknown": sum(stats["verification_time_unknown"] for stats in holding),
        **_check_counts(),
        "sample": _sample_totals([stats["sample"] for stats in holding]),
    }
    for stats in holding:
        _add_checks(totals, stats)
    for stats in (*stores.values(), totals):
        stats["oldest_verified_at"] = _iso(stats["oldest_verified_at"])
        for proof in stats["checks"].values():
            proof["oldest_at"] = _iso(proof["oldest_at"])
    result = {**_coverage(census, exports_complete=exports_complete,
                          damage_complete=not any(row["unproved"] for row in rows.values())),
              "source": "media ledger table, rebuilt from the bindings",
              "stores": list(stores.values()), "totals": totals}
    if eviction is not None:
        result["eviction"] = eviction
    return result


def _coverage(census: sqlite3.Row, *, exports_complete: bool = True, damage_complete: bool = True) -> dict[str, object]:
    unread = census["unread"]
    if unread:
        reason = "some bindings could not be read during the rebuild"
    elif not damage_complete:
        reason = _DAMAGE_EVIDENCE_UNAVAILABLE
    else:
        reason = None if exports_complete else _EXPORT_EVIDENCE_UNAVAILABLE
    return {"state": "partial" if reason else "measured",
            "reason": reason,
            "as_of": _iso(census["rebuilt_at"]), "unread_originals": unread}


_CHECK_COLUMNS = (
    "level, checked_at, decrypted_at, failure, "
    f"{_proof_level()} AS proof_level, {_PROOF_AT} AS proof_at, {_upload_proof()} AS upload_proved"
)
_SHARED_HEALTH_UNKNOWN = (f"({_unproved_health('r.')} AND NOT EXISTS (SELECT 1 FROM {_DECRYPT_PROOFS} "
                          f"WHERE ({_HEALTH_COLUMNS}) = ({_health_columns('r.')}) AND sampled = 1))")


def _check_view(row: sqlite3.Row) -> dict[str, object]:
    """The latest successful check, the last decrypt time and any failure of the pinned version."""
    quarantined = row["state"] == QUARANTINED
    return {
        "last_check": ({"level": row["proof_level"], "at": _iso(row["proof_at"])}
                       if row["proof_level"] is not None else None),
        "decrypted_at": _iso(row["decrypted_at"] or None),
        "finding": ({"failed_level": row["level"], "failure": row["failure"], "at": _iso(row["checked_at"])}
                    if quarantined else None),
        # What the last check or sample attempt found when it could not prove the replica and found no damage.
        "failed_attempt": None if quarantined else row["failure"],
    }


def _evidence_state(row: sqlite3.Row) -> str:
    if row["state"] == QUARANTINED:
        return QUARANTINED
    if row["unproved"]:
        return "unknown"
    return UPLOAD_VERIFIED if row["upload_proved"] else DECRYPTED


def page(vault: Path, *, after: str | None = None, limit: int = 100) -> dict[str, object]:
    """The originals that have replicas, by path, `limit` at a time; `next_after` is the cursor for the next page.

    Each original lists its recorded replicas with the store's descriptor. An original with none is not listed:
    the table records replicas, and the copy queue counts what remains to copy.
    """
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return _unknown("no media store is configured", originals=None, next_after=None)
    entries = originals.store_entries(config)
    limit = max(1, min(int(limit), 500))
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:  # a read never recreates a table
            conn.execute("BEGIN")
            schema = _status_schema(conn)
            if not _STATUS_REQUIRED_SCHEMA.issubset(schema):
                return _unknown("the replica status schema is incomplete", originals=None, next_after=None)
            census = _census(conn)
            exports = _exports(conn)
            exports_complete = _export_coverage_complete(conn, schema)
            paths = [row[0] for row in conn.execute(
                "SELECT DISTINCT binary_rel FROM media_replicas WHERE binary_rel > ? ORDER BY binary_rel LIMIT ?",
                (after or "", limit + 1))]
            rows = conn.execute(
                f"SELECT binary_rel, store, object, version, key_version, verified_at, state, {_CHECK_COLUMNS}, "
                f"{_SHARED_HEALTH_UNKNOWN} AS unproved FROM media_replicas AS r WHERE binary_rel > ? AND binary_rel <= ? "
                "ORDER BY binary_rel, store",
                (after or "", paths[min(limit, len(paths)) - 1] if paths else "")).fetchall()
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}", originals=None, next_after=None)
    if census is None:
        return _unknown("the replica table is being rebuilt from the bindings", originals=None, next_after=None)
    more = len(paths) > limit
    listed: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        entry = entries.get(row["store"])
        recoverable, reason = recoverability(config, row["key_version"], exports,
                                             quarantined=row["state"] == QUARANTINED, exports_complete=exports_complete)
        if row["unproved"]:
            recoverable, reason = None, _DAMAGE_EVIDENCE_UNAVAILABLE
        listed.setdefault(row["binary_rel"], []).append({
            "store": row["store"], "state": _evidence_state(row),
            "verified_at": _iso(row["verified_at"]),
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": row["key_version"],
            **_check_view(row),
        })
    return {**_coverage(census, exports_complete=exports_complete, damage_complete=not any(row["unproved"] for row in rows)),
            "originals": [{"path": path, "replicas": found} for path, found in listed.items()],
            "next_after": next(reversed(listed)) if more and listed else None}


#: The quarantined replicas in path order, with the state of the copy that will replace each: a range of
#: `media_replicas_findings` and one primary-key lookup in the queue per row of the page.
_FINDINGS = ("SELECT r.binary_rel, r.store, r.object, r.version, r.level, r.failure, r.checked_at, "
             "q.state AS queue_state, q.code AS queue_code "
             "FROM media_replicas AS r LEFT JOIN media_replica_queue AS q "
             f"ON q.store = r.store AND q.binary_rel = r.binary_rel AND q.kind = '{RECOPY}' "
             f"WHERE r.state = '{QUARANTINED}' AND (r.binary_rel, r.store) > (?, ?) "
             "ORDER BY r.binary_rel, r.store LIMIT ?")


def findings(vault: Path, *, after: tuple[str, str] | None = None, limit: int = 100) -> dict[str, object]:
    """The quarantined replicas by original and store, `limit` at a time; `next_after` is the cursor for the next page.

    Each finding names the original, the store, the damaged version that stays in its store, the level that failed and
    why, when, and the copy again: pending, or refused with its code, or none when no copy is queued.
    """
    vault = Path(vault).resolve()
    limit = max(1, min(int(limit), 500))
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:  # a read never recreates a table
            conn.execute("BEGIN")
            if not _STATUS_REQUIRED_SCHEMA.issubset(_status_schema(conn)):
                return _unknown("the replica status schema is incomplete", findings=None, next_after=None)
            census = _census(conn)
            rows = conn.execute(_FINDINGS, (*(after or ("", "")), limit + 1)).fetchall()
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}", findings=None, next_after=None)
    if census is None:
        return _unknown("the replica table is being rebuilt from the bindings", findings=None, next_after=None)
    shown = rows[:limit]
    return {**_coverage(census),
            "findings": [{
                "path": row["binary_rel"], "store": row["store"], "object": row["object"], "version": row["version"],
                "failed_level": row["level"], "failure": row["failure"], "found_at": _iso(row["checked_at"]),
                "recopy": None if row["queue_state"] is None else {"state": row["queue_state"], "code": row["queue_code"]},
            } for row in shown],
            "next_after": [shown[-1]["binary_rel"], shown[-1]["store"]] if len(rows) > limit else None}


def _matches_replica(row: sqlite3.Row | None, replica: originals.StoredObject) -> bool:
    return row is not None and (
        row["object"], row["version"], row["key_version"], row["size"], row["ciphertext_sha256"]
    ) == (replica.object, replica.version, replica.key_version, replica.ciphertext_size, replica.ciphertext_sha256)


# One first-row probe of the quarantined-version index per requested binding entry, even for shared versions.
_SAME_VERSION_QUARANTINES = (
    "SELECT r.store, r.level, r.failure FROM json_each(?) AS wanted CROSS JOIN media_replicas AS r "
    "ON (r.binary_rel, r.store) = (SELECT held.binary_rel, held.store FROM media_replicas AS held "
    f"INDEXED BY {_QUARANTINED_VERSION_INDEX} WHERE (held.object, held.version, held.store) = (json_extract(wanted.value, '$[1]'), "
    f"json_extract(wanted.value, '$[2]'), json_extract(wanted.value, '$[0]')) AND held.state = '{QUARANTINED}' LIMIT 1)"
)


def original_view(vault: Path, path: str) -> dict[str, object]:
    """One binding and its evidence from one readonly ledger snapshot."""
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return {"state": "unavailable", "reason": "no media store is configured", "replicas": None}
    snapshot = originals.original_state(vault, path)
    replicas = tuple(_replicas_of(snapshot))
    entries = originals.store_entries(config)
    ledger = MediaJobStore(vault, create=False)
    recorded: dict[str, sqlite3.Row] = {}
    versions: dict[str, sqlite3.Row] = {}
    digests: set[str] = set()
    exports: dict[str, float] = {}
    damage_complete = projection_complete = exports_complete = False
    if replicas and ledger.path.exists():
        wanted = _quadruples(replicas)
        try:
            with closing(ledger._connect(readonly=True)) as conn:
                conn.execute("BEGIN")
                schema = _status_schema(conn)
                sources = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' OR (type = 'index' AND name = ?)",
                    (_QUARANTINED_VERSION_INDEX,))}
                # Each damage source may survive independently. A surviving positive finding is still authoritative.
                if "media_damaged_versions" in sources:
                    versions = {row["store"]: row for row in conn.execute(
                        "SELECT d.store, d.level, d.failure FROM json_each(?) AS wanted JOIN media_damaged_versions AS d "
                        "ON (d.store, d.object, d.version) = (json_extract(wanted.value, '$[0]'), "
                        "json_extract(wanted.value, '$[1]'), json_extract(wanted.value, '$[2]'))", (wanted,))}
                if "media_damaged_digests" in sources:
                    digests = {row[0] for row in conn.execute(
                        "SELECT ciphertext_sha256 FROM media_damaged_digests WHERE ciphertext_sha256 IN "
                        "(SELECT json_extract(value, '$[3]') FROM json_each(?))", (wanted,))}
                damage_complete = {"media_damaged_versions", "media_damaged_digests"} <= sources
                if "media_key_exports" in sources:
                    exports = dict(conn.execute(
                        "SELECT key_version, confirmed_at FROM media_key_exports WHERE key_version IN "
                        "(SELECT value FROM json_each(?))", (json.dumps(sorted({r.key_version for r in replicas})),)))
                    exports_complete = _export_coverage_complete(conn, schema)
                if "media_replicas" in sources:
                    missing_versions = tuple(replica for replica in replicas if replica.store not in versions)
                    if missing_versions and _QUARANTINED_VERSION_INDEX not in sources:
                        damage_complete = False
                    elif missing_versions:
                        surviving = {row["store"]: row for row in conn.execute(
                            _SAME_VERSION_QUARANTINES, (_quadruples(missing_versions),))}
                        if surviving:
                            versions.update(surviving)
                            damage_complete = False
                    unproved = _SHARED_HEALTH_UNKNOWN if _DECRYPT_PROOFS in schema else _unproved_health()
                    recorded = {row["store"]: row for row in conn.execute(
                        f"SELECT store, object, version, key_version, size, ciphertext_sha256, verified_at, "
                        f"state, {_CHECK_COLUMNS}, {unproved} AS unproved FROM media_replicas AS r WHERE binary_rel = ?",
                        (snapshot.path.relative_to(vault).as_posix(),))}
                    projection_complete = _REPLICA_STATUS_TRIGGERS.issubset(schema)
        except (sqlite3.Error, OSError):
            # Retain any positive evidence already read in the snapshot; the missing part remains unknown.
            pass
    found = []
    filings_complete = True
    for replica in replicas:
        entry = entries.get(replica.store)
        row = recorded.get(replica.store)
        checked = row if _matches_replica(row, replica) else None
        filings_complete &= checked is not None and not checked["unproved"]
        view = _check_view(checked) if checked is not None else {
            "last_check": None, "decrypted_at": None, "finding": None, "failed_attempt": None,
        }
        version = versions.get(replica.store)
        if version is not None or replica.ciphertext_sha256 in digests:
            level = version["level"] if version is not None else DECRYPTED
            failure = version["failure"] if version is not None else PLAINTEXT_MISMATCH
            if view["finding"] is None or view["finding"]["failure"] != failure:
                view["finding"] = {"failed_level": level, "failure": failure, "at": None}
            view["last_check"] = view["failed_attempt"] = None
        if view["finding"] is not None:
            state, recoverable, reason = QUARANTINED, False, QUARANTINED
        elif not damage_complete or not projection_complete or checked is None or checked["unproved"]:
            state, recoverable, reason = "unknown", None, _DAMAGE_EVIDENCE_UNAVAILABLE
        else:
            state = _evidence_state(checked)
            recoverable, reason = recoverability(config, replica.key_version, exports, exports_complete=exports_complete)
        found.append({
            "store": replica.store, "state": state,
            "verified_at": _iso(checked["verified_at"]) if checked is not None else None,
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": replica.key_version,
            **view,
        })
    partial = bool(replicas) and not (projection_complete and filings_complete and damage_complete and exports_complete)
    return {"state": "partial" if partial else "measured",
            "reason": "some replica evidence is unavailable" if partial else None,
            "original_state": snapshot.state, "replicas": found}


# --- the copy queue ---------------------------------------------------------------------------------


def request(vault: Path, store: str) -> dict[str, int]:
    """Ask for a replica of every original in `store`; the background drain makes them. Returns the current counts.

    While a request for the store is running (its scan has not finished, or originals are left to copy) a repeat
    changes nothing and returns the counts: read the progress there, or in the status. Once it has finished, a new
    request starts a new scan, drops the finished rows of the last one and arms the refused ones. It raises
    `MediaStoreError` for a store that cannot take a replica now.
    """
    from . import media_store

    vault = Path(vault).resolve()
    media_store.require_replica_store(vault, store)
    now = time.time()
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT 1 FROM media_replica_demands WHERE store = ? AND (scanned_at IS NULL OR EXISTS "
            "(SELECT 1 FROM media_replica_buckets WHERE store = ? AND kind = 'copy' AND key_version = '' AND token = ?))",
            (store, store, PENDING)).fetchone()
        if active is None:
            conn.execute(f"DELETE FROM media_replica_queue WHERE store = ? AND state = ? AND {_copy_member()}",
                         (store, DONE))
            conn.execute("UPDATE media_replica_queue SET state = ?, attempts = 0, retry_at = 0, code = NULL, "
                         f"token = random() WHERE store = ? AND state = ? AND {_copy_member()}",
                         (PENDING, store, REFUSED))
            conn.execute("INSERT INTO media_replica_demands(store, requested_at, scan_token) VALUES (?, ?, random()) "
                         "ON CONFLICT(store) DO UPDATE SET requested_at = excluded.requested_at, "
                         "scanned_at = NULL, scan_token = excluded.scan_token", (store, now))
    wake()
    return progress(vault, store)


def _copy_counts(counts: dict[str, int]) -> dict[str, int]:
    return {"queued": sum(counts.values()), "copied": counts.get(DONE, 0),
            "refused": counts.get(REFUSED, 0), "remaining": counts.get(PENDING, 0)}


def progress(vault: Path, store: str) -> dict[str, object]:
    """One store's copy counts and scan state, read without initializing the ledger."""
    unknown = {"store": store, **dict.fromkeys(_copy_counts({})), "scan": "unknown"}
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:
            conn.execute("BEGIN")
            if not _STATUS_REQUIRED_SCHEMA.issubset(_status_schema(conn)):
                return unknown
            counts = dict(conn.execute("SELECT token, n FROM media_replica_buckets "
                                       "WHERE store = ? AND kind = 'copy' AND key_version = ''", (store,)))
            scanned = conn.execute("SELECT scanned_at FROM media_replica_demands WHERE store = ?", (store,)).fetchone()
    except (sqlite3.Error, OSError):
        return unknown
    return {"store": store, **_copy_counts(counts),
            "scan": "complete" if scanned is not None and scanned["scanned_at"] is not None else "running"}


def scan(vault: Path, store: str) -> int:
    """Queue each original whose binding lists no replica in `store`; return the number queued.

    The walk holds no guard: a copy re-reads its binding under the guard, so a binding that changed after the scan
    is the copy's retry, not a wrong replica.
    """
    from . import media_store

    ledger = MediaJobStore(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        demand = conn.execute("SELECT scan_token FROM media_replica_demands WHERE store = ? AND scanned_at IS NULL",
                              (store,)).fetchone()
    if demand is None:
        return 0
    scan_token = demand["scan_token"]
    queued = 0
    batch: list[tuple[str, str]] = []

    def flush() -> bool:
        with closing(ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM media_replica_demands WHERE store = ? AND scan_token = ? AND scanned_at IS NULL",
                            (store, scan_token)).fetchone() is None:
                return False
            conn.executemany("INSERT INTO media_replica_queue(store, binary_rel, state, token, copy_member) "
                             "VALUES (?, ?, 'pending', random(), 1) ON CONFLICT(store, binary_rel) "
                             f"DO UPDATE SET copy_member = 1 WHERE NOT {_copy_member()}", batch)
        batch.clear()
        return True

    for snapshot in foreground_priority.yielding(media_store.bound_originals(
            vault, unconfirmed=media_store.skip_unknown_original)):
        if any(replica.store == store for replica in _replicas_of(snapshot)):
            continue
        batch.append((store, snapshot.path.relative_to(vault).as_posix()))
        queued += 1
        if len(batch) >= _SCAN_BATCH and not flush():
            return 0
    if not flush():
        return 0
    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replica_demands SET scanned_at = ? WHERE store = ? AND scan_token = ? "
                     "AND scanned_at IS NULL", (time.time(), store, scan_token))
    return queued


def _pick(paused: set[str], store: str | None = None) -> str:
    """The query for the next pending original outside the paused stores: `(state, store, binary_rel)` order, no sort."""
    destination = " AND store = ?" if store is not None else ""
    skipped = f" AND store NOT IN ({', '.join('?' for _ in paused)})" if paused else ""
    return ("SELECT store, binary_rel, attempts, kind, token FROM media_replica_queue WHERE state = ? AND retry_at <= ?"
            + destination + skipped + " ORDER BY store, binary_rel LIMIT 1")


def drain(vault: Path, *, deadline: float | None = None, store: str | None = None) -> int:
    """Copy a batch of queued originals, optionally for one destination, and return the count settled."""
    from . import media_store, mutation_lock
    from .cli_ops import OpError

    vault = Path(vault).resolve()
    ledger = MediaJobStore(vault)
    settled = 0
    limit = time.monotonic() + _DRAIN_SECONDS if deadline is None else deadline
    paused: set[str] = set()
    destination = () if store is None else (store,)
    while settled < _DRAIN_BATCH and time.monotonic() < limit:
        with closing(ledger._connect(readonly=True)) as conn:
            row = conn.execute(_pick(paused, store), (PENDING, time.time(), *destination, *paused)).fetchone()
        if row is None:
            break
        foreground_priority.yield_to_foreground()
        code = None
        try:
            copy = media_store.recopy if row["kind"] == RECOPY else media_store.replicate
            with mutation_lock.absorbing_busy():
                copy(vault, vault / row["binary_rel"], row["store"])
                _reconcile_copy(ledger, row)
        except (media_store.StoreReadPending, media_store.CopyStoreUnavailable) as error:
            # The copy had not passed preflight and spent no upload or retry attempt.
            foreground_priority.check_cancelled()
            if isinstance(error, media_store.CopyStoreUnavailable):
                _note_reach(vault, row["store"], False)
            paused.add(row["store"])
            continue
        except sqlite3.Error as error:
            # SQLite attaches extended result codes. Their low byte identifies the primary condition.
            result_code = getattr(error, "sqlite_errorcode", None)
            if result_code is None or result_code & 0xFF not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            code = media_store.STORE_BUSY_CODE
        except media_store.MediaStoreError as error:
            code = error.code
        except OpError as error:
            if error.code == media_store.STORE_BUSY_CODE:
                code = error.code
            elif error.code == "MUTATION_BUSY" and error.details.get("committed") is False:
                code = media_store.STORE_BUSY_CODE
            else:
                raise
        except (OSError, ValueError) as error:
            code = type(error).__name__
        # A transfer that returns after shutdown leaves its request for the next activation.
        foreground_priority.check_cancelled()
        if code is None:
            settled += 1
        elif code == media_store.STORE_BUSY_CODE:
            # Ordinary contention with a foreground write: it spends no attempt, and the round ends so that the
            # next one finds the boundary free (the uploaded ciphertext keeps its name and is reused).
            break
        elif code not in media_store.REPLICA_REFUSAL_CODES:
            attempts = row["attempts"] + 1
            if attempts >= _MAX_ATTEMPTS:
                _settle(ledger, row, REFUSED, code, attempts=attempts)
                settled += 1
            else:
                _settle(ledger, row, PENDING, code, attempts=attempts, retry_at=time.time() + _RETRY_SECONDS * attempts)
            if code == "MEDIA_STORE_UNAVAILABLE":
                paused.add(row["store"])  # A store that failed mid-copy fails the same way for the next original.
        else:
            _settle(ledger, row, REFUSED, code)
            settled += 1
    return settled


_PENDING_COPY = f"store = :store AND binary_rel = :path AND token = :token AND state = '{PENDING}'"


def _copy_identity(row: sqlite3.Row) -> dict[str, object]:
    return {"store": row["store"], "path": row["binary_rel"], "token": row["token"]}


def _complete_copies(conn: sqlite3.Connection, matches: str, parameters: dict[str, object]) -> None:
    """Complete first-copy members. Remove standalone repairs."""
    conn.execute(f"DELETE FROM media_replica_queue WHERE ({matches}) AND NOT {_copy_member()}", parameters)
    conn.execute(f"UPDATE media_replica_queue SET state = '{DONE}', code = NULL "
                 f"WHERE ({matches}) AND {_copy_member()}", parameters)


def _reconcile_copy(ledger: MediaJobStore, row: sqlite3.Row) -> None:
    """Project the current binding and settle a pending copy under the publication boundary."""
    from . import media_store

    vault = ledger.vault_root

    def reconcile() -> None:
        snapshot = originals.original_state(vault, row["binary_rel"])
        current = media_store._entry_in(snapshot, row["store"])
        identity = _copy_identity(row)
        with closing(ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(f"SELECT 1 FROM media_replica_queue WHERE {_PENDING_COPY}", identity).fetchone() is None:
                return  # A newer finding or a completed copy owns the row.
            if current is None:
                if row["kind"] == REPLICATE:
                    raise media_store.MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the copy is no longer in its binding")
            else:
                recorded = conn.execute(
                    "SELECT object, version, key_version, size, ciphertext_sha256, state FROM media_replicas "
                    "WHERE binary_rel = ? AND store = ?", (row["binary_rel"], current.store),
                ).fetchone()
                damaged = quarantined_in(conn, (current,))
                if not _matches_replica(recorded, current) or (damaged and recorded["state"] != QUARANTINED):
                    _record_replica(conn, row["binary_rel"], current, verified_at=None)
                scope = "" if snapshot.reference == current else current.store
                conn.execute("DELETE FROM media_protections WHERE binary_rel = ? AND object = ? AND store = ? "
                             "AND uploadable = 0", (row["binary_rel"], current.object, scope))
                if damaged:
                    return  # A finding owns the current repair request.
            _complete_copies(conn, _PENDING_COPY, identity)

    media_store._publishing(vault, "media_store_replica_settle", reconcile)


def _settle(ledger: MediaJobStore, row: sqlite3.Row, state: str, code: str | None, *, attempts: int | None = None,
            retry_at: float | None = None) -> None:
    with closing(ledger._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # The writer lock may have taken time. Check the copy's generation again before writing its outcome.
        foreground_priority.check_cancelled()
        conn.execute("UPDATE media_replica_queue SET state = :state, code = :code, attempts = COALESCE(:attempts, attempts), "
                     f"retry_at = COALESCE(:retry_at, retry_at) WHERE {_PENDING_COPY}",
                     {**_copy_identity(row), "state": state, "code": code, "attempts": attempts, "retry_at": retry_at})


_observed: dict[Path, float] = {}
_checked: dict[Path, float] = {}  # when each vault last ran a check round


def work(vault: Path, *, deadline: float | None = None, copies: _CopyDispatch | None = None) -> bool:
    """One bounded unit of background work: a rebuild, a scan, or a drain and the check round that is due; True when it did something."""
    from . import media_store
    from .cli_ops import OpError

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return False
    if time.monotonic() - _observed.get(vault, -_OBSERVE_SECONDS) >= _OBSERVE_SECONDS:
        _observed[vault] = time.monotonic()
        observe(vault, deadline=deadline)
    if rebuild_due(vault):
        rebuild(vault)
        return True
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        waiting = [row["store"] for row in conn.execute(
            "SELECT store FROM media_replica_demands WHERE scanned_at IS NULL ORDER BY requested_at")]
    if waiting:
        scan(vault, waiting[0])
        return True
    now = time.monotonic()
    next_check = _checked.get(vault)
    drain_until = now + _DRAIN_SECONDS if deadline is None else min(deadline, now + _DRAIN_SECONDS)
    if next_check is not None:
        drain_until = min(drain_until, next_check + _CHECK_INTERVAL)
    try:
        settled = (drain(vault, deadline=drain_until) if copies is None
                   else copies.offer(vault, deadline=deadline))
    except OpError as error:
        if error.code != media_store.STORE_BUSY_CODE:
            raise
        settled = 0
    except sqlite3.Error:
        # The request stays pending. A broken copy write cannot stop due verification.
        log.warning("media replicas: a copy round could not update its ledger", exc_info=True)
        settled = 0
    # Verification does not wait for an empty queue: a backlog of copies would otherwise hold every check and sample
    # back for as long as it lasts, with replicas overdue. A round costs one bounded drain and one bounded check.
    now = time.monotonic()
    if (deadline is not None and now >= deadline) or (next_check is not None and now - next_check < _CHECK_INTERVAL):
        return settled > 0
    _checked[vault] = now
    return settled + check(vault, deadline=deadline) + sample(vault, deadline=deadline) > 0


# --- scheduled checks --------------------------------------------------------------------------------


def check_cycle() -> float:
    """Seconds in which every replica is checked once; the operator's setting, 30 days by default."""
    from . import original_fixity

    return original_fixity.setting("EXOMEM_REPLICA_CHECK_CYCLE_DAYS", DEFAULT_CHECK_CYCLE_DAYS) * 86400.0


def check_read_rate() -> float:
    """The cap on the bytes per second that content checks may make a store read."""
    from . import original_fixity

    return original_fixity.setting("EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND", DEFAULT_CHECK_READ_MIB_PER_SECOND) * 1024**2


#: What each vault's reads still owe their cap, as the monotonic instant that they are paid for, by kind of round.
#: A read too large for one round's time leaves its wait to the next round: a fresh cap each round would forgive it.
#: It lives in this process only, so a restart starts clean.
_owed: dict[tuple[Path, str], float] = {}


class _ReadCap:
    """Keeps reads under the read-rate cap: each read earns its own time, and time spent elsewhere earns no burst.

    A store's `head` and a sample read whole objects, so the cap applies per object. `reading` accounts for the read
    however it ends, and `ready` is the one place that waits: it holds the next read until the earlier ones are paid
    for. A round gives its `key` (vault and kind) to take up what the rounds before it left owing, and to leave its own.
    """

    def __init__(self, rate: float, limit: float, key: tuple[Path, str] | None = None) -> None:
        self.rate, self.limit, self.key = rate, limit, key
        self.due = _owed.get(key, 0.0) if key is not None else 0.0

    def ready(self, *, until: float | None = None) -> bool:
        """False when the wait that earlier reads left owing outlasts this round; else waits it out, and the read may go."""
        limit = self.limit if until is None else min(self.limit, until)
        if self.due >= limit:
            return False
        foreground_priority.wait_for_retry(max(0.0, self.due - time.monotonic()))
        return time.monotonic() < limit

    def charge(self, started: float, size: int) -> None:
        if size > 0:
            self.due = max(self.due, started) + size / self.rate
            if self.key is not None:
                _owed[self.key] = self.due

    @contextmanager
    def reading(self, size: int):
        """Account for a read of `size` bytes that starts now, whether it verified, failed or was cut off.

        The read's time counts from its start, so the read itself is part of it and only what remains is waited out.
        A failed read is charged its whole size: how much it moved before it failed is unknown, and a store that
        fails after most of the bytes must not be read faster than the cap for it.
        An invalid locator or a confirmed missing version costs no bytes.
        """
        from .media_store import ReplicaAbsent, StoreReadPending

        started, charged = time.monotonic(), True
        try:
            yield
        except (ValueError, ReplicaAbsent, StoreReadPending):
            charged = False
            raise
        finally:
            if charged:
                self.charge(started, size)


def scheduled_level(entry: originals.StoreEntry) -> str | None:
    """The check that a store's descriptor allows: content when its `head` recomputes, presence when it does not.

    A descriptor that does not state it, or a store that is not usable, gets none: a check of unknown strength would
    report a proof that it cannot name. The decision reads the descriptor value and never a store's or adapter's name.
    """
    if not entry.available or entry.head_recomputes is None:
        return None
    return CONTENT if entry.head_recomputes else PRESENCE


_BINDING_UNREADABLE = (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)


def _bound(vault: Path, row: sqlite3.Row) -> tuple[originals.OriginalSnapshot, originals.StoredObject] | str | None:
    """Read the current binding. A changed projection ends this pick and is left for the next one."""
    from . import media_store

    try:
        snapshot = originals.original_state(vault, row["binary_rel"])
    except _BINDING_UNREADABLE:
        return UNREADABLE
    entry = media_store._entry_in(snapshot, row["store"])
    if entry is None:
        return UNLISTED
    if not _matches_replica(row, entry):
        from .writer_lease import get_manager

        with get_manager().consistency_guard(vault, operation="media_replica_refresh", holder_kind="background"):
            try:
                current = originals.original_state(vault, row["binary_rel"])
            except _BINDING_UNREADABLE:
                return None
            if current != snapshot:
                return None
            with closing(MediaJobStore(vault, create=False)._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    f"SELECT 1 FROM media_replicas WHERE binary_rel = ? AND ({_HEALTH_COLUMNS}) IS ({_HEALTH_PARAMETERS})",
                    (row["binary_rel"], *(row[field] for field in _HEALTH_FIELDS)),
                ).fetchone() is None:
                    return None
                changed = _record_replica(conn, row["binary_rel"], entry, verified_at=None)
        if changed:
            wake()
        return None
    return snapshot, entry


def _passed(vault: Path, binary_rel: str, replica: originals.StoredObject, level: str) -> None:
    """Record a check that matched. A replica that was quarantined meanwhile stays so.

    It clears what an earlier attempt found, except a key refusal: only a download that decrypts can disprove that.
    """
    from . import media_store

    now = time.time()
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET checked_at = ?, check_from = ?, level = ?, "
                     "failure = CASE WHEN failure = ? THEN failure ELSE NULL END "
                     "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                     f"AND state <> '{QUARANTINED}'",
                     (now, now, level, media_store.KEY_REFUSED, binary_rel, replica.store, replica.object,
                      replica.version))


def _failed(vault: Path, row: sqlite3.Row, finding: str, *, attempted: originals.StoredObject | None = None,
            sample: bool = False) -> None:
    """Record an attempt on a replica that found no damage and proved nothing, and move the row on in its schedule.

    The finding replaces what the row showed and is counted in the status. It never quarantines: an attempt that
    could not complete says nothing about the bytes. The row's next turn comes after `_FAILED_RETRY`, so one unreadable
    row never holds back the rows behind it. It lands on the version that was attempted (`attempted`, else the row's
    own), so a version that a copy again published meanwhile does not inherit a finding about the one it replaced.
    """
    column, cycle = ("sample_from" if sample else "check_from"), check_cycle()
    name, version = (attempted.object, attempted.version) if attempted is not None else (row["object"], row["version"])
    # A quarantined row (a sample's second look) keeps the finding that quarantined it; only its turn moves.
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute(f"UPDATE media_replicas SET {column} = ?, failure = CASE WHEN state = '{QUARANTINED}' "
                     "THEN failure ELSE ? END WHERE binary_rel = ? AND store = ? AND object = ? AND version = ?",
                     (time.time() - cycle + min(cycle, _FAILED_RETRY), finding, row["binary_rel"], row["store"],
                      name, version))


#: The replicas of a store attempted longest ago, never-attempted first, that the cycle has made due: a range of
#: `media_replicas_due` in its own order, so a pick reads `LIMIT` rows whatever the table holds.
_DUE = (f"SELECT binary_rel, {_HEALTH_COLUMNS}, check_from FROM media_replicas "
        f"WHERE store = ? AND state <> '{QUARANTINED}' AND check_from <= ? ORDER BY check_from LIMIT ?")


def _due(ledger: MediaJobStore, store: str, cycle: float, limit: int) -> list[sqlite3.Row]:
    with closing(ledger._connect(readonly=True)) as conn:
        return conn.execute(_DUE, (store, time.time() - cycle, limit)).fetchall()


# One indexed budget row and one first due row per store, in a single snapshot.
_CHECK_PLAN = (
    "SELECT wanted.value AS store, COALESCE(t.replicas, 0) AS replicas, COALESCE(t.bytes, 0) AS bytes, "
    "(SELECT check_from FROM media_replicas WHERE store = wanted.value "
    f"AND state <> '{QUARANTINED}' AND check_from <= ? ORDER BY check_from LIMIT 1) AS oldest "
    "FROM json_each(?) AS wanted LEFT JOIN media_replica_counts AS t ON t.store = wanted.value "
    "ORDER BY oldest NULLS LAST, wanted.key"
)


def _check_plan(ledger: MediaJobStore, stores: Iterable[str], cycle: float) -> dict[str, tuple[int, int]]:
    """The round's shares, in oldest-due order. A store with no due replica goes last."""
    from . import original_fixity

    with closing(ledger._connect(readonly=True)) as conn:
        rows = conn.execute(_CHECK_PLAN, (time.time() - cycle, json.dumps(list(stores)))).fetchall()
    return {
        row["store"]: (max(_CHECK_FLOOR_ROWS, math.ceil(row["replicas"] * _CHECK_INTERVAL / cycle)),
                       original_fixity.budget_for([row["bytes"]], interval=_CHECK_INTERVAL, cycle=cycle))
        for row in rows
    }


def _round_seconds(content_bytes: int, rate: float) -> float:
    """How long the content shares take at the round's one read cap, within its allowed window."""
    return min(_CHECK_ROUND_MAX, max(_CHECK_ROUND_SECONDS, _CHECK_ROUND_FACTOR * content_bytes / rate))


_scheduled_reads: dict[tuple[Path, str], media_store.ScheduledRead] = {}


def _store_deadline(round_deadline: float, stores_left: int) -> float:
    now = time.monotonic()
    return now + (round_deadline - now) / stores_left


def check(vault: Path, *, deadline: float | None = None) -> int:
    """One bounded round of scheduled checks over every store whose descriptor allows one; return the replicas attempted.

    Each store takes the replicas attempted longest ago first, a pick at a time, until its share of the cycle is spent
    (replicas, and for a content check bytes under the read-rate cap) or the round's time is. It holds no vault guard: a
    check reads a store and writes one derived row. A replica whose binding or read fails is recorded and skipped; a
    store that does not answer as itself, or fails `_STORE_FAILURES` replicas in a row, ends its own round and changes
    no row that it did not attempt. The store that has waited longest goes first.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return 0
    ledger = MediaJobStore(vault)
    cycle, rate = check_cycle(), check_read_rate()
    entries = originals.store_entries(config)
    levels = {name: level for name, entry in entries.items()
              if (level := scheduled_level(entry)) is not None}
    plan = _check_plan(ledger, levels, cycle)
    reads_bytes = {name for name in levels if media_store.head_reads_bytes(entries[name])}
    for key in tuple(_scheduled_reads):
        if key[0] == vault and key[1] not in reads_bytes:
            del _scheduled_reads[key]
    content_bytes = sum(share[1] for name, share in plan.items() if name in reads_bytes)
    limit = time.monotonic() + _round_seconds(content_bytes, rate)
    if deadline is not None:
        limit = min(limit, deadline)
    cap = _ReadCap(rate, limit, (vault, "check"))
    checked = 0
    for index, (name, share) in enumerate(plan.items()):
        until = _store_deadline(limit, len(plan) - index)
        checked += _check_store(vault, ledger, name, levels[name], cycle, cap, until, share,
                                reads_bytes=name in reads_bytes)
    return checked


def _check_store(vault: Path, ledger: MediaJobStore, name: str, level: str, cycle: float, cap: _ReadCap,
                 limit: float, share: tuple[int, int], *, reads_bytes: bool = True) -> int:
    """One store's part of a check round: the replicas attempted."""
    from . import media_store

    if time.monotonic() >= limit:
        return 0
    if not _reachable_now(vault, name, deadline=limit):
        return 0
    rows, budget = share
    attempted = spent = failing = 0
    read = (_scheduled_reads.setdefault((vault, name), media_store.ScheduledRead())
            if reads_bytes else media_store.ScheduledRead())
    while attempted < rows:
        picked = _due(ledger, name, cycle, min(rows - attempted, _CHECK_BATCH))
        if not picked:
            break
        for row in picked:
            if time.monotonic() >= limit or (reads_bytes and (spent >= budget or not cap.ready(until=limit))):
                return attempted
            foreground_priority.yield_to_foreground()
            attempted += 1
            bound = _bound(vault, row)
            if bound is None:
                continue
            if isinstance(bound, str):
                _failed(vault, row, bound)
                continue
            replica = bound[1]
            started = time.monotonic()
            if started >= limit:
                return attempted - 1
            size = replica.ciphertext_size if reads_bytes else 0
            charged = None

            def head(reference=replica):
                read.bytes_read = None
                return media_store.check_replica(vault, reference, deadline=limit - _HEAD_RETURN_SECONDS, scheduled=read)

            try:
                failure = media_store.wait_store_read(vault, name, head, timeout=limit - started)
            except media_store.StoreReadPending:
                charged = 0
                return attempted - 1
            except media_store.StoreReadTimedOut:
                # The timed-out read may still be updating its count.
                charged = size
                _failed(vault, row, READ_FAILED, attempted=replica)
                return attempted
            except media_store.ReplicaCheckPending:
                return attempted
            except media_store.ReplicaAbsent as missing:
                charged = 0
                failure = missing.failure
            except ValueError:
                charged = 0  # refused before it read anything
                _failed(vault, row, RECORD_INVALID, attempted=replica)  # the store answered; it cannot address this version
                continue
            except media_store.MediaStoreError:
                if not _reachable_now(vault, name, deadline=limit):
                    # The store went away mid-round. Keep the prior evidence.
                    return attempted - 1
                _failed(vault, row, READ_FAILED, attempted=replica)  # unavailable is no proof of loss: not quarantined
                failing += 1
                if failing >= _STORE_FAILURES:
                    return attempted
                continue
            finally:
                if charged is None:
                    charged = size if read.bytes_read is None else read.bytes_read
                spent += charged
                cap.charge(started, charged)
            failing = 0
            if failure is None:
                _passed(vault, row["binary_rel"], replica, level)
            else:
                quarantine(vault, row["binary_rel"], replica, level=level, failure=failure)
    return attempted


# --- sampled checks ----------------------------------------------------------------------------------

#: The replicas that one round samples in a store, the replicas it may skip in one for size, and the skipped ones that a
#: new month releases in one write.
_SAMPLE_READS = 8
_SAMPLE_SKIPS = 64
_SAMPLE_RELEASE = 256

#: The replica of a store whose sample turn is oldest, never-sampled first, among those not skipped this month and
#: whose turn the cycle has made due: a range of `media_replicas_sample` in its own order. The key version is the one
#: whose identity this install holds. A replica is sampled once per cycle, so a small store does not spend its budget twice.
_SAMPLE = (f"SELECT binary_rel, {_HEALTH_COLUMNS} FROM media_replicas "
           f"WHERE store = ? AND key_version = ? AND state <> '{QUARANTINED}' AND sample_month = 0 "
           "AND sample_from <= ? ORDER BY sample_from LIMIT 1")
#: The quarantined replica that a sample may release, whose turn comes first: a range of `media_replicas_reproof`, whose
#: every row a plaintext finding still holds (a finding of another cause moves its filings out of it, `quarantine_all`).
#: It also reads the generation of its ciphertext's damage (see `_released`).
_REPROOF = (f"SELECT binary_rel, {_HEALTH_COLUMNS}, (SELECT generation FROM media_damaged_digests AS d "
            "WHERE d.ciphertext_sha256 = media_replicas.ciphertext_sha256) AS generation "
            f"FROM media_replicas WHERE store = ? AND key_version = ? AND state = '{QUARANTINED}' "
            f"AND failure = '{PLAINTEXT_MISMATCH}' AND sample_from <= ? ORDER BY sample_from LIMIT 1")
#: A new month gives the replicas that the last one skipped for size another chance, a bounded batch at a time.
_SAMPLE_RESET = ("UPDATE media_replicas SET sample_month = 0 WHERE (binary_rel, store) IN ("
                 "SELECT binary_rel, store FROM media_replicas WHERE store = ? AND key_version = ? "
                 f"AND state <> '{QUARANTINED}' AND sample_month > 0 AND sample_month < ? LIMIT ?)")


def _month(moment: float) -> int:
    """The UTC calendar month of `moment` as YYYYMM: the period of a store's sample budget."""
    stamp = datetime.fromtimestamp(moment, timezone.utc)
    return stamp.year * 100 + stamp.month


def _mark_sample(vault: Path, row: sqlite3.Row, month: int, replica: originals.StoredObject) -> None:
    """Take a replica out of the sample picks for `month` (or for good, for `_UNSAMPLEABLE`).

    It lands on the exact version that was looked at.
    """
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET sample_month = ? WHERE binary_rel = ? "
                     f"AND ({_HEALTH_COLUMNS}) = ({_HEALTH_PARAMETERS})", (month, row["binary_rel"], *_health_key(replica)))


def _proved(vault: Path, row: sqlite3.Row, replica: originals.StoredObject) -> None:
    """Record a sample that matched: it clears every finding of an attempt, and a replica quarantined meanwhile stays so."""
    now = time.time()
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        exact = _health_key(replica)
        if conn.execute("UPDATE media_replicas SET decrypted_at = ?, sample_from = ?, failure = NULL "
                        f"WHERE binary_rel = ? AND ({_HEALTH_COLUMNS}) = ({_HEALTH_PARAMETERS}) "
                        f"AND state <> '{QUARANTINED}'", (now, now, row["binary_rel"], *exact)).rowcount:
            conn.execute(_RECORD_EXACT_DECRYPT, exact)


def _released(vault: Path, row: sqlite3.Row, replica: originals.StoredObject) -> None:
    """A quarantined replica whose download decrypted to the binding was a false alarm: it leaves quarantine, unchecked.

    The proof answers the damage that the sample picked, and the ciphertext's record names that damage by its
    generation: every finding of a wrong plaintext of these bytes, on any version of them and from any filing, gives it
    a new one (`_record_damage`). Only a generation that is still the one the sample read is released, in one
    transaction. The ciphertext decrypted, so every filing of it under any name that a wrong plaintext quarantined is
    released too. Their copies settle unless a sealed hold needs guarded cleanup. No record of the wrong plaintext
    remains; a newer finding
    on any of them leaves all of it, this replica included, quarantined with its request. A filing that holds another
    cause stays: its version is lost or differs, which this download does not answer. Shared health follows the exact
    downloaded version. Only the sampled filing keeps a check time, and the other released filings become unchecked.
    """
    now = time.time()
    store, name, version = row["store"], replica.object, replica.version
    exact = _health_key(replica)
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        if not conn.execute(
                "UPDATE media_replicas SET state = ?, level = NULL, failure = NULL, checked_at = 0, check_from = 0, "
                f"decrypted_at = ?, sample_from = ? WHERE binary_rel = ? AND ({_HEALTH_COLUMNS}) = ({_HEALTH_PARAMETERS}) "
                f"AND state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}' "
                "AND (SELECT generation FROM media_damaged_digests WHERE ciphertext_sha256 = ?) = ? "
                "AND EXISTS (SELECT 1 FROM media_damaged_versions AS d "
                "WHERE (d.store, d.object, d.version) = (media_replicas.store, media_replicas.object, media_replicas.version) "
                f"AND d.failure = '{PLAINTEXT_MISMATCH}')",
                (UPLOAD_VERIFIED, now, now, row["binary_rel"], *exact,
                 replica.ciphertext_sha256, row["generation"])).rowcount:
            return
        conn.execute("UPDATE media_damaged_versions SET ciphertext_sha256 = ? "
                     "WHERE (store, object, version) = (?, ?, ?) AND ciphertext_sha256 IS NULL",
                     (replica.ciphertext_sha256, store, name, version))
        # Clear the shared plaintext finding. Each version still needs its own proof of a successful read.
        # Keep other causes and records that a remaining quarantine depends on.
        digest = replica.ciphertext_sha256
        same = (f"WHERE state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}' AND (object, version, store) IN "
                "(SELECT object, version, store FROM media_damaged_versions "
                f"WHERE ciphertext_sha256 = :digest AND failure = '{PLAINTEXT_MISMATCH}')")
        # The first update already released the sampled row. Include it alongside the remaining filings of the digest.
        released = ("SELECT store, binary_rel, object FROM media_replicas WHERE store = :store AND binary_rel = :path "
                    f"UNION ALL SELECT store, binary_rel, object FROM media_replicas {same}")
        # Protections use an empty scope for the eviction pointer and the store name for an additional replica.
        # The guarded settlement identifies the actual scope before deleting the sealed hold.
        jobs = (f"kind = '{RECOPY}' AND (store, binary_rel) IN (SELECT r.store, r.binary_rel FROM ({released}) AS r "
                "WHERE EXISTS (SELECT 1 FROM media_protections AS hold WHERE hold.binary_rel = r.binary_rel "
                "AND hold.object = r.object AND hold.store IN ('', r.store) AND hold.uploadable = 0) = :sealed)")
        identity = {"store": store, "path": row["binary_rel"], "digest": digest}
        cleanup = conn.execute(
            "UPDATE media_replica_queue SET state = :pending, code = NULL, attempts = 0, retry_at = 0, token = random() "
            f"WHERE {jobs}", {**identity, "pending": PENDING, "sealed": True}).rowcount
        _complete_copies(conn, jobs, {**identity, "sealed": False})
        conn.execute("UPDATE media_replicas SET state = :healthy, level = NULL, failure = NULL, checked_at = 0, "
                     f"check_from = 0, decrypted_at = 0, sample_from = 0 {same}",
                     {"healthy": UPLOAD_VERIFIED, "digest": digest})
        conn.execute(_RECORD_EXACT_DECRYPT, exact)
        conn.execute("DELETE FROM media_damaged_versions WHERE ciphertext_sha256 = ? AND failure = ? AND NOT EXISTS ("
                     "SELECT 1 FROM media_replicas AS held WHERE held.object = media_damaged_versions.object "
                     "AND held.version = media_damaged_versions.version AND held.store = media_damaged_versions.store "
                     f"AND held.state = '{QUARANTINED}')", (digest, PLAINTEXT_MISMATCH))
        conn.execute("UPDATE media_damaged_versions SET ciphertext_sha256 = NULL WHERE ciphertext_sha256 = ?", (digest,))
        conn.execute("DELETE FROM media_damaged_digests WHERE ciphertext_sha256 = ?", (digest,))
    if cleanup:
        wake()


def _charge(conn: sqlite3.Connection, store: str, month: int, size: int, budget: int | None = None) -> bool:
    """Add `size` bytes to what `store` has spent in `month`, and say whether they were added.

    With a `budget`, the add is one statement that reads the balance under the write lock it takes, and it adds nothing
    when the sum would pass the budget: two runtimes that read the same remainder cannot both spend it. Without one (a
    refund) the add is unconditional.
    """
    return bool(conn.execute(
        "INSERT INTO media_replica_samples(store, month, bytes) SELECT :store, :month, :size "
        "WHERE :budget IS NULL OR :size <= :budget "
        "ON CONFLICT(store, month) DO UPDATE SET bytes = bytes + excluded.bytes "
        "WHERE :budget IS NULL OR bytes <= :budget - excluded.bytes",
        {"store": store, "month": month, "size": size, "budget": budget}).rowcount)


def sample(vault: Path, *, deadline: float | None = None) -> int:
    """One bounded round of sampled checks; return the replicas it sampled.

    A sample downloads a replica, decrypts it into a sink that keeps nothing, and hashes the plaintext against the
    binding. Each store spends at most its `sample_bytes_per_month` in a UTC calendar month (zero turns it off), and a
    replica that does not fit the month's remainder is skipped for the month. The bytes are charged before the
    download, so a sample that stops part-way is still paid for. It runs where checks run (`_unscheduled_reason`) and
    reads only where repair could read: an `immediate` store and the key version whose identity this install holds.
    A store that does not answer as itself, or an unreadable identity, is no finding: nothing changes. A replica whose
    read fails, or that the key refuses, is recorded and skipped; a quarantined replica whose plaintext differed gets
    one more download per round, and leaves quarantine when it decrypts to the binding.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return 0
    ledger = MediaJobStore(vault)
    limit = time.monotonic() + _CHECK_ROUND_SECONDS
    if deadline is not None:
        limit = min(limit, deadline)
    cap, cycle = _ReadCap(check_read_rate(), limit, (vault, "sample")), check_cycle()
    month, key_version, sampled = _month(time.time()), str(config.get("key_version")), 0
    stores = [(name, entry) for name, entry in originals.store_entries(config).items()
              if _unscheduled_reason(entry) is None and entry.sample_bytes_per_month > 0
              and media_store.unreadable_reason(vault, name, key_version) is None]
    for index, (name, entry) in enumerate(stores):
        if time.monotonic() >= limit:
            break
        until = _store_deadline(limit, len(stores) - index)
        if not _reachable_now(vault, name, deadline=until):
            continue
        with closing(ledger._connect()) as conn, conn:
            conn.execute(_SAMPLE_RESET, (name, key_version, month, _SAMPLE_RELEASE))
        reads = skips = failing = 0
        second_look = True  # a quarantined replica comes first, once per round
        while reads < _SAMPLE_READS and skips < _SAMPLE_SKIPS and time.monotonic() < until:
            with closing(ledger._connect(readonly=True)) as conn:
                row = conn.execute(_REPROOF, (name, key_version, time.time() - cycle)).fetchone() if second_look else None
                quarantined = row is not None
                if row is None:
                    row = conn.execute(_SAMPLE, (name, key_version, time.time() - cycle)).fetchone()
                held = conn.execute("SELECT bytes FROM media_replica_samples WHERE store = ? AND month = ?",
                                    (name, month)).fetchone()
            second_look = False
            if row is None:
                break
            remaining = entry.sample_bytes_per_month - (held["bytes"] if held is not None else 0)
            if remaining <= 0:
                break
            foreground_priority.yield_to_foreground()
            bound = _bound(vault, row)
            if bound is None:
                skips += 1
                continue
            if isinstance(bound, str):
                _failed(vault, row, bound, sample=True)  # a binding that cannot be read now is no reason to give up on the row
                skips += 1
                continue
            snapshot, replica = bound
            if snapshot.binary_sha256 is None or snapshot.binary_size is None:
                # the binding is readable and lists no plaintext digest: nothing to hash against
                _mark_sample(vault, row, _UNSAMPLEABLE, replica)
                skips += 1
                continue
            reserved = False
            if replica.ciphertext_size <= remaining:
                if not cap.ready(until=until):
                    break  # an earlier round's read is not yet paid for: nothing is charged or read until it is
                with closing(ledger._connect()) as conn, conn:  # the balance read above may be stale: reserve against the budget
                    reserved = _charge(conn, name, month, replica.ciphertext_size, entry.sample_bytes_per_month)
            if not reserved:  # it does not fit the month's remainder, or another runtime spent the remainder meanwhile
                if not quarantined:
                    _mark_sample(vault, row, month, replica)
                    skips += 1
                continue
            if time.monotonic() >= until:
                with closing(ledger._connect()) as conn, conn:
                    _charge(conn, name, month, -replica.ciphertext_size)
                break
            reads += 1
            sampled += 1
            try:
                with cap.reading(replica.ciphertext_size):
                    damage = media_store.sample_replica(
                        vault, replica, media_store.ObjectHead(snapshot.binary_size, snapshot.binary_sha256), deadline=until)
            except media_store.StoreReadPending:
                with closing(ledger._connect()) as conn, conn:
                    _charge(conn, name, month, -replica.ciphertext_size)
                sampled -= 1
                break
            except media_store.KeyRefused:
                # the bytes match their record: no damage
                _failed(vault, row, media_store.KEY_REFUSED, attempted=replica, sample=True)
                failing += 1
            except (ValueError, media_store.ReplicaAbsent) as error:
                # The store answered before moving ciphertext, so refund the reservation.
                with closing(ledger._connect()) as conn, conn:
                    _charge(conn, name, month, -replica.ciphertext_size)
                if isinstance(error, media_store.ReplicaAbsent):
                    quarantine(vault, row["binary_rel"], replica, level=DECRYPTED, failure=error.failure)
                    failing = 0
                else:
                    _failed(vault, row, RECORD_INVALID, attempted=replica, sample=True)
            except media_store.MediaStoreError:
                _failed(vault, row, READ_FAILED, attempted=replica, sample=True)  # unavailable is no proof of loss
                failing += 1
            else:
                failing = 0
                if damage is not None:
                    quarantine(vault, row["binary_rel"], replica, level=DECRYPTED, failure=damage)
                elif quarantined:
                    _released(vault, row, replica)
                else:
                    _proved(vault, row, replica)
            if failing >= _STORE_FAILURES:
                break  # a store or key that fails three in a row is the likelier cause; the next round tries again
    return sampled


# --- the thread -------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None
# One stalled transfer leaves one slot free. An old runtime retains its slots through cleanup.
_COPY_SLOTS = 2
_active_copies: set[tuple[Path, str]] = set()
_NEXT_COPY_STORE = (
    "SELECT store FROM media_replica_queue WHERE state = ? AND store > ? ORDER BY store LIMIT 1"
)


class _CopyDispatch:
    """Offer each destination a turn without waiting on its ongoing transfer."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.after = ""
        self.retry_after: dict[str, float] = {}

    def offer(self, vault: Path, *, deadline: float | None) -> int:
        now = time.monotonic()
        if self.stop.is_set() or (deadline is not None and now >= deadline):
            return 0
        with _LOCK:
            free = _COPY_SLOTS - len(_active_copies)
            active = {store for root, store in _active_copies if root == vault}
            self.retry_after = {store: retry for store, retry in self.retry_after.items() if retry > now}
            skipped = active | self.retry_after.keys()
        if free <= 0:
            return 0

        picked = []
        initial = self.after
        wrapped = False
        ledger = MediaJobStore(vault)
        with closing(ledger._connect(readonly=True)) as conn:
            # Seek past each distinct store using the queue's (state, store, path) index.
            # A blocked store's entire queue costs one seek, including when its backlog is large.
            for _ in range(_DRAIN_BATCH):
                row = conn.execute(_NEXT_COPY_STORE, (PENDING, self.after)).fetchone()
                if row is None:
                    if wrapped or not initial:
                        break
                    self.after, wrapped = "", True
                    continue
                store = row["store"]
                if wrapped and store > initial:
                    break
                self.after = store
                if store not in skipped:
                    picked.append(store)
                    if len(picked) == free:
                        break

        for store in picked:
            self._launch(vault, store)
        return len(picked)

    def _launch(self, vault: Path, store: str) -> None:
        key = vault, store
        with _LOCK:
            if self.stop.is_set() or key in _active_copies or len(_active_copies) >= _COPY_SLOTS:
                return
            _active_copies.add(key)

        def copy() -> None:
            settled = 0
            try:
                with foreground_priority.bulk(stop=self.stop):
                    settled = drain(vault, store=store)
            except foreground_priority.BulkCancelled:
                pass
            except Exception:  # noqa: BLE001 - an individual destination cannot stop the scheduler
                log.warning("media replicas: a destination copy round failed", exc_info=True)
            finally:
                with _LOCK:
                    if not settled:
                        self.retry_after[store] = time.monotonic() + _IDLE_SECONDS
                    _active_copies.remove(key)
                wake()

        try:
            threading.Thread(target=copy, name="exomem-replica-copy", daemon=True).start()
        except RuntimeError:
            with _LOCK:
                _active_copies.remove(key)
            raise


def wake() -> None:
    """Tell the background thread that new work is queued."""
    _wake.set()


def start(vault_root: Path) -> threading.Thread | None:
    """Start the replica worker. Idempotent: a second call returns the live one."""
    global _thread, _stop
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            if _stop.is_set():
                raise RuntimeError("the replica worker is stopping")
            return _thread
        _stop = threading.Event()
        thread = threading.Thread(target=_run, args=(Path(vault_root).resolve(), _stop),
                                  name="exomem-media-replicas", daemon=True)
        _thread = thread
        thread.start()
        return thread


def stop(timeout: float = 2.0, *, worker: threading.Thread | None = None) -> None:
    """Stop dispatch and wait briefly for the scheduler; transfers retain their slots until cleanup."""
    global _thread
    with _LOCK:
        thread = _thread
        if worker is not None and thread is not worker:
            return
        _stop.set()
        _wake.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    with _LOCK:
        if _thread is thread and (thread is None or not thread.is_alive()):
            _thread = None


def _run(vault: Path, stop: threading.Event | None = None) -> None:
    stop = _stop if stop is None else stop
    copies = _CopyDispatch(stop)
    while not stop.is_set():
        _wake.clear()
        busy = False
        try:
            with foreground_priority.bulk(stop=stop):
                busy = work(vault, copies=copies)
        except foreground_priority.BulkCancelled:
            return
        except Exception:  # noqa: BLE001 - a failed unit must not end the worker or deny the service
            log.warning("media replicas: a unit of work failed; the worker tries again", exc_info=True)
        if not busy:
            until_check = _checked.get(vault, 0.0) + _CHECK_INTERVAL - time.monotonic()
            _wake.wait(min(_IDLE_SECONDS, until_check) if until_check > 0 else _IDLE_SECONDS)
