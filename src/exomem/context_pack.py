"""Reasoning-ready context packs — the optional assembled return of `find(pack=true)`.

`find` normally returns ranked hit *excerpts*; to reason over them a caller then fans out
`get` calls, chases wikilinks by hand, and has no view of contradictions among the hits.
This module assembles, in one pass over the top hits, a **context pack**: each note's key
claims, bounded citable semantic units, the 1-hop wikilink neighbourhood of those notes,
and the contradictions / supersessions among them.

It is PURE ASSEMBLY (measurement), mirroring `attention.py`:
- "Key claims" are extracted STRUCTURALLY from the note's own markdown (lede, recognized
  headline-section lines, the `##` outline) — never generated or summarized by a model.
- Compact and rich semantic units come from the same parent snapshot and canonical parser,
  grouped under bounded provenance/lifecycle context. Selected unit hits are packed first.
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

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    corpus_aware,
    epistemic_graph,
    find_corpus,
    semantic_index,
    semantic_units,
)
from . import find as find_module
from . import vault as vault_module
from .find_types import Hit, ParsedPage, SemanticUnitHit

# --- bounds (env-overridable at call time so tests can monkeypatch) ---
_DEFAULT_MAX_HITS = 5
_DEFAULT_MAX_NEIGHBORS = 10
_DEFAULT_MAX_TENSION = 10
_DEFAULT_CLAIM_CHARS = 280
_DEFAULT_MAX_UNITS_PER_PAGE = 8
_DEFAULT_MAX_UNITS = 24
_DEFAULT_UNIT_CHARS = 360
_DEFAULT_MAX_UNIT_TOTAL_CHARS = 12_000

_MAX_UNIT_TAGS = 16
_MAX_UNIT_TAG_CHARS = 64
_MAX_UNIT_CONTEXT_CHARS = 240
_MAX_UNIT_RELATIONS = 8
_MAX_UNIT_RELATION_TARGET_CHARS = 240
_MAX_UNIT_RELATION_RAW_CHARS = 320
_MAX_PARENT_PROVENANCE = 8
_MAX_PARENT_PROVENANCE_CHARS = 240
_MAX_LEGACY_METADATA = 8
_MAX_LEGACY_METADATA_KEY_CHARS = 64
_MAX_LEGACY_METADATA_VALUE_CHARS = 160

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
    if n <= 0:
        return ""
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


def _hit_parent_path(hit: Hit | SemanticUnitHit) -> str:
    return hit.parent_path if isinstance(hit, SemanticUnitHit) else hit.path


def _selected_unit_refs(hit: Hit | SemanticUnitHit) -> list[str]:
    if isinstance(hit, SemanticUnitHit):
        return [hit.unit_ref]
    return [
        str(item["unit_ref"])
        for item in (hit.matched_units or [])
        if item.get("unit_ref")
    ]


def _load_parent_snapshot(
    vault_root: Path,
    rel_path: str,
) -> tuple[ParsedPage, semantic_index.SemanticParentIndexState] | None:
    """Read one parent once, then derive page and semantic state from those bytes."""
    try:
        root = vault_root.resolve()
        path = (root / rel_path).resolve()
        path.relative_to(root)
        content = path.read_bytes()
        mtime = path.stat().st_mtime
        source = content.decode("utf-8")
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    page = find_corpus.parse_page(path, mtime, root, content=content)
    if page is None:
        return None
    try:
        state = semantic_index.build_parent_index_state(root, path, source=source)
    except (OSError, UnicodeError, ValueError):
        return None
    return page, state


def _bounded_values(value: Any, *, limit: int, chars: int) -> tuple[list[str], int]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    cap = max(0, limit)
    shown = [_cap(str(item), chars) for item in values[:cap]]
    clipped = sum(len(str(item).strip()) > chars for item in values[:cap])
    return shown, max(0, len(values) - len(shown)) + clipped


def _parent_context(page: ParsedPage, parent_ref: str | None) -> tuple[dict, int]:
    sources, dropped_sources = _bounded_values(
        page.frontmatter.get("sources"),
        limit=_MAX_PARENT_PROVENANCE,
        chars=_MAX_PARENT_PROVENANCE_CHARS,
    )
    evidence_values: list[Any] = []
    for key in ("evidence", "evidence_file"):
        value = page.frontmatter.get(key)
        if isinstance(value, list):
            evidence_values.extend(value)
        elif value is not None:
            evidence_values.append(value)
    evidence, dropped_evidence = _bounded_values(
        evidence_values,
        limit=_MAX_PARENT_PROVENANCE,
        chars=_MAX_PARENT_PROVENANCE_CHARS,
    )
    return (
        {
            "path": page.rel_path,
            "ref": parent_ref,
            "title": _cap(page.title, _MAX_PARENT_PROVENANCE_CHARS),
            "type": _cap(page.page_type, 64) if page.page_type else None,
            "status": _cap(page.status or "active", 64),
            "updated": _cap(page.updated, 64),
            "supersedes": [
                _cap(value, _MAX_PARENT_PROVENANCE_CHARS)
                for value in page.supersedes[:_MAX_PARENT_PROVENANCE]
            ],
            "superseded_by": [
                _cap(value, _MAX_PARENT_PROVENANCE_CHARS)
                for value in page.superseded_by[:_MAX_PARENT_PROVENANCE]
            ],
            "sources": sources,
            "evidence": evidence,
        },
        dropped_sources
        + dropped_evidence
        + max(0, len(page.supersedes) - _MAX_PARENT_PROVENANCE)
        + max(0, len(page.superseded_by) - _MAX_PARENT_PROVENANCE),
    )


def _pack_unit(
    unit: semantic_units.SemanticUnit,
    *,
    unit_chars: int,
) -> tuple[dict, int]:
    relations: list[dict] = []
    for relation in unit.relations[:_MAX_UNIT_RELATIONS]:
        relations.append(
            {
                "kind": relation.kind,
                "target": _cap(relation.target, _MAX_UNIT_RELATION_TARGET_CHARS),
                "line": relation.line,
                "origin": "authored_rich_unit",
                "direction": "outbound",
                "source_anchor": unit.anchor,
            }
        )
    tags = [_cap(tag, _MAX_UNIT_TAG_CHARS) for tag in unit.tags[:_MAX_UNIT_TAGS]]
    clipped_fields = int(len(unit.content.strip()) > max(0, unit_chars))
    if unit.context:
        clipped_fields += int(
            len(unit.context.strip()) > _MAX_UNIT_CONTEXT_CHARS
        )
    clipped_fields += sum(
        len(str(tag).strip()) > _MAX_UNIT_TAG_CHARS for tag in unit.tags[:_MAX_UNIT_TAGS]
    )
    clipped_fields += sum(
        len(relation.target.strip()) > _MAX_UNIT_RELATION_TARGET_CHARS
        for relation in unit.relations[:_MAX_UNIT_RELATIONS]
    )
    return (
        {
            "unit_ref": unit.unit_ref,
            "fingerprint": unit.fingerprint,
            "form": unit.form,
            "category_raw": unit.category_raw,
            "category_key": unit.category_key,
            "category": unit.category,
            "kind": unit.kind,
            "excerpt": _cap(unit.content, max(0, unit_chars)),
            "tags": tags,
            "context": _cap(unit.context, _MAX_UNIT_CONTEXT_CHARS)
            if unit.context
            else None,
            "source_anchor": unit.anchor,
            "source_span": {
                "start_line": unit.span.start_line,
                "start_column": unit.span.start_column,
                "end_line": unit.span.end_line,
                "end_column": unit.span.end_column,
                "start_offset": unit.span.start_offset,
                "end_offset": unit.span.end_offset,
            },
            "source_hash": unit.source_hash,
            "relations": relations,
        },
        max(0, len(unit.tags) - len(tags))
        + max(0, len(unit.relations) - len(relations))
        + clipped_fields,
    )


def _bounded_legacy_metadata(unit: semantic_units.SemanticUnit) -> tuple[dict[str, str], int]:
    items = list(unit.metadata.items())
    shown = {
        _cap(str(key), _MAX_LEGACY_METADATA_KEY_CHARS): _cap(
            str(value), _MAX_LEGACY_METADATA_VALUE_CHARS
        )
        for key, value in items[:_MAX_LEGACY_METADATA]
    }
    clipped = sum(
        len(str(key).strip()) > _MAX_LEGACY_METADATA_KEY_CHARS
        or len(str(value).strip()) > _MAX_LEGACY_METADATA_VALUE_CHARS
        for key, value in items[:_MAX_LEGACY_METADATA]
    )
    return shown, max(0, len(items) - len(shown)) + clipped


def _legacy_block_from_packed_unit(
    unit: semantic_units.SemanticUnit,
    packed: dict,
) -> tuple[dict | None, int]:
    if unit.form != "rich":
        return None, 0
    metadata, dropped = _bounded_legacy_metadata(unit)
    relations: list[dict[str, Any]] = []
    for authored, relation in zip(
        unit.relations,
        packed["relations"],
        strict=False,
    ):
        relations.append(
            {
                "kind": relation["kind"],
                "target": relation["target"],
                "raw": _cap(authored.raw, _MAX_UNIT_RELATION_RAW_CHARS),
                "line": relation["line"],
            }
        )
        dropped += int(len(authored.raw.strip()) > _MAX_UNIT_RELATION_RAW_CHARS)
    out: dict[str, Any] = {
        "type": unit.kind,
        "title": _cap(unit.title, 160) if unit.title else None,
        "level": unit.level,
        "line": packed["source_span"]["start_line"],
        "end_line": packed["source_span"]["end_line"],
        "body": packed["excerpt"],
        "metadata": metadata,
        "relations": relations,
    }
    if unit.anchor:
        out["id"] = unit.anchor
    return out, dropped


@dataclass
class _UnitPackPlan:
    page: ParsedPage
    parent: dict[str, Any]
    selected: list[tuple[int, int, semantic_units.SemanticUnit]]
    fillers: list[semantic_units.SemanticUnit]
    dropped_provenance: int = 0
    packed_units: list[dict[str, Any]] = field(default_factory=list)
    legacy_blocks: list[dict[str, Any]] = field(default_factory=list)
    chosen_units: list[tuple[semantic_units.SemanticUnit, dict[str, Any]]] = field(
        default_factory=list
    )
    omitted_reasons: set[str] = field(default_factory=set)
    dropped_fields: int = 0


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
    hits: list[Hit | SemanticUnitHit],
    *,
    max_hits: int | None = None,
    max_neighbors: int | None = None,
    max_tension: int | None = None,
    max_units_per_page: int | None = None,
    max_units: int | None = None,
    unit_chars: int | None = None,
    max_unit_total_chars: int | None = None,
    graph_enrich: bool = False,
) -> dict:
    """Assemble a reasoning-ready context pack over the top `hits`. Pure measurement.

    Returns ``{packed_paths, claims, semantic_units, semantic_blocks, neighborhood,
    contradictions, embeddings_available, truncation}``. Reads note content,
    frontmatter, wikilinks, and precomputed sidecar embeddings only — no mutation,
    no generative model, `find` ordering untouched.
    """
    max_hits = _resolve_cap(max_hits, "EXOMEM_PACK_MAX_HITS", _DEFAULT_MAX_HITS)
    max_neighbors = _resolve_cap(max_neighbors, "EXOMEM_PACK_MAX_NEIGHBORS", _DEFAULT_MAX_NEIGHBORS)
    max_tension = _resolve_cap(max_tension, "EXOMEM_PACK_MAX_TENSION", _DEFAULT_MAX_TENSION)
    claim_chars = _resolve_cap(None, "EXOMEM_PACK_CLAIM_CHARS", _DEFAULT_CLAIM_CHARS)
    max_units_per_page = max(
        0,
        _resolve_cap(
            max_units_per_page,
            "EXOMEM_PACK_MAX_UNITS_PER_PAGE",
            _DEFAULT_MAX_UNITS_PER_PAGE,
        ),
    )
    max_units = max(
        0,
        _resolve_cap(max_units, "EXOMEM_PACK_MAX_UNITS", _DEFAULT_MAX_UNITS),
    )
    unit_chars = max(
        0,
        _resolve_cap(unit_chars, "EXOMEM_PACK_UNIT_CHARS", _DEFAULT_UNIT_CHARS),
    )
    max_unit_total_chars = max(
        0,
        _resolve_cap(
            max_unit_total_chars,
            "EXOMEM_PACK_MAX_UNIT_TOTAL_CHARS",
            _DEFAULT_MAX_UNIT_TOTAL_CHARS,
        ),
    )

    truncation: list[str] = []
    # Group page/unit/mixed results by parent before applying max_hits. Repeated unit
    # hits from one parent therefore cannot starve lower-ranked parents.
    groups: list[dict[str, Any]] = []
    by_parent: dict[str, dict[str, Any]] = {}
    for hit_rank, hit in enumerate(hits):
        parent_path = _hit_parent_path(hit)
        canon = corpus_aware._canon(parent_path)
        group = by_parent.get(canon)
        if group is None:
            group = {"path": parent_path, "selected_refs": []}
            by_parent[canon] = group
            groups.append(group)
        selected_refs = group["selected_refs"]
        known_refs = {item[2] for item in selected_refs}
        for unit_order, unit_ref in enumerate(_selected_unit_refs(hit)):
            if unit_ref not in known_refs:
                selected_refs.append((hit_rank, unit_order, unit_ref))
                known_refs.add(unit_ref)

    total = len(groups)
    packed_groups = groups[:max_hits] if max_hits > 0 else groups
    if total > len(packed_groups):
        truncation.append(
            f"packed {len(packed_groups)} of {total} parent hits "
            f"({total - len(packed_groups)} more not packed; raise EXOMEM_PACK_MAX_HITS)"
        )

    packed_pages: list[ParsedPage] = []
    semantic_states: dict[str, semantic_index.SemanticParentIndexState] = {}
    selected_by_path: dict[str, list[tuple[int, int, str]]] = {}
    missing = 0
    for group in packed_groups:
        snapshot = _load_parent_snapshot(vault_root, str(group["path"]))
        if snapshot is None:
            missing += 1  # a packed hit whose file is gone/unreadable — surface it.
            continue
        page, state = snapshot
        packed_pages.append(page)
        semantic_states[page.rel_path] = state
        selected_by_path[page.rel_path] = list(group["selected_refs"])
    if missing:
        truncation.append(f"{missing} packed hit(s) unreadable or missing, not packed")

    claims = {p.rel_path: _extract_claims(p, claim_chars=claim_chars) for p in packed_pages}
    semantic_unit_map: dict[str, dict[str, Any]] = {}
    semantic_block_map: dict[str, list[dict]] = {}
    plans: list[_UnitPackPlan] = []
    for page in packed_pages:
        document = semantic_states[page.rel_path].document
        by_ref = {
            unit.unit_ref: unit for unit in document.units if unit.unit_ref is not None
        }
        selected: list[tuple[int, int, semantic_units.SemanticUnit]] = []
        unresolved = 0
        for hit_rank, unit_order, unit_ref in selected_by_path.get(page.rel_path, []):
            unit = by_ref.get(unit_ref)
            if unit is None:
                unresolved += 1
            elif all(existing[2] is not unit for existing in selected):
                selected.append((hit_rank, unit_order, unit))
        if unresolved:
            truncation.append(
                f"{page.rel_path}: {unresolved} selected semantic unit(s) stale or missing"
            )

        selected_ids = {id(item[2]) for item in selected}
        parent, dropped_provenance = _parent_context(
            page, semantic_states[page.rel_path].parent_ref
        )
        plans.append(
            _UnitPackPlan(
                page=page,
                parent=parent,
                selected=selected,
                fillers=[
                    unit for unit in document.units if id(unit) not in selected_ids
                ],
                dropped_provenance=dropped_provenance,
            )
        )

    packed_unit_count = 0

    def _try_pack(
        plan: _UnitPackPlan,
        unit: semantic_units.SemanticUnit,
    ) -> None:
        nonlocal packed_unit_count
        if len(plan.packed_units) >= max_units_per_page:
            plan.omitted_reasons.add("per-page cap")
            return
        if packed_unit_count >= max_units:
            plan.omitted_reasons.add("pack-wide cap")
            return

        packed, dropped = _pack_unit(unit, unit_chars=unit_chars)
        block, dropped_metadata = _legacy_block_from_packed_unit(unit, packed)
        candidate_unit_map = {
            **semantic_unit_map,
            plan.page.rel_path: {
                "parent": plan.parent,
                "units": plan.packed_units + [packed],
            },
        }
        candidate_block_map = dict(semantic_block_map)
        if block is not None:
            candidate_block_map[plan.page.rel_path] = plan.legacy_blocks + [block]
        encoded = json.dumps(
            {
                "semantic_units": candidate_unit_map,
                "semantic_blocks": candidate_block_map,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded) > max_unit_total_chars:
            plan.omitted_reasons.add("character cap")
            return

        plan.packed_units.append(packed)
        plan.chosen_units.append((unit, packed))
        if block is not None:
            plan.legacy_blocks.append(block)
        plan.dropped_fields += dropped + dropped_metadata
        packed_unit_count += 1
        semantic_unit_map[plan.page.rel_path] = {
            "parent": plan.parent,
            "units": plan.packed_units,
        }
        if plan.legacy_blocks:
            semantic_block_map[plan.page.rel_path] = plan.legacy_blocks

    selected_candidates = sorted(
        (
            (hit_rank, unit_order, plan_index, plan, unit)
            for plan_index, plan in enumerate(plans)
            for hit_rank, unit_order, unit in plan.selected
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    for _hit_rank, _unit_order, _plan_index, plan, unit in selected_candidates:
        _try_pack(plan, unit)
    for plan in plans:
        for unit in plan.fillers:
            _try_pack(plan, unit)

    for plan in plans:
        total_units = len(plan.selected) + len(plan.fillers)
        omitted = total_units - len(plan.packed_units)
        if omitted:
            reason = ", ".join(sorted(plan.omitted_reasons)) or "configured bounds"
            truncation.append(
                f"{plan.page.rel_path}: {omitted} semantic units omitted by {reason}"
            )
        selected_included = {
            packed["unit_ref"]
            for _unit, packed in plan.chosen_units
            if packed["unit_ref"]
        }
        selected_omitted = sum(
            1
            for _hit_rank, _unit_order, unit in plan.selected
            if unit.unit_ref not in selected_included
        )
        if selected_omitted:
            truncation.append(
                f"{plan.page.rel_path}: {selected_omitted} selected semantic unit(s) omitted by bounds"
            )
        if plan.dropped_provenance:
            truncation.append(
                f"{plan.page.rel_path}: {plan.dropped_provenance} provenance/lifecycle value(s) omitted"
            )
        if plan.dropped_fields:
            truncation.append(
                f"{plan.page.rel_path}: {plan.dropped_fields} semantic-unit field value(s) omitted by bounds"
            )

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
        "semantic_units": semantic_unit_map,
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
