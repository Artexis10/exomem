"""Corpus-aware writes: let the existing graph + embeddings inform authoring.

Today the write path is corpus-blind — every wikilink and source is caller-
supplied, so the dense link graph and the embedding sidecar contribute nothing
at authoring time. This module closes that loop using ONLY the existing retrieval
stack (find() + EmbeddingIndex), no new dependency and no server-side LLM:

- `suggest_related()` — given a draft (title + body), return ranked EXISTING
  pages it should probably link to, preferring graph hubs, excluding itself and
  anything already linked. Reuses find() wholesale, so it inherits graceful
  BM25/keyword degradation when embeddings are unavailable.
- `detect_duplicates()` — flag existing pages whose content is near-identical to
  a draft (cosine over the sidecar), so a new entry doesn't silently duplicate an
  old one. A WARNING, never a block — append-only + supersession invariants mean
  the client decides (edit/replace/append), we just make the overlap visible.

ALTITUDE: everything here is *surfaced* (returned as structured suggestions /
warnings) for the client LLM to act on — never auto-injected into a body. Hugo
rubber-stamps approvals, so visibility beats silent graph mutation.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Tunable knobs — intuition-seeded like find.RankingConfig; revisit against the
# eval harness (scripts/eval_retrieval.py) once a golden set exists. Kept here
# as named constants so they're one-line greppable.
HUB_WEIGHT = 0.15  # weight on log1p(graph_in_degree) when re-ranking suggestions
DUP_THRESHOLD = 0.86  # min doc-doc cosine to call something a near-duplicate
RELATED_OVERFETCH = 3  # fetch limit * this from find(), then re-rank + trim

# Lead-body word budget for the synthesized "what is this about" query.
_QUERY_LEAD_WORDS = 400


@dataclass
class RelatedSuggestion:
    path: str
    title: str
    type: str | None
    why: str
    excerpt: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "title": self.title,
            "type": self.type,
            "why": self.why,
            "excerpt": self.excerpt,
        }


@dataclass
class DupCandidate:
    path: str
    title: str
    cosine: float

    def as_dict(self) -> dict:
        return {"path": self.path, "title": self.title, "cosine": self.cosine}


def _canon(path: str) -> str:
    """Comparable key across find paths (with .md), sources (no .md), wikilinks."""
    p = (path or "").strip().replace("\\", "/").split("#", 1)[0].strip()
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith("Knowledge Base/"):
        p = p[len("Knowledge Base/"):]
    return p.lower()


def _why(hit) -> str:
    """One-line rationale assembled from the hit's ranking signals."""
    bits: list[str] = []
    if hit.vector_rank:
        bits.append(f"semantic #{hit.vector_rank}")
    if hit.bm25_rank:
        bits.append(f"keyword #{hit.bm25_rank}")
    if hit.graph_in_degree:
        hub = " (hub)" if hit.graph_in_degree >= 3 else ""
        bits.append(f"{hit.graph_in_degree} shared link(s){hub}")
    return ", ".join(bits) or "related"


def suggest_related(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    existing_links: set[str] | None = None,
    limit: int = 8,
    scope: str = "kb",
) -> list[RelatedSuggestion]:
    """Rank existing pages a draft should link to. Reuses find(); never writes.

    Excludes the draft itself (`self_path`) and anything in `existing_links`
    (cited sources + wikilinks already in the body). Re-ranks find()'s order
    with a small log-scaled graph-in-degree bonus so well-connected hubs float
    up — linking a hub compounds more than linking a leaf.
    """
    from . import find as find_module

    lead = " ".join((body or "").split()[:_QUERY_LEAD_WORDS])
    query = f"{title}\n\n{lead}".strip() or (title or "").strip()
    if not query:
        return []

    self_canon = _canon(self_path) if self_path else None
    excluded = {_canon(e) for e in (existing_links or set())}

    try:
        hits = find_module.find(
            vault_root,
            query=query,
            limit=limit * RELATED_OVERFETCH,
            mode="hybrid",
            graph=True,
            scope=scope,
            prefer_compiled=True,
        )
    except Exception as e:  # noqa: BLE001 — suggestions are best-effort
        log.debug("suggest_related find() failed: %s", e)
        return []

    eligible = []
    for h in hits:
        hc = _canon(h.path)
        if self_canon and hc == self_canon:
            continue
        if hc in excluded:
            continue
        eligible.append(h)

    # Re-rank: find's fused position (1/(i+1)) + hub bonus on graph_in_degree.
    def _score(i_h: tuple[int, object]) -> float:
        i, h = i_h
        return 1.0 / (i + 1) + HUB_WEIGHT * math.log1p(getattr(h, "graph_in_degree", 0) or 0)

    ranked = sorted(enumerate(eligible), key=_score, reverse=True)
    return [
        RelatedSuggestion(
            path=h.path, title=h.title, type=h.type, why=_why(h), excerpt=h.excerpt
        )
        for _, h in ranked[:limit]
    ]


def detect_duplicates(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    types_filter: list[str] | None = None,
    threshold: float = DUP_THRESHOLD,
    top_n: int = 3,
) -> list[DupCandidate]:
    """Flag existing pages whose content is near-identical to a draft.

    Embeds the draft as PASSAGES (is_query=False — this is doc-to-doc, not a
    query) and cosine-matches against the existing sidecar. Returns at most
    `top_n` candidates at/above `threshold`, optionally restricted to
    `types_filter` page types. No-ops (returns []) when embeddings are disabled
    or the sidecar is empty, so the fast test suite and torch-less deploys are
    unaffected.
    """
    if os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):
        return []
    try:
        from . import embeddings, find as find_module

        chunks = embeddings.chunk_text(title, body)
        if not chunks:
            return []
        vecs = embeddings.embed_texts(chunks, is_query=False)
        idx = embeddings.EmbeddingIndex(vault_root)
        best_per_file: dict[str, float] = {}
        for v in vecs:
            for fp, _cidx, _ctext, score in idx.search(v, k=top_n * 5):
                if fp not in best_per_file or score > best_per_file[fp]:
                    best_per_file[fp] = score
    except ImportError as e:
        log.debug("detect_duplicates unavailable (%s)", e)
        return []
    except Exception as e:  # noqa: BLE001 — best-effort
        log.debug("detect_duplicates failed: %s", e)
        return []

    self_canon = _canon(self_path) if self_path else None
    out: list[DupCandidate] = []
    for fp, score in sorted(best_per_file.items(), key=lambda t: -t[1]):
        if score < threshold:
            break  # sorted desc — nothing below threshold remains
        if self_canon and _canon(fp) == self_canon:
            continue
        page = find_module._CACHE.get(vault_root / fp, vault_root)
        if page is None:
            continue
        if types_filter and page.page_type not in types_filter:
            continue
        out.append(DupCandidate(path=fp, title=page.title, cosine=round(float(score), 4)))
        if len(out) >= top_n:
            break
    return out


def dup_warning(candidate: DupCandidate) -> str:
    """Render a near-duplicate as a single warning string for a write result."""
    return (
        f"possible near-duplicate of [[{candidate.path}]] (cosine "
        f"{candidate.cosine}) — consider edit/replace/append instead of a new page"
    )
