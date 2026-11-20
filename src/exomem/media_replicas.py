"""Replica custody in the media ledger: where each original's replicas are, and how protected it is.

The sidecar binding is the record: `stored_object` and `replicas` (`originals.REPLICAS`) list what the vault holds.
The tables here are derived from those bindings and rebuild from them, so a lost ledger loses no custody fact. The
exceptions are the owner's key-export confirmations, which a lost ledger turns back into "not exported", and the
copy queue, which the owner starts again with `replicate`.

Status reads one grouped query over an indexed table and never walks the bindings. A table that is missing, or that
no rebuild has completed, shows every value as unknown.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing
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
#: The least time between two check rounds of a vault, the longest one runs, the replicas one pick takes, and the
#: replicas a round may check in a store however small it is, so a small vault still finishes its cycle.
_CHECK_INTERVAL = 600.0
_CHECK_ROUND_SECONDS = 120.0
_CHECK_BATCH = 64
_CHECK_FLOOR_ROWS = 16


#: Columns that a table made by an earlier release lacks. `0` in a time column means never.
_ADDED_COLUMNS = (
    ("size", "INTEGER NOT NULL DEFAULT 0"),  # ciphertext bytes, which the check and sample budgets spend
    ("checked_at", "REAL NOT NULL DEFAULT 0"),  # the last scheduled check, passed or failed
    ("level", "TEXT"),  # what that check proved, or the level that failed for a quarantined row
    ("decrypted_at", "REAL NOT NULL DEFAULT 0"),  # the last sample whose plaintext hashed to the binding
    ("failure", "TEXT"),  # why a quarantined row failed
    ("sample_month", "INTEGER NOT NULL DEFAULT 0"),  # the UTC month (YYYYMM) in which a sample skipped this row for size
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
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_due ON media_replicas(store, checked_at) "
                 f"WHERE state <> '{QUARANTINED}'")
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_sample ON media_replicas("
                 "store, key_version, sample_month, decrypted_at) "
                 f"WHERE state <> '{QUARANTINED}'")
    # The quarantined rows, which the findings list reads in path order.
    conn.execute("CREATE INDEX IF NOT EXISTS media_replicas_findings ON media_replicas(binary_rel, store) "
                 f"WHERE state = '{QUARANTINED}'")
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
                 f"kind TEXT NOT NULL DEFAULT '{REPLICATE}', PRIMARY KEY (store, binary_rel)) WITHOUT ROWID")
    if "kind" not in {row[1] for row in conn.execute("PRAGMA table_info(media_replica_queue)")}:
        conn.execute(f"ALTER TABLE media_replica_queue ADD COLUMN kind TEXT NOT NULL DEFAULT '{REPLICATE}'")
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

    return ("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, state, seen) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {seen}) ON CONFLICT(binary_rel, store) DO UPDATE SET "
            "object = excluded.object, version = excluded.version, key_version = excluded.key_version, "
            "size = excluded.size, seen = excluded.seen, "
            f"verified_at = CASE WHEN {unchanged} THEN COALESCE(excluded.verified_at, media_replicas.verified_at) "
            "ELSE excluded.verified_at END, "
            + keep("state", "excluded.state") + ", " + keep("checked_at", "0") + ", " + keep("level", "NULL") + ", "
            + keep("decrypted_at", "0") + ", " + keep("failure", "NULL") + ", " + keep("sample_month", "0"))


#: A publication reads the generation in the same statement as its write, so a rebuild that starts in between cannot
#: strand the row below its own generation.
_UPSERT_PUBLISHED = _upsert("(SELECT value FROM media_replica_generation WHERE id = 1)")
_UPSERT_REBUILT = _upsert("?")


def record(vault: Path, binary_rel: str, replica: originals.StoredObject, *, verified_at: float | None) -> None:
    """Note a replica that a binding now lists. `verified_at` is its upload time; None when this write does not know it."""
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute(_UPSERT_PUBLISHED, (binary_rel, replica.store, replica.object, replica.version,
                                         replica.key_version, replica.ciphertext_size, verified_at, UPLOAD_VERIFIED))


def quarantine(vault: Path, binary_rel: str, replica: originals.StoredObject, *, level: str, failure: str) -> None:
    """Mark `replica` quarantined: the `level` of the check that failed, and why. Nothing trusts it from now on.

    The row is written first when the table does not know the store's replica (a publication whose write was lost), so
    a finding is never dropped for want of a row. A row that names another version is left alone: it is a newer
    replica, not the one that failed. The same write queues the new copy that the worker makes.
    """
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, "
                     "state, seen) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, (SELECT value FROM media_replica_generation "
                     "WHERE id = 1)) ON CONFLICT(binary_rel, store) DO NOTHING",
                     (binary_rel, replica.store, replica.object, replica.version, replica.key_version,
                      replica.ciphertext_size, UPLOAD_VERIFIED))
        changed = conn.execute(
            "UPDATE media_replicas SET state = ?, level = ?, failure = ?, checked_at = ? "
            "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ?",
            (QUARANTINED, level, failure, time.time(), binary_rel, replica.store, replica.object,
             replica.version)).rowcount
        if changed:
            conn.execute("INSERT INTO media_replica_queue(store, binary_rel, state, kind) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(store, binary_rel) DO UPDATE SET state = excluded.state, kind = excluded.kind, "
                         "code = NULL, attempts = 0, retry_at = 0",
                         (replica.store, binary_rel, PENDING, RECOPY))
    if changed:
        wake()


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
                          replica.ciphertext_size, None, UPLOAD_VERIFIED, generation))
            held = totals.setdefault(replica.store, [0, 0])
            held[0] += 1
            held[1] += replica.ciphertext_size
            written += 1
        if len(batch) >= _SCAN_BATCH:
            flush()
    flush()
    with closing(ledger._connect()) as conn, conn:
        conn.execute("DELETE FROM media_replicas WHERE seen < ?", (generation,))
        conn.execute("DELETE FROM media_replica_store_totals")
        conn.executemany("INSERT INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                         [(store, held[0], held[1]) for store, held in totals.items()])
        # A new copy that had no verified source may have one now: the original came back, or another replica did.
        conn.execute("UPDATE media_replica_queue SET state = ?, attempts = 0, retry_at = 0, code = NULL "
                     "WHERE state = ? AND kind = ?", (PENDING, REFUSED, RECOPY))
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


def recoverability(config: dict[str, object], key_version: str, exports: dict[str, float]) -> tuple[bool | None, str | None]:
    """Whether a replica made under `key_version` survives the loss of this machine, and the reason when it does not.

    It does when a platform holds the identity (the configuration says so) or the owner confirmed an export. A vault
    that lists its own stores is a personal install, where the owner's export is the only way, so an unconfirmed
    key reads as not recoverable. A vault with neither, such as a Cloud cell's, says nothing about custody: unknown.
    """
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


def _unknown(reason: str, **extra: object) -> dict[str, object]:
    return {"state": "unknown", "reason": reason, "as_of": None, "stores": None, "totals": None, **extra}


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
            groups = conn.execute(
                "SELECT store, key_version, state, COUNT(*) AS replicas, COUNT(verified_at) AS timed, "
                "MIN(verified_at) AS oldest FROM media_replicas GROUP BY store, key_version, state").fetchall()
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
        }
    for row in groups:
        stats = stores[row["store"]]
        stats["replicas"] += row["replicas"]
        if row["state"] == UPLOAD_VERIFIED:
            stats["verified_at_upload"] += row["replicas"]
        stats["verification_time_unknown"] += row["replicas"] - row["timed"]
        if row["oldest"] is not None and (stats["oldest_verified_at"] is None or row["oldest"] < stats["oldest_verified_at"]):
            stats["oldest_verified_at"] = row["oldest"]
        recoverable, reason = recoverability(config, row["key_version"], exports)
        if recoverable is True:
            stats["recoverable"] += row["replicas"]
            continue
        # A copy that is known not to survive and one whose custody nobody stated are different answers.
        counted, reasons = (("not_recoverable", "not_recoverable_reasons") if recoverable is False
                            else ("recoverability_unknown", "recoverability_unknown_reasons"))
        stats[counted] += row["replicas"]
        if reason is not None and reason not in stats[reasons]:
            stats[reasons].append(reason)
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
    }
    for stats in (*stores.values(), totals):
        stats["oldest_verified_at"] = _iso(stats["oldest_verified_at"])
    return {"state": "measured", "reason": None, "as_of": _iso(census["rebuilt_at"]),
            "unread_originals": census["unread"], "source": "media ledger table, rebuilt from the bindings",
            "stores": list(stores.values()), "totals": totals}


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
                "SELECT binary_rel, store, object, version, key_version, verified_at, state FROM media_replicas "
                "WHERE binary_rel > ? AND binary_rel <= ? ORDER BY binary_rel, store",
                (after or "", paths[min(limit, len(paths)) - 1] if paths else "")).fetchall()
    except (sqlite3.Error, OSError) as error:
        return _unknown(f"the replica state cannot be read: {type(error).__name__}", originals=None, next_after=None)
    if census is None:
        return _unknown("the replica table is being rebuilt from the bindings", originals=None, next_after=None)
    more = len(paths) > limit
    listed: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        entry = entries.get(row["store"])
        recoverable, reason = recoverability(config, row["key_version"], exports)
        listed.setdefault(row["binary_rel"], []).append({
            "store": row["store"], "state": row["state"], "verified_at": _iso(row["verified_at"]),
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": row["key_version"],
        })
    return {"state": "measured", "reason": None, "as_of": _iso(census["rebuilt_at"]),
            "originals": [{"path": path, "replicas": found} for path, found in listed.items()],
            "next_after": next(reversed(listed)) if more and listed else None}


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
    times: dict[str, float] = {}
    if ledger.path.exists():  # With no ledger the binding alone lists the replicas, and nothing is confirmed.
        with closing(ledger._connect(readonly=True)) as conn:
            exports = _exports(conn)
            try:
                times = {row["store"]: row["verified_at"] for row in conn.execute(
                    "SELECT store, verified_at FROM media_replicas WHERE binary_rel = ?",
                    (snapshot.path.relative_to(vault).as_posix(),))}
            except sqlite3.OperationalError:
                times = {}
    found = []
    for replica in _replicas_of(snapshot):
        entry = entries.get(replica.store)
        recoverable, reason = recoverability(config, replica.key_version, exports)
        found.append({
            "store": replica.store, "state": UPLOAD_VERIFIED, "verified_at": _iso(times.get(replica.store)),
            "failure_domain": entry.failure_domain if entry is not None else None,
            "retrieval_class": entry.retrieval_class if entry is not None else None,
            "recoverable": recoverable, "reason": reason, "key_version": replica.key_version,
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
            conn.execute("UPDATE media_replica_queue SET state = ?, attempts = 0, retry_at = 0, code = NULL "
                         "WHERE store = ? AND state = ? AND kind = ?", (PENDING, store, REFUSED, REPLICATE))
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
            conn.executemany("INSERT INTO media_replica_queue(store, binary_rel, state) VALUES (?, ?, 'pending') "
                             "ON CONFLICT(store, binary_rel) DO NOTHING", batch)
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
    return ("SELECT store, binary_rel, attempts, kind FROM media_replica_queue WHERE state = ? AND retry_at <= ?"
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
            media_store.replicate(vault, vault / row["binary_rel"], row["store"])
        except media_store.MediaStoreError as error:
            code = error.code
        except (OSError, ValueError, sqlite3.Error) as error:
            code = type(error).__name__
        if code is None:
            _settle(ledger, row, DONE, None)
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
                                 "WHERE store = ? AND binary_rel = ?",
                                 (attempts, code, time.time() + _RETRY_SECONDS * attempts, row["store"], row["binary_rel"]))
            if code == "MEDIA_STORE_UNAVAILABLE":
                paused.add(row["store"])  # A store that failed mid-copy fails the same way for the next original.
        else:
            _settle(ledger, row, REFUSED, code)
            settled += 1
    return settled


def _settle(ledger: MediaJobStore, row: sqlite3.Row, state: str, code: str | None, *, attempts: int | None = None) -> None:
    with closing(ledger._connect()) as conn, conn:
        conn.execute("UPDATE media_replica_queue SET state = ?, code = ?, attempts = COALESCE(?, attempts) "
                     "WHERE store = ? AND binary_rel = ?", (state, code, attempts, row["store"], row["binary_rel"]))


_observed: dict[Path, float] = {}
_checked: dict[Path, float] = {}  # when each vault last ran a check round


def work(vault: Path, *, deadline: float | None = None) -> bool:
    """One bounded unit of background work: a rebuild, a scan, a drain or a check round; True when it did something."""
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
    if drain(vault, deadline=deadline) > 0:
        return True
    if time.monotonic() - _checked.get(vault, -_CHECK_INTERVAL) < _CHECK_INTERVAL:
        return False
    _checked[vault] = time.monotonic()
    return check(vault) + sample(vault) > 0


# --- scheduled checks --------------------------------------------------------------------------------


def check_cycle() -> float:
    """Seconds in which every replica is checked once; the operator's setting, 30 days by default."""
    from . import original_fixity

    return original_fixity.setting("EXOMEM_REPLICA_CHECK_CYCLE_DAYS", DEFAULT_CHECK_CYCLE_DAYS) * 86400.0


def check_read_rate() -> float:
    """The cap on the bytes per second that content checks may make a store read."""
    from . import original_fixity

    return original_fixity.setting("EXOMEM_REPLICA_CHECK_READ_MIB_PER_SECOND", DEFAULT_CHECK_READ_MIB_PER_SECOND) * 1024**2


class _ReadCap:
    """Keeps a round's reads under the read-rate cap: each read earns its own wait, and time spent elsewhere earns no burst.

    A store's `head` and a sample read whole objects, so the cap applies per object, after its read.
    """

    def __init__(self, rate: float, limit: float) -> None:
        self.rate, self.limit, self.due = rate, limit, 0.0

    def spend(self, size: int) -> bool:
        """Account for a read of `size` bytes and wait out its share; False when the wait would pass the round's end."""
        now = time.monotonic()
        self.due = max(self.due, now) + size / self.rate
        if self.due >= self.limit:
            return False
        foreground_priority.wait_for_retry(max(0.0, self.due - now))
        return True


def scheduled_level(entry: originals.StoreEntry) -> str | None:
    """The check that a store's descriptor allows: content when its `head` recomputes, presence when it does not.

    A descriptor that does not state it, or a store that is not usable, gets none: a check of unknown strength would
    report a proof that it cannot name. The decision reads the descriptor value and never a store's or adapter's name.
    """
    if not entry.available or entry.head_recomputes is None:
        return None
    return CONTENT if entry.head_recomputes else PRESENCE


_BINDING_UNREADABLE = (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)


def _bound(vault: Path, row: sqlite3.Row) -> tuple[originals.OriginalSnapshot, originals.StoredObject] | None:
    """The binding and the entry that it lists for the row's store now; the table follows the binding when they differ.

    None when the binding lists no replica in the store, or cannot be read: the next rebuild drops such a row.
    """
    try:
        snapshot = originals.original_state(vault, row["binary_rel"])
    except _BINDING_UNREADABLE:
        return None
    entry = next((found for found in _replicas_of(snapshot) if found.store == row["store"]), None)
    if entry is None:
        return None
    if (entry.object, entry.version) != (row["object"], row["version"]):
        record(vault, row["binary_rel"], entry, verified_at=None)  # a newer version, which starts unchecked
    return snapshot, entry


def _passed(vault: Path, binary_rel: str, replica: originals.StoredObject, level: str) -> None:
    """Record a check that matched. A replica that was quarantined meanwhile stays so."""
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET checked_at = ?, level = ?, failure = NULL "
                     "WHERE binary_rel = ? AND store = ? AND object = ? AND version = ? "
                     f"AND state <> '{QUARANTINED}'",
                     (time.time(), level, binary_rel, replica.store, replica.object, replica.version))


#: The replicas of a store checked longest ago, never-checked first, that the cycle has made due: a range of
#: `media_replicas_due` in its own order, so a pick reads `LIMIT` rows whatever the table holds.
_DUE = ("SELECT binary_rel, store, object, version FROM media_replicas "
        f"WHERE store = ? AND state <> '{QUARANTINED}' AND checked_at <= ? ORDER BY checked_at LIMIT ?")


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


def check(vault: Path, *, deadline: float | None = None) -> int:
    """One bounded round of scheduled checks over every store whose descriptor allows one; return the replicas checked.

    Each store takes the replicas checked longest ago first, within its share of the cycle (replicas, and for a content
    check bytes under the read-rate cap) and within the round's time. It holds no vault guard: a check reads a store and
    writes one derived row. A store that does not answer as itself changes no row and is tried again on a later round.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return 0
    ledger = MediaJobStore(vault)
    cycle, rate = check_cycle(), check_read_rate()
    limit = time.monotonic() + _CHECK_ROUND_SECONDS if deadline is None else deadline
    checked = 0
    cap = _ReadCap(rate, limit)
    for name, entry in originals.store_entries(config).items():
        level = scheduled_level(entry)
        if level is None:
            continue
        if (answer := media_store.store_reachable(vault, name)) is not None:
            _note_reach(vault, name, answer)
        if answer is False:
            continue
        rows, budget = _store_budget(ledger, name, cycle)
        spent = 0
        for row in _due(ledger, name, cycle, min(rows, _CHECK_BATCH)):
            if time.monotonic() >= limit or (level == CONTENT and spent >= budget):
                break
            foreground_priority.yield_to_foreground()
            bound = _bound(vault, row)
            if bound is None:
                continue
            replica = bound[1]
            try:
                failure = media_store.check_replica(vault, replica)
            except (media_store.MediaStoreError, ValueError):
                break  # The store cannot answer now: unavailable is no proof of loss, so no row changes.
            if failure is None:
                _passed(vault, row["binary_rel"], replica, level)
            else:
                quarantine(vault, row["binary_rel"], replica, level=level, failure=failure)
            checked += 1
            if level == CONTENT:
                spent += replica.ciphertext_size
                if not cap.spend(replica.ciphertext_size):
                    break  # The cap would hold the worker past the round: the next round goes on.
    return checked


# --- sampled checks ----------------------------------------------------------------------------------

#: The replicas that one round samples in a store, the replicas it may skip in one for size, and the skipped ones that a
#: new month releases in one write.
_SAMPLE_READS = 8
_SAMPLE_SKIPS = 64
_SAMPLE_RELEASE = 256
#: `sample_month` of a row that no sample can read (its binding lists no exact plaintext digest, or cannot be read).
_UNSAMPLEABLE = -1

#: The replica of a store whose last decrypt proof is oldest, never-proven first, among those not skipped this month and
#: not proved within the cycle: a range of `media_replicas_sample` in its own order. The key version is the one whose
#: identity this install holds. A replica is sampled once per cycle, so a small store does not spend its budget twice.
_SAMPLE = ("SELECT binary_rel, store, object, version, size FROM media_replicas "
           f"WHERE store = ? AND key_version = ? AND state <> '{QUARANTINED}' AND sample_month = 0 "
           "AND decrypted_at <= ? ORDER BY decrypted_at LIMIT 1")
#: A new month gives the replicas that the last one skipped for size another chance, a bounded batch at a time.
_SAMPLE_RESET = ("UPDATE media_replicas SET sample_month = 0 WHERE (binary_rel, store) IN ("
                 "SELECT binary_rel, store FROM media_replicas WHERE store = ? AND key_version = ? "
                 f"AND state <> '{QUARANTINED}' AND sample_month > 0 AND sample_month < ? LIMIT ?)")


def _month(moment: float) -> int:
    """The UTC calendar month of `moment` as YYYYMM: the period of a store's sample budget."""
    stamp = datetime.fromtimestamp(moment, timezone.utc)
    return stamp.year * 100 + stamp.month


def _mark_sample(vault: Path, row: sqlite3.Row, month: int) -> None:
    """Take a replica out of the sample picks for `month` (or for good, for `_UNSAMPLEABLE`)."""
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET sample_month = ? WHERE binary_rel = ? AND store = ?",
                     (month, row["binary_rel"], row["store"]))


def _proved(vault: Path, row: sqlite3.Row, replica: originals.StoredObject, store: str, month: int) -> None:
    """Record a sample that matched and charge its bytes to the month; a replica quarantined meanwhile stays so."""
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_replicas SET decrypted_at = ?, failure = NULL WHERE binary_rel = ? AND store = ? "
                     f"AND object = ? AND version = ? AND state <> '{QUARANTINED}'",
                     (time.time(), row["binary_rel"], store, replica.object, replica.version))
        _charge(conn, store, month, replica.ciphertext_size)


def _charge(conn: sqlite3.Connection, store: str, month: int, size: int) -> None:
    conn.execute("INSERT INTO media_replica_samples(store, month, bytes) VALUES (?, ?, ?) "
                 "ON CONFLICT(store, month) DO UPDATE SET bytes = bytes + excluded.bytes", (store, month, size))


def sample(vault: Path, *, deadline: float | None = None) -> int:
    """One bounded round of sampled checks; return the replicas it sampled.

    A sample downloads a replica, decrypts it into a sink that keeps nothing, and hashes the plaintext against the
    binding. Each store spends at most its `sample_bytes_per_month` in a UTC calendar month (zero turns it off), and a
    replica that does not fit the month's remainder is skipped for the month. It reads only where repair could read:
    an `immediate` store and the key version whose identity this install holds. A store that does not answer as
    itself, or an unreadable identity, is no finding: nothing changes.
    """
    from . import media_store

    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return 0
    ledger = MediaJobStore(vault)
    limit = time.monotonic() + _CHECK_ROUND_SECONDS if deadline is None else deadline
    cap, cycle = _ReadCap(check_read_rate(), limit), check_cycle()
    month, key_version, sampled = _month(time.time()), str(config.get("key_version")), 0
    for name, entry in originals.store_entries(config).items():
        if entry.sample_bytes_per_month <= 0 or media_store.unreadable_reason(vault, name, key_version) is not None:
            continue
        if (answer := media_store.store_reachable(vault, name)) is not None:
            _note_reach(vault, name, answer)
        if answer is False:
            continue
        with closing(ledger._connect()) as conn, conn:
            conn.execute(_SAMPLE_RESET, (name, key_version, month, _SAMPLE_RELEASE))
        reads = skips = 0
        while reads < _SAMPLE_READS and skips < _SAMPLE_SKIPS and time.monotonic() < limit:
            with closing(ledger._connect(readonly=True)) as conn:
                row = conn.execute(_SAMPLE, (name, key_version, time.time() - cycle)).fetchone()
                held = conn.execute("SELECT bytes FROM media_replica_samples WHERE store = ? AND month = ?",
                                    (name, month)).fetchone()
            if row is None:
                break
            remaining = entry.sample_bytes_per_month - (held["bytes"] if held is not None else 0)
            if remaining <= 0:
                break
            if row["size"] > remaining:  # a size of 0 is not yet known: the binding gives it below
                _mark_sample(vault, row, month)
                skips += 1
                continue
            foreground_priority.yield_to_foreground()
            bound = _bound(vault, row)
            if bound is None or bound[0].binary_sha256 is None or bound[0].binary_size is None:
                _mark_sample(vault, row, _UNSAMPLEABLE)
                skips += 1
                continue
            snapshot, replica = bound
            if replica.ciphertext_size > remaining:
                _mark_sample(vault, row, month)
                skips += 1
                continue
            try:
                failure = media_store.sample_replica(
                    vault, replica, media_store.ObjectHead(snapshot.binary_size, snapshot.binary_sha256))
            except (media_store.MediaStoreError, ValueError):
                break  # The store cannot answer now: unavailable is no proof of loss, so no row changes.
            if failure is None:
                _proved(vault, row, replica, name, month)
            else:
                with closing(ledger._connect()) as conn, conn:
                    _charge(conn, name, month, replica.ciphertext_size)
                quarantine(vault, row["binary_rel"], replica, level=DECRYPTED, failure=failure)
            reads += 1
            sampled += 1
            if not cap.spend(replica.ciphertext_size):
                break
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
