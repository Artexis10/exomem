"""Derive a store-mode page's rows from the intent that its commit queued.

A governed write commits page bytes, audit rows, projection intents and one derivation intent per path
in one short transaction under the vault mutation boundary (``write.commit``). The lexical, identity,
prior-path, page-fact, inbound and graph rows follow here, on the store thread, in transactions of
their own that never take that boundary: the store thread is the store's only writer, and it runs this
drain before it takes a request, a slice of at most ``SLICE_SECONDS`` a turn. A slice that its deadline
cut, with intents still due, yields the thread to the requests then queued
(``StoreServer._derive_owed``). A check that meets a page whose rows still wait reads that page from its
stored bytes (``underived_parsed``).

A batch takes whole revisions of intents in ``(revision, path)`` order, derives them with the code that
file mode uses, re-derives the pages whose links they move, deletes the intents and advances the store
revision. A batch that fails marks each of its intents, so that each is taken alone from then on.
A failed intent keeps its row and its reason code and retries alone, with a growing backoff, so it
holds back neither the other intents nor any reader; after ``ATTEMPTS`` status counts it as failed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, OperationalError
from typing import TYPE_CHECKING, TypeVar

from .. import find, foreground_priority, media_capacity, writer_lease
from ..cli_ops import OpError
from ..collection_store import connection
from . import derive, mode, read, rederive, resident_catalogue

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

_Parsed = TypeVar("_Parsed")

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

#: The most pending pages that a check which reads every one of them takes (``underived_parsed``):
#: 256 pages parse in about 0.15 s, at the 0.6 ms a page measured on the store thread, so a check
#: stays well inside a request's budget, and a backlog past it refuses the check until the drain
#: shrinks it.
UNDERIVED_CAP = 256
#: The longest that one stretch of derivation holds the store thread, whether the thread derives what
#: is owed (``StoreServer._derive_owed``) or a request derives its own intents (``committed``). It
#: stays well below the mutation timeout (5 s by default), so a queued request is taken in time; a
#: batch transaction runs whole, so a stretch ends at the first batch boundary after it, and the
#: first page of a failed batch always runs alone. A request acknowledges with its derived state
#: pending past it, and the store thread derives the rest in later stretches, between its requests.
SLICE_SECONDS = 1.0
#: The longest that a read waits for the derivation of the writes acknowledged before it began
#: (``caught_up``); its request budget may cut the wait shorter.
READ_WAIT_SECONDS = 1.0
#: The share of a request's remaining budget that the wait may take, leaving the rest to the read.
READ_WAIT_BUDGET_SHARE = 0.5
#: The first and the longest pause between two looks at the queue by a waiting reader. A drain
#: wakes the reader at once; a change that signals nothing is seen only at a look.
_LOOK_FIRST_SECONDS, _LOOK_MOST_SECONDS = 0.01, 0.1

#: The vault whose derived rows this context writes without the vault mutation boundary.
_SCOPE: ContextVar[Path | None] = ContextVar("vault_store_derivation_scope", default=None)
#: The ``own_rows`` block of this context, if one is open: its commits derive their rows before their
#: request acknowledges.
_OWN_ROWS: ContextVar[_Own | None] = ContextVar("vault_store_derivation_own_rows", default=None)
#: Signalled after each drain batch commits and after each failure is recorded, so a reader waiting
#: in this process (``caught_up``) looks again at once. ``_TURNS`` counts the signals, so a reader
#: that looked just before one does not sleep through it.
_DERIVED = threading.Condition()
_TURNS = 0

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


@dataclass(slots=True)
class _Own:
    """An ``own_rows`` block: the revision of its first commit, where its request's rows begin."""

    revision: int | None = None


@contextmanager
def own_rows() -> Iterator[None]:
    """Derive the rows of the commits in this block before their request acknowledges.

    For a governed trash or recovery, which settles its receipt from those rows after the release
    (``governed.settle``): its reply then reports the rows that its receipt was settled from. The
    request derives its own rows ahead of any older backlog (``drain``'s ``from_revision``): the
    store thread cannot wait for the rows that it derives itself, and a queue of older intents would
    otherwise take the whole slice and leave the receipt unsettled.
    """
    token = _OWN_ROWS.set(_Own())
    try:
        yield
    finally:
        _OWN_ROWS.reset(token)


def committed(writer: CollectionWriter, before: tuple, after: tuple, paths: tuple[str, ...]) -> None:
    """Follow a canonical commit: keep its caches current and derive its intents after the boundary.

    The resident catalogue moves to the commit's triple at once, since the commit wrote no catalogue
    row. A request on the store thread acknowledges at the commit: the thread derives the intents,
    and moves the resolver and corpus contexts, before it takes a request unless its last slice was
    cut (``StoreServer._derive_owed``), and a reader waits for them (``caught_up``). Any other
    request derives them once it has released the vault mutation boundary and before it
    acknowledges, for one slice (``SLICE_SECONDS``); so does an upload, whose rows draw on the room
    that its session reserved. A governed transition (``own_rows``) derives its own rows the same
    way, on the store thread, and an upload derives its own too: both ahead of the older intents. A
    caller with no boundary and no request (a test, an offline route) derives them here.
    """
    root = Path(writer.root)
    # An upload's derived rows draw on the room that its session reserved, as its page did.
    credit = media_capacity.held_commit_credit()
    own = _OWN_ROWS.get()
    if own is None and credit is not None:
        # The reservation ends with the commit's request, so its rows derive ahead of any older
        # backlog, as a governed transition's do: they begin at this commit's revision.
        own = _Own()
    if own is not None and own.revision is None:
        own.revision = after[1]
    # First, so that nothing below can strand the intents: should the request end before its follow
    # runs, the store thread drains before its next work.
    owe(root)
    resident_catalogue.advance(root, before, after)
    with _UNADVANCED_LOCK:
        _UNADVANCED.setdefault(root, []).append((before, after, tuple(paths)))
    if credit is None and own is None and writer_lease.terminal_follows():
        from ..collection_store import runtime

        if runtime.on_store_thread(root):
            return

    def follow() -> list[object]:
        try:
            with media_capacity.commit_credit(credit) if credit is not None else nullcontext():
                drain(
                    root, writer, deadline=time.monotonic() + SLICE_SECONDS,
                    from_revision=0 if own is None or own.revision is None else own.revision,
                )
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


#: The whole seconds since the oldest intent was queued, as SQLite's own clock reads them.
_OLDEST_AGE = (
    "CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', MIN(queued_at)) AS INTEGER)"
)


def lag(conn: Connection) -> tuple[int, int, int | str | None]:
    """The pending and the failed intents at ``conn``'s snapshot, and the age in seconds of the
    oldest one (None when none waits), for a reader's notice."""
    rows = _queue_rows(
        conn,
        "SELECT COUNT(*) FILTER (WHERE attempts < ?), COUNT(*) FILTER (WHERE attempts >= ?), "
        f"{_OLDEST_AGE} FROM store_derivation_queue",
        (ATTEMPTS, ATTEMPTS),
    )
    if rows is None:
        return 0, 0, None
    pending, failed, oldest = rows[0]
    return pending, failed, _age(oldest)


def _age(oldest: int | None) -> int | str | None:
    # A negative age is a clock that went backwards: it is not known, and it is never a zero.
    return mode.UNKNOWN if oldest is not None and oldest < 0 else oldest


def holds_back(conn: Connection, revision: int) -> bool:
    """Whether an intent at or below ``revision`` is still pending at ``conn``'s snapshot.

    One indexed read. An intent with a recorded failure holds no reader back: its retry is a second
    or more away, and status and the notice report it.
    """
    rows = _queue_rows(
        conn,
        "SELECT 1 FROM store_derivation_queue WHERE revision <= ? AND failed_code IS NULL LIMIT 1",
        (revision,),
    )
    return bool(rows)


def caught_up(still_held_back: Callable[[], bool], seconds: float) -> bool:
    """Wait until ``still_held_back()`` is False, for at most ``seconds``; whether it became False.

    The caller holds no store connection while it waits, and ``still_held_back`` opens one only for
    its look. Each drain batch and each recorded failure wakes the wait at once (``_signal``); a
    change that signals nothing, such as a later save that moves a page's intent past the reader's
    revision, is seen at the next look, after a pause that doubles up to ``_LOOK_MOST_SECONDS``. The
    waiting request lets the store thread run meanwhile, which a foreground request otherwise parks.
    """
    deadline = time.monotonic() + seconds
    pause = _LOOK_FIRST_SECONDS
    with foreground_priority.awaiting_bulk():
        while True:
            with _DERIVED:
                turn = _TURNS
            if not still_held_back():
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            with _DERIVED:
                if _TURNS == turn:
                    _DERIVED.wait(min(pause, left))
            pause = min(pause * 2, _LOOK_MOST_SECONDS)


def _pending_paths(conn: Connection, paths: Iterable[str] | None) -> dict[str, int]:
    """The ``derive`` intents that ``underived_parsed`` reads, each path with its ``attempts``, or
    the refusal that it makes."""
    wanted = None if paths is None else list(dict.fromkeys(paths))
    if wanted is None or len(wanted) > UNDERIVED_CAP:
        waiting = _queue_rows(
            conn,
            "SELECT path FROM store_derivation_queue WHERE operation = ? AND attempts < ? LIMIT ?",
            (DERIVE, ATTEMPTS, UNDERIVED_CAP + 1),
        )
        if waiting is not None and len(waiting) > UNDERIVED_CAP:
            raise read.backlog_too_large()
        rows = _queue_rows(
            conn, "SELECT path, attempts FROM store_derivation_queue WHERE operation = ?", (DERIVE,)
        )
        pending = dict(rows or ())
        if wanted is not None:
            keep = set(wanted)
            pending = {path: attempts for path, attempts in pending.items() if path in keep}
        return pending
    pending = {}
    for start in range(0, len(wanted), read.KEYS_PER_QUERY):
        batch = wanted[start : start + read.KEYS_PER_QUERY]
        rows = _queue_rows(
            conn,
            "SELECT path, attempts FROM store_derivation_queue WHERE operation = ? "
            f"AND path IN ({', '.join('?' * len(batch))})",
            (DERIVE, *batch),
        )
        pending.update(rows or ())
    return pending


def _bodies(conn: Connection, paths: list[str]) -> Iterator[tuple[str, bytes]]:
    """The stored bytes of each of ``paths`` that the store holds a page at, in keyed reads."""
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        yield from ((path, bytes(body)) for path, body in conn.execute(
            f"SELECT path, body FROM store_pages WHERE path IN ({', '.join('?' * len(batch))})", batch,
        ))


#: The parse of each failed page that a check reads (``underived_parsed``), by the check's kind and
#: the page's path: the ``sha256`` of the bytes that were parsed, and the result. A failed page
#: stays queued for as long as it fails, and every such check would parse it again, so a result is
#: kept while the page's bytes stay the same. A page that has not failed drains within moments, so
#: its parse is not kept: the memory follows the failed pages, never a backlog or a large page that
#: a reader that never drains would hold. Process memory is right for it: a result is a pure
#: function of the path and the bytes, and a lost entry costs one parse.
_PARSED: dict[str, dict[str, tuple[str, object]]] = {}
_PARSED_LOCK = threading.Lock()
#: The most results kept per kind. A failed page is rare and its result small, so this holds a
#: failed set far past any that a vault has met; a set past it reparses each check, as before the
#: cache.
PARSED_CAP = 2048


def underived_parsed(
    conn: Connection,
    paths: Iterable[str] | None,
    kind: str,
    parse: Callable[[str, bytes], _Parsed],
) -> dict[str, _Parsed | None]:
    """The pages whose derived rows trail their stored bytes at ``conn``'s snapshot, each with what
    ``parse(path, bytes)`` makes of its stored bytes.

    Each ``derive`` intent, pending or failed, maps its path to the page that the store holds there,
    or to None when it holds none (a delete, or the source of a move). A check that reads link or
    identity rows derives these pages' rows from their bytes in their place, by the rule that the
    drain applies, so a derivation that failed or waits for room never lets a write trust rows that
    its page no longer gives. A ``refresh`` intent stays out: it names a file that the store does not
    own, whose rows trail the file until its drain succeeds, as an outside edit's rows trail it until
    a commit sees the edit.

    With ``paths``, only those pages count, each in a keyed read: the cost follows the caller's own
    paths, whatever the backlog. A check that asks about those pages alone, or that asks whether the
    rows it found belong to a pending page, takes this form. Without ``paths``, every page counts,
    for a check that any pending page could change (a new link, a claimed id): its cost follows the
    backlog, so more than ``UNDERIVED_CAP`` pending pages refuse it with ``read.backlog_too_large()``
    before it reads one. The store thread derives the backlog between requests, so the retry finds it
    smaller. Only an intent that still has attempts left counts toward the cap: one that used up its
    ``ATTEMPTS`` keeps retrying on a growing backoff and never leaves the queue while it fails, so it
    would refuse every such check for good. Its bytes are read all the same, and status reports it as failed. An
    intent that failed fewer times is a backlog still, because a retry that lands removes it.

    Keyed paths that outnumber ``UNDERIVED_CAP`` could name every waiting page, so that call takes
    this form too.

    ``kind`` names what ``parse`` makes, one name per parse. A page that failed (its intent has
    attempts) stays here for as long as it fails, so it is parsed once while its bytes stay the
    same, whatever the number of checks that read it: the result is kept by the page's ``sha256``,
    so a page that changed is parsed anew, and it is dropped when the intent leaves the queue
    (``_forget``). That saves the parse and the copy of its bytes, not the read of its row: SQLite
    still reads the overflow pages of ``body``, which comes before ``sha256`` in the row. A page
    that has not failed is parsed on every check, as it drains soon.
    """
    pending = _pending_paths(conn, paths)
    found: dict[str, _Parsed | None] = dict.fromkeys(pending)
    failed = [path for path, attempts in pending.items() if attempts]
    hashes: dict[str, str] = {}
    for start in range(0, len(failed), read.KEYS_PER_QUERY):
        batch = failed[start : start + read.KEYS_PER_QUERY]
        marks = ", ".join("?" * len(batch))
        hashes.update(
            conn.execute(f"SELECT path, sha256 FROM store_pages WHERE path IN ({marks})", batch)
        )
    to_parse = [path for path, attempts in pending.items() if not attempts]
    with _PARSED_LOCK:
        kept = _PARSED.get(kind, {})
        for path, digest in hashes.items():
            held = kept.get(path)
            if held is not None and held[0] == digest:
                found[path] = held[1]  # type: ignore[assignment]  # this kind's own parse made it
            else:
                to_parse.append(path)
    for path, body in _bodies(conn, to_parse):
        found[path] = parsed = parse(path, body)
        if path in hashes:
            with _PARSED_LOCK:
                results = _PARSED.setdefault(kind, {})
                results.pop(path, None)  # a re-parsed page goes last, so the oldest goes first
                results[path] = (hashes[path], parsed)
                if len(results) > PARSED_CAP:
                    del results[next(iter(results))]
    return found


def _forget(paths: list[str]) -> None:
    """Drop the parsed results of ``paths``, whose intents left the queue."""
    with _PARSED_LOCK:
        for results in _PARSED.values():
            for path in paths:
                results.pop(path, None)


def figures(conn: Connection) -> dict:
    """The derived state for status: counts and the oldest age, never a path."""
    pending, failed, oldest = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE attempts < ?), COUNT(*) FILTER (WHERE attempts >= ?), "
        f"{_OLDEST_AGE} FROM store_derivation_queue",
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
        "oldest_derivation_age_seconds": _age(oldest),
    }


@dataclass(slots=True)
class Drained:
    """What one drain call did: the batches it committed, the intents that reached ``ATTEMPTS`` in it,
    whether it stopped with intents still due (a refusal, or a failure it could not record), and
    whether its deadline ended it while intents were still due (``cut``)."""

    batches: int = 0
    failed: int = 0
    stopped: bool = False
    cut: bool = False


def drain(
    root: Path, writer: CollectionWriter, *, deadline: float | None = None, from_revision: int = 0
) -> Drained:
    """Derive every due intent, a batch per transaction, until none is due or ``deadline`` passes.

    ``from_revision`` leaves the intents of older revisions to the store thread: a request derives
    only the intents that it queued itself (``own_rows``).

    A refusal to start a transaction (lease lost, store handed off) stops the drain and leaves every
    intent as it was. A batch that raises is rolled back and each of its intents is marked
    (``_record_failure``), so that none is taken in a batch again; its paths then run one by one, so
    a single bad page only delays itself. The first of them runs whatever the deadline, so that a
    batch that fails after the deadline still settles an intent; the next drain takes the others
    alone.
    """
    root = Path(root)
    result = Drained()
    with _UNADVANCED_LOCK:
        _OWED.pop(root, None)
    with scope(root):
        _advance(root, writer)
        while not result.stopped and (deadline is None or time.monotonic() < deadline):
            try:
                batch = _next_batch(writer, from_revision)
                if not batch:
                    break
                if _derive(root, writer, batch, result) or len(batch) == 1:
                    continue
                for position, intent in enumerate(batch):
                    if result.stopped or (position and deadline is not None and time.monotonic() >= deadline):
                        break
                    _derive(root, writer, [intent], result)
            except connection.CollectionStoreError:
                result.stopped = True
    due_now = _owe_remaining(root, writer, stopped=result.stopped)
    result.cut = due_now and deadline is not None and time.monotonic() >= deadline
    return result


def _owe_remaining(root: Path, writer: CollectionWriter, *, stopped: bool) -> bool:
    """Owe the next drain: now when an intent is due, else when the first retry falls due. Whether
    an intent is due now.

    A drain that a refusal stopped (a handoff, a lost lease) is owed after ``RETRY_SECONDS``, so the
    store thread does not spin on the refusal.
    """
    if stopped:
        owe(root, time.monotonic() + RETRY_SECONDS)
        return False
    try:
        due_now, first_retry = writer.connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM store_derivation_queue WHERE {_DUE}), "
            "MIN(retry_at) FROM store_derivation_queue",
            (_stamp(_now()),),
        ).fetchone()
    except Exception:  # noqa: BLE001 - a store that cannot be read is retried from its next start
        log.debug("the derivation queue could not be read after a drain", exc_info=True)
        return False
    if due_now:
        owe(root)
    elif first_retry is not None:
        wait = datetime.strptime(first_retry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - _now()
        owe(root, time.monotonic() + max(0.0, wait.total_seconds()))
    return bool(due_now)


@dataclass(frozen=True, slots=True)
class _Intent:
    path: str
    revision: int
    operation: str
    attempts: int
    metered: int
    failed_code: str | None


#: The columns that make an ``_Intent``, in the order of its fields.
_INTENT_COLUMNS = "path, revision, operation, attempts, metered, failed_code"


def _next_batch(writer: CollectionWriter, from_revision: int = 0) -> list[_Intent]:
    """The next due intents at ``from_revision`` or later: one that failed before runs alone, so that
    it never splits a batch of new intents; new ones run in whole revisions of up to ``BATCH_PAGES``
    pages."""
    first = writer.connection.execute(
        f"SELECT {_INTENT_COLUMNS} FROM store_derivation_queue WHERE {_DUE} "
        "AND revision >= ? ORDER BY revision, path LIMIT 1",
        (_stamp(_now()), from_revision),
    ).fetchone()
    if first is None:
        return []
    if first[3] > 0:
        return [_Intent(*first)]
    rows = writer.connection.execute(
        f"SELECT {_INTENT_COLUMNS} FROM store_derivation_queue WHERE {_DUE} "
        "AND revision >= ? AND attempts = 0 ORDER BY revision, path LIMIT ?",
        (_stamp(_now()), from_revision, BATCH_PAGES + 1),
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
        f"SELECT {_INTENT_COLUMNS} FROM store_derivation_queue WHERE {_DUE} "
        "AND attempts = 0 AND revision = ? ORDER BY path",
        (_stamp(_now()), last),
    ).fetchall()
    return [_Intent(*row) for row in rows]


def _derive(root: Path, writer: CollectionWriter, batch: list[_Intent], result: Drained) -> bool:
    """Derive ``batch`` in one transaction; on failure record it against each intent and return False."""
    from . import write

    started = False
    try:
        # Close the transaction before releasing capacity, including on rollback, as a commit does. A
        # derivation writes the vault store's derived rows alone, never a collection's.
        with ExitStack() as allocations, writer.handle.transaction(collection_free=True) as tx:
            started = True
            row = tx.execute("SELECT mode, revision FROM store_mode WHERE id = 1").fetchone()
            if row is None or row[0] != mode.STORE_MODE:
                # A rollback empties the queue; until it commits, nothing here is derived.
                result.stopped = True
                return True
            rederive.adopt_change_journal(tx)
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
                metered=frozenset(metered),
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
    _forget([intent.path for intent in batch])
    # After the transaction: a reader that meets the advanced catalogue must see this revision.
    resident_catalogue.remember(root, after, delta.catalogue)
    find.on_store_commit(root, before, after, delta.entries, delta.deleted, ())
    _signal()
    return True


def _signal() -> None:
    global _TURNS
    with _DERIVED:
        _TURNS += 1
        _DERIVED.notify_all()


def _stored(tx: Connection, paths: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(paths), read.KEYS_PER_QUERY):
        batch = paths[start : start + read.KEYS_PER_QUERY]
        found.update(rel for (rel,) in tx.execute(
            f"SELECT path FROM store_pages WHERE path IN ({', '.join('?' * len(batch))})", batch,
        ))
    return found


def _record_failure(writer: CollectionWriter, batch: list[_Intent], error: Exception, result: Drained) -> None:
    """Record a failure against each intent of a failed batch, or against the one that failed.

    A batch of several intents cannot say which of them failed: each is marked (attempts 1, no
    reason code and no retry time), and so stays due and still holds a reader back, but
    ``_next_batch`` takes an intent that has attempts alone from then on. The drain runs them one by
    one at once, and a later turn takes the rest the same way, so one bad page never fails the batch
    of the others again. The mark counts no attempt: a page's own failures are the ones that make it
    failed, as before a batch was marked. The mark survives a restart, as the queue does.
    """
    if len(batch) > 1:
        log.info("a derivation batch failed (%s); deriving its paths one by one", type(error).__name__)
        try:
            with writer.handle.transaction() as tx:
                # Only the intents that failed: a newer write to a path reset its row meanwhile.
                tx.executemany(
                    "UPDATE store_derivation_queue SET attempts = 1 "
                    "WHERE path = ? AND revision = ? AND attempts = 0",
                    [(intent.path, intent.revision) for intent in batch],
                )
        except Exception:  # noqa: BLE001 - the batch then fails again, as it did before the count
            log.warning("a failed derivation batch could not be recorded", exc_info=True)
        return
    [intent] = batch
    code = NO_ROOM if isinstance(error, OpError) and error.code == media_capacity.UPLOAD_SPACE_BUSY else FAILED
    # A batch's mark (attempts 1, no code) counted nothing that left a reason, so the first failure
    # on its own is the page's first.
    attempts = intent.attempts + 1 if intent.failed_code else 1
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
    # A recorded failure no longer holds a reader back.
    _signal()
    if attempts == ATTEMPTS:
        result.failed += 1
