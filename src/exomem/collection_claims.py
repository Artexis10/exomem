"""Deterministic routing over audience-filtered collection claims.

Both sides of the comparison -- a collection's claims and an observation's
terms -- are reduced the same way before they meet: compatibility-normalised,
split into words, stripped of closed-class function words and navigation glue
(`structure_promotion._STOPWORDS`) and folded to one inflection
(`vocabulary_fold.fold_term`). None of it changes authored storage; it decides
only which spellings count as the same term.

A collection may also declare `claims.match` frontmatter predicates. A page
satisfying every predicate belongs to that collection by declaration, so it
routes there as `strong` whatever its words share. A page whose `type` or
`project` contradicts the declaration (another project, say) never routes
there, not even by shared words.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .structure_promotion import _STOPWORDS, _terms
from .vocabulary_fold import fold_term

MIN_CLAIM_COVERAGE = 2  # PROVISIONAL
MAX_MATCHED_TERMS = 6
#: Page facets a `claims.match` predicate may test (see structured_collections).
MATCH_KEYS = ("type", "category", "project", "tags")

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    collection: str
    title: str
    claims: frozenset[str]
    natural_key: tuple[str, ...]
    natural_key_types: tuple[str, ...] = ()
    natural_key_values: frozenset[str] = frozenset()
    #: Folded `claims.match` predicates: every key must hold, any value may match.
    match: Mapping[str, frozenset[str]] = field(default_factory=dict)


def normalize_text(value: object) -> str:
    """Normalize claim-bearing text without changing its authored storage."""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def normalize_terms(values: Iterable[object]) -> frozenset[str]:
    """Return comparable claim terms: normalised, function words dropped, folded.

    The folded token passes the same filter `_terms` applies to raw tokens, so
    normalising stored terms again returns them unchanged.
    """
    out: set[str] = set()
    for token in _terms(normalize_text(value) for value in values):
        folded = fold_term(token)
        if len(folded) > 2 and folded not in _STOPWORDS and not folded.isdigit():
            out.add(folded)
    return frozenset(out)


def fold_value(value: object) -> str:
    """One whole facet or predicate value as a comparison key."""
    return fold_term(normalize_text(value))


def normalize_match(values: Mapping[str, Iterable[object]] | None) -> dict[str, frozenset[str]]:
    """Fold declared predicates, or observed facets, into comparable value sets."""
    out: dict[str, frozenset[str]] = {}
    for key in MATCH_KEYS:
        raw = (values or {}).get(key)
        if raw is None:
            continue
        items = [raw] if isinstance(raw, (str, bytes)) else list(raw)
        folded = frozenset(
            text for item in items if type(item) in {str, int, float, bool}
            if (text := fold_value(item))
        )
        if folded:
            out[key] = folded
    return out


def matched_predicates(
    target: RoutingTarget, facets: Mapping[str, frozenset[str]]
) -> list[str] | None:
    """`key:value` evidence when every declared predicate holds, else None."""
    if not target.match:
        return None
    evidence: list[str] = []
    for key, allowed in sorted(target.match.items()):
        hits = sorted(facets.get(key, frozenset()) & allowed)
        if not hits:
            return None
        evidence.append(f"{key}:{hits[0]}")
    return evidence


#: The keys that say what a page IS. Only these can contradict a declaration;
#: `tags` and `category` are open and multi-valued, so a page carrying other
#: values than the declared ones has simply not said it belongs.
IDENTITY_KEYS = ("type", "project")
#: A layer type says where a page lives -- an Evidence sidecar is `type:
#: source` -- not what it is about, so for contradiction it is silence.
LAYER_TYPES = frozenset({"source"})


def contradicts(target: RoutingTarget, facets: Mapping[str, frozenset[str]]) -> bool:
    """Whether the page states an identity value the collection's `match` excludes.

    Silence about a key is not a contradiction: a page with no `project` can
    still route by coverage to a collection that declares one.
    """
    for key in IDENTITY_KEYS:
        allowed = target.match.get(key)
        stated = facets.get(key, frozenset())
        if key == "type":
            stated = stated - LAYER_TYPES
        if allowed and stated and not stated & allowed:
            return True
    return False


def _matches_type(value: str, kind: str) -> bool:
    text = normalize_text(value)
    if kind == "date":
        try:
            dt.date.fromisoformat(text)
        except ValueError:
            return False
        return True
    if kind == "datetime":
        try:
            dt.datetime.fromisoformat(
                f"{text[:-1]}+00:00" if text.endswith("z") else text
            )
        except ValueError:
            return False
        return True
    if kind == "link":
        return (
            (text.startswith("[[") and text.endswith("]]"))
            or text.startswith("exomem://")
            or text.startswith("https://")
            or text.startswith("http://")
        )
    if kind in {"integer", "number"}:
        return _NUMBER.fullmatch(text) is not None
    if kind == "boolean":
        return text.casefold() in {"true", "false"}
    return False


def _strong(raw_terms: Sequence[str], target: RoutingTarget) -> bool:
    observed = {normalize_text(value) for value in target.natural_key_values}
    return any(
        normalize_text(value) in observed
        or any(_matches_type(value, kind) for kind in target.natural_key_types)
        for value in raw_terms
    )


def _claim_document_frequency(
    claim_sets: Sequence[frozenset[str]],
) -> Counter[str]:
    """Count how many of this call's targets declare each claim term.

    The population is exactly the targets `route` was asked to choose among --
    the live set of active Records collections at call time. That is a real,
    per-call corpus statistic, not a fixed list: a term every collection
    declares says nothing about which one an observation belongs to, while a
    term only a minority declare is meaningful evidence for its collection.
    """
    frequency: Counter[str] = Counter()
    for claims in claim_sets:
        frequency.update(claims)
    return frequency


def _distinctive_matched_terms(
    matched: frozenset[str], frequency: Counter[str], total_targets: int
) -> frozenset[str]:
    """Matched terms not shared by a majority of this call's routing targets."""
    # A singleton has no comparative population. With multiple targets, a
    # term shared by more than half cannot distinguish the collection.
    ceiling = max(1, total_targets // 2)
    return frozenset(term for term in matched if frequency[term] <= ceiling)


def _has_subject_signal(
    raw_terms: Sequence[str],
    target: RoutingTarget,
    matched: frozenset[str],
    frequency: Counter[str],
    total_targets: int,
) -> bool:
    """Require a subject signal, not bare generic-term overlap, to route.

    A signal is one of: a shared anchor (`_strong`'s natural-key match), the
    observation naming the collection's own subject (its title), or matched
    claim terms distinctive to this collection among the current routing
    corpus. Terms most or all targets share carry none of these alone.
    """
    if _strong(raw_terms, target):
        return True
    if normalize_text(target.title) in normalize_terms(raw_terms):
        return True
    return bool(_distinctive_matched_terms(matched, frequency, total_targets))


def route(
    terms: Iterable[str],
    targets: Iterable[RoutingTarget],
    *,
    facets: Mapping[str, Iterable[object]] | None = None,
) -> dict[str, object] | None:
    """Return one strict claims winner with a subject signal, else stay silent.

    A collection whose `match` predicates all hold wins as `strong` before any
    word coverage is counted. Several such collections rank by the number of
    predicates held, then by coverage, and stay silent on a tie. Otherwise the
    coverage route runs over every collection the page does not contradict.
    """
    raw_terms = [str(value) for value in terms]
    normalized = normalize_terms(raw_terms)
    targets = list(targets)
    observed = normalize_match(facets)
    declared = sorted(
        (
            (
                len(evidence),
                len(normalized & normalize_terms(target.claims)),
                target.collection,
                target,
                evidence,
            )
            for target in targets
            if (evidence := matched_predicates(target, observed)) is not None
        ),
        key=lambda row: (-row[0], -row[1], row[2]),
    )
    if declared:
        if len(declared) > 1 and declared[0][:2] == declared[1][:2]:
            return None
        _held, _coverage, _name, target, evidence = declared[0]
        return {
            "collection": target.collection,
            "title": target.title,
            "matched_terms": sorted(normalized & normalize_terms(target.claims))[
                :MAX_MATCHED_TERMS
            ],
            "matched_predicates": evidence,
            "natural_key": list(target.natural_key),
            "strength": "strong",
        }
    target_claims = [(target, normalize_terms(target.claims)) for target in targets]
    # Distinctiveness is judged against every target, contradicted or not, so
    # excluding a collection never makes a shared term look rarer than it is.
    frequency = _claim_document_frequency([claims for _, claims in target_claims])
    total_targets = len(target_claims)
    ranked = sorted(
        (
            (len(normalized & claims), target.collection, target, claims)
            for target, claims in target_claims
            if not contradicts(target, observed)
        ),
        key=lambda row: (-row[0], row[1]),
    )
    if not ranked or ranked[0][0] < MIN_CLAIM_COVERAGE:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    _, _, target, claims = ranked[0]
    matched = normalized & claims
    if not _has_subject_signal(raw_terms, target, matched, frequency, total_targets):
        return None
    return {
        "collection": target.collection,
        "title": target.title,
        "matched_terms": sorted(matched)[:MAX_MATCHED_TERMS],
        "natural_key": list(target.natural_key),
        "strength": "strong" if _strong(raw_terms, target) else "moderate",
    }
