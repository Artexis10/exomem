"""The dreamer's disposable SQLite sidecar: `<vault_state_dir>/dreamer.sqlite`.

Derived state only. It follows the activation index's conventions: a
`meta(key, value)` table holding the schema version and a write `generation`
bumped inside every write transaction, a wipe when the schema on disk is not
the one this binary writes, and WAL. Deleting the file costs a reseed and
nothing else: every triage decision lives in the portable review state, keyed
by candidate ids that depend only on the proposal (never on this file), so a
dismissal survives the rebuild.

Only the dreamer's worker thread writes. Carriers read through `read_view`,
which opens the file read-only and query-only, never creates it, never waits
on a lock, and memoises its parsed view on `meta.generation`.

The store lives in the machine-local state directory, outside the vault, so a
write here fires no watcher event, no freshness change and no graph debt.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import review_state
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

#: 3 added the alias and convention families' page-contribution tables and 4
#: their casefolded spelling columns. A mismatch wipes the file and reseeds,
#: which also resets the delivery ledger.
SCHEMA_VERSION = 4
SIDECAR_NAME = "dreamer.sqlite"

#: The size cap (`size_cap_bytes`): past it the global-count families stop
#: recording and the page-local families continue. Prevents an unbounded state
#: file, and is the global families' one bound: they are exempt from the row
#: caps. It is `max(floor, headroom x bytes per page x indexed pages)`.
SIZE_CAP_FLOOR_BYTES = 64 * 1024 * 1024
#: Sidecar bytes per indexed page with every per-page bound full (16 names,
#: 16 link targets, 16 tags): measured at 13.3 KB marginal and 14.7 KB in all
#: on a 200-page synthetic vault, 13.8 KB at 600 pages; rounded up.
BYTES_PER_PAGE = 16 * 1024
SIZE_CAP_HEADROOM = 3

#: Open candidates per family, and rows in total including resolved history,
#: for the page-local families. Prevents an ignored backlog accumulating in the
#: store; the lowest-evidence candidate is evicted first and comes back when its
#: evidence grows. The global families are exempt: whether one of their rows
#: survives must not depend on rows a caller may not see, so only the size cap
#: bounds them.
MAX_OPEN_PER_FAMILY = 64
MAX_ROWS = 512

#: Evidence entries kept per candidate. A family that needs more abstains.
EVIDENCE_CAP = 8

#: Resolved and abstained history is pruned after this long.
RESOLVED_RETENTION_SECONDS = 30 * 86400.0
#: The delivery ledger's bound: deliveries are rate-limited per vault, so this
#: holds weeks of them, including those of rows evicted since.
MAX_DELIVERY_ROWS = 4 * MAX_ROWS

#: Families that need vault-wide counts, and so stop at the size cap.
GLOBAL_FAMILIES = frozenset({"upkeep_alias", "upkeep_convention"})

#: A fold key carried by more pages than this, among the pages one caller may
#: see, is not served to that caller. Withheld pages never count toward it.
MEMBER_BOUND = 32

#: The page-contribution tables of the global families: each page replaces its
#: own rows. After `fold_key` and `path`, their columns: a name's source and its
#: casefolded spelling (`name_keys`); a link's authored spelling and that
#: spelling casefolded (`name_refs`); a tag's authored spelling (`term_uses`).
#: Spellings are compared on the stored casefolded columns only.
CONTRIBUTION_TABLES: dict[str, tuple[str, ...]] = {
    "name_keys": ("source", "spelling"),
    "name_refs": ("raw", "raw_cf"),
    "term_uses": ("raw",),
}

#: SQLite page cache for the worker's connection: 1 MiB.
_CACHE_SIZE_KIB = 1024

_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, sig TEXT NOT NULL)",
    # `held` orders a page that could not run yet behind the rest of the queue.
    "CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY, held INTEGER NOT NULL DEFAULT 0)",
    """
    CREATE TABLE IF NOT EXISTS candidates (
        id TEXT PRIMARY KEY, family TEXT NOT NULL, kind TEXT NOT NULL,
        subject_path TEXT NOT NULL, subject_ref TEXT NOT NULL,
        proposal_key TEXT NOT NULL, ref TEXT NOT NULL,
        signal_version TEXT NOT NULL, fingerprint TEXT NOT NULL,
        evidence_json TEXT NOT NULL, evidence_count INTEGER NOT NULL,
        route_json TEXT NOT NULL, reason_code TEXT NOT NULL,
        measures_json TEXT NOT NULL, producers_json TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at REAL NOT NULL, refreshed_at REAL NOT NULL,
        settled_at REAL, resolved_at REAL,
        deliverable INTEGER NOT NULL DEFAULT 0, deliverable_token TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS candidates_family_state ON candidates(family, state)",
    """
    CREATE TABLE IF NOT EXISTS candidate_paths (
        path TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (path, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS candidate_paths_id ON candidate_paths(id)",
    """
    CREATE TABLE IF NOT EXISTS deliveries (
        id TEXT NOT NULL, fingerprint TEXT NOT NULL, caller_hash TEXT NOT NULL,
        delivered_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS deliveries_id ON deliveries(id, fingerprint)",
    # A row with a `fold_key` marks a possible name ambiguity: its category and
    # paths are recomputed per request from the pages that caller may see.
    """
    CREATE TABLE IF NOT EXISTS integrity (
        category TEXT NOT NULL, path_set TEXT NOT NULL, observed_at REAL NOT NULL,
        fold_key TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (category, path_set, fold_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS integrity_fold_key ON integrity(fold_key)",
    *(
        statement
        for table, columns in CONTRIBUTION_TABLES.items()
        for statement in (
            f"CREATE TABLE IF NOT EXISTS {table} (fold_key TEXT NOT NULL, path TEXT NOT NULL, "
            + "".join(f"{column} TEXT NOT NULL, " for column in columns)
            + f"PRIMARY KEY (fold_key, path, {', '.join(columns)}))",
            f"CREATE INDEX IF NOT EXISTS {table}_path ON {table}(path)",
        )
    ),
    "CREATE INDEX IF NOT EXISTS name_keys_spelling ON name_keys(fold_key, spelling, path)",
    "CREATE INDEX IF NOT EXISTS name_refs_spelling ON name_refs(fold_key, raw_cf, path)",
    "CREATE INDEX IF NOT EXISTS term_uses_raw ON term_uses(fold_key, raw, path)",
)
_DATA_TABLES = (
    "seen",
    "pending",
    "candidates",
    "candidate_paths",
    "deliveries",
    "integrity",
    *CONTRIBUTION_TABLES,
)


def sidecar_path(vault_root: Path) -> Path:
    return vault_state_dir(Path(vault_root)) / SIDECAR_NAME


def candidate_id(kind: str, subject_path: str, proposal_key: str) -> str:
    """A proposal's identity: independent of evidence, order, time or producer."""
    return review_state.item_id(f"upkeep:{kind}:{subject_path}:{proposal_key}")


def encode_sig(signature: Iterable[int] | None) -> str | None:
    if signature is None:
        return None
    return ":".join(str(int(part)) for part in signature)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sanitize_health(health: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only known fields, as numbers, closed codes and family flags.

    Health is served on status surfaces and must never carry a path or any
    content, so this is an allowlist rather than a scrubber: a field or value
    it does not recognise is dropped or replaced by `UNKNOWN`.
    """
    out: dict[str, Any] = {}
    for key in ("last_tick_at", "hour_cpu_used", "waiting_since", "failed_since", "recorded_at"):
        value = health.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    for key in (
        "consecutive_failures",
        "reseed_remaining",
        "pages_processed",
        "ticks",
        "open_candidates",
    ):
        value = health.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = int(value)
    for key in ("last_error_code", "waiting_reason", "state", "last_stop_reason"):
        if key not in health or health[key] is None:
            continue
        value = str(health[key])
        out[key] = value if _CODE.match(value) and "/" not in value else "UNKNOWN"
    complete = health.get("evidence_complete")
    if isinstance(complete, Mapping):
        out["evidence_complete"] = {
            str(family): bool(flag)
            for family, flag in complete.items()
            if isinstance(family, str) and _CODE.match(family)
        }
    return out


def proposal_fingerprint(
    *,
    family: str,
    subject_ref: str,
    signal_version: str,
    evidence: Iterable[Mapping[str, Any]],
) -> str:
    """The review-state fingerprint of one proposal: its signal, not its pages.

    Keyed on the family, the subject, the minimal structural evidence
    (`signal_version`) and the evidence refs, so a dismissal stays quiet while
    that evidence is the same and reopens only when it changes.
    """
    ordered = sorted(evidence, key=lambda item: str(item.get("path") or ""))
    return review_state.fingerprint(
        target_ref=subject_ref,
        categories=[family],
        reasons=[{"category": family, "meta": {"signal_version": signal_version}}],
        related_refs=[str(item.get("ref") or "") for item in ordered[:EVIDENCE_CAP]],
    )


def _merge_evidence(producers: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    merged: dict[str, dict[str, Any]] = {}
    counts = [0]
    for name in sorted(producers):
        entry = producers[name]
        counts.append(int(entry.get("evidence_count") or 0))
        for item in entry.get("evidence") or []:
            merged.setdefault(str(item.get("path") or ""), dict(item))
    ordered = [merged[path] for path in sorted(merged)]
    return ordered[:EVIDENCE_CAP], max(len(ordered), *counts)


def _merged_signal_version(producers: Mapping[str, Any]) -> str:
    if len(producers) == 1:
        return str(next(iter(producers.values())).get("signal_version") or "")
    return "|".join(
        f"{name}={producers[name].get('signal_version') or ''}" for name in sorted(producers)
    )


class DreamerStore:
    """The worker's handle on one vault's sidecar."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open, wiping a mismatched, unreadable or damaged file, and ensure the schema.

        A file this process did not leave as it is now (the first open in a
        process, or a file changed by anything else) is checked whole before
        use: damage in a page no tick happens to read would otherwise stay
        until a later write met it. A file that cannot be brought to this
        schema is wiped and rebuilt too, rather than failing every tick. Only
        damage or an incompatible file is a reason to wipe: a lock, a full
        disk or a passing I/O fault raises and the next tick tries again.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open()
            if self._schema(conn) not in {None, str(SCHEMA_VERSION)}:
                conn.close()
                self.wipe()
                conn = self._open()
            elif not self._known() and not self._sound(conn):
                conn.close()
                self.wipe()
                conn = self._open()
            self._ensure_schema(conn)
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            if not _incompatible(exc):
                raise
            self.wipe()
            conn = self._open()
            self._ensure_schema(conn)
        self._remember()
        return conn

    def close(self, conn: sqlite3.Connection) -> None:
        """Close the worker's connection and remember the file as this process left it."""
        conn.close()
        self._remember()

    def _state(self) -> tuple[int, int, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    def _known(self) -> bool:
        with _MEMO_LOCK:
            return _LEFT.get(str(self.path)) == self._state()

    def _remember(self) -> None:
        state = self._state()
        with _MEMO_LOCK:
            _LEFT[str(self.path)] = state

    @staticmethod
    def _sound(conn: sqlite3.Connection) -> bool:
        """False when the open file fails an integrity check. A lock is not damage."""
        try:
            return conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        except sqlite3.OperationalError:
            return True

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        # Takes effect only on a fresh file, before its first table: freed
        # pages can then be returned to the filesystem (`reclaim`).
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA cache_size=-{_CACHE_SIZE_KIB}")
        return conn

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> str | None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        return None if row is None else str(row[0])

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _TABLES:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('instance', ?)",
                (str(random.SystemRandom().randint(1, 2**31 - 1)),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def is_damaged(self) -> bool:
        """True when the sidecar file itself fails an integrity check.

        A lock, a read-only file or an I/O error is not damage: those raise
        `OperationalError` here and answer False, so only a corrupt file is
        ever wiped.
        """
        if not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(str(self.path), timeout=1.0)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return False
        except sqlite3.DatabaseError:
            return True
        return rows != [("ok",)]

    def wipe(self) -> None:
        """Remove the sidecar and its WAL files. It is derived; a reseed rebuilds it."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = self.path.with_name(self.path.name + suffix)
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
        clear_reader_memo()

    @contextlib.contextmanager
    def write(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """One write transaction; commits with a generation bump or rolls back."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute(
                "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                "WHERE key = 'generation'"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def generation(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # meta: checkpoint and health
    # ------------------------------------------------------------------

    @staticmethod
    def get_meta(conn: sqlite3.Connection, key: str) -> Any:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, None if value is None else _dumps(value)),
        )

    def health(self, conn: sqlite3.Connection) -> dict[str, Any]:
        value = self.get_meta(conn, "health")
        return _sanitize_health(value) if isinstance(value, Mapping) else {}

    def set_health(self, conn: sqlite3.Connection, health: Mapping[str, Any]) -> None:
        self.set_meta(conn, "health", _sanitize_health(health))

    # ------------------------------------------------------------------
    # seen / pending
    # ------------------------------------------------------------------

    @staticmethod
    def seen_get(conn: sqlite3.Connection, path: str) -> str | None:
        row = conn.execute("SELECT sig FROM seen WHERE path=?", (path,)).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def seen_set(conn: sqlite3.Connection, path: str, signature: Iterable[int]) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO seen(path, sig) VALUES (?, ?)",
            (path, encode_sig(signature)),
        )

    @staticmethod
    def seen_delete(conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM seen WHERE path=?", (path,))

    @staticmethod
    def seen_map(conn: sqlite3.Connection) -> dict[str, str]:
        return {str(path): str(sig) for path, sig in conn.execute("SELECT path, sig FROM seen")}

    @staticmethod
    def pending_add(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO pending(path) VALUES (?)", ((path,) for path in paths)
        )

    @staticmethod
    def pending_next(conn: sqlite3.Connection, limit: int) -> list[str]:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT path FROM pending ORDER BY held, path LIMIT ?", (max(0, int(limit)),)
            )
        ]

    @staticmethod
    def pending_hold(conn: sqlite3.Connection, path: str) -> None:
        """Keep `path` pending, behind every page that can run now."""
        conn.execute("UPDATE pending SET held=1 WHERE path=?", (path,))

    @staticmethod
    def pending_remove(conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM pending WHERE path=?", (path,))

    @staticmethod
    def pending_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT count(*) FROM pending").fetchone()[0])

    # ------------------------------------------------------------------
    # capacity
    # ------------------------------------------------------------------

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += self.path.with_name(self.path.name + suffix).stat().st_size
        return total

    @staticmethod
    def size_cap_bytes(conn: sqlite3.Connection) -> int:
        """The size cap for the pages indexed so far (see `SIZE_CAP_FLOOR_BYTES`)."""
        pages = int(conn.execute("SELECT count(*) FROM seen").fetchone()[0])
        return max(SIZE_CAP_FLOOR_BYTES, SIZE_CAP_HEADROOM * BYTES_PER_PAGE * pages)

    def capacity_exceeded(self, conn: sqlite3.Connection) -> bool:
        return self.size_bytes() > self.size_cap_bytes(conn)

    def family_enabled(self, conn: sqlite3.Connection, family: str) -> bool:
        return family not in GLOBAL_FAMILIES or not self.capacity_exceeded(conn)

    @staticmethod
    def reclaim(conn: sqlite3.Connection) -> None:
        """Return freed pages to the filesystem. Call outside a transaction.

        Pruning and replaced contributions leave free pages; without this the
        file never shrinks, and a sidecar once past the size cap would stay
        there until a wipe.
        """
        if not int(conn.execute("PRAGMA freelist_count").fetchone()[0]):
            return
        # The pragma frees one page per step, and a cursor steps a statement
        # with no result columns once: `executescript` runs it to completion.
        conn.executescript("PRAGMA incremental_vacuum;")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()

    # ------------------------------------------------------------------
    # candidates: the one write entry
    # ------------------------------------------------------------------

    def upsert_proposal(
        self,
        conn: sqlite3.Connection,
        *,
        family: str,
        kind: str,
        subject_path: str,
        subject_ref: str,
        proposal_key: str,
        evidence: list[dict[str, Any]],
        route: Mapping[str, Any],
        reason_code: str,
        producer: str,
        signal_version: str,
        now: float,
        measures: Mapping[str, Any] | None = None,
        evidence_count: int | None = None,
        identity: str | None = None,
        ref: str | None = None,
        fingerprint: str | None = None,
        parked: Callable[[str, str], bool] | None = None,
    ) -> str | None:
        """Write or refresh one proposal; the id is `candidate_id` unless given.

        Two producers arriving at the same proposal land on one row and merge
        their evidence. A producer's own contribution is replaced whole. The
        candidate's settle clock (`refreshed_at`) restarts only when its
        evidence signatures or its signal version change. Returns the id, or
        None when capacity or the per-family cap kept it out.
        """
        if not self.family_enabled(conn, family):
            return None
        cid = identity or candidate_id(kind, subject_path, proposal_key)
        row = conn.execute(
            "SELECT producers_json, state, evidence_json, signal_version, created_at, "
            "refreshed_at, settled_at FROM candidates WHERE id=?",
            (cid,),
        ).fetchone()
        producers: dict[str, Any] = {}
        if row is not None and row[1] == "open":
            try:
                producers = dict(json.loads(row[0]) or {})
            except (TypeError, json.JSONDecodeError):
                producers = {}
        own = sorted(evidence, key=lambda item: str(item.get("path") or ""))
        producers[str(producer)] = {
            "evidence": own[:EVIDENCE_CAP],
            "evidence_count": int(evidence_count if evidence_count is not None else len(own)),
            "signal_version": str(signal_version),
            "reason_code": str(reason_code),
            "measures": dict(measures or {}),
        }
        merged, count = _merge_evidence(producers)
        version = _merged_signal_version(producers)
        if fingerprint is None:
            fingerprint = proposal_fingerprint(
                family=family,
                subject_ref=subject_ref,
                signal_version=version,
                evidence=merged,
            )
        signatures = sorted((str(i.get("path") or ""), str(i.get("sig") or "")) for i in merged)
        created_at = now
        refreshed_at = now
        settled_at = None
        if row is not None:
            created_at = float(row[4])
            if row[1] == "open":
                try:
                    previous = json.loads(row[2]) or []
                except (TypeError, json.JSONDecodeError):
                    previous = []
                prior_sigs = sorted(
                    (str(i.get("path") or ""), str(i.get("sig") or "")) for i in previous
                )
                if prior_sigs == signatures and row[3] == version:
                    refreshed_at = float(row[5])
                    settled_at = row[6]
        combined_measures: dict[str, Any] = {}
        for name in sorted(producers):
            combined_measures.update(producers[name].get("measures") or {})
        conn.execute(
            "INSERT OR REPLACE INTO candidates (id, family, kind, subject_path, subject_ref, "
            "proposal_key, ref, signal_version, fingerprint, evidence_json, evidence_count, "
            "route_json, reason_code, measures_json, producers_json, state, created_at, "
            "refreshed_at, settled_at, resolved_at, deliverable, deliverable_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, NULL, 0, NULL)",
            (
                cid,
                family,
                kind,
                subject_path,
                subject_ref,
                proposal_key,
                ref or f"exomem://review/upkeep/{cid}",
                version,
                fingerprint,
                _dumps(merged),
                count,
                _dumps(dict(route)),
                str(reason_code),
                _dumps(combined_measures),
                _dumps(producers),
                created_at,
                refreshed_at,
                settled_at,
            ),
        )
        conn.execute("DELETE FROM candidate_paths WHERE id=?", (cid,))
        paths = {subject_path, *(str(item.get("path") or "") for item in merged)} - {""}
        conn.executemany(
            "INSERT OR IGNORE INTO candidate_paths(path, id) VALUES (?, ?)",
            ((path, cid) for path in sorted(paths)),
        )
        if self._enforce_family_cap(conn, family, keep=cid, parked=parked):
            return None
        self._enforce_row_cap(conn, now, parked=parked)
        return cid

    def resolve(
        self,
        conn: sqlite3.Connection,
        cid: str,
        *,
        now: float,
        producer: str | None = None,
        state: str = "resolved",
    ) -> None:
        """Withdraw one producer's support, or the whole proposal.

        A proposal another producer still supports stays open on that
        producer's evidence; the last withdrawal resolves (or abstains) it.
        """
        row = conn.execute(
            "SELECT producers_json, state FROM candidates WHERE id=?", (cid,)
        ).fetchone()
        if row is None or row[1] != "open":
            return
        try:
            producers = dict(json.loads(row[0]) or {})
        except (TypeError, json.JSONDecodeError):
            producers = {}
        if producer is not None:
            producers.pop(str(producer), None)
        else:
            producers = {}
        if producers:
            merged, count = _merge_evidence(producers)
            conn.execute(
                "UPDATE candidates SET producers_json=?, evidence_json=?, evidence_count=?, "
                "signal_version=?, refreshed_at=?, settled_at=NULL, deliverable=0 WHERE id=?",
                (
                    _dumps(producers),
                    _dumps(merged),
                    count,
                    _merged_signal_version(producers),
                    now,
                    cid,
                ),
            )
            return
        conn.execute(
            "UPDATE candidates SET state=?, resolved_at=?, deliverable=0, "
            "producers_json='{}' WHERE id=?",
            (state, now, cid),
        )
        conn.execute("DELETE FROM candidate_paths WHERE id=?", (cid,))

    @staticmethod
    def candidates_for_path(conn: sqlite3.Connection, path: str) -> list[str]:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM candidate_paths WHERE path=? ORDER BY id", (path,)
            )
        ]

    @staticmethod
    def candidate(conn: sqlite3.Connection, cid: str) -> dict[str, Any] | None:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        finally:
            conn.row_factory = None
        return None if row is None else _row_dict(row)

    @staticmethod
    def open_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE state='open' ORDER BY id"
            ).fetchall()
        finally:
            conn.row_factory = None
        return [_row_dict(row) for row in rows]

    @staticmethod
    def set_deliverable(
        conn: sqlite3.Connection,
        cid: str,
        *,
        deliverable: bool,
        token: str | None,
        settled_at: float | None,
    ) -> None:
        conn.execute(
            "UPDATE candidates SET deliverable=?, deliverable_token=?, settled_at=? WHERE id=?",
            (1 if deliverable else 0, token, settled_at, cid),
        )

    def _enforce_family_cap(
        self,
        conn: sqlite3.Connection,
        family: str,
        *,
        keep: str,
        parked: Callable[[str, str], bool] | None = None,
    ) -> bool:
        """Evict the weakest eligible open rows past the cap. True when `keep` was evicted.

        Only rows still eligible for delivery count and compete: a row the
        review state has decided (dismissed, snoozed, competing) or that is held
        after its deliveries is `parked`. It stays so a snooze can expire and a
        reopen can find it, but it can never fill the cap or push out a new
        proposal. Among eligible rows the weakest evidence goes first, then the
        OLDEST, so a newcomer is never starved by rows that were there first.
        """
        if family in GLOBAL_FAMILIES:
            return False
        rows = conn.execute(
            "SELECT id, fingerprint, evidence_count, created_at FROM candidates "
            "WHERE family=? AND state='open'",
            (family,),
        ).fetchall()
        eligible = [row for row in rows if parked is None or not parked(str(row[0]), str(row[1]))]
        excess = len(eligible) - MAX_OPEN_PER_FAMILY
        if excess <= 0:
            return False
        eligible.sort(key=lambda row: (int(row[2] or 0), float(row[3] or 0.0), str(row[0])))
        victims = [str(row[0]) for row in eligible[:excess]]
        self._delete(conn, victims)
        return keep in victims

    def _enforce_row_cap(
        self,
        conn: sqlite3.Connection,
        now: float,
        *,
        parked: Callable[[str, str], bool] | None = None,
    ) -> None:
        """Bound the whole table: expired resolved rows go, then resolved rows,
        then parked ones, then the weakest and oldest eligible rows."""
        conn.execute(
            "DELETE FROM candidates WHERE state<>'open' AND resolved_at < ?",
            (now - RESOLVED_RETENTION_SECONDS,),
        )
        exempt = sorted(GLOBAL_FAMILIES)
        marks = ",".join("?" for _ in exempt)
        excess = (
            int(
                conn.execute(
                    f"SELECT count(*) FROM candidates WHERE family NOT IN ({marks})", exempt
                ).fetchone()[0]
            )
            - MAX_ROWS
        )
        if excess <= 0:
            return
        rows = conn.execute(
            "SELECT id, fingerprint, state, resolved_at, evidence_count, created_at, "
            f"subject_path, measures_json FROM candidates WHERE family NOT IN ({marks})",
            exempt,
        ).fetchall()
        held = {
            str(row[0])
            for row in rows
            if row[2] == "open" and parked is not None and parked(str(row[0]), str(row[1]))
        }

        def rank(row) -> tuple:
            if row[2] != "open":
                return (0, float(row[3] or 0.0), 0, 0.0, str(row[0]))
            tier = 1 if str(row[0]) in held else 2
            return (tier, 0.0, int(row[4] or 0), float(row[5] or 0.0), str(row[0]))

        victims = {str(row[0]) for row in sorted(rows, key=rank)[:excess]}
        # The two directions of a held link pair go together: a pair is one
        # proposal, and a lone survivor would hold a slot it can never use.
        groups: dict[tuple[str, ...], set[str]] = {}
        for row in rows:
            group = _pair_group(str(row[6] or ""), row[7])
            if group is not None and str(row[0]) in held:
                groups.setdefault(group, set()).add(str(row[0]))
        for members in groups.values():
            if members & victims:
                victims |= members
        self._delete(conn, sorted(victims))

    @staticmethod
    def _delete(conn: sqlite3.Connection, ids: list[str]) -> None:
        for cid in ids:
            conn.execute("DELETE FROM candidates WHERE id=?", (cid,))
            conn.execute("DELETE FROM candidate_paths WHERE id=?", (cid,))

    # ------------------------------------------------------------------
    # deliveries and integrity
    # ------------------------------------------------------------------

    @staticmethod
    def record_deliveries(
        conn: sqlite3.Connection, rows: Iterable[tuple[str, str, str, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO deliveries(id, fingerprint, caller_hash, delivered_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        # A delivery outlives an evicted row, so a row re-created with the same
        # identity and fingerprint keeps its count. Bounded: rows no candidate
        # holds are kept for the resolved-row retention, and at most
        # MAX_DELIVERY_ROWS rows are kept in all, the oldest orphans going first.
        latest = conn.execute("SELECT max(delivered_at) FROM deliveries").fetchone()[0]
        newest = float(latest) if latest is not None else 0.0
        conn.execute(
            "DELETE FROM deliveries WHERE id NOT IN (SELECT id FROM candidates) "
            "AND delivered_at < ?",
            (newest - RESOLVED_RETENTION_SECONDS,),
        )
        excess = int(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]) - (
            MAX_DELIVERY_ROWS
        )
        if excess > 0:
            conn.execute(
                "DELETE FROM deliveries WHERE rowid IN (SELECT d.rowid FROM deliveries AS d "
                "ORDER BY d.id IN (SELECT id FROM candidates), d.delivered_at, d.rowid LIMIT ?)",
                (excess,),
            )

    @staticmethod
    def deliveries(conn: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
        return [
            (str(a), str(b), str(c), float(d))
            for a, b, c, d in conn.execute(
                "SELECT id, fingerprint, caller_hash, delivered_at FROM deliveries"
            )
        ]

    @staticmethod
    def clear_integrity_key(conn: sqlite3.Connection, fold_key: str) -> None:
        """Drop what the global families recorded for one fold key."""
        conn.execute("DELETE FROM integrity WHERE fold_key=? AND fold_key<>''", (fold_key,))

    @staticmethod
    def note_ambiguity(conn: sqlite3.Connection, fold_key: str, now: float) -> None:
        """Mark a fold key whose name ambiguity is judged per request."""
        conn.execute(
            "INSERT OR REPLACE INTO integrity(category, path_set, observed_at, fold_key) "
            "VALUES ('', '[]', ?, ?)",
            (now, fold_key),
        )

    # ------------------------------------------------------------------
    # page contributions: the global families' vault-wide membership
    # ------------------------------------------------------------------

    @staticmethod
    def contribution_keys(conn: sqlite3.Connection, table: str, path: str) -> set[str]:
        """The fold keys `path` contributes to `table`."""
        _check_table(table)
        return {
            str(row[0])
            for row in conn.execute(f"SELECT DISTINCT fold_key FROM {table} WHERE path=?", (path,))
        }

    @staticmethod
    def replace_contributions(
        conn: sqlite3.Connection, table: str, path: str, rows: Iterable[tuple[str, ...]]
    ) -> None:
        """Replace `path`'s rows in `table` with `(fold_key, *columns)` tuples."""
        _check_table(table)
        columns = CONTRIBUTION_TABLES[table]
        marks = ", ".join("?" for _ in columns)
        conn.execute(f"DELETE FROM {table} WHERE path=?", (path,))
        conn.executemany(
            f"INSERT OR IGNORE INTO {table}(fold_key, path, {', '.join(columns)}) "
            f"VALUES (?, ?, {marks})",
            ((row[0], path, *row[1:]) for row in rows if row[0]),
        )

    @staticmethod
    def members(
        conn: sqlite3.Connection,
        table: str,
        fold_key: str,
        *,
        keep: Callable[[str], bool] | None = None,
        pages: int | None = MEMBER_BOUND + 1,
    ) -> list[tuple[str, ...]]:
        """`(path, *columns)` rows for one fold key, from at most `pages` pages.

        Pages come in path order. Under a release predicate a withheld page's
        rows are skipped and never counted, so the answer is exactly what it
        would be on a vault where that page did not exist; only the number of
        rows read grows with the withheld pages it passes. `pages=None` reads
        every page the predicate admits.
        """
        _check_table(table)
        columns = CONTRIBUTION_TABLES[table]
        cursor = conn.execute(
            f"SELECT path, {', '.join(columns)} FROM {table} WHERE fold_key=? "
            f"ORDER BY path, {', '.join(columns)}",
            (fold_key,),
        )
        out: list[tuple[str, ...]] = []
        verdicts: dict[str, bool] = {}
        taken: set[str] = set()
        for row in cursor:
            path = str(row[0])
            if path not in verdicts:
                verdicts[path] = keep is None or bool(keep(path))
                if verdicts[path]:
                    if pages is not None and len(taken) >= pages:
                        break
                    taken.add(path)
            if verdicts[path]:
                out.append((path, *(str(value) for value in row[1:])))
        cursor.close()
        return out

    @staticmethod
    def first_member(conn: sqlite3.Connection, table: str, fold_key: str) -> str | None:
        _check_table(table)
        row = conn.execute(
            f"SELECT path FROM {table} WHERE fold_key=? ORDER BY path LIMIT 1", (fold_key,)
        ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def spelling_firsts(conn: sqlite3.Connection, fold_key: str) -> list[tuple[str, str]]:
        """`(path, spelling)`: the first page of the least spelling and of the next one.

        Two index seeks, whatever the cluster's size.
        """
        out: list[tuple[str, str]] = []
        after = None
        for _ in range(2):
            row = conn.execute(
                "SELECT path, raw FROM term_uses WHERE fold_key=? "
                + ("AND raw>? " if after is not None else "")
                + "ORDER BY raw, path LIMIT 1",
                (fold_key, after) if after is not None else (fold_key,),
            ).fetchone()
            if row is None:
                break
            out.append((str(row[0]), str(row[1])))
            after = str(row[1])
        return out

    @staticmethod
    def spelling_span(conn: sqlite3.Connection, fold_key: str) -> tuple[str, str] | None:
        """The least and greatest spelling of one tag fold key, each one index probe."""
        low = conn.execute(
            "SELECT min(raw) FROM term_uses WHERE fold_key=?", (fold_key,)
        ).fetchone()[0]
        high = conn.execute(
            "SELECT max(raw) FROM term_uses WHERE fold_key=?", (fold_key,)
        ).fetchone()[0]
        return None if low is None or high is None else (str(low), str(high))


def _incompatible(exc: sqlite3.DatabaseError) -> bool:
    """Damage or a file this schema cannot use: the only reasons to wipe.

    SQLITE_ERROR is what a table shape this code does not expect raises
    ("no such column"); CORRUPT and NOTADB are damage. Busy, locked, I/O,
    full, read-only and out-of-memory are passing faults, never wiped.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return int(code) & 0xFF in {1, 11, 26}  # SQLITE_ERROR, _CORRUPT, _NOTADB
    text = str(exc).casefold()
    return any(cue in text for cue in ("no such", "malformed", "not a database"))


def _check_table(table: str) -> None:
    if table not in CONTRIBUTION_TABLES:
        raise ValueError(f"not a contribution table: {table!r}")


def open_readonly(vault_root: Path) -> sqlite3.Connection | None:
    """A read-only, non-waiting connection to this binary's sidecar, or None."""
    path = sidecar_path(Path(vault_root))
    if not path.is_file():
        return None
    try:
        conn = _open_readonly(path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    if row is None or str(row[0]) != str(SCHEMA_VERSION):
        conn.close()
        return None
    return conn


def _pair_group(subject: str, measures_json: Any) -> tuple[str, ...] | None:
    """Both directions of one link pair share this key; other rows have none."""
    try:
        measures = json.loads(measures_json) if isinstance(measures_json, str) else {}
    except ValueError:
        return None
    target = str((measures or {}).get("to") or "") if isinstance(measures, dict) else ""
    if not subject or not target:
        return None
    return (
        *sorted((subject, target)),
        str(measures.get("relation_type") or ""),
        str(measures.get("method") or ""),
    )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for column, key in (
        ("evidence_json", "evidence"),
        ("route_json", "route"),
        ("measures_json", "measures"),
    ):
        try:
            out[key] = json.loads(out.pop(column) or "null")
        except (TypeError, json.JSONDecodeError):
            out[key] = None
    out.pop("producers_json", None)
    out["deliverable"] = bool(out.get("deliverable"))
    return out


# ----------------------------------------------------------------------
# the carrier's read-only view
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StoreView:
    """One generation of the sidecar, parsed once for every carrier read."""

    generation: int
    instance: str
    candidates: tuple[dict[str, Any], ...]
    health: dict[str, Any]
    deliveries: tuple[tuple[str, str, str, float], ...]
    #: `(category, paths, fold_key)`; a fold key is judged per request.
    integrity: tuple[tuple[str, tuple[str, ...], str], ...]


_MEMO_LOCK = threading.Lock()
_MEMO: dict[str, StoreView] = {}
#: Each sidecar's file identity as this process last left it (see `connect`).
_LEFT: dict[str, tuple[int, int, int] | None] = {}


def clear_reader_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
    conn.execute("PRAGMA query_only=1")
    return conn


#: SQLite's own primary result codes for a database another connection holds,
#: a closed set the library documents.
_LOCK_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _failure(exc: sqlite3.Error) -> str:
    code = getattr(exc, "sqlite_errorcode", None)
    # An extended result code keeps its primary code in its low 8 bits.
    return "locked" if isinstance(code, int) and (code & 0xFF) in _LOCK_CODES else "unreadable"


def sidecar_condition(vault_root: Path) -> str:
    """Why `read_view` may answer None: `missing`, `schema_mismatch`, `locked`,
    `unreadable` or `readable`. Never creates the file and never waits on a lock."""
    path = sidecar_path(Path(vault_root))
    if not path.is_file():
        return "missing"
    try:
        conn = _open_readonly(path)
    except sqlite3.Error as exc:
        return _failure(exc)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error as exc:
        return _failure(exc)
    finally:
        conn.close()
    return "readable" if row is not None and row[0] == str(SCHEMA_VERSION) else "schema_mismatch"


def refused_condition(vault_root: Path) -> str:
    """Why `read_view` just answered None: `missing`, `schema_mismatch`, `locked`
    or `unreadable`. A file that probes readable now was refused a moment ago (a
    write in flight), so it reads as `unreadable`, never as nothing wrong."""
    condition = sidecar_condition(vault_root)
    return "unreadable" if condition == "readable" else condition


def read_view(vault_root: Path) -> StoreView | None:
    """The current view, or None when the sidecar is missing, locked or unreadable.

    Never creates the file and never waits: a carrier that finds nothing to
    read attaches nothing.
    """
    path = sidecar_path(Path(vault_root))
    if not path.is_file():
        return None
    key = str(path)
    try:
        conn = _open_readonly(path)
    except sqlite3.Error:
        return None
    try:
        meta = dict(
            conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('schema', 'generation', "
                "'instance', 'health')"
            ).fetchall()
        )
        if meta.get("schema") != str(SCHEMA_VERSION):
            return None
        generation = int(meta.get("generation") or 0)
        instance = str(meta.get("instance") or "")
        with _MEMO_LOCK:
            held = _MEMO.get(key)
        if held is not None and held.generation == generation and held.instance == instance:
            return held
        conn.row_factory = sqlite3.Row
        candidates = tuple(
            _row_dict(row)
            for row in conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
        )
        conn.row_factory = None
        deliveries = tuple(
            (str(a), str(b), str(c), float(d))
            for a, b, c, d in conn.execute(
                "SELECT id, fingerprint, caller_hash, delivered_at FROM deliveries"
            )
        )
        integrity = tuple(
            (str(category), tuple(json.loads(path_set or "[]")), str(fold_key or ""))
            for category, path_set, fold_key in conn.execute(
                "SELECT category, path_set, fold_key FROM integrity "
                "ORDER BY category, path_set, fold_key"
            )
        )
        try:
            health_raw = json.loads(meta.get("health") or "{}")
        except (TypeError, json.JSONDecodeError):
            health_raw = {}
        view = StoreView(
            generation=generation,
            instance=instance,
            candidates=candidates,
            health=_sanitize_health(health_raw) if isinstance(health_raw, Mapping) else {},
            deliveries=deliveries,
            integrity=integrity,
        )
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        conn.close()
    with _MEMO_LOCK:
        _MEMO[key] = view
    return view
