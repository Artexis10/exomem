"""Standby warm-up and promotion for the managed worker handoff (D7-D9).

A standby worker binds its own private socket and warms everything a cutover
needs — the lexical catalog, the embedding model when preload is allowed, and a
read-only proof of the current graph snapshot — while the previous worker keeps
serving.  Until the supervisor promotes it, the standby owns nothing: it takes
no writer lease, publishes no index or graph state, schedules no drain, media or
watcher work, and starts no descendants.

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
CUTOVER_COMPONENTS = ("lexical", "embeddings", "graph_snapshot")

_lock = threading.Lock()
_standby = False
_promoted = False
_proved_token: str | None = None
_activation: Any = None


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
    components: dict[str, str] = {}
    for component in CUTOVER_COMPONENTS:
        if component == "graph_snapshot":
            components[component] = "ready" if snapshot_ready else "waiting"
        elif component == "embeddings":
            if _preload_allowed():
                components[component] = "ready" if readiness.is_ready("embeddings") else "waiting"
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
    }


def snapshot_token(vault_root: Path) -> str | None:
    """Prove the published graph snapshot read-only and name the checkpoint.

    This is the graph module's own validated read transaction: it runs the cold
    source-bytes and resolver-topology proof for a process that did not publish
    the sidecar, and declines rather than repair.  Nothing is written, opened for
    write, or published; the connection is closed before returning.
    """
    from . import epistemic_graph

    try:
        index = epistemic_graph.EpistemicGraphIndex(Path(vault_root))
        conn = index._open_read_snapshot()
    except Exception:  # noqa: BLE001 - an unprovable snapshot is a waiting component
        log.warning("standby graph snapshot proof failed", exc_info=True)
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
        log.warning("standby graph checkpoint read failed", exc_info=True)
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
    """Run the read-only cold proof and retain the checkpoint it proved."""
    global _proved_token
    token = snapshot_token(vault_root)
    with _lock:
        _proved_token = token
    return token is not None


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


def warm(vault_root: Path) -> None:
    """Warm a standby to cutover readiness without owning any state.

    Deliberately not :func:`warmup.warm_all`: that path reconciles or schedules
    repair of the lexical catalog, which is a publication this process must not
    make while another worker owns the vault. Everything here reads: the catalog
    is proved, the rebuildable caches are populated in memory, the models are
    loaded, and the graph snapshot is proved against disk. Never raises; an
    unready component stays ``waiting`` and the supervisor's warm budget decides
    what happens next.
    """
    from . import mode, readiness

    vault_root = Path(vault_root)
    readiness.begin_warm()
    try:
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
        else:
            log.info(
                "standby retrieval catalog is not current; the serving worker's "
                "repair owner has to publish one before this candidate can cut over"
            )
        _preload_models()
    except Exception:  # noqa: BLE001 - a standby warm must never die loudly
        log.warning("standby warm-up crashed", exc_info=True)
    finally:
        readiness.finish_warm()
    try:
        prove_graph_snapshot(vault_root)
    except Exception:  # noqa: BLE001 - an unproven snapshot is a waiting component
        log.warning("standby graph snapshot proof crashed", exc_info=True)


def start_warm(vault_root: Path) -> threading.Thread:
    """Run :func:`warm` on a daemon thread so the transport answers meanwhile."""
    thread = threading.Thread(
        target=warm,
        args=(Path(vault_root),),
        name="exomem-standby-warm",
        daemon=True,
    )
    thread.start()
    return thread


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
    global _promoted, _standby
    with _lock:
        if _promoted:
            return {"ok": True, "already_promoted": True}
        if not _standby:
            raise RuntimeError("only a standby worker can be promoted")
        proved = _proved_token
    record: dict[str, Any] = {"ok": True, "migrated": bool(migrated), "reproved": False}
    started = time.monotonic()
    if proved is None:
        record["snapshot"] = "unproven"
    else:
        current = snapshot_token(Path(vault_root))
        record["reproved"] = True
        if current is None:
            record["snapshot"] = "rebuild-after-promotion"
        elif current != proved:
            record["snapshot"] = "advanced"
        else:
            record["snapshot"] = "current"
    _acquire_ownership()
    with _lock:
        _promoted = True
        _standby = False
        activation = _activation
    release = getattr(activation, "release", None)
    if callable(release):
        release()
    record["promotion_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    log.info("standby promoted: %s", record)
    return record


def reset_for_tests() -> None:
    """Clear process-local standby state; intentionally public for tests."""
    global _standby, _promoted, _proved_token, _activation
    with _lock:
        _standby = False
        _promoted = False
        _proved_token = None
        _activation = None
