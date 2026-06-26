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

from . import find as find_module


log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STEMMER = None
_STEMMER_LOCK = threading.Lock()


def _get_stemmer():
    global _STEMMER
    if _STEMMER is None:
        with _STEMMER_LOCK:
            if _STEMMER is None:
                import snowballstemmer
                _STEMMER = snowballstemmer.stemmer("english")
    return _STEMMER


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
        self._cache: dict[tuple[Path, str], tuple[float, object, list[str]]] = {}
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

    def _build(
        self, vault_root: Path, scope: str
    ) -> tuple[float, object, list[str]]:
        """Walk the KB (or full vault), tokenize each file, build BM25Okapi.

        Returns (max_mtime, bm25, paths) where `paths` is parallel to the
        BM25 document index. Reuses cached per-doc tokens for unchanged files
        (see `_doc_tokens`), so only changed docs are re-tokenized.
        """
        # Lazy import — rank_bm25 isn't on the keyword-only hot path.
        from rank_bm25 import BM25Okapi

        if scope == "vault":
            from .vault import walk_vault_md
            walk = walk_vault_md(vault_root)
        else:
            kb = vault_root / "Knowledge Base"
            walk = find_module._walk_md(kb)

        self.last_tokenized = 0
        self.last_reused = 0
        paths: list[str] = []
        corpus: list[list[str]] = []
        max_mtime = 0.0
        for md in walk:
            page = find_module._CACHE.get(md, vault_root)
            if page is None:
                continue
            tokens = self._doc_tokens(md, page)
            if not tokens:
                continue
            paths.append(page.rel_path)
            corpus.append(tokens)
            if page.mtime > max_mtime:
                max_mtime = page.mtime
        if not corpus:
            # rank_bm25 chokes on empty corpora; return a sentinel.
            return max_mtime, None, []
        bm25 = BM25Okapi(corpus)
        return max_mtime, bm25, paths

    def search(
        self, vault_root: Path, query: str, k: int, *, scope: str = "kb"
    ) -> list[tuple[str, float]]:
        """Return top-k `(rel_path, bm25_score)` for `query`. Empty query → []."""
        if not query.strip():
            return []
        cache_key = (vault_root, scope)
        cached = self._cache.get(cache_key)
        current_max = _current_max_mtime(vault_root, scope)
        if cached is None or current_max > cached[0]:
            log.debug("bm25: rebuilding index for %s scope=%s", vault_root, scope)
            cached = self._build(vault_root, scope)
            self._cache[cache_key] = cached
        max_mtime, bm25, paths = cached
        if bm25 is None or not paths:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = bm25.get_scores(tokens)
        ranked = sorted(
            zip(paths, scores), key=lambda t: (-t[1], t[0])
        )[:k]
        # Drop zero-score hits — they aren't really matches.
        return [(p, float(s)) for p, s in ranked if s > 0]

    def clear(self) -> None:
        self._cache.clear()
        self._tokens.clear()
        self.last_tokenized = 0
        self.last_reused = 0


def _current_max_mtime(vault_root: Path, scope: str) -> float:
    """Walk the tree once to get the most-recent file mtime. Cheap at this scale."""
    if scope == "vault":
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)
    else:
        kb = vault_root / "Knowledge Base"
        if not kb.is_dir():
            return 0.0
        walk = find_module._walk_md(kb)
    m = 0.0
    for p in walk:
        try:
            t = p.stat().st_mtime
        except OSError:
            continue
        if t > m:
            m = t
    return m


_INDEX = BM25Index()


def search(
    vault_root: Path, query: str, k: int, *, scope: str = "kb"
) -> list[tuple[str, float]]:
    """Module-level convenience using the per-process singleton."""
    return _INDEX.search(vault_root, query, k, scope=scope)


def clear_cache() -> None:
    """Test hook: flush the singleton cache between tests."""
    _INDEX.clear()
