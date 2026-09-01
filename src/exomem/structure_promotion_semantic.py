"""Advisory detection that a compiled page holds material its own shape rejects.

The semantic sibling of `structure_promotion`. That module is deliberately
lexical: at commit time it compares what a page SAYS it is about against the
terms its units carry, from state already in memory. It is right for the write
path and it is blind in one specific place — when the divergent material wears
the parent domain's vocabulary. A licence-administration note that absorbs
stopping-physics analysis, every unit tagged with the same licence words, has
nothing for a term comparison to catch.

This module reads geometry instead. The corpus already stores a vector per
durable unit (`semantic_unit_vectors`, written by the indexing pipeline from raw
unit text), so dispersion inside one page is measurable with no write-time
embedding, no model call, no new index, and no second opinion about what a unit
means. Vocabulary is demoted to what it is good at: naming the group after the
geometry has already found it.

The gate composition is v1's, with only the evidence swapped, and the constants
that express "enough material to justify its own note" and "enough of the
original subject left" are IMPORTED from v1 rather than restated — one
definition of child-note mass for both detectors.

Determinism is a hard requirement: fixed thresholds, no RNG, no clock, and
grouping whose result cannot depend on iteration order. Absence is never
evidence — a page whose units carry no stored vectors is not judged at all,
which is the difference between a sensor and a nag.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from .structure_promotion import (
    BREADTH_TAGS,
    CLUSTER_MIN_TERMS,
    CLUSTER_MIN_UNITS,
    MAX_CLUSTER_TERMS,
    MIN_RETAINED_UNITS,
    MISMATCH_MAX_OVERLAP,
    NAVIGATION_BASENAMES,
    TERM_RECURRENCE_MIN,
    _frontmatter_tags,
    _routed_terms,
    _terms,
)

KIND = "scope_divergence_semantic"
REASON_SEMANTIC_CLUSTER = "semantic_cluster_diverges"

#: Named so a skipped page reports WHY it was skipped instead of looking clean.
SKIP_UNIT_COUNT_OVER_CAP = "unit_count_over_cap"

# ---------------------------------------------------------------------------
# PROVISIONAL thresholds.
#
# These are PRODUCT constants, not the frozen falsification-bench budgets: moving
# one is a code change with its own evidence, never a §7 amendment. They are
# unvalidated against a corpus at scale — the calibration posture is that the
# tests pin behaviour AT each gate using constructed geometry, so a threshold can
# move without rewriting what the tests mean.
# ---------------------------------------------------------------------------

#: Two units join the same group at or above this cosine. Grouping is the cheap
#: pass; it decides candidacy, never divergence — `COHESION_MIN_COSINE` is what
#: decides whether the thing that formed is actually one thing.
LINK_MIN_COSINE = 0.50  # PROVISIONAL

#: φ — mean pairwise cosine inside a group. Linking is transitive and can chain a
#: string of loosely related units into one component; this rejects that chain.
COHESION_MIN_COSINE = 0.60  # PROVISIONAL

#: θ — cosine between a group's centroid and the page's identity remainder. Above
#: it the group is a sub-topic of the declared subject, not a rival to it.
SEPARATION_MAX_COSINE = 0.35  # PROVISIONAL

#: The pairwise pass is O(units²). Above this a page is skipped with a named note
#: rather than judged slowly or judged partially.
MAX_JUDGED_UNITS = 400  # PROVISIONAL


@dataclass(frozen=True, slots=True)
class JudgedUnit:
    """One unit that has BOTH a stored vector and a resolvable parse.

    Vectors supply the geometry, the parse supplies the label vocabulary, and a
    unit missing either side never reaches this type.
    """

    unit_ref: str
    tags: tuple[str, ...]
    vector: np.ndarray


@dataclass(frozen=True, slots=True)
class SemanticPageShape:
    """The minimum a page must expose to be judged geometrically."""

    title: str
    tags: tuple[str, ...]
    projects: tuple[str, ...]
    basename: str
    path: str
    units: tuple[JudgedUnit, ...]


@dataclass(frozen=True, slots=True)
class _DestinationPage:
    """A candidate home, shaped for v1's `_declared_identity` reader."""

    frontmatter: Mapping[Any, Any]
    title: str
    projects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DestinationCorpus:
    """The resolution context, in the shape v1's routing check already reads."""

    pages: dict[str, _DestinationPage]
    eligible_compiled_paths: frozenset[str]


def destination_corpus(frontmatters: Mapping[str, Mapping[Any, Any]]) -> DestinationCorpus:
    """Build the resolve-by-state-change context from eligible compiled pages."""
    pages: dict[str, _DestinationPage] = {}
    for path, frontmatter in frontmatters.items():
        raw = frontmatter.get("projects") or frontmatter.get("project") or ()
        if isinstance(raw, str):
            raw = (raw,)
        pages[path] = _DestinationPage(
            frontmatter=frontmatter,
            title=str(frontmatter.get("title") or ""),
            projects=tuple(str(item) for item in raw),
        )
    return DestinationCorpus(pages=pages, eligible_compiled_paths=frozenset(pages))


def _unit_matrix(units: tuple[JudgedUnit, ...]) -> np.ndarray:
    """L2-normalised rows, so a dot product IS the cosine.

    The pipeline writes normalised vectors, but normalising here keeps the gates
    honest against any producer that does not, instead of silently reading an
    inflated similarity as agreement.
    """
    matrix = np.stack([unit.vector for unit in units]).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.copyto(norms, 1.0, where=(norms == 0))
    return matrix / norms


def _groups(similarity: np.ndarray, threshold: float) -> list[list[int]]:
    """Connected components of the graph "cosine >= threshold".

    This is the fixed point of single-link agglomerative merging at `threshold`,
    which is why it needs no merge ORDER and therefore admits no tie-break
    ambiguity: the components are the same whichever pair is visited first. Union
    by smallest root and a sorted return make the emitted order stable too, so a
    group is identified by its earliest unit — anchor order, as the page is read.
    """
    count = similarity.shape[0]
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for index in range(count):
        neighbours = np.nonzero(similarity[index, index + 1 :] >= threshold)[0]
        for offset in neighbours:
            union(index, index + 1 + int(offset))

    grouped: dict[int, list[int]] = {}
    for index in range(count):
        grouped.setdefault(find(index), []).append(index)
    return [grouped[root] for root in sorted(grouped)]


def _cohesion(similarity: np.ndarray, members: list[int]) -> float:
    """Mean pairwise cosine inside one group; 1.0 for a group that cannot vary."""
    if len(members) < 2:
        return 1.0
    block = similarity[np.ix_(members, members)]
    total = float(block.sum() - np.trace(block))
    return total / (len(members) * (len(members) - 1))


def _centroid(matrix: np.ndarray, members: list[int]) -> np.ndarray:
    centroid = matrix[members].mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    return centroid / norm if norm else centroid


def _label_counts(
    units: tuple[JudgedUnit, ...], members: list[int], identity: frozenset[str]
) -> dict[str, int]:
    """Recurring group vocabulary that the page does NOT already declare.

    Two v1 rules, reused rather than re-invented: a term must recur across more
    than one unit (`TERM_RECURRENCE_MIN`) before it names anything, and terms the
    page already announces are excluded — a label made of the parent's own words
    would describe nothing and, worse, would let any page sharing that vocabulary
    resolve the advisory as though it were a home for the divergent material.
    """
    counts: dict[str, int] = {}
    for index in members:
        for term in _terms(units[index].tags) - identity:
            counts[term] = counts.get(term, 0) + 1
    return {term: count for term, count in counts.items() if count >= TERM_RECURRENCE_MIN}


def detect(shape: SemanticPageShape, *, corpus: Any = None) -> dict[str, Any] | None:
    """Decide whether `shape` holds a group its own declared scope rejects.

    Pure and deterministic: no I/O, no clock, no RNG. `corpus` is the resolution
    context; pass it and a group whose vocabulary eligible pages already own is
    treated as routed, omit it and the result is what it would have been without
    resolution.
    """
    if shape.basename.casefold() in NAVIGATION_BASENAMES:
        return None
    tag_terms = _terms(shape.tags)
    if tag_terms & BREADTH_TAGS:
        return None

    units = shape.units
    if not units:
        return None
    if len(units) > MAX_JUDGED_UNITS:
        # Named, not silent: a page nobody judged must not read as a clean one.
        return {"kind": KIND, "skipped": SKIP_UNIT_COUNT_OVER_CAP, "units": len(units)}

    identity = tag_terms | _terms([shape.title]) | _terms(shape.projects)
    matrix = _unit_matrix(units)
    similarity = matrix @ matrix.T

    candidates = [
        members
        for members in _groups(similarity, LINK_MIN_COSINE)
        if len(members) >= CLUSTER_MIN_UNITS
    ]
    if not candidates:
        return None

    # The page's identity remainder is what is left once every candidate group is
    # set aside. A page with nothing left has been renamed, not outgrown, which is
    # v1's retained-scope rule and the reason "separate" beats "rename".
    claimed = {index for members in candidates for index in members}
    remainder = [index for index in range(len(units)) if index not in claimed]
    if len(remainder) < MIN_RETAINED_UNITS:
        return None
    identity_centroid = _centroid(matrix, remainder)

    best: tuple[tuple[int, int], list[int], dict[str, int]] | None = None
    for members in candidates:
        if _cohesion(similarity, members) < COHESION_MIN_COSINE:
            continue
        separation = float(_centroid(matrix, members) @ identity_centroid)
        if separation > SEPARATION_MAX_COSINE:
            continue
        counts = _label_counts(units, members, identity)
        if corpus is not None and counts:
            routed = _routed_terms(
                corpus, exclude_path=shape.path, cluster_terms=frozenset(counts)
            )
            counts = {term: c for term, c in counts.items() if term not in routed}
        # v1's term floor, applied to what routing LEFT BEHIND. Nothing left to
        # name is nothing left to say, and neither is a residue: a destination
        # that covers part of a group's vocabulary must resolve the advice, not
        # shrink its label. The label set IS the fingerprint, so a shrunken label
        # is a new signal that would re-raise something the reader already acted
        # on — acting on advice must never be what brings it back.
        if len(counts) < CLUSTER_MIN_TERMS:
            continue
        ranked = (-len(members), members[0])
        if best is None or ranked < best[0]:
            best = (ranked, members, counts)

    if best is None:
        return None
    _ranked, members, counts = best

    # Most-recurrent first for SELECTION, alphabetical in the payload, so the
    # evidence is the strongest available and byte-stable across runs.
    top = sorted(counts, key=lambda term: (-counts[term], term))[:MAX_CLUSTER_TERMS]
    vocabulary = frozenset().union(*(_terms(units[index].tags) for index in members))
    overlap = len(vocabulary & identity) / len(vocabulary) if vocabulary else 1.0
    return {
        "kind": KIND,
        "strength": "strong" if overlap < MISMATCH_MAX_OVERLAP else "moderate",
        "reasons": [REASON_SEMANTIC_CLUSTER],
        "off_scope_units": len(members),
        "cluster_terms": sorted(top),
    }


def shape_from_parse(
    *,
    path: str,
    frontmatter: Mapping[Any, Any],
    units: Any,
    vectors_by_ref: Mapping[str, np.ndarray],
) -> SemanticPageShape:
    """Join a page's parsed units to its stored vectors by `unit_ref`.

    The sidecar rows carry no tags, so geometry and vocabulary have to be brought
    back together here. The join is the honest half of that: a vector row whose
    `unit_ref` no longer resolves against the current parse belongs to a
    superseded generation, and it is DROPPED from judgment rather than guessed
    at — an advisory built on units the page no longer has would be fiction.
    """
    judged: list[JudgedUnit] = []
    for unit in units:
        unit_ref = getattr(unit, "unit_ref", None)
        if not unit_ref:
            continue
        vector = vectors_by_ref.get(str(unit_ref))
        if vector is None:
            continue
        judged.append(
            JudgedUnit(str(unit_ref), tuple(getattr(unit, "tags", ()) or ()), vector)
        )
    raw = frontmatter.get("projects") or frontmatter.get("project") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return SemanticPageShape(
        title=str(frontmatter.get("title") or ""),
        tags=tuple(_frontmatter_tags(frontmatter)),
        projects=tuple(str(item) for item in raw),
        basename=path.rsplit("/", 1)[-1],
        path=path,
        units=tuple(judged),
    )
