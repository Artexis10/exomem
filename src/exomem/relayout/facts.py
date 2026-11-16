"""The facts of a Markdown page that the plan needs from its first read: what the page says about itself.

The first pass reads each page once and keeps these facts, never its body, so the second pass
can stream the bodies again for their links. Both modes parse the page's bytes with this
function, so a stored page and a projected file give the same facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .. import fold_leaf, memory_refs, originals, vault
from ..find_types import ParsedPage
from ..governance import membership

#: Frontmatter field names that the page schema fixes for pointers to a file and for back references.
PARENT_MEDIA = "parent_media"
NEXT_PART = "next_part"
PART_OF = "part_of"
#: The pointers whose value is a path from the page's own folder, not from the vault root.
PAGE_RELATIVE_POINTERS = (NEXT_PART, PART_OF)
INGESTED_INTO = "ingested_into"
#: The version 1 container binding fixes this field and its three vault-relative path keys.
CONTAINER_MEMBER = "container_member"
# nosemgrep: ep-word-set -- closed binding-schema keys of document_containers, not vocabulary.
CONTAINER_PATH_KEYS = ("parent_path", "parent_page", "artifact_path")
#: The pointer fields by which a page names a binary and gives it semantic scopes: a companion
#: descriptor's artifact, and the data file of a dataset card (the companion registry binds both).
# nosemgrep: ep-word-set -- closed pointer-field names of the companion registry, not vocabulary.
COMPANION_FIELDS = (f"{originals.GOVERNANCE_COMPANION}.{originals.NESTED_PATH_KEY}", "data_file")


@dataclass(frozen=True, slots=True)
class PageFact:
    """A single Markdown page, as its own bytes describe it.

    `type_value` and `title` are the frontmatter values as authored, or None. `display_title`
    is the title that link resolution uses, and None when the page is not UTF-8 text.
    `pointers` lists `(field, value)` for each field that names a file by path:
    `originals` scalar and nested pointers, a container member's binding paths and `parent_media`
    hold a vault-relative path, and `next_part` and `part_of` hold a path relative to the page.
    `declares_part_of`, `parent_media` and `container_member` are the three facts that the
    membership kernel reads to find where a page takes scopes from: whether the frontmatter has a
    `part_of` key, the `parent_media` value as `ParsedPage` reads it, and whether it has a
    `container_member` key. `ingested` holds the wikilink entries
    of `ingested_into`. `exomem_id` is the page's normalized identity, and `subject` is the
    selector input that governance membership reads (projects, tags, type, classes, identity),
    built by the membership kernel itself; both are None for a file that is not UTF-8 text.
    """

    path: str
    type_value: object = None
    title: object = None
    display_title: str | None = None
    pointers: tuple[tuple[str, str], ...] = ()
    ingested: tuple[str, ...] = ()
    exomem_id: str | None = None
    subject: membership.MarkdownSubject | None = None
    declares_part_of: bool = False
    parent_media: str | None = None
    container_member: bool = False


def _frontmatter_valid(frontmatter: dict, block: str | None) -> bool:
    """Whether the page's frontmatter block parsed as a mapping, as the page parser of search judges it.

    An empty mapping from a non-empty block is a block of comments only, or a block that did not
    parse; the block is read again to tell them apart, which costs nothing for a page with fields.
    """
    if frontmatter or block is None or not block.strip():
        return True
    try:
        return isinstance(vault.yaml_safe_load(block) or {}, dict)
    except yaml.YAMLError:
        return False


def read_fact(path: str, data: bytes | None) -> PageFact:
    """The facts of the page at `path`, from its bytes; empty facts when `data` is None or not UTF-8."""
    if data is None:
        return PageFact(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return PageFact(path)
    # A text-mode read translates newlines before it parses; the bytes path parses the same text.
    frontmatter, body, block = vault.parse_frontmatter(
        text.replace("\r\n", "\n").replace("\r", "\n")
    )
    pointers: list[tuple[str, str]] = []
    for key in originals.SCALAR_POINTERS:
        value = frontmatter.get(key)
        if isinstance(value, str) and value:
            pointers.append((key, value))
    for key in originals.NESTED_POINTERS:
        descriptor = frontmatter.get(key)
        value = descriptor.get(originals.NESTED_PATH_KEY) if isinstance(descriptor, dict) else None
        if isinstance(value, str) and value:
            pointers.append((f"{key}.{originals.NESTED_PATH_KEY}", value))
    binding = frontmatter.get(CONTAINER_MEMBER)
    if isinstance(binding, dict):
        for key in CONTAINER_PATH_KEYS:
            value = binding.get(key)
            if isinstance(value, str) and value:
                pointers.append((f"{CONTAINER_MEMBER}.{key}", value))
    for key in (PARENT_MEDIA, *PAGE_RELATIVE_POINTERS):
        value = frontmatter.get(key)
        if isinstance(value, str) and value:
            pointers.append((key, value))
    parsed = ParsedPage(
        path=Path(path),
        rel_path=path,
        frontmatter=frontmatter,
        body="",
        title="",
        mtime=0,
        frontmatter_valid=_frontmatter_valid(frontmatter, block),
    )
    subject = membership.markdown_subject(parsed)
    return PageFact(
        path,
        frontmatter.get("type"),
        frontmatter.get("title"),
        vault.resolve_display_title(frontmatter, body, path) or None,
        tuple(pointers),
        tuple(fold_leaf.ingested_entries(frontmatter.get(INGESTED_INTO))),
        memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD)),
        subject,
        PART_OF in frontmatter,
        parsed.parent_media,
        CONTAINER_MEMBER in frontmatter,
    )
