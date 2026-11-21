"""Replica custody in the media ledger: where each original's replicas are, and how protected it is.

The sidecar binding is the record: `stored_object` and `replicas` (`originals.REPLICAS`) list what the vault holds.
The tables here are derived from those bindings and rebuild from them, so a lost ledger loses no custody fact. The
exceptions are the owner's key-export confirmations, which a lost ledger turns back into "not exported", and the
copy queue, which the owner starts again with `replicate`.

Status reads one grouped query over an indexed table and never walks the bindings. A table that is missing, or that
no rebuild has completed, shows every value as unknown.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import foreground_priority, held_fs, originals, reserved_paths
from .media_jobs import MediaJobStore

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
)
_STATUS_INDEX_COLUMNS = ("store", "key_version", "state", "level", "verified_at", "checked_at", "decrypted_at",
                         "sample_month")


def initialize(conn: sqlite3.Connection) -> None:
    """Create the replica tables. A table created fresh clears the census, so its first rows read as unknown."""
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_replicas'").fetchone()
    conn.execute("CREATE TABLE IF NOT EXISTS media_replicas ("
                 "binary_rel TEXT NOT NULL, store TEXT NOT NULL, object TEXT NOT NULL, version TEXT NOT NULL, "
                 "key_version TEXT NOT NULL, verified_at REAL, state TEXT NOT NULL, seen INTEGER NOT NULL, "
                 + ", ".join(f"{name} {kind}" for name, kind in _ADDED_COLUMNS)
                 + ", PRIMARY KEY (binary_rel, store)) WITHOUT ROWID")
    have = {row[1] for row in conn.execute("PRAGMA table_info(media_replicas)")}
    healed = [(name, kind) for name, kind in _ADDED_COLUMNS if name not in have]
    for name, kind in healed:
        conn.execute(f"ALTER TABLE media_replicas ADD COLUMN {name} {kind}")
    # The status groups by these columns in this order, so it reads the index alone. An index of an earlier release
    # lacks the check columns, so it is replaced.
    wanted = [row[2] for row in conn.execute("PRAGMA index_info(media_replicas_status)")]
    if wanted and tuple(wanted) != _STATUS_INDEX_COLUMNS:
        conn.execute("DROP INDEX media_replicas_status")
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_status ON media_replicas("
                 + ", ".join(_STATUS_INDEX_COLUMNS) + ")")
    # A check round takes the replicas checked longest ago first, and a sample those proved by decryption longest ago
    # first, each as one range read in index order with no sort. The literal in the partial index is the one the
    # queries below repeat: SQLite uses a partial index only when the query states the same condition.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_due ON media_replicas(store, check_from) "
                 f"WHERE state <> '{QUARANTINED}'")
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_sample ON media_replicas("
                 "store, key_version, sample_month, sample_from) "
                 f"WHERE state <> '{QUARANTINED}'")
    # The rows whose last attempt found something, which the status counts by store and token: only those rows are in it.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_failing ON media_replicas(store, failure) "
                 f"WHERE state <> '{QUARANTINED}' AND failure IS NOT NULL")
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
    # The filings of one ciphertext, under any name, that are not quarantined: the ones a wrong plaintext of it
    # quarantines together (`_inherit`, `_quarantine_listing`). A finding that repeats reads none that it handled before,
    # however many filings hold the ciphertext. A release finds the quarantined ones through the records (`_released`).
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_digest ON media_replicas(ciphertext_sha256) "
                 f"WHERE state <> '{QUARANTINED}'")
    # The filings of one stored version that a plaintext finding quarantined, which a later finding of another cause on
    # the version moves to that cause (`quarantine_all`): a version has few filings, so that reads only them, however
    # many other replicas are quarantined.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_reproof_version ON media_replicas(object, version, store) "
                 f"WHERE state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}'")
    # A stored version that a check, a sample or eviction found damaged, whichever filings list it: what eviction asks
    # before it drops an original's local bytes. Damage is a fact of the version, so it outlives any filing's row: a row
    # that a rebuild writes late, or that a lost write never made, is gated by it and quarantined by `rebuild`.
    # `generation` is the version's one authority: every write that records or renews damage gives it a new random
    # value, and an operation that acts on the damage state commits only if the generation it read is still current
    # (none, for a version that was not damaged). Filings' rows and requests are projections of this record.
    # `ciphertext_sha256` is set when a finding is a wrong plaintext: those bytes decrypt wrong under any name, so each
    # copy of them is as wrong, and the digest finds the copies. A later finding of another cause on the version leaves
    # it, and only the release that proves the bytes good clears it.
    conn.execute("CREATE TABLE IF NOT EXISTS media_damaged_versions ("
                 "store TEXT NOT NULL, object TEXT NOT NULL, version TEXT NOT NULL, level TEXT NOT NULL, "
                 "failure TEXT NOT NULL, repair_via TEXT, generation INTEGER NOT NULL, ciphertext_sha256 TEXT, "
                 "PRIMARY KEY (store, object, version)) WITHOUT ROWID")
    conn.execute("CREATE INDEX IF NOT EXISTS media_damaged_versions_ciphertext "
                 "ON media_damaged_versions(ciphertext_sha256)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_census ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), rebuilt_at REAL NOT NULL, unread INTEGER NOT NULL)")
    if existed is None or healed:
        # A fresh table has no rows to trust, and a healed one has no sizes until a rebuild writes them.
        conn.execute("DELETE FROM media_replica_census")
    # Every rebuild takes the next number. A row carries the number of the rebuild that last confirmed it, or of the
    # one running when a publication wrote it, so "stale" never depends on a wall clock that can step backwards.
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_generation ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO media_replica_generation(id, value) VALUES (1, 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_demands ("
                 "store TEXT PRIMARY KEY, requested_at REAL NOT NULL, scanned_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_queue ("
                 "store TEXT NOT NULL, binary_rel TEXT NOT NULL, state TEXT NOT NULL, code TEXT, "
                 "attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, "
                 f"kind TEXT NOT NULL DEFAULT '{REPLICATE}', token INTEGER NOT NULL DEFAULT 0, "
                 "PRIMARY KEY (store, binary_rel)) WITHOUT ROWID")
    queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(media_replica_queue)")}
    if "kind" not in queue_columns:
        conn.execute(f"ALTER TABLE media_replica_queue ADD COLUMN kind TEXT NOT NULL DEFAULT '{REPLICATE}'")
    # Every write that makes a request pending gives it a new random `token`: a settlement matches the token it
    # picked, so it never closes or overwrites a request made after it, even one that replaced a deleted row.
    if "token" not in queue_columns:
        conn.execute("ALTER TABLE media_replica_queue ADD COLUMN token INTEGER NOT NULL DEFAULT 0")
    # The drain's pick is `ORDER BY store, binary_rel` among the pending rows, so this order reads it with no sort;
    # `retry_at` only filters.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replica_queue_pick ON media_replica_queue(state, store, binary_rel)")
    # What the worker last saw of each store that has a cheap check, so a status never reads a store.
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_reach ("
                 "store TEXT PRIMARY KEY, reachable INTEGER NOT NULL, observed_at REAL NOT NULL)")
    # The replicas and bytes each store held at the last rebuild: what a check round sizes its byte budget from.
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_store_totals ("
                 "store TEXT PRIMARY KEY, replicas INTEGER NOT NULL, bytes INTEGER NOT NULL)")
    # The ciphertext bytes that sampled checks downloaded from each store in each UTC calendar month (YYYYMM).
    conn.execute("CREATE TABLE IF NOT EXISTS media_replica_samples ("
                 "store TEXT NOT NULL, month INTEGER NOT NULL, bytes INTEGER NOT NULL, "
                 "PRIMARY KEY (store, month)) WITHOUT ROWID")
    # The random id that the owner's declaration recorded for each filesystem store's marker.
    conn.execute("CREATE TABLE IF NOT EXISTS media_store_ids (store TEXT PRIMARY KEY, store_id TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_key_exports ("
                 "key_version TEXT PRIMARY KEY, confirmed_at REAL NOT NULL)")


def _iso(value: float | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


# --- what a publication records ---------------------------------------------------------------------


def _upsert(seen: str) -> str:
    """The statement that writes one row; `seen` is the SQL that gives the row's generation.

    A row for the same object and version keeps its upload time and its check results, a quarantine included. A new
    version is a new replica: it starts unchecked and unquarantined.
    """
    unchanged = "media_replicas.object = excluded.object AND media_replicas.version = excluded.version"

    def keep(column: str, fresh: str) -> str:
        return f"{column} = CASE WHEN {unchanged} THEN media_replicas.{column} ELSE {fresh} END"

    return ("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, state, seen, "
            f"ciphertext_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {seen}, ?) ON CONFLICT(binary_rel, store) DO UPDATE SET "
            "object = excluded.object, version = excluded.version, key_version = excluded.key_version, "
            "size = excluded.size, seen = excluded.seen, ciphertext_sha256 = excluded.ciphertext_sha256, "
            f"verified_at = CASE WHEN {unchanged} THEN COALESCE(excluded.verified_at, media_replicas.verified_at) "
            "ELSE excluded.verified_at END, "
            + keep("state", "excluded.state") + ", " + keep("checked_at", "0") + ", " + keep("level", "NULL") + ", "
            + keep("decrypted_at", "0") + ", " + keep("failure", "NULL") + ", " + keep("sample_month", "0") + ", "
            + keep("check_from", "0") + ", " + keep("sample_from", "0"))


#: A publication reads the generation in the same statement as its write, so a rebuild that starts in between cannot
#: strand the row below its own generation.
_UPSERT_PUBLISHED = _upsert("(SELECT value FROM media_replica_generation WHERE id = 1)")
_UPSERT_REBUILT = _upsert("?")


def record(vault: Path, binary_rel: str, replica: originals.StoredObject, *, verified_at: float | None) -> None:
    """Note a replica that a binding now lists. `verified_at` is its upload time; None when this write does not know it.

    A version that a finding recorded as damaged stays damaged for a filing that lists it later: the row is written
    quarantined with its request, in the same transaction, so a publication that raced the finding is never trusted.
    So is a copy of ciphertext that a finding recorded as decrypting wrong, under whatever name it was written
    (`_inherit`): a finding that lands before this transaction reaches the new version here, and one after it reaches
    the row through its digest (`quarantine_all`).
    """
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute(_UPSERT_PUBLISHED, (binary_rel, replica.store, replica.object, replica.version,
                                         replica.key_version, replica.ciphertext_size, verified_at, UPLOAD_VERIFIED,
                                         replica.ciphertext_sha256))
        conn.execute(_INHERIT_OF, {"digest": replica.ciphertext_sha256})
        changed = _quarantine_listing(conn, time.time(), replica, binary_rel=binary_rel)
    if changed:
        wake()


def quarantine(vault: Path, binary_rel: str, replica: originals.StoredObject, *, level: str, failure: str) -> None:
    """Mark `replica` quarantined: the `level` of the check that failed, and why. Nothing trusts it from now on."""
    quarantine_all(vault, [binary_rel], replica, level=level, failure=failure)


#: A request that is made again: pending, with no attempt spent and a new token.
_RENEW = "state = excluded.state, kind = excluded.kind, code = NULL, attempts = 0, retry_at = 0, token = excluded.token"


def _record_damage(conn: sqlite3.Connection, replica: originals.StoredObject, level: str, failure: str) -> None:
    """Record the stored version as damaged, or renew its record: every write gives it a new generation.

    A wrong plaintext also records the ciphertext digest, which a later finding of another cause on the version keeps.
    """
    conn.execute("INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256, "
                 "generation) VALUES (?, ?, ?, ?, ?, ?, random()) ON CONFLICT(store, object, version) DO UPDATE SET "
                 "level = excluded.level, failure = excluded.failure, generation = excluded.generation, "
                 "ciphertext_sha256 = COALESCE(excluded.ciphertext_sha256, media_damaged_versions.ciphertext_sha256)",
                 (replica.store, replica.object, replica.version, level, failure,
                  replica.ciphertext_sha256 if failure == PLAINTEXT_MISMATCH else None))


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
        _record_damage(conn, replica, level, failure)
        for binary_rel in binary_rels:
            conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, "
                         "state, seen, ciphertext_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, (SELECT value FROM "
                         "media_replica_generation WHERE id = 1), ?) ON CONFLICT(binary_rel, store) DO NOTHING",
                         (binary_rel, replica.store, replica.object, replica.version, replica.key_version,
                          replica.ciphertext_size, UPLOAD_VERIFIED, replica.ciphertext_sha256))
            if conn.execute(
                    "UPDATE media_replicas SET state = ?, level = ?, failure = ?, checked_at = ?, sample_from = ? "
                    "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ?",
                    (QUARANTINED, level, failure, now, now, binary_rel, replica.store, replica.object,
                     replica.version)).rowcount:
                conn.execute("INSERT INTO media_replica_queue(store, binary_rel, state, kind, token) "
                             "VALUES (?, ?, ?, ?, random()) "
                             f"ON CONFLICT(store, binary_rel) DO UPDATE SET {_RENEW}",
                             (replica.store, binary_rel, PENDING, RECOPY))
                changed = True
        if failure == PLAINTEXT_MISMATCH:
            conn.execute(_INHERIT_OF, {"digest": replica.ciphertext_sha256})
            changed |= _quarantine_listing(conn, now, digest=replica.ciphertext_sha256)
        else:
            # The record now names this cause, so the filings that a wrong plaintext quarantined follow it: left as they
            # were, a sample that decrypts the ciphertext would release a version that this finding holds lost or differing.
            conn.execute("UPDATE media_replicas SET level = ?, failure = ? WHERE object = ? AND version = ? AND store = ? "
                         f"AND state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}'",
                         (level, failure, replica.object, replica.version, replica.store))
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
        f"checked_at = ?, sample_from = ? WHERE state <> '{QUARANTINED}' AND {damaged}",
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
#: probe of `media_damaged_versions` for the version, and a probe of its digest index for the ciphertext, for each, and no
#: scan. The quadruples travel as one value, so the statement is the same for one and for thousands.
_RECORDED = ("FROM json_each(?) AS wanted WHERE EXISTS (SELECT 1 FROM media_damaged_versions "
             "WHERE store = json_extract(wanted.value, '$[0]') AND object = json_extract(wanted.value, '$[1]') "
             "AND version = json_extract(wanted.value, '$[2]')) OR EXISTS (SELECT 1 FROM media_damaged_versions "
             "WHERE ciphertext_sha256 = json_extract(wanted.value, '$[3]'))")


def _inherit(recorded: str) -> str:
    """The statement that records, for each filing of ciphertext that a finding recorded as decrypting wrong, its version.

    A filing of such bytes is damaged under its own name, whatever it is called: `_quarantine_listing` then quarantines
    it, so its row says so and the status, the findings and the recoverability read the row. `recorded` selects the
    digests, from the table that holds them; a version the table already holds keeps its record. A filing that is
    quarantined is never read (`media_replicas_digest`), so a repeated finding costs what it costs for one filing.
    """
    return ("INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256, generation) "
            f"SELECT filing.store, filing.object, filing.version, '{DECRYPTED}', '{PLAINTEXT_MISMATCH}', "
            f"filing.ciphertext_sha256, random() FROM ({recorded}) AS wrong CROSS JOIN media_replicas AS filing "
            f"ON filing.ciphertext_sha256 = wrong.digest WHERE filing.state <> '{QUARANTINED}' "
            "ON CONFLICT(store, object, version) DO NOTHING")


_INHERIT_OF = _inherit(
    "SELECT :digest AS digest WHERE EXISTS (SELECT 1 FROM media_damaged_versions WHERE ciphertext_sha256 = :digest)")
_INHERIT_ALL = _inherit("SELECT DISTINCT ciphertext_sha256 AS digest FROM media_damaged_versions "
                        "WHERE ciphertext_sha256 IS NOT NULL")
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


#: One replica as the table holds it, quarantined: the primary-key lookup that tells a copy again whether its finding stands.
_QUARANTINED_AT = ("SELECT 1 FROM media_replicas WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                   f"AND state = '{QUARANTINED}'")


def is_quarantined(vault: Path, binary_rel: str, replica: originals.StoredObject) -> bool:
    """Whether the table holds exactly this entry of this original as quarantined."""
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        return conn.execute(_QUARANTINED_AT, (binary_rel, replica.store, replica.object, replica.version)).fetchone() is not None


def _replicas_of(snapshot: originals.OriginalSnapshot) -> Iterator[originals.StoredObject]:
    """Every replica a binding lists, the eviction store's pointer first."""
    if snapshot.reference is not None:
        yield snapshot.reference
    yield from snapshot.replicas


def rebuild(vault: Path) -> int:
    """Fill the table from every binding and drop each row that no binding names; return the rows written.

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
    unread = 0

    def skip(error: originals.RetirementUnconfirmed) -> None:
        nonlocal unread
        unread += 1
        media_store.skip_unknown_original(error)

    written = 0
    totals: dict[str, list[int]] = {}  # per store: replicas and ciphertext bytes, for the check budget
    batch: list[tuple] = []

    def flush() -> None:
        with closing(ledger._connect()) as conn, conn:
            conn.executemany(_UPSERT_REBUILT, batch)
        batch.clear()

    for snapshot in foreground_priority.yielding(media_store.bound_originals(vault, unconfirmed=skip)):
        relative = snapshot.path.relative_to(vault).as_posix()
        for replica in _replicas_of(snapshot):
            batch.append((relative, replica.store, replica.object, replica.version, replica.key_version,
                          replica.ciphertext_size, None, UPLOAD_VERIFIED, generation, replica.ciphertext_sha256))
            held = totals.setdefault(replica.store, [0, 0])
            held[0] += 1
            held[1] += replica.ciphertext_size
            written += 1
        if len(batch) >= _SCAN_BATCH:
            flush()
    flush()
    with closing(ledger._connect()) as conn, conn:
        conn.execute("DELETE FROM media_replicas WHERE seen < ?", (generation,))
        # A filing that the walk wrote as sound but lists a damaged version is quarantined with it, and so is one that lists
        # a copy of ciphertext that decrypts wrong. The record of a damaged version is never forgotten here: the walk skips a
        # binding whose Trash retirement is unconfirmed and never enters the Trash, so a version that no row lists may still
        # be listed by a filing, and eviction trusts the record.
        conn.execute(_INHERIT_ALL)
        _quarantine_listing(conn, time.time())
        conn.execute("DELETE FROM media_replica_store_totals")
        conn.executemany("INSERT INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                         [(store, held[0], held[1]) for store, held in totals.items()])
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
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
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
    try:
        return {row["key_version"]: row["confirmed_at"]
                for row in conn.execute("SELECT key_version, confirmed_at FROM media_key_exports")}
    except sqlite3.OperationalError:
        return {}


def recoverability(config: dict[str, object], key_version: str, exports: dict[str, float], *,
                   quarantined: bool = False) -> tuple[bool | None, str | None]:
    """Whether a replica made under `key_version` survives the loss of this machine, and the reason when it does not.

    A quarantined replica does not: its bytes are damaged or absent, whoever holds the key. Otherwise it does when a
    platform holds the identity (the configuration says so) or the owner confirmed an export. A vault that lists its
    own stores is a personal install, where the owner's export is the only way, so an unconfirmed key reads as not
    recoverable. A vault with neither, such as a Cloud cell's, says nothing about custody: unknown.
    """
    if quarantined:
        return False, QUARANTINED
    if config.get(originals.KEY_CUSTODY) == originals.PLATFORM_CUSTODY or key_version in exports:
        return True, None
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


def observe(vault: Path) -> None:
    """Record whether each store that has a cheap check answers as itself now.

    Only the worker calls it (and the drain, when a copy meets a store that does not answer), so a status reports an
    observation with its time and never reads a store itself. A store with no cheap check (a remote one) is not
    probed and keeps no record.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return
    for name in originals.store_entries(config):
        if (answer := media_store.store_reachable(vault, name)) is not None:
            _note_reach(vault, name, answer, force=True)


def _observations(conn: sqlite3.Connection) -> dict[str, tuple[bool, float]]:
    try:
        return {row["store"]: (bool(row["reachable"]), row["observed_at"])
                for row in conn.execute("SELECT store, reachable, observed_at FROM media_replica_reach")}
    except sqlite3.OperationalError:
        return {}


# --- the id that the owner's declaration recorded for a store's marker ------------------------------


def declared_store_id(vault: Path, store: str) -> str | None:
    """The marker id recorded when the owner declared `store`; None when there is none on this machine.

    Without a record the marker's name alone identifies the store, as before a declaration; the owner declares the
    store again to record the id that its root carries, after a lost ledger as well.
    """
    from . import media_store

    ledger = MediaJobStore(vault, create=False)
    if not ledger.path.exists():
        return None
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_store_ids'").fetchone() is None:
                return None
            row = conn.execute("SELECT store_id FROM media_store_ids WHERE store = ?", (store,)).fetchone()
    except (sqlite3.Error, OSError) as error:
        # Not knowing the recorded id is not the same as there being none: the store is not served on a guess.
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE",
                                          f"the store's recorded identity cannot be read: {type(error).__name__}") from error
    return None if row is None else row["store_id"]


def record_store_id(vault: Path, store: str, store_id: str) -> None:
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_store_ids(store, store_id) VALUES (?, ?) "
                     "ON CONFLICT(store) DO UPDATE SET store_id = excluded.store_id", (store, store_id))


# --- status -----------------------------------------------------------------------------------------


#: Everything the aggregate counts, in one pass over `media_replicas_status`: the grouping is a prefix of that index and
#: every column read is in it, so the table itself is never touched. The parameter is the current UTC month.
_GROUPS = ("SELECT store, key_version, state, level, COUNT(*) AS replicas, COUNT(verified_at) AS timed, "
           "MIN(verified_at) AS oldest, MIN(NULLIF(checked_at, 0)) AS oldest_checked, "
           "SUM(decrypted_at > 0) AS decrypted, MIN(NULLIF(decrypted_at, 0)) AS oldest_decrypted, "
           "SUM(sample_month = ?) AS skipped, SUM(sample_month < 0) AS unsampleable "
           "FROM media_replicas GROUP BY store, key_version, state, level")

#: The replicas whose last attempt found something but no damage, by store and what it found: a range of the small
#: partial index `media_replicas_failing`, which holds only those rows.
_FAILING = ("SELECT store, failure, COUNT(*) AS replicas FROM media_replicas "
            f"WHERE state <> '{QUARANTINED}' AND failure IS NOT NULL GROUP BY store, failure")

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


def _count_checks(vault: Path, stats: dict[str, Any], entry: originals.StoreEntry | None, row: sqlite3.Row) -> None:
    """Add one group of the grouped query to its store's check values."""
    from . import media_store

    if row["state"] == QUARANTINED:
        stats["quarantined"] += row["replicas"]
        return
    if row["level"] in stats["checks"]:
        proof = stats["checks"][row["level"]]
        proof["replicas"] += row["replicas"]
        if row["oldest_checked"] is not None and (proof["oldest_at"] is None or row["oldest_checked"] < proof["oldest_at"]):
            proof["oldest_at"] = row["oldest_checked"]
    elif (why := _unscheduled_reason(entry)) is not None:
        waiting = stats["checks_not_scheduled"] or {"replicas": 0, "reasons": []}
        stats["checks_not_scheduled"] = {"replicas": waiting["replicas"] + row["replicas"],
                                         "reasons": sorted({*waiting["reasons"], why})}
    else:
        stats["never_checked"] += row["replicas"]
    proof = stats["checks"][DECRYPTED]
    proof["replicas"] += row["decrypted"]
    if row["oldest_decrypted"] is not None and (proof["oldest_at"] is None or row["oldest_decrypted"] < proof["oldest_at"]):
        proof["oldest_at"] = row["oldest_decrypted"]
    # The reason a replica is not sampled is a property of its store and key version, so it is read here from the
    # group, never from a store. A store whose checks are not scheduled is not sampled either: one rule for both.
    reason = (_unscheduled_reason(entry) or (SAMPLING_OFF if entry.sample_bytes_per_month <= 0
              else media_store.unreadable_reason(vault, row["store"], row["key_version"])))
    if reason is None:
        stats["sample"]["skipped_for_size"] += row["skipped"]
        reason, replicas = NO_EXACT_DIGEST, row["unsampleable"]
    else:
        replicas = row["replicas"]
    if replicas:
        listed = {item["reason"]: item for item in stats["sample"]["not_sampled"]}
        listed.setdefault(reason, {"reason": reason, "replicas": 0})["replicas"] += replicas
        stats["sample"]["not_sampled"] = sorted(listed.values(), key=lambda item: item["reason"])


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
    """The aggregate of every store's replicas: one grouped query over the indexed table.

    Counts are replicas this vault's bindings list, as of the last rebuild plus every publication since.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return {"state": "unavailable", "reason": "no media store is configured", "as_of": None, "stores": None,
                "totals": None}
    entries = originals.store_entries(config)
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:  # a read never recreates a table
            census = _census(conn)
            exports = _exports(conn)
            month = _month(time.time())
            groups = conn.execute(_GROUPS, (month,)).fetchall()
            failing = conn.execute(_FAILING).fetchall()
            spent = {row["store"]: row["bytes"] for row in conn.execute(
                "SELECT store, bytes FROM media_replica_samples WHERE month = ?", (month,))}
            queue = conn.execute("SELECT store, state, COUNT(*) AS n FROM media_replica_queue WHERE kind = ? "
                                 "GROUP BY store, state", (REPLICATE,)).fetchall()
            reach = _observations(conn)
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}")
    if census is None:
        return _unknown("the replica table is being rebuilt from the bindings")
    names = list(entries)
    names.extend(sorted({row["store"] for row in groups} - set(entries)))
    stores: dict[str, dict[str, Any]] = {}
    for name in names:
        entry = entries.get(name)
        refusal = media_store.connection_unusable(entry) if entry is not None else None
        seen_reachable, observed_at = reach.get(name, (None, None))
        stores[name] = {
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
            # The worker's last observation, never a read of the store: None when it has made none (a remote store
            # has no cheap check, and a vault without a running worker has no observation).
            "reachable": seen_reachable,
            "reachable_observed_at": _iso(observed_at),
            "replicas": 0, "verified_at_upload": 0, "recoverable": 0, "not_recoverable": 0,
            "recoverability_unknown": 0, "not_recoverable_reasons": [], "recoverability_unknown_reasons": [],
            "oldest_verified_at": None, "verification_time_unknown": 0,
            **_check_counts(), "sample": _sample_budget(entry, spent.get(name, 0), month),
        }
    for row in groups:
        stats = stores[row["store"]]
        stats["replicas"] += row["replicas"]
        if row["state"] == UPLOAD_VERIFIED:
            stats["verified_at_upload"] += row["replicas"]
        stats["verification_time_unknown"] += row["replicas"] - row["timed"]
        if row["oldest"] is not None and (stats["oldest_verified_at"] is None or row["oldest"] < stats["oldest_verified_at"]):
            stats["oldest_verified_at"] = row["oldest"]
        _count_checks(vault, stats, entries.get(row["store"]), row)
        recoverable, reason = recoverability(config, row["key_version"], exports, quarantined=row["state"] == QUARANTINED)
        if recoverable is True:
            stats["recoverable"] += row["replicas"]
            continue
        # A copy that is known not to survive and one whose custody nobody stated are different answers.
        counted, reasons = (("not_recoverable", "not_recoverable_reasons") if recoverable is False
                            else ("recoverability_unknown", "recoverability_unknown_reasons"))
        stats[counted] += row["replicas"]
        if reason is not None and reason not in stats[reasons]:
            stats[reasons].append(reason)
    for row in failing:
        if row["store"] in stores:
            stores[row["store"]]["failed_attempts"].append({"failure": row["failure"], "replicas": row["replicas"]})
    progress: dict[str, dict[str, int]] = {}
    for row in queue:
        progress.setdefault(row["store"], {})[row["state"]] = row["n"]
    for name, counts in progress.items():
        if name in stores:
            queued = sum(counts.values())
            stores[name]["copy"] = {"queued": queued, "copied": counts.get(DONE, 0), "refused": counts.get(REFUSED, 0),
                                    "remaining": counts.get(PENDING, 0)}
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
    return {"state": "measured", "reason": None, "as_of": _iso(census["rebuilt_at"]),
            "unread_originals": census["unread"], "source": "media ledger table, rebuilt from the bindings",
            "stores": list(stores.values()), "totals": totals}


def _check_view(row: sqlite3.Row) -> dict[str, object]:
    """What a replica's checks proved, as an owner reads it: the last scheduled check's level and time, the last
    decrypt proof, and the finding of a quarantined replica. A replica never checked has no check, not a zero time."""
    quarantined = row["state"] == QUARANTINED
    return {
        "last_check": ({"level": row["level"], "at": _iso(row["checked_at"])}
                       if row["level"] is not None and not quarantined else None),
        "decrypted_at": _iso(row["decrypted_at"] or None),
        "finding": ({"failed_level": row["level"], "failure": row["failure"], "at": _iso(row["checked_at"])}
                    if quarantined else None),
        # What the last check or sample attempt found when it could not prove the replica and found no damage.
        "failed_attempt": None if quarantined else row["failure"],
    }


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
            census = _census(conn)
            exports = _exports(conn)
            paths = [row[0] for row in conn.execute(
                "SELECT DISTINCT binary_rel FROM media_replicas WHERE binary_rel > ? ORDER BY binary_rel LIMIT ?",
                (after or "", limit + 1))]
            rows = conn.execute(
                "SELECT binary_rel, store, object, version, key_version, verified_at, state, level, checked_at, "
                "decrypted_at, failure FROM media_replicas WHERE binary_rel > ? AND binary_rel <= ? "
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
        recoverable, reason = recoverability(config, row["key_version"], exports, quarantined=row["state"] == QUARANTINED)
        listed.setdefault(row["binary_rel"], []).append({
            "store": row["store"], "state": row["state"], "verified_at": _iso(row["verified_at"]),
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": row["key_version"],
            **_check_view(row),
        })
    return {"state": "measured", "reason": None, "as_of": _iso(census["rebuilt_at"]),
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
            census = _census(conn)
            rows = conn.execute(_FINDINGS, (*(after or ("", "")), limit + 1)).fetchall()
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}", findings=None, next_after=None)
    if census is None:
        return _unknown("the replica table is being rebuilt from the bindings", findings=None, next_after=None)
    shown = rows[:limit]
    return {"state": "measured", "reason": None, "as_of": _iso(census["rebuilt_at"]),
            "findings": [{
                "path": row["binary_rel"], "store": row["store"], "object": row["object"], "version": row["version"],
                "failed_level": row["level"], "failure": row["failure"], "found_at": _iso(row["checked_at"]),
                "recopy": None if row["queue_state"] is None else {"state": row["queue_state"], "code": row["queue_code"]},
            } for row in shown],
            "next_after": [shown[-1]["binary_rel"], shown[-1]["store"]] if len(rows) > limit else None}


def original_view(vault: Path, path: str) -> dict[str, object]:
    """One original's replicas, read from its binding: no table and no walk, so it never reads as stale."""
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None:
        return {"state": "unavailable", "reason": "no media store is configured", "replicas": None}
    snapshot = originals.original_state(vault, path)
    entries = originals.store_entries(config)
    ledger = MediaJobStore(vault, create=False)  # a read never recreates a table
    exports: dict[str, float] = {}
    recorded: dict[str, sqlite3.Row] = {}
    if ledger.path.exists():  # With no ledger the binding alone lists the replicas, and nothing is confirmed.
        with closing(ledger._connect(readonly=True)) as conn:
            exports = _exports(conn)
            try:
                recorded = {row["store"]: row for row in conn.execute(
                    "SELECT store, object, version, verified_at, state, level, checked_at, decrypted_at, failure "
                    "FROM media_replicas WHERE binary_rel = ?", (snapshot.path.relative_to(vault).as_posix(),))}
            except sqlite3.OperationalError:
                recorded = {}
    found = []
    for replica in _replicas_of(snapshot):
        entry = entries.get(replica.store)
        row = recorded.get(replica.store)
        # A row that names another version describes a replica that the binding no longer lists: no check applies.
        checked = row if row is not None and (row["object"], row["version"]) == (replica.object, replica.version) else None
        recoverable, reason = recoverability(config, replica.key_version, exports,
                                             quarantined=checked is not None and checked["state"] == QUARANTINED)
        found.append({
            "store": replica.store, "state": UPLOAD_VERIFIED if checked is None else checked["state"],
            "verified_at": _iso(row["verified_at"]) if row is not None else None,
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": replica.key_version,
            **(_check_view(checked) if checked is not None else {"last_check": None, "decrypted_at": None, "finding": None,
                                                              "failed_attempt": None}),
        })
    return {"state": "measured", "reason": None, "original_state": snapshot.state, "replicas": found}


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
        active = conn.execute(
            "SELECT 1 FROM media_replica_demands WHERE store = ? AND (scanned_at IS NULL OR EXISTS "
            "(SELECT 1 FROM media_replica_queue WHERE state = ? AND store = ? AND kind = ?))",
            (store, PENDING, store, REPLICATE)).fetchone()
        if active is None:
            conn.execute("DELETE FROM media_replica_queue WHERE store = ? AND state = ? AND kind = ?",
                         (store, DONE, REPLICATE))
            conn.execute("UPDATE media_replica_queue SET state = ?, attempts = 0, retry_at = 0, code = NULL, "
                         "token = random() WHERE store = ? AND state = ? AND kind = ?",
                         (PENDING, store, REFUSED, REPLICATE))
            conn.execute("INSERT INTO media_replica_demands(store, requested_at, scanned_at) VALUES (?, ?, NULL) "
                         "ON CONFLICT(store) DO UPDATE SET requested_at = excluded.requested_at, scanned_at = NULL",
                         (store, now))
    wake()
    return progress(vault, store)


def progress(vault: Path, store: str) -> dict[str, object]:
    """The counts of one store's copy queue: queued, copied, refused and remaining, and whether the scan finished."""
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        counts = {row["state"]: row["n"] for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM media_replica_queue WHERE store = ? AND kind = ? GROUP BY state",
            (store, REPLICATE))}
        scanned = conn.execute("SELECT scanned_at FROM media_replica_demands WHERE store = ?", (store,)).fetchone()
    return {"store": store, "queued": sum(counts.values()), "copied": counts.get(DONE, 0),
            "refused": counts.get(REFUSED, 0), "remaining": counts.get(PENDING, 0),
            "scan": "complete" if scanned is not None and scanned["scanned_at"] is not None else "running"}


def scan(vault: Path, store: str) -> int:
    """Queue each original whose binding lists no replica in `store`; return the number queued.

    The walk holds no guard: a copy re-reads its binding under the guard, so a binding that changed after the scan
    is the copy's retry, not a wrong replica.
    """
    from . import media_store

    ledger = MediaJobStore(vault)
    queued = 0
    batch: list[tuple[str, str]] = []

    def flush() -> None:
        with closing(ledger._connect()) as conn, conn:
            conn.executemany("INSERT INTO media_replica_queue(store, binary_rel, state, token) "
                             "VALUES (?, ?, 'pending', random()) ON CONFLICT(store, binary_rel) DO NOTHING", batch)
        batch.clear()

    for snapshot in foreground_priority.yielding(media_store.bound_originals(
            vault, unconfirmed=media_store.skip_unknown_original)):
        if any(replica.store == store for replica in _replicas_of(snapshot)):
            continue
        batch.append((store, snapshot.path.relative_to(vault).as_posix()))
        queued += 1
        if len(batch) >= _SCAN_BATCH:
            flush()
    flush()
    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replica_demands SET scanned_at = ? WHERE store = ?", (time.time(), store))
    return queued


def _pick(paused: set[str]) -> str:
    """The query for the next pending original outside the paused stores: `(state, store, binary_rel)` order, no sort."""
    skipped = f" AND store NOT IN ({', '.join('?' for _ in paused)})" if paused else ""
    return ("SELECT store, binary_rel, attempts, kind, token FROM media_replica_queue WHERE state = ? AND retry_at <= ?"
            + skipped + " ORDER BY store, binary_rel LIMIT 1")


def drain(vault: Path, *, deadline: float | None = None) -> int:
    """Copy up to a batch of queued originals; return how many it settled (copied or refused)."""
    from . import media_store

    vault = Path(vault).resolve()
    ledger = MediaJobStore(vault)
    settled = 0
    limit = time.monotonic() + _DRAIN_SECONDS if deadline is None else deadline
    paused: set[str] = set()
    while settled < _DRAIN_BATCH and time.monotonic() < limit:
        with closing(ledger._connect(readonly=True)) as conn:
            row = conn.execute(_pick(paused), (PENDING, time.time(), *paused)).fetchone()
        if row is None:
            break
        foreground_priority.yield_to_foreground()
        if (answer := media_store.store_reachable(vault, row["store"])) is not None:
            _note_reach(vault, row["store"], answer)
        if answer is False:
            # The store does not answer as itself: an outage, which spends no attempt and writes no refusal. Its
            # queue waits for the next round, and the other stores go on.
            paused.add(row["store"])
            continue
        code = None
        try:
            if row["kind"] == RECOPY:
                media_store.recopy(vault, vault / row["binary_rel"], row["store"])
            else:
                media_store.replicate(vault, vault / row["binary_rel"], row["store"])
        except media_store.MediaStoreError as error:
            code = error.code
        except (OSError, ValueError, sqlite3.Error) as error:
            code = type(error).__name__
        if code is None:
            _settle_done(ledger, row)
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
                with closing(ledger._connect()) as conn, conn:
                    conn.execute("UPDATE media_replica_queue SET attempts = ?, code = ?, retry_at = ? "
                                 "WHERE store = ? AND binary_rel = ? AND token = ?",
                                 (attempts, code, time.time() + _RETRY_SECONDS * attempts, row["store"], row["binary_rel"],
                                  row["token"]))
            if code == "MEDIA_STORE_UNAVAILABLE":
                paused.add(row["store"])  # A store that failed mid-copy fails the same way for the next original.
        else:
            _settle(ledger, row, REFUSED, code)
            settled += 1
    return settled


def _settle_done(ledger: MediaJobStore, row: sqlite3.Row) -> None:
    """A finished copy leaves a done row for the scan's progress; a finished copy again leaves nothing to count."""
    if row["kind"] != RECOPY:
        _settle(ledger, row, DONE, None)
        return
    with closing(ledger._connect()) as conn, conn:
        conn.execute("DELETE FROM media_replica_queue WHERE store = ? AND binary_rel = ? AND token = ?",
                     (row["store"], row["binary_rel"], row["token"]))


def _settle(ledger: MediaJobStore, row: sqlite3.Row, state: str, code: str | None, *, attempts: int | None = None) -> None:
    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replica_queue SET state = ?, code = ?, attempts = COALESCE(?, attempts) "
                     "WHERE store = ? AND binary_rel = ? AND token = ?",
                     (state, code, attempts, row["store"], row["binary_rel"], row["token"]))


_observed: dict[Path, float] = {}
_checked: dict[Path, float] = {}  # when each vault last ran a check round


def work(vault: Path, *, deadline: float | None = None) -> bool:
    """One bounded unit of background work: a rebuild, a scan, or a drain and the check round that is due; True when it did something."""
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return False
    if time.monotonic() - _observed.get(vault, -_OBSERVE_SECONDS) >= _OBSERVE_SECONDS:
        _observed[vault] = time.monotonic()
        observe(vault)
    if rebuild_due(vault):
        rebuild(vault)
        return True
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        waiting = [row["store"] for row in conn.execute(
            "SELECT store FROM media_replica_demands WHERE scanned_at IS NULL ORDER BY requested_at")]
    if waiting:
        scan(vault, waiting[0])
        return True
    settled = drain(vault, deadline=deadline)
    # Verification does not wait for an empty queue: a backlog of copies would otherwise hold every check and sample
    # back for as long as it lasts, with replicas overdue. A round costs one bounded drain and one bounded check.
    if time.monotonic() - _checked.get(vault, -_CHECK_INTERVAL) < _CHECK_INTERVAL:
        return settled > 0
    _checked[vault] = time.monotonic()
    return settled + check(vault) + sample(vault) > 0


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

    def ready(self) -> bool:
        """False when the wait that earlier reads left owing outlasts this round; else waits it out, and the read may go."""
        if self.due >= self.limit:
            return False
        foreground_priority.wait_for_retry(max(0.0, self.due - time.monotonic()))
        return True

    @contextmanager
    def reading(self, size: int):
        """Account for a read of `size` bytes that starts now, whether it verified, failed or was cut off.

        The read's time counts from its start, so the read itself is part of it and only what remains is waited out.
        A failed read is charged its whole size: how much it moved before it failed is unknown, and a store that
        fails after most of the bytes must not be read faster than the cap for it. A `ValueError` is the store
        refusing the locator before it reads anything, so it costs nothing.
        """
        started, charged = time.monotonic(), True
        try:
            yield
        except ValueError:
            charged = False
            raise
        finally:
            if charged and size > 0:
                self.due = max(self.due, started) + size / self.rate
                if self.key is not None:
                    _owed[self.key] = self.due


def scheduled_level(entry: originals.StoreEntry) -> str | None:
    """The check that a store's descriptor allows: content when its `head` recomputes, presence when it does not.

    A descriptor that does not state it, or a store that is not usable, gets none: a check of unknown strength would
    report a proof that it cannot name. The decision reads the descriptor value and never a store's or adapter's name.
    """
    if not entry.available or entry.head_recomputes is None:
        return None
    return CONTENT if entry.head_recomputes else PRESENCE


_BINDING_UNREADABLE = (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)


def _bound(vault: Path, row: sqlite3.Row) -> tuple[originals.OriginalSnapshot, originals.StoredObject] | str:
    """The binding and the entry that it lists for the row's store now, else the finding that says why there is none.

    The table follows the binding when they differ. The finding is `UNREADABLE` for a binding that cannot be read now,
    and `UNLISTED` for one that lists no replica in the store (the next rebuild drops the row).
    """
    try:
        snapshot = originals.original_state(vault, row["binary_rel"])
    except _BINDING_UNREADABLE:
        return UNREADABLE
    entry = next((found for found in _replicas_of(snapshot) if found.store == row["store"]), None)
    if entry is None:
        return UNLISTED
    if (entry.object, entry.version) != (row["object"], row["version"]):
        record(vault, row["binary_rel"], entry, verified_at=None)  # a newer version, which starts unchecked
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
_DUE = ("SELECT binary_rel, store, object, version, check_from FROM media_replicas "
        f"WHERE store = ? AND state <> '{QUARANTINED}' AND check_from <= ? ORDER BY check_from LIMIT ?")


def _due(ledger: MediaJobStore, store: str, cycle: float, limit: int) -> list[sqlite3.Row]:
    with closing(ledger._connect(readonly=True)) as conn:
        return conn.execute(_DUE, (store, time.time() - cycle, limit)).fetchall()


def _store_budget(ledger: MediaJobStore, store: str, cycle: float) -> tuple[int, int]:
    """The replicas and the bytes that one round may check in `store`: its share of the cycle, as of the last rebuild."""
    from . import original_fixity

    with closing(ledger._connect(readonly=True)) as conn:
        held = conn.execute("SELECT replicas, bytes FROM media_replica_store_totals WHERE store = ?", (store,)).fetchone()
    replicas, size = (held["replicas"], held["bytes"]) if held is not None else (0, 0)
    return (max(_CHECK_FLOOR_ROWS, math.ceil(replicas * _CHECK_INTERVAL / cycle)),
            original_fixity.budget_for([size], interval=_CHECK_INTERVAL, cycle=cycle))


def _round_seconds(ledger: MediaJobStore, content_stores: list[str], cycle: float, rate: float) -> float:
    """How long a check round may run to read the byte shares of all its content stores at the one read cap."""
    shares = sum(_store_budget(ledger, name, cycle)[1] for name in content_stores)
    return min(_CHECK_ROUND_MAX, max(_CHECK_ROUND_SECONDS, _CHECK_ROUND_FACTOR * shares / rate))


def _longest_waiting_first(ledger: MediaJobStore, stores: list[str], cycle: float) -> list[str]:
    """The stores in the order of their oldest due replica, a store with nothing due last.

    A round that the shared cap ends before its last store would leave that store behind every time in a fixed order.
    Here the store that was left waits longest, so it comes first in the next round: the order rotates by itself, and
    keeps no state to lose.
    """
    waiting = {name: (due[0]["check_from"] if (due := _due(ledger, name, cycle, 1)) else math.inf) for name in stores}
    return sorted(stores, key=waiting.__getitem__)


def check(vault: Path, *, deadline: float | None = None) -> int:
    """One bounded round of scheduled checks over every store whose descriptor allows one; return the replicas attempted.

    Each store takes the replicas attempted longest ago first, a pick at a time, until its share of the cycle is spent
    (replicas, and for a content check bytes under the read-rate cap) or the round's time is. It holds no vault guard: a
    check reads a store and writes one derived row. A replica whose binding or read fails is recorded and skipped; a
    store that does not answer as itself, or fails `_STORE_FAILURES` replicas in a row, ends its own round and changes
    no row that it did not attempt. The store that has waited longest goes first.
    """
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return 0
    ledger = MediaJobStore(vault)
    cycle, rate = check_cycle(), check_read_rate()
    levels = {name: level for name, entry in originals.store_entries(config).items()
              if (level := scheduled_level(entry)) is not None}
    content = [name for name, level in levels.items() if level == CONTENT]
    limit = time.monotonic() + _round_seconds(ledger, content, cycle, rate) if deadline is None else deadline
    cap = _ReadCap(rate, limit, (vault, "check"))
    return sum(_check_store(vault, ledger, name, levels[name], cycle, cap, limit)
               for name in _longest_waiting_first(ledger, list(levels), cycle))


def _check_store(vault: Path, ledger: MediaJobStore, name: str, level: str, cycle: float, cap: _ReadCap,
                 limit: float) -> int:
    """One store's part of a check round: the replicas attempted."""
    from . import media_store

    if (answer := media_store.store_reachable(vault, name)) is not None:
        _note_reach(vault, name, answer)
    if answer is False:
        return 0
    rows, budget = _store_budget(ledger, name, cycle)
    attempted = spent = failing = 0
    while attempted < rows:
        picked = _due(ledger, name, cycle, min(rows - attempted, _CHECK_BATCH))
        if not picked:
            break
        for row in picked:
            if time.monotonic() >= limit or (level == CONTENT and (spent >= budget or not cap.ready())):
                return attempted
            foreground_priority.yield_to_foreground()
            attempted += 1
            bound = _bound(vault, row)
            if isinstance(bound, str):
                _failed(vault, row, bound)
                continue
            replica = bound[1]
            size = replica.ciphertext_size if level == CONTENT else 0  # a presence check moves no object
            spent += size
            try:
                with cap.reading(size):
                    failure = media_store.check_replica(vault, replica)
            except ValueError:
                spent -= size  # refused before it read anything
                _failed(vault, row, RECORD_INVALID, attempted=replica)  # the store answered; it cannot address this version
                continue
            except media_store.MediaStoreError:
                if media_store.store_reachable(vault, name) is False:
                    _note_reach(vault, name, False)  # the store went away mid-round: an outage, and no row's finding
                    return attempted - 1
                _failed(vault, row, READ_FAILED, attempted=replica)  # unavailable is no proof of loss: not quarantined
                failing += 1
                if failing >= _STORE_FAILURES:
                    return attempted
                continue
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
#: `sample_month` of a row that no sample can read (its binding lists no exact plaintext digest, or cannot be read).
_UNSAMPLEABLE = -1

#: The replica of a store whose sample turn is oldest, never-sampled first, among those not skipped this month and
#: whose turn the cycle has made due: a range of `media_replicas_sample` in its own order. The key version is the one
#: whose identity this install holds. A replica is sampled once per cycle, so a small store does not spend its budget twice.
_SAMPLE = ("SELECT binary_rel, store, object, version, size FROM media_replicas "
           f"WHERE store = ? AND key_version = ? AND state <> '{QUARANTINED}' AND sample_month = 0 "
           "AND sample_from <= ? ORDER BY sample_from LIMIT 1")
#: The quarantined replica that a sample may release, whose turn comes first: a range of `media_replicas_reproof`, whose
#: every row a plaintext finding still holds (a finding of another cause moves its filings out of it, `quarantine_all`).
#: It also reads the generation of the version's damage record (see `_released`).
_REPROOF = ("SELECT binary_rel, store, object, version, size, (SELECT generation FROM media_damaged_versions AS d "
            "WHERE d.store = media_replicas.store AND d.object = media_replicas.object "
            "AND d.version = media_replicas.version) AS generation "
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


def _mark_sample(vault: Path, row: sqlite3.Row, month: int, *, attempted: originals.StoredObject | None = None) -> None:
    """Take a replica out of the sample picks for `month` (or for good, for `_UNSAMPLEABLE`).

    It lands on the version that was looked at (`attempted`, else the row's own), as `_failed` does.
    """
    name, version = (attempted.object, attempted.version) if attempted is not None else (row["object"], row["version"])
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET sample_month = ? WHERE binary_rel = ? AND store = ? "
                     "AND object = ? AND version = ?", (month, row["binary_rel"], row["store"], name, version))


def _proved(vault: Path, row: sqlite3.Row, replica: originals.StoredObject, store: str) -> None:
    """Record a sample that matched: it clears every finding of an attempt, and a replica quarantined meanwhile stays so."""
    now = time.time()
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET decrypted_at = ?, sample_from = ?, failure = NULL "
                     "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                     f"AND state <> '{QUARANTINED}'", (now, now, row["binary_rel"], store, replica.object, replica.version))


def _released(vault: Path, row: sqlite3.Row, replica: originals.StoredObject) -> None:
    """A quarantined replica whose download decrypted to the binding was a false alarm: it leaves quarantine, unchecked.

    The proof answers the damage that the sample picked, and the version's record names that damage by its generation:
    every finding on the version, of any cause and from any filing, gives the record a new one (`_record_damage`). Only
    a record that is still the one the sample read is released, in one transaction. The ciphertext decrypted, so every
    filing of it under any name that a wrong plaintext quarantined is released too, with the copies again they queued,
    and no record of a wrong plaintext of it is left; a newer finding leaves all of it, this replica included,
    quarantined with its request. A filing that holds another cause stays: its version is lost or differs, which this
    download does not answer. Released rows are unchecked, so the scheduled check proves them again.
    """
    now = time.time()
    store, name, version = row["store"], replica.object, replica.version
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        if not conn.execute(
                "UPDATE media_replicas SET state = ?, level = NULL, failure = NULL, checked_at = 0, check_from = 0, "
                "decrypted_at = ?, sample_from = ? WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                f"AND state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}' "
                "AND (SELECT generation FROM media_damaged_versions WHERE store = ? AND object = ? AND version = ?) IS ?",
                (UPLOAD_VERIFIED, now, now, row["binary_rel"], store, name, version, store, name, version,
                 row["generation"])).rowcount:
            return
        conn.execute("DELETE FROM media_replica_queue WHERE store = ? AND binary_rel = ? AND kind = ?",
                     (store, row["binary_rel"], RECOPY))
        # The ciphertext decrypted to the binding, so no copy of it is damage: the filings that a wrong plaintext of it
        # quarantined, under this version or another, and the copies they queued, go, or each would upload a copy of
        # bytes just proven good. A record of another cause on such a version stays and only loses the digest.
        digest = replica.ciphertext_sha256
        same = (f"WHERE state = '{QUARANTINED}' AND failure = '{PLAINTEXT_MISMATCH}' AND (object, version, store) IN "
                "(SELECT object, version, store FROM media_damaged_versions "
                f"WHERE ciphertext_sha256 = ? AND failure = '{PLAINTEXT_MISMATCH}')")
        conn.execute("DELETE FROM media_replica_queue WHERE kind = ? AND (store, binary_rel) IN "
                     f"(SELECT store, binary_rel FROM media_replicas {same})", (RECOPY, digest))
        conn.execute("UPDATE media_replicas SET state = ?, level = NULL, failure = NULL, checked_at = 0, "
                     f"check_from = 0, decrypted_at = ?, sample_from = ? {same}", (UPLOAD_VERIFIED, now, now, digest))
        conn.execute("DELETE FROM media_damaged_versions WHERE ciphertext_sha256 = ? AND failure = ?",
                     (digest, PLAINTEXT_MISMATCH))
        conn.execute("UPDATE media_damaged_versions SET ciphertext_sha256 = NULL WHERE ciphertext_sha256 = ?", (digest,))


def _charge(conn: sqlite3.Connection, store: str, month: int, size: int) -> None:
    conn.execute("INSERT INTO media_replica_samples(store, month, bytes) VALUES (?, ?, ?) "
                 "ON CONFLICT(store, month) DO UPDATE SET bytes = bytes + excluded.bytes", (store, month, size))


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
    limit = time.monotonic() + _CHECK_ROUND_SECONDS if deadline is None else deadline
    cap, cycle = _ReadCap(check_read_rate(), limit, (vault, "sample")), check_cycle()
    month, key_version, sampled = _month(time.time()), str(config.get("key_version")), 0
    for name, entry in originals.store_entries(config).items():
        if (_unscheduled_reason(entry) is not None or entry.sample_bytes_per_month <= 0
                or media_store.unreadable_reason(vault, name, key_version) is not None):
            continue
        if (answer := media_store.store_reachable(vault, name)) is not None:
            _note_reach(vault, name, answer)
        if answer is False:
            continue
        with closing(ledger._connect()) as conn, conn:
            conn.execute(_SAMPLE_RESET, (name, key_version, month, _SAMPLE_RELEASE))
        reads = skips = failing = 0
        second_look = True  # a quarantined replica comes first, once per round
        while reads < _SAMPLE_READS and skips < _SAMPLE_SKIPS and time.monotonic() < limit:
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
            if row["size"] > remaining:  # a size of 0 is not yet known: the binding gives it below
                if not quarantined:
                    _mark_sample(vault, row, month)
                    skips += 1
                continue
            foreground_priority.yield_to_foreground()
            bound = _bound(vault, row)
            if isinstance(bound, str):
                _failed(vault, row, bound, sample=True)  # a binding that cannot be read now is no reason to give up on the row
                skips += 1
                continue
            snapshot, replica = bound
            if snapshot.binary_sha256 is None or snapshot.binary_size is None:
                # the binding is readable and lists no plaintext digest: nothing to hash against
                _mark_sample(vault, row, _UNSAMPLEABLE, attempted=replica)
                skips += 1
                continue
            if replica.ciphertext_size > remaining:
                if not quarantined:
                    _mark_sample(vault, row, month, attempted=replica)
                    skips += 1
                continue
            if not cap.ready():
                break  # an earlier round's read is not yet paid for: nothing is charged or read until it is
            with closing(ledger._connect()) as conn, conn:
                _charge(conn, name, month, replica.ciphertext_size)
            reads += 1
            sampled += 1
            try:
                with cap.reading(replica.ciphertext_size):
                    damage = media_store.sample_replica(
                        vault, replica, media_store.ObjectHead(snapshot.binary_size, snapshot.binary_sha256))
            except media_store.KeyRefused:
                # the bytes match their record: no damage
                _failed(vault, row, media_store.KEY_REFUSED, attempted=replica, sample=True)
                failing += 1
            except ValueError:
                # The store refused the locator before it read anything, so nothing was downloaded and nothing is billed.
                with closing(ledger._connect()) as conn, conn:
                    _charge(conn, name, month, -replica.ciphertext_size)
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
                    _proved(vault, row, replica, name)
            if failing >= _STORE_FAILURES:
                break  # a store or key that fails three in a row is the likelier cause; the next round tries again
    return sampled


# --- the thread -------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None


def wake() -> None:
    """Tell the background thread that new work is queued."""
    _wake.set()


def start(vault_root: Path) -> threading.Thread | None:
    """Start the replica worker. Idempotent: a second call returns the live one."""
    global _thread
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            if _stop.is_set():
                raise RuntimeError("the replica worker is stopping")
            return _thread
        _stop.clear()
        thread = threading.Thread(target=_run, args=(Path(vault_root).resolve(),), name="exomem-media-replicas",
                                  daemon=True)
        _thread = thread
        thread.start()
        return thread


def stop(timeout: float = 2.0, *, worker: threading.Thread | None = None) -> None:
    """Stop the worker and wait briefly; a drain stops at its next original."""
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


def _run(vault: Path) -> None:
    while not _stop.is_set():
        busy = False
        try:
            with foreground_priority.bulk(stop=_stop):
                busy = work(vault)
        except foreground_priority.BulkCancelled:
            return
        except Exception:  # noqa: BLE001 - a failed unit must not end the worker or deny the service
            log.warning("media replicas: a unit of work failed; the worker tries again", exc_info=True)
        if not busy:
            _wake.wait(_IDLE_SECONDS)
        _wake.clear()
