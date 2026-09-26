"""Scorer and report rows for the multilingual context-activation fixtures
(``context-activation-multilingual-v1``, step 4).

A sibling of :mod:`membench.utility.context_activation`, which scores the
digest-pinned English set: nothing here touches that module's scoring or its
report, so the English verdicts and report keys stay byte-identical. The
multilingual block is its own report (:func:`multilingual_report`).

A case is scored from TWO packets of the same turn, semantic evidence on and
off, because the arms expect different statuses for the named cases and the
menu can only be judged by comparing them. Optional further packets of the
same turn in other non-ready semantic states (encoder cold, vectors absent)
must equal the off packet: degradation is one shape, never several.

Pure functions of packets (the product's own dict output), a case and the
corpus key -> path map. No model, no I/O.

Two readings differ from the English scorer on purpose:

* **Status.** The manifest's ``partial`` means an anchor surfaced at
  ``partial``, whether or not the packet abstained; the English
  ``turn_status`` folds an abstained packet into ``unresolved``.
* **Menu.** ``gold_first`` is the design target for relevance ordering.
  Promotion is not shipped (the step-4 T8 ruling: under int8 encoding the
  pool-only rule promoted a poison), so a menu case's rank is REPORTED, never
  gated. What is gated is that no poison is ever promoted and a content-free
  turn's menu is byte-identical across the arms.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from epistemic.corpora.context_activation_multilingual import (
    RECENT_MENU_SIZE,
    TWIN_OF,
    MultilingualCase,
)

from membench.utility.context_activation import _percentile

#: Contact kinds in which the turn's own words reached the anchor. Mirrors the
#: compiler's `working_set_resolve.WORDED_CONTACT_KINDS` (pinned by a test): a
#: `vector_band` may help resolve an anchor only beside one of these, or beside
#: continuity or the agent's own choice.
WORDED_CONTACT_KINDS: frozenset[str] = frozenset({"exact_alias", "lexical_overlap", "claims_match", "rare_term"})
_BAND_PARTNERS = WORDED_CONTACT_KINDS | {"continuity", "agent_choice"}
_NAMED_KINDS = frozenset({"named_rare", "cjk_named"})
_MENU_KINDS = frozenset({"cross_language_menu", "language_bias", "lone_script_menu"})


@dataclass(frozen=True)
class MultilingualRow:
    """One case's §9.2 row: both arms, what was expected, what was observed."""

    case_id: str
    language: str
    kind: str
    scored: bool
    guarded: bool
    expected_status_on: str
    observed_status_on: str
    expected_status_off: str
    observed_status_off: str
    gold_resolved: bool | None
    gold_menu_rank_on: int | None
    gold_menu_rank_off: int | None
    poison_promoted: tuple[str, ...]
    poison_banded: tuple[str, ...]
    semantic_evidence: str | None
    semantic_ms: float | None
    encode_ms: float | None
    degrade_identical: bool
    passed: bool
    failure_reasons: tuple[str, ...]


def case_status(packet: Mapping[str, Any]) -> str:
    """``resolved``/``ambiguous``/``partial``/``unresolved`` for one packet."""

    statuses = {str(item.get("status")) for item in packet.get("anchors") or ()}
    if packet.get("ambiguity"):
        return "ambiguous"
    if "resolved" in statuses and not packet.get("abstained"):
        return "resolved"
    if "partial" in statuses:
        return "partial"
    return "unresolved"


def _anchors(packet: Mapping[str, Any], status: str | None = None) -> dict[str, frozenset[str]]:
    return {
        str(item.get("path")): frozenset(item.get("evidence") or ())
        for item in packet.get("anchors") or ()
        if status is None or item.get("status") == status
    }


def menu_rank(packet: Mapping[str, Any], path: str) -> int | None:
    """1-based position of ``path`` in the packet's own ``recent_context``."""

    for rank, entry in enumerate(packet.get("recent_context") or (), start=1):
        if entry.get("path") == path:
            return rank
    return None


def packet_shape(packet: Mapping[str, Any]) -> str:
    """What degradation must keep identical: status, anchors with their status
    and evidence, and the recent menu in order."""

    return json.dumps(
        {
            "abstained": bool(packet.get("abstained")),
            "abstention": (packet.get("abstention") or {}).get("reason"),
            "anchors": sorted(
                (str(item.get("path")), str(item.get("status")), sorted(item.get("evidence") or ()))
                for item in packet.get("anchors") or ()
            ),
            "recent": [entry.get("path") for entry in packet.get("recent_context") or ()],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _surfaced(packet: Mapping[str, Any]) -> set[str]:
    """Every path a packet serves as activated: resolved anchors, ambiguity
    candidates, units, pointers and current-state entries."""

    found = set(_anchors(packet, "resolved"))
    for key in ("ambiguity", "units", "pointers"):
        for item in packet.get(key) or ():
            if isinstance(item, Mapping):
                found.add(str(item.get("path") or item.get("ref") or ""))
    for item in packet.get("current_state") or ():
        if isinstance(item, Mapping):
            found.add(str(item.get("anchor") or ""))
    return found - {""}


def _carried(packet: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("path"))
        for item in packet.get("anchors") or ()
        if item.get("status") == "retrieval_carried"
    }


def _stage_ms(packet: Mapping[str, Any], stage: str) -> float | None:
    value = ((packet.get("timings") or {}).get("stages") or {}).get(stage)
    if isinstance(value, Mapping) and value.get("ms") is not None:
        return float(value["ms"])
    return None


def score_multilingual_case(
    case: MultilingualCase,
    packet_on: Mapping[str, Any],
    packet_off: Mapping[str, Any],
    *,
    key_to_path: Mapping[str, str],
    degraded: Sequence[Mapping[str, Any]] = (),
    encode_ms: float | None = None,
) -> MultilingualRow:
    """Score one case from its semantic-on and semantic-off packets."""

    gold = {key_to_path[key] for key in case.gold}
    poison = {key_to_path[key] for key in case.poison}
    never = {key_to_path[key] for key in case.never_resolved}
    reasons: list[str] = []
    status_on, status_off = case_status(packet_on), case_status(packet_off)
    if status_on != case.expected_status_on:
        reasons.append(f"status on {status_on} != {case.expected_status_on}")
    if status_off != case.expected_status_off:
        reasons.append(f"status off {status_off} != {case.expected_status_off}")

    resolved_on = _anchors(packet_on, "resolved")
    gold_resolved = bool(gold) and gold <= set(resolved_on) if gold else None
    if case.expected_status_on == "resolved" and gold and not gold_resolved:
        reasons.append("gold not resolved with semantic evidence on")

    for arm, packet in (("on", packet_on), ("off", packet_off)):
        resolved = set(_anchors(packet, "resolved"))
        if (never | poison) & resolved:
            reasons.append(f"guarded key resolved ({arm}): {sorted((never | poison) & resolved)}")
        if poison & _carried(packet):
            reasons.append(f"poison carried ({arm}): {sorted(poison & _carried(packet))}")
        if case.kind == "carry_fragment" and (_carried(packet) or (packet.get("generation") or {}).get("carried_by")):
            reasons.append(f"a single word carried a page ({arm})")
        if case.kind in TWIN_OF:
            false = _surfaced(packet) - gold
            if false:
                reasons.append(f"twin false activation ({arm}): {sorted(false)}")
        alone = sorted(
            path
            for path, evidence in _anchors(packet, "resolved").items()
            if "vector_band" in evidence and not evidence & _BAND_PARTNERS
        )
        if alone:
            reasons.append(f"resolved by vector_band alone ({arm}): {alone}")
    if any("vector_band" in evidence for evidence in _anchors(packet_off).values()):
        reasons.append("vector_band served with semantic evidence off")

    banded = tuple(sorted(path for path, evidence in _anchors(packet_on).items() if path in poison and "vector_band" in evidence))
    if banded:
        reasons.append(f"poison banded: {list(banded)}")
    below_menu = RECENT_MENU_SIZE + 1
    promoted = tuple(
        sorted(
            path
            for path in poison
            if (menu_rank(packet_on, path) or below_menu) < (menu_rank(packet_off, path) or below_menu)
        )
    )
    if promoted:
        reasons.append(f"poison promoted: {list(promoted)}")
    if case.menu == "recency" and packet_on.get("recent_context") != packet_off.get("recent_context"):
        reasons.append("a content-free turn's menu differs between the arms")

    shape = packet_shape(packet_off)
    degrade_identical = all(packet_shape(other) == shape for other in degraded)
    if not degrade_identical:
        reasons.append("a degraded semantic state served a different packet than semantic off")

    gold_path = next(iter(sorted(gold)), None)
    return MultilingualRow(
        case_id=case.case_id,
        language=case.language,
        kind=case.kind,
        scored=case.scored,
        guarded=bool(poison),
        expected_status_on=case.expected_status_on,
        observed_status_on=status_on,
        expected_status_off=case.expected_status_off,
        observed_status_off=status_off,
        gold_resolved=gold_resolved,
        gold_menu_rank_on=menu_rank(packet_on, gold_path) if gold_path else None,
        gold_menu_rank_off=menu_rank(packet_off, gold_path) if gold_path else None,
        poison_promoted=promoted,
        poison_banded=banded,
        semantic_evidence=(packet_on.get("generation") or {}).get("semantic_evidence"),
        semantic_ms=_stage_ms(packet_on, "working_set.semantic"),
        encode_ms=encode_ms,
        degrade_identical=degrade_identical,
        passed=not reasons or not case.scored,
        failure_reasons=tuple(reasons),
    )


def _distribution(values: Iterable[float | None]) -> dict[str, float | None]:
    present = sorted(float(value) for value in values if value is not None)
    return {"p50": _percentile(present, 0.50), "p95": _percentile(present, 0.95), "n": len(present)}


def multilingual_report(rows: Sequence[MultilingualRow], *, encoder: Mapping[str, Any]) -> dict[str, Any]:
    """The ``multilingual`` report block: encoder, arms, per-case rows, summary."""

    guarded = [row for row in rows if row.scored and row.guarded]
    named = [row for row in rows if row.kind in _NAMED_KINDS]
    below_menu = RECENT_MENU_SIZE + 1
    gains = [
        (row.gold_menu_rank_off or below_menu) - (row.gold_menu_rank_on or below_menu)
        for row in rows
        if row.kind in _MENU_KINDS and row.gold_resolved is not None
    ]
    return {
        "encoder": dict(encoder),
        "arms": ["semantic_on", "semantic_off"],
        "per_case": [
            {
                "case_id": row.case_id,
                "language": row.language,
                "kind": row.kind,
                "scored": row.scored,
                "expected_status": {"on": row.expected_status_on, "off": row.expected_status_off},
                "observed_status": {"on": row.observed_status_on, "off": row.observed_status_off},
                "gold_resolved": row.gold_resolved,
                "gold_menu_rank": {"on": row.gold_menu_rank_on, "off": row.gold_menu_rank_off},
                "poison_promoted": list(row.poison_promoted),
                "poison_banded": list(row.poison_banded),
                "semantic_evidence": row.semantic_evidence,
                "semantic_ms": row.semantic_ms,
                "encode_ms": row.encode_ms,
                "passed": row.passed,
                "failure_reasons": list(row.failure_reasons),
            }
            for row in rows
        ],
        "summary": {
            "false_band_rate": (
                sum(1 for row in guarded if row.poison_banded or row.poison_promoted) / len(guarded) if guarded else None
            ),
            "menu_gain": statistics.median(gains) if gains else None,
            "named_resolution_rate": (
                sum(1 for row in named if row.gold_resolved) / len([row for row in named if row.gold_resolved is not None])
                if any(row.gold_resolved is not None for row in named)
                else None
            ),
            "degrade_identical": all(row.degrade_identical for row in rows),
            "semantic_ms": _distribution(row.semantic_ms for row in rows),
            "encode_ms": _distribution(row.encode_ms for row in rows),
            "passed": all(row.passed for row in rows),
        },
    }
