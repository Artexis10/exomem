"""The `compile` block of a recorded recap: the pages it names, for the close step.

A session's knowledge belongs on the pages it changed, and the recap is filed
after those pages are updated. This block lists the pages a recap names, each
with its last update time, so the agent can see which of them it has not
updated yet.

It claims nothing the server cannot know. The episode ledger binds recap inputs
only and ordinary writes carry no episode key, so the server cannot tell when a
session started or which writes belong to it: "updated this session" is the
agent's judgement, never a field here.

A page the caller may not see, or a link that resolves to no page, is left out
and never counted, so a withheld page reads exactly like an absent one.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import access, find, memory_refs, vault

log = logging.getLogger(__name__)

# Where a page's reported update time came from. The two values are this module's
# own report of which branch of `_updated` answered, not a vocabulary a vault defines.
_FROM_FRONTMATTER = "frontmatter"
_FROM_FILE = "file_mtime"

_CLOSE_STEP = "record again with each decided item linked to its page"


def linked_pages(
    vault_root: Path, decided: Sequence[str]
) -> tuple[tuple[str, ...], ...] | None:
    """For each Decided item, the vault-relative paths its `[[...]]` links resolve to.

    Only pages the caller may see resolve (`writer_link_visibility`), so a link to
    a withheld page yields what a link to a missing page yields: nothing. `None`
    means the links were not checked because resolution failed; it never means
    that no item links a page.

    Call this before the recap is written: the write leaves the resolver cache
    behind its own change until the terminal's fanout catches it up. The resolver
    is the writers' own (`edit_note` takes it the same way), so a cold cache
    costs the walk those writers pay.
    """
    targets = [
        [
            vault.split_wikilink_target(match.group(1))[0]
            for match in vault.iter_body_wikilinks(item)
        ]
        for item in decided
    ]
    if not any(targets):
        return tuple(() for _ in decided)
    root = Path(vault_root)
    try:
        resolver = find.writer_resolver_snapshot(root)
        keep = vault.writer_link_visibility(root)
        resolved = [[_resolved(root, page, resolver, keep) for page in pages] for pages in targets]
        return tuple(tuple(dict.fromkeys(path for path in paths if path)) for paths in resolved)
    except Exception:  # noqa: BLE001 - the recap is not yet written; an unchecked link is reported, never raised
        log.debug("episode compile links unchecked", exc_info=True)
        return None


def _resolved(root: Path, page: str, resolver: Any, keep: Any) -> str:
    """The one visible page `page` names as a vault-relative path, or `""`."""
    try:
        canonical, _warning = vault.normalize_wikilink(
            page, root, resolver=resolver, strict=True, visible=keep
        )
    except (vault.UnresolvedWikilinkError, vault.AmbiguousWikilinkError):
        return ""
    path = canonical.split("#", 1)[0] + ".md"
    return "" if access.refuse_if_excluded(root, path) else path


def _updated(root: Path, path: str, frontmatter: Mapping[str, Any]) -> tuple[str, str]:
    """The page's last update time and which source gave it."""
    value = frontmatter.get("updated")
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds"), _FROM_FRONTMATTER
    if isinstance(value, dt.date):
        return value.isoformat(), _FROM_FRONTMATTER
    if isinstance(value, str):
        try:
            dt.datetime.fromisoformat(value.strip())
        except ValueError:
            pass
        else:
            return value.strip(), _FROM_FRONTMATTER
    modified = dt.datetime.fromtimestamp((root / path).stat().st_mtime).astimezone()
    return modified.isoformat(timespec="seconds"), _FROM_FILE


def _page(root: Path, path: str, ref: str | None) -> tuple[dict[str, str], str] | None:
    """One page's ref, path, title and update time, plus the time's source; `None` if unreadable."""
    try:
        text, _guard = vault.read_guarded_text(root, root / path)
        frontmatter, body, _raw = vault.parse_frontmatter(text)
        updated, source = _updated(root, path, frontmatter)
    except (OSError, ValueError):
        return None
    if ref is None:
        memory_id = memory_refs.normalize_id(str(frontmatter.get("exomem_id") or ""))
        ref = memory_refs.memory_ref(memory_id) if memory_id else None
    page = {
        "path": path,
        "title": vault.resolve_display_title(frontmatter, body, path),
        "updated": updated,
    }
    return ({"ref": ref, **page} if ref else page), source


def _outcome(pages: int, unlinked: int | None) -> str:
    if unlinked is None:
        return f"This recap committed, but its page links were not checked. Update the pages this session changed, then {_CLOSE_STEP}."
    if not pages:
        return f"This recap names no page. Update the page each decided item changes, then {_CLOSE_STEP}."
    names = f"{pages} page" if pages == 1 else f"{pages} pages"
    head = f"This recap names {names}. Update any that a decided item changes and this session has not updated yet"
    if not unlinked:
        return f"{head}."
    clause = "1 decided item names" if unlinked == 1 else f"{unlinked} decided items name"
    return f"{head}; {clause} no page."


def block(
    vault_root: Path,
    *,
    decided: int,
    decided_links: tuple[tuple[str, ...], ...] | None,
    about_paths: Mapping[str, str],
) -> dict[str, Any] | None:
    """The `compile` block, or `None` when the recap names no page and has no Decided item.

    `about_paths` are the recap's `about` refs the caller may see, by path;
    `decided_links` are `linked_pages` for its Decided items. Never raises: the
    recap is already committed.
    """
    if not decided and not about_paths:
        return None
    root = Path(vault_root)
    try:
        keep = vault.writer_link_visibility(root)
        pages: dict[str, dict[str, str]] = {}
        sources: dict[str, str] = {}

        def add(path: str, ref: str | None = None) -> bool:
            if path not in pages:
                found = _page(root, path, ref)
                if found is None:
                    return False
                pages[path], sources[path] = found
            return True

        for ref, path in about_paths.items():
            if keep is None or keep(path):
                add(path, ref)
        unlinked: int | None = None
        if decided_links is not None:
            # `add` runs for every link of an item, so the listing never depends on short-circuiting.
            unlinked = sum(
                not any([add(path) for path in paths]) for paths in decided_links
            )
        return {
            "outcome": _outcome(len(pages), unlinked),
            "pages": list(pages.values()),
            **({"unlinked_decided": unlinked} if unlinked is not None else {}),
            "diagnostics": {
                "updated_source": sources,
                "links": "unchecked" if unlinked is None else "checked",
            },
        }
    except Exception:  # noqa: BLE001 - the recap already committed; the block is advisory
        log.debug("episode compile block unavailable", exc_info=True)
        return {
            "outcome": _outcome(0, None),
            "pages": [],
            "diagnostics": {"links": "unchecked"},
        }
