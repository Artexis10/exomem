"""Compile coverage of a conversation recap, by citation.

A recap is a Source with `ingested_into: []`. When a live compiled note cites it in
`sources:`, source closure writes the back-ref and `source_coverage.evaluate` counts
the recap compiled for the caller. An episode's coverage reads that one signal over
the episode's revisions: a Decided line of the live revision that no cited revision
holds is open.

Coverage counts recaps and lines. It never claims that a note states a decided item:
it is "cited by a live page (citation only; items unassessed)". Known limit: entity,
plan and record pages hold no `sources:` claim, so a compile into one of them leaves
its line open.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import (
    episode_capture,
    lifecycle_statuses,
    memory_refs,
    note_types,
    review_state,
    source_coverage,
    source_taxonomy,
    vault,
)
from .find_types import ParsedPage

WORDING = "cited by a live page (citation only; items unassessed)"


def folder(vault_root: Path) -> Path:
    """The folder that holds every recap Source."""
    return vault.kb_root(vault_root) / source_taxonomy.SOURCES_ROOT / source_taxonomy.EPISODE_PATH_LABEL


@dataclass(frozen=True, slots=True)
class Coverage:
    """One episode's live revision, and which of its Decided lines no cited revision holds."""

    key: str
    path: str
    ref: str | None
    captured: str | None
    decided: int
    open: tuple[str, ...]
    #: Revisions a live page cites, and the paths of the pages that cite them.
    cited: int
    citing: tuple[str, ...]

    @classmethod
    def of(
        cls,
        key: str,
        path: str,
        frontmatter: Mapping[str, Any],
        decided: Sequence[str],
        hits: Sequence[ParsedPage],
        citing: Iterable[str],
    ) -> Coverage:
        """The coverage of the live revision at `path`, whose Decided lines are `decided`, given the cited revisions `hits`."""
        held = {line for page in hits for line in decided_lines(page.body)}
        memory_id = memory_refs.normalize_id(str(frontmatter.get("exomem_id") or ""))
        captured = frontmatter.get("captured")
        return cls(
            key=key,
            path=path,
            ref=memory_refs.memory_ref(memory_id) if memory_id else None,
            captured=str(captured) if captured else None,
            decided=len(decided),
            open=tuple(line for line in decided if line not in held),
            cited=len(hits),
            citing=tuple(sorted(set(citing))),
        )


def decided_lines(body: str) -> tuple[str, ...]:
    """The Decided lines of a recap page's body, or `()` when the body is not a recap as recorded.

    The recap shape is closed and server-written: a body that `episode_capture.parse`
    does not accept, as a hand edit is not, yields no lines rather than a partial reading.
    """
    _head, found, rest = f"\n{body}".partition(f"\n{source_coverage.CAPTURE_HEADING}\n\n")
    parsed = episode_capture.parse(rest.removesuffix("\n")) if found else None
    return () if parsed is None else parsed.sections["decided"]


def episodes(pages: Iterable[ParsedPage]) -> list[list[ParsedPage]]:
    """The recorded recap pages among `pages`, grouped by episode, each group oldest first.

    An episode is the recap pages that share a frontmatter `episode` key and the
    filename group the recorder retires revisions within, so one audience's chain
    never merges with another's. The filename order token orders them without a read.
    """
    groups: dict[tuple[str, str], list[tuple[str, ParsedPage]]] = {}
    for page in pages:
        parts = episode_capture.filename_parts(page.rel_path)
        key = page.frontmatter.get("episode")
        # `episode` is the server-reserved source kind, not a vocabulary entry.
        if page.frontmatter.get("source_type") == source_taxonomy.EPISODE_KIND and parts and isinstance(key, str):
            groups.setdefault((key, parts[0]), []).append((parts[1], page))
    return [[page for _order, page in sorted(group, key=lambda row: row[0])] for group in groups.values()]


def cited(
    root: Path,
    revisions: Sequence[ParsedPage],
    *,
    lookup: Callable[[str], ParsedPage | None],
    resolver: Callable[[], vault.WikilinkResolver],
    status_basis: lifecycle_statuses.Basis,
    type_basis: note_types.Basis,
) -> tuple[list[ParsedPage], list[str]]:
    """The revisions a live page cites for this caller, and the paths of the pages that cite them.

    `lookup` is the caller's: a citing page it cannot see reads as absent.
    """
    hits: list[ParsedPage] = []
    citing: list[str] = []
    for page in revisions:
        found = source_coverage.evaluate(
            root, page, lookup=lookup, resolver=resolver, status_basis=status_basis, type_basis=type_basis
        )
        if found.compiled:
            hits.append(page)
            citing.extend(item.path for item in found.derivatives if item.reason is None and item.path)
    return hits, citing


def evaluate(
    root: Path,
    revisions: Sequence[ParsedPage],
    *,
    lookup: Callable[[str], ParsedPage | None],
    resolver: Callable[[], vault.WikilinkResolver],
    status_basis: lifecycle_statuses.Basis,
    type_basis: note_types.Basis,
) -> Coverage | None:
    """The coverage of one episode's revisions (oldest first), or `None` when none is live."""
    live = next(
        (
            page
            for page in reversed(revisions)
            if status_basis.classify(
                page.frontmatter.get("status"), path=page.rel_path, frontmatter=page.frontmatter
            ).lifecycle_class
            != "superseded"
        ),
        None,
    )
    if live is None:
        return None
    hits, citing = cited(
        root, revisions, lookup=lookup, resolver=resolver, status_basis=status_basis, type_basis=type_basis
    )
    return Coverage.of(
        str(live.frontmatter["episode"]), live.rel_path, live.frontmatter, decided_lines(live.body), hits, citing
    )


def survey(
    root: Path,
    pages: Iterable[ParsedPage],
    *,
    lookup: Callable[[str], ParsedPage | None] | None = None,
    resolver: Callable[[], vault.WikilinkResolver] | None = None,
    status_basis: lifecycle_statuses.Basis | None = None,
) -> list[Coverage]:
    """The coverage of every live episode among `pages`.

    `lookup` finds a citing page and defaults to `pages`, so a citing page the caller
    cannot see reads as absent. The vault resolver is built on first use: a vault whose
    recaps are all uncited never pays for it.
    """
    pages = list(pages)
    status_basis = status_basis or lifecycle_statuses.Basis(root)
    type_basis = note_types.Basis(root)
    if lookup is None:
        lookup = {page.rel_path: page for page in pages}.get
    if resolver is None:

        @lru_cache(maxsize=1)
        def resolver() -> vault.WikilinkResolver:
            from . import find

            return find.writer_resolver_snapshot(root)

    found = (
        evaluate(
            root, revisions, lookup=lookup, resolver=resolver, status_basis=status_basis, type_basis=type_basis
        )
        for revisions in episodes(pages)
    )
    return [coverage for coverage in found if coverage is not None]


def acted_ids(vault_root: Path) -> frozenset[str]:
    """The review item ids of the live recaps whose every Decided line a cited revision now holds.

    An item that was open and is now in this set was acted on: a live page cites a
    recap that holds its lines. A recap that was superseded, deleted or lost a line
    is not live or not fully held, so it is never in the set: that is `cleared`, not
    `acted`. Owner-only: it reads every recap and every citing page unfiltered.
    """
    from . import find

    base = folder(vault_root)
    try:
        names = sorted(entry.name for entry in os.scandir(base) if entry.is_file())
    except FileNotFoundError:
        return frozenset()
    pages = [find._CACHE.get(base / name, vault_root) for name in names]
    covered = survey(
        vault_root,
        [page for page in pages if page is not None],
        lookup=lambda path: find._CACHE.get(vault_root / path, vault_root),
    )
    return frozenset(
        review_state.item_id(coverage.ref) for coverage in covered if coverage.ref and coverage.decided and not coverage.open
    )


def revision_paths(vault_root: Path, paths: Iterable[str]) -> set[str]:
    """Every revision path of each episode that a recap among `paths` belongs to, from one folder listing.

    A citation of one revision changes the coverage of the live one, so a write that
    cites a recap recomputes the whole episode.
    """
    groups = {parts[0] for path in paths if (parts := episode_capture.filename_parts(path))}
    if not groups:
        return set()
    base = folder(vault_root)
    try:
        names = [entry.name for entry in os.scandir(base) if entry.is_file()]
    except FileNotFoundError:
        return set()
    return {
        (base / name).relative_to(vault_root).as_posix()
        for name in names
        if (parts := episode_capture.filename_parts(name)) and parts[0] in groups
    }
