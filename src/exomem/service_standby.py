"""Standby warm-up and promotion for the managed worker handoff (D7-D9).

A standby worker binds its own private socket and warms everything a cutover
needs — the lexical catalog, the embedding model when preload is allowed, and a
read-only proof of the current graph snapshot — while the previous worker keeps
serving.  Until the supervisor promotes it, the standby owns nothing: it takes
no writer lease, publishes no index or graph state, schedules no drain, media or
watcher work, and starts no descendants.  Where the live lexical catalogue is an
earlier release's, the standby builds its own into a rebuild temp it holds, and
only promotion publishes it.

Promotion is the only place ownership changes hands.  It runs after the previous
worker and its descendants have provably exited, re-validates the checkpoint the
standby proved, then acquires the lease and releases the ordinary activation
sequence.  A failed re-proof is recorded and promotion proceeds: the coalesced
rebuild path owns that repair.

Pure process state.  Nothing here reasons over notes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import warmup

log = logging.getLogger(__name__)

#: Set by the supervisor on the standby child only.
STANDBY_ENV = "EXOMEM_MANAGED_STANDBY"

#: Cutover components in the order a standby warms them.  ``embeddings`` is
#: reported only when this process's mode and overrides allow a model preload.
#: ``semantic_corpus`` joins them because the write admission gate waits on it
#: (task 1.14) and it is a read-only, process-local build -- so a standby that
#: has not built it is a standby whose promotion cannot admit a write, however
#: short the cutover was.
CUTOVER_COMPONENTS = ("lexical", "embeddings", "graph_snapshot", "semantic_corpus")

#: Readiness components this standby completed AND promotion either re-verified
#: or cannot invalidate. Written at promotion, read by the promoted worker's own
#: `warm_all`, and deliberately still readable after `promote()` returns: it is
#: the whole point.
_carried: frozenset[str] = frozenset()

_lock = threading.Lock()
_standby = False
_promoted = False
_proved_token: str | None = None
_adoption: Any = None
_activation: Any = None
_corpus_built = False
#: Whether the corpus build RAN, however it ended. See `cutover_components`.
_corpus_attempted = False
#: This release's lexical catalogue, built beside a live one an earlier release
#: published, held until promotion adopts it or a stop discards it.
_detached_catalog: Any = None
#: Whether that catalogue was built; `lexical` is cutover-ready once it was.
_detached_built = False
#: Set once an unpromoted standby is being stopped; a build that finishes
#: afterwards discards what it built instead of holding it.
_discarded = False
#: The same stop as an event the detached build polls from its first walked
#: file, so a discard that lands while the build is starting still stops it.
_discard_requested = threading.Event()
#: Set when promotion starts, so a re-proof still running cannot replace the
#: adoption promotion is acting on. Cleared again if promotion raises.
_promoting = False
#: The proof this standby holds: the durable checkpoint generation and the
#: published snapshot's token sampled before it ran, and the whole-vault marker
#: standing then. Promotion may retire that marker only while the durable
#: generation is still the one the proof sampled.
_proof_signal: Any = None
_proof_marker: tuple[int, int] | None = None
#: The latest attempt, held or not: what it sampled before it ran (the signal and
#: the cheap stat), and when it ended. The waiting standby re-proves when these
#: move, at most once per interval after the last attempt ended.
_attempt_signal: Any = None
_attempt_stat: Any = None
_attempt_ended_at: float | None = None

#: How long after a proof ENDS the standby may run it again. The proof is
#: O(vault) hashing -- about 45 s on a 5k-page vault -- so it runs only when the
#: graph moved, and never back to back.
REPROVE_INTERVAL_SECONDS = 30.0
#: How often the standby looks at the graph for movement: a stat of the durable
#: checkpoint and of the sidecar, and a sidecar read only when one moved.
REPROVE_POLL_SECONDS = 1.0
#: The re-proof clock; a seam for tests.
_clock = time.monotonic


def standby_requested() -> bool:
    """Whether this process was spawned as a standby candidate."""
    return bool(os.environ.get(STANDBY_ENV, "").strip())


def enter_standby() -> None:
    """Declare this process a standby; it owns nothing until promotion."""
    global _standby
    with _lock:
        _standby = True


def in_standby() -> bool:
    """Whether this process is warming as an unpromoted standby."""
    with _lock:
        return _standby


def promoted() -> bool:
    """Whether this process has taken ownership through promotion."""
    with _lock:
        return _promoted


def register_activation(activation: Any) -> None:
    """Record the deferred activation this process releases on promotion."""
    global _activation
    with _lock:
        _activation = activation


def graph_cleanup_attempted() -> bool:
    """Whether this process's managed lifecycle consumed its graph exit wait."""
    with _lock:
        return bool(getattr(_activation, "graph_cleanup_attempted", False))


def _preload_allowed() -> bool:
    try:
        return bool(warmup.model_preload_allowed())
    except Exception:  # noqa: BLE001 - readiness must stay structured
        return False


def cutover_components() -> dict[str, str]:
    """Report each cutover component as ``ready`` or ``waiting``."""
    from . import readiness

    with _lock:
        snapshot_ready = _proved_token is not None
        corpus_settled = _corpus_attempted
        catalogue_built = _detached_built
    components: dict[str, str] = {}
    for component in CUTOVER_COMPONENTS:
        if component == "graph_snapshot":
            components[component] = "ready" if snapshot_ready else "waiting"
        elif component == "semantic_corpus":
            # Settled, not built -- the same distinction `warm_all` draws when
            # it marks `graph_handoff` on every exit. A build that RAN and
            # failed is as good as this standby will get, and the promoted
            # worker pays the same failing build whether it cut over or cold
            # started; holding every release for a defect the standby cannot
            # fix would cost more than it prevents. A build that never ran
            # still leaves the component waiting, so a stalled standby is
            # still discarded when its budget expires.
            #
            # Read from this module rather than `readiness`, which
            # `finish_warm` does not clear but `begin_warm` would: the
            # supervisor polls this after the standby warm has finished.
            components[component] = "ready" if corpus_settled else "waiting"
        elif component == "embeddings":
            if _preload_allowed():
                components[component] = "ready" if readiness.is_ready("embeddings") else "waiting"
        elif component == "lexical":
            # A catalogue built detached is what promotion adopts, so it is as
            # ready as a live one proved current. Its caches are the promoted
            # worker's to warm (`lexical` is not carried in that case).
            ready = catalogue_built or readiness.is_ready(component)
            components[component] = "ready" if ready else "waiting"
        else:
            components[component] = "ready" if readiness.is_ready(component) else "waiting"
    return components


def cutover_ready() -> bool:
    """Whether this process could be promoted without a cold start."""
    return all(state == "ready" for state in cutover_components().values())


def waiting_component() -> str | None:
    """Name the first cutover component still warming, for the handoff record."""
    for component, state in cutover_components().items():
        if state != "ready":
            return component
    return None


def readiness_payload() -> dict[str, Any]:
    """The ``cutover`` block published beside the serving readiness status."""
    components = cutover_components()
    return {
        "components": components,
        "cutover_ready": all(state == "ready" for state in components.values()),
        "standby": in_standby(),
        # An adoption that succeeded but owes a bounded drain is not the same
        # event as a clean one, and the operator needs to see which happened.
        "adoption": adoption_record(),
        # What the promoted worker's own warm skipped because this process had
        # already done it. Empty everywhere except after a promotion, and the
        # first thing to look at if a cutover is fast but the writes after it
        # are not.
        "carried_from_standby": sorted(carried_warm_components()),
    }


def snapshot_token(vault_root: Path) -> str | None:
    """Name the checkpoint of the inherited snapshot, over a maintenance read.

    Deliberately ``require_current_projection=False``: an adoption that took on
    a bounded residue leaves the availability marker withdrawn until the repair
    drains, so a public read refuses exactly the snapshot this has to identify.
    Nothing is written, opened for write, or published.
    """
    return _read_snapshot_token(vault_root, quiet=False)


def _read_snapshot_token(vault_root: Path, *, quiet: bool) -> str | None:
    """`snapshot_token`'s read; `quiet` for the re-proof poll, which runs every second."""
    from . import epistemic_graph

    report = log.debug if quiet else log.warning
    try:
        index = epistemic_graph.EpistemicGraphIndex(Path(vault_root))
        conn = index._open_read_snapshot(require_current_projection=False)
    except Exception:  # noqa: BLE001 - an unprovable snapshot is a waiting component
        report("standby graph snapshot read failed", exc_info=True)
        return None
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT key, value FROM graph_meta WHERE key IN (?, ?)",
            (
                epistemic_graph._RECALL_CHECKPOINT_KEY,
                epistemic_graph._GRAPH_SYNC_CHECKPOINT_KEY,
            ),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a sidecar we cannot read is not proven
        report("standby graph checkpoint read failed", exc_info=True)
        return None
    finally:
        conn.close()
    values = {str(key): value for key, value in rows}
    return json.dumps(
        [
            values.get(epistemic_graph._RECALL_CHECKPOINT_KEY),
            values.get(epistemic_graph._GRAPH_SYNC_CHECKPOINT_KEY),
        ],
        sort_keys=True,
    )


def prove_graph_snapshot(vault_root: Path) -> bool:
    """Prove and adopt the inherited snapshot, retaining what it established.

    Adoption is what makes the promoted worker's first governed write
    incremental: it proves the sidecar against disk over a maintenance read and
    makes that checkpoint this process's delta origin. A bounded residue -- the
    deferred writes the outgoing worker left behind -- is adopted too, enqueued
    as incremental repair with the availability marker left withdrawn, and
    reported so an operator sees an adoption that still owes a drain.

    Must run after the recall registry is seeded and after the resolver is
    primed: `adopt_recall_origin` refuses a cold scope, and the bounded repair
    needs the resolver at this exact checkpoint.

    The residue is recorded, not applied: enqueueing the repair is scheduling a
    drain and withdrawing the availability marker is publishing graph state, and
    the worker still serving owns both until this one is promoted. Making the
    checkpoint the delta origin is process-local, so the adoption still does
    everything that removes the promoted worker's whole-vault pass.
    """
    global _proved_token, _adoption, _proof_signal, _proof_marker
    global _attempt_signal, _attempt_stat, _attempt_ended_at
    from . import epistemic_graph

    vault_root = Path(vault_root)
    # Sampled before the proof, so movement during it still earns a re-proof,
    # a snapshot published during it is never named as the one proved, and
    # whole-vault debt raised during it is never retired on its account.
    stat = _graph_stat(vault_root)
    signal = (_durable_generation(vault_root), snapshot_token(vault_root))
    marker = _observe_full_marker(vault_root)
    with _lock:
        if _promoting or _promoted or _discarded:
            return False
        _attempt_signal = signal
        _attempt_stat = stat
    try:
        adoption = epistemic_graph.EpistemicGraphIndex(
            vault_root
        ).adopt_published_snapshot(apply_residue=False)
    except Exception:  # noqa: BLE001 - an unadopted snapshot is a waiting component
        log.warning("standby graph snapshot adoption failed", exc_info=True)
        adoption = epistemic_graph.SnapshotAdoption(False, reason="adoption_raised")
    token = snapshot_token(vault_root) if adoption.adopted else None
    if adoption.adopted and token != signal[1]:
        # The proof read the snapshot that was published when it started; the
        # serving worker has published another since. Naming that one as
        # proved would carry a snapshot nobody proved across promotion, so this
        # attempt proves nothing and the moved token earns a re-proof.
        adoption = epistemic_graph.SnapshotAdoption(
            False, residue=adoption.residue, reason="snapshot_published_during_proof"
        )
        token = None
    with _lock:
        _attempt_ended_at = _clock()
        if _promoting or _promoted or _discarded:
            # Promotion is already acting on the adoption it read; a late
            # re-proof must not replace it underneath.
            return False
        if token is None and _proved_token is not None:
            # A newer attempt that declined does not unprove the one held: the
            # held proof still names its own snapshot, and promotion compares
            # that snapshot before relying on it.
            log.info(
                "standby snapshot re-proof declined reason=%s; keeping the held proof",
                adoption.reason,
            )
            return False
        _adoption = adoption
        _proved_token = token
        _proof_signal = signal if token is not None else None
        _proof_marker = marker if token is not None else None
    log.info(
        "standby snapshot adoption adopted=%s residue=%d reason=%s",
        adoption.adopted,
        len(adoption.residue),
        adoption.reason,
    )
    return bool(adoption.adopted and token is not None)


def _durable_generation(vault_root: Path) -> Any:
    """The durable graph checkpoint generation, None when absent. Never raises."""
    from . import graph_sync

    try:
        checkpoint = graph_sync.read_checkpoint(Path(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable checkpoint is itself a signal
        return "unreadable"
    return None if checkpoint is None else int(checkpoint.generation)


def _graph_stat(vault_root: Path) -> tuple[Any, ...]:
    """Stat signatures of the durable checkpoint and the sidecar. Opens nothing."""
    from . import epistemic_graph, graph_sync

    def signature(path: Path) -> Any:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    try:
        sidecar = epistemic_graph.sidecar_path(Path(vault_root))
        paths = (
            graph_sync.checkpoint_path(Path(vault_root)),
            sidecar,
            sidecar.with_name(sidecar.name + "-wal"),
        )
    except Exception:  # noqa: BLE001 - an unresolvable state root never matches
        return (object(),)
    return tuple(signature(path) for path in paths)


def _graph_snapshot_signal(vault_root: Path) -> tuple[Any, Any]:
    """What a declined proof waits on: the durable generation and the published snapshot.

    A serving worker's write moves the first; its publication -- the thing a
    declined proof usually needs -- moves the second.
    """
    return _durable_generation(vault_root), _read_snapshot_token(vault_root, quiet=True)


def _observe_full_marker(vault_root: Path) -> tuple[int, int] | None:
    """The whole-vault marker and its raise count, or None. Never raises."""
    from . import deferred_index

    try:
        return deferred_index.graph_full_rebuild_observation(Path(vault_root))
    except Exception:  # noqa: BLE001 - an unread marker is simply not retired
        return None


def reprove_graph_snapshot_if_due(vault_root: Path) -> bool:
    """Re-run the graph proof when the graph moved and the interval passed.

    True only when this call re-proved and its proof is now the one held. The
    2026-09-27 upgrade is why this exists: the standby's single proof declined
    50 s after the serving worker's last publication, and nothing ever ran it
    again, so the cutover waited out its whole budget on a proof that could not
    change. It keeps running after a proof succeeds, so the proof promotion
    relies on is the latest one the standby could make.
    """
    global _attempt_stat
    from . import freshness

    vault_root = Path(vault_root)
    with _lock:
        if not _standby or _promoting or _promoted or _discarded:
            return False
        ended = _attempt_ended_at
        last_signal = _attempt_signal
        last_stat = _attempt_stat
    if ended is not None and _clock() - ended < REPROVE_INTERVAL_SECONDS:
        return False
    stat = _graph_stat(vault_root)
    if last_stat is not None and stat == last_stat:
        return False
    signal = _graph_snapshot_signal(vault_root)
    with _lock:
        # Whatever moved the stat, the signal read here describes it; the
        # next poll only opens the sidecar again if something moves again.
        _attempt_stat = stat
    if signal == last_signal:
        return False
    # This process has no watcher, so its recall registry is as old as the
    # last seed; the adoption's origin is judged against it.
    try:
        freshness.rebaseline(vault_root)
    except Exception:  # noqa: BLE001 - an unseeded scope only costs adoption
        log.warning("standby recall registry reseed failed", exc_info=True)
    proved = prove_graph_snapshot(vault_root)
    log.info("standby graph snapshot re-proof adopted=%s", proved)
    return proved


def reprove_until_promoted(
    vault_root: Path, *, poll_seconds: float | None = None
) -> bool:
    """Keep the graph proof current until promotion or discard.

    Returns whether a proof is held when it stops. Reads only, like the proof;
    the supervisor's warm budget still bounds how long a standby may wait.
    """
    interval = REPROVE_POLL_SECONDS if poll_seconds is None else poll_seconds
    while True:
        with _lock:
            if not _standby or _promoting or _promoted or _discarded:
                return _proved_token is not None
            stop = _discard_requested
        try:
            reprove_graph_snapshot_if_due(vault_root)
        except Exception:  # noqa: BLE001 - a failed re-proof leaves the held proof
            log.warning("standby graph snapshot re-proof failed", exc_info=True)
        if stop.wait(interval):
            with _lock:
                return _proved_token is not None


def adoption_record() -> dict[str, Any]:
    """What the snapshot adoption established, for the cutover readiness block."""
    with _lock:
        adoption = _adoption
    if adoption is None:
        return {"residue": 0, "reason": "not_attempted"}
    return {"residue": len(adoption.residue), "reason": adoption.reason}


def proved_checkpoint() -> str | None:
    """The checkpoint token this standby proved, if any."""
    with _lock:
        return _proved_token


def prove_retrieval_catalog(vault_root: Path) -> bool:
    """Prove the maintained lexical catalog current, read-only.

    A standby must never reconcile or repair the catalog: ``ensure_fresh``
    discards verified state and mutates the live sidecar under the per-vault
    publication barrier, and ``request_repair`` schedules a publication — both
    belong to the worker that is still serving, which is the single repair owner
    for this vault. The standby therefore only asks whether the catalog is
    already current, with repair scheduling explicitly off. An uncurrent catalog
    leaves ``lexical`` waiting; the serving worker's own repair owner is the only
    thing allowed to fix it, and the warm budget covers the wait.
    """
    from . import freshness, lexstore

    try:
        if not lexstore.maintained_content_index_enabled():
            # No maintained content index to prove; nothing gates retrieval.
            return True
        return lexstore.runtime_retrieval_catalog_current(
            Path(vault_root),
            require_live_projection=freshness.event_indexes_enabled(),
            schedule_repair=False,
        )
    except Exception:  # noqa: BLE001 - an unprovable catalog is a waiting component
        log.warning("standby retrieval catalog proof failed", exc_info=True)
        return False


def prepare_detached_catalog(vault_root: Path) -> bool:
    """Build this release's lexical catalogue beside an incompatible live one.

    Called only when the read-only proof failed. A live catalogue at this
    release's schema and semantic identity that merely lags the corpus is the
    serving worker's repair owner's to catch up, and so is a missing one; the
    standby leaves both alone and `lexical` stays waiting, as before. A live
    catalogue an earlier release published can never become current for this
    one while that release serves, so the standby builds its own into a rebuild
    temp. The build takes no publication barrier and touches no live file; the
    temp is published only by promotion, after the serving worker has exited.
    """
    global _detached_catalog, _detached_built
    from . import lexstore

    try:
        if not lexstore.maintained_content_index_enabled():
            return False
        vault_root = Path(vault_root)
        if not lexstore.lexical_path(vault_root).exists():
            return False
        if lexstore.live_catalog_compatible(vault_root):
            return False
        with _lock:
            if _discarded or _promoted:
                return False
        started = time.monotonic()
        detached = lexstore.build_detached_catalog(vault_root, cancel=_discard_requested)
    except Exception:  # noqa: BLE001 - an unbuilt catalogue is a waiting component
        log.warning("standby detached catalogue build failed", exc_info=True)
        return False
    if detached is None:
        return False
    with _lock:
        stopped = _discarded or _promoted
        if not stopped:
            _detached_catalog = detached
            _detached_built = True
    if stopped:
        lexstore.discard_detached_catalog(vault_root, detached)
        return False
    log.info(
        "standby built a detached lexical catalogue in %.1f ms; promotion adopts it",
        (time.monotonic() - started) * 1000.0,
    )
    return True


def _adopt_detached_catalog(vault_root: Path) -> str | None:
    """Publish the catalogue this standby built, now that it owns the vault.

    Returns the handoff record's verdict, or None when nothing was built. A
    catalogue that cannot be adopted leaves the live one to the promoted
    worker's ordinary repair, which rebuilds it: the cold path, recorded.
    """
    global _detached_catalog, _detached_built
    from . import lexstore

    with _lock:
        detached = _detached_catalog
        _detached_catalog = None
        # Adopted or not, there is no detached catalogue any more.
        _detached_built = False
    if detached is None:
        return None
    try:
        adopted = lexstore.adopt_detached_catalog(vault_root, detached)
    except Exception:  # noqa: BLE001 - the coalesced repair owns an unadopted catalogue
        log.warning("promoted worker could not adopt its detached catalogue", exc_info=True)
        adopted = False
        try:
            lexstore.discard_detached_catalog(vault_root, detached)
        except Exception:  # noqa: BLE001 - an orphan temp is reaped by the next sweep
            log.warning("promoted worker could not discard its detached catalogue", exc_info=True)
    return "adopted" if adopted else "rebuild-after-promotion"


def discard() -> None:
    """Remove what an unpromoted standby built. Called when the process stops.

    A discarded candidate must not leave a whole catalogue behind in the state
    directory. A build still running is cancelled and waited for, bounded, so
    no temp outlives the discard without its builder's lock.
    """
    global _detached_catalog, _detached_built, _discarded
    from . import lexstore

    with _lock:
        if _promoted or not _standby:
            return
        _discarded = True
        _detached_catalog = None
        _detached_built = False
    _discard_requested.set()
    try:
        lexstore.discard_detached_catalogs()
    except Exception:  # noqa: BLE001 - an orphan temp is reaped by the next sweep
        log.warning("standby detached catalogue discard failed", exc_info=True)


def _preload_models() -> None:
    """Load the models a promoted worker would otherwise fault in per request."""
    from . import readiness

    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS") or not _preload_allowed():
        # Nothing to load, or this mode keeps models lazy. Mark the component so
        # a promoted process does not defer on a preload that never runs.
        readiness.mark_ready("embeddings")
        return
    from . import embeddings

    try:
        embeddings.get_model().encode(["warm"])
    except Exception:  # noqa: BLE001 - a failed preload leaves the component waiting
        log.warning("standby embedding preload failed", exc_info=True)
        return
    readiness.mark_ready("embeddings")


def build_semantic_corpus(vault_root: Path) -> bool:
    """Build this process's semantic corpus context. Reads only; publishes nothing.

    On the 0.85.0 upgrade the cutover itself was 1.6 s and governed writes
    stayed refused for ~30 s after it, because the admission gate waits on
    `semantic_corpus` (task 1.14) and the standby had never built one: the
    promoted worker paid 9.9 s of corpus build on 4271 pages while every write
    got `MUTATION_WARMING`. A short unavailable window that hands back a
    process which refuses writes has moved the outage, not removed it.

    Safe for a standby, which is the bar every step here has to clear. Checked
    both ways: statically, `build_corpus_context` walks and parses Markdown,
    resolves in memory, and caches under a module-level `threading.RLock` --
    there is no `batch_atomic_write`, no mutation-lock hold, no sidecar write
    and no publication anywhere in it, and its `freshness.consumer_checkpoint`
    and registry loads are in-memory reads. Empirically, a build over a fixture
    vault changed zero bytes under the vault and zero under the state root.
    `tests/test_standby_promotion.py` pins that as a census either side of the
    call, so a future build that starts publishing fails here rather than on a
    live cutover.

    A serving worker may write while this runs, which makes the built context
    stale for those pages and is not a problem: the cache is captioned by a stat
    census, so the first use after promotion reparses only the changed parents.
    What the gate needs is that nobody pays the COLD build, and that survives.
    """
    global _corpus_built, _corpus_attempted
    from . import readiness, semantic_contract

    with _lock:
        _corpus_attempted = True
    try:
        semantic_contract.build_corpus_context(Path(vault_root))
    except Exception:  # noqa: BLE001 - an unbuilt corpus is a waiting component
        log.warning("standby semantic corpus build failed", exc_info=True)
        return False
    with _lock:
        _corpus_built = True
    readiness.mark_ready("semantic_corpus")
    return True


def carried_warm_components() -> frozenset[str]:
    """Readiness components the promoted worker may treat as already satisfied.

    Empty on a cold start, so `warm_all` runs in full exactly as it always has.
    """
    with _lock:
        return _carried


def _carried_at_promotion(record: dict[str, Any]) -> frozenset[str]:
    """Name the warm this promotion inherits rather than repeats.

    `lexical` and `embeddings` are read from `readiness`, which still holds the
    standby warm's marks at this point: `promote()` computes this BEFORE
    `release()`, and `release()` is what eventually reaches
    `readiness.begin_warm()` and clears them. The corpus is read from this
    module's own record instead, for the same reason `cutover_components` does:
    one fact, one place to read it.

    `graph_snapshot` is carried only on a `current` verdict. That is the one
    where promotion re-proved that the checkpoint the standby adopted is still
    the checkpoint on disk, so re-adopting would buy nothing -- 19.5 s of it on
    the 0.85.0 cutover. `advanced`, `unproven` and `rebuild-after-promotion` all
    mean the opposite, and each leaves the component out so `warm_all` runs the
    handoff in full. (A residue failure rewrites the verdict to
    `rebuild-after-promotion`, so it is covered by the same test.)
    """
    from . import readiness

    carried = {
        component
        for component in ("lexical", "embeddings")
        if readiness.is_ready(component)
    }
    with _lock:
        if _corpus_built:
            carried.add("semantic_corpus")
    if record.get("snapshot") == "current":
        carried.add("graph_handoff")
    if record.get("lexical_catalogue") == "adopted":
        # Not a readiness component: it tells the promoted worker's warm that
        # the live catalogue is one this process built before the serving
        # worker's last writes, so one bounded heal runs before its proof.
        carried.add("catalogue_handoff")
    return frozenset(carried)


def warm(vault_root: Path) -> None:
    """Warm a standby to cutover readiness without owning any state.

    Deliberately not :func:`warmup.warm_all`: that path reconciles or schedules
    repair of the lexical catalog, which is a publication this process must not
    make while another worker owns the vault. Everything here reads: the catalog
    is proved, the rebuildable caches are populated in memory, the models are
    loaded, and the graph snapshot is proved against disk. The one write is a
    catalogue built detached, beside a live one an earlier release published,
    which nothing reads until promotion adopts it. Never raises; an unready
    component stays ``waiting`` and the supervisor's warm budget decides what
    happens next.
    """
    from . import freshness, mode, readiness

    vault_root = Path(vault_root)
    readiness.begin_warm()
    try:
        # Seed this process's recall registry before anything reads it. The
        # registry is process-local module state, so a standby seeding it
        # publishes nothing; without it `adopt_recall_origin` refuses a cold
        # scope and the promoted worker pays the whole-vault pass adoption
        # exists to remove.
        try:
            freshness.rebaseline(vault_root)
        except Exception:  # noqa: BLE001 - an unseeded scope only costs adoption
            log.warning("standby recall registry seed failed", exc_info=True)
        if prove_retrieval_catalog(vault_root):
            readiness.mark_ready("retrieval_catalog")
            try:
                warmup.warm_caches(
                    vault_root,
                    preload_models=_preload_allowed(),
                    preload_cpu_caches=mode.preload_cpu_caches(),
                )
            except Exception:  # noqa: BLE001 - caches are rebuildable on demand
                log.warning("standby cache warm-up failed", exc_info=True)
            readiness.mark_ready("lexical")
        elif prepare_detached_catalog(vault_root):
            # No caches are warmed over rows promotion replaces. The build took
            # up to a minute, during which this unwatched registry went stale;
            # reseed it so the graph proof below starts from the disk as it is.
            try:
                freshness.rebaseline(vault_root)
            except Exception:  # noqa: BLE001 - an unseeded scope only costs adoption
                log.warning("standby recall registry reseed failed", exc_info=True)
        else:
            log.info(
                "standby retrieval catalog is not current; the serving worker's "
                "repair owner has to publish one before this candidate can cut over"
            )
        # After the seed and after the resolver, which is primed here rather
        # than left to `warm_caches`: that gate is closed in every resource mode
        # that does not preload CPU caches, and the ordering is what makes the
        # adoption's origin acceptable.
        try:
            warmup.prime_recall_resolver(vault_root)
        except Exception:  # noqa: BLE001 - an unprimed resolver only costs adoption
            log.warning("standby recall resolver prime failed", exc_info=True)
        prove_graph_snapshot(vault_root)
        # After the graph step, for the same reason `warm_all` puts the handoff
        # first: adoption is what the first governed write depends on, and the
        # corpus is what the gate that admits it waits for.
        build_semantic_corpus(vault_root)
        _preload_models()
    except Exception:  # noqa: BLE001 - a standby warm must never die loudly
        log.warning("standby warm-up crashed", exc_info=True)
    finally:
        readiness.finish_warm()


def start_warm(vault_root: Path) -> threading.Thread:
    """Run :func:`warm` on a daemon thread so the transport answers meanwhile."""
    thread = threading.Thread(
        target=_warm_then_reprove,
        args=(Path(vault_root),),
        name="exomem-standby-warm",
        daemon=True,
    )
    thread.start()
    return thread


def _warm_then_reprove(vault_root: Path) -> None:
    """Warm, then keep a declined graph proof alive until the cutover can use it."""
    warm(vault_root)
    try:
        reprove_until_promoted(vault_root)
    except Exception:  # noqa: BLE001 - a standby must never die loudly
        log.warning("standby graph snapshot re-proof loop crashed", exc_info=True)


def _reprove(vault_root: Path) -> Any:
    """Re-run the source proof after the migrator wrote state."""
    from . import epistemic_graph

    try:
        return epistemic_graph.EpistemicGraphIndex(vault_root).adopt_published_snapshot(
            apply_residue=False
        )
    except Exception:  # noqa: BLE001 - a failed re-proof rebuilds after promotion
        log.warning("standby snapshot re-proof failed", exc_info=True)
        return epistemic_graph.SnapshotAdoption(False, reason="reproof_raised")


def _apply_residue(vault_root: Path, adoption: Any) -> tuple[int, str]:
    """Enqueue the repair this adoption owes, now that this process owns it.

    Returns how many paths were queued and, when something was owed and could
    not be, the reason the cold path uses for the same failure. Owing nothing
    and failing to record what you owe are different events: collapsing them
    made an upgrade that silently lost its repair look like a clean one.
    """
    from . import epistemic_graph

    residue = getattr(adoption, "residue", ()) if adoption is not None else ()
    if not residue or not getattr(adoption, "adopted", False):
        return 0, ""
    try:
        applied = epistemic_graph.EpistemicGraphIndex(vault_root).apply_adopted_residue(
            residue
        )
    except Exception:  # noqa: BLE001 - the coalesced rebuild owns an unenqueued residue
        log.warning("promoted worker could not enqueue its adopted residue", exc_info=True)
        return 0, "residue_enqueue_failed"
    if not applied:
        log.warning("promoted worker could not enqueue its adopted residue")
        return 0, "residue_enqueue_failed"
    return len(residue), ""


def _retire_covered_full_marker(
    vault_root: Path,
    adoption: Any,
    record: dict[str, Any],
    observation: tuple[int, int] | None,
    generation: Any,
) -> None:
    """Retire whole-vault debt the source proof covered, and say what happened.

    The proof hashed every admitted page against the snapshot and enumerated
    each divergence as residue, so once that residue is queued the snapshot plus
    the queue describe the disk exactly: the unknown-scope debt a marker stands
    for is known and owned. Without this the promoted worker inherits a marker
    its drain can pay only with a whole-vault pass, which never held still under
    agent writes on 2026-09-27. Compare-and-swap on the value and raise count,
    so debt raised after the proof's sample survives -- and only while the
    durable generation is the one the proof sampled: a full-scope batch raises
    its marker before its bytes land, so a proof that finished between the two
    saw the marker but not the bytes it stands for.
    """
    from . import deferred_index

    try:
        standing = deferred_index.graph_full_rebuild_pending(vault_root)
    except Exception:  # noqa: BLE001 - an unread marker is left as it is
        standing = None
    if standing is None:
        return
    covered = (
        observation is not None
        and bool(getattr(adoption, "adopted", False))
        and record.get("snapshot") == "current"
        and not record.get("reason")
        and generation != "unreadable"
        and _durable_generation(vault_root) == generation
    )
    retired = False
    if covered:
        assert observation is not None
        try:
            retired = deferred_index.retire_observed_graph_full_rebuild(
                vault_root, observation
            )
        except Exception:  # noqa: BLE001 - the marker stays, so the debt stays
            log.warning("promotion could not retire the covered full marker", exc_info=True)
    record["full_marker"] = "retired" if retired else "retained"


def _acquire_ownership() -> None:
    """Take the writer lease this process deliberately refused while standby."""
    from .writer_lease import start_server_lifecycle

    start_server_lifecycle()


def promote(vault_root: Path, *, migrated: bool) -> dict[str, Any]:
    """Take ownership after the previous worker and its descendants have exited.

    Re-validates the proved checkpoint against disk first.  A checkpoint that
    advanced is recorded as such; a proof that now fails is recorded and
    promotion proceeds anyway, leaving the repair to the coalesced rebuild path.
    """
    global _promoting
    with _lock:
        if _promoted:
            return {"ok": True, "already_promoted": True}
        if not _standby:
            raise RuntimeError("only a standby worker can be promoted")
        _promoting = True
        proved = _proved_token
        proof_marker = _proof_marker
        proof_generation = None if _proof_signal is None else _proof_signal[0]
    try:
        return _promote_owned(
            Path(vault_root),
            migrated=migrated,
            proved=proved,
            proof_marker=proof_marker,
            proof_generation=proof_generation,
        )
    except BaseException:
        # A promotion that raised promoted nothing; the standby keeps proving.
        with _lock:
            if not _promoted:
                _promoting = False
        raise


def _promote_owned(
    vault_root: Path,
    *,
    migrated: bool,
    proved: str | None,
    proof_marker: tuple[int, int] | None,
    proof_generation: Any,
) -> dict[str, Any]:
    """`promote` once it has claimed the standby; see there."""
    global _promoted, _standby, _adoption, _carried
    # `revalidated` says promotion re-checked the snapshot at all; `reproved`
    # says it re-ran the whole SOURCE proof, which only the migrated branch
    # does. The other branch compares the checkpoint pair -- a real
    # re-validation, and deliberately cheaper, but not a proof, and calling it
    # one overstated what the handoff record was attesting to.
    record: dict[str, Any] = {
        "ok": True,
        "migrated": bool(migrated),
        "revalidated": False,
        "reproved": False,
    }
    started = time.monotonic()
    adoption = _adoption
    # The whole-vault marker standing before the source proof this promotion
    # relies on, and so the only debt that proof may be said to have paid.
    covered_marker: tuple[int, int] | None = None
    covered_generation: Any = None
    if proved is None:
        record["snapshot"] = "unproven"
    elif migrated:
        # The offline migrator is the only writer between the two workers, and
        # it ran. Re-run the whole proof rather than compare a checkpoint pair
        # that describes state it may have rewritten.
        # Generation before marker, the order the warm-time proof samples in.
        covered_generation = _durable_generation(Path(vault_root))
        covered_marker = _observe_full_marker(Path(vault_root))
        adoption = _reprove(Path(vault_root))
        with _lock:
            # The re-proof supersedes the warm's: `adoption_record()` must
            # report the residue this process actually owes.
            _adoption = adoption
        record["revalidated"] = True
        record["reproved"] = True
        record["snapshot"] = "current" if adoption.adopted else "rebuild-after-promotion"
    else:
        current = snapshot_token(Path(vault_root))
        # A checkpoint-pair comparison, not a source proof: see `record` above.
        record["revalidated"] = True
        if current is None:
            record["snapshot"] = "rebuild-after-promotion"
        elif current != proved:
            record["snapshot"] = "advanced"
        else:
            record["snapshot"] = "current"
            covered_marker = proof_marker
            covered_generation = proof_generation
    # The writer lease is what makes this process the owner, so it comes first:
    # enqueueing the repair the adoption owes is a write, and this process has
    # no standing to make it until the lease says the vault is its own.
    _acquire_ownership()
    # Owned now, and the worker that kept an earlier release's catalogue current
    # has exited, so the catalogue this standby built may replace it.
    catalogue = _adopt_detached_catalog(Path(vault_root))
    if catalogue is not None:
        record["lexical_catalogue"] = catalogue
    # The private-identity inventory this process built while warming was proved
    # against a generation the outgoing worker was still advancing, and that
    # worker's publications are invisible here. The shared token is the only
    # evidence the carried inventory still covers the vault; when it disagrees
    # the inventory is dropped and rebuilt off the request thread, so the first
    # interactive caller after promotion does not pay for a whole-vault walk.
    from . import reserved_paths

    identity_cause = reserved_paths.identity_catalogue_refusal(Path(vault_root))
    if identity_cause is None:
        record["identity_catalogue"] = "current"
    else:
        # Name the cause: a vault that moved under the standby and a gate that
        # was merely busy refuse identically, and the record is the only place
        # the difference survives.
        record["identity_catalogue"] = "rebuild-after-promotion"
        record["identity_catalogue_cause"] = identity_cause
        reserved_paths.schedule_identity_catalogue_warm(Path(vault_root))
    applied, residue_failure = _apply_residue(Path(vault_root), adoption)
    record["residue_applied"] = applied
    if residue_failure:
        # The repair this process owes is not recorded anywhere durable, so the
        # coalesced rebuild is what will fix the projection. Say so.
        record["snapshot"] = "rebuild-after-promotion"
        record["reason"] = residue_failure
    _retire_covered_full_marker(
        Path(vault_root), adoption, record, covered_marker, covered_generation
    )
    carried = _carried_at_promotion(record)
    record["carried_from_standby"] = sorted(carried)
    from . import runtime_readiness

    with _lock:
        _promoted = True
        _standby = False
        _carried = carried
        activation = _activation
        # Under the same lock as the flip: a readiness proof that reads
        # `standby: false` measured part of its state as a standby, and the
        # supervisor reads readiness as soon as this POST returns.
        runtime_readiness.invalidate_cached_readiness()
    # Everything this process owes the handoff is settled above. `release()`
    # starts the promoted worker's own warm, which reads `_carried` on its way
    # through `begin_warm`, so nothing below may still be deciding what it holds.
    release = getattr(activation, "release", None)
    if callable(release):
        release()
    record["promotion_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    log.info("standby promoted: %s", record)
    return record


def reset_for_tests() -> None:
    """Clear process-local standby state; intentionally public for tests."""
    global _standby, _promoted, _proved_token, _adoption, _activation
    global _carried, _corpus_built, _corpus_attempted
    global _detached_catalog, _detached_built, _discarded, _discard_requested
    global _promoting, _proof_signal, _proof_marker
    global _attempt_signal, _attempt_stat, _attempt_ended_at
    with _lock:
        _promoting = False
        _proof_signal = None
        _proof_marker = None
        _attempt_signal = None
        _attempt_stat = None
        _attempt_ended_at = None
        _detached_catalog = None
        _detached_built = False
        _discarded = False
        _discard_requested = threading.Event()
        _standby = False
        _promoted = False
        _proved_token = None
        _adoption = None
        _carried = frozenset()
        _corpus_built = False
        _corpus_attempted = False
        _activation = None
