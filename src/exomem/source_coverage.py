"""Source citation linkage for the existing unprocessed-source backlog.

A Source's `ingested_into` records every page that cites it, whatever that page's
status. It is a citation edge, never a completion claim. A Source clears the
citation backlog when at least one page it names

1. exists,
2. has a status whose registry lifecycle class is `live`, and
3. cites the Source in its own `sources:` claim, as source closure resolves it.

A page that is itself a Source or Evidence page holds no resolvable claim, so rule 3
rejects it. Every other Source stays in the `unprocessed_source` family, and each
derivative that fails reports one reason: `missing`, `pending`, `retired` or
`claim_unresolved`.

The audit family and due-state projection share this criterion. The compatibility
property `compiled` means citation linkage only; semantic compilation remains
unassessed. The rule branches only on registry lifecycle `class` and note-type `role`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import lifecycle_statuses, note_types, source_closure, vault
from .find_types import ParsedPage

#: The closed reasons a derivative fails the citation-backlog criterion.
MISSING = "missing"
PENDING = "pending"
RETIRED = "retired"
CLAIM_UNRESOLVED = "claim_unresolved"

#: A finding names at most this many failing derivatives and counts the rest.
DERIVATIVES_LISTED = 5
#: Bounds on the text a finding or a compilation scaffold carries from a Source.
TITLE_CHARS = 200
EXCERPT_CHARS = 400
FIELD_CHARS = 200
#: The excerpts of one compilation scaffold share this many characters.
SCAFFOLD_EXCERPT_CHARS = 6000
#: An excerpt shorter than this says too little to group by, so the scaffold omits it.
MIN_EXCERPT_CHARS = 60
#: Raw characters read before whitespace collapses, so one excerpt never walks a whole body.
_SCAN_CHARS = 8 * EXCERPT_CHARS
#: Shared response bounds for admitted compilation routes, not a source coverage quota.
COMPILATION_SOURCES_LISTED = 5
COMPILATION_PATH_CHARS = 2048


@dataclass(frozen=True, slots=True)
class Derivative:
    """One `ingested_into` entry judged against the coverage rule."""

    reference: str
    path: str | None
    reason: str | None
    status_class: str | None
    claim_resolved: bool

    def public(self) -> dict[str, str]:
        """The failing derivative as a finding shows it: the entry, where it points, why."""
        row = {"reference": _clip(self.reference, FIELD_CHARS), "reason": str(self.reason)}
        if self.path is not None:
            row["path"] = self.path
        return row


@dataclass(frozen=True, slots=True)
class Coverage:
    derivatives: tuple[Derivative, ...]

    @property
    def compiled(self) -> bool:
        """Compatibility name for one live citing derivative, not semantic completion."""
        return any(item.reason is None for item in self.derivatives)

    @property
    def failing(self) -> tuple[Derivative, ...]:
        return tuple(item for item in self.derivatives if item.reason is not None)

    def signal(self) -> str:
        """Identity of the derivative states, folded into the finding's fingerprint.

        Covers each derivative's reference, status class and claim result, so a
        derivative that moves from draft to retired, loses its claim or disappears
        changes the finding a reviewer already dismissed.
        """
        digest = hashlib.sha256()
        for item in self.derivatives:
            digest.update(
                "\x1f".join(
                    (item.reference, str(item.status_class), str(item.claim_resolved), str(item.reason))
                ).encode("utf-8", errors="replace")
                + b"\x1e"
            )
        return digest.hexdigest()[:16]


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _entries(value: Any) -> tuple[str, ...]:
    """The `ingested_into` entries, flattened: an unquoted `[[x]]` parses as a nested list."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple):
        return tuple(dict.fromkeys(entry for item in value for entry in _entries(item)))
    return ()


def _reason(status_class: str | None, claim_resolved: bool) -> str | None:
    if status_class == "live":
        return None if claim_resolved else CLAIM_UNRESOLVED
    # A status the registry cannot classify has not shown itself live: it stays pending.
    if status_class == "pending" or status_class is None:
        return PENDING
    return RETIRED


def evaluate(
    root: Path,
    source: ParsedPage,
    *,
    lookup: Callable[[str], ParsedPage | None],
    resolver: Callable[[], vault.WikilinkResolver],
    status_basis: lifecycle_statuses.Basis,
    type_basis: note_types.Basis,
) -> Coverage:
    """Judge every page `source` names in `ingested_into`.

    `lookup` returns the parsed page at a vault-relative path, or None when the
    caller has no such page to see. The audit passes its admitted pages, so a page
    a caller may not read reports as missing, exactly as it would in a vault
    without it. `resolver` is called only when the Source names a derivative.
    """
    references = _entries(source.frontmatter.get("ingested_into"))
    if not references:
        return Coverage(())
    snapshot = resolver()
    derivatives: list[Derivative] = []
    for reference in references:
        path = source_closure.locate(root, reference, resolver=snapshot)
        page = lookup(path) if path is not None else None
        if page is None:
            derivatives.append(Derivative(reference, None, MISSING, None, False))
            continue
        status_class = status_basis.classify(
            page.frontmatter.get("status"), path=page.rel_path, frontmatter=page.frontmatter
        ).lifecycle_class
        claim_resolved = _claims(root, page, source.rel_path, snapshot, type_basis)
        derivatives.append(
            Derivative(reference, page.rel_path, _reason(status_class, claim_resolved), status_class, claim_resolved)
        )
    return Coverage(tuple(derivatives))


def _claims(
    root: Path,
    page: ParsedPage,
    source_path: str,
    resolver: vault.WikilinkResolver,
    type_basis: note_types.Basis,
) -> bool:
    """Whether the page's `sources:` claim resolves to `source_path` under source closure."""
    return any(
        source_closure.resolve_reference(root, value, resolver=resolver) == source_path
        for value in source_closure.claims_in(page.frontmatter, type_basis)
    )


def _text(value: object) -> str | None:
    return _clip(value.strip(), FIELD_CHARS) if isinstance(value, str) and value.strip() else None


#: The heading that opens the captured text on a Source page. Both Source writers
#: (capture and import) emit it from here, so it is part of the page shape, not a guess.
CAPTURE_HEADING = "## Capture"
#: Fence lines wrap the original text of an imported Source.
_FENCES = ("```", "~~~")


def _captured_text(body: str) -> str:
    """The first `_SCAN_CHARS` of a Source's captured text, after its `## Capture` heading.

    The lines above the heading say where the page came from (a copy notice, the
    original path and hash), which tells an agent nothing about what the Source says.
    Without the heading the body's own opening stands in, minus the page title.
    A search for the heading, not a split of the body: a Source can run to megabytes.
    """
    at = body.find(f"\n{CAPTURE_HEADING}")
    while at != -1:
        end = at + 1 + len(CAPTURE_HEADING)
        if body[end : end + 1] in ("", "\n", "\r", " ", "\t"):
            window = body[end : end + _SCAN_CHARS]
            break
        at = body.find(f"\n{CAPTURE_HEADING}", end)
    else:
        window = body[:_SCAN_CHARS]
        if window.startswith("# "):
            window = window.partition("\n")[2]
    return "\n".join(
        line for line in window.splitlines() if not line.lstrip().startswith(_FENCES)
    )


def excerpt(root: Path, page: ParsedPage, limit: int = EXCERPT_CHARS) -> str:
    """The opening of a Source's captured text, as the current caller may read it, within `limit`.

    Carrier spans are withheld the way every other door that serves page prose to a
    caller withholds them.
    """
    from .governance import egress

    prose = egress.prose_for_caller(root, _captured_text(page.body), owner_path=page.rel_path)
    return _clip(" ".join(prose.split()), limit)


def compilation_guidance(root: Path, paths: Sequence[str]) -> dict[str, Any]:
    """Informational assessment state and bounded routes over released Source paths."""
    from .governance import egress

    keep = egress.restricted_release_filter(root)
    # Citation closure owns parent-reference syntax; preparation reads the whole source.
    source_paths = (
        source_closure._target(path).removesuffix(".md") + ".md" for path in paths
    )
    released = list(dict.fromkeys(
        path for path in source_paths if keep is None or keep(path)
    ))
    selected = [path for path in released if len(path) <= COMPILATION_PATH_CHARS][
        :COMPILATION_SOURCES_LISTED
    ]
    out = compilation_routes(selected)
    if len(selected) < len(released):
        out["truncated"] = True
    return out


def compilation_routes(paths: Sequence[str]) -> dict[str, Any]:
    """Render already admitted, bounded paths; callers own admission and bounds."""
    # `unassessed` is a declared protocol state, never inferred from citations or extraction.
    out: dict[str, Any] = {"state": "unassessed"}
    if paths:
        # Inherit the caller's purpose; replacing it can invalidate an admitted session grant.
        out["full_source_reads"] = [
            {"tool": "read_memory", "args": {
                "path": path, "include_raw": True,
            }}
            for path in paths
        ]
        out["preparation_route"] = {"tool": "prepare_note", "args": {"sources": list(paths)}}
    return out


def describe(
    root: Path, page: ParsedPage, *, excerpt_chars: int | None = EXCERPT_CHARS
) -> dict[str, Any]:
    """What an agent needs to group a Source: title, opening excerpt and recorded metadata.

    Each metadata field appears only when the import recorded it, and the excerpt
    only when `excerpt_chars` is set. The server does not group: the agent decides
    which Sources belong in one note.
    """
    frontmatter: Mapping[str, Any] = page.frontmatter
    out: dict[str, Any] = {
        "title": _clip(page.title, TITLE_CHARS),
        "semantic_compilation": compilation_guidance(root, [page.rel_path]),
    }
    opening = excerpt(root, page, excerpt_chars) if excerpt_chars else ""
    if opening:
        out["excerpt"] = opening
        out["excerpt_scope"] = "opening_only"
    collection_path = _text(frontmatter.get("collection_path"))
    folder = PurePosixPath(collection_path).parent.as_posix() if collection_path else "."
    for key, value in (
        ("import_collection", _text(frontmatter.get("import_collection"))),
        ("collection_folder", folder if folder != "." else None),
        # The Source's front matter records its kind as `source_type`.
        ("source_kind", _text(frontmatter.get("source_type"))),
        ("domain", _text(frontmatter.get("domain"))),
    ):
        if value is not None:
            out[key] = value
    return out


def context(root: Path, page: ParsedPage, coverage: Coverage) -> dict[str, Any]:
    """`describe` plus derivatives that fail the citation-backlog criterion, for a finding."""
    out = describe(root, page)
    failing = coverage.failing
    if failing:
        out["derivatives"] = [item.public() for item in failing[:DERIVATIVES_LISTED]]
        out["derivative_count"] = len(failing)
    return out
