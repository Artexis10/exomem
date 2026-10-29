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

**Scoped suppression.** A trustworthy custody footprint suppresses every pending
identity, even when content hydration is incomplete. Corrupt or unreadable
custody remains unavailable because it cannot prove that footprint.

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
import stat
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import call_spans, derived_receipts, freshness
from .find_types import ParsedPage

#: Bound content hydration, while the complete custody footprint stays available.
PENDING_HYDRATION_LIMIT: Final = 512
# Targeted worker snapshots load receipt metadata, never page bodies.
_TARGETED_SNAPSHOT_LIMIT: Final = 2**63 - 1

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
#: noncanonical background review. The context catalogue is a disposable
#: sidecar that recall never reads, so a slow or failing fold must not keep a
#: page shadowed. Every OTHER required component -- including one added after
#: this module was written -- must prove completion before the overlay stops
#: shadowing, because ordinary recall reads through it.
_OMITTABLE_COMPONENTS: Final[frozenset[str]] = frozenset(
    {
        derived_receipts.DerivedComponent.GRAPH.value,
        derived_receipts.DerivedComponent.EMBEDDINGS.value,
        derived_receipts.DerivedComponent.CLAIMS.value,
        derived_receipts.DerivedComponent.WRITE_ADVISORY.value,
        derived_receipts.DerivedComponent.CONTEXT_CATALOGUE.value,
    }
)

#: An identity that could not be observed at all. Never a retirement target.
_UNREADABLE: Final = object()

_STATE_LOCK = threading.RLock()
_STATES: dict[str, _VaultState] = {}
_OBSERVED_CUSTODY: set[str] = set()
#: Bodies already proven against their receipt, by vault key and path. Every
#: publication and retirement fences `_STATES` out, and the rehydration after it
#: would otherwise read, hash and parse every pending body again.
_PROVEN: dict[str, dict[str, _ProvenBody]] = {}


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
class _ProvenBody:
    """One body whose bytes hashed to `after_hash` while the file had `signature`."""

    after_hash: str
    signature: freshness.FileSignature
    page: ParsedPage
    page_ref: str | None


@dataclass(frozen=True, slots=True)
class PendingOverlay:
    """The bounded pending projection one request may serve from.

    Complete coverage is ``ready``. Known paths with incomplete content are
    ``degraded`` and servable; unknown custody remains ``warming``.
    """

    outcome: str
    failure_code: str | None
    snapshot_generation: int
    rows: Mapping[str, PendingRow]
    paths: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", self.paths | self.rows.keys())

    @property
    def servable(self) -> bool:
        # Closed coverage outcomes: only known custody can suppress stale rows.
        return self.outcome in {"ready", "degraded"}

    @property
    def ready(self) -> bool:
        return self.outcome == "ready"

    @property
    def empty(self) -> bool:
        return not self.paths

    def covers(self, rel_path: str) -> bool:
        """Whether pending custody owns this canonical identity."""
        return rel_path in self.paths

    def shadow(self, rel_paths: Iterable[str]) -> list[str]:
        """Drop every persistent identity a pending update or tombstone shadows.

        The single suppression point for the catalogue, vector, graph, CLIP and
        keyword lanes: a path under pending custody carries no persistent-lane
        evidence, because the only generation that can be attested for it is the
        overlay's own. Applied before scoring, rank fusion, top-k caps, excerpts
        and cache insertion.
        """
        shadowed = self.paths
        return [rel_path for rel_path in rel_paths if rel_path not in shadowed]

    def shadow_set(self, rel_paths: AbstractSet[str]) -> frozenset[str]:
        """`shadow` for a whole path set: one set difference, not a Python loop."""
        return frozenset(rel_paths - self.paths)

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
    blocked_paths: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not self.paths_by_id and not self.absent_paths and not self.refs_by_path


@dataclass(frozen=True, slots=True)
class _VaultState:
    """One fenced hydration of a vault's durable pending custody."""

    snapshot_generation: int
    outcome: str
    rows: Mapping[str, PendingRow]
    paths: frozenset[str]
    batches: tuple[object, ...]
    hydrated_at: float
    #: A footprint-only fallback holds no proven bodies. The next request owes one
    #: re-hydration, which the proven-body memo keeps cheap.
    footprint_only: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _key(vault_root: Path) -> str:
    try:
        return str(Path(vault_root).resolve())
    except OSError:
        return str(Path(vault_root))


def _accept_custody_snapshot(
    key: str, snapshot: derived_receipts.PendingVisibilitySnapshot,
) -> bool:
    """Initial absence is compatible; lost observed custody remains unknown."""
    with _STATE_LOCK:
        if snapshot.snapshot_generation or snapshot.paths:
            _OBSERVED_CUSTODY.add(key)
        return bool(snapshot.snapshot_generation or snapshot.paths) or key not in _OBSERVED_CUSTODY


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
    key: str,
    rel_path: str,
    *,
    after_hash: str | None,
    canonical_generation: str,
    batch_id: str,
    stable_memory_ref: str | None,
) -> PendingRow | None:
    """Prove one changed path against its intended after state, then parse it.

    Reading and parsing happen only after the exact intended hash or tombstone
    is proven, so no unproven generation ever enters the projection. A body
    already proven at the same hash and file identity (`freshness` signature)
    is not read again. Returns None when the path cannot be proven; its custody
    still suppresses old rows.
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
    # The identity is taken before the read: an edit between the two leaves a
    # newer signature than the one remembered, so the next proof reads again.
    try:
        before = target.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode):
        return None
    signature = freshness.signature_from_stat(before)
    with _STATE_LOCK:
        known = _PROVEN.get(key, {}).get(rel_path)
    if known is not None and known.signature == signature:
        # The bytes are the ones hashed to `known.after_hash`; a receipt that
        # intends another hash (a superseded batch) cannot be proven by them.
        if known.after_hash != after_hash:
            return None
        return PendingRow(
            rel_path=rel_path,
            canonical_generation=canonical_generation,
            batch_id=batch_id,
            after_hash=after_hash,
            page=known.page,
            stable_memory_ref=stable_memory_ref or known.page_ref,
        )
    try:
        content = target.read_bytes()
    except OSError:
        return None
    if _digest(content) != after_hash:
        return None

    from . import find_corpus

    try:
        page = find_corpus.parse_page(
            target, before.st_mtime, Path(vault_root), content=content
        )
    except (OSError, UnicodeError, ValueError):
        return None
    if page is None:
        return None
    page_ref = _page_memory_ref(page)
    _remember_body(
        key, rel_path, target, signature,
        _ProvenBody(after_hash=after_hash, signature=signature, page=page, page_ref=page_ref),
    )
    return PendingRow(
        rel_path=rel_path,
        canonical_generation=canonical_generation,
        batch_id=batch_id,
        after_hash=after_hash,
        page=page,
        stable_memory_ref=stable_memory_ref or page_ref,
    )


def _remember_body(
    key: str, rel_path: str, target: Path, signature: freshness.FileSignature, body: _ProvenBody
) -> None:
    """Keep a proven body only if the file still has the identity it was read at."""
    try:
        unchanged = freshness.signature_from_stat(target.lstat()) == signature
    except OSError:
        unchanged = False
    if not unchanged:
        return
    with _STATE_LOCK:
        bodies = _PROVEN.setdefault(key, {})
        bodies.pop(rel_path, None)
        bodies[rel_path] = body
        # A bound for a process that publishes without ever hydrating (which is
        # where the retired rows are pruned): evict the oldest proven body.
        while len(bodies) > PENDING_HYDRATION_LIMIT:
            del bodies[next(iter(bodies))]


def _retain_proven(key: str, custody_paths: Iterable[str]) -> None:
    """Drop the bodies of paths that no longer have pending custody."""
    with _STATE_LOCK:
        bodies = _PROVEN.get(key)
        if bodies:
            kept = {rel: bodies[rel] for rel in custody_paths if rel in bodies}
            _PROVEN[key] = kept


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


def _project_batches(vault_root: Path, key: str, batches: Sequence[object]) -> _Projection:
    """Project every snapshot row, resolving supersession by exact proof.

    Two batches may cover one path when a later write lands before the earlier
    one converges. Only the generation whose after state the canonical bytes
    actually prove is current; the older row is superseded and cannot publish.

    A path no batch can prove stays suppressed while other proven rows remain
    servable. Only the worker and publication callbacks can retire its custody.
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
                key,
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


def recall_lanes_hold(vault_root: Path, expected: dict[str, str | None]) -> bool:
    """Whether both persistent recall lanes hold exactly these identities.

    The overlay's lane test, the part of its retirement test that asks the
    stores; retirement also waits for the batch's other components (see
    :func:`_components_allow_retirement`). The receipt store asks only this to
    hand on a path whose current bytes no receipt covers.
    """
    return _lanes_hold(Path(vault_root), expected)


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
    for batch in _settled_batches(vault_root, batches, projection):
        if derived_receipts.retire_pending_visibility(vault_root, batch).outcome == "retired":
            retired = True
    return retired


def _settled_batches(
    vault_root: Path, batches: Sequence[object], projection: _Projection
) -> list[object]:
    """The batches whose custody both recall lanes have published (no mutation)."""
    settled = []
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
        settled.append(batch)
    return settled


def readonly_visibility(vault_root: Path) -> tuple[str, str | None]:
    """Strict pending-publication readiness, without retiring anything.

    Doctor runs out of process and cannot mutate custody. Exact body and
    persistent-lane proofs can establish strict readiness; degraded recall
    does not promote it. Returns ``("ready", None)`` or a closed warming code.
    """
    root = Path(vault_root)
    try:
        snapshot = derived_receipts.snapshot_pending_visibility(
            root, limit=PENDING_HYDRATION_LIMIT
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return "warming", "pending_visibility_unprovable"
    if not _accept_custody_snapshot(_key(root), snapshot):
        return "warming", "pending_visibility_unprovable"
    if snapshot.outcome != "complete":
        return "warming", snapshot.failure_code or f"pending_visibility_{snapshot.outcome}"
    key = _key(root)
    projection = _project_batches(root, key, snapshot.batches)
    if projection.unprovable:
        cleared = {
            batch.receipt.batch_id
            for batch in _settled_batches(root, snapshot.batches, projection)
        }
        remaining = [
            batch for batch in snapshot.batches if batch.receipt.batch_id not in cleared
        ]
        if _project_batches(root, key, remaining).unprovable:
            return "warming", "pending_visibility_unprovable"
    return "ready", None


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
    key = _key(root)
    for path in receipt.paths:
        row = _prove_row(
            root,
            key,
            path.rel_path,
            after_hash=path.after_hash,
            canonical_generation=receipt.canonical_generation,
            batch_id=receipt.batch_id,
            stable_memory_ref=path.stable_memory_ref,
        )
        if row is None:
            return False
    with _STATE_LOCK:
        # Publishing advances the store's pending generation, so any cached
        # projection is already fenced out and the next request re-hydrates from
        # durable custody. The bodies proved above stay in `_PROVEN`, so that
        # rehydration reads only what changed. Whether a persistent lane holds
        # an identity is asked of that lane, never cached.
        _STATES.pop(key, None)
        _OBSERVED_CUSTODY.add(key)
    return True


# --------------------------------------------------------------------------- #
# Hydration and the request-time projection
# --------------------------------------------------------------------------- #


def overlay(vault_root: Path) -> PendingOverlay:
    """Hydrate once; lost body proof falls back to a fresh path-only snapshot.

    Readers never retire custody. Publication callbacks and the bounded service
    worker prove retirement independently of this cache or its hydration cap.
    """
    root = Path(vault_root)
    key = _key(root)
    with _STATE_LOCK:
        state = _STATES.get(key)
    if (
        state is not None
        and not state.footprint_only
        and derived_receipts.pending_visibility_snapshot_is_current(
            root, state.snapshot_generation
        )
    ):
        return PendingOverlay(
            outcome=state.outcome,
            failure_code=None if state.outcome == "ready" else "pending_visibility_incomplete",
            snapshot_generation=state.snapshot_generation,
            rows=dict(state.rows), paths=state.paths,
        )
    try:
        snapshot = derived_receipts.snapshot_pending_visibility(
            root, limit=PENDING_HYDRATION_LIMIT
        )
        if not _accept_custody_snapshot(key, snapshot) or snapshot.outcome == "unprovable":
            _forget(key)
            return _warming("pending_visibility_unprovable", snapshot.snapshot_generation)
        projection = _project_batches(root, key, snapshot.batches)
        if not derived_receipts.pending_visibility_snapshot_is_current(
            root, snapshot.snapshot_generation
        ):
            # One fresh metadata transaction replaces all body proof. Repeating
            # hydration under sustained writes charges the same bodies again.
            snapshot = derived_receipts.snapshot_pending_visibility(
                root, limit=PENDING_HYDRATION_LIMIT, footprint_only=True
            )
            if not _accept_custody_snapshot(key, snapshot) or snapshot.outcome == "unprovable":
                _forget(key)
                return _warming("pending_visibility_unprovable", snapshot.snapshot_generation)
            projection = _Projection(rows={}, unprovable=snapshot.paths)
            degraded = footprint_only = True
        else:
            degraded = snapshot.outcome == "overflow" or bool(projection.unprovable)
            footprint_only = False
    except (OSError, RuntimeError, TypeError, ValueError):
        _forget(key)
        return _warming("pending_visibility_unprovable")
    _retain_proven(key, snapshot.paths)
    # Cache parsed coverage under its custody fence, never a result list. A
    # degraded projection still disables every consumer's shared result cache.
    with _STATE_LOCK:
        _STATES[key] = _VaultState(
            snapshot_generation=snapshot.snapshot_generation,
            outcome="degraded" if degraded else "ready",
            rows=projection.rows, paths=snapshot.paths,
            batches=tuple(snapshot.batches), hydrated_at=time.monotonic(),
            footprint_only=footprint_only,
        )
    return PendingOverlay(
        outcome="degraded" if degraded else "ready",
        failure_code="pending_visibility_incomplete" if degraded else None,
        snapshot_generation=snapshot.snapshot_generation,
        rows=dict(projection.rows), paths=snapshot.paths,
    )


def _forget(key: str) -> None:
    with _STATE_LOCK:
        _STATES.pop(key, None)


def reference_projection(vault_root: Path) -> ReferenceProjection | None:
    """Pending stable-ref/path resolution, or None when nothing is pending."""
    current = overlay(Path(vault_root))
    if not current.servable:
        from .find import RetrievalIndexWarming

        call_spans.measure("pending_custody_unknown")
        raise RetrievalIndexWarming(site="pending_visibility_incomplete", status="temporarily_unavailable")
    if current.empty:
        return None
    paths_by_id: dict[str, list[str]] = {}
    refs_by_path: dict[str, str | None] = dict.fromkeys(current.paths)
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
        blocked_paths=current.paths - current.rows.keys(),
    )


# --------------------------------------------------------------------------- #
# Retirement
# --------------------------------------------------------------------------- #


def note_persistent_publication(
    vault_root: Path, lane: str, rel_paths: Iterable[str]
) -> None:
    """A persistent recall lane published; re-derive that custody and retire it.

    Nothing is remembered from this call. Whether a lane holds an identity is a
    question for that lane's durable state, asked at retirement time against the
    exact generation the receipt recorded -- so a pass that indexed one
    generation before the file moved cannot retire custody the store does not
    hold, and a restart between two lane publications does not strand a row.

    The proof reads only the named paths' batches. It never needs a hydrated
    reader cache, so overflow and restart cannot prevent retirement.
    """
    if lane not in set(_RETIREMENT_LANES.values()):
        raise ValueError(f"unknown persistent recall lane: {lane!r}")
    named = {rel for rel in rel_paths if isinstance(rel, str) and rel}
    if not named:
        return
    root = Path(vault_root)
    snapshot = derived_receipts.snapshot_pending_visibility(
        root, limit=_TARGETED_SNAPSHOT_LIMIT, rel_paths=named,
    )
    if snapshot.outcome != "complete":
        return
    if _retire_settled(root, snapshot.batches, _Projection({}, frozenset())):
        _forget(_key(root))


def note_components_completed(vault_root: Path, batch_ids: Iterable[str]) -> None:
    """Derived components completed; retire whatever custody they settled.

    Retirement needs both recall lanes to hold the generation *and* every
    non-omittable component to be completed. The lanes publish inside the
    receipt-owned fan-out, before the drain records the components that fan-out
    proved, so the attempt :func:`note_persistent_publication` makes at that
    moment cannot retire. Completing a component does not move the pending
    generation fence either, so without this second attempt a cached projection
    kept shadowing the page's vector and graph evidence until some unrelated
    pending-row mutation happened to invalidate it.
    """
    named = {batch_id for batch_id in batch_ids if isinstance(batch_id, str) and batch_id}
    if not named:
        return
    root = Path(vault_root)
    snapshot = derived_receipts.snapshot_pending_visibility(
        root, limit=_TARGETED_SNAPSHOT_LIMIT, batch_ids=named,
    )
    if snapshot.outcome != "complete":
        return
    if _retire_settled(root, snapshot.batches, _Projection({}, frozenset())):
        _forget(_key(root))


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
        "pending_batches": batches if current.ready else None,
        "pending_paths": len(current.paths) if current.servable else None,
        "tombstones": sum(1 for row in current.rows.values() if row.page is None) if current.ready else None,
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
            _PROVEN.clear()
            _OBSERVED_CUSTODY.clear()
        else:
            key = _key(Path(vault_root))
            _STATES.pop(key, None)
            _PROVEN.pop(key, None)
            _OBSERVED_CUSTODY.discard(key)
