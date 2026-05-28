"""Read-only search across the Knowledge Base.

Scans every `.md` under `Knowledge Base/`, parses YAML frontmatter, filters by
structured fields, then does case-insensitive substring matching on
title + body. Hugo's vault is hundreds of pages — full-scan is fast enough.

Cached in-process between calls: keyed by file path, invalidated by mtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


log = logging.getLogger(__name__)

EXCLUDED_DIR_NAMES = frozenset({"_Schema", "_attachments", "_archive", "_trash"})
# Navigation files — auto-generated summaries / activity logs. Their bodies
# mention every recently-written page, so they false-positive on hybrid
# queries that touch any term recently introduced into the KB. Excluded
# from search results regardless of mode.
_NAVIGATION_BASENAMES = frozenset({"index.md", "log.md"})
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)

EXCERPT_RADIUS = 100  # chars on each side of the match
EXCERPT_MAX_LEN = 220


@dataclass
class ParsedPage:
    path: Path  # absolute
    rel_path: str  # vault-relative, e.g. "Knowledge Base/Notes/Insights/foo.md"
    frontmatter: dict[str, Any]
    body: str
    title: str
    mtime: float

    @property
    def page_type(self) -> str | None:
        t = self.frontmatter.get("type")
        return str(t) if t else None

    @property
    def scope(self) -> str | None:
        """Per-type "scope" field as defined in the plan.

        - research-note → project
        - pattern / insight / failure → project (singular) or projects (joined)
        - experiment → domain
        - production-log → medium
        - entity → entity_type
        - source → source_type
        Fallback: project / projects / domain / medium / entity_type in that order.
        """
        fm = self.frontmatter
        t = self.page_type

        def _project_or_projects() -> str | None:
            if (proj := fm.get("project")):
                return str(proj)
            if (projects := fm.get("projects")):
                if isinstance(projects, list) and projects:
                    return ",".join(str(p) for p in projects)
                return str(projects)
            return None

        if t == "production-log":
            return str(fm["medium"]) if fm.get("medium") else None
        if t == "experiment":
            return str(fm["domain"]) if fm.get("domain") else None
        if t == "entity":
            return str(fm["entity_type"]) if fm.get("entity_type") else None
        if t == "source":
            return str(fm["source_type"]) if fm.get("source_type") else None
        if t in ("research-note", "pattern", "insight", "failure"):
            return _project_or_projects()

        # Unknown type: fall back across all candidates
        return (
            _project_or_projects()
            or (str(fm["domain"]) if fm.get("domain") else None)
            or (str(fm["medium"]) if fm.get("medium") else None)
            or (str(fm["entity_type"]) if fm.get("entity_type") else None)
        )

    @property
    def updated(self) -> str:
        u = self.frontmatter.get("updated") or self.frontmatter.get("captured") or ""
        return str(u)

    @property
    def tags(self) -> list[str]:
        t = self.frontmatter.get("tags") or []
        return [str(x).lower() for x in t] if isinstance(t, list) else []


@dataclass
class Hit:
    path: str
    type: str | None
    scope: str | None
    title: str
    updated: str
    excerpt: str
    # Per-mode ranking signals — populated by hybrid/vector modes only.
    # `None` means the ranker did not surface this path in its top-K. The
    # presence of these fields lets a caller introspect WHY a hit ranked
    # (vector_rank=1 vs bm25_rank=1 vs graph_hop=True) without re-running
    # the query in each mode. Always omitted from as_dict() when empty so
    # keyword-mode callers don't see noise.
    bm25_rank: int | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    graph_hop: bool = False
    graph_in_degree: int = 0
    rerank_score: float | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
            "excerpt": self.excerpt,
        }
        signals: dict = {}
        if self.bm25_rank is not None:
            signals["bm25_rank"] = self.bm25_rank
        if self.vector_rank is not None:
            signals["vector_rank"] = self.vector_rank
        if self.vector_score is not None:
            signals["vector_score"] = round(self.vector_score, 4)
        if self.graph_hop:
            signals["graph_hop"] = True
        if self.graph_in_degree:
            signals["graph_in_degree"] = self.graph_in_degree
        if self.rerank_score is not None:
            signals["rerank_score"] = round(self.rerank_score, 4)
        if signals:
            out["signals"] = signals
        return out


@dataclass
class FrontmatterCache:
    """Per-process cache of parsed pages, invalidated by mtime."""

    entries: dict[Path, ParsedPage] = field(default_factory=dict)

    def get(self, path: Path, vault_root: Path) -> ParsedPage | None:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self.entries.pop(path, None)
            return None
        cached = self.entries.get(path)
        if cached and cached.mtime == mtime:
            return cached
        parsed = _parse_page(path, mtime, vault_root)
        if parsed is not None:
            self.entries[path] = parsed
        return parsed


_CACHE = FrontmatterCache()


def find(
    vault_root: Path,
    *,
    query: str,
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    graph: bool = True,
    rerank: bool = False,
    prefer_compiled: bool = True,
) -> list[Hit]:
    """Search the vault. Returns up to `limit` hits.

    `scope` controls the walk root:
    - "kb" (default): only `Knowledge Base/`. Compiled material + sources.
    - "vault": full vault, including curated trees (`Cognitive Core/`,
      `Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`,
      `Systems Thinking/`). Use when you need to discover content
      outside the KB. Existing filters still apply — curated-tree pages
      typically lack structured frontmatter so `types`/`projects`/`tags`
      filters won't match many of them; free-text queries work fine.

    `mode` controls the ranker:
    - "hybrid" (default): BM25 + local vector embeddings fused via RRF.
      Best recall on natural-language queries. Empty query falls back to
      keyword behavior (filtered most-recent). Embedding sidecar is
      KB-scoped; with `scope="vault"`, vector results cover KB only
      while BM25 covers the full vault.
    - "keyword": case-insensitive substring matching across title + body,
      sorted most-recently-updated first. The original behavior, preserved
      for backward compatibility.
    - "vector": vector embeddings only, no BM25. Testing aid for
      isolating semantic recall.

    `graph`: when True (default for hybrid/vector), the outbound wikilinks
    of top-ranked BM25/vector candidates contribute a third ranking that
    surfaces 1-hop neighbours of strong matches. Set False for pure
    BM25+vector hybrid without graph expansion.

    `rerank`: when True (off by default; opt-in due to model-load cost),
    runs the top `3 * limit` fused candidates through BAAI/bge-reranker-base
    (a CrossEncoder) and re-sorts by reranker score. Recovers ordering
    quality on ambiguous queries — the LLM-Wiki cases where vector floats
    a topically-off doc to the top. ~50ms / candidate on Blackwell.

    `prefer_compiled`: when True (default), applies a small multiplicative
    boost to fused/rerank scores for COMPILED page types (insight, pattern,
    failure, research-note, entity) and a small penalty for raw `source`
    pages. Reflects the KB's epistemic hierarchy — compiled distillations
    are the intentional output, sources are inputs. Set False to retrieve
    raw source discussion verbatim (e.g. "what did I capture from Dr. X").
    """
    if scope not in ("kb", "vault"):
        raise ValueError(
            f"find: scope must be 'kb' or 'vault', got {scope!r}"
        )
    if mode not in ("hybrid", "keyword", "vector"):
        raise ValueError(
            f"find: mode must be 'hybrid', 'keyword', or 'vector', got {mode!r}"
        )
    if limit < 1:
        limit = 1
    limit = min(limit, 100)
    query_norm = (query or "").lower().strip()

    # Empty queries always degrade to keyword behavior — there's no signal
    # to embed or score with, just "give me recent stuff that matches the
    # structured filters."
    if mode == "keyword" or not query_norm:
        return _find_keyword(
            vault_root,
            query_norm=query_norm,
            types=types, projects=projects, tags=tags,
            limit=limit, scope=scope,
        )
    return _find_semantic(
        vault_root,
        query=query, query_norm=query_norm,
        types=types, projects=projects, tags=tags,
        limit=limit, scope=scope, mode=mode, graph=graph, rerank=rerank,
        prefer_compiled=prefer_compiled,
    )


def _find_keyword(
    vault_root: Path,
    *,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    limit: int,
    scope: str,
) -> list[Hit]:
    """Original keyword-mode find. Preserved for backward compat."""
    if scope == "kb":
        kb = vault_root / "Knowledge Base"
        if not kb.is_dir():
            log.error("KB directory missing: %s", kb)
            return []
        walk = _walk_md(kb)
    else:
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)

    hits: list[tuple[str, Hit]] = []
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        if not _passes_filters(page, types=types, projects=projects, tags=tags):
            continue
        excerpt = _make_excerpt(page, query_norm)
        if query_norm and excerpt is None:
            continue
        hits.append(
            (
                page.updated or "0000-00-00",
                Hit(
                    path=page.rel_path,
                    type=page.page_type,
                    scope=page.scope,
                    title=page.title,
                    updated=page.updated,
                    excerpt=excerpt or "",
                ),
            )
        )

    hits.sort(key=lambda t: (t[0], t[1].path), reverse=True)
    return [h for _, h in hits[:limit]]


def _find_semantic(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    limit: int,
    scope: str,
    mode: str,
    graph: bool = True,
    rerank: bool = False,
    prefer_compiled: bool = True,
) -> list[Hit]:
    """Hybrid (BM25+vector) or vector-only mode."""
    # Lazy imports — keep keyword-mode users out of the torch import path.
    from . import bm25, embeddings, fusion

    # Pull more than we need so post-filter losses don't starve the result.
    candidate_k = max(limit * 5, 50)

    # ---- Vector contribution ----
    vector_ranking: list[str] = []
    chunk_text_by_path: dict[str, str] = {}
    vector_score_by_path: dict[str, float] = {}
    try:
        idx = embeddings.EmbeddingIndex(vault_root)
        query_vec = embeddings.embed_texts([query], is_query=True)[0]
        chunk_hits = idx.search(query_vec, k=candidate_k * 3)  # over-fetch chunks
        # Collapse chunks → file-level: keep the best-scoring chunk per file.
        best_per_file: dict[str, tuple[float, str]] = {}
        for fp, _idx, ctext, score in chunk_hits:
            existing = best_per_file.get(fp)
            if existing is None or score > existing[0]:
                best_per_file[fp] = (score, ctext)
        vector_ranking = sorted(
            best_per_file.keys(), key=lambda p: -best_per_file[p][0]
        )[:candidate_k]
        chunk_text_by_path = {p: best_per_file[p][1] for p in vector_ranking}
        vector_score_by_path = {p: best_per_file[p][0] for p in vector_ranking}
    except ImportError as e:
        log.warning(
            "vector search unavailable (%s); falling back to BM25-only ranking",
            e,
        )
    except Exception as e:
        log.warning("vector search failed: %s; falling back to BM25-only", e)

    bm25_ranking: list[str] = []
    if mode == "vector":
        rankings = [vector_ranking] if vector_ranking else []
    else:
        # ---- BM25 contribution ----
        try:
            bm25_hits = bm25.search(vault_root, query, k=candidate_k, scope=scope)
            bm25_ranking = [p for p, _ in bm25_hits]
        except ImportError as e:
            log.warning("BM25 unavailable (%s); using vector-only", e)
        except Exception as e:
            log.warning("BM25 search failed: %s; using vector-only", e)
        rankings = [r for r in (vector_ranking, bm25_ranking) if r]

    # ---- Graph expansion: 1-hop outbound wikilinks of STRONG candidates ----
    # Strong = ranked by vector (semantically gated by construction), or
    # BM25-ranked AND passing the stem-aware all-tokens-present check. Seeding
    # from raw BM25 alone leaks neighbours of weak matches into results
    # (e.g. queries like "skip-marker-abc" where every token is common).
    graph_ranking: list[str] = []
    graph_in_degree_by_path: dict[str, int] = {}
    if graph:
        primary_set: set[str] = set(vector_ranking) | set(bm25_ranking)
        vector_set: set[str] = set(vector_ranking)
        graph_seeds: list[str] = []
        seen_seed: set[str] = set()
        for r in (vector_ranking, bm25_ranking):
            for p in r[:20]:  # cap fanout
                if p in seen_seed:
                    continue
                seen_seed.add(p)
                if p in vector_set:
                    graph_seeds.append(p)
                    continue
                # BM25-only seed: gate via stem-aware tokens-present.
                page = _CACHE.get(vault_root / p, vault_root)
                if page is None:
                    continue
                if (
                    _make_excerpt(page, query_norm) is not None
                    or _stem_tokens_present(page, query_norm)
                ):
                    graph_seeds.append(p)
        seen_target: set[str] = set()
        for seed_rel in graph_seeds:
            page = _CACHE.get(vault_root / seed_rel, vault_root)
            if page is None:
                continue
            for target_rel in _outbound_wikilink_paths(page, vault_root):
                # Count in-degree for ALL targets — primary-ranked hubs benefit
                # too. graph_ranking still only carries non-primary targets so
                # RRF doesn't double-count them.
                graph_in_degree_by_path[target_rel] = (
                    graph_in_degree_by_path.get(target_rel, 0) + 1
                )
                if target_rel in primary_set or target_rel in seen_target:
                    continue
                seen_target.add(target_rel)
                graph_ranking.append(target_rel)
        if graph_ranking:
            rankings.append(graph_ranking)

    # Pre-compute per-mode rank lookups so we can tag each Hit's signals.
    vector_rank_by_path = {p: i + 1 for i, p in enumerate(vector_ranking)}
    bm25_rank_by_path = {p: i + 1 for i, p in enumerate(bm25_ranking)}
    graph_set = set(graph_ranking)

    if not rankings:
        # Both rankers failed or produced nothing. Degrade to keyword.
        log.info("semantic search produced no candidates; falling back to keyword")
        return _find_keyword(
            vault_root,
            query_norm=query_norm,
            types=types, projects=projects, tags=tags,
            limit=limit, scope=scope,
        )

    fused = fusion.reciprocal_rank_fusion(rankings, k=60)
    # Apply type-weight boost before iterating fused candidates — affects the
    # iteration order for non-rerank flows. For rerank, the boost is also
    # applied to rerank_score below so it survives the final sort.
    if prefer_compiled:
        fused = _apply_type_boost(fused, vault_root)
    vector_paths: set[str] = set(vector_ranking)

    # Resolve fused paths back to ParsedPage, filter, build hits in fused order.
    # BM25-only candidates must still satisfy the keyword all-tokens-present
    # gate — without it, BM25's word-level tokenizer surfaces files that share
    # any single token with the query (false positives). Vector-ranked
    # candidates skip that gate by design: surfacing semantically-similar
    # files that don't contain the literal tokens is the whole point.
    # When reranking, we over-fetch then trim post-rerank.
    target_n = limit * 3 if rerank else limit
    hits: list[Hit] = []
    seen: set[str] = set()
    for rel_path, _score in fused:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        abs_path = vault_root / rel_path
        if abs_path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(abs_path, vault_root)
        if page is None:
            continue
        if not _passes_filters(page, types=types, projects=projects, tags=tags):
            continue
        keyword_excerpt = _make_excerpt(page, query_norm)
        if rel_path not in vector_paths and rel_path not in graph_set and keyword_excerpt is None:
            # No literal match, not a graph hop, not vector-ranked. Try stem
            # match before dropping — recovers morphology ("regulation"
            # matching a "regulator" page).
            if not _stem_tokens_present(page, query_norm):
                continue
            keyword_excerpt = _stem_anchored_excerpt(page, query_norm)
        elif rel_path in graph_set and keyword_excerpt is None:
            # Graph-hop neighbour: no all-tokens-present requirement. The
            # rationale for surfacing is connectivity to a strong match,
            # not lexical overlap with the query. Use leading body snippet.
            body = page.body.strip()
            keyword_excerpt = _collapse(body[:EXCERPT_MAX_LEN]) if body else ""
        chunk = chunk_text_by_path.get(rel_path)
        excerpt = _semantic_excerpt(page, query_norm, chunk, keyword_excerpt)
        is_graph_only = (
            rel_path in graph_set
            and rel_path not in vector_rank_by_path
            and rel_path not in bm25_rank_by_path
        )
        hits.append(Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            bm25_rank=bm25_rank_by_path.get(rel_path),
            vector_rank=vector_rank_by_path.get(rel_path),
            vector_score=vector_score_by_path.get(rel_path),
            graph_hop=is_graph_only,
            graph_in_degree=graph_in_degree_by_path.get(rel_path, 0),
        ))
        if len(hits) >= target_n:
            break

    if rerank and hits:
        try:
            from . import embeddings as emb
            # Best passage for each hit: the matched chunk when we have one,
            # else the leading body slice.
            passages: list[str] = []
            for h in hits:
                ctext = chunk_text_by_path.get(h.path)
                if ctext:
                    passages.append(ctext)
                else:
                    abs_p = vault_root / h.path
                    pg = _CACHE.get(abs_p, vault_root)
                    body = (pg.body if pg else "") or h.excerpt
                    passages.append(body[:1500])  # CrossEncoder caps at 512 tokens
            scores = emb.rerank_pairs(query, passages)
            for h, s in zip(hits, scores):
                h.rerank_score = float(s)
            # Re-apply the type boost to rerank scores so prefer_compiled
            # survives the post-rerank sort. This rescues compiled material
            # that bge-reranker-base demotes — see Hugo's "thoughts on..."
            # query case where the reranker preferred raw Source discussion
            # over compiled Insights.
            if prefer_compiled:
                for h in hits:
                    if h.rerank_score is not None:
                        h.rerank_score *= _type_multiplier(h.type)
            hits.sort(key=lambda h: -(h.rerank_score if h.rerank_score is not None else float("-inf")))
        except ImportError as e:
            log.warning("rerank requested but reranker unavailable: %s", e)
        except Exception as e:
            log.warning("rerank failed: %s; returning fused order", e)

    return hits[:limit]


# KB epistemic hierarchy: compiled distillations are the intentional output,
# raw sources are inputs. Surfaced via prefer_compiled=True post-RRF boost.
# Multipliers are small — designed as tie-breakers between similar fused
# scores, not as dominators. Tune in one place if needed.
_COMPILED_TYPES = frozenset(
    {"insight", "pattern", "failure", "research-note", "entity"}
)
_SOURCE_TYPES = frozenset({"source"})
_COMPILED_BOOST = 1.15
_SOURCE_PENALTY = 0.85


def _type_multiplier(page_type: str | None) -> float:
    if page_type in _COMPILED_TYPES:
        return _COMPILED_BOOST
    if page_type in _SOURCE_TYPES:
        return _SOURCE_PENALTY
    return 1.0


def _apply_type_boost(
    fused: list[tuple[str, float]], vault_root: Path
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` pairs after applying per-type multipliers.

    Paths whose ParsedPage can't be loaded keep their original score (no
    multiplier known). Stable sort by adjusted score desc, path asc.
    """
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = _CACHE.get(vault_root / path, vault_root)
        mult = _type_multiplier(page.page_type if page else None)
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def _outbound_wikilink_paths(page: ParsedPage, vault_root: Path) -> list[str]:
    """Vault-relative POSIX paths (no .md) that this page's body links to.

    Skips matches inside fenced code blocks and inline code (delegates to
    vault.find_body_wikilinks). Targets are normalised through
    `normalize_wikilink` so bare / KB-stripped / aliased forms all resolve to
    the same canonical path. Unresolvable targets and folder-hub links
    (trailing `/`) are dropped. `#anchor` is stripped — anchors are intra-
    page jumps, not separate files.
    """
    from .vault import (
        WikilinkResolver,
        find_body_wikilinks,
        normalize_wikilink,
    )
    # Build the resolver once per query (or once across the whole find call —
    # see _resolver_cache below).
    resolver = _get_query_resolver(vault_root)
    out: list[str] = []
    seen: set[str] = set()
    for m in find_body_wikilinks(page.body):
        inner = m.group(0)[2:-2]
        target = inner.split("|", 1)[0].strip()
        if not target or target.endswith("/"):
            continue
        try:
            canonical, warning = normalize_wikilink(
                target, vault_root, resolver=resolver, strict=False
            )
        except Exception:
            continue
        if warning:
            continue  # unresolved — don't pollute the ranking
        rel = canonical.split("#", 1)[0].strip()
        if not rel:
            continue
        rel_with_md = rel if rel.endswith(".md") else rel + ".md"
        # Sanity: only walk into the KB itself for graph expansion; curated
        # trees are intentional out-of-graph references.
        if not rel_with_md.startswith("Knowledge Base/"):
            continue
        if rel_with_md in seen:
            continue
        seen.add(rel_with_md)
        out.append(rel_with_md)
    return out


_RESOLVER_CACHE: dict[Path, tuple[float, "object"]] = {}


def _get_query_resolver(vault_root: Path):
    """Per-process WikilinkResolver cache, invalidated when the vault changes."""
    from .vault import WikilinkResolver, walk_vault_md
    # Cheap freshness key: hash a count + most-recent mtime. WikilinkResolver
    # build walks the whole vault, so we want to reuse it across queries.
    latest = 0.0
    count = 0
    for p in walk_vault_md(vault_root):
        try:
            t = p.stat().st_mtime
        except OSError:
            continue
        if t > latest:
            latest = t
        count += 1
    key = (count, latest)
    cached = _RESOLVER_CACHE.get(vault_root)
    if cached and cached[0] == key:
        return cached[1]
    resolver = WikilinkResolver(vault_root)
    _RESOLVER_CACHE[vault_root] = (key, resolver)
    return resolver


def _stem_tokens_present(page: ParsedPage, query_norm: str) -> bool:
    """All-tokens-present check using Snowball stems on both sides.

    Recovers morphological matches that the literal substring gate
    misses — query "regulation" passes for a page that mentions
    "regulator", "compounding" passes for one that mentions "compound".
    Used only as a fallback in hybrid mode; keyword mode keeps the
    strict substring gate (precision is the feature there).
    """
    if not query_norm:
        return True
    from . import bm25 as bm25_module
    text_stems = set(bm25_module.tokenize(page.title + " " + page.body))
    for tok in query_norm.split():
        if not tok:
            continue
        if bm25_module.stem_word(tok) not in text_stems:
            return False
    return True


def _stem_anchored_excerpt(page: ParsedPage, query_norm: str) -> str:
    """Snippet anchored on the first body word whose stem matches the query.

    Falls back to the leading body snippet when nothing in the body matches
    a query stem (e.g. the match was title-only).
    """
    from . import bm25 as bm25_module
    body = page.body.strip()
    if not body:
        return ""
    query_stems = {bm25_module.stem_word(t) for t in query_norm.split() if t}
    if not query_stems:
        return _collapse(body[:EXCERPT_MAX_LEN])
    anchor_idx = -1
    anchor_len = 0
    # Walk body words in order; first one whose stem is in query_stems wins.
    for m in re.finditer(r"[A-Za-z0-9]+", body):
        word = m.group(0)
        if bm25_module.stem_word(word.lower()) in query_stems:
            anchor_idx = m.start()
            anchor_len = len(word)
            break
    if anchor_idx == -1:
        return _collapse(body[:EXCERPT_MAX_LEN])
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return _collapse(snippet)


def _semantic_excerpt(
    page: ParsedPage,
    query_norm: str,
    best_chunk: str | None,
    keyword_excerpt: str | None,
) -> str:
    """Prefer the matching chunk text (trimmed); fall back to the keyword excerpt."""
    if best_chunk:
        # Strip the title prefix the chunker prepends — it's redundant with
        # the Hit.title field.
        body = best_chunk
        title_prefix = (page.title or "").strip()
        if title_prefix and body.startswith(title_prefix + "\n\n"):
            body = body[len(title_prefix) + 2:]
        snippet = body.strip()[:EXCERPT_MAX_LEN].strip()
        if len(body) > EXCERPT_MAX_LEN:
            snippet = snippet.rstrip() + "…"
        return _collapse(snippet)
    return keyword_excerpt or ""


def _walk_md(root: Path):
    """Yield every .md path under root, skipping excluded subtrees."""
    for child in root.iterdir():
        if child.is_dir():
            if child.name in EXCLUDED_DIR_NAMES:
                continue
            yield from _walk_md(child)
        elif child.is_file() and child.suffix.lower() == ".md":
            yield child


def _parse_page(path: Path, mtime: float, vault_root: Path) -> ParsedPage | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None

    fm_match = FRONTMATTER_PATTERN.match(text)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except yaml.YAMLError as e:
            log.warning("YAML parse error in %s: %s", path, e)
            frontmatter = {}
        body = fm_match.group(2)
        # The FRONTMATTER_PATTERN consumes the closing `\n---\n` but not the
        # blank line that conventionally follows. Strip a single leading `\n`
        # so callers (notably `get`) can feed `body` back into `edit` without
        # accumulating blanks across round-trips.
        if body.startswith("\n"):
            body = body[1:]
    else:
        frontmatter = {}
        body = text

    h1_match = H1_PATTERN.search(body)
    title = h1_match.group(1).strip() if h1_match else path.stem

    try:
        rel_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        rel_path = path.as_posix()

    return ParsedPage(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=mtime,
    )


def _passes_filters(
    page: ParsedPage,
    *,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
) -> bool:
    if types and page.page_type not in types:
        return False
    if projects:
        page_projects = _all_projects(page.frontmatter)
        if not any(p in page_projects for p in projects):
            return False
    if tags:
        page_tags = set(page.tags)
        if not any(t.lower() in page_tags for t in tags):
            return False
    return True


def _all_projects(fm: dict) -> set[str]:
    out: set[str] = set()
    if (p := fm.get("project")):
        out.add(str(p))
    if (ps := fm.get("projects")):
        if isinstance(ps, list):
            out.update(str(x) for x in ps)
        else:
            out.add(str(ps))
    return out


def _make_excerpt(page: ParsedPage, query_norm: str) -> str | None:
    """Return ~200-char snippet anchored to the query; None if no match.

    Tokenizes the query on whitespace and requires every token to appear in
    title or body (case-insensitive, any order). So `contract employment`
    matches a page mentioning "employment contract" — natural-language
    queries don't have to guess exact phrasing.

    If query is empty, returns the first ~200 chars of body (no match required).
    """
    body = page.body.strip()
    if not query_norm:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    title_norm = page.title.lower()
    body_norm = body.lower()
    tokens = query_norm.split()
    if not tokens:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    # Every token must appear somewhere in title or body.
    for tok in tokens:
        if tok not in title_norm and tok not in body_norm:
            return None
    # Pick the anchor: first token's first body occurrence; if every token is
    # title-only, return a leading body snippet for context.
    anchor_idx = -1
    anchor_len = 0
    for tok in tokens:
        idx = body_norm.find(tok)
        if idx != -1:
            anchor_idx = idx
            anchor_len = len(tok)
            break
    if anchor_idx == -1:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return _collapse(snippet)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clear_cache() -> None:
    """Test hook: flush the in-process cache between tests."""
    _CACHE.entries.clear()
