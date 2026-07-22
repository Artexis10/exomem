"""BM25Okapi over compiled KB pages, with mtime-based per-process caches.

On every `search()` call we scan the tree once for the max observed mtime and
rebuild the index if it advanced. The rebuild is *incremental at the document
level*: a per-doc token cache (keyed by path + mtime, mirroring
`find.FrontmatterCache`) means only the documents that actually changed get
re-tokenized. So one large doc, a big corpus, or a write-heavy session no
longer forces an O(corpus) Snowball re-tokenize on the next `find` — which was
the failure behind the "uncapped large doc poisoned find" incident (the 512 KB
extract cap is the complementary, orthogonal fix). The `BM25Okapi` object
itself is still reconstructed from the cached token lists each rebuild
(`rank_bm25` has no incremental add/remove API), but that step is cheap
relative to the stemming it now avoids.

Tokens are stemmed with Snowball (English) so morphologically related
words score together — "regulation" matches a page with "regulator",
"compounding" matches "compound". The same stemmer is exposed to find.py
for its stem-aware all-tokens-present gate.
"""

from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import find as find_module
from .kbdir import kb_dirname

log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STEMMER_LOCAL = threading.local()


def _get_stemmer():
    stemmer = getattr(_STEMMER_LOCAL, "stemmer", None)
    if stemmer is None:
        import snowballstemmer

        stemmer = snowballstemmer.stemmer("english")
        _STEMMER_LOCAL.stemmer = stemmer
    return stemmer


@lru_cache(maxsize=16384)
def stem_word(word: str) -> str:
    """Memoized single-word stem. Tokens repeat across documents at scale."""
    return _get_stemmer().stemWord(word)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on word chars, Snowball-stem each token."""
    return [stem_word(w) for w in _TOKEN_RE.findall(text.lower())]


# Back-compat alias for callers that still import _tokenize.
_tokenize = tokenize


class BM25Index:
    """Per-process BM25 corpus over KB markdown files.

    Lazy: nothing happens until `search()` is called. Caches the built
    index keyed by (vault_root, max_mtime, scope). Rebuilds when the
    vault has any file newer than the cached max mtime.
    """

    def __init__(self) -> None:
        # (vault_root, scope) -> (freshness key triple, bm25, paths)
        self._cache: dict[tuple[Path, str], tuple[tuple, Any, list[str]]] = {}
        # Per-doc token cache, shared across scopes (a file's tokens don't depend
        # on scope; KB ⊆ vault). Mirrors find.FrontmatterCache's mtime
        # invalidation: a doc is Snowball-tokenized once and reused until its
        # mtime advances, so a rebuild only re-stems the docs that changed.
        # Stale entries for deleted files linger harmlessly — the corpus is
        # assembled only from currently-walked paths; clear() flushes them.
        self._tokens: dict[Path, tuple[float, list[str]]] = {}
        # Diagnostics for the most recent _build(): how many docs were actually
        # (re)tokenized vs reused from cache. Lets tests assert incrementality
        # without timing the wall clock.
        self.last_tokenized: int = 0
        self.last_reused: int = 0
        # Serializes corpus builds: the background warm thread and a racing
        # request must produce ONE build (the loser waits, then reuses).
        self._build_lock = threading.Lock()

    def _doc_tokens(self, path: Path, page) -> list[str]:
        """Tokens for `page`, reusing the cache while the file's mtime is unchanged."""
        cached = self._tokens.get(path)
        if cached is not None and cached[0] == page.mtime:
            self.last_reused += 1
            return cached[1]
        tokens = _tokenize(page.title + " " + page.body)
        self._tokens[path] = (page.mtime, tokens)
        self.last_tokenized += 1
        return tokens

    def _build(self, vault_root: Path, scope: str) -> tuple[Any, list[str]]:
        """Walk the KB (or full vault), tokenize each file, build BM25Okapi.

        Returns (bm25, paths) where `paths` is parallel to the BM25 document
        index. Reuses cached per-doc tokens for unchanged files (see
        `_doc_tokens`), so only changed docs are re-tokenized.
        """
        # Lazy import — rank_bm25 isn't on the keyword-only hot path.
        from rank_bm25 import BM25Okapi

        if scope == "vault":
            from .vault import walk_vault_md

            walk = walk_vault_md(vault_root)
        else:
            kb = vault_root / kb_dirname()
            walk = find_module._walk_md(kb)

        self.last_tokenized = 0
        self.last_reused = 0
        paths: list[str] = []
        corpus: list[list[str]] = []
        for md in walk:
            page = find_module._CACHE.get(md, vault_root)
            if page is None:
                continue
            tokens = self._doc_tokens(md, page)
            if not tokens:
                continue
            paths.append(page.rel_path)
            corpus.append(tokens)
        if not corpus:
            # rank_bm25 chokes on empty corpora; return a sentinel.
            return None, []
        bm25 = BM25Okapi(corpus)
        return bm25, paths

    def _fresh_corpus(
        self, vault_root: Path, scope: str, freshness: tuple | None
    ) -> tuple[Any, list[str]]:
        """The cached (bm25, paths) pair, rebuilt when the freshness key moved.

        The key is find's digest-strength `_walk_freshness_key` triple — the
        historical `current_max > cached_max` comparison missed deletes,
        renames, and replacements carrying an older mtime, all of which now
        rebuild correctly. Callers inside a `find` request pass the request
        snapshot's key so this never re-walks; `freshness=None` computes it
        here for out-of-request callers.
        """
        if freshness is None:
            freshness = corpus_key(vault_root, scope)
        cache_key = (vault_root, scope)
        cached = self._cache.get(cache_key)
        if cached is None or cached[0] != freshness:
            with self._build_lock:
                # Double-check: a concurrent builder may have stored a fresh
                # corpus while this thread waited on the lock.
                cached = self._cache.get(cache_key)
                if cached is None or cached[0] != freshness:
                    log.debug("bm25: rebuilding index for %s scope=%s", vault_root, scope)
                    bm25, paths = self._build(vault_root, scope)
                    cached = (freshness, bm25, paths)
                    self._cache[cache_key] = cached
        return cached[1], cached[2]

    def search(
        self,
        vault_root: Path,
        query: str,
        k: int,
        *,
        scope: str = "kb",
        freshness: tuple | None = None,
        allowed_paths: set[str] | None = None,
        repair: bool = True,
    ) -> list[tuple[str, float]]:
        """Return top-k `(rel_path, bm25_score)` for `query`. Empty query → [].

        Backend ladder: the FTS5 lexical sidecar serves the lane when
        available (posting-list cost instead of scoring all N docs); any
        unavailability — kill switch, FTS5 absent, sidecar failure — falls
        through to the in-process BM25Okapi rung below, which remains the
        reference implementation and the `EXOMEM_LEXICAL_BACKEND=python`
        target. Interface identical either way.
        """
        if not query.strip():
            return []
        from . import lexstore

        indexed = lexstore.search_bm25(
            vault_root,
            query,
            k,
            scope=scope,
            freshness=freshness,
            allowed_paths=allowed_paths,
            repair=repair,
        )
        if indexed is not None:
            return indexed
        bm25, paths = self._fresh_corpus(vault_root, scope, freshness)
        if bm25 is None or not paths:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = bm25.get_scores(tokens)
        ranked = sorted(
            (
                (path, score)
                for path, score in zip(paths, scores, strict=True)
                if allowed_paths is None or path in allowed_paths
            ),
            key=lambda item: (-item[1], item[0]),
        )[:k]
        # Drop zero-score hits — they aren't really matches.
        return [(p, float(s)) for p, s in ranked if s > 0]

    def warm(self, vault_root: Path, scope: str = "kb") -> None:
        """Build (or freshness-check) whichever backend serves this lane —
        the startup warm-up hook, so the first hybrid find doesn't pay the
        first-build cliff (sidecar sync/population under FTS5; the corpus
        stemming build on the in-process rung)."""
        from . import lexstore

        if lexstore.search_bm25(vault_root, "warm", 1, scope=scope) is not None:
            # FTS5 serves: the probe query ran the sync check and faulted the
            # index in. The rank-bm25 corpus stays cold on purpose — not
            # holding N token lists resident is part of the backend's win;
            # a mid-process FTS5 retirement pays one rebuild, lazily.
            return
        self._fresh_corpus(vault_root, scope, None)

    def unload_cache(self) -> bool:
        """Drop rebuildable in-process BM25 corpus/token caches."""
        with self._build_lock:
            loaded = bool(self._cache or self._tokens)
            self._cache.clear()
            self._tokens.clear()
            self.last_tokenized = 0
            self.last_reused = 0
            return loaded

    def cache_status(self) -> dict:
        """No-allocation residency status for the Python BM25 rung."""
        with self._build_lock:
            doc_count = sum(len(entry[2]) for entry in self._cache.values())
            token_count = sum(len(tokens) for _mtime, tokens in self._tokens.values())
            return {
                "loaded": bool(self._cache or self._tokens),
                "corpora": len(self._cache),
                "documents": doc_count,
                "tokenized_documents": len(self._tokens),
                "tokens": token_count,
            }

    def clear(self) -> None:
        self.unload_cache()


def corpus_key(vault_root: Path, scope: str) -> tuple:
    """Digest-strength corpus freshness key for a scope (one stat walk)."""
    if scope == "vault":
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)
    else:
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return (0, 0, "")
        walk = find_module._walk_md(kb)
    return find_module._walk_freshness_key(walk)


_INDEX = BM25Index()


def search(
    vault_root: Path,
    query: str,
    k: int,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
    allowed_paths: set[str] | None = None,
    repair: bool = True,
) -> list[tuple[str, float]]:
    """Module-level convenience using the per-process singleton."""
    return _INDEX.search(
        vault_root,
        query,
        k,
        scope=scope,
        freshness=freshness,
        allowed_paths=allowed_paths,
        repair=repair,
    )


def warm(vault_root: Path, scope: str = "kb") -> None:
    """Module-level warm-up hook using the per-process singleton."""
    _INDEX.warm(vault_root, scope)


def unload_cache() -> bool:
    """Evict the singleton BM25 corpus/token cache without touching vault files."""
    return _INDEX.unload_cache()


def cache_status() -> dict:
    """No-allocation residency status for the singleton BM25 cache."""
    return _INDEX.cache_status()


def clear_cache() -> None:
    """Test hook: flush the singleton cache between tests."""
    _INDEX.clear()
