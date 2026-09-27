"""Deterministic routing over audience-filtered collection claims.

Both sides of the comparison -- a collection's claims and an observation's
terms -- are reduced the same way before they meet: compatibility-normalised,
split into words, stripped of closed-class function words
(`structure_promotion.FUNCTION_WORDS`) and folded to one inflection
(`vocabulary_fold.fold_term`). None of it changes authored storage; it decides
only which spellings count as the same term.

A collection may also declare `claims.match` frontmatter predicates. A page
satisfying every predicate belongs to that collection by declaration, so it
routes there as `strong` whatever its words share.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .structure_promotion import FUNCTION_WORDS, _terms
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
    """Return comparable claim terms: normalised, function words dropped, folded."""
    out: set[str] = set()
    for token in _terms(normalize_text(value) for value in values):
        folded = fold_term(token)
        if len(folded) > 2 and folded not in FUNCTION_WORDS:
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


def route(
    terms: Iterable[str],
    targets: Iterable[RoutingTarget],
    *,
    facets: Mapping[str, Iterable[object]] | None = None,
) -> dict[str, object] | None:
    """Return one strict claims winner, or stay silent on ties and misses.

    A collection whose `match` predicates all hold wins as `strong` before any
    word coverage is counted; two such collections are ranked by coverage and
    stay silent on a tie. Predicates only widen: a page that fails them still
    routes by coverage exactly as before.
    """
    raw_terms = [str(value) for value in terms]
    normalized = normalize_terms(raw_terms)
    targets = list(targets)
    observed = normalize_match(facets)
    declared = sorted(
        (
            (
                len(normalized & normalize_terms(target.claims)),
                target.collection,
                target,
                evidence,
            )
            for target in targets
            if (evidence := matched_predicates(target, observed)) is not None
        ),
        key=lambda row: (-row[0], row[1]),
    )
    if declared:
        if len(declared) > 1 and declared[0][0] == declared[1][0]:
            return None
        _coverage, _name, target, evidence = declared[0]
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
    ranked = sorted(
        (
            (
                len(normalized & normalize_terms(target.claims)),
                target.collection,
                target,
            )
            for target in targets
        ),
        key=lambda row: (-row[0], row[1]),
    )
    if not ranked or ranked[0][0] < MIN_CLAIM_COVERAGE:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    target = ranked[0][2]
    claims = normalize_terms(target.claims)
    return {
        "collection": target.collection,
        "title": target.title,
        "matched_terms": sorted(normalized & claims)[:MAX_MATCHED_TERMS],
        "natural_key": list(target.natural_key),
        "strength": "strong" if _strong(raw_terms, target) else "moderate",
    }
