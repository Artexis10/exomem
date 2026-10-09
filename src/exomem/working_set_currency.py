"""Temporal currency for served units, from AUTHORED signals only.

A unit is served with its OWN time: the date its author wrote in the unit's
context slot (the compact `(context)` or the rich `context:` row), never a date
its prose happens to mention and never one the context marks as a deadline. A
unit is history only when an authored supersession says so: its page is
superseded or archived (the lifecycle already carries that), or a unit on the
SAME page carries a `supersedes` relation naming it. Nothing is inferred from
dates, categories or shared words.

Same page only, deliberately: a relation authored on another page could be on
a page the reader may not see, and marking the released unit "replaced" would
tell that reader the other page exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from . import lifecycle_statuses

if TYPE_CHECKING:  # pragma: no cover
    from .working_set import LaneItem

#: The lifecycle an authored unit relation gives the unit it supersedes.
SUPERSEDED = "superseded"
#: Lifecycles that are history. Draft, planned and dropped material is not
#: history: it has not been replaced, it has not happened yet.
#: Provenance keys the compiler uses internally and never publishes.
INTERNAL_PROVENANCE = frozenset({"page_updated", "supersedes_targets", "supersession"})

_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
#: A date the context introduces with one of these is when something is DUE,
#: not when the unit was written.
_DEADLINE_CUE = re.compile(
    r"(?:\bdue|\bby|\bbefore|\buntil|\btill|\bdeadline|\bexpir\w*|\bno later than)"
    r"(?:\s+on)?[\s:,-]*$",
    re.IGNORECASE,
)
_VAULT_REF_PREFIX = "exomem://vault/"


def own_time(context: str | None) -> str:
    """The unit's own authored date, read from its context slot; else empty."""
    text = str(context or "")[:512]
    for match in _ISO_DATE.finditer(text):
        if _DEADLINE_CUE.search(text[max(0, match.start() - 32) : match.start()]):
            continue
        try:
            date.fromisoformat(match.group(1))
        except ValueError:
            continue
        return match.group(1)
    return ""


def relation_targets(relations: Sequence[Mapping[str, Any]] | None) -> tuple[str, ...]:
    """The raw targets of a unit's authored `supersedes` relations."""
    out = []
    for relation in relations or ():
        if str(relation.get("kind") or "").casefold() == "supersedes":
            target = str(relation.get("target") or "").strip()
            if target:
                out.append(target)
    return tuple(out)


def _page_names(path: str) -> frozenset[str]:
    """Every spelling that names `path` as a link target, casefolded."""
    folded = path.casefold()
    stem = folded.removesuffix(".md")
    names = {folded, stem, stem.rsplit("/", 1)[-1]}
    if stem.startswith("knowledge base/"):
        names.add(stem.removeprefix("knowledge base/"))
    return frozenset(names)


def _target_fragment(target: str, path: str) -> str:
    """The unit anchor `target` names ON `path`, or empty when it names another page."""
    text = target.strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0].strip()
    page, separator, fragment = text.rpartition("#")
    if not separator or not fragment.strip():
        return ""
    page = unquote(page.strip().removeprefix(_VAULT_REF_PREFIX))
    if page and page.casefold() not in _page_names(path):
        return ""
    return fragment.strip().casefold()


def _unit_fragment(item: LaneItem) -> str:
    _page, separator, fragment = item.ref.rpartition("#")
    return fragment.casefold() if separator else ""


def annotate(
    items: Sequence[LaneItem], *, status_basis: lifecycle_statuses.Basis | None = None
) -> tuple[LaneItem, ...]:
    """Mark as superseded each unit a same-page `supersedes` relation names.

    Units only; pages, records and pointers pass through untouched. Pure and
    deterministic: the same items give the same marks.
    """
    status_basis = status_basis or lifecycle_statuses.Basis(None)
    by_page: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.level == "unit" and item.path:
            by_page.setdefault(item.path, []).append(index)
    superseded: set[int] = set()
    for path, indices in by_page.items():
        by_fragment: dict[str, list[int]] = {}
        for index in indices:
            fragment = _unit_fragment(items[index])
            if fragment:
                by_fragment.setdefault(fragment, []).append(index)
        for index in indices:
            own = _unit_fragment(items[index])
            for target in items[index].provenance.get("supersedes_targets") or ():
                fragment = _target_fragment(str(target), path)
                if fragment and fragment != own:
                    superseded.update(by_fragment.get(fragment, ()))
    if not superseded:
        return tuple(items)
    return tuple(
        replace(item, lifecycle=SUPERSEDED)
        if index in superseded
        and status_basis.classify(item.lifecycle, path=item.path or None).require() == "live"
        else item
        for index, item in enumerate(items)
    )
