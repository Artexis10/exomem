"""Temporal currency for served units: what a unit says about WHEN it holds.

A unit is served with its OWN time, never its parent page's; older material that
newer material on the same page replaced is marked history; and a recommendation
whose action a later outcome records is marked superseded by that outcome. Every
rule reads authored values only (a unit's own ISO date, its category and kind,
its supersession relations, the words it shares with a sibling) — no model, no
inferred timeline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .working_set_index import terms_of

if TYPE_CHECKING:  # pragma: no cover
    from .working_set import LaneItem

HISTORICAL = "historical"
_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_RECOMMENDATION_KINDS = frozenset({"recommendation", "next_step", "suggestion", "action"})
_OUTCOME_KINDS = frozenset({"outcome", "result", "done", "completed"})
_OUTCOME_CATEGORIES = frozenset({"outcome", "result"})
_CURRENT_CATEGORIES = frozenset({"decision"})
#: Words an action and its outcome must share for them to be the SAME action.
MIN_SHARED_ACTION_TERMS = 3


def own_time(content: str, context: str | None) -> str:
    """The unit's own authored date: its context first, then its text; else empty."""
    for text in (context or "", content or ""):
        match = _ISO_DATE.search(text)
        if match:
            return match.group(1)
    return ""


def _label(item: "LaneItem", key: str) -> str:
    return str(item.provenance.get(key) or "").strip().casefold()


def _is_recommendation(item: "LaneItem") -> bool:
    return _label(item, "kind") in _RECOMMENDATION_KINDS or (
        _label(item, "category") == "action" and _label(item, "kind") != "outcome"
    )


def _is_outcome(item: "LaneItem") -> bool:
    return _label(item, "kind") in _OUTCOME_KINDS or _label(item, "category") in _OUTCOME_CATEGORIES


def _action_terms(text: str) -> frozenset[str]:
    return frozenset(term for term in terms_of(text) if len(term) > 3)


def relation_targets(relations: Sequence[Mapping[str, Any]] | None) -> tuple[str, ...]:
    out = []
    for relation in relations or ():
        if str(relation.get("kind") or "").casefold() == "supersedes":
            target = str(relation.get("target") or "").strip().strip("[]")
            if target:
                out.append(target)
    return tuple(out)


def _same_unit(target: str, item: "LaneItem") -> bool:
    if not target:
        return False
    return target == item.ref or item.ref.endswith("#" + target.lstrip("#")) or target.endswith(
        item.ref
    )


def annotate(items: Sequence["LaneItem"]) -> tuple["LaneItem", ...]:
    """Mark history and outcome ordering among sibling units of one page.

    Units only; pages, records and pointers pass through untouched. Pure and
    deterministic: the same items give the same marks.
    """
    marks: dict[int, dict[str, Any]] = {}
    lifecycle: dict[int, str] = {}
    by_page: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.level == "unit" and item.path:
            by_page.setdefault(item.path, []).append(index)
    for indices in by_page.values():
        siblings = [(i, items[i]) for i in indices]
        # Explicit supersession relations: the target is history.
        for _, item in siblings:
            for target in item.provenance.get("supersedes_targets") or ():
                for j, other in siblings:
                    if other is not item and _same_unit(target, other):
                        lifecycle[j] = HISTORICAL
                        marks.setdefault(j, {})["superseded_by_unit"] = item.ref
        # An observation older than a dated current decision on the same page.
        decisions = [
            item.updated
            for _, item in siblings
            if _label(item, "category") in _CURRENT_CATEGORIES and item.updated
        ]
        newest_decision = max(decisions, default="")
        for j, item in siblings:
            if (
                newest_decision
                and item.updated
                and item.updated < newest_decision
                and _label(item, "category") not in _CURRENT_CATEGORIES
                and item.lifecycle == "active"
            ):
                lifecycle.setdefault(j, HISTORICAL)
        # A later outcome supersedes the recommendation for the same action.
        for i, rec in siblings:
            if not _is_recommendation(rec) or not rec.updated:
                continue
            rec_terms = _action_terms(rec.text)
            for j, out in siblings:
                if not _is_outcome(out) or not out.updated or out.updated <= rec.updated:
                    continue
                if len(rec_terms & _action_terms(out.text)) < MIN_SHARED_ACTION_TERMS:
                    continue
                lifecycle[i] = HISTORICAL
                marks.setdefault(i, {})["superseded_by_outcome"] = out.ref
                marks.setdefault(j, {}).setdefault("newer_than", []).append(rec.ref)
    if not marks and not lifecycle:
        return tuple(items)
    out_items = []
    for index, item in enumerate(items):
        if index in marks or index in lifecycle:
            item = replace(
                item,
                lifecycle=lifecycle.get(index, item.lifecycle),
                provenance={**item.provenance, **marks.get(index, {})},
            )
        out_items.append(item)
    return tuple(out_items)
