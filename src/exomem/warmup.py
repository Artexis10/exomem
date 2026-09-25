"""Startup warm-up: lexical caches first, then models — off the boot path.

`warm_all` runs everything the first user-facing calls would otherwise pay
inline: the parsed-page cache, the BM25 corpora for BOTH scopes (auto-widen
runs a vault-scope BM25 on every kb query), the wikilink resolver, the semantic corpus, the
embedding/CLIP matrices, and then the model preloads (bge, reranker, CLIP)
that used to block `build_server` for ~30s (minutes on a first-ever
download).

`start_background` runs `warm_all` on a daemon thread so `mcp.run()` listens
immediately (OpenSpec: add-instant-start-boot). Coordination with request
threads goes through `readiness`: lexical caches are marked ready before any
model load starts, each model marks its component as it lands, and request
paths defer model-touching lanes while the warm is in flight instead of
blocking on the singleton locks. Cross-thread cache builds are safe — the
request path already runs on FastMCP/REST worker threads today, and the BM25
corpus + resolver builds are serialized by their own build locks.

Every step soft-fails and records its duration; a failed model preload leaves
its readiness event unset (requests fall back to inline lazy-load semantics
once the warm finishes). `EXOMEM_DISABLE_WARMUP` skips warm-up entirely —
pure lazy, the pre-warmup cold behavior. `EXOMEM_EAGER_BOOT=1` (handled in
`server.build_server`) runs `warm_all` synchronously instead: bit-for-bit the
old blocking boot, the rollback lever for deployments.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

_WARM_THREAD: threading.Thread | None = None


def warmup_enabled() -> bool:
    return not os.environ.get("EXOMEM_DISABLE_WARMUP")


def model_preload_allowed(mode_name: str | None = None) -> bool:
    """Whether startup may eagerly load model weights in this process.

    Normal and quiet modes default to lazy model loads on every OS. Multiple local
    clients naturally mean multiple Python processes, and eager BGE/CLIP preloads
    multiply memory residency. `EXOMEM_PRELOAD_MODELS=1` explicitly opts in.

    The decision itself lives in `mode.preload_models` so that `mode.resolved()` —
    and therefore `status.policy` and `doctor` — report the same answer warm-up
    acts on, rather than the mode default with the override invisibly applied here.
    """
    from . import mode

    return mode.preload_models(mode_name or "normal")


def warm_retrieval_catalog(vault_root: Path) -> bool:
    """Verify both maintained lexical scopes through exactly one repair owner.

    A managed server must never run ``ensure_fresh`` alongside the watcher-driven
    repair worker. Both paths can rebuild the full catalog, and starting them
    together makes each publication contend with or invalidate the other. The
    server therefore proves an already-current catalog cheaply and otherwise
    delegates to the existing single-flight repair worker. Offline callers keep
    the synchronous reconciliation contract.

    Returns ``True`` only when retrieval may be admitted now. A ``False`` result
    means normal managed repair is running in the background and will promote
    readiness after it publishes a checkpoint-current catalog. Eager managed
    boot waits for that same owner, preserving its synchronous rollback contract
    without introducing a second rebuild path.
    """
    from . import freshness, lexstore, readiness

    if not lexstore.maintained_content_index_enabled():
        # Explicit Python mode and SQLite builds without FTS retain the supported
        # reference implementation; there is no maintained content index to gate.
        if readiness.runtime_managed():
            generation = readiness.retrieval_proof_generation()
            return readiness.admit_retrieval_proof(
                generation
            ) or readiness.is_ready("retrieval_catalog")
        return True
    event_indexes = freshness.event_indexes_enabled()
    if readiness.runtime_managed() and event_indexes and not all(
        freshness.recall_is_live(vault_root, scope) for scope in freshness.SCOPES
    ):
        # The normal activation path waits for the watcher seed before getting
        # here.  Watchdog-free or failed-seed deployments still need one
        # authoritative process projection, but the expensive source walk must
        # remain on this background warm thread rather than migrate to find().
        baselines = freshness.rebaseline(vault_root)
        if not all(baselines.values()):
            raise RuntimeError(
                f"maintained recall projection incomplete: {baselines!r}"
            )
    if readiness.runtime_managed():
        def _prove_and_admit() -> bool:
            proof_generation = readiness.retrieval_proof_generation()
            if not lexstore.runtime_retrieval_catalog_current(
                vault_root,
                require_live_projection=event_indexes,
            ):
                return False
            # A catalog inherited from the previous process is current but
            # stamped in that registry's lineage; adopt it into this one so the
            # first governed write can bless it with an ordinary delta.
            lexstore.rebase_inherited_catalog_lineage(vault_root)
            # Bind the CAS token to this successful proof, not to the whole
            # warm operation: an earlier stale proof or completed repair may
            # legitimately advance admission before an eager retry succeeds.
            return readiness.admit_retrieval_proof(
                proof_generation
            ) or readiness.is_ready("retrieval_catalog")

        if _prove_and_admit():
            return True
        # ``catalog_readiness`` above may already have scheduled this repair.
        # Request it explicitly as well so every incomplete outcome records the
        # full-rebuild requirement; the repair scheduler is single-flight.
        lexstore.request_repair(vault_root)
        log.info("managed retrieval catalog delegated to background repair")
        if os.environ.get("EXOMEM_EAGER_BOOT"):
            if not lexstore.await_repairs_idle(vault_root):
                raise RuntimeError("managed retrieval catalog repair timed out")
            if not _prove_and_admit():
                raise RuntimeError(
                    "managed retrieval catalog repair did not publish a current catalog"
                )
            return True
        return False
    lexstore.ensure_fresh(vault_root)
    store = lexstore.get_store(vault_root)
    incomplete = [
        (scope, verdict.status)
        for scope in ("kb", "vault")
        if not (
            verdict := store.catalog_readiness(
                scope,
                freshness.recall_checkpoint(vault_root, scope).triple,
                allow_delta=False,
            )
        ).complete
    ]
    if incomplete:
        raise RuntimeError(f"maintained lexical catalog incomplete: {incomplete!r}")
    return True


def _adopt_graph_snapshot(vault_root: Path, durations: dict[str, float]) -> bool:
    """Prove and adopt an inherited graph snapshot, if there is one to adopt.

    Records `graph_snapshot_residue`: how many paths the adoption took on as
    queued repair because the outgoing process left them deferred. Zero is a
    clean handoff; a positive count is an adoption that succeeded *and* owes the
    drain that many pages, which is what the operator needs to see rather than a
    bare success.
    """
    from . import epistemic_graph, service_standby

    if not epistemic_graph.graph_enabled():
        return False
    if service_standby.in_standby():
        # A standby runs this step itself, after the same seed and resolver, so
        # that it can report the residue and reason in its cutover readiness.
        # Adopting twice would pay the source proof twice for one handoff.
        return False
    adoption = epistemic_graph.EpistemicGraphIndex(vault_root).adopt_published_snapshot()
    durations["graph_snapshot_residue"] = float(len(adoption.residue))
    log.info(
        "graph snapshot adoption adopted=%s residue=%d reason=%s",
        adoption.adopted,
        len(adoption.residue),
        adoption.reason,
    )
    return adoption.adopted


def _run_step(durations: dict[str, float], name: str, fn) -> None:
    """Run one warm-up step and record its duration; warm-up must never raise."""
    t0 = time.perf_counter()
    try:
        fn()
    except Exception:  # noqa: BLE001 — warm-up must never break startup
        log.warning("warm-up step %s failed", name, exc_info=True)
    finally:
        durations[name] = round((time.perf_counter() - t0) * 1000.0, 1)


def prime_recall_resolver(vault_root: Path) -> None:
    """Build this process's recall resolver snapshot.

    Ordinary recall resolves links through the policy-projected view; the broad
    writer resolver stays lazy so warm-up never reads raw Records titles.

    Its own function because two unconditional callers need it and neither may
    have the other's side effects: start-up adopts the inherited snapshot after
    it, and a standby proves one without adopting anything the serving worker
    still owns. `recall_resolver_snapshot_at_checkpoint` refuses to build on a
    miss by design, so a process that never built one leaves every incremental
    pass bailing on `resolver_snapshot_unavailable`.
    """
    from . import find

    find.recall_resolver_snapshot(vault_root)


def warm_graph_handoff(vault_root: Path) -> dict[str, float]:
    """Adopt the inherited graph snapshot and prime the resolver the first write needs.

    Deliberately NOT inside `warm_caches`. Everything in there is a disposable
    CPU cache that a resource mode is entitled to skip, and skipping one costs
    only latency on a later request. This is not that. A replacement worker that
    never adopts its predecessor's published snapshot has no lineage it is
    allowed to advance, so its first governed write falls back to a whole-vault
    rebuild -- measured on the 0.83.1 deploy, where `mode=normal` leaves
    `preload_cpu_caches` False, `warm_caches` returned at its first gate, and
    the adoption step it used to contain never ran at all.

    The resolver primer belongs on the same unconditional path and for the same
    reason: `recall_resolver_snapshot_at_checkpoint` refuses to build on a miss
    by design, so a process that never built one leaves every incremental pass
    bailing on `resolver_snapshot_unavailable`.

    Ordering is load-bearing and pinned by test: this runs after the watcher's
    registry seed -- a seed replaces the registry maps wholesale -- and BEFORE
    the semantic corpus build that admits writers, because a write admitted
    ahead of adoption has no lineage to advance. Each step soft-fails like
    every other warm step.

    `EXOMEM_DISABLE_WARMUP` is the one gate still allowed to skip it, and
    deliberately: unlike `mode=normal` and `catalog_ready`, which are default
    states this process reaches on its own, it is an explicit operator opt-out
    from warming at all. It is not set on the personal service.
    """
    durations: dict[str, float] = {}
    if not warmup_enabled():
        return durations
    _run_step(durations, "resolver", lambda: prime_recall_resolver(vault_root))
    # A replacement worker inherits a derived graph it did not publish, and
    # `recall_delta_since` refuses a foreign origin by construction, so its first
    # governed write used to rebuild the whole vault purely to obtain a lineage
    # it could advance. Proving the inherited snapshot here -- against the disk
    # this registry is already projecting -- makes that checkpoint the delta
    # origin instead (`seamless-managed-worker-handoff`).
    _run_step(durations, "graph_snapshot", lambda: _adopt_graph_snapshot(vault_root, durations))
    return durations


def warm_caches(
    vault_root: Path,
    *,
    preload_models: bool = True,
    preload_cpu_caches: bool | None = None,
) -> dict[str, float]:
    """Warm find's rebuildable caches; returns per-step durations in ms.

    Quiet mode disables CPU cache preloading entirely: parsed pages, BM25 corpora,
    resolver state, and vector matrices all stay cold until a request needs them.
    `preload_models=False` still only gates model-backed vector/CLIP matrix warm-up
    when CPU cache preloading is otherwise allowed.
    """
    if not warmup_enabled():
        log.info("cache warm-up disabled via EXOMEM_DISABLE_WARMUP")
        return {}
    if preload_cpu_caches is None:
        from . import mode

        preload_cpu_caches = mode.preload_cpu_caches()
    if not preload_cpu_caches:
        log.info("cache warm-up skipped by resource mode")
        return {}
    from . import bm25, find, recall_policy

    durations: dict[str, float] = {}

    def _step(name: str, fn) -> None:
        _run_step(durations, name, fn)

    def _warm_pages() -> None:
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return
        for p in find._walk_md(kb):
            if not recall_policy.is_recall_candidate(vault_root, p):
                continue
            find._CACHE.get(p, vault_root)

    _step("pages", _warm_pages)
    _step("bm25_kb", lambda: bm25.warm(vault_root, "kb"))
    _step("bm25_vault", lambda: bm25.warm(vault_root, "vault"))
    # The resolver primer and the graph-snapshot adoption used to sit here. They
    # are not caches, so `warm_graph_handoff` now runs them on the unconditional
    # start-up path instead; this gate is only allowed to skip disposable work.
    if preload_models and not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        # One tiny search warms WHICHEVER backend serves vector search: the vec0
        # backend (sync check + first KNN faults in the vec tables; the numpy
        # matrix stays cold — not holding it resident is the backend's point) or
        # the in-memory scan (search loads the matrix via all_vectors(), the
        # historical warm). A missing sidecar is a no-op either way.

        def _warm_matrix() -> None:
            import numpy as np

            from . import embeddings

            q = np.full(
                embeddings.VECTOR_DIM,
                1.0 / (embeddings.VECTOR_DIM**0.5),
                dtype=np.float32,
            )
            embeddings.get_embedding_index(vault_root).search(q, k=1)

        def _warm_clip() -> None:
            import numpy as np

            from . import embeddings

            if embeddings.clip_enabled():
                q = np.full(
                    embeddings.CLIP_DIM,
                    1.0 / (embeddings.CLIP_DIM**0.5),
                    dtype=np.float32,
                )
                embeddings.get_clip_index(vault_root).search(q, k=1)

        _step("embedding_matrix", _warm_matrix)
        _step("clip_matrix", _warm_clip)
    log.info("cache warm-up done: %s", durations)
    return durations


def warm_all(vault_root: Path) -> dict[str, float]:
    """Required catalog/graph/corpus state, then optional caches and model preloads.

    Order is the product contract (catalog-first): retrieval admission lands
    first, then the graph handoff, then semantic write admission, before
    optional full-corpus caches and models can extend the warm window.

    The graph handoff runs *before* the semantic corpus, and that order is
    load-bearing rather than tidy. Writers are admitted once the corpus is
    built, and on the personal service that build is 30-36 s on 4209 pages --
    so with adoption behind it, the first governed write of a replacement
    worker dispatched with no delta origin, fell back on
    `recall_delta_incomplete`, and registered the whole-vault rebuild adoption
    exists to remove. The rebuild was then still in flight when adoption ran
    and declined its proof against a sidecar being rewritten under it. Adoption
    is sub-second to a few seconds; it belongs in the first seconds of the warm.
    Each stage soft-fails; a failed model preload leaves its component
    not-ready (never marked), so requests defer for the rest of the warm and
    then return to inline lazy-load semantics. Never raises.

    A worker promoted from a standby subtracts the steps that standby already
    ran in this same process and promotion re-verified (`carried_from_standby`
    in the returned durations and the completion line). It is a subtraction, not
    a second path: a cold start carries nothing and runs every step.
    """
    from . import mode, readiness, service_standby

    mode_name = mode.resolve_mode()
    preload = model_preload_allowed(mode_name)
    durations: dict[str, float] = {}
    catalog_ready = False
    catalog_started = time.perf_counter()
    managed_catalog = readiness.runtime_managed()
    # What the standby already did and promotion re-verified. Empty on a cold
    # start, which is the only reason this is a subtraction from a full warm
    # rather than a second warm path: there is exactly one step list, and a
    # cold start reads an empty set and runs all of it.
    carried = service_standby.carried_warm_components()
    durations["carried_from_standby"] = float(len(carried))
    try:
        catalog_ready = warm_retrieval_catalog(vault_root)
        if catalog_ready and not managed_catalog:
            readiness.mark_ready("retrieval_catalog")
    except Exception:  # noqa: BLE001 — startup stays live while repair retries
        log.warning("maintained retrieval catalog warm-up failed", exc_info=True)
    finally:
        durations["retrieval_catalog"] = round(
            (time.perf_counter() - catalog_started) * 1000.0,
            1,
        )
    try:
        # Unconditional, and first. Adoption is what lets a replacement worker's
        # first governed write stay incremental, so nothing that merely makes
        # this process nicer may run ahead of it, and no gate that exists to
        # protect a *cache* may skip it:
        #
        #   * not the resource mode -- `mode=normal` is the default and leaves
        #     `preload_cpu_caches` False, which is how the 0.83.1 deploy skipped
        #     adoption entirely;
        #   * not the corpus build -- writers are admitted on `semantic_corpus`,
        #     30-36 s on this vault, which is how 0.84.1 admitted a write into a
        #     process with no delta origin;
        #   * not `catalog_ready` -- the repair it defers to republishes the
        #     *lexical* catalogue, while this step builds a process-local
        #     resolver (`find.recall_resolver_snapshot`) and proves the graph
        #     sidecar. Neither is the artifact the detached repair owner is
        #     rewriting, so the collision that makes `warm_caches` wait does not
        #     apply here. A proof taken against a moving projection declines and
        #     says so; a proof never taken costs the next write a whole vault.
        if "graph_handoff" in carried:
            # Promotion re-proved this standby's adopted checkpoint against
            # disk and found it current, so the adoption `warm_graph_handoff`
            # would take is the one this process already holds. Re-taking it
            # cost the 0.85.0 cutover 19.5 s of snapshot proof and a second
            # `graph snapshot adoption` line, while the writes it was supposed
            # to protect were being refused for want of the components below.
            log.info("graph handoff carried from standby; adoption not repeated")
        else:
            durations.update(warm_graph_handoff(vault_root))
    finally:
        # Marked on every exit, including the ones where nothing ran. The
        # writers' gate waits on this component, and a component that can be
        # skipped without being marked would hold every write until the whole
        # warm -- model preloads included -- finished. "Settled" here means the
        # delta origin is as good as this process will get it, which an
        # adoption that declined or never ran satisfies as much as one that
        # succeeded.
        readiness.mark_ready("graph_handoff")
    semantic_started = time.perf_counter()
    try:
        if "semantic_corpus" in carried:
            # Built in this same process while it was a standby. The context is
            # captioned by a stat census, so pages the outgoing worker changed
            # under it reparse on first use; what is carried is the cold build,
            # 9.9 s on a 4271-page vault, which is what the admission gate was
            # waiting on for ~30 s after a 1.6 s cutover.
            log.info("semantic corpus carried from standby; build not repeated")
        else:
            from . import semantic_contract

            semantic_contract.build_corpus_context(vault_root)
        readiness.mark_ready("semantic_corpus")
    except Exception:  # noqa: BLE001 — semantic warm-up remains rebuildable
        log.warning("semantic corpus warm-up failed", exc_info=True)
    finally:
        durations["semantic_corpus"] = round(
            (time.perf_counter() - semantic_started) * 1000.0,
            1,
        )
    if catalog_ready and "lexical" in carried:
        # Same process, same caches: `service_standby.warm` calls `warm_caches`
        # with this process's own mode and preload policy.
        log.info("lexical caches carried from standby; warm not repeated")
        readiness.mark_ready("lexical")
    elif catalog_ready:
        durations.update(
            warm_caches(
                vault_root,
                preload_models=preload,
                preload_cpu_caches=mode.preload_cpu_caches(),
            )
        )
        readiness.mark_ready("lexical")
    else:
        # BM25/resolver warm-up reaches the live lexical sidecar with repair
        # enabled. Running it beside the detached repair owner makes the two
        # publications invalidate one another, so leave these disposable caches
        # cold until the admitted catalogue can serve their first request.
        #
        # A carried `lexical` keeps the mark it was given in
        # `_carry_forward_standby_readiness`, and that is deliberate rather than
        # an oversight of this branch. The standby marks `lexical` only after
        # proving the catalogue current and warming these caches IN THIS
        # PROCESS, so the caches are real memory that a later repair does not
        # take away; what this branch declines is building more of them beside
        # a repair owner, which is a publication concern, not a readiness one.
        # Re-marking here would be wrong; un-marking would defer requests for
        # caches this process already holds.
        log.info("optional recall cache warm-up skipped during catalog repair")

    def _model_step(name: str, fn) -> bool:
        t0 = time.perf_counter()
        try:
            fn()
            return True
        except Exception as e:  # noqa: BLE001 — preload is best-effort
            log.warning("%s preload failed (%s); first use pays the cost", name, e)
            return False
        finally:
            durations[name] = round((time.perf_counter() - t0) * 1000.0, 1)

    def _preload(step: str, loader, warm) -> bool:
        """Preload a model (readiness gates on this) then run a best-effort throwaway
        encode on the loaded object. Loading the weights isn't enough: the backend compiles
        its compute kernels on the FIRST forward pass (most visibly the Metal/MPS graph on
        Apple Silicon; CUDA/CPU pay a smaller first-call cost too), so warming a dummy input
        here moves that one-time compile onto the boot/idle path instead of the user's first
        query. The warm runs on the already-loaded model (loader is called exactly once) and
        is never load-bearing — readiness gates on the preload, and a warm failure (e.g. an
        MPS op gap) is swallowed. Cross-platform, not a Mac-only tweak."""
        box: dict = {}
        if not _model_step(step, lambda: box.update(m=loader())):
            return False
        try:
            warm(box["m"])
        except Exception:  # noqa: BLE001 — a warm-encode is a latency nicety, never a gate
            log.debug("%s warm-encode skipped", step, exc_info=True)
        return True

    def _replay_deferred_embeddings(items: list) -> None:
        from . import index_sync

        for item in items:
            item_vault, paths, *receipt_payload = item
            receipts = receipt_payload[0] if receipt_payload else None
            try:
                index_sync.replay_deferred_embedding(item_vault, list(paths), receipts)
            except Exception:  # noqa: BLE001 — durable receipt survives retry
                log.warning("deferred embed drain failed", exc_info=True)

    disabled = bool(os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"))
    if disabled or not preload:
        # Skip model preloads: either a lexical-only install (DISABLE_EMBEDDINGS,
        # nothing to load) or quiet mode (models lazy-load on first use, then the
        # idle-unload reaper reclaims them). Mark the model components ready either
        # way so finds during the lexical warm don't carry a "warming" marker for
        # models that won't preload, and writers stop deferring.
        reason = "EXOMEM_DISABLE_EMBEDDINGS" if disabled else "mode/preload policy"
        log.info("model preloads skipped (%s); models lazy-load on first use", reason)
        readiness.mark_ready("reranker")
        readiness.mark_ready("clip")
        drained = readiness.mark_ready("embeddings")
        # Quiet mode: embeddings ARE available (just lazy), so replay any write
        # parked during the brief lexical warm — mirror the real-preload branch so
        # those edits aren't stranded. Under DISABLE_EMBEDDINGS there's nothing to
        # embed, so the in-memory drain is discarded and its durable receipt remains.
        if not disabled and drained:
            _replay_deferred_embeddings(drained)
            log.info("drained %d deferred write-embed batch(es)", len(drained))
    else:
        from . import embeddings

        log.info("preloading embedding model %s", embeddings.MODEL_NAME)
        bge_ok = _preload("model_bge", embeddings.get_model, lambda m: m.encode(["warm"]))
        # Drain the parked write-embed work REGARDLESS of preload outcome. This
        # drain used to be nested inside the success branch, so a failed preload
        # stranded every write deferred during the warm — mark_ready is the only
        # drainer, and it never ran. Now: on success mark_ready() sets the event
        # AND drains atomically; on FAILURE the component must stay not-ready (so
        # request paths keep their inline lazy-load + soft-degrade fallback for the
        # rest of the warm), but drain_deferred() still empties the queue so those
        # writes are replayed instead of lost.
        if bge_ok:
            log.info("embedding model ready")
            drained = readiness.mark_ready("embeddings")
        else:
            drained = readiness.drain_deferred("embeddings")
        _replay_deferred_embeddings(drained)
        if drained:
            log.info("drained %d deferred write-embed batch(es)", len(drained))

        if embeddings.ranking_enabled():
            log.info("preloading reranker %s", embeddings.RERANKER_NAME)
            if _preload(
                "model_reranker", embeddings.get_reranker, lambda m: m.predict([("warm", "warm")])
            ):
                log.info("reranker model ready")
                readiness.mark_ready("reranker")
        else:
            log.info("reranker disabled (EXOMEM_DISABLE_RANKING); skipping preload")
            readiness.mark_ready("reranker")

        if embeddings.clip_enabled():
            log.info("preloading CLIP model %s", embeddings.CLIP_MODEL_NAME)
            if _preload("model_clip", embeddings.get_clip_model, lambda m: m.encode(["warm"])):
                log.info("CLIP model ready")
                readiness.mark_ready("clip")

    if carried:
        log.info("warm complete: %s carried_from_standby=%s", durations, sorted(carried))
    else:
        log.info("warm complete: %s", durations)
    return durations


def _carry_forward_standby_readiness() -> frozenset[str]:
    """Re-mark the components a promoted standby already warmed. Returns them.

    `begin_warm` clears every readiness event, so a promoted worker that did
    nothing here would spend its second warm refusing the governed writes its
    predecessor's users are already sending: measured on the 0.85.0 cutover as
    ~30 s of `MUTATION_WARMING` behind a 1.6 s unavailable window, which moves
    an outage rather than removing one.

    Marked synchronously, before the warm thread starts and therefore before
    `start_background` returns, so no request can observe the window between the
    clear and the re-mark. Empty and a no-op on a cold start.
    """
    from . import readiness, service_standby

    carried = service_standby.carried_warm_components()
    for component in carried:
        readiness.mark_ready(component)
    if carried:
        log.info("carried warm components forward from the standby: %s", sorted(carried))
    return carried


def start_background(vault_root: Path) -> threading.Thread:
    """Run `warm_all` on a daemon thread; the transport serves meanwhile.

    `readiness.begin_warm()` fires BEFORE the thread starts so request paths
    already defer when this returns; `finish_warm()` runs in a finally so a
    crashed warm can never leave the process deferring forever.

    A promoted standby's carried components are re-marked in the same
    synchronous stretch, between the clear and the thread, because that clear is
    what made a promoted worker refuse writes for thirty seconds after a 1.6 s
    cutover.
    """
    global _WARM_THREAD
    from . import readiness

    readiness.begin_warm()
    _carry_forward_standby_readiness()

    def _run() -> None:
        try:
            warm_all(vault_root)
        except Exception:  # noqa: BLE001 — the warm thread must never die loudly
            log.warning("background warm-up crashed", exc_info=True)
        finally:
            readiness.finish_warm()

    thread = threading.Thread(target=_run, name="exomem-warm", daemon=True)
    _WARM_THREAD = thread
    try:
        thread.start()
    except Exception:  # noqa: BLE001 — a failed start must not defer forever
        readiness.finish_warm()
        raise
    return thread
