"""Startup cache warm-up (OpenSpec: reduce-find-per-query-overhead).

`build_server` already pays ~30s of model preloads so the FIRST user-facing
call is fast; this module extends the same trade to the lexical/derived
caches the first hybrid `find` would otherwise build inline: the parsed-page
cache, the BM25 corpora for BOTH scopes (auto-widen runs a vault-scope BM25
on every kb query), the wikilink resolver, and the embedding/CLIP matrices
when enabled.

Synchronous and best-effort by design: a background thread would be the
first cross-thread mutation of the BM25/page-cache singletons, and that
concurrency question isn't worth ~1-3s of startup. Every step soft-fails and
records its duration; `KB_MCP_DISABLE_WARMUP` skips the whole thing. No
user-facing surface change — behavior is identical, only the first-query
cliff moves to startup.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def warmup_enabled() -> bool:
    return not os.environ.get("KB_MCP_DISABLE_WARMUP")


def warm_caches(vault_root: Path) -> dict[str, float]:
    """Warm find's caches; returns per-step durations in ms (empty when
    disabled). Never raises."""
    if not warmup_enabled():
        log.info("cache warm-up disabled via KB_MCP_DISABLE_WARMUP")
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
    if not os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):

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
