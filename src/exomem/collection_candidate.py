"""Pure recurrence sensor for domains that may warrant a Records collection."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import collection_claims
from .structure_promotion import BREADTH_TAGS

SPREAD_MIN_PAGES = 3  # PROVISIONAL
DATES_MIN = 3  # PROVISIONAL
SPAN_MIN_DAYS = 14  # PROVISIONAL
STATE_UNITS_MIN = 3  # PROVISIONAL
IDENTITIES_MIN = 2  # PROVISIONAL
MAX_DOMAIN_TERMS = 6  # PROVISIONAL
MAX_EVIDENCE_UNITS = 8  # PROVISIONAL

STATE_LEXEMES: tuple[str, ...] = (  # PROVISIONAL
    "activate",
    "activated",
    "cancel",
    "cancelled",
    "changed",
    "expired",
    "paid",
    "purchase",
    "purchased",
    "refund",
    "refunded",
    "renew",
    "renewed",
    "started",
    "stopped",
)

CORE_CATEGORIES = frozenset(
    {
        "decision",
        "experiment",
        "failure",
        "finding",
        "hypothesis",
        "observation",
        "operating-constraint",
        "pattern",
        "prediction",
        "question",
        "research",
    }
)

_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_CURRENCY = re.compile(
    r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|\b\d+(?:[.,]\d{1,2})?\s?(?:USD|EUR|GBP)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Candidate:
    term: str
    domain_terms: tuple[str, ...]
    evidence_units: tuple[str, ...]
    strength: str
    signal_version: str


@dataclass(frozen=True, slots=True)
class _Unit:
    page: str
    unit_ref: str
    terms: frozenset[str]
    date: dt.date
    text: str


def _day(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _canonical_term(value: Any) -> str:
    return re.sub(r"\s+", "-", collection_claims.normalize_text(value))


def _units(rows: Iterable[Mapping[str, Any]]) -> list[_Unit]:
    units: list[_Unit] = []
    for row in rows:
        page = str(row.get("page") or "")
        unit_ref = str(row.get("unit_ref") or "")
        day = _day(row.get("date"))
        raw_terms = row.get("terms")
        if (
            not page
            or not unit_ref
            or day is None
            or isinstance(raw_terms, (str, bytes))
            or not isinstance(raw_terms, Iterable)
        ):
            continue
        terms = frozenset(
            term
            for value in raw_terms
            if (term := _canonical_term(value))
        )
        if terms:
            units.append(_Unit(page, unit_ref, terms, day, str(row.get("text") or "")))
    return sorted(units, key=lambda unit: (unit.date, unit.page, unit.unit_ref))


def _state_unit(unit: _Unit) -> bool:
    lowered = unit.text.casefold()
    return bool(
        _ISO_DATE.search(unit.text)
        or _CURRENCY.search(unit.text)
        or any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in STATE_LEXEMES)
    )


def _excluded(
    term: str,
    *,
    covered_terms: set[str],
    project_terms: set[str],
    core_categories: set[str],
) -> bool:
    normalized = set(collection_claims.normalize_terms([term]))
    return bool(
        term in BREADTH_TAGS
        or term in project_terms
        or term in core_categories
        or term in STATE_LEXEMES
        or term in covered_terms
        or (normalized and normalized <= covered_terms)
    )


def detect(
    rows: Iterable[Mapping[str, Any]],
    *,
    covered_terms: Iterable[str] = (),
    project_terms: Iterable[str] = (),
    core_categories: Iterable[str] = CORE_CATEGORIES,
) -> list[Candidate]:
    """Return deterministic candidates from a bounded authored-units table."""
    units = _units(rows)
    covered = {_canonical_term(term) for term in covered_terms}
    projects = {_canonical_term(term) for term in project_terms}
    core = {_canonical_term(term) for term in core_categories}
    all_terms = sorted({term for unit in units for term in unit.terms})
    candidates: list[Candidate] = []
    for term in all_terms:
        if _excluded(
            term,
            covered_terms=covered,
            project_terms=projects,
            core_categories=core,
        ):
            continue
        supporting = [unit for unit in units if term in unit.terms]
        pages = {unit.page for unit in supporting}
        dates = {unit.date for unit in supporting}
        state_units = [unit for unit in supporting if _state_unit(unit)]
        if (
            len(pages) < SPREAD_MIN_PAGES
            or len(dates) < DATES_MIN
            or (max(dates) - min(dates)).days < SPAN_MIN_DAYS
            or len(state_units) < STATE_UNITS_MIN
        ):
            continue
        identity_counts: dict[str, set[str]] = {}
        for unit in supporting:
            for other in unit.terms - {term}:
                if _excluded(
                    other,
                    covered_terms=covered,
                    project_terms=projects,
                    core_categories=core,
                ):
                    continue
                identity_counts.setdefault(other, set()).add(unit.page)
        identities = sorted(
            (other for other, seen in identity_counts.items() if len(seen) >= 2),
            key=lambda other: (-len(identity_counts[other]), other),
        )
        if len(identities) < IDENTITIES_MIN:
            continue
        evidence = tuple(unit.unit_ref for unit in supporting[:MAX_EVIDENCE_UNITS])
        signal_version = hashlib.sha256(
            json.dumps(sorted(unit.unit_ref for unit in supporting), separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        candidates.append(
            Candidate(
                term=term,
                domain_terms=tuple([term, *identities][:MAX_DOMAIN_TERMS]),
                evidence_units=evidence,
                strength=(
                    "strong"
                    if len(pages) >= 2 * SPREAD_MIN_PAGES
                    or len(state_units) >= 2 * STATE_UNITS_MIN
                    else "moderate"
                ),
                signal_version=signal_version,
            )
        )
    return candidates
