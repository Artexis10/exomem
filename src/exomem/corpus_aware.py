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
- `detect_contradictions()` — flag existing ACTIVE COMPILED notes whose content
  sits in the band *just below* the dup threshold (`[floor, dup_threshold)`):
  close enough to plausibly restate, refine, OR contradict, but not a duplicate.
  This is PROXIMITY measurement, not a stance judgment — the cosine band can't
  tell agreement from contradiction, so the warning surfaces the tension and the
  reader judges (and supersedes if it's a real conflict). Shares one embedding
  pass with `detect_duplicates` so a write encodes the draft only once.

ALTITUDE: everything here is *surfaced* (returned as structured suggestions /
warnings) for the client LLM to act on — never auto-injected into a body. The
user makes the call, so visibility beats silent graph mutation.
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
DUP_THRESHOLD = 0.90  # default min doc-doc cosine for a near-dup; override via EXOMEM_DUP_THRESHOLD
CONTRADICTION_FLOOR = 0.82  # default lower edge of the contradiction band [floor, dup_threshold); override via EXOMEM_CONTRADICTION_FLOOR
RELATED_OVERFETCH = 3  # fetch limit * this from find(), then re-rank + trim

# Lead-body word budget for the synthesized "what is this about" query.
_QUERY_LEAD_WORDS = 400


def _dup_threshold() -> float:
    """DUP_THRESHOLD, overridable at runtime via EXOMEM_DUP_THRESHOLD.

    Lower = more near-dup warnings (0.86 was the old, looser default); higher =
    stricter (e.g. 0.93). Resolved per call so the env is read live, not frozen
    at import. Bad values fall back to the default with a logged warning.
    """
    raw = os.environ.get("EXOMEM_DUP_THRESHOLD")
    if raw is None:
        return DUP_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid EXOMEM_DUP_THRESHOLD=%r; using %s", raw, DUP_THRESHOLD)
        return DUP_THRESHOLD


def _contradiction_floor() -> float:
    """CONTRADICTION_FLOOR, overridable at runtime via EXOMEM_CONTRADICTION_FLOOR.

    The lower edge of the contradiction band `[floor, dup_threshold)`. Pages this
    close to a draft (but not near-identical) often restate / refine / contradict
    it. Resolved per call so the env is read live, not frozen at import. Bad
    values fall back to the default with a logged warning.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_FLOOR")
    if raw is None:
        return CONTRADICTION_FLOOR
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "invalid EXOMEM_CONTRADICTION_FLOOR=%r; using %s", raw, CONTRADICTION_FLOOR
        )
        return CONTRADICTION_FLOOR


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
    # Claim-level polarity (EXOMEM_CLAIM_LEVEL only). None on the baseline path —
    # left None, `as_dict`/`overlap_warning` are byte-identical to pre-feature.
    polarity: str | None = None          # contradict | refine | duplicate | unrelated
    polarity_score: float | None = None
    polarity_method: str | None = None   # heuristic | nli

    def as_dict(self) -> dict:
        d = {"path": self.path, "title": self.title, "cosine": self.cosine}
        if self.polarity is not None:
            d["polarity"] = self.polarity
            d["polarity_score"] = self.polarity_score
            d["polarity_method"] = self.polarity_method
        return d


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


def _best_cosine_per_file(
    vault_root: Path, *, title: str, body: str, k: int = 15
) -> dict[str, float]:
    """Embed a draft (title+body) as PASSAGES and return the max cosine per
    existing file over the sidecar: ``{file_path: best_score}``.

    The shared core of `detect_duplicates` / `detect_contradictions`: one encode
    + search pass, so a single write can partition the same scores into the dup
    band and the contradiction band without embedding the draft twice (the
    encode is the expensive part). Embeds with `is_query=False` (doc-to-doc, not
    a query). Returns ``{}`` when embeddings are disabled, unimportable, or the
    sidecar is empty — the no-op contract both callers depend on, so the fast
    test suite and torch-less deploys are unaffected.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}
    # While the background warm-up is loading the model, embedding the draft
    # would BLOCK on the singleton lock (minutes on a first-ever download) —
    # and this runs inline on every add/note/edit and pack assembly. Skip the
    # sweep; {} is the same no-op contract used for torch-less deploys.
    from . import readiness
    if readiness.should_defer("embeddings"):
        return {}
    try:
        from . import embeddings

        chunks = embeddings.chunk_text(title, body)
        if not chunks:
            return {}
        vecs = embeddings.embed_texts(chunks, is_query=False)
        idx = embeddings.get_embedding_index(vault_root)
        best_per_file: dict[str, float] = {}
        for v in vecs:
            for fp, _cidx, _ctext, score in idx.search(v, k=k):
                if fp not in best_per_file or score > best_per_file[fp]:
                    best_per_file[fp] = score
        return best_per_file
    except ImportError as e:
        log.debug("_best_cosine_per_file unavailable (%s)", e)
        return {}
    except Exception as e:  # noqa: BLE001 — best-effort
        log.debug("_best_cosine_per_file failed: %s", e)
        return {}


def detect_duplicates(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    types_filter: list[str] | None = None,
    threshold: float | None = None,
    top_n: int = 3,
    precomputed: dict[str, float] | None = None,
) -> list[DupCandidate]:
    """Flag existing pages whose content is near-identical to a draft.

    Cosine-matches the draft against the existing sidecar (via
    `_best_cosine_per_file`) and returns at most `top_n` candidates at/above
    `threshold` (default resolved from `EXOMEM_DUP_THRESHOLD`, else
    `DUP_THRESHOLD`), optionally restricted to `types_filter` page types. Pass
    `precomputed` (a `_best_cosine_per_file` map) to reuse one embedding pass
    across the dup + contradiction checks on a single write. No-ops (returns [])
    when embeddings are disabled or the sidecar is empty.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    if threshold is None:
        threshold = _dup_threshold()
    best_per_file = (
        precomputed
        if precomputed is not None
        else _best_cosine_per_file(vault_root, title=title, body=body, k=top_n * 5)
    )
    if not best_per_file:
        return []

    from . import find as find_module

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


def detect_contradictions(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    top_n: int = 3,
    precomputed: dict[str, float] | None = None,
) -> list[DupCandidate]:
    """Flag existing ACTIVE COMPILED notes in the band `[floor, dup_threshold)`.

    A page this close to the draft (but not near-identical) plausibly restates,
    refines, OR contradicts it. This is a PROXIMITY measurement, not a polarity
    judgment — embeddings can't separate "X works" from "X doesn't" — so the
    server surfaces the tension and the reader decides (and supersedes if it's a
    real conflict). Candidates are restricted to *active compiled conclusions in
    a writeable (read-write) tree* — the only things resolvable via edit/replace
    — so a raw source never trips this, and an `add` only flags genuine
    new-capture-vs-active-conclusion tension (never source-vs-source noise).

    `floor` is resolved from `EXOMEM_CONTRADICTION_FLOOR`, the `ceiling` from
    `EXOMEM_DUP_THRESHOLD`; an inverted band (floor >= ceiling) is logged and
    disabled (returns []). Pass `precomputed` to share one embedding pass with
    `detect_duplicates`. No-ops (returns []) when embeddings are disabled/empty.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    floor = _contradiction_floor()
    ceiling = _dup_threshold()
    if floor >= ceiling:
        log.warning(
            "EXOMEM_CONTRADICTION_FLOOR (%s) >= dup ceiling (%s); "
            "contradiction band disabled this call",
            floor, ceiling,
        )
        return []
    best_per_file = (
        precomputed
        if precomputed is not None
        else _best_cosine_per_file(vault_root, title=title, body=body, k=top_n * 5)
    )
    if not best_per_file:
        return []

    from . import access, find as find_module

    self_canon = _canon(self_path) if self_path else None
    out: list[DupCandidate] = []
    for fp, score in sorted(best_per_file.items(), key=lambda t: -t[1]):
        if score >= ceiling:
            continue  # a near-duplicate — detect_duplicates owns that band
        if score < floor:
            break  # sorted desc — nothing else reaches the band
        if self_canon and _canon(fp) == self_canon:
            continue
        page = find_module._CACHE.get(vault_root / fp, vault_root)
        if page is None:
            continue
        # Restrict to active compiled conclusions in a writeable tree: the only
        # candidates a contradiction can actually be reconciled against.
        if page.page_type not in find_module._COMPILED_TYPES:
            continue
        if page.status in ("superseded", "archived"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        out.append(DupCandidate(path=fp, title=page.title, cosine=round(float(score), 4)))
        if len(out) >= top_n:
            break
    # Sharpen PROXIMITY → POLARITY on the flagged pairs (opt-in; no-op when the
    # EXOMEM_CLAIM_LEVEL gate is off, so `out` and every downstream warning are
    # byte-identical to baseline).
    return _refine_contradictions(vault_root, title=title, body=body, candidates=out)


def dup_warning(candidate: DupCandidate) -> str:
    """Render a near-duplicate as a single warning string for a write result."""
    return (
        f"possible near-duplicate of [[{candidate.path}]] (cosine "
        f"{candidate.cosine}) — consider edit/replace/append instead of a new page"
    )


# How a claim-level polarity verdict sharpens the (otherwise proximity-only)
# overlap warning. Only rendered when EXOMEM_CLAIM_LEVEL produced a `polarity`.
_POLARITY_CLAUSE = {
    "contradict": "claim-level check: LIKELY CONTRADICTS — read both and supersede the stale one if they conflict",
    "refine": "claim-level check: likely a REFINEMENT (same topic, differing detail) — consider merging/linking",
    "duplicate": "claim-level check: likely a near-RESTATEMENT — consider edit/replace instead of a new page",
    "unrelated": "claim-level check: claims look UNRELATED — the proximity may be a false positive",
}


def overlap_warning(candidate: DupCandidate) -> str:
    """Render a band-overlap as a single honest warning for a write result.

    Deliberately NOT phrased as an asserted contradiction — the cosine band is a
    proximity measurement, not a stance judgment. It names contradiction as one
    possibility and hands the call to the reader (measure-don't-judge), pointing
    at supersession as the resolution if it IS a conflict.

    When `EXOMEM_CLAIM_LEVEL` attached a claim-level polarity verdict
    (`candidate.polarity`), a second clause SHARPENS the proximity flag into a
    stance hint. With no polarity (the default, gate-off path) the string is
    byte-identical to the pre-feature warning.
    """
    base = (
        f"overlaps active note [[{candidate.path}]] (cosine {candidate.cosine}) "
        "— review: does this restate, refine, or contradict it? supersede the "
        "stale one if they conflict"
    )
    if candidate.polarity is None:
        return base
    clause = _POLARITY_CLAUSE.get(candidate.polarity)
    if not clause:
        return base
    return f"{base}. [{clause}; via {candidate.polarity_method}]"


def _refine_contradictions(
    vault_root: Path,
    *,
    title: str,
    body: str,
    candidates: list[DupCandidate],
) -> list[DupCandidate]:
    """Attach a claim-level polarity verdict to each proximity-flagged candidate.

    Gated by `EXOMEM_CLAIM_LEVEL` (via `claims.claim_level_enabled`): off → the
    candidates are returned untouched (polarity None), so the caller's warnings
    stay byte-identical to baseline. On → the draft's claim is extracted from
    `title`/`body` and compared, pairwise, against each candidate page's stored
    (or live-extracted) claim through `claims.classify_polarity`. Bounded like the
    rerank lane (`_max_polarity_pairs`): candidates past the cap flow through
    unrefined rather than being dropped. Best-effort — any failure leaves the
    candidate unrefined (never raises into a write).
    """
    from . import claims as claims_module

    if not candidates or not claims_module.claim_level_enabled():
        return candidates
    try:
        draft_claim = claims_module.extract_claim_text(title, body)
    except Exception as e:  # noqa: BLE001
        log.debug("claim extraction failed for draft (%s)", e)
        return candidates
    if not draft_claim:
        return candidates

    max_pairs = claims_module._max_polarity_pairs()
    index = claims_module.get_claim_index(vault_root)
    for i, cand in enumerate(candidates):
        if i >= max_pairs:
            break  # bounded lane — leave the tail unrefined
        try:
            cand_claim = claims_module.claim_text_for_page(
                vault_root, cand.path, index=index
            )
            if not cand_claim:
                continue
            res = claims_module.classify_polarity(
                draft_claim, cand_claim, cosine=cand.cosine
            )
            cand.polarity = res.label
            cand.polarity_score = res.score
            cand.polarity_method = res.method
        except Exception as e:  # noqa: BLE001 — a polarity miss never breaks a write
            log.debug("polarity check failed for %s (%s)", cand.path, e)
    return candidates
