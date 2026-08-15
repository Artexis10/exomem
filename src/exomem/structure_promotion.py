"""Advisory detection that a compiled page has outgrown its own declared scope.

The runtime measures; the agent reasons. This module answers exactly one question
about the page a caller just wrote: has recurring durable material accumulated that
sits outside what the page says it is about? It never proposes a destination, never
names another page, and never moves anything.

Everything it reads is already parsed and in memory at commit time — the page's
frontmatter, title, project keys, and semantic units. There is no file read, no
database access, no embedding, and no model call, so the result is deterministic and
the cost is a bounded pass over units the write already produced.

The signal is convergent by construction. A single tangent cannot trigger it: a term
only groups material when it recurs across more than one off-scope unit, the group
must reach enough mass to justify its own note, and the page must still hold its
original subject. Raw length is never an input, because a long note about one thing
is not structural debt.

Known approximation: recurrence is counted over durable units, not over write events.
There is no cheap per-page record of which units a given mutation added, and units are
what writes deposit, so the reason codes speak of units and claim nothing more.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KIND = "scope_divergence"

#: A page must carry at least this many durable units before its shape means anything.
MIN_UNITS = 6
#: A page that declares almost nothing about itself cannot be judged against itself.
MIN_IDENTITY_TERMS = 2
#: A unit needs this many out-of-scope terms before one stray word can push it out.
MIN_OFF_SCOPE_TERMS = 2
#: A term groups material only when it recurs — this is the recurrence requirement.
TERM_RECURRENCE_MIN = 2
#: How much material must gather before a focused child note would actually help.
CLUSTER_MIN_UNITS = 5
CLUSTER_MIN_TERMS = 4
#: Enough of the original subject must remain for "separate" to beat "rename".
MIN_RETAINED_UNITS = 3
#: Above this overlap the group is a sub-topic of the declared subject, not a rival.
MISMATCH_MAX_OVERLAP = 0.34
#: Bound on the evidence returned to the caller.
MAX_CLUSTER_TERMS = 6

REASON_RECURS = "off_scope_cluster_recurs"
REASON_MASS = "cluster_reaches_child_note_mass"
REASON_RETAINED = "page_retains_original_scope"
REASON_MISMATCH = "declared_scope_mismatch"

#: Pages that deliberately announce breadth. Same convention that already exempts
#: these tags from staleness pressure and semantic-unit requirements elsewhere.
BREADTH_TAGS = frozenset({"hub", "snapshot"})

#: Filenames the corpus already treats as navigation rather than knowledge.
NAVIGATION_BASENAMES = frozenset({"index.md", "log.md"})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Title words that carry no subject. Deliberately small: this is a stop list for
# glue, not an attempt to model English.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in",
        "into", "is", "it", "its", "of", "on", "or", "our", "over", "the", "their",
        "this", "to", "via", "was", "were", "what", "when", "which", "why", "with",
        "note", "notes", "plan", "plans", "draft", "overview", "summary",
    }
)


def _terms(values: Iterable[str]) -> frozenset[str]:
    """Normalise tags, title words, and project keys into one comparable vocabulary."""
    out: set[str] = set()
    for value in values:
        for token in _TOKEN_SPLIT.split(str(value).casefold()):
            if len(token) > 2 and token not in _STOPWORDS and not token.isdigit():
                out.add(token)
    return frozenset(out)


def _frontmatter_tags(frontmatter: Mapping[Any, Any]) -> list[str]:
    raw = frontmatter.get("tags")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return []


@dataclass(frozen=True, slots=True)
class PageShape:
    """The minimum a page must expose to be judged. Deliberately not a vault type."""

    page_type: str | None
    title: str
    tags: tuple[str, ...]
    projects: tuple[str, ...]
    basename: str
    unit_tags: tuple[tuple[str, ...], ...]


def _cluster(off_scope: Sequence[frozenset[str]]) -> tuple[list[int], frozenset[str]]:
    """Group off-scope units by vocabulary that recurs across more than one of them.

    Returns the largest group's unit indices and the recurring terms that formed it.
    A term appearing in only one unit joins nothing — that is what keeps an
    incidental aside from ever looking like an emerging project.
    """
    counts: dict[str, int] = {}
    for terms in off_scope:
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
    recurring = {term for term, count in counts.items() if count >= TERM_RECURRENCE_MIN}
    if not recurring:
        return [], frozenset()

    # Union-find over units that share at least one recurring term.
    parent = list(range(len(off_scope)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    first_seen: dict[str, int] = {}
    for index, terms in enumerate(off_scope):
        for term in terms & recurring:
            if term in first_seen:
                union(first_seen[term], index)
            else:
                first_seen[term] = index

    groups: dict[int, list[int]] = {}
    for index, terms in enumerate(off_scope):
        if terms & recurring:
            groups.setdefault(find(index), []).append(index)
    if not groups:
        return [], frozenset()

    # Largest group wins; ties break on the earliest unit so the result is stable.
    best_root = min(groups, key=lambda root: (-len(groups[root]), groups[root][0]))
    members = groups[best_root]
    seeds = frozenset(
        term for index in members for term in off_scope[index] & recurring
    )
    return members, seeds


def detect(shape: PageShape) -> dict[str, Any] | None:
    """Decide whether `shape` has outgrown its declared scope. Pure; no I/O."""
    if shape.page_type not in _compiled_types():
        return None
    if shape.basename.casefold() in NAVIGATION_BASENAMES:
        return None
    if len(shape.unit_tags) < MIN_UNITS:
        return None

    tag_terms = _terms(shape.tags)
    if tag_terms & BREADTH_TAGS:
        return None

    identity = tag_terms | _terms([shape.title]) | _terms(shape.projects)
    if len(identity) < MIN_IDENTITY_TERMS:
        return None

    off_scope: list[frozenset[str]] = []
    off_scope_all_terms: list[frozenset[str]] = []
    retained = 0
    for tags in shape.unit_tags:
        terms = _terms(tags)
        if not terms:
            continue
        outside = terms - identity
        inside = terms & identity
        if len(outside) > len(inside) and len(outside) >= MIN_OFF_SCOPE_TERMS:
            off_scope.append(outside)
            off_scope_all_terms.append(terms)
        else:
            retained += 1

    if not off_scope:
        return None

    members, seeds = _cluster(off_scope)
    if not members:
        return None

    reasons = [REASON_RECURS]
    if len(members) >= CLUSTER_MIN_UNITS and len(seeds) >= CLUSTER_MIN_TERMS:
        reasons.append(REASON_MASS)
    if retained >= MIN_RETAINED_UNITS:
        reasons.append(REASON_RETAINED)

    # All three gates must hold. Mass alone is noise; without retained scope the page
    # has simply moved on, which is a naming problem rather than a splitting one.
    if REASON_MASS not in reasons or REASON_RETAINED not in reasons:
        return None

    group_vocabulary = frozenset().union(*(off_scope_all_terms[i] for i in members))
    overlap = len(group_vocabulary & identity) / len(group_vocabulary)
    if overlap < MISMATCH_MAX_OVERLAP:
        reasons.append(REASON_MISMATCH)

    # Most-recurrent terms first for selection, alphabetical for the payload, so the
    # evidence is both the strongest available and byte-stable across runs.
    seed_counts = {
        term: sum(1 for index in members if term in off_scope[index]) for term in seeds
    }
    top = sorted(seed_counts, key=lambda term: (-seed_counts[term], term))
    return {
        "kind": KIND,
        "strength": "strong" if REASON_MISMATCH in reasons else "moderate",
        "reasons": sorted(reasons),
        "off_scope_units": len(members),
        "cluster_terms": sorted(top[:MAX_CLUSTER_TERMS]),
    }


def _compiled_types() -> frozenset[str]:
    from . import semantic_contract

    return semantic_contract.COMPILED_TYPES


def shape_from_state(state: Any) -> PageShape:
    """Adapt a `SemanticPageState` the write path already built."""
    frontmatter = state.frontmatter or {}
    return PageShape(
        page_type=state.page_type,
        title=state.title or "",
        tags=tuple(_frontmatter_tags(frontmatter)),
        projects=tuple(state.projects or ()),
        basename=state.path.rsplit("/", 1)[-1],
        unit_tags=tuple(tuple(unit.tags) for unit in state.document.units),
    )


def suggest_for_state(state: Any) -> dict[str, Any] | None:
    """Detect over a page state the caller already holds."""
    return detect(shape_from_state(state))


def suggest_for_page(vault_root: Path, rel_path: str) -> dict[str, Any] | None:
    """Detect over a page on disk. For out-of-band callers; the write path uses state."""
    from . import semantic_units, vault

    source = (vault_root / rel_path).read_text(encoding="utf-8")
    frontmatter, body, _ = vault.parse_frontmatter(source)
    document = semantic_units.parse_semantic_units(body, path=rel_path, validate=False)
    projects = frontmatter.get("projects") or frontmatter.get("project") or ()
    if isinstance(projects, str):
        projects = (projects,)
    return detect(
        PageShape(
            page_type=frontmatter.get("type"),
            title=str(frontmatter.get("title") or ""),
            tags=tuple(_frontmatter_tags(frontmatter)),
            projects=tuple(str(item) for item in projects),
            basename=rel_path.rsplit("/", 1)[-1],
            unit_tags=tuple(tuple(unit.tags) for unit in document.units),
        )
    )
