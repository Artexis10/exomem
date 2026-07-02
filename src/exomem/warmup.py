"""Startup warm-up: lexical caches first, then models — off the boot path.

`warm_all` runs everything the first user-facing calls would otherwise pay
inline: the parsed-page cache, the BM25 corpora for BOTH scopes (auto-widen
runs a vault-scope BM25 on every kb query), the wikilink resolver, the
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

log = logging.getLogger(__name__)

_WARM_THREAD: threading.Thread | None = None


def warmup_enabled() -> bool:
    return not os.environ.get("EXOMEM_DISABLE_WARMUP")


def warm_caches(vault_root: Path) -> dict[str, float]:
    """Warm find's lexical/derived caches; returns per-step durations in ms
    (empty when disabled). Never raises."""
    if not warmup_enabled():
        log.info("cache warm-up disabled via EXOMEM_DISABLE_WARMUP")
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
        kb = vault_root / "Knowledge Base"
        if not kb.is_dir():
            return
        for p in find._walk_md(kb):
            find._CACHE.get(p, vault_root)

    _step("pages", _warm_pages)
    _step("bm25_kb", lambda: bm25.warm(vault_root, "kb"))
    _step("bm25_vault", lambda: bm25.warm(vault_root, "vault"))
    _step("resolver", lambda: find._get_query_resolver(vault_root))
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):

        def _warm_matrix() -> None:
            from . import embeddings

            embeddings.EmbeddingIndex(vault_root).all_vectors()

        def _warm_clip() -> None:
            from . import embeddings

            if embeddings.clip_enabled():
                embeddings.ClipIndex(vault_root).all_vectors()

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
    from . import readiness

    durations = warm_caches(vault_root)
    readiness.mark_ready("lexical")

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

    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        # Lexical-only install: there are no models to wait for, so mark the
        # model components ready immediately — otherwise finds during the
        # few-second lexical warm would carry a "warming" marker naming
        # models this install will never load.
        log.info("model preloads skipped (EXOMEM_DISABLE_EMBEDDINGS)")
        for component in ("embeddings", "reranker", "clip"):
            readiness.mark_ready(component)
    else:
        from . import embeddings

        log.info("preloading embedding model %s", embeddings.MODEL_NAME)
        if _model_step("model_bge", embeddings.get_model):
            log.info("embedding model ready")
            drained = readiness.mark_ready("embeddings")
            for item_vault, paths in drained:
                try:
                    embeddings.upsert_after_write(item_vault, list(paths))
                except Exception:  # noqa: BLE001 — drain is best-effort
                    log.warning("deferred embed drain failed", exc_info=True)
            if drained:
                log.info("drained %d deferred write-embed batch(es)", len(drained))

        log.info("preloading reranker %s", embeddings.RERANKER_NAME)
        if _model_step("model_reranker", embeddings.get_reranker):
            log.info("reranker model ready")
            readiness.mark_ready("reranker")

        if embeddings.clip_enabled():
            log.info("preloading CLIP model %s", embeddings.CLIP_MODEL_NAME)
            if _model_step("model_clip", embeddings.get_clip_model):
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
