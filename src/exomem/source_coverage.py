"""Source coverage: which captured Sources a live compiled page compiles.

A Source's `ingested_into` records every page that cites it, whatever that page's
status. It is a citation edge, never a completion claim. A Source counts as
compiled when at least one page it names

1. exists,
2. has a status whose registry lifecycle class is `live`, and
3. cites the Source in its own `sources:` claim, as source closure resolves it.

A page that is itself a Source or Evidence page holds no resolvable claim, so rule 3
rejects it. Every other Source stays in the `unprocessed_source` family, and each
derivative that fails reports one reason: `missing`, `pending`, `retired` or
`claim_unresolved`.

The audit family and the due-state projection both evaluate through this module, so
the two never hold two definitions of "compiled". The rule branches only on the
registry's closed lifecycle `class` and note-type `role`, never on an entry key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import lifecycle_statuses, note_types, source_closure, vault
from .find_types import ParsedPage

#: The closed reasons a derivative fails to compile its Source.
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
#: Raw characters read before whitespace collapses, so one excerpt never walks a whole body.
_SCAN_CHARS = 8 * EXCERPT_CHARS


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


def excerpt(root: Path, page: ParsedPage, limit: int = EXCERPT_CHARS) -> str:
    """The opening prose of a Source, as the current caller may read it, within `limit` characters.

    The page's own heading is the title the finding already carries, so the excerpt
    starts after it. Carrier spans are withheld the way every other door that serves
    page prose to a caller withholds them.
    """
    from .governance import egress

    prose = egress.prose_for_caller(root, page.body[:_SCAN_CHARS], owner_path=page.rel_path)
    lines = prose.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return _clip(" ".join(" ".join(lines).split()), limit)


def context(root: Path, page: ParsedPage, coverage: Coverage) -> dict[str, Any]:
    """What an agent needs to group and act on a Source finding, each field only when recorded.

    The server does not group: it reports the metadata the import recorded and the
    derivative states, and the agent decides which Sources belong in one note.
    """
    frontmatter: Mapping[str, Any] = page.frontmatter
    out: dict[str, Any] = {"title": _clip(page.title, TITLE_CHARS)}
    opening = excerpt(root, page)
    if opening:
        out["excerpt"] = opening
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
    failing = coverage.failing
    if failing:
        out["derivatives"] = [item.public() for item in failing[:DERIVATIVES_LISTED]]
        out["derivative_count"] = len(failing)
    return out
