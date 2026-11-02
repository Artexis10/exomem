"""The `compile` block of a recorded recap: the pages it names, for the close step.

A session's knowledge belongs on the pages it changed, and the recap is filed
after those pages are updated. This block lists the pages a recap names, each
with its last update time, so the agent can see which of them it has not
updated yet.

It claims nothing the server cannot know. The episode ledger binds recap inputs
only and ordinary writes carry no episode key, so the server cannot tell when a
session started or which writes belong to it: "updated this session" is the
agent's judgement, never a field here. The `outcome` states the result only; a
next step rides separately and only when live policy permits capture, so a recap
request never orders a page write that the policy forbids.

A page the caller may not see, or a link that resolves to no page, is left out
and never counted, so a withheld page reads exactly like an absent one.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import access, capture_sweep, find, memory_refs, vault

log = logging.getLogger(__name__)

# Where a page's reported update time came from. The three values are this module's
# own report of which branch of `_page` answered, not a vocabulary a vault defines.
_FROM_FRONTMATTER = "frontmatter"
_FROM_FILE = "file_mtime"
_UNREADABLE = "unreadable"

# What `updated` holds for a page that resolved but cannot be read: unknown is a
# visible state, never a date and never absent.
_UNKNOWN_TIME = "unknown"

# The one next step, sent only when live policy permits capture. It is a
# condition for the agent to judge, because the server cannot tell which listed
# pages this session already updated.
_NEXT_STEP = "If a decided item changes a listed page that this session has not updated, update that page."


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
    is the writers' own snapshot, taken without the disk fallback: a cold cache
    would cost a walk of the whole vault on every record, so the snapshot
    declines and the links are reported unchecked instead.
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
        resolver = find.writer_resolver_snapshot(root, allow_disk=False)
        if resolver is None:
            # `normalize_wikilink` would build its own resolver from disk: never reach it.
            return None
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


def _page(root: Path, path: str, ref: str | None) -> tuple[dict[str, str], str]:
    """One page's ref, path, title and update time, plus the time's source.

    A page that resolved but cannot be read stays listed with an unknown time:
    it must not read as a page the recap does not name.
    """
    frontmatter: Mapping[str, Any] = {}
    body = ""
    updated, source = _UNKNOWN_TIME, _UNREADABLE
    try:
        text, _guard = vault.read_guarded_text(root, root / path)
        frontmatter, body, _raw = vault.parse_frontmatter(text)
        updated, source = _updated(root, path, frontmatter)
    except (OSError, ValueError):
        log.debug("episode compile page unreadable: %s", path, exc_info=True)
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
    """The result of the recap's links, never an instruction."""
    if unlinked is None:
        return "This recap's links were not checked."
    names = "no page" if not pages else f"{pages} page" if pages == 1 else f"{pages} pages"
    if not unlinked:
        return f"This recap names {names}."
    clause = "1 decided item names" if unlinked == 1 else f"{unlinked} decided items name"
    return f"This recap names {names}; {clause} no page."


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
    root = Path(vault_root)
    try:
        keep = vault.writer_link_visibility(root)
        pages: dict[str, dict[str, str]] = {}
        sources: dict[str, str] = {}

        def add(path: str, ref: str | None = None) -> None:
            if path not in pages:
                pages[path], sources[path] = _page(root, path, ref)

        for ref, path in about_paths.items():
            if keep is None or keep(path):
                add(path, ref)
        for paths in decided_links or ():
            for path in paths:
                add(path)
        if not pages and not decided:
            return None
        unlinked = None if decided_links is None else sum(not paths for paths in decided_links)
        # A next step only when policy permits capture, and only with a page to act on.
        permitted = capture_sweep._proactive_capture_permitted()  # noqa: SLF001
        return {
            "outcome": _outcome(len(pages), unlinked),
            **({"next_step": _NEXT_STEP} if pages and permitted else {}),
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
