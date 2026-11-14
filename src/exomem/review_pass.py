"""The review queues' whole-vault passes, held in process memory for the unrestricted caller.

A review listing ranks the findings of two whole-vault passes: the review audit (`audit.audit`
over every category a review item can come from) and the activation scan (`activation.scan`).
On a large vault each pass takes tens of seconds, and every listing paid for them again, as did
every decision that names an item by id. This module runs each pass once and holds the result.
Nothing is stored: a restart starts empty.

The held pass holds findings and their coverage only. Everything that decides what a caller sees
(release decisions, review state, ranking) runs on copies, in the caller's own request.

- A pass is held only for a caller from whom nothing can be withheld, and only such a caller
  reads it. Any other caller, a bound collection writer and a call with no principal bound run
  their own pass in the request, so what they learn and what their request records do not
  change. A release verdict can depend on live state that no key names, so no pass is held for
  them.
- A held pass is current while the vault's change generation is the one the pass started at,
  the day is the day it ran, and it is younger than the maximum age.
- A read with no held pass computes the pass in its request and holds the result. A reader that
  arrives meanwhile waits for that one pass.
- A pass runs every review category in one audit, so one failing check fails the pass. A failed
  pass holds nothing and replaces nothing: the read runs its own audit of its own categories, as
  before, and no read tries the pass again until the gap after the failure has passed.
- A read of a stale pass answers at once, and one background refresh replaces the pass. The
  stale answer drops a finding whose page is gone and a due finding that the due-state
  projection no longer holds, and it names the pages that changed. A process with no refresh
  worker computes instead.
- The refresh worker also runs a pass when the vault has been quiet and no pass is held, a page
  changed or the day changed. Age alone starts no pass: a pass that nobody reads is not renewed.
- A vault with no live change generation holds nothing.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import call_spans, foreground_activity, foreground_priority, freshness, request_budget

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


def _setting(name: str, default: float) -> float:
    """A positive number from the environment, or the default when unset or unusable."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


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
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Worker:
    vault_root: Path
    stop: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    #: Keys that a stale read asked to refresh, and the key being refreshed now.
    requested: set[tuple] = field(default_factory=set)
    running: tuple | None = None
    cancel: threading.Event | None = None

    def serving(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and not self.stop.is_set()


_LOCK = threading.Lock()
_HELD: dict[tuple, _Pass] = {}
_FLIGHTS: dict[tuple, _Flight] = {}
#: `(end, duration, failed)` of the last pass of each key. A background pass waits a gap after
#: it, and so does a pass in a request after a failure.
_LAST: dict[tuple, tuple[float, float, bool]] = {}
_WORKERS: dict[str, _Worker] = {}


def view(
    vault_root: Path, part: str, *, categories: Collection[str] | None = None
) -> View | None:
    """The findings of the held `part` pass for this caller, or None to compute in the request.

    `categories` keeps the findings of those categories only.
    """
    if request_budget.current_checkpoint() is not None:
        # A read checkpoint can cut a pass short, and a partial pass must never be held.
        return None
    if not _unrestricted(vault_root):
        return None
    key = (_canonical(vault_root), part)
    wanted = None if categories is None else frozenset(categories)
    while True:
        generation = freshness.generation(vault_root, _SCOPE)
        if generation is None:
            return None
        with _LOCK:
            held = _HELD.get(key)
            flight = _FLIGHTS.get(key)
            worker = _WORKERS.get(key[0])
        if held is not None:
            if _current(held, generation):
                return _view(vault_root, key, held, wanted, generation)
            if worker is not None and worker.serving():
                _ask(worker, key)
                return _view(vault_root, key, held, wanted, generation)
        if flight is not None:
            if not flight.done.wait(limit_seconds()):
                # The pass outlived the limit a background pass gets: compute in the request.
                return None
            continue
        if _failed_lately(key):
            return None
        flight = _Flight()
        with _LOCK:
            if key in _FLIGHTS:
                continue
            _FLIGHTS[key] = flight
        try:
            fresh = _compute(vault_root, key, generation)
        except Exception:  # noqa: BLE001 - the read runs its own audit, which reports its own failure
            log.warning("review pass failed; this read computes its own", exc_info=True)
            return None
        finally:
            with _LOCK:
                _FLIGHTS.pop(key, None)
            flight.done.set()
        _hold(key, fresh)
        return _view(vault_root, key, fresh, wanted, freshness.generation(vault_root, _SCOPE))


def _failed_lately(key: tuple) -> bool:
    """Whether the last pass of this key failed and its gap has not passed."""
    with _LOCK:
        last = _LAST.get(key)
    return last is not None and last[2] and time.monotonic() - last[0] < gap_factor() * last[1]


def _unrestricted(vault_root: Path) -> bool:
    """Whether nothing can be withheld from the bound caller: the one caller a pass is held for.

    The audit parses every page for this caller, and for no other. An unbound call is not this
    caller: the release plane gives it no filter, and the audit still withholds from it.
    """
    from .governance import egress
    from .governance import principal as principal_module

    return (
        principal_module.current_principal() is not None
        and egress.restricted_release_filter(Path(vault_root)) is None
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
    """Run one pass under the bound principal, and record what a stale read compares with it."""
    part = key[1]
    started = time.monotonic()
    now = dt.datetime.now(dt.UTC)
    live = freshness.live_entries(vault_root, _SCOPE) or {}
    failed = True
    try:
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
        failed = False
    finally:
        with _LOCK:
            _LAST[key] = (time.monotonic(), time.monotonic() - started, failed)
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
    from . import due_state

    due = [f for f in findings if f.category in due_state.PROJECTION_CATEGORIES]
    if not due:
        return frozenset()
    payload = due_state._load_shared(vault_root)
    return frozenset((f.category, f.path) for f in due if _holds(payload, f.category, f.path))


def _holds(payload: dict[str, Any] | None, category: str, path: str) -> bool:
    """Whether the stored due-state projection holds an entry for this page and category."""
    from . import due_state

    pages = ((payload or {}).get("categories") or {}).get(category)
    return isinstance(pages, dict) and bool(due_state._unbucket(pages.get(path)))


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
    payload = (
        due_state._load_shared(vault_root)
        if any((f.category, f.path) in held.projected for f in findings)
        else None
    )
    kept = [
        f
        for f in findings
        if not any(rel in gone for rel in (f.path, *(f.paths or ())))
        and (
            payload is None
            or (f.category, f.path) not in held.projected
            or _holds(payload, f.category, f.path)
        )
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
            _WORKERS.pop(_canonical(item.vault_root), None)
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

    A read of a stale pass asked for it. Or the vault has been quiet, and no pass is held, a page
    changed or the day changed. Either way no request is computing the pass, and the last pass
    ended at least the gap ago.
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
        computing = key in _FLIGHTS
    if computing:
        return False
    if last is not None and time.monotonic() - last[0] < gap_factor() * last[1]:
        return False
    if asked:
        return True
    moved = (
        held is None
        or held.generation != generation
        or held.day != dt.datetime.now(dt.UTC).date()
    )
    return moved and foreground_activity.idle_seconds(worker.vault_root) >= quiet_seconds()


def _refresh(worker: _Worker, key: tuple) -> None:
    """Run one pass at background priority; keep it only when it finished in time."""
    from .governance import principal as principal_module

    cancel = threading.Event()
    timer = threading.Timer(limit_seconds(), cancel.set)
    timer.daemon = True
    with _LOCK:
        worker.requested.discard(key)
        worker.running = key
        worker.cancel = cancel
    timer.start()
    try:
        with (
            principal_module.library_scope(),
            foreground_activity.background_scope(worker.vault_root),
            foreground_priority.cancellable(cancel),
        ):
            generation = freshness.generation(worker.vault_root, _SCOPE)
            if generation is None or not _unrestricted(worker.vault_root):
                return
            fresh = _compute(worker.vault_root, key, generation)
        if cancel.is_set():
            log.warning("review pass took longer than %.0f s; abandoned", limit_seconds())
            return
        _hold(key, fresh)
    except foreground_priority.BulkCancelled:
        log.info("review pass abandoned")
    except Exception:  # noqa: BLE001 - a failed pass keeps the held one; the next refresh runs later
        log.warning("review pass failed; the held pass stays", exc_info=True)
    finally:
        timer.cancel()
        with _LOCK:
            worker.running = None
            worker.cancel = None
