"""The review queues' whole-vault passes, run in the background and held in process memory.

A review listing ranks the findings of two whole-vault passes: the review audit (`audit.audit`
over every category a review item can come from) and the activation scan (`activation.scan`).
On a large vault each pass takes tens of seconds, and every listing paid for them again, as did
every decision that names an item by id. The serving runtime's refresh worker runs each pass and
this module holds the result. Nothing is stored: a restart starts empty.

The held pass holds findings and their coverage only. Everything that decides what a caller sees
(release decisions, review state, ranking) runs on copies, in the caller's own request.

- Only the refresh worker runs a pass. A request never runs one: it reads the held pass, or it
  runs its own audit of its own categories, as before. So no request does more work than before.
- A pass is held only for a caller from whom nothing can be withheld, and only such a caller
  reads it. Any other caller, a bound collection writer and a call with no principal bound keep
  the request path, so what they learn and what their request records do not change. A release
  verdict can depend on live state that no key names, so no pass is held for them.
- A held pass is current while the vault's change generation is the one the pass started at,
  the day is the day it ran, and it is younger than the maximum age.
- A read with no held pass keeps the request path and asks the worker for a pass.
- A read of a stale pass answers at once, and asks the worker for one refresh. The stale answer
  drops a finding whose page is gone and a due finding that the due-state projection no longer
  holds, and it names the pages that changed.
- When the last pass failed, nothing will replace a stale pass soon. A read of it then keeps the
  request path, until a pass succeeds.
- The worker runs a pass when a read asks for one. It also runs the first pass of the process
  once the vault has been quiet. Nothing else starts a pass: a pass that nobody reads is not
  renewed, and a failed pass is not tried again until a read asks.
- A pass runs every review category in one audit, so one failing check fails the pass. A failed
  pass replaces nothing. The gap before the next pass doubles with each failure in a row.
- A process with no refresh worker (a stdio server, the CLI, a standby) and a vault with no live
  change generation hold nothing.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import os
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import call_spans, foreground_activity, foreground_priority, freshness

if TYPE_CHECKING:
    from .audit import AuditFinding

log = logging.getLogger(__name__)

#: The two passes a review listing ranks.
AUDIT = "audit"
ACTIVATION = "activation"

#: The freshness scope whose generation moves on every Markdown change in the vault.
_SCOPE = "vault"
#: Held passes per process: two for each vault it serves.
_HELD_CAP = 16
#: After a failed pass the gap is counted from at least this duration, so that a check that fails
#: at once is not tried again at once.
_FAILED_PASS_SECONDS = 1.0
#: The gap stops doubling after this many failures in a row.
_GAP_DOUBLINGS = 6


def _setting(name: str, default: float) -> float:
    """A positive, finite number from the environment, or the default when unset or unusable."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    # A timer refuses a longer wait than `threading.TIMEOUT_MAX`.
    return min(value, threading.TIMEOUT_MAX) if value > 0 and math.isfinite(value) else default


def quiet_seconds() -> float:
    """How long the vault has no foreground call before a background refresh may start."""
    return _setting("EXOMEM_REVIEW_PASS_QUIET_SECONDS", 60.0)


def max_age_seconds() -> float:
    """The age after which a held pass is stale even when no page changed.

    Inputs other than Markdown pages (registries, governance, fixity state) do not move the change
    generation, so this bounds how long a pass can miss them.
    """
    return _setting("EXOMEM_REVIEW_PASS_MAX_AGE_SECONDS", 1800.0)


def gap_factor() -> float:
    """The pause before a pass may start, as a multiple of the last pass's duration."""
    return _setting("EXOMEM_REVIEW_PASS_GAP_FACTOR", 3.0)


def limit_seconds() -> float:
    """How long a background pass may run before it is abandoned.

    A background pass yields to each foreground call, so under load it runs several times longer
    than a pass in a request.
    """
    return _setting("EXOMEM_REVIEW_PASS_LIMIT_SECONDS", 1800.0)


@dataclass(frozen=True)
class _Pass:
    findings: tuple[AuditFinding, ...]
    coverage: Any
    generation: int
    day: dt.date
    as_of: str
    started: float
    #: Each finding page's key in the live freshness map and its signature when the pass started.
    pages: dict[str, tuple[str, Any]]
    #: `(category, path)` of each due finding that the due-state projection held after the pass.
    projected: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class View:
    """Copies of one held pass's findings, for one request to filter, rank and annotate."""

    findings: list[AuditFinding]
    coverage: Any
    #: Vault-relative paths of finding pages that changed after the pass started.
    changed: frozenset[str]
    #: When the pass started, in UTC.
    as_of: str
    #: Whether a background refresh of this pass is due or running.
    refreshing: bool


@dataclass
class _Worker:
    vault_root: Path
    stop: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    #: Keys that a read asked the worker to run, and the key it runs now.
    requested: set[tuple] = field(default_factory=set)
    running: tuple | None = None
    cancel: threading.Event | None = None

    def serving(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and not self.stop.is_set()


_LOCK = threading.Lock()
_HELD: dict[tuple, _Pass] = {}
#: `(end, duration, failures in a row)` of the last pass the worker tried for each key.
_LAST: dict[tuple, tuple[float, float, int]] = {}
_WORKERS: dict[str, _Worker] = {}


def view(
    vault_root: Path, part: str, *, categories: Collection[str] | None = None
) -> View | None:
    """The findings of the held `part` pass for this caller, or None to keep the request path.

    `categories` keeps the findings of those categories only. This never runs a pass: with no
    pass held it asks the worker for one, and the request runs its own audit as before.
    """
    if not _unrestricted(vault_root):
        return None
    generation = freshness.generation(vault_root, _SCOPE)
    if generation is None:
        return None
    key = (_canonical(vault_root), part)
    with _LOCK:
        held = _HELD.get(key)
        worker = _WORKERS.get(key[0])
        last = _LAST.get(key)
    if worker is None or not worker.serving():
        # Nothing keeps a pass current in this process.
        return None
    if held is None:
        _ask(worker, key)
        return None
    if not _current(held, generation):
        _ask(worker, key)
        if last is not None and last[2]:
            # The last pass failed, so this one would stay stale: answer as before.
            return None
    wanted = None if categories is None else frozenset(categories)
    return _view(vault_root, key, held, wanted, generation)


def _unrestricted(vault_root: Path) -> bool:
    """Whether nothing can be withheld from the bound caller: the one caller a pass is held for.

    The audit parses every page for this caller, and for no other. An unbound call is not this
    caller: the release plane gives it no filter, and the audit still withholds from it.
    """
    from .governance import egress
    from .governance import principal as principal_module

    return (
        principal_module.current_principal() is not None
        and not egress.caller_restricted(Path(vault_root))
    )


def _canonical(vault_root: Path) -> str:
    return str(Path(vault_root).resolve(strict=False))


def _current(held: _Pass, generation: int) -> bool:
    return (
        held.generation == generation
        and held.day == dt.datetime.now(dt.UTC).date()
        and time.monotonic() - held.started < max_age_seconds()
    )


def _compute(vault_root: Path, key: tuple, generation: int) -> _Pass:
    """Run one pass under the worker's principal, and record what a stale read compares with it."""
    part = key[1]
    started = time.monotonic()
    now = dt.datetime.now(dt.UTC)
    live = freshness.live_entries(vault_root, _SCOPE) or {}
    with call_spans.span("review_pass.compute", {"part": part}):
        if part == AUDIT:
            from . import attention, audit

            report = audit.audit(
                vault_root,
                categories=sorted(attention._TRIAGEABLE_CATEGORIES),
                today=now.date(),
                now=now,
            )
            findings, coverage = report.findings, (report.metadata or {}).get("coverage")
        else:
            from . import activation

            scan = activation.scan(vault_root)
            findings, coverage = scan.findings, scan.coverage
    root = Path(vault_root)
    pages: dict[str, tuple[str, Any]] = {}
    for finding in findings:
        for rel in (finding.path, *(finding.paths or ())):
            spelled = str(root / rel)
            if rel not in pages and spelled in live:
                pages[rel] = (spelled, live[spelled])
    return _Pass(
        findings=tuple(findings),
        coverage=coverage,
        generation=generation,
        day=now.date(),
        as_of=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        started=started,
        pages=pages,
        projected=_projected(vault_root, findings),
    )


def _projected(vault_root: Path, findings: list[AuditFinding]) -> frozenset[tuple[str, str]]:
    """`(category, path)` of each due finding that the due-state projection holds now."""
    from . import due_state

    due = {(f.category, f.path) for f in findings if f.category in due_state.PROJECTION_CATEGORIES}
    return (due_state.projected(vault_root, due) if due else None) or frozenset()


def _hold(key: tuple, fresh: _Pass) -> None:
    with _LOCK:
        held = _HELD.get(key)
        if held is not None and held.started > fresh.started:
            return
        _HELD.pop(key, None)
        while len(_HELD) >= _HELD_CAP:
            _HELD.pop(next(iter(_HELD)))
        _HELD[key] = fresh


def _view(
    vault_root: Path,
    key: tuple,
    held: _Pass,
    wanted: frozenset[str] | None,
    generation: int | None,
) -> View:
    findings = [f for f in held.findings if wanted is None or f.category in wanted]
    changed: set[str] = set()
    if generation != held.generation:
        findings, changed = _still_true(vault_root, held, findings)
    with _LOCK:
        worker = _WORKERS.get(key[0])
        refreshing = worker is not None and (key in worker.requested or worker.running == key)
    return View(
        # Ranking shares a finding's meta with the reason it builds, and review state annotates
        # reasons in place, so each request ranks its own copies.
        findings=[copy.deepcopy(f) for f in findings],
        coverage=copy.deepcopy(held.coverage),
        changed=frozenset(changed),
        as_of=held.as_of,
        refreshing=refreshing,
    )


def _still_true(
    vault_root: Path, held: _Pass, findings: list[AuditFinding]
) -> tuple[list[AuditFinding], set[str]]:
    """The findings a stale pass may still answer with, and the pages that changed since it.

    A page the pass saw and the live map no longer holds was moved or deleted. A due finding that
    the projection held after the pass and holds no longer was resolved by a write; a due finding
    the projection did not hold then is kept, because the projection lags edits made outside the
    service. A projection that cannot be read resolves nothing.
    """
    from . import due_state

    rels = sorted({
        rel for f in findings for rel in (f.path, *(f.paths or ())) if rel in held.pages
    })
    live = freshness.live_signatures(vault_root, _SCOPE, [held.pages[rel][0] for rel in rels])
    if live is None:
        # The change map stopped being live, so nothing here can judge a page.
        return findings, set()
    now = dict(zip(rels, live, strict=True))
    gone = {rel for rel in rels if now[rel] is None}
    changed = {rel for rel in rels if now[rel] is not None and now[rel] != held.pages[rel][1]}
    watched = {(f.category, f.path) for f in findings} & held.projected
    # One read of the projection for the listing. None: it cannot be read, so it resolves nothing.
    still = due_state.projected(vault_root, watched) if watched else None
    kept = [
        f
        for f in findings
        if not any(rel in gone for rel in (f.path, *(f.paths or ())))
        and (still is None or (f.category, f.path) not in watched or (f.category, f.path) in still)
    ]
    return kept, changed


# --- the background refresh -------------------------------------------------------------------


def start(vault_root: Path) -> threading.Thread | None:
    """Keep this vault's held passes current in the background. Idempotent."""
    canonical = _canonical(vault_root)
    with _LOCK:
        worker = _WORKERS.get(canonical)
        if worker is not None and worker.serving():
            return worker.thread
        worker = _Worker(Path(vault_root))
        worker.thread = threading.Thread(
            target=_run, args=(worker,), name="exomem-review-pass", daemon=True
        )
        _WORKERS[canonical] = worker
    worker.thread.start()
    return worker.thread


def stop(timeout: float = 2.0, *, worker: threading.Thread | None = None) -> None:
    """Stop the refresh worker that runs `worker`, or every one; a pass in flight is abandoned."""
    with _LOCK:
        stopping = [
            item for item in list(_WORKERS.values()) if worker is None or item.thread is worker
        ]
        for item in stopping:
            canonical = _canonical(item.vault_root)
            _WORKERS.pop(canonical, None)
            for part in (AUDIT, ACTIVATION):
                _HELD.pop((canonical, part), None)
            item.stop.set()
            item.wake.set()
            if item.cancel is not None:
                item.cancel.set()
    for item in stopping:
        if item.thread is not None and item.thread is not threading.current_thread():
            item.thread.join(timeout=timeout)


def _ask(worker: _Worker, key: tuple) -> None:
    with _LOCK:
        worker.requested.add(key)
    worker.wake.set()


def _run(worker: _Worker) -> None:
    canonical = _canonical(worker.vault_root)
    while not worker.stop.is_set():
        worker.wake.wait(min(5.0, quiet_seconds()))
        worker.wake.clear()
        for part in (AUDIT, ACTIVATION):
            if worker.stop.is_set():
                return
            key = (canonical, part)
            if _due(worker, key):
                _refresh(worker, key)


def _due(worker: _Worker, key: tuple) -> bool:
    """Whether the worker should run this pass now.

    A read asked for it, or the worker has not tried this pass yet and the vault has been quiet.
    Nothing else starts a pass. The last pass ended at least the gap ago, and the gap doubles
    with each failure in a row.
    """
    generation = freshness.generation(worker.vault_root, _SCOPE)
    if generation is None:
        return False
    with _LOCK:
        held = _HELD.get(key)
        last = _LAST.get(key)
        if held is not None and _current(held, generation):
            # Reads that asked while the last pass ran are answered by it.
            worker.requested.discard(key)
            return False
        asked = key in worker.requested
    if last is None:
        return asked or foreground_activity.idle_seconds(worker.vault_root) >= quiet_seconds()
    end, duration, failures = last
    if failures:
        duration = max(duration, _FAILED_PASS_SECONDS) * 2 ** min(failures, _GAP_DOUBLINGS)
    return asked and time.monotonic() - end >= gap_factor() * duration


def _refresh(worker: _Worker, key: tuple) -> None:
    """Run one pass at background priority; hold it only when it finished in time."""
    from .governance import principal as principal_module

    cancel = threading.Event()
    timer = threading.Timer(limit_seconds(), cancel.set)
    timer.daemon = True
    with _LOCK:
        worker.requested.discard(key)
        worker.running = key
        worker.cancel = cancel
    started = time.monotonic()
    fresh: _Pass | None = None
    try:
        with (
            principal_module.library_scope(),
            foreground_activity.background_scope(worker.vault_root),
            foreground_priority.cancellable(cancel),
        ):
            generation = freshness.generation(worker.vault_root, _SCOPE)
            # The worker's own caller must be the caller that a pass is held for.
            if generation is not None and _unrestricted(worker.vault_root):
                timer.start()
                fresh = _compute(worker.vault_root, key, generation)
        if cancel.is_set():
            log.warning("review pass took longer than %.0f s; abandoned", limit_seconds())
            fresh = None
    except foreground_priority.BulkCancelled:
        log.info("review pass abandoned")
    except Exception:  # noqa: BLE001 - a failed pass keeps the held one; a read asks for the next
        log.warning("review pass failed; the held pass stays", exc_info=True)
    finally:
        timer.cancel()
        with _LOCK:
            failures = 0 if fresh is not None else _LAST.get(key, (0.0, 0.0, 0))[2] + 1
            _LAST[key] = (time.monotonic(), time.monotonic() - started, failures)
            worker.running = None
            worker.cancel = None
    if fresh is not None:
        _hold(key, fresh)
