"""Bounded exact pending-recall overlay over unfinished derived receipts.

A governed write is canonically durable long before its derived components
converge.  Between those two moments the lexical catalogue, the reference
sidecar and the vector/graph sidecars still describe the *previous* generation,
so recall would either miss the committed page or return a stale row for it.

This module closes that window without a second index.  Lane 1's receipt store
already records, for every unfinished batch, the exact safe relative paths and
their intended after hash or tombstone.  The overlay is an O(changed-paths)
projection of exactly those rows:

* :func:`publish` is the callee side of
  ``derived_receipts.publish_pending_visibility``.  It reads and parses only the
  batch's own changed paths, and only after proving each one equals its intended
  after state.  It never walks, stats or rebuilds an unrelated vault path.
* :func:`overlay` returns the bounded projection a request may serve from.  It
  hydrates from durable custody through the frozen snapshot/fence seams, so a
  fresh process converges before recall is declared ready and never depends on
  the publisher being replayed.
* :func:`note_persistent_publication` is the retirement seam.  An overlay row is
  removed only after every persistent lane the batch requires has published the
  exact after generation, so there is no removal-before-publication gap.

Three properties are deliberate and load-bearing:

**Fail closed.**  ``overflow``, an unprovable snapshot, a corrupt or unsafe row,
a hash/generation mismatch, or a fence that moved under a hydration attempt all
produce a non-ready coverage.  Recall then returns its existing typed
warming/temporarily-unavailable outcome; it never serves the last published
catalogue as if no pending mutation existed.

**Custody is not release.**  A row enters the overlay because a receipt covers
it, not because anyone decided it may be disclosed.  Every consumer applies the
*current* request's recall policy, access tier and filters before a pending page
becomes a candidate, so the writer's earlier disclosure decision is never
inherited.

**Content stays in memory.**  Parsed pending pages live only in this process.
Operational status, logs and telemetry expose bounded counts and closed codes;
:func:`status` is the only status surface and carries no path, title, body,
stable reference or query term.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import derived_receipts
from .find_types import ParsedPage

#: How many outstanding pending-visibility rows one process may hydrate.
#:
#: The overlay is a delta over unfinished receipts, never a corpus copy, so the
#: bound is sized for a burst of governed writes rather than for a vault. Past
#: it the snapshot reports ``overflow`` and recall fails closed to warming until
#: the component drains catch up. Deliberately a module constant: reading it
#: from the environment would let a deployment widen an unbounded in-memory
#: projection, and the fail-closed outcome is the safe direction anyway.
PENDING_HYDRATION_LIMIT: Final = 512

#: Persistent lanes ordinary recall reads through, keyed by the derived
#: component that publishes each one. A batch retires only once every lane it
#: requires has proven the exact after generation.
_RETIREMENT_LANES: Final[dict[str, str]] = {
    derived_receipts.DerivedComponent.LEXSTORE.value: "lexstore",
    derived_receipts.DerivedComponent.MEMORY_REFS.value: "memory_refs",
}

#: The only components Design Decisions 4 and 6 allow to omit the pending
#: generation: vector and graph lanes contribute proven-current generations
#: only and disclose their pending coverage instead, and advisory work is
#: noncanonical background review. Every OTHER required component -- including
#: one added after this module was written -- must prove completion before the
#: overlay stops shadowing, because ordinary recall reads through it.
_OMITTABLE_COMPONENTS: Final[frozenset[str]] = frozenset(
    {
        derived_receipts.DerivedComponent.GRAPH.value,
        derived_receipts.DerivedComponent.EMBEDDINGS.value,
        derived_receipts.DerivedComponent.CLAIMS.value,
        derived_receipts.DerivedComponent.WRITE_ADVISORY.value,
    }
)

#: Bounded re-attempts when the store's pending generation moves under a
#: hydration pass, or when a pass retires custody and must re-snapshot. A
#: concurrent publisher invalidates the fence rather than corrupting it, so a
#: small fixed budget converges or fails closed.
_HYDRATION_ATTEMPTS: Final = 4

#: An identity that could not be observed at all. Never a retirement target.
_UNREADABLE: Final = object()

_STATE_LOCK = threading.RLock()
_STATES: dict[str, _VaultState] = {}


@dataclass(frozen=True, slots=True)
class PendingRow:
    """One proven pending identity: the exact after state, or a tombstone."""

    rel_path: str
    canonical_generation: str
    batch_id: str
    after_hash: str | None
    page: ParsedPage | None
    stable_memory_ref: str | None

    @property
    def updated(self) -> str:
        return "" if self.page is None else (self.page.updated or "")


@dataclass(frozen=True, slots=True)
class PendingOverlay:
    """The bounded pending projection one request may serve from.

    ``outcome`` is ``ready`` when complete coverage was proven and ``warming``
    otherwise; a non-ready overlay carries no rows, so nothing can be served
    from a partial hydration.
    """

    outcome: str
    failure_code: str | None
    snapshot_generation: int
    rows: Mapping[str, PendingRow]

    @property
    def ready(self) -> bool:
        return self.outcome == "ready"

    @property
    def empty(self) -> bool:
        return not self.rows

    def covers(self, rel_path: str) -> bool:
        """Whether pending custody owns this canonical identity."""
        return rel_path in self.rows

    def shadow(self, rel_paths: Iterable[str]) -> list[str]:
        """Drop every persistent identity a pending update or tombstone shadows.

        The single suppression point for the catalogue, vector, graph, CLIP and
        keyword lanes: a path under pending custody carries no persistent-lane
        evidence, because the only generation that can be attested for it is the
        overlay's own. Applied before scoring, rank fusion, top-k caps, excerpts
        and cache insertion.
        """
        shadowed = self.rows
        return [rel_path for rel_path in rel_paths if rel_path not in shadowed]

    def page(self, rel_path: str) -> ParsedPage | None:
        """The proven current page for a covered identity; None for a tombstone."""
        row = self.rows.get(rel_path)
        return None if row is None else row.page

    def current_pages(self) -> tuple[PendingRow, ...]:
        """Non-tombstoned rows, most recently updated first."""
        rows = [row for row in self.rows.values() if row.page is not None]
        rows.sort(key=lambda row: (row.updated or "0000-00-00", row.rel_path), reverse=True)
        return tuple(rows)

    def current_paths(self) -> frozenset[str]:
        return frozenset(row.rel_path for row in self.rows.values() if row.page is not None)

    def tombstoned_paths(self) -> frozenset[str]:
        return frozenset(row.rel_path for row in self.rows.values() if row.page is None)


_EMPTY_OVERLAY: Final = PendingOverlay(
    outcome="ready", failure_code=None, snapshot_generation=0, rows={}
)


@dataclass(frozen=True, slots=True)
class ReferenceProjection:
    """Pending stable-reference state consulted before the persistent sidecar."""

    paths_by_id: Mapping[str, tuple[str, ...]]
    absent_paths: frozenset[str]
    refs_by_path: Mapping[str, str | None]

    @property
    def empty(self) -> bool:
        return not self.paths_by_id and not self.absent_paths and not self.refs_by_path


@dataclass(frozen=True, slots=True)
class _VaultState:
    """One fenced hydration of a vault's durable pending custody."""

    snapshot_generation: int
    rows: Mapping[str, PendingRow]
    batches: tuple[object, ...]
    hydrated_at: float


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _key(vault_root: Path) -> str:
    try:
        return str(Path(vault_root).resolve())
    except OSError:
        return str(Path(vault_root))


def vault_rel_path(vault_root: Path, path: Path | str) -> str | None:
    """The one vault-relative identity spelling every pending consumer uses.

    ``find`` walks candidates, ``lexstore`` and ``memory_refs`` report published
    identities, and this module keys custody by the same string. They used to
    normalize differently -- one through ``absolute()``, the others through
    ``resolve()`` -- which agreed on ordinary vaults and diverged through a
    symlinked root. One helper, used by all of them, removes the class.

    The cheap spelling is tried first because the walk calls this per candidate;
    ``resolve()`` is the fallback that also handles a link on either side.
    """
    root = Path(vault_root)
    candidate = Path(path)
    try:
        return candidate.absolute().relative_to(root.absolute()).as_posix()
    except (OSError, ValueError):
        pass
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _prove_row(
    vault_root: Path,
    rel_path: str,
    *,
    after_hash: str | None,
    canonical_generation: str,
    batch_id: str,
    stable_memory_ref: str | None,
) -> PendingRow | None:
    """Prove one changed path against its intended after state, then parse it.

    Reading and parsing happen only after the exact intended hash or tombstone
    is proven, so no unproven generation ever enters the projection. Returns
    None when the path cannot be proven, which fails the whole hydration closed.
    """
    target = Path(vault_root).joinpath(*rel_path.split("/"))
    if after_hash is None:
        try:
            if target.exists():
                return None
        except OSError:
            return None
        return PendingRow(
            rel_path=rel_path,
            canonical_generation=canonical_generation,
            batch_id=batch_id,
            after_hash=None,
            page=None,
            stable_memory_ref=stable_memory_ref,
        )
    try:
        if target.is_symlink() or not target.is_file():
            return None
        content = target.read_bytes()
        mtime = target.stat().st_mtime
    except OSError:
        return None
    if _digest(content) != after_hash:
        return None

    from . import find_corpus

    try:
        page = find_corpus.parse_page(target, mtime, Path(vault_root), content=content)
    except (OSError, UnicodeError, ValueError):
        return None
    if page is None:
        return None
    return PendingRow(
        rel_path=rel_path,
        canonical_generation=canonical_generation,
        batch_id=batch_id,
        after_hash=after_hash,
        page=page,
        stable_memory_ref=stable_memory_ref or _page_memory_ref(page),
    )


def _page_memory_ref(page: ParsedPage) -> str | None:
    """The page's own stable reference, read from bytes already proven exact."""
    from . import memory_refs

    identity = memory_refs.normalize_id(page.frontmatter.get(memory_refs.ID_FIELD))
    return None if identity is None else memory_refs.memory_ref(identity)


@dataclass(frozen=True, slots=True)
class _Projection:
    """One hydration pass: what proved, and what could not."""

    rows: dict[str, PendingRow]
    unprovable: frozenset[str]


def _project_batches(vault_root: Path, batches: Sequence[object]) -> _Projection:
    """Project every snapshot row, resolving supersession by exact proof.

    Two batches may cover one path when a later write lands before the earlier
    one converges. Only the generation whose after state the canonical bytes
    actually prove is current; the older row is superseded and cannot publish.

    A path no batch can prove is reported rather than raised. Overlay-wide
    fail-closed is still what a caller gets -- that is the spec sentence -- but
    the retirement pass first gets a chance to clear an out-of-band supersession
    the persistent lanes have already published, so one hand edit cannot deny
    every unrelated query forever.
    """
    proven: dict[str, PendingRow] = {}
    wanted: set[str] = set()
    for batch in batches:
        receipt = batch.receipt
        by_path = {path.rel_path: path for path in receipt.paths}
        for row in batch.rows:
            wanted.add(row.rel_path)
            if row.rel_path in proven:
                continue
            path = by_path.get(row.rel_path)
            if path is None or row.canonical_generation != receipt.canonical_generation:
                continue
            candidate = _prove_row(
                vault_root,
                row.rel_path,
                after_hash=path.after_hash,
                canonical_generation=row.canonical_generation,
                batch_id=receipt.batch_id,
                stable_memory_ref=path.stable_memory_ref,
            )
            if candidate is not None:
                proven[row.rel_path] = candidate
    return _Projection(rows=proven, unprovable=frozenset(wanted - set(proven)))


def _canonical_identity(vault_root: Path, rel_path: str) -> str | None | object:
    """The identity the persistent lanes must hold for this path right now.

    ``None`` means proven absence. The ``_UNREADABLE`` sentinel means the path
    could not be observed at all, which never authorizes retirement.
    """
    target = Path(vault_root).joinpath(*rel_path.split("/"))
    try:
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            return _UNREADABLE
        return _digest(target.read_bytes())
    except OSError:
        return _UNREADABLE


def _required_components(receipt: object) -> tuple[object, ...]:
    return tuple(
        status for status in receipt.components if status.state != "not_required"
    )


def _components_allow_retirement(vault_root: Path, batch: object) -> bool:
    """Whether every component ordinary recall reads through has converged.

    `lexstore` and `memory_refs` are proven by what those stores actually hold
    (see :func:`_lanes_hold`). Graph, embeddings, claims and write-advisory may
    omit the pending generation by design. Everything else -- resolver, semantic
    purge, freshness, and any component added after this module was written --
    must be `completed` at the batch's own component revision, read through the
    frozen public `component_status` seam. An unmapped component therefore
    blocks retirement rather than silently contributing nothing.
    """
    for status in _required_components(batch.receipt):
        component = status.component.value
        if component in _RETIREMENT_LANES or component in _OMITTABLE_COMPONENTS:
            continue
        try:
            current = derived_receipts.component_status(
                vault_root, batch.receipt, status.component
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return False
        if current.state != "completed" or current.revision != status.revision:
            return False
    return True


def _lanes_hold(vault_root: Path, expected: dict[str, str | None]) -> bool:
    """Whether both persistent recall lanes hold exactly these identities.

    The lanes answer from what they durably store, not from a report that a pass
    ran: a pass that indexed one generation and a file that moved before the
    report would otherwise retire custody the catalogue does not actually hold.
    Asking the stores also means the proof survives a restart, so a process that
    dies between the two publications does not strand the row.
    """
    if not expected:
        return True
    from . import lexstore, memory_refs

    try:
        # The identity sidecar answers the whole batch in one query, so it is
        # asked first and short-circuits the per-path catalogue comparison.
        identities = memory_refs.holds_content_identities(vault_root, expected)
        if not all(identities.get(rel, False) for rel in expected):
            return False
        catalogue = lexstore.holds_content_identities(vault_root, expected)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return all(catalogue.get(rel, False) for rel in expected)


def _retire_settled(
    vault_root: Path, batches: Sequence[object], projection: _Projection
) -> bool:
    """Retire every batch both recall lanes have published. Returns whether any did.

    The identity a lane must hold is the row's proven after state, or -- when an
    out-of-band write superseded that state -- the canonical bytes now on disk.
    The second case is what lets a hand edit heal instead of denying recall
    forever, and it is still exact: the lanes must hold the current generation,
    and the clearing itself goes through the frozen exact-batch CAS.
    """
    retired = False
    for batch in batches:
        expected: dict[str, str | None] = {}
        blocked = False
        for row in batch.rows:
            proven = projection.rows.get(row.rel_path)
            if proven is not None:
                expected[row.rel_path] = proven.after_hash
                continue
            identity = _canonical_identity(vault_root, row.rel_path)
            if identity is _UNREADABLE:
                blocked = True
                break
            expected[row.rel_path] = identity
        if blocked:
            continue
        if not _components_allow_retirement(vault_root, batch):
            continue
        if not _lanes_hold(vault_root, expected):
            continue
        if derived_receipts.retire_pending_visibility(vault_root, batch).outcome == "retired":
            retired = True
    return retired


def _warming(failure_code: str, generation: int = 0) -> PendingOverlay:
    return PendingOverlay(
        outcome="warming",
        failure_code=failure_code,
        snapshot_generation=generation,
        rows={},
    )


# --------------------------------------------------------------------------- #
# Publication (the callee side of publish_pending_visibility)
# --------------------------------------------------------------------------- #


def publish(vault_root: Path, receipt: object) -> bool:
    """Publish one batch's pending projection from its own changed paths only.

    Invoked by ``derived_receipts.publish_pending_visibility`` while the batch
    holds exact committed proof. Returns whether every path in the batch was
    proven and projected; a False return refuses the durable publication rather
    than marking custody live over an unprovable delta.
    """
    root = Path(vault_root)
    rows: dict[str, PendingRow] = {}
    for path in receipt.paths:
        row = _prove_row(
            root,
            path.rel_path,
            after_hash=path.after_hash,
            canonical_generation=receipt.canonical_generation,
            batch_id=receipt.batch_id,
            stable_memory_ref=path.stable_memory_ref,
        )
        if row is None:
            return False
        rows[path.rel_path] = row
    with _STATE_LOCK:
        # Publishing advances the store's pending generation, so any cached
        # projection is already fenced out and the next request re-hydrates from
        # durable custody. Nothing else is remembered here: whether a persistent
        # lane holds an identity is asked of that lane, never cached.
        _STATES.pop(_key(root), None)
    return True


# --------------------------------------------------------------------------- #
# Hydration and the request-time projection
# --------------------------------------------------------------------------- #


def overlay(vault_root: Path) -> PendingOverlay:
    """Return the bounded pending projection, hydrating from custody as needed.

    A cached projection is reused only while the store's opaque pending
    generation still fences it; any concurrent pending-row mutation forces
    another bounded hydration attempt. Every incomplete outcome is typed,
    carries a closed failure code, and carries no rows.

    Each attempt first retires whatever both recall lanes have published. That
    is what converges a restart which lost its in-process lane bookkeeping, and
    what heals a row an out-of-band write made unprovable once the lanes publish
    the current canonical bytes.
    """
    root = Path(vault_root)
    key = _key(root)
    with _STATE_LOCK:
        state = _STATES.get(key)
    if state is not None and derived_receipts.pending_visibility_snapshot_is_current(
        root, state.snapshot_generation
    ):
        return PendingOverlay(
            outcome="ready",
            failure_code=None,
            snapshot_generation=state.snapshot_generation,
            rows=dict(state.rows),
        )

    for _attempt in range(_HYDRATION_ATTEMPTS):
        try:
            snapshot = derived_receipts.snapshot_pending_visibility(
                root, limit=PENDING_HYDRATION_LIMIT
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            _forget(key)
            return _warming("pending_visibility_unprovable")
        if snapshot.outcome != "complete":
            _forget(key)
            return _warming(
                snapshot.failure_code or f"pending_visibility_{snapshot.outcome}",
                snapshot.snapshot_generation,
            )
        projection = _project_batches(root, snapshot.batches)
        if _retire_settled(root, snapshot.batches, projection):
            # Custody converged during this pass; re-snapshot so the projection
            # and its fence describe the same state.
            _forget(key)
            continue
        if projection.unprovable:
            _forget(key)
            return _warming(
                "pending_visibility_unprovable", snapshot.snapshot_generation
            )
        if not derived_receipts.pending_visibility_snapshot_is_current(
            root, snapshot.snapshot_generation
        ):
            continue
        with _STATE_LOCK:
            _STATES[key] = _VaultState(
                snapshot_generation=snapshot.snapshot_generation,
                rows=projection.rows,
                batches=tuple(snapshot.batches),
                hydrated_at=time.monotonic(),
            )
        return PendingOverlay(
            outcome="ready",
            failure_code=None,
            snapshot_generation=snapshot.snapshot_generation,
            rows=dict(projection.rows),
        )
    _forget(key)
    return _warming("pending_visibility_unprovable")


def _forget(key: str) -> None:
    with _STATE_LOCK:
        _STATES.pop(key, None)


def reference_projection(vault_root: Path) -> ReferenceProjection | None:
    """Pending stable-ref/path resolution, or None when nothing is pending."""
    current = overlay(Path(vault_root))
    if not current.ready or current.empty:
        return None
    paths_by_id: dict[str, list[str]] = {}
    refs_by_path: dict[str, str | None] = {}
    absent: set[str] = set()
    for row in current.rows.values():
        refs_by_path[row.rel_path] = None if row.page is None else row.stable_memory_ref
        if row.page is None:
            absent.add(row.rel_path)
            continue
        if row.stable_memory_ref:
            paths_by_id.setdefault(row.stable_memory_ref, []).append(row.rel_path)
    return ReferenceProjection(
        paths_by_id={ref: tuple(sorted(paths)) for ref, paths in paths_by_id.items()},
        absent_paths=frozenset(absent),
        refs_by_path=refs_by_path,
    )


# --------------------------------------------------------------------------- #
# Retirement
# --------------------------------------------------------------------------- #


def note_persistent_publication(
    vault_root: Path, lane: str, rel_paths: Iterable[str]
) -> None:
    """A persistent recall lane published; re-derive custody and retire it.

    Nothing is remembered from this call. Whether a lane holds an identity is a
    question for that lane's durable state, asked at retirement time against the
    exact generation the receipt recorded -- so a pass that indexed one
    generation before the file moved cannot retire custody the store does not
    hold, and a restart between two lane publications does not strand a row.
    """
    if lane not in set(_RETIREMENT_LANES.values()):
        raise ValueError(f"unknown persistent recall lane: {lane!r}")
    if not any(isinstance(rel, str) and rel for rel in rel_paths):
        return
    _forget(_key(Path(vault_root)))
    overlay(Path(vault_root))


# --------------------------------------------------------------------------- #
# Operational status (content-free by construction)
# --------------------------------------------------------------------------- #


def status(vault_root: Path) -> dict[str, object]:
    """Bounded, content-free pending-visibility status.

    Only closed state, a closed failure code and content-free counts and ages
    escape: no Markdown, title, excerpt, stable reference, query term, path or
    arbitrary metadata. The generation is the store's own opaque fence value.
    """
    current = overlay(Path(vault_root))
    key = _key(Path(vault_root))
    with _STATE_LOCK:
        state = _STATES.get(key)
        age = None if state is None else round(max(0.0, time.monotonic() - state.hydrated_at), 3)
        batches = 0 if state is None else len(state.batches)
    return {
        "state": current.outcome,
        "failure_code": current.failure_code,
        "snapshot_generation": current.snapshot_generation,
        "pending_batches": batches,
        "pending_paths": len(current.rows),
        "tombstones": sum(1 for row in current.rows.values() if row.page is None),
        "hydration_limit": PENDING_HYDRATION_LIMIT,
        "hydration_age_seconds": age,
    }


def reset(vault_root: Path | None = None) -> None:
    """Drop this process's in-memory projection; durable custody is untouched.

    A restart is exactly this state: no process global survives, and the next
    request hydrates the whole bounded projection from Lane 1's receipts before
    managed recall may be declared ready.
    """
    with _STATE_LOCK:
        if vault_root is None:
            _STATES.clear()
        else:
            _STATES.pop(_key(Path(vault_root)), None)
