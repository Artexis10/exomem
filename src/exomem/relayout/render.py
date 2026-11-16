"""The plan as a JSON-ready report: the outcome first, then counts, alternatives and listings.

A long list shows `SAMPLE_LIMIT` entries and says how many exist, unless `full` is set. The
keys keep the order below so that the outcome sentence leads whatever prints the report.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Mapping

from .. import kbdir, vault
from . import disclosure as disclosure_diff
from .plan import CHOICES, Move, RelayoutPlan
from .sampled import Sampled


def _resolve(value: object, *, full: bool) -> object:
    """The report value with every `Sampled` list turned into its listing, however deep it sits."""
    if isinstance(value, Sampled):
        return value.listing(full=full)
    if isinstance(value, Mapping):
        return {key: _resolve(item, full=full) for key, item in value.items()}
    return value


def _attention(plan: RelayoutPlan) -> list[str]:
    counts = plan.counts()
    managed = kbdir.managed_root_rel()
    figures = plan.disclosure.figures() if plan.disclosure is not None else {}
    found: list[tuple[int, str]] = [
        (counts["unmappable_files"], "files that cannot get a new path"),
        (counts["top_level_collisions"], "new top-level names that collide with existing entries"),
        (len(plan.links.failures), "planned links that failed the self-check"),
        (len(plan.pointers.failures), "pointer edits that failed the self-check"),
        (counts["outside_links_that_break"], f"links from outside {managed} that would break"),
        (counts["pointers_left"], "pointer fields that name files the plan cannot repoint"),
        (len(plan.registry_findings), "registries that could not be read in full"),
        (figures.get("broken_inheritance_edges") or 0, "inheritance edges that break"),
        (
            figures.get("untranslatable_selectors") or 0,
            "policy selectors that cannot be translated",
        ),
        (
            plan.disclosure.unevaluated_governed() if plan.disclosure is not None else 0,
            "governed files whose disclosure the plan could not compare",
        ),
    ]
    return [f"{sentence}: {count}" for count, sentence in found if count]


def _disclosure_sentence(plan: RelayoutPlan) -> str:
    """The disclosure differences as a count, "unknown" naming the source that did not load, and zero as zero."""
    found = plan.disclosure
    if found is None:
        return "The disclosure diff was not computed."
    figures = found.figures()
    if figures["differences"] is None:
        unknown = [
            detail
            for state, detail in (
                (found.policy_state, found.policy_detail),
                (found.tiers.state, found.tiers.detail),
            )
            if state == disclosure_diff.STATE_UNKNOWN
        ]
        return f"Disclosure differences: unknown ({'; '.join(unknown)})."
    return (
        f"Disclosure differences: {figures['differences']}"
        f" ({figures['scopes_changed']} scopes and {figures['tiers_changed']} access tiers change members,"
        f" {figures['broken_inheritance_edges']} inheritance edges break,"
        f" {figures['untranslatable_selectors']} selectors cannot be translated)."
    )


def outcome(plan: RelayoutPlan) -> str:
    """The sentence that states the plan's result, for a person or an agent to repeat."""
    counts = plan.counts()
    managed = kbdir.managed_root_rel()
    parts = [
        f"Relayout plan from {plan.current.value} to {plan.target.value}: the content of {managed} moves to the vault root.",
        f"{counts['moved_files']} of {counts['files']} files move and {counts['renamed_files']} get a new file name.",
        f"{counts['link_edits']} links change in {counts['link_edit_pages']} pages"
        f" and {counts['pointer_edits']} pointer fields change;"
        f" {counts['link_edits_in_collection_and_part_pages']} more link edits sit in pages"
        " that the collection store and the part writer own.",
        _disclosure_sentence(plan),
    ]
    attention = _attention(plan)
    if attention:
        parts.append("Needs attention: " + "; ".join(attention) + ".")
    parts.append("This plan wrote nothing.")
    return " ".join(parts)


def _move_row(move: Move) -> dict[str, object]:
    return dataclasses.asdict(move)


def _edits_per_page(plan: RelayoutPlan) -> list[dict[str, object]]:
    held = Counter(edit.page for edit in plan.links.edits)
    return [
        {
            "page": page,
            "edits": held[page],
            "append_only_area": vault.in_append_only_tree(page) is not None,
        }
        for page in sorted(held)
    ]


def render(plan: RelayoutPlan, *, full: bool = False) -> dict[str, object]:
    """The report for `plan`; `full` lists every entry instead of the first `SAMPLE_LIMIT`."""
    renamed = [_move_row(move) for move in plan.moves if move.renamed]
    folder_only = [_move_row(move) for move in plan.moves if not move.renamed]
    links = plan.links
    report: dict[str, object] = {
        "outcome": outcome(plan),
        "layout": {
            "current": plan.current.value,
            "target": plan.target.value,
            "obsidian_config": plan.obsidian_config,
        },
        "choices": dict(CHOICES),
        "counts": plan.counts(),
        "alternatives": plan.alternatives,
        "disclosure": disclosure_diff.report(plan.disclosure) if plan.disclosure else None,
        "moves": {"renamed": Sampled(renamed), "folder_only": Sampled(folder_only)},
        "link_edits": Sampled([dataclasses.asdict(edit) for edit in links.edits]),
        "link_edits_per_page": Sampled(_edits_per_page(plan)),
        "links_left_as_written": {
            "unresolved": Sampled([dataclasses.asdict(note) for note in links.unresolved]),
            "ambiguous": Sampled([dataclasses.asdict(note) for note in links.ambiguous]),
            "to_folders": Sampled([dataclasses.asdict(note) for note in links.folder]),
        },
        "links_in_collection_and_part_pages": {
            "note": "Pages that the collection store and the part writer own; the plan lists their edits and writes none.",
            "pages_scanned": plan.collection_links.counts.pages_scanned,
            "edits": Sampled([dataclasses.asdict(edit) for edit in plan.collection_links.edits]),
        },
        "to_retired_collection_paths": Sampled(
            [
                dataclasses.asdict(note)
                for note in (*links.retired_collection, *plan.collection_links.retired_collection)
            ]
        ),
        "pointer_edits": Sampled([dataclasses.asdict(edit) for edit in plan.pointers.edits]),
        "pointers_left_as_written": Sampled(
            [dataclasses.asdict(note) for note in plan.pointers.left]
        ),
        "unregistered_notes_folders": Sampled(
            [{"folder": name, "files": count} for name, count in plan.unregistered_folders]
        ),
        "outside_content": {
            "entries": Sampled([dataclasses.asdict(entry) for entry in plan.outside]),
            "links_into_managed_folder": Sampled(
                [dataclasses.asdict(link) for link in links.outside]
            ),
        },
        "top_level_collisions": Sampled([dataclasses.asdict(item) for item in plan.collisions]),
        "unmappable": Sampled([dataclasses.asdict(item) for item in plan.unmappable]),
        "hidden_entries_not_planned": Sampled(list(plan.hidden_entries)),
        "diagnostics": {
            **plan.diagnostics,
            "leftover_schema_folders": list(plan.leftover_schema_folders),
        },
    }
    return {key: _resolve(value, full=full) for key, value in report.items()}
