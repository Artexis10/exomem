"""Deterministic routing over audience-filtered collection claims."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
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


def route(
    terms: Iterable[str], targets: Iterable[RoutingTarget]
) -> dict[str, object] | None:
    """Return one strict claims winner, or stay silent on ties and misses."""
    raw_terms = [str(value) for value in terms]
    normalized = normalize_terms(raw_terms)
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
