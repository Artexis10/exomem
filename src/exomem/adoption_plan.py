"""Collection adoption planner: file facts, frozen destinations, links, earlier imports.

Both the durable Adoption Studio run and the stateless ``adopt`` copy mode plan
through this module, so a note's links and a re-import are decided in one place.
Planning reads originals and the Sources front matter; it writes nothing.

A plan item is a JSON-able dict. Destinations are allocated first and never move,
because the links rendered into other Sources point at them. Links resolve in a
second pass over the whole item set.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    access,
    context_refs,
    find_results,
    markdown_regions,
    source_taxonomy,
)
from .vault import (
    kb_root,
    parse_frontmatter,
    slugify_with_truncation_check,
)

IMPORTED_FOLDER = source_taxonomy.IMPORTED_PATH_LABEL
# A text file past this size plans as an artifact: its bytes are kept, but the
# page never holds them in memory. A resource bound, not a vocabulary.
INLINE_TEXT_MAX_BYTES = 4 * 1024 * 1024
# Front matter of an earlier import is read from the head of the page only.
_INDEX_HEAD_BYTES = 32 * 1024
_MARKDOWN_SUFFIXES = (".md", ".markdown")
# A rule may name only a key the registry already holds: these are the closed
# `Resolution.status` values (see source_taxonomy) that mean it does.
ACCEPTED_STATUSES = frozenset({"builtin", "alias", "registered"})
_UNRESOLVED_NOT_FOUND = "not-found"
_UNRESOLVED_AMBIGUOUS = "ambiguous"


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _key(value: str) -> str:
    """Comparison form of a name: composed and case-folded, as filesystems differ."""
    return nfc(value).casefold()


def _strip_markdown_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in _MARKDOWN_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


# --------------------------------------------------------------------------- #
# earlier imports
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EarlierImport:
    """One imported Source read from its front matter."""

    path: str  # vault-relative page path
    imported_from: str | None
    import_collection: str | None
    collection_path: str | None
    original_sha256: str | None
    captured: str
    aliases: tuple[str, ...]


def _text_field(value: Any) -> str | None:
    return nfc(value) if isinstance(value, str) and value.strip() else None


def read_import_index(root: Path) -> list[EarlierImport]:
    """Every Source page whose parsed front matter names its original.

    The decision is the parsed ``imported_from`` field, never a substring of the
    page, so a page that merely quotes a path does not count as an import.
    """
    sources = kb_root(root) / "Sources"
    found: list[EarlierImport] = []
    if not sources.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(sources):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            page = Path(dirpath) / name
            try:
                with page.open("rb") as stream:
                    head = stream.read(_INDEX_HEAD_BYTES)
            except OSError:
                continue
            if not head.startswith(b"---"):
                continue
            frontmatter, _body, _raw = parse_frontmatter(head.decode("utf-8", errors="replace"))
            imported_from = _text_field(frontmatter.get("imported_from"))
            if imported_from is None:
                continue
            aliases = frontmatter.get("aliases")
            found.append(
                EarlierImport(
                    path=page.relative_to(root).as_posix(),
                    imported_from=imported_from,
                    import_collection=_text_field(frontmatter.get("import_collection")),
                    collection_path=_text_field(frontmatter.get("collection_path")),
                    original_sha256=_text_field(frontmatter.get("original_sha256")),
                    captured=str(frontmatter.get("captured") or ""),
                    aliases=tuple(
                        nfc(a) for a in aliases if isinstance(a, str) and a.strip()
                    ) if isinstance(aliases, list) else (),
                )
            )
    return found


def latest_earlier_import(
    index: Iterable[EarlierImport],
    *,
    collection: str | None,
    collection_path: str | None,
    original_path: str,
) -> EarlierImport | None:
    """The newest earlier Source for this original, or None.

    A Source carrying ``import_collection`` and ``collection_path`` matches on that
    pair. One that lacks both matches on ``imported_from``, so imports made before
    the pair existed still count.
    """
    matches = []
    for entry in index:
        if entry.import_collection is not None or entry.collection_path is not None:
            same = (
                collection is not None
                and entry.import_collection == nfc(collection)
                and entry.collection_path == collection_path
            )
        else:
            same = entry.imported_from == nfc(original_path)
        if same:
            matches.append(entry)
    if not matches:
        return None
    return max(matches, key=lambda entry: (entry.captured, entry.path))


# --------------------------------------------------------------------------- #
# destinations
# --------------------------------------------------------------------------- #
class Destinations:
    """Allocates page and artifact paths, unique on disk and within the plan."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._taken: dict[Path, set[str]] = {}

    def _names(self, folder: Path) -> set[str]:
        if folder not in self._taken:
            try:
                self._taken[folder] = {entry.name.casefold() for entry in folder.iterdir()}
            except OSError:
                self._taken[folder] = set()
        return self._taken[folder]

    def reserve(self, rel_path: str) -> None:
        path = self.root / rel_path
        self._names(path.parent).add(path.name.casefold())

    def allocate(
        self, folder: Path, stem: str, *, artifact_suffix: str | None
    ) -> tuple[str, str | None]:
        """A free page path, and the artifact path beside it for an artifact item."""
        taken = self._names(folder)
        attempt = 1
        while True:
            seed = stem if attempt == 1 else f"{stem}-{attempt}"
            page_name = f"{seed}{artifact_suffix or ''}.md"
            names = {page_name.casefold()}
            if artifact_suffix is not None:
                names.add(f"{seed}{artifact_suffix}".casefold())
            if not names & taken:
                taken |= names
                page = (folder / page_name).relative_to(self.root).as_posix()
                artifact = (
                    page[: -len(".md")] if artifact_suffix is not None else None
                )
                return page, artifact
            attempt += 1


def _folder_for(root: Path, classification: Mapping[str, Any] | None,
                taxonomy: source_taxonomy.SourceTaxonomy | None) -> Path | str:
    """Where this item's Source lives, or a refusal code when the registry no longer agrees."""
    if not classification:
        return kb_root(root) / "Sources" / IMPORTED_FOLDER
    assert taxonomy is not None
    try:
        kind = taxonomy.resolve_kind(classification["source_kind"])
        domain_key = classification.get("domain")
        domain = taxonomy.resolve_domain(domain_key) if domain_key else None
    except source_taxonomy.TaxonomyError:
        return "TAXONOMY_CHANGED"
    for resolution in (kind, domain):
        if resolution is not None and resolution.status not in ACCEPTED_STATUSES:
            return "TAXONOMY_CHANGED"
    return source_taxonomy.source_directory(root, kind, domain)


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #
def _tag_values(raw: Any) -> list[Any]:
    if isinstance(raw, str):
        return [part for part in raw.split(",")]
    return list(raw) if isinstance(raw, list) else []


def normalize_tags(raw: Any) -> tuple[list[str], list[str]]:
    """Front-matter tags as the vault writes them, and those it cannot carry.

    The spelling rule is capture's own (`add._clean_tags`). A tag is carried only
    when it is a string of word characters, `-` and `/`: the shape of a tag, so
    it survives the inline YAML list unquoted.
    """
    from .add import _clean_tags

    kept: list[str] = []
    rejected: list[str] = []
    for value in _tag_values(raw):
        if not isinstance(value, str):
            rejected.append(str(value))
            continue
        text = value.strip().removeprefix("#")
        cleaned = _clean_tags([text])
        if cleaned and all(ch.isalnum() or ch in "-/" for ch in cleaned[0]):
            if cleaned[0] not in kept:
                kept.append(cleaned[0])
        elif text:
            rejected.append(value.strip())
    return kept, rejected


def normalize_aliases(raw: Any) -> list[str]:
    values = _tag_values(raw) if isinstance(raw, str) else raw
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and "\n" not in value:
            alias = nfc(value.strip())
            if alias not in out:
                out.append(alias)
    return out


# --------------------------------------------------------------------------- #
# item facts
# --------------------------------------------------------------------------- #
def title_from_text(path: str, text: str) -> str:
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or Path(path).stem
    stem = Path(path).stem
    return stem.replace("-", " ").replace("_", " ").strip() or stem


def decode_text(data: bytes) -> str | None:
    """The text of `data`, or None when it is not valid UTF-8 text."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def link_references(body: str) -> list[dict[str, Any]]:
    """Wikilinks, embeds and local Markdown links of a body, in source order."""
    seen: set[tuple[str, str, bool]] = set()
    refs: list[dict[str, Any]] = []
    for wiki in find_results.wikilink_references(body):
        key = ("wiki", wiki.target, wiki.embed)
        if wiki.target and key not in seen:
            seen.add(key)
            refs.append({"raw": wiki.target, "embed": wiki.embed, "style": "wikilink"})
    for link in markdown_regions.local_links(body):
        key = ("md", link.target, link.image)
        if key not in seen:
            seen.add(key)
            refs.append({"raw": link.target, "embed": link.image, "style": "markdown"})
    return refs


def read_facts(
    root: Path, original_path: str, *, ceiling: int, text_hint: bool
) -> tuple[dict[str, Any], str | None] | dict[str, Any]:
    """Hash one original and decide how it imports.

    Returns ``(facts, text)`` (``text`` is None for an artifact) or a refusal dict
    ``{"code", "reason"}``. A text-like file that is not valid UTF-8, or is too big
    to hold, imports as an artifact so its bytes survive.
    """
    from .add import AddError, artifact_identity

    path = root / original_path
    try:
        size = path.stat().st_size
    except OSError:
        return {"code": "NOT_FOUND", "reason": "original missing at plan time"}
    if size > ceiling:
        return {"code": "TOO_LARGE", "reason": f"original exceeds the {ceiling:,} byte artifact ceiling"}
    text: str | None = None
    if text_hint and size <= INLINE_TEXT_MAX_BYTES:
        try:
            data = path.read_bytes()
        except OSError:
            return {"code": "NOT_FOUND", "reason": "original unreadable at plan time"}
        text = decode_text(data)
        digest, length = hashlib.sha256(data).hexdigest(), len(data)
    else:
        try:
            digest, length = artifact_identity(path, ceiling=ceiling)
        except AddError as error:
            return {"code": error.code, "reason": error.reason}
        except OSError:
            return {"code": "NOT_FOUND", "reason": "original unreadable at plan time"}
    facts: dict[str, Any] = {
        "original_path": original_path,
        "original_sha256": digest,
        "original_bytes": length,
        "form": "text" if text is not None else "artifact",
    }
    return facts, text


def text_facts(original_path: str, text: str) -> dict[str, Any]:
    """Title, tags, aliases and raw link references of a text original."""
    frontmatter, body, _raw = parse_frontmatter(text)
    tags, rejected = normalize_tags(frontmatter.get("tags"))
    return {
        "title": title_from_text(original_path, text),
        "tags": tags,
        "rejected_tags": rejected,
        "aliases": normalize_aliases(frontmatter.get("aliases")),
        "_refs": link_references(body),
    }


# --------------------------------------------------------------------------- #
# one item
# --------------------------------------------------------------------------- #
def plan_item(
    root: Path,
    original_path: str,
    *,
    collection: str | None,
    collection_root: str,
    classification: Mapping[str, Any] | None,
    taxonomy: source_taxonomy.SourceTaxonomy | None,
    text_hint: bool,
    date_iso: str,
    destinations: Destinations,
    index: Iterable[EarlierImport],
    ceiling: int,
) -> tuple[dict[str, Any], str | None] | dict[str, Any]:
    """Facts, identity and a frozen destination for one original; or a refusal dict."""
    read = read_facts(root, original_path, ceiling=ceiling, text_hint=text_hint)
    if isinstance(read, dict):
        return {"path": original_path, **read}
    facts, text = read
    prefix = f"{collection_root}/" if collection_root else ""
    relative = original_path[len(prefix) :] if prefix and original_path.startswith(prefix) else original_path
    item: dict[str, Any] = {
        **facts,
        "collection_path": nfc(relative),
        "action": "copy-as-source",
        "classification": dict(classification) if classification else None,
    }
    if text is not None:
        item.update(text_facts(original_path, text))
    else:
        item.update(
            title=Path(original_path).stem or original_path,
            tags=[], rejected_tags=[], aliases=[], _refs=[],
        )

    earlier = latest_earlier_import(
        index, collection=collection, collection_path=item["collection_path"],
        original_path=original_path,
    )
    if earlier is not None and earlier.original_sha256 == item["original_sha256"]:
        item["action"] = "already-imported"
        item["target_path"] = earlier.path
        item["target_ref"] = context_refs.source_ref(earlier.path)
        item["existing_ref"] = item["target_ref"]
        item["artifact_path"] = artifact_of(root, earlier.path)
        return item, text
    if earlier is not None:
        item["previous_import"] = context_refs.source_ref(earlier.path)

    folder = _folder_for(root, classification, taxonomy)
    if isinstance(folder, str):
        return {"path": original_path, "code": folder,
                "reason": "the Source taxonomy no longer accepts this rule's kind or domain"}
    slug, slug_warning = slugify_with_truncation_check(item["title"])
    suffix = None
    if item["form"] == "artifact":
        from .preserve import _sanitize_filename

        suffix = Path(_sanitize_filename(Path(original_path).name) or "").suffix
        if suffix == ".md":
            # An artifact is a `.md` file only when its bytes are not UTF-8 text, and the
            # graph reads every `.md` under the knowledge base as strict UTF-8.
            # Storing the bytes under `.bin` keeps them out of the graph input set.
            suffix = ".bin"
    page, artifact = destinations.allocate(folder, f"{date_iso}-{slug}", artifact_suffix=suffix)
    reason = access.writable_reason(root, page)
    if reason is not None:
        return {"path": original_path, "code": "WRITE_REFUSED", "reason": reason}
    item["target_path"] = page
    item["target_ref"] = context_refs.source_ref(page)
    item["artifact_path"] = artifact
    if slug_warning:
        item["slug_warning"] = slug_warning
    return item, text


def artifact_of(root: Path, page_path: str) -> str | None:
    """The artifact beside an earlier Source page, when there is one."""
    candidate = page_path[: -len(".md")]
    return candidate if candidate != page_path and (root / candidate).is_file() else None


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
class LinkResolver:
    """Resolves a link target to a Source page of the collection.

    Order: the exact collection path, the path relative to the note, a unique
    basename for a bare name, then a unique front-matter alias. A step that finds
    more than one candidate leaves the link unresolved; nothing is guessed.
    """

    def __init__(self, entries: Iterable[Mapping[str, Any]]) -> None:
        self._by_path: dict[str, set[str]] = {}
        self._by_name: dict[str, set[str]] = {}
        self._by_alias: dict[str, set[str]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        for entry in entries:
            cp = entry["collection_path"]
            self.pages[cp] = dict(entry)
            name = posixpath.basename(cp)
            for variant in {cp, _strip_markdown_suffix(cp)}:
                self._by_path.setdefault(_key(variant), set()).add(cp)
            for variant in {name, _strip_markdown_suffix(name)}:
                self._by_name.setdefault(_key(variant), set()).add(cp)
            for alias in entry.get("aliases") or ():
                self._by_alias.setdefault(_key(alias), set()).add(cp)

    def resolve(self, raw: str, note_path: str) -> tuple[str | None, str, str | None]:
        """``(collection_path, via, reason)``: a target, or None with why."""
        target = nfc(raw.strip()).replace("\\", "/")
        if not target:
            return None, "", _UNRESOLVED_NOT_FOUND
        target = target.lstrip("/")
        steps: list[tuple[str, set[str]]] = [
            ("path", self._by_path.get(_key(posixpath.normpath(target)), set())),
        ]
        relative = posixpath.normpath(posixpath.join(posixpath.dirname(note_path), target))
        if not relative.startswith(".."):
            steps.append(("note-relative", self._by_path.get(_key(relative), set())))
        if "/" not in target:
            steps.append(("basename", self._by_name.get(_key(target), set())))
        steps.append(("alias", self._by_alias.get(_key(target), set())))
        for via, candidates in steps:
            if len(candidates) == 1:
                return next(iter(candidates)), via, None
            if len(candidates) > 1:
                return None, via, _UNRESOLVED_AMBIGUOUS
        return None, "", _UNRESOLVED_NOT_FOUND


def resolve_links(
    items: list[dict[str, Any]],
    index: Iterable[EarlierImport],
    *,
    collection: str | None,
) -> None:
    """Fill every text item's ``links`` and drop its raw references.

    The namespace is the planned items plus the collection's earlier imports that
    this plan does not replace, each at its frozen or existing Source page.
    """
    entries: dict[str, dict[str, Any]] = {}
    if collection is not None:
        for entry in sorted(index, key=lambda e: (e.captured, e.path)):
            if entry.import_collection == nfc(collection) and entry.collection_path:
                entries[entry.collection_path] = {
                    "collection_path": entry.collection_path,
                    "target_path": entry.path,
                    "aliases": entry.aliases,
                }
    for item in items:
        entries[item["collection_path"]] = {
            "collection_path": item["collection_path"],
            "target_path": item["target_path"],
            "aliases": item.get("aliases") or (),
        }
    resolver = LinkResolver(entries.values())
    for item in items:
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for ref in item.pop("_refs", []):
            cp, via, reason = resolver.resolve(ref["raw"], item["collection_path"])
            if cp is None:
                unresolved.append({"raw": ref["raw"], "embed": ref["embed"], "reason": reason})
            elif cp != item["collection_path"]:
                resolved.append(
                    {
                        "raw": ref["raw"], "embed": ref["embed"], "via": via,
                        "target_path": resolver.pages[cp]["target_path"],
                    }
                )
        item["links"] = {"resolved": resolved, "unresolved": unresolved}


# --------------------------------------------------------------------------- #
# provenance and identity of a whole plan
# --------------------------------------------------------------------------- #
def item_frontmatter(item: Mapping[str, Any], collection: str | None) -> dict[str, Any]:
    """The front-matter fields the Source will carry, as previewed in the plan."""
    classification = item.get("classification") or {}
    frontmatter: dict[str, Any] = {
        "type": "source",
        "source_type": classification.get("source_kind") or source_taxonomy.UNCLASSIFIED_KIND,
    }
    if classification.get("domain"):
        frontmatter["domain"] = classification["domain"]
    frontmatter["imported_from"] = item["original_path"]
    if collection is not None:
        frontmatter["import_collection"] = nfc(collection)
        frontmatter["collection_path"] = item["collection_path"]
    frontmatter["original_sha256"] = item["original_sha256"]
    frontmatter["original_bytes"] = item["original_bytes"]
    if item.get("previous_import"):
        frontmatter["previous_import"] = item["previous_import"]
    if item.get("aliases"):
        frontmatter["aliases"] = list(item["aliases"])
    frontmatter["tags"] = ["imported", *item.get("tags", [])]
    return frontmatter


def item_digest(item: Mapping[str, Any]) -> str:
    """Hash of one complete planned action (destination, classification, provenance, links)."""
    payload = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def plan_total_bytes(items: Iterable[Mapping[str, Any]]) -> int:
    """Bytes `apply` will write for the originals: existing Sources write none."""
    return sum(
        int(item.get("original_bytes") or 0)
        for item in items
        if item.get("action") != "already-imported"
    )


def wikilink_for(page_path: str) -> str:
    """The wikilink target of a Source page: its vault path without the `.md` suffix."""
    return page_path[: -len(".md")] if page_path.lower().endswith(".md") else page_path


__all__ = [
    "ACCEPTED_STATUSES",
    "IMPORTED_FOLDER",
    "INLINE_TEXT_MAX_BYTES",
    "Destinations",
    "EarlierImport",
    "LinkResolver",
    "item_digest",
    "artifact_of",
    "decode_text",
    "item_frontmatter",
    "latest_earlier_import",
    "normalize_aliases",
    "normalize_tags",
    "plan_item",
    "plan_total_bytes",
    "read_import_index",
    "resolve_links",
    "title_from_text",
    "wikilink_for",
]
