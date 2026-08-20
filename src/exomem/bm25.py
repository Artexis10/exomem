"""BM25Okapi over compiled KB pages, with incremental per-process caches.

The assembled corpus is retained with a freshness checkpoint. A complete,
small change delta repairs only the changed/deleted paths; cold starts, policy
changes, incomplete histories, and large deltas fall back to a full walk. The
per-doc token cache remains keyed by path + mtime, mirroring
`find.FrontmatterCache`, so unchanged documents are not re-tokenized on either
path. `BM25Okapi` itself is still reconstructed from the retained token lists
whenever the corpus changes (`rank_bm25` has no incremental add/remove API),
but that in-memory global-stat step is cheap relative to walking, admitting,
and reading the vault.

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
from typing import Any, NamedTuple

from . import find as find_module
from . import freshness, recall_policy
from .kbdir import kb_dirname

log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STEMMER_LOCAL = threading.local()

# Above this fraction of the retained corpus, bounded per-path repair gives way
# to the existing full walk. The measurement supporting the value lives in the
# worker result for the change that introduced incremental corpus repair.
MAX_INCREMENTAL_REPAIR_FRACTION = 0.10


class _CorpusCacheEntry(NamedTuple):
    """One assembled corpus plus the provenance needed for safe delta repair.

    Keep the first three fields in the historical positional order: residency
    diagnostics intentionally read ``entry[2]`` without allocating projections.
    """

    cache_identity: tuple
    bm25: Any
    paths: list[str]
    tokens_by_path: dict[str, list[str]]
    checkpoint: freshness.RecallFreshnessCheckpoint
    policy_identity: tuple[str, str]


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
        self._cache: dict[tuple[Path, str], _CorpusCacheEntry] = {}
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

    @staticmethod
    def _derive_bm25(tokens_by_path: dict[str, list[str]]) -> tuple[Any, list[str]]:
        """Recompute corpus-global BM25 maths without touching the filesystem."""
        if not tokens_by_path:
            # rank_bm25 chokes on empty corpora; return a sentinel.
            return None, []
        from rank_bm25 import BM25Okapi

        paths = list(tokens_by_path)
        return BM25Okapi([tokens_by_path[path] for path in paths]), paths

    def _build(
        self, vault_root: Path, scope: str
    ) -> tuple[Any, list[str], dict[str, list[str]]]:
        """Walk the KB (or full vault), tokenize each file, build BM25Okapi.

        Returns the BM25 index, its parallel paths, and the retained per-path
        token corpus. Reuses cached per-doc tokens for unchanged files (see
        `_doc_tokens`), so only changed docs are re-tokenized.
        """
        walk = _recall_walk(vault_root, scope)

        self.last_tokenized = 0
        self.last_reused = 0
        tokens_by_path: dict[str, list[str]] = {}
        for md in walk:
            page = find_module._CACHE.get(md, vault_root)
            if page is None:
                continue
            tokens = self._doc_tokens(md, page)
            if not tokens:
                continue
            tokens_by_path[page.rel_path] = tokens
        bm25, paths = self._derive_bm25(tokens_by_path)
        return bm25, paths, tokens_by_path

    @staticmethod
    def _relative_path(vault_root: Path, path: str) -> str | None:
        """Return a delta path's vault spelling without resolving or stat'ing it."""
        try:
            return Path(path).relative_to(vault_root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _repair_limit(document_count: int) -> int:
        """Maximum delta size worth repairing for the retained corpus."""
        return max(1, int(document_count * MAX_INCREMENTAL_REPAIR_FRACTION))

    def _repair(
        self,
        vault_root: Path,
        scope: str,
        cached: _CorpusCacheEntry,
        target_freshness: tuple,
        policy_identity: tuple[str, str],
    ) -> _CorpusCacheEntry | None:
        """Repair one cached corpus, or return ``None`` when proof is absent."""
        if cached.policy_identity != policy_identity:
            return None

        delta = freshness.recall_delta_since(vault_root, scope, cached.checkpoint)
        delta_policy_identity = (
            delta.to.policy_version,
            delta.to.access_policy_fingerprint,
        )
        if (
            not delta.complete
            or delta.to.triple != target_freshness
            or delta_policy_identity != policy_identity
        ):
            return None

        changed_count = len(delta.changed) + len(delta.deleted)
        if changed_count > self._repair_limit(len(cached.tokens_by_path)):
            return None

        self.last_tokenized = 0
        self.last_reused = 0
        tokens_by_path = dict(cached.tokens_by_path)

        for raw_path in delta.deleted:
            rel_path = self._relative_path(vault_root, raw_path)
            if rel_path is None:
                return None
            tokens_by_path.pop(rel_path, None)

        for raw_path in delta.changed:
            path = Path(raw_path)
            rel_path = self._relative_path(vault_root, raw_path)
            if rel_path is None:
                return None
            if not recall_policy.is_recall_candidate(vault_root, path):
                tokens_by_path.pop(rel_path, None)
                continue
            page = find_module._CACHE.get(path, vault_root)
            if page is None:
                tokens_by_path.pop(rel_path, None)
                continue
            tokens = self._doc_tokens(path, page)
            if not tokens:
                tokens_by_path.pop(rel_path, None)
                tokens_by_path.pop(page.rel_path, None)
                continue
            if page.rel_path != rel_path:
                tokens_by_path.pop(rel_path, None)
            tokens_by_path[page.rel_path] = tokens

        repaired_bm25, repaired_paths = self._derive_bm25(tokens_by_path)
        return _CorpusCacheEntry(
            (*delta.to.triple, *policy_identity),
            repaired_bm25,
            repaired_paths,
            tokens_by_path,
            delta.to,
            policy_identity,
        )

    def _fresh_corpus(
        self, vault_root: Path, scope: str, freshness_key: tuple | None
    ) -> tuple[Any, list[str]]:
        """Return cached corpus state, repaired or rebuilt when identity moves.

        The key is find's digest-strength `_walk_freshness_key` triple — the
        historical `current_max > cached_max` comparison missed deletes,
        renames, and replacements carrying an older mtime. Callers inside a
        `find` request pass the request snapshot's key; out-of-request callers
        compute it from the live registry. A small complete delta repairs only
        its paths. Every unprovable case retains the full-build behavior.
        """
        if freshness_key is None:
            freshness_key = corpus_key(vault_root, scope)
        policy_identity = recall_policy.recall_policy_identity(vault_root)
        cache_identity = (*freshness_key, *policy_identity)
        cache_key = (vault_root, scope)
        cached = self._cache.get(cache_key)
        if cached is None or cached.cache_identity != cache_identity:
            with self._build_lock:
                # Double-check: a concurrent builder may have stored a fresh
                # corpus while this thread waited on the lock.
                cached = self._cache.get(cache_key)
                if cached is None or cached.cache_identity != cache_identity:
                    repaired = None
                    if cached is not None:
                        repaired = self._repair(
                            vault_root,
                            scope,
                            cached,
                            freshness_key,
                            policy_identity,
                        )
                    if repaired is not None:
                        log.debug(
                            "bm25: repaired index for %s scope=%s", vault_root, scope
                        )
                        cached = repaired
                    else:
                        log.debug(
                            "bm25: rebuilding index for %s scope=%s", vault_root, scope
                        )
                        checkpoint = freshness.recall_checkpoint(vault_root, scope)
                        build_policy_identity = (
                            checkpoint.policy_version,
                            checkpoint.access_policy_fingerprint,
                        )
                        bm25, paths, tokens_by_path = self._build(vault_root, scope)
                        cached = _CorpusCacheEntry(
                            (*checkpoint.triple, *build_policy_identity),
                            bm25,
                            paths,
                            tokens_by_path,
                            checkpoint,
                            build_policy_identity,
                        )
                    self._cache[cache_key] = cached
        return cached.bm25, cached.paths

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
                if (allowed_paths is None or path in allowed_paths)
                and bool(
                    set(tokens)
                    & set(self._tokens[vault_root / path][1])
                )
            ),
            key=lambda item: (-item[1], item[0]),
        )[:k]
        # Rank-BM25's epsilon fallback can make every matching score
        # non-positive in a tiny corpus. Token overlap, not score sign, is the
        # relevance proof; otherwise a valid lone structured manifest vanishes.
        return [(p, float(s)) for p, s in ranked]

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
    """Projected three-field recall triple for lexical sidecars."""
    return freshness.recall_triple(vault_root, scope)


def _recall_walk(vault_root: Path, scope: str):
    if scope == "vault":
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)
    else:
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return ()
        walk = find_module._walk_md(kb)
    return recall_policy.iter_recall_markdown(vault_root, walk)


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
