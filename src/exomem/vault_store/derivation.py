"""Derive a store-mode page's rows from the intent that its commit queued.

A governed write commits page bytes, audit rows, projection intents and one derivation intent per path
in one short transaction under the vault mutation boundary (``write.commit``). The lexical, identity,
prior-path, page-fact, inbound and graph rows follow here, on the store thread, in transactions of
their own that never take that boundary: the store thread is the store's only writer, and it runs this
drain before any other work, so every later write and check sees the rows of every earlier commit.

A batch takes whole revisions of intents in ``(revision, path)`` order, derives them with the code that
file mode uses, re-derives the pages whose links they move, deletes the intents and advances the store
revision. A failed intent keeps its row and its reason code and retries alone, with a growing backoff, so
it holds back neither the other intents nor any reader; after ``ATTEMPTS`` status counts it as failed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, OperationalError
from typing import TYPE_CHECKING

from .. import find, media_capacity, writer_lease
from ..cli_ops import OpError
from ..collection_store import connection
from . import derive, mode, read, resident_catalogue

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

#: The queue's operations, a closed set that collection schema 20 fixes: derive the rows of a path
#: from its stored page (or drop them when the store holds no page there), or refresh a file that the
#: store does not own from the file.
DERIVE, REFRESH = "derive", "refresh"
#: The reason codes of a failed intent, a closed set of this protocol: the vault had no room for the
#: rows, or the derivation raised.
NO_ROOM, FAILED = "no_room", "derivation_failed"
#: How many failed attempts make status count an intent as failed; it keeps retrying.
ATTEMPTS = 3
#: The most pages that one drain transaction derives; a revision with more runs whole.
BATCH_PAGES = 32
#: The seconds before the first retry; each later one waits twice as long, up to the most.
RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 300.0

#: The seconds that a writing request derives its own intents for before it acknowledges with its
#: derived state pending; the store thread derives the rest before its next request.
OWN_DERIVATION_SECONDS = 30.0

#: The vault whose derived rows this context writes without the vault mutation boundary.
_SCOPE: ContextVar[Path | None] = ContextVar("vault_store_derivation_scope", default=None)

#: Per vault, the commits whose resident caches still stand where the commit found them, in order:
#: ``(before, after, paths)``. The next drain moves the caches across them (``_advance``).
_UNADVANCED: dict[Path, list[tuple[tuple, tuple, tuple[str, ...]]]] = {}
_UNADVANCED_LOCK = threading.Lock()
#: Per vault, the monotonic time from which the store thread owes a drain: set by a commit, by a start
#: and by a drain that left due or retrying intents. A plain dict read, so an idle loop pays nothing.
_OWED: dict[Path, float] = {}


@contextmanager
def scope(root: Path) -> Iterator[None]:
    """Let the store's lease check accept derived-row work on ``root`` without the vault boundary.

    Only this module and the registry job (``rederive.settle``) enter it, and both write derived rows
    only: ``write.commit`` refuses inside it, so a canonical commit still needs the boundary.
    """
    token = _SCOPE.set(Path(root).resolve())
    try:
        yield
    finally:
        _SCOPE.reset(token)


def in_scope(root: Path | None = None) -> bool:
    """Whether this context derives rows, for ``root`` when one is given."""
    current = _SCOPE.get()
    return current is not None and (root is None or current == Path(root).resolve())


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(at: datetime) -> str:
    # The projection queue's whole-second UTC format, which SQLite's own clock reads.
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(
    tx: Connection, derived: list[str], refreshed: list[str], revision: int, now: str,
    metered: frozenset[str] = frozenset(),
) -> None:
    """Queue one intent per path in the commit's own transaction.

    The upsert moves the revision forward and keeps the oldest queue time; new bytes clear an earlier
    failure, so the path is derived again. ``metered`` names the paths of an original, whose derived
    rows the vault's disk floor meters when the drain writes them.
    """
    tx.executemany(
        "INSERT INTO store_derivation_queue(path, revision, operation, queued_at, metered) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET revision = excluded.revision, operation = excluded.operation, "
        "metered = excluded.metered, attempts = 0, failed_code = NULL, retry_at = NULL",
        [*((rel, revision, DERIVE, now, int(rel in metered)) for rel in derived),
         *((rel, revision, REFRESH, now, 0) for rel in refreshed)],
    )


def committed(writer: CollectionWriter, before: tuple, after: tuple, paths: tuple[str, ...]) -> None:
    """Follow a canonical commit: keep its caches current and derive its intents after the boundary.

    The resident catalogue moves to the commit's triple at once, since the commit wrote no catalogue
    row. The resolver and corpus contexts move, and the intents are derived, once the writing request
    has released the vault mutation boundary and before it acknowledges. A caller with no boundary
    and no request (a test, an offline route) derives them here.
    """
    root = Path(writer.root)
    # First, so that nothing below can strand the intents: should the request end before its follow
    # runs, the store thread drains before its next work.
    owe(root)
    resident_catalogue.advance(root, before, after)
    with _UNADVANCED_LOCK:
        _UNADVANCED.setdefault(root, []).append((before, after, tuple(paths)))
    # An upload's derived rows draw on the room that its session reserved, as its page did.
    credit = media_capacity.held_commit_credit()

    def follow() -> list[object]:
        try:
            with media_capacity.commit_credit(credit) if credit is not None else nullcontext():
                drain(root, writer, deadline=time.monotonic() + OWN_DERIVATION_SECONDS)
        except Exception:  # noqa: BLE001 - the page is durable; the store thread drains it again
            log.warning("the intents of a commit to %s could not be derived yet", root, exc_info=True)
            owe(root, time.monotonic() + RETRY_SECONDS)
        return []

    if writer_lease.defer_until_terminal_persisted(follow):
        return
    try:
        coordinator = writer_lease.active_manager()._mutation_coordinator_for(root)
        deferred = coordinator.current_thread_holds_boundary() and coordinator.defer_optional_after_release(
            (follow.__qualname__, root), follow
        )
    except Exception:  # noqa: BLE001 - with no coordinator to defer to, the caller derives them now
        log.debug("no boundary to follow the commit to %s after", root, exc_info=True)
        deferred = False
    if not deferred:
        follow()


def owe(root: Path, at: float | None = None) -> None:
    """Record that the store thread owes ``root`` a drain from ``at`` (monotonic; now by default)."""
    when = time.monotonic() if at is None else at
    key = Path(root)
    with _UNADVANCED_LOCK:
        _OWED[key] = min(_OWED.get(key, when), when)


def owe_after_failure(root: Path, at: float) -> None:
    """Owe the next drain of ``root`` from ``at`` and no sooner, after a drain step that raised.

    It replaces the owed time where ``owe`` keeps the earlier one: the time that the failed step was
    owed from has passed, so keeping it would retry at once, in a loop.
    """
    with _UNADVANCED_LOCK:
        _OWED[Path(root)] = at


def settled(root: Path) -> None:
    """Forget an owed drain of ``root``: a file-mode vault has no queue to drain."""
    with _UNADVANCED_LOCK:
        _OWED.pop(Path(root), None)


def owed(root: Path) -> bool:
    """Whether a drain of ``root`` is owed now."""
    with _UNADVANCED_LOCK:
        when = _OWED.get(Path(root))
    return when is not None and when <= time.monotonic()


def owed_in(root: Path) -> float | None:
    """The seconds until a drain of ``root`` is owed, or None when none is."""
    with _UNADVANCED_LOCK:
        when = _OWED.get(Path(root))
    return None if when is None else max(0.0, when - time.monotonic())


def started(root: Path, writer: CollectionWriter) -> None:
    """Make every intent due when the store thread opens the store: a start retries each failed one."""
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE store_derivation_queue SET retry_at = NULL WHERE retry_at IS NOT NULL")
        waiting = tx.execute("SELECT 1 FROM store_derivation_queue LIMIT 1").fetchone() is not None
    if waiting:
        owe(root)


def _advance(root: Path, writer: CollectionWriter) -> None:
    """Move the resolver and corpus contexts across the commits since the last drain, as one step.

    The commits since the last drain are contiguous, so one advance from the first one's triple to
    the last one's, over all of their paths, holds the same maps as one per commit. A store that moved
    since (the overlay job, another drain) leaves the contexts to their next reader.
    """
    with _UNADVANCED_LOCK:
        commits = _UNADVANCED.pop(root, [])
    if not commits:
        return
    contiguous = all(earlier[1] == later[0] for earlier, later in zip(commits, commits[1:], strict=False))
    revision = writer.connection.execute("SELECT revision FROM store_mode WHERE id = 1").fetchone()
    if not contiguous or revision is None or revision[0] != commits[-1][1][1]:
        return
    paths = tuple(dict.fromkeys(rel for _before, _after, named in commits for rel in named))
    find.on_store_commit(root, commits[0][0], commits[-1][1], (), (), paths)


_DUE = "(retry_at IS NULL OR retry_at <= ?)"


def due(conn: Connection) -> bool:
    """Whether an intent waits to be derived now: one indexed read, cheap when the queue is empty."""
    return conn.execute(
        f"SELECT 1 FROM store_derivation_queue WHERE {_DUE} LIMIT 1", (_stamp(_now()),)
    ).fetchone() is not None


def pending_paths(conn: Connection, paths: list[str]) -> set[str]:
    """Which of ``paths`` still have an intent, failed or not."""
    found: set[str] = set()
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        found.update(rel for (rel,) in conn.execute(
            f"SELECT path FROM store_derivation_queue WHERE path IN ({', '.join('?' * len(batch))})",
            batch,
        ))
    return found


def outcome(conn: Connection, paths: list[str]) -> tuple[str, str]:
    """The derived state of ``paths`` as an index outcome and its code, from the queue.

    ``completed`` when no intent names one of them, ``failed`` with the reason code when one reached
    ``ATTEMPTS``, else ``deferred``: the store thread still derives it.
    """
    worst: tuple[int, str | None] | None = None
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        found = conn.execute(
            "SELECT MAX(attempts), MAX(failed_code) FROM store_derivation_queue "
            f"WHERE path IN ({', '.join('?' * len(batch))}) HAVING COUNT(*) > 0",
            batch,
        ).fetchone()
        if found is not None and (worst is None or found[0] > worst[0]):
            worst = found
    if worst is None:
        return "completed", "store_derivation"
    if worst[0] >= ATTEMPTS:
        return "failed", f"store_derivation_{worst[1] or FAILED}"
    return "deferred", "store_derivation_queue"


def _queue_rows(conn: Connection, sql: str, parameters: tuple = ()) -> list[tuple] | None:
    """The rows of ``sql`` on the queue, or None on a store that an earlier release wrote.

    That store has no queue: its release derived every page's rows in the page's own transaction, so
    nothing lags.
    """
    try:
        return conn.execute(sql, parameters).fetchall()
    except OperationalError:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_derivation_queue'"
        ).fetchone() is not None:
            raise
        return None


def lag(conn: Connection) -> tuple[int, int]:
    """The pending and the failed intents at ``conn``'s snapshot, for a reader's notice."""
    rows = _queue_rows(
        conn,
        "SELECT COUNT(*) FILTER (WHERE attempts < ?), COUNT(*) FILTER (WHERE attempts >= ?) "
        "FROM store_derivation_queue",
        (ATTEMPTS, ATTEMPTS),
    )
    return (0, 0) if rows is None else rows[0]


def underived(conn: Connection) -> dict[str, bytes | None]:
    """The pages whose derived rows trail their stored bytes at ``conn``'s snapshot, with those bytes.

    Each ``derive`` intent, pending or failed, maps its path to the page that the store holds there,
    or to None when it holds none (a delete, or the source of a move). A check that reads link or
    identity rows derives these pages' rows from these bytes in their place, by the rule that the
    drain applies, so a derivation that failed or waits for room never lets a write trust rows that
    its page no longer gives. A ``refresh`` intent stays out: it names a file that the store does not
    own, whose rows trail the file until its drain succeeds, as an outside edit's rows trail it until
    a commit sees the edit. The cost follows the lag.
    """
    rows = _queue_rows(conn, "SELECT path FROM store_derivation_queue WHERE operation = ?", (DERIVE,))
    if not rows:
        return {}
    paths = [path for path, in rows]
    found: dict[str, bytes | None] = dict.fromkeys(paths)
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        found.update((path, bytes(body)) for path, body in conn.execute(
            f"SELECT path, body FROM store_pages WHERE path IN ({', '.join('?' * len(batch))})", batch,
        ))
    return found


def figures(conn: Connection) -> dict:
    """The derived state for status: counts and the oldest age, never a path."""
    pending, failed, oldest = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE attempts < ?), COUNT(*) FILTER (WHERE attempts >= ?), "
        "CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', MIN(queued_at)) AS INTEGER) "
        "FROM store_derivation_queue",
        (ATTEMPTS, ATTEMPTS),
    ).fetchone()
    by_code = dict(conn.execute(
        "SELECT failed_code, COUNT(*) FROM store_derivation_queue WHERE attempts >= ? GROUP BY failed_code",
        (ATTEMPTS,),
    ).fetchall())
    state = "failed" if failed else "behind" if pending else "current"
    return {
        "derived": state,
        "derived_pending": pending,
        "derived_failed": failed,
        "derived_failed_by_code": by_code,
        # A negative age is a clock that went backwards: it is not known, and it is never a zero.
        "oldest_derivation_age_seconds": mode.UNKNOWN if oldest is not None and oldest < 0 else oldest,
    }


@dataclass(slots=True)
class Drained:
    """What one drain call did: the batches it committed, the intents that reached ``ATTEMPTS`` in it,
    and whether it stopped with intents still due (a refusal, or a failure it could not record)."""

    batches: int = 0
    failed: int = 0
    stopped: bool = False


def drain(root: Path, writer: CollectionWriter, *, deadline: float | None = None) -> Drained:
    """Derive every due intent, a batch per transaction, until none is due or ``deadline`` passes.

    A refusal to start a transaction (lease lost, store handed off) stops the drain and leaves every
    intent as it was. A batch that raises is rolled back and its paths run one by one, so a single bad
    page only delays itself.
    """
    root = Path(root)
    result = Drained()
    with _UNADVANCED_LOCK:
        _OWED.pop(root, None)
    with scope(root):
        _advance(root, writer)
        while not result.stopped and (deadline is None or time.monotonic() < deadline):
            try:
                batch = _next_batch(writer)
                if not batch:
                    break
                if _derive(root, writer, batch, result) or len(batch) == 1:
                    continue
                for intent in batch:
                    if result.stopped or (deadline is not None and time.monotonic() >= deadline):
                        break
                    _derive(root, writer, [intent], result)
            except connection.CollectionStoreError:
                result.stopped = True
    _owe_remaining(root, writer, stopped=result.stopped)
    return result


def _owe_remaining(root: Path, writer: CollectionWriter, *, stopped: bool) -> None:
    """Owe the next drain: now when an intent is due, else when the first retry falls due.

    A drain that a refusal stopped (a handoff, a lost lease) is owed after ``RETRY_SECONDS``, so the
    store thread does not spin on the refusal.
    """
    if stopped:
        owe(root, time.monotonic() + RETRY_SECONDS)
        return
    try:
        due_now, first_retry = writer.connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM store_derivation_queue WHERE {_DUE}), "
            "MIN(retry_at) FROM store_derivation_queue",
            (_stamp(_now()),),
        ).fetchone()
    except Exception:  # noqa: BLE001 - a store that cannot be read is retried from its next start
        log.debug("the derivation queue could not be read after a drain", exc_info=True)
        return
    if due_now:
        owe(root)
    elif first_retry is not None:
        wait = datetime.strptime(first_retry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - _now()
        owe(root, time.monotonic() + max(0.0, wait.total_seconds()))


@dataclass(frozen=True, slots=True)
class _Intent:
    path: str
    revision: int
    operation: str
    attempts: int
    metered: int


def _next_batch(writer: CollectionWriter) -> list[_Intent]:
    """The next due intents: one that failed before runs alone, so that it never splits a batch of new
    intents; new ones run in whole revisions of up to ``BATCH_PAGES`` pages."""
    first = writer.connection.execute(
        f"SELECT path, revision, operation, attempts, metered FROM store_derivation_queue WHERE {_DUE} "
        "ORDER BY revision, path LIMIT 1",
        (_stamp(_now()),),
    ).fetchone()
    if first is None:
        return []
    if first[3] > 0:
        return [_Intent(*first)]
    rows = writer.connection.execute(
        f"SELECT path, revision, operation, attempts, metered FROM store_derivation_queue WHERE {_DUE} "
        "AND attempts = 0 ORDER BY revision, path LIMIT ?",
        (_stamp(_now()), BATCH_PAGES + 1),
    ).fetchall()
    intents = [_Intent(*row) for row in rows]
    if len(intents) <= BATCH_PAGES:
        return intents
    # Whole revisions only, so that one transaction never derives half of a commit's pages; a single
    # revision larger than the bound runs whole.
    last = intents[BATCH_PAGES].revision
    whole = [intent for intent in intents[:BATCH_PAGES] if intent.revision != last]
    if whole:
        return whole
    rows = writer.connection.execute(
        f"SELECT path, revision, operation, attempts, metered FROM store_derivation_queue WHERE {_DUE} "
        "AND attempts = 0 AND revision = ? ORDER BY path",
        (_stamp(_now()), last),
    ).fetchall()
    return [_Intent(*row) for row in rows]


def _derive(root: Path, writer: CollectionWriter, batch: list[_Intent], result: Drained) -> bool:
    """Derive ``batch`` in one transaction; on failure record it against each intent and return False."""
    from . import write

    started = False
    try:
        # Close the transaction before releasing capacity, including on rollback, as a commit does.
        with ExitStack() as allocations, writer.handle.transaction() as tx:
            started = True
            row = tx.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
            if row is None or row[0] != mode.STORE_MODE:
                # A rollback empties the queue; until it commits, nothing here is derived.
                result.stopped = True
                return True
            stored = _stored(tx, [intent.path for intent in batch if intent.operation == DERIVE])
            written = [intent.path for intent in batch if intent.path in stored]
            removed = [intent.path for intent in batch
                       if intent.operation == DERIVE and intent.path not in stored]
            refreshed = [intent.path for intent in batch if intent.operation == REFRESH]
            before = read.vault_triple(tx, row[1])
            metered = {intent.path for intent in batch if intent.metered}
            prepared, reserve_graph, settle = allocations.enter_context(write.derived_room(
                writer, tx, [rel for rel in written if rel in metered],
                tuple(rel for rel in removed if rel in metered),
            ))
            delta = derive.derive_paths(
                tx, root, written=written, removed=removed, refreshed=refreshed,
                before=before, prepared_lexical=prepared, reserve_graph=reserve_graph,
            )
            settle()
            tx.executemany(
                "DELETE FROM store_derivation_queue WHERE path = ?", [(intent.path,) for intent in batch]
            )
            revision = row[1] + 1
            tx.execute("UPDATE store_mode SET revision = ? WHERE id = 1", (revision,))
            after = read.vault_triple(tx, revision)
    except Exception as error:  # noqa: BLE001 - a failed intent is recorded and retried, never lost
        if not started:
            raise  # a refusal before the transaction (lease, handoff) is no attempt: the drain stops
        _record_failure(writer, batch, error, result)
        return False
    result.batches += 1
    # After the transaction: a reader that meets the advanced catalogue must see this revision.
    resident_catalogue.remember(root, after, delta.catalogue)
    find.on_store_commit(root, before, after, delta.entries, delta.deleted, ())
    return True


def _stored(tx: Connection, paths: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        found.update(rel for (rel,) in tx.execute(
            f"SELECT path FROM store_pages WHERE path IN ({', '.join('?' * len(batch))})", batch,
        ))
    return found


def _record_failure(writer: CollectionWriter, batch: list[_Intent], error: Exception, result: Drained) -> None:
    """Count one attempt against each intent of a failed single-path batch; a larger batch is split first."""
    if len(batch) > 1:
        log.info("a derivation batch failed (%s); deriving its paths one by one", type(error).__name__)
        return
    [intent] = batch
    code = NO_ROOM if isinstance(error, OpError) and error.code == media_capacity.UPLOAD_SPACE_BUSY else FAILED
    attempts = intent.attempts + 1
    log.warning("a store page's derivation failed (%s, attempt %d)", type(error).__name__, attempts,
                exc_info=not isinstance(error, OpError))
    wait = min(RETRY_SECONDS * 2 ** (attempts - 1), MAX_RETRY_SECONDS)
    try:
        with writer.handle.transaction() as tx:
            # Only the intent that failed: a newer write to the path reset its row meanwhile.
            tx.execute(
                "UPDATE store_derivation_queue SET attempts = ?, retry_at = ?, failed_code = ? "
                "WHERE path = ? AND revision = ?",
                (attempts, _stamp(_now() + timedelta(seconds=wait)), code, intent.path, intent.revision),
            )
    except Exception:  # noqa: BLE001 - the intent stays due, and the next drain counts this attempt
        log.warning("a failed derivation could not be recorded", exc_info=True)
        # Still due, it would be taken again at once: this drain ends, and the next one retries it.
        result.stopped = True
        return
    if attempts == ATTEMPTS:
        result.failed += 1
