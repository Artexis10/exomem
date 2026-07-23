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
    """
    override = os.environ.get("EXOMEM_PRELOAD_MODELS")
    if override is not None and override.strip() != "":
        return override.strip().lower() not in {"0", "false", "no", "off"}
    return (mode_name or "normal") == "performance"


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
    from . import bm25, find

    durations: dict[str, float] = {}

    def _step(name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:  # noqa: BLE001 — warm-up must never break startup
            log.warning("warm-up step %s failed", name, exc_info=True)
        finally:
            durations[name] = round((time.perf_counter() - t0) * 1000.0, 1)

    def _warm_pages() -> None:
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return
        for p in find._walk_md(kb):
            find._CACHE.get(p, vault_root)

    _step("pages", _warm_pages)
    _step("bm25_kb", lambda: bm25.warm(vault_root, "kb"))
    _step("bm25_vault", lambda: bm25.warm(vault_root, "vault"))
    _step("resolver", lambda: find._get_query_resolver(vault_root))
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
    """Lexical caches, then model preloads, marking readiness as each lands.

    Order is the product contract (lexical-first): a `find` is useful the
    moment the BM25/page caches are hot, long before torch finishes loading.
    Each stage soft-fails; a failed model preload leaves its component
    not-ready (never marked), so requests defer for the rest of the warm and
    then return to inline lazy-load semantics. Never raises.
    """
    from . import mode, readiness

    mode_name = mode.resolve_mode()
    preload = model_preload_allowed(mode_name)
    durations: dict[str, float] = {}
    durations.update(
        warm_caches(
            vault_root,
            preload_models=preload,
            preload_cpu_caches=mode.preload_cpu_caches(),
        )
    )
    readiness.mark_ready("lexical")
    semantic_started = time.perf_counter()
    try:
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

    log.info("warm complete: %s", durations)
    return durations


def start_background(vault_root: Path) -> threading.Thread:
    """Run `warm_all` on a daemon thread; the transport serves meanwhile.

    `readiness.begin_warm()` fires BEFORE the thread starts so request paths
    already defer when this returns; `finish_warm()` runs in a finally so a
    crashed warm can never leave the process deferring forever.
    """
    global _WARM_THREAD
    from . import readiness

    readiness.begin_warm()

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
