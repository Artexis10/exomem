"""Pure, PROVISIONAL English predicates for authored role and current-state review.

Cue normalization is deliberately separate from represented-text equality. These
finite grammars report review evidence, never semantic equivalence or authority.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FAMILIES = ("artifact_role_promotion", "transient_state_review")
PREDICATE_VERSION = 1
MAX_UNITS = 400
MAX_UNIT_CHARS = 8192
MAX_EVIDENCE = 8
MAX_CANDIDATES = 8
MAX_SUPPORTS = 8
MAX_DEPENDENT_ORIGINS = 8
REUSABLE_CUES = ("reusable", "repeatable", "use this method", "use this procedure")
SUCCESS_CUES = ("worked well", "succeeded", "successful outcome", "repeatable success")
SYNTHESIS_CUES = ("taken together", "across these sources", "across the studies", "both sources")
EXCLUSIONS = frozenset("if might may planned will not never failed failure".split())
HISTORICAL = ("previously", "at that time", "before", "earlier")
STOPWORDS = frozenset("a an the this that these those is are was were for of to and or".split())
_PENDING = re.compile(
    r"(?:no (?:(?P<no_subject>[\w-]+(?: [\w-]+){0,3}) )?"
    r"(?:results?|conclusions?)(?: exists?| is available| are available)? yet|"
    r"awaiting (?:(?P<await_subject>[\w-]+(?: [\w-]+){0,3}) )?(?:results?|conclusions?))"
)
_RESULT_LABEL = re.compile(
    r"^(?:(?P<subject>[\w-]+(?: [\w-]+){0,3}) )?(?:results?|conclusions?)\s*:"
)
_EPISODE = re.compile(r"\b(?:trial|run|batch)\s+[\w-]+\b")


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def represented_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def clauses(unit: Any) -> tuple[str, ...]:
    authored = []
    fence = None
    for line in unit.content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence or stripped.startswith(">"):
            continue
        # Inline quotation cannot provide an authored qualifying clause.
        stripped = re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`', "", stripped)
        authored.append(stripped)
    return tuple(normalized(c) for c in re.split(r"[.!?\n]+", "\n".join(authored)) if c.strip())


def _cue(unit: Any, cues: tuple[str, ...]) -> bool:
    return any(
        not (set(re.findall(r"\b\w+\b", clause)) & EXCLUSIONS)
        and any(re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", clause) for cue in cues)
        for clause in clauses(unit)
    )


def procedure(unit: Any) -> bool:
    return unit.kind == "procedure" or unit.category in {"procedure", "technique"}


def outcome(unit: Any) -> bool:
    return unit.kind == "result" or unit.category in {"result", "outcome"}


def synthesis(unit: Any) -> bool:
    return unit.category in {"finding", "inference", "synthesis"}


def unit_identity(unit: Any) -> str:
    return str(unit.unit_ref or unit.anchor or f"{unit.fingerprint}:{unit.occurrence}")


@dataclass(frozen=True)
class Candidate:
    family: str
    role: str
    partition: str
    signal_version: str
    evidence_refs: tuple[str, ...]
    reason: str
    units: tuple[Any, ...]
    sources: tuple[str, ...] = ()
    material_version: str = ""


@dataclass(frozen=True)
class Detection:
    candidates: tuple[Candidate, ...]
    coverage: Mapping[str, str]


def _candidate(
    page: Any,
    family: str,
    role: str,
    units: tuple[Any, ...],
    reason: str,
    sources: tuple[str, ...] = (),
) -> Candidate:
    identities = tuple(sorted(unit_identity(u) for u in units))
    material = sorted(
        (
            unit_identity(u),
            represented_text(u.content),
            normalized(u.context or ""),
            u.category,
            u.kind,
            u.verdict,
        )
        for u in units
    )
    material_version = _hash((PREDICATE_VERSION, material))
    return Candidate(
        family,
        role,
        _hash((family, page.identity, role, identities)),
        _hash((material_version, sources)),
        identities[:MAX_EVIDENCE],
        reason,
        units,
        sources,
        material_version,
    )


def _historical_or_future(unit: Any) -> bool:
    context = normalized(unit.context or "")
    tokens = set(re.findall(r"\b\w+\b", context))
    return bool(tokens & (EXCLUSIONS | {"future"})) or any(
        re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", context) for marker in HISTORICAL
    )


def _pending_subject(unit: Any) -> str | None:
    if _historical_or_future(unit):
        return None
    for clause in clauses(unit):
        match = _PENDING.fullmatch(clause)
        if match:
            subject = match["no_subject"] or match["await_subject"] or ""
            if not set(subject.split()) & STOPWORDS:
                return subject
    return None


def _result_subject(unit: Any) -> str | None:
    if (
        not outcome(unit)
        or unit.verdict in {"retracted", "false", "failed"}
        or _historical_or_future(unit)
    ):
        return None
    authored = clauses(unit)
    if _pending_subject(unit) is not None or any(
        _PENDING.fullmatch(c.rsplit(":", 1)[-1].strip()) for c in authored
    ):
        return None
    if not authored or any(set(re.findall(r"\b\w+\b", c)) & EXCLUSIONS for c in authored):
        return None
    match = _RESULT_LABEL.match(authored[0])
    if match:
        return match["subject"] or ""
    title = normalized(unit.title or "")
    match = re.fullmatch(r"(?:result|outcome)\s*[:—-]\s*(.+)", title)
    if match:
        return match[1]
    return ""


def detect(
    page: Any,
    *,
    source_documents: Mapping[str, tuple[str, ...]] | None = None,
    exact_links: Mapping[str, tuple[str, ...]] | None = None,
    superseded_refs: frozenset[str] = frozenset(),
    candidate_limit: int | None = MAX_CANDIDATES,
) -> Detection:
    """Classify immutable parsed units and already-resolved, audience-readable links."""
    coverage = dict.fromkeys(FAMILIES, "complete")
    if (
        not page.eligible_compiled
        or page.page_type != "experiment"
        or page.status not in {None, "", "active", "ongoing"}
    ):
        return Detection((), coverage)
    all_units = page.document.units
    if len(all_units) > MAX_UNITS or any(
        len(u.content) + len(u.context or "") > MAX_UNIT_CHARS for u in all_units
    ):
        return Detection((), dict.fromkeys(FAMILIES, "capped"))
    units = []
    seen = set()
    for unit in all_units:
        signature = (unit.category, unit.content, unit.context)
        if signature not in seen and unit_identity(unit) not in superseded_refs:
            seen.add(signature)
            units.append(unit)
    exact_links = exact_links or {}
    sources = source_documents or {}
    methods = [u for u in units if procedure(u) and _cue(u, REUSABLE_CUES)]
    results = [
        u
        for u in units
        if outcome(u)
        and u.verdict not in {"retracted", "false", "failed"}
        and not _historical_or_future(u)
        and _cue(u, SUCCESS_CUES)
    ]
    contexts = {id(u): normalized(u.context or "") for u in units}
    procedure_counts = Counter(contexts[id(u)] for u in units if procedure(u))
    outcome_counts = Counter(contexts[id(u)] for u in units if outcome(u))
    methods_by_ref = {unit_identity(u): u for u in methods}
    unique_methods = {contexts[id(u)]: u for u in methods if procedure_counts[contexts[id(u)]] == 1}
    candidates = []
    for result in results:
        joined = {
            ref: methods_by_ref[ref]
            for ref in exact_links.get(unit_identity(result), ())
            if ref in methods_by_ref
        }
        context = contexts[id(result)]
        if context and outcome_counts[context] == 1 and context in unique_methods:
            method = unique_methods[context]
            joined[unit_identity(method)] = method
        for method in joined.values():
            candidates.append(
                _candidate(
                    page,
                    FAMILIES[0],
                    "reusable_method",
                    (method, result),
                    "reusable_method_with_success",
                )
            )
    for unit in units:
        documents = tuple(sorted(set(sources.get(unit_identity(unit), ()))))
        if synthesis(unit) and _cue(unit, SYNTHESIS_CUES) and len(documents) >= 2:
            candidates.append(
                _candidate(
                    page,
                    FAMILIES[0],
                    "research_synthesis",
                    (unit,),
                    "attributed_multi_source_synthesis",
                    documents,
                )
            )
    pending = [(u, subject) for u in units if (subject := _pending_subject(u)) is not None]
    observed = [(u, subject) for u in units if (subject := _result_subject(u)) is not None]
    episode_text = normalized(" ".join(u.content + " " + (u.context or "") for u in units))
    episodes = set(_EPISODE.findall(episode_text))
    authored_labels = re.findall(r"\b(?:trial|run|batch)\s+(\S+)", episode_text)
    supported_labels = all(re.fullmatch(r"[\w-]+[.,:;!?]?", label) for label in authored_labels)
    result_subjects: dict[str, set[str]] = {}
    qualified_pending = set()
    for unit, subject in observed:
        result_subjects.setdefault(contexts[id(unit)], set()).add(subject)
    for unit, subject in pending:
        if subject:
            qualified_pending.add(contexts[id(unit)])
    dates = {}
    for unit, _ in [*pending, *observed]:
        authored = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", unit.context or "")
        if len(authored) == 1:
            try:
                dates[id(unit)] = dt.date.fromisoformat(authored[0])
            except ValueError:
                pass
    compatible = []
    for old, subject in pending:
        context = contexts[id(old)]
        for result, result_subject in observed:
            if old is result:
                continue
            result_context = contexts[id(result)]
            explicit = unit_identity(old) in exact_links.get(
                unit_identity(result), ()
            ) or unit_identity(result) in exact_links.get(unit_identity(old), ())
            same = bool(context and context == result_context)
            fallback = (
                supported_labels
                and page.frontmatter.get("n") in {None, 1, "1"}
                and len(episodes) <= 1
                and len({c for c in (context, result_context) if c}) <= 1
            )
            if not (explicit or same or fallback) or (subject and subject != result_subject):
                continue
            if id(old) in dates and id(result) in dates and dates[id(old)] > dates[id(result)]:
                continue
            if not subject and (
                len(result_subjects.get("", set()) | result_subjects.get(context, set())) != 1
                or bool({"", context} & qualified_pending)
            ):
                continue
            compatible.append((old, result, subject or result_subject, explicit))
    pending_matches = Counter(id(old) for old, _, _, _ in compatible)
    result_matches = Counter(id(result) for _, result, _, _ in compatible)
    for old, result, subject, explicit in compatible:
        if not explicit and (pending_matches[id(old)] != 1 or result_matches[id(result)] != 1):
            continue
        reason = "current_pending_with_result"
        if (
            explicit
            and id(old) in dates
            and id(result) in dates
            and dates[id(old)] < dates[id(result)]
        ):
            reason = "pending_before_authored_result"
        candidates.append(_candidate(page, FAMILIES[1], subject or "result", (old, result), reason))
    ordered = sorted(
        {(c.family, c.partition): c for c in candidates}.values(),
        key=lambda c: (c.family, c.partition),
    )
    counts = Counter(c.family for c in ordered)
    for family in FAMILIES:
        if candidate_limit is not None and counts[family] > candidate_limit:
            coverage[family] = "capped"
    retained = tuple(
        c
        for family in FAMILIES
        for c in [c for c in ordered if c.family == family][:candidate_limit]
    )
    return Detection(retained, coverage)
