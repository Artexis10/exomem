"""Frontmatter fields that name another file: which of them change when the files move.

A page names a file in four ways. `originals` pointers, the paths of a container member's
binding and `parent_media` hold a path from the vault root, `next_part` and `part_of` hold a
path from the page's own folder, and `ingested_into` holds wikilinks. A path field changes when its target moves. A page-relative
value changes only when the page or its part folder is renamed, because the two stay together.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .. import kbdir
from ..governance import membership
from .facts import INGESTED_INTO, PAGE_RELATIVE_POINTERS, PARENT_MEDIA, PageFact
from .links import LinkRewriter, Outcome

NOT_FOUND = "target_not_found"
LEAVES_FRAMES_FOLDER = "frame_leaves_its_frames_folder"
NOT_A_LINK = "entry_is_not_a_wikilink"
UNRESOLVED_ENTRY = "entry_resolves_to_no_page"

# The wikilink grammar fixes the brackets that wrap an entry's text.
_OPEN, _CLOSE = "[[", "]]"


@dataclass(frozen=True, slots=True)
class PointerEdit:
    """A field of `page` whose value changes from `old` to `new`."""

    page: str
    field: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class PointerNote:
    """A field that the plan cannot repoint, with the reason, so that the owner sees it."""

    page: str
    field: str
    value: str
    reason: str


@dataclass(slots=True)
class PointerPlan:
    """Every pointer edit, the pointers that stay as written, and the pointers that dangle."""

    edits: list[PointerEdit]
    left: list[PointerNote]
    failures: list[PointerNote]
    unchanged: int = 0


def _relative_target(page: str, value: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(page), value))


def plan_pointers(
    facts: Iterable[PageFact],
    moved: Mapping[str, str],
    rewriter: LinkRewriter,
) -> PointerPlan:
    """The pointer edits of every page that has a new path, in page order.

    A path field whose value is not a file below the managed root stays as written. A value
    below it that names no moved file is listed in `left`: the plan cannot repoint it. A
    frame that would leave its video's frames folder is a plan failure.
    """
    result = PointerPlan([], [], [])
    for fact in sorted(facts, key=lambda item: item.path):
        new_page = moved.get(fact.path)
        if new_page is None:
            continue
        for field_name, value in fact.pointers:
            if field_name in PAGE_RELATIVE_POINTERS:
                old = _relative_target(fact.path, value)
                new = moved.get(old)
                if new is None:
                    result.left.append(PointerNote(fact.path, field_name, value, NOT_FOUND))
                    continue
                relative = posixpath.relpath(new, posixpath.dirname(new_page))
                _record(result, fact.path, field_name, value, relative)
                continue
            new = moved.get(value)
            if new is None:
                if _below_managed_root(value):
                    result.left.append(PointerNote(fact.path, field_name, value, NOT_FOUND))
                else:
                    result.unchanged += 1
                continue
            _record(result, fact.path, field_name, value, new)
            # The frame stays a frame only while it sits in its video's frames folder, by the kernel's own rule.
            if field_name == PARENT_MEDIA and not membership.inheritance_edge(
                new_page, part_of=False, parent_media=new
            ):
                result.failures.append(
                    PointerNote(fact.path, field_name, value, LEAVES_FRAMES_FOLDER)
                )
        for entry in fact.ingested:
            _plan_entry(result, rewriter, fact.path, entry)
    return result


def _below_managed_root(path: str) -> bool:
    return kbdir.managed_split(path) is not None


def _record(result: PointerPlan, page: str, field_name: str, old: str, new: str) -> None:
    if new == old:
        result.unchanged += 1
    else:
        result.edits.append(PointerEdit(page, field_name, old, new))


def _plan_entry(result: PointerPlan, rewriter: LinkRewriter, page: str, entry: str) -> None:
    text = entry.strip()
    if not (text.startswith(_OPEN) and text.endswith(_CLOSE)):
        result.left.append(PointerNote(page, INGESTED_INTO, entry, NOT_A_LINK))
        return
    inner = text[len(_OPEN) : -len(_CLOSE)]
    target = rewriter.target(inner)
    if target.outcome is not Outcome.RESOLVED:
        result.left.append(PointerNote(page, INGESTED_INTO, entry, UNRESOLVED_ENTRY))
        return
    planned = rewriter.new_text(inner, target)
    if planned is None:
        result.unchanged += 1
        return
    _record(result, page, INGESTED_INTO, entry, f"{_OPEN}{planned[0]}{_CLOSE}")
