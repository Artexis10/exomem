"""What each owner choice would change, as counts over the same pages the plan placed.

The primary plan keeps partition folders, names pages by their titles and keeps names unique
across the vault. Each alternative re-runs the allocation under a different choice, or counts
what the other choice would touch, so the owner can compare the choices without a second plan.
Nothing here reads a file.
"""

from __future__ import annotations

import dataclasses
import posixpath
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .. import layout, obsidian_links, vault
from ..held_fs import vault_name_key
from .facts import PageFact
from .links import LinkNote
from .sampled import Sampled

# A note type declares no partition with this value; the note-type registry schema fixes it.
_NO_PARTITION = "none"


def _basename(path: str) -> str:
    return posixpath.basename(path)


def _suffixed(allocations: layout.Allocations) -> int:
    return sum(1 for allocation in allocations.placed if allocation.number > 1)


def _homonyms(paths: Iterable[str]) -> tuple[int, int]:
    """The number of basename groups with several visible Markdown files, and the files in them."""
    groups = Counter(
        obsidian_links.basename_key(path) for path in paths if obsidian_links.visible(path)
    )
    shared = [count for count in groups.values() if count > 1]
    return len(shared), sum(shared)


def flatten(
    placements: Sequence[layout.Placement],
    primary: layout.Allocations,
    facts: Mapping[str, PageFact],
    registry: layout.Registry,
) -> tuple[dict[str, object], dict[str, str]]:
    """What flattening the partition folders would change beyond the primary plan.

    Returns the report's counts and the new path of every file under the flattened plan, for
    the disclosure diff to read the policy's selectors against.
    """
    flattened: list[layout.Placement] = []
    extra: list[str] = []
    for placement in placements:
        fact = facts.get(placement.current_path)
        note_type = registry.note_type(fact.type_value) if fact is not None else None
        area = layout.area_folder(note_type, layout.Layout.ROOT) if note_type else None
        partitioned = (
            placement.name_class is layout.NameClass.DERIVED
            and note_type is not None
            and note_type.partition != _NO_PARTITION
            and area is not None
            and placement.new_dir.startswith(area + "/")
        )
        if partitioned:
            extra.append(placement.current_path)
            flattened.append(dataclasses.replace(placement, new_dir=area or ""))
        else:
            flattened.append(placement)
    result = layout.allocate(flattened)
    report = {
        "covers": "derived pages of the note types that declare a partition",
        "extra_pages_moved": Sampled(extra),
        "extra_suffixed_names": _suffixed(result) - _suffixed(primary),
        "extra_unmappable_pages": len(result.unmappable) - len(primary.unmappable),
    }
    return report, layout.final_paths(flattened, result)


def slug_names(
    placements: Sequence[layout.Placement], primary: layout.Allocations
) -> dict[str, object]:
    """Pages renamed under title names, against slug names where every page keeps its file name."""
    kept = [
        dataclasses.replace(
            placement,
            name_class=layout.NameClass.FIXED,
            stem=_basename(placement.current_path).removesuffix(placement.extension),
            date_prefix="",
            corrections=(),
        )
        for placement in placements
    ]
    result = layout.allocate(kept)
    renamed = sum(
        1
        for allocation in primary.placed
        if _basename(allocation.new_path) != _basename(allocation.current_path)
    )
    return {
        "renamed_under_titles": renamed,
        "renamed_under_slugs": 0,
        "unmappable_under_slugs": len(result.unmappable),
    }


def per_folder(
    placements: Sequence[layout.Placement], primary: layout.Allocations
) -> dict[str, object]:
    """Suffixes and homonyms when names need be unique only within a folder, against the whole vault."""
    folders: dict[str, list[layout.Placement]] = defaultdict(list)
    for placement in placements:
        folders[vault_name_key(placement.new_dir)].append(placement)
    placed = [
        allocation for key in sorted(folders) for allocation in layout.allocate(folders[key]).placed
    ]
    wide_groups, wide_pages = _homonyms(allocation.new_path for allocation in primary.placed)
    folder_groups, folder_pages = _homonyms(allocation.new_path for allocation in placed)
    return {
        "suffixed_names_vault_wide": _suffixed(primary),
        "suffixed_names_per_folder": sum(1 for allocation in placed if allocation.number > 1),
        "homonym_groups_vault_wide": wide_groups,
        "homonym_pages_vault_wide": wide_pages,
        "homonym_groups_per_folder": folder_groups,
        "homonym_pages_per_folder": folder_pages,
    }


def _without_collision_suffix(stem: str) -> str:
    """A stem without the ` (N)` or `-N` that a collision adds; the stem itself when it has none."""
    head, space, tail = stem.rpartition(" (")
    if space and tail.endswith(")") and tail[:-1].isdecimal():
        return head
    head, dash, tail = stem.rpartition("-")
    return head if dash and tail.isdecimal() else stem


def drift(
    placements: Sequence[layout.Placement], facts: Mapping[str, PageFact]
) -> dict[str, object]:
    """Derived pages whose current file name already differs from the name that their title gives."""
    differing: list[str] = []
    derived = 0
    for placement in placements:
        fact = facts.get(placement.current_path)
        if placement.name_class is not layout.NameClass.DERIVED or fact is None:
            continue
        derived += 1
        title = str(fact.title)
        stem = _basename(placement.current_path).removesuffix(placement.extension)
        base = stem.removeprefix(placement.date_prefix)
        names = {
            vault.slugify_title(title),
            vault.sanitize_title_filename(title),
            placement.stem,
        }
        if not {base, _without_collision_suffix(base)} & names:
            differing.append(placement.current_path)
    return {
        "derived_pages": derived,
        "name_differs_from_title": len(differing),
        "pages": Sampled(differing),
    }


def trailing_path_links(
    unresolved: Sequence[LinkNote], today_paths: Iterable[str]
) -> dict[str, object]:
    """Links that today's resolver leaves open but Obsidian's rule resolves over today's paths."""
    resolver = obsidian_links.IndexResolver(obsidian_links.build_index(today_paths))
    recovered = [
        {"page": note.page, "text": note.text}
        for note in unresolved
        if resolver.resolve(vault.split_wikilink_target(note.text)[0]).outcome
        is obsidian_links.Resolution.RESOLVED
    ]
    return {
        "unresolved_links": len(unresolved),
        "resolved_by_trailing_path": Sampled(recovered),
    }


def corrections(
    placements: Sequence[layout.Placement], primary: layout.Allocations
) -> dict[str, object]:
    """How many derived names each correction changes under the title style."""
    counts = Counter(
        correction
        for placement in placements
        if placement.name_class is layout.NameClass.DERIVED
        for correction in placement.corrections
    )
    return {
        "by_correction": dict(sorted(counts.items())),
        "cut_to_the_byte_cap": sum(1 for allocation in primary.placed if allocation.truncated),
    }


@dataclass(frozen=True, slots=True)
class Alternatives:
    """The alternatives' report keyed as the report names them, and the files that O1 places differently."""

    report: dict[str, object]
    flattened: Mapping[str, str]


def build(
    *,
    placements: Sequence[layout.Placement],
    primary: layout.Allocations,
    primary_paths: Mapping[str, str],
    facts: Mapping[str, PageFact],
    registry: layout.Registry,
    unresolved: Sequence[LinkNote],
    today_paths: Iterable[str],
) -> Alternatives:
    """Every alternative's counts. `flattened` holds the new path of each file that O1 places elsewhere than `primary_paths`."""
    o1_report, o1_paths = flatten(placements, primary, facts, registry)
    report = {
        "o1_flatten": o1_report,
        "o2_slug": slug_names(placements, primary),
        "o3_per_folder": per_folder(placements, primary),
        "o5_drift": drift(placements, facts),
        "o7": {
            **trailing_path_links(unresolved, today_paths),
            "name_corrections": corrections(placements, primary),
        },
    }
    differing = {path: new for path, new in o1_paths.items() if primary_paths.get(path) != new}
    return Alternatives(report, differing)
