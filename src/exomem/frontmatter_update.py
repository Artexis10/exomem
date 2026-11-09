"""The one frontmatter-only write of an existing page that a duplicate-handling door makes.

A door that finds identical bytes already stored records what it learned on that page: a family
(Evidence) or an import mapping (Sources). The write sets named frontmatter fields, keeps the body and
every other field as written, and lands through a content-hash-guarded write, so a page that changed
since it was read is never overwritten.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import vault
from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter


def updated_text(
    text: str, fields: Mapping[str, object], appended: Mapping[str, Sequence[str]]
) -> str:
    """`text` with `fields` set and `appended` list items present; the body and every other field stay as written."""
    updated = splice_markdown_frontmatter(text, dict(fields)) if fields else text
    for key, items in appended.items():
        for item in items:
            updated = vault.append_frontmatter_list_item(updated, key, item)
    before_fm, before_body, _ = vault.parse_frontmatter(text, strict=True)
    after_fm, after_body, _ = vault.parse_frontmatter(updated, strict=True)
    owned = {*fields, *appended}
    kept = {key: value for key, value in before_fm.items() if key not in owned}
    if after_body != before_body or {key: after_fm.get(key) for key in kept} != kept:
        raise FrontmatterSpliceError("INVALID_FRONTMATTER", "the update changed more than the fields it owns")
    return updated


def update_page_frontmatter(
    vault_root: Path,
    page: str,
    fields: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object] | None],
    *,
    appended: Mapping[str, Sequence[str]] = {},  # noqa: B006 - read-only default, never mutated
) -> bool:
    """Set `fields` and append `appended` list items on the live page at `page`. False when it did not land.

    `fields` may be a function of the page's frontmatter as this write reads it, so a field built from the
    old value (a list that gains one entry) never rests on an earlier read. It returns None to decline.
    False means the page changed since it was read, cannot be parsed as managed Markdown, was declined, or
    cannot be written. The caller holds the vault mutation boundary and has already checked its write admission.
    The page is committed before its index and graph refresh follows, the way a new Source's does.
    """
    from . import add, preserve
    from .vault import ContentHashMismatchError, PathGuardError

    path = vault_root / page
    try:
        text, guard = vault.read_guarded_text(vault_root, path)
        if callable(fields):
            fields = fields(vault.parse_frontmatter(text, strict=True)[0])
            if fields is None:
                return False
        updated = updated_text(text, fields, appended)
        intents: list[object] = []
        committed = preserve.commit_media_sidecar_writes(
            vault_root,
            (vault.PlannedWrite(path, updated, guard=guard, expected_hash=vault.content_hash(text)),),
            post_commit_fanout=False,
            publication_intents_out=intents,
        )
    except (FrontmatterSpliceError, vault.FrontmatterError, PathGuardError, ContentHashMismatchError, OSError):
        return False
    add.deferred_fanout(vault_root, list(committed), created_paths=[], publication_intents=intents)
    return True
