"""Reasoning-ready context packs — the optional assembled return of `find(pack=true)`.

`find` normally returns ranked hit *excerpts*; to reason over them a caller then fans out
`get` calls, chases wikilinks by hand, and has no view of contradictions among the hits.
This module assembles, in one pass over the top hits, a **context pack**: each note's key
claims, the 1-hop wikilink neighbourhood of those notes, and the contradictions /
supersessions among them.

It is PURE ASSEMBLY (measurement), mirroring `attention.py`:
- "Key claims" are extracted STRUCTURALLY from the note's own markdown (lede, recognized
  headline-section lines, the `##` outline) — never generated or summarized by a model.
- The neighbourhood reuses `find`'s outbound-link resolution + `vault`'s inbound search.
- Contradictions are recorded supersession edges (frontmatter) plus proximity "tension"
  pairs whose cosine sits in the existing `[floor, dup)` band (reusing
  `corpus_aware._best_cosine_per_file`) — proximity, not polarity; the reader decides.

Nothing is mutated, no generative/reasoning model runs, and `find` ordering is untouched.
The tension part soft-fails to empty (`embeddings_available: false`) when the embedding
sidecar is disabled or unimportable, so the rest of the pack still assembles. Every cap
that drops content is reported in `truncation` — never a silent truncation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import (
    corpus_aware,
    epistemic_graph,
    relation_registry,
    semantic_language_registry,
    semantic_units,
)
from . import find as find_module
from . import vault as vault_module
from .find import Hit, ParsedPage

# --- bounds (env-overridable at call time so tests can monkeypatch) ---
_DEFAULT_MAX_HITS = 5
_DEFAULT_MAX_NEIGHBORS = 10
_DEFAULT_MAX_TENSION = 10
_DEFAULT_CLAIM_CHARS = 280

# Per-note claim shaping — small, fixed (claims are inherently bounded by note structure).
_SECTION_MAX_LINES = 3
_SECTION_CHARS = 200
_MAX_SECTIONS = 8
_MAX_OUTLINE = 16
_NEIGHBOR_LEDE_CHARS = 160

# Headline sections whose lead line is a high-signal "claim". Matched case-insensitively
# against the heading text (trailing colon stripped). Connections/See-also are links, not
# claims, so they are deliberately absent.
RECOGNIZED_SECTIONS: frozenset[str] = frozenset(
    {
        "summary",
        "problem",
        "conclusion",
        "decision",
        "pattern",
        "hypothesis",
        "result",
        "results",
        "insight",
        "tl;dr",
        "tldr",
        "takeaway",
        "why",
        "finding",
        "findings",
        "claim",
        "claims",
    }
)

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_H1_RE = re.compile(r"^#\s+")  # level-1 (the title line in body)
_H2_RE = re.compile(r"^##\s+(.*)$")  # level-2 only (the outline skeleton)
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")  # level 2-6 (recognized-section scan)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s")


# ----------------------------- small text utils -----------------------------


def _resolve_cap(value: int | None, env: str, default: int) -> int:
    if value is not None:
        return value
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _cap(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def _strip_fences(body: str) -> list[str]:
    """Body lines with fenced code blocks removed (a `#`/`[[ ]]` inside a fence is not a
    heading/link). Mirrors the fence-awareness of `vault.find_body_wikilinks`."""
    out: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def _first_sentence(text: str) -> str:
    text = _collapse(text)
    if not text:
        return ""
    return _SENTENCE_SPLIT.split(text, maxsplit=1)[0]


# ----------------------------- claim extraction -----------------------------


def _lede(lines: list[str]) -> str:
    """The first content paragraph (collapsed), skipping leading blanks + the H1 title."""
    i, n = 0, len(lines)
    while i < n and not lines[i].strip():
        i += 1
    if i < n and _H1_RE.match(lines[i].lstrip()):
        i += 1
        while i < n and not lines[i].strip():
            i += 1
    buf: list[str] = []
    while i < n:
        stripped = lines[i].lstrip()
        if not stripped.strip():
            break
        if _HEADING_RE.match(stripped) or _H1_RE.match(stripped):
            break
        buf.append(stripped.lstrip("-*+ ").strip())
        i += 1
    return _collapse(" ".join(buf))


def _sections(lines: list[str]) -> list[str]:
    """`"Heading: lead text"` for each recognized headline section, in document order."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = _HEADING_RE.match(lines[i].lstrip())
        if not m:
            i += 1
            continue
        heading = m.group(2).strip()
        key = heading.lower().strip().rstrip(":").strip()
        i += 1
        if key not in RECOGNIZED_SECTIONS:
            continue
        while i < n and not lines[i].strip():
            i += 1
        buf: list[str] = []
        while i < n and len(buf) < _SECTION_MAX_LINES:
            stripped = lines[i].lstrip()
            if not stripped.strip() or _HEADING_RE.match(stripped):
                break
            buf.append(stripped.lstrip("-*+ ").strip())
            i += 1
        text = _cap(_collapse(" ".join(buf)), _SECTION_CHARS)
        if text:
            out.append(f"{heading}: {text}")
        if len(out) >= _MAX_SECTIONS:
            break
    return out


def _outline(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        m = _H2_RE.match(line.lstrip())
        if m:
            out.append(m.group(1).strip())
            if len(out) >= _MAX_OUTLINE:
                break
    return out


def _extract_claims(page: ParsedPage, *, claim_chars: int = _DEFAULT_CLAIM_CHARS) -> dict:
    lines = _strip_fences(page.body)
    return {
        "title": page.title,
        "type": page.page_type,
        "lede": _cap(_lede(lines), claim_chars),
        "sections": _sections(lines),
        "outline": _outline(lines),
    }


def _extract_semantic_blocks(
    page: ParsedPage,
    document: semantic_units.SemanticUnitDocument,
) -> list[dict]:
    del page
    return document.semantic_blocks


# ----------------------------- neighbourhood -----------------------------


def _neighborhood(
    vault_root: Path, packed_pages: list[ParsedPage], max_neighbors: int
) -> tuple[list[dict], int]:
    """1-hop inbound+outbound wikilink neighbours of the packed notes, packed notes
    excluded, ranked by co-citation (distinct packed notes linked), capped."""
    packed_canon = {corpus_aware._canon(p.rel_path) for p in packed_pages}
    # canon -> {"path", "directions": set, "referenced_by": set}
    neigh: dict[str, dict] = {}

    def _touch(target_path: str, packed_rel: str, direction: str) -> None:
        canon = corpus_aware._canon(target_path)
        if canon in packed_canon:
            return
        entry = neigh.setdefault(
            canon, {"path": target_path, "directions": set(), "referenced_by": set()}
        )
        entry["directions"].add(direction)
        entry["referenced_by"].add(packed_rel)

    for page in packed_pages:
        for target in find_module._outbound_wikilink_paths(page, vault_root):
            _touch(target, page.rel_path, "out")
        for link in vault_module.find_inbound_wikilinks(vault_root, page.rel_path):
            _touch(link.path, page.rel_path, "in")

    items = sorted(
        neigh.values(),
        key=lambda e: (-len(e["referenced_by"]), -len(e["directions"]), e["path"]),
    )
    shown = items[:max_neighbors] if max_neighbors > 0 else items
    dropped = len(items) - len(shown)

    out: list[dict] = []
    for entry in shown:
        page = find_module._CACHE.get(vault_root / entry["path"], vault_root)
        directions = entry["directions"]
        direction = "both" if len(directions) > 1 else next(iter(directions))
        lede = (
            _cap(_first_sentence(_lede(_strip_fences(page.body))), _NEIGHBOR_LEDE_CHARS)
            if page
            else ""
        )
        out.append(
            {
                "path": entry["path"],
                "title": page.title
                if page
                else entry["path"].rsplit("/", 1)[-1].removesuffix(".md"),
                "type": page.page_type if page else None,
                "direction": direction,
                "referenced_by": sorted(entry["referenced_by"]),
                "lede": lede,
            }
        )
    return out, dropped


# ----------------------------- contradictions -----------------------------


def _wikilink_target(raw: str) -> str:
    t = raw.strip()
    if t.startswith("[[") and t.endswith("]]"):
        t = t[2:-2]
    return t.split("|", 1)[0].split("#", 1)[0].strip()


def _supersession_edges(packed_pages: list[ParsedPage]) -> list[dict]:
    """Recorded supersession edges among the set, read straight from frontmatter."""
    by_canon = {corpus_aware._canon(p.rel_path): p.rel_path for p in packed_pages}
    edges: list[dict] = []
    for page in packed_pages:
        for raw in page.superseded_by:
            canon = corpus_aware._canon(_wikilink_target(raw))
            if canon in by_canon and by_canon[canon] != page.rel_path:
                edges.append({"from": page.rel_path, "to": by_canon[canon], "kind": "supersession"})
    return edges


def _tension_pairs(
    vault_root: Path, packed_pages: list[ParsedPage], max_tension: int
) -> tuple[list[dict], int, bool]:
    """Proximity-tension pairs AMONG the packed notes whose pairwise cosine lands in the
    contradiction band. Reuses the embedding sidecar; soft-fails to empty when off.

    `embeddings_available` is True iff a cosine pass returned scores AND the band is
    active; an inverted/disabled band (floor >= ceiling) reports it False — the band is
    off, so no tension can be measured regardless of the sidecar."""
    floor = corpus_aware._contradiction_floor()
    ceiling = corpus_aware._dup_threshold()
    by_canon = {corpus_aware._canon(p.rel_path): p.rel_path for p in packed_pages}
    pair_best: dict[frozenset[str], float] = {}
    embeddings_available = False

    if floor < ceiling:
        for page in packed_pages:
            cmap = corpus_aware._best_cosine_per_file(vault_root, title=page.title, body=page.body)
            if cmap:
                embeddings_available = True
            self_canon = corpus_aware._canon(page.rel_path)
            for fp, score in cmap.items():
                canon = corpus_aware._canon(fp)
                if canon == self_canon or canon not in by_canon:
                    continue
                if not (floor <= score < ceiling):
                    continue
                key = frozenset((self_canon, canon))
                if key not in pair_best or score > pair_best[key]:
                    pair_best[key] = score

    pairs: list[dict] = []
    for key, score in pair_best.items():
        a, b = sorted(key)
        pairs.append(
            {
                "a": by_canon[a],
                "b": by_canon[b],
                "cosine": round(float(score), 4),
                "note": "proximity, not polarity — reader decides",
            }
        )
    pairs.sort(key=lambda d: (-d["cosine"], d["a"], d["b"]))
    shown = pairs[:max_tension] if max_tension > 0 else pairs
    dropped = len(pairs) - len(shown)
    return shown, dropped, embeddings_available


# ----------------------------- assembly -----------------------------


def assemble_pack(
    vault_root: Path,
    hits: list[Hit],
    *,
    max_hits: int | None = None,
    max_neighbors: int | None = None,
    max_tension: int | None = None,
    graph_enrich: bool = False,
) -> dict:
    """Assemble a reasoning-ready context pack over the top `hits`. Pure measurement.

    Returns ``{packed_paths, claims, neighborhood, contradictions, embeddings_available,
    truncation}``. Reads note content, frontmatter, wikilinks, and precomputed sidecar
    embeddings only — no mutation, no generative model, `find` ordering untouched.
    """
    max_hits = _resolve_cap(max_hits, "EXOMEM_PACK_MAX_HITS", _DEFAULT_MAX_HITS)
    max_neighbors = _resolve_cap(max_neighbors, "EXOMEM_PACK_MAX_NEIGHBORS", _DEFAULT_MAX_NEIGHBORS)
    max_tension = _resolve_cap(max_tension, "EXOMEM_PACK_MAX_TENSION", _DEFAULT_MAX_TENSION)
    claim_chars = _resolve_cap(None, "EXOMEM_PACK_CLAIM_CHARS", _DEFAULT_CLAIM_CHARS)

    truncation: list[str] = []
    # De-dupe by canonical path up front: `find` rarely emits the same path twice, but a
    # duplicate would otherwise double-count in `packed_paths` and the supersession edges.
    seen: set[str] = set()
    unique_hits: list[Hit] = []
    for hit in hits:
        canon = corpus_aware._canon(hit.path)
        if canon in seen:
            continue
        seen.add(canon)
        unique_hits.append(hit)

    total = len(unique_hits)
    packed_hits = unique_hits[:max_hits] if max_hits > 0 else unique_hits
    if total > len(packed_hits):
        truncation.append(
            f"packed {len(packed_hits)} of {total} hits "
            f"({total - len(packed_hits)} more not packed; raise EXOMEM_PACK_MAX_HITS)"
        )

    packed_pages: list[ParsedPage] = []
    missing = 0
    for hit in packed_hits:
        page = find_module._CACHE.get(vault_root / hit.path, vault_root)
        if page is None:
            missing += 1  # a packed hit whose file is gone/unreadable — surface it.
            continue
        packed_pages.append(page)
    if missing:
        truncation.append(f"{missing} packed hit(s) unreadable or missing, not packed")

    claims = {p.rel_path: _extract_claims(p, claim_chars=claim_chars) for p in packed_pages}
    relations = relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)
    semantic_documents = {
        page.rel_path: semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=language,
            relation_registry=relations,
            project=(str(page.frontmatter.get("project")) if page.frontmatter.get("project") else None),
            page_type=page.page_type,
        )
        for page in packed_pages
    }
    semantic_block_map: dict[str, list[dict]] = {}
    for page in packed_pages:
        blocks = _extract_semantic_blocks(page, semantic_documents[page.rel_path])
        if blocks:
            semantic_block_map[page.rel_path] = blocks

    neighborhood, n_dropped = _neighborhood(vault_root, packed_pages, max_neighbors)
    if n_dropped > 0:
        truncation.append(
            f"neighborhood capped at {max_neighbors} "
            f"({n_dropped} more not shown; raise EXOMEM_PACK_MAX_NEIGHBORS)"
        )

    superseded = _supersession_edges(packed_pages)
    tension, t_dropped, embeddings_available = _tension_pairs(vault_root, packed_pages, max_tension)
    if t_dropped > 0:
        truncation.append(
            f"tension pairs capped at {max_tension} "
            f"({t_dropped} more not shown; raise EXOMEM_PACK_MAX_TENSION)"
        )

    result = {
        "packed_paths": [p.rel_path for p in packed_pages],
        "claims": claims,
        "semantic_blocks": semantic_block_map,
        "neighborhood": neighborhood,
        "contradictions": {"superseded": superseded, "tension": tension},
        "embeddings_available": embeddings_available,
        "truncation": truncation,
    }
    if graph_enrich:
        result["graph"] = _graph_enrichment(vault_root, packed_pages)
    return result


def _graph_enrichment(vault_root: Path, packed_pages: list[ParsedPage]) -> dict:
    if not packed_pages:
        return {"available": False, "reason": "no packed pages", "nodes": [], "edges": []}
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    unavailable: list[str] = []
    for page in packed_pages:
        ctx = epistemic_graph.graph_context(vault_root, path=page.rel_path, depth=1)
        if not ctx.get("available"):
            unavailable.append(str(ctx.get("reason") or "graph unavailable"))
            continue
        for node in ctx.get("nodes", []):
            nodes.setdefault(node["node_key"], node)
        for edge in ctx.get("edges", []):
            edges.setdefault(edge["edge_key"], edge)
    if not nodes and unavailable:
        return {
            "available": False,
            "reason": unavailable[0],
            "nodes": [],
            "edges": [],
            "truncation": [],
        }
    return {
        "available": True,
        "reason": None,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "truncation": [],
        "warnings": sorted(set(unavailable)),
    }
