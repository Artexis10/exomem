"""Deterministic routing over audience-filtered collection claims."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .structure_promotion import _terms

MIN_CLAIM_COVERAGE = 2  # PROVISIONAL
MAX_MATCHED_TERMS = 6

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    collection: str
    title: str
    claims: frozenset[str]
    natural_key: tuple[str, ...]
    natural_key_types: tuple[str, ...] = ()
    natural_key_values: frozenset[str] = frozenset()


def normalize_text(value: object) -> str:
    """Normalize claim-bearing text without changing its authored storage."""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def normalize_terms(values: Iterable[object]) -> frozenset[str]:
    """Return claim terms after compatibility normalization."""
    return _terms(normalize_text(value) for value in values)


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
    ceiling = (total_targets + 1) // 2
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
    terms: Iterable[str], targets: Iterable[RoutingTarget]
) -> dict[str, object] | None:
    """Return one strict claims winner with a subject signal, else stay silent."""
    raw_terms = [str(value) for value in terms]
    normalized = normalize_terms(raw_terms)
    target_claims = [(target, normalize_terms(target.claims)) for target in targets]
    frequency = _claim_document_frequency([claims for _, claims in target_claims])
    total_targets = len(target_claims)
    ranked = sorted(
        (
            (len(normalized & claims), target.collection, target, claims)
            for target, claims in target_claims
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
