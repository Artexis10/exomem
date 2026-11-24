"""The read-only relayout plan: where every file goes, and what has to change with it.

`plan(vault_root)` reads a `nested` vault (the managed folder inside the vault root) and
describes the move to `root` (the managed folder's content at the vault root). It writes
nothing, takes no lease and holds no page body after it has used it. It reads in two passes:

1. Facts. Every Markdown file is read once for what the page says about itself
   (`facts.PageFact`): its type, title, display title and the pointer fields that name a file.
   The facts decide each file's new place (`layout.place`, `layout.allocate`) and build the
   resolver that today's links resolve against.
2. Edits. Each page that Evermatter owns is read again, singly, for its links
   (`links.LinkRewriter`). Pages outside the managed folder are scanned for links into it.
   Pointer fields come from the first pass's facts (`pointers.plan_pointers`).

The disclosure diff (`relayout/disclosure.py`) reads a built `RelayoutPlan`: `moved` maps every
moved file to its new path, `facts` holds the page facts of the first pass, `registry` is the
loaded vocabulary and `flattened` holds the paths that the flat-partition alternative changes.
The apply, verify and revert steps use `moves`, `moved`, `links` and `pointers` as the list of
what to write, and `collection_links` as the list of edits that the owners of the collection
and part pages receive: the plan reads those pages and writes none of them.

A file-mode and a store-mode vault with the same pages give the same plan: both modes list
files and parse bytes through `inventory`, and only the source of the bytes differs.
"""

from __future__ import annotations

import posixpath
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .. import extraction_parts, layout, obsidian_links, vault
from ..held_fs import vault_name_key
from ..vault_store import read
from . import alternatives
from .disclosure import Disclosure, flatten_report
from .disclosure import build as build_disclosure
from .facts import PageFact, read_fact
from .inventory import Inventory, open_inventory
from .links import LinkRewriter, LinkScan
from .pointers import PointerPlan, plan_pointers

#: The owner choices that the primary plan makes, named as the alternatives name them.
CHOICES = {
    "partitions": "keep",
    "filenames": "title",
    "uniqueness": "vault_wide",
    "outside_content": "unmanaged",
}

#: The diagnostics key that holds what differs between two honest runs: the mode, the store
#: revision and the timings. Two plans of the same vault are equal without it.
READ_KEY = "read"


@dataclass(frozen=True, slots=True)
class Move:
    """A file's new path. `renamed` says the file name changes, not only its folder."""

    current_path: str
    new_path: str
    name_class: layout.NameClass
    renamed: bool
    number: int = 1
    truncated: bool = False
    corrections: tuple[str, ...] = ()
    fixed_because: layout.FixedBecause | None = None


@dataclass(frozen=True, slots=True)
class OutsideSummary:
    """A top-level entry outside the managed folder: what it holds and how it links into the managed folder."""

    name: str
    is_directory: bool
    files: int
    markdown_files: int
    links_into_managed: int
    breaking_links: int


@dataclass(frozen=True, slots=True)
class TopLevelCollision:
    """A new top-level name that an existing outside entry already holds, with the files that would sit under it."""

    name: str
    outside_entry: str
    files: int


@dataclass(slots=True)
class RelayoutPlan:
    """Everything a relayout from `current` to `target` would do, and what it cannot do.

    `moved` and `facts` are lookups for later steps and are not part of a report.
    """

    current: layout.Layout
    target: layout.Layout
    obsidian_config: str
    registry_findings: tuple[str, ...]
    leftover_schema_folders: tuple[str, ...]
    moves: tuple[Move, ...]
    moved: Mapping[str, str]
    facts: Mapping[str, PageFact]
    registry: layout.Registry
    unmappable: tuple[layout.Unmappable, ...]
    unregistered_folders: tuple[tuple[str, int], ...]
    links: LinkScan
    pointers: PointerPlan
    outside: tuple[OutsideSummary, ...]
    collisions: tuple[TopLevelCollision, ...]
    alternatives: dict[str, object]
    hidden_entries: tuple[str, ...]
    collection_links: LinkScan = field(default_factory=LinkScan)
    flattened: Mapping[str, str] = field(default_factory=dict)
    disclosure: Disclosure | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """The plan's figures, each over the files the plan covers."""
        moves = self.moves
        links = self.links.counts
        return {
            "files": len(moves) + len(self.unmappable),
            "moved_files": len(moves),
            "markdown_files": sum(1 for path in self.moved if path.lower().endswith(".md")),
            "renamed_files": sum(1 for move in moves if move.renamed),
            "derived_names": sum(
                1 for move in moves if move.name_class is layout.NameClass.DERIVED
            ),
            "fixed_names": sum(1 for move in moves if move.name_class is layout.NameClass.FIXED),
            "suffixed_names": sum(1 for move in moves if move.number > 1),
            "truncated_names": sum(1 for move in moves if move.truncated),
            "corrected_names": sum(1 for move in moves if move.corrections),
            "unmappable_files": len(self.unmappable),
            "pages_scanned_for_links": links.pages_scanned,
            "links": links.links,
            "links_to_attachments": links.attachment_links,
            "link_edits": links.edits,
            "link_edits_to_attachments": links.attachment_edits,
            "link_edit_pages": links.edit_pages,
            "link_edit_pages_in_append_only_areas": links.append_only_edit_pages,
            "link_edits_in_frontmatter": links.frontmatter_edits,
            "links_with_full_path_text": links.full_path_forms,
            "links_unchanged": links.unchanged,
            "links_to_pages_outside_the_managed_folder": links.to_outside,
            "links_to_unmapped_pages": links.to_unmapped,
            "links_unresolved": links.unresolved,
            "links_ambiguous": links.ambiguous,
            "links_to_folders": links.folder,
            "links_in_origin_carriers": links.carrier_links,
            "links_to_retired_collection_paths": links.to_retired_collection_paths
            + self.collection_links.counts.to_retired_collection_paths,
            "pages_in_collection_and_part_pages_scanned": self.collection_links.counts.pages_scanned,
            "link_edits_in_collection_and_part_pages": self.collection_links.counts.edits,
            "pointer_edits": len(self.pointers.edits),
            "pointers_left": len(self.pointers.left),
            "outside_links_into_managed_folder": len(self.links.outside),
            "outside_links_that_break": sum(1 for link in self.links.outside if link.breaks),
            "top_level_collisions": len(self.collisions),
            "hidden_entries_not_planned": len(self.hidden_entries),
        }


def _obsidian_config(vault_root: Path) -> str:
    """Where Obsidian's settings sit: the vault root, the managed folder, both, or nowhere."""
    at_root = (vault_root / ".obsidian").is_dir()
    in_managed = vault.obsidian_uses_kb_root(vault_root)
    if at_root and in_managed:
        return "both"
    if at_root:
        return "vault_root"
    return "managed_root" if in_managed else "absent"


def _read_facts(inventory: Inventory) -> tuple[dict[str, PageFact], list[str]]:
    """The first pass: the facts of every managed Markdown file, and the files that could not be read."""
    facts: dict[str, PageFact] = {}
    unreadable: list[str] = []
    for path, data in inventory.read_all(item.path for item in inventory.managed if item.markdown):
        if data is None:
            unreadable.append(path)
        facts[path] = read_fact(path, data)
    return facts, unreadable


def _resolver_entries(
    inventory: Inventory, facts: Mapping[str, PageFact]
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """The `(path, title)` of every Markdown file that today's resolver sees, and the outside titles' unreadable files.

    The resolver sees the files a vault walk reaches, which is a judgment about the path alone.
    """
    entries: list[tuple[str, str | None]] = []
    unreadable: list[str] = []
    for path, fact in facts.items():
        if read.walk_member(path):
            entries.append((path, fact.display_title))
    for entry in inventory.outside.values():
        for path in entry.markdown:
            if not read.walk_member(path):
                continue
            data = inventory.read(path)
            if data is None:
                unreadable.append(path)
            entries.append((path, read_fact(path, data).display_title))
    return entries, unreadable


def _place_all(
    inventory: Inventory, facts: Mapping[str, PageFact], registry: layout.Registry
) -> list[layout.Placement]:
    placements: list[layout.Placement] = []
    files = frozenset(item.path for item in inventory.managed)
    for item in inventory.managed:
        fact = facts.get(item.path)
        placement = layout.place(
            item.path,
            type_value=fact.type_value if fact else None,
            title=fact.title if fact else None,
            registry=registry,
            files=files,
        )
        if placement is not None:
            placements.append(placement)
    return placements


def _moves(
    placements: Sequence[layout.Placement],
    allocations: layout.Allocations,
    moved: Mapping[str, str],
) -> tuple[Move, ...]:
    by_current = {placement.current_path: placement for placement in placements}
    allocated = {allocation.current_path: allocation for allocation in allocations.placed}
    moves = []
    for current in sorted(moved):
        placement, allocation = by_current[current], allocated[current]
        new = moved[current]
        moves.append(
            Move(
                current,
                new,
                placement.name_class,
                posixpath.basename(new) != posixpath.basename(current),
                allocation.number,
                allocation.truncated,
                placement.corrections,
                placement.fixed_because,
            )
        )
    return tuple(moves)


def _outside_summaries(inventory: Inventory, links: LinkScan) -> tuple[OutsideSummary, ...]:
    held: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for link in links.outside:
        counts = held[link.page.split("/", 1)[0]]
        counts[0] += 1
        counts[1] += link.breaks
    return tuple(
        OutsideSummary(
            entry.name,
            entry.is_directory,
            entry.files,
            len(entry.markdown),
            *held[name],
        )
        for name, entry in inventory.outside.items()
    )


def _collisions(inventory: Inventory, moved: Mapping[str, str]) -> tuple[TopLevelCollision, ...]:
    """New top-level names that equal an existing outside entry under the vault's name comparison."""
    outside = {vault_name_key(name): name for name in inventory.outside}
    files: Counter[str] = Counter(new.split("/", 1)[0] for new in moved.values())
    return tuple(
        TopLevelCollision(name, outside[vault_name_key(name)], count)
        for name, count in sorted(files.items())
        if vault_name_key(name) in outside
    )


def plan(vault_root: Path, target: layout.Layout | str = layout.Layout.ROOT) -> RelayoutPlan:
    """Plan the move of the vault at `vault_root` to the `target` layout, writing nothing.

    Raises `layout.LayoutError` when the layout cannot be read, when the vault is already in
    the target layout, or when the target is not `root`: no other direction exists yet.
    """
    started = time.monotonic()
    root = Path(vault_root)
    wanted = layout.Layout(target)
    resolved = layout.inspect(root)
    if wanted is not layout.Layout.ROOT:
        raise layout.LayoutError(
            "RELAYOUT_TARGET_UNSUPPORTED",
            f"A relayout to {wanted.value} is not available.",
            "Plan the move to `root`.",
        )
    if resolved.layout is wanted:
        raise layout.LayoutError(
            "RELAYOUT_NOTHING_TO_DO",
            f"The vault already uses layout {wanted.value}.",
            "Nothing to plan.",
        )
    registry = layout.load_registry(root)
    marks: dict[str, float] = {}
    with open_inventory(root) as inventory:
        marks["inventory"] = time.monotonic()
        facts, unreadable = _read_facts(inventory)
        entries, unreadable_outside = _resolver_entries(inventory, facts)
        placements = _place_all(inventory, facts, registry)
        allocations = layout.allocate(placements)
        moved = layout.final_paths(placements, allocations)
        marks["facts"] = time.monotonic()

        outside_markdown = [path for entry in inventory.outside.values() for path in entry.markdown]
        markdown_moved = {
            current: new for current, new in moved.items() if current.lower().endswith(".md")
        }
        outside_attachments = [
            path for entry in inventory.outside.values() for path in entry.attachments
        ]
        post_index = obsidian_links.build_index([*markdown_moved.values(), *outside_markdown])
        resolver = vault.WikilinkResolver.from_entries(root, entries)
        rewriter = LinkRewriter(
            resolver,
            root,
            moved,
            post_index,
            {path.removesuffix(".md"): path for path, _title in entries},
            attachments_today=obsidian_links.build_attachment_index(
                [
                    *(item.path for item in inventory.managed if not item.markdown),
                    *outside_attachments,
                ]
            ),
            attachments_after=obsidian_links.build_attachment_index(
                [
                    *(new for current, new in moved.items() if current not in markdown_moved),
                    *outside_attachments,
                ]
            ),
        )
        scan = LinkScan()
        foreign = LinkScan()
        pages = [item for item in inventory.managed if item.markdown]
        for item, (_, data) in zip(
            pages, inventory.read_all(item.path for item in pages), strict=True
        ):
            # A collection page and a part page have another owner; its links are counted apart.
            into = scan if item.owned else foreign
            if data is None:
                into.counts.unreadable_pages += 1
                continue
            rewriter.scan_managed(into, item.path, data, frozenset(facts[item.path].ingested))
        for path, data in inventory.read_all(outside_markdown):
            if data is not None:
                rewriter.scan_outside(scan, path, data)
        pointers = plan_pointers(facts.values(), moved, rewriter)
        marks["links"] = time.monotonic()

        built = alternatives.build(
            placements=placements,
            primary=allocations,
            primary_paths=moved,
            facts=facts,
            registry=registry,
            unresolved=scan.unresolved,
            today_paths=[
                *(item.path for item in inventory.managed if item.markdown),
                *outside_markdown,
            ],
        )
        marks["alternatives"] = time.monotonic()
        part_folders = {
            posixpath.dirname(path)
            for path in moved
            if extraction_parts.source_page_for(PurePosixPath(path)) is not None
        }
        unregistered = Counter(
            placement.unregistered_folder
            for placement in placements
            if placement.unregistered_folder is not None
        )
        result = RelayoutPlan(
            current=resolved.layout,
            target=wanted,
            obsidian_config=_obsidian_config(root),
            registry_findings=registry.findings,
            leftover_schema_folders=resolved.leftover_schema_folders,
            moves=_moves(placements, allocations, moved),
            moved=moved,
            facts=facts,
            registry=registry,
            unmappable=allocations.unmappable,
            unregistered_folders=tuple(sorted(unregistered.items())),
            links=scan,
            pointers=pointers,
            outside=_outside_summaries(inventory, scan),
            collisions=_collisions(inventory, moved),
            alternatives=built.report,
            collection_links=foreign,
            flattened=built.flattened,
            hidden_entries=tuple(inventory.hidden),
        )
        result.disclosure = build_disclosure(result, root)
        flat = built.report["o1_flatten"]
        if isinstance(flat, dict):
            flat["scopes_whose_members_change"] = flatten_report(result.disclosure)
        marks["disclosure"] = time.monotonic()
        stages = list(marks.items())
        timings = {
            name: round(finished - (stages[index - 1][1] if index else started), 3)
            for index, (name, finished) in enumerate(stages)
        }
        timings["total"] = round(time.monotonic() - started, 3)
        result.diagnostics = {
            "link_self_check_failures": [asdict(miss) for miss in scan.failures],
            "pointer_failures": [
                {"page": note.page, "field": note.field, "value": note.value, "reason": note.reason}
                for note in pointers.failures
            ],
            "files_listed": len(inventory.managed),
            "files_skipped": dict(sorted(inventory.skipped.items())),
            "unreadable_files": sorted([*unreadable, *unreadable_outside]),
            "part_folders": len(part_folders),
            "registry_findings": list(registry.findings),
            "limits": [
                "A link that resolves to no page and names a path inside a collection layer counts as a link to a retired collection path; the collection store is not read, so a path that never existed counts there too.",
            ],
            READ_KEY: {
                "mode": "store" if inventory.revision is not None else "file",
                "store_revision": inventory.revision,
                "timings_seconds": timings,
            },
        }
    return result
