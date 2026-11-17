"""A Records collection's declared capture method: the one vault page that says how to capture into it.

The manifest keeps the author's reference (`capture.method`). This resolves it, for the caller asking, to a
page that caller may read. A reference that names nothing and a page the caller may not read both resolve to
nothing, and nothing here tells the two apart, so no answer shows that a withheld page exists. A resolution is
advisory: routing never depends on it, so a failure is an omitted pointer, never an error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import memory_refs, page_names, reserved_paths, vault
from .governance import egress

# The compact write terminal validates what this module produces, so it owns the bounds: a method that
# met them here is never the reason the terminal drops the whole advisory.
from .mutation_terminal import _MAX_ROUTING_COLLECTION_CHARS as REF_MAX_CHARS
from .mutation_terminal import _MAX_ROUTING_TITLE_CHARS as TITLE_MAX_CHARS

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaptureMethod:
    """The page a collection's `capture.method` names, as the caller asking may read it.

    `ref` is the page's memory ref, which survives a move or a rename; a page that holds no id is named by
    its path.
    """

    path: str
    ref: str
    title: str


def locate(
    vault_root: Path, reference: str, *, resolver_for: Callable[[], Any | None] | None = None
) -> str | None:
    """The vault path of the page `reference` names, or None; it decides nothing about who may read it.

    Only the page's release decision for the caller asking admits it to anyone, so a caller of this
    function owns that decision. A path or a memory ref needs no resolver. A title or a stem needs the
    broad resolver, which `resolver_for` supplies (see `page_names.resolve_page`); a read on a request
    path passes a lookup that never builds it, and a title it cannot resolve names nothing.
    """
    root = Path(vault_root)
    try:
        page, _anchor = vault.split_wikilink_target(reference)
        if not page:
            return None
        path = page_names.resolve_page(root, page, serve_stale=True, resolver_for=resolver_for)
        confined = (root / path).resolve()
        # A path that leaves the vault, or that a link spelled around, is no page of this vault.
        if (
            not path.endswith(".md")
            or not confined.is_relative_to(root.resolve())
            or confined.relative_to(root.resolve()).as_posix() != path
            or not confined.is_file()
            or reserved_paths.classify_logical(path).blocked
        ):
            return None
        return path
    except Exception:  # noqa: BLE001 -- a pointer that cannot be resolved is omitted, never an error
        log.debug("capture method resolution failed (non-fatal)", exc_info=True)
        return None


def resolve(
    vault_root: Path, reference: str, *, readable: Callable[[str], bool] | None = None
) -> CaptureMethod | None:
    """The page `reference` names when `readable` admits it, else None.

    `readable` decides one vault path for the caller asking; left out, it is the release decision that
    every page read makes. A page whose reference does not fit the write terminal's bound resolves to
    nothing, so the advisory it would ride on is never dropped for it.
    """
    root = Path(vault_root)
    path = locate(root, reference)
    if path is None or not (readable(path) if readable is not None else egress.quick_page_visible(root, path)):
        return None
    try:
        identity = page_names.read_identity(root, path)
        if identity is None:
            return None
        owner = memory_refs.read_reference_owner(root, root / path)
        title = " ".join(egress.without_wikilinks(identity.title).split())
        if not title:
            return None
        ref = memory_refs.memory_ref(owner) if owner else path
        if len(ref) > REF_MAX_CHARS:
            return None
        return CaptureMethod(
            path=path,
            ref=ref,
            title=title if len(title) <= TITLE_MAX_CHARS else f"{title[: TITLE_MAX_CHARS - 1].rstrip()}\u2026",
        )
    except Exception:  # noqa: BLE001 -- a pointer that cannot be resolved is omitted, never an error
        log.debug("capture method resolution failed (non-fatal)", exc_info=True)
        return None
