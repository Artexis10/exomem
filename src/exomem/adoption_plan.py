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
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    access,
    context_refs,
    find_results,
    held_fs,
    import_stage,
    markdown_regions,
    note_types,
    reserved_paths,
    source_taxonomy,
)
from .vault import (
    kb_root,
    parse_frontmatter,
    slugify_with_truncation_check,
)

IMPORTED_FOLDER = source_taxonomy.IMPORTED_PATH_LABEL
# The code of a file above `client_artifacts.adoption_ceiling()`, on an inventory row, a skipped item and the residual.
OVER_CEILING = "OVER_ADOPTION_CEILING"
# A text file past this size plans as an artifact: its bytes are kept, but the
# page never holds them in memory. A resource bound, not a vocabulary.
INLINE_TEXT_MAX_BYTES = 4 * 1024 * 1024
# A page's front matter is read in blocks of this size until its closing fence.
_INDEX_BLOCK_BYTES = 32 * 1024
_MARKDOWN_SUFFIXES = (".md", ".markdown")
# A rule may name only a key the registry already holds: these are the closed
# `Resolution.status` values (see source_taxonomy) that mean it does.
ACCEPTED_STATUSES = frozenset({"builtin", "alias", "registered"})  # nosemgrep: ep-word-set -- `Resolution.status` values that source_taxonomy._resolve assigns.
_UNRESOLVED_NOT_FOUND = "not-found"
_UNRESOLVED_AMBIGUOUS = "ambiguous"
# The one file of that name is in the scan but no import holds it yet: it belongs to a later page.
_UNRESOLVED_LATER_PAGE = "later-page"


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
    # `imported_from` as written. Two originals whose names differ only in Unicode
    # normalisation share every normalised field; only this spelling tells them apart.
    spelled_from: str | None = None


def _text_field(value: Any) -> str | None:
    return nfc(value) if isinstance(value, str) and value.strip() else None


def _leading_frontmatter(root: Path, page: Path) -> dict[str, Any] | None:
    """The parsed front matter of a page, however long, or None when it has none.

    The page is read block by block until the closing fence, so a long title or a
    long alias list cannot hide the provenance that follows it.
    """
    with reserved_paths.open_generic_binary(
        root, page.relative_to(root).as_posix(), physical=True
    ) as source:
        stream = source.stream
        data = stream.read(_INDEX_BLOCK_BYTES)
        if not data.startswith(b"---"):
            return None
        while True:
            frontmatter, _body, raw = parse_frontmatter(data.decode("utf-8", errors="replace"))
            if raw is not None:
                return frontmatter
            more = stream.read(_INDEX_BLOCK_BYTES)
            if not more:
                return None
            data += more


def earlier_import_of(
    planned: Mapping[str, Any], *, collection: str | None, captured: str
) -> EarlierImport:
    """The entry the index holds for a Source just written for ``planned``.

    It is what `read_import_index` would read back from that page, so a caller
    that keeps the index in memory across several commits stays current without
    walking the Sources tree again.
    """
    return EarlierImport(
        path=planned["target_path"],
        imported_from=nfc(planned["original_path"]),
        import_collection=nfc(collection) if collection is not None else None,
        collection_path=planned["collection_path"] if collection is not None else None,
        original_sha256=planned["original_sha256"],
        captured=captured,
        aliases=tuple(planned.get("aliases") or ()),
        spelled_from=planned["original_path"],
    )


def _source_pages(root: Path) -> list[Path]:
    """Every Markdown page under Sources, in the order `read_import_index` reads them."""
    sources = kb_root(root) / "Sources"
    pages: list[Path] = []
    if not sources.is_dir():
        return pages
    for dirpath, dirnames, filenames in os.walk(sources):
        dirnames.sort()
        pages.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.lower().endswith(".md")
        )
    return pages


def source_page_paths(root: Path) -> list[str]:
    """The vault-relative path of every Markdown page under Sources that can hold an import.

    Names only, no page is opened. The Sources index is left out: every filing rewrites it and it
    holds no import, so it must not move `sources_seal`.
    """
    sources_index = kb_root(root) / "Sources" / "index.md"
    return [
        page.relative_to(root).as_posix() for page in _source_pages(root) if page != sources_index
    ]


def sources_seal(paths: Iterable[str]) -> str:
    """An order-free digest of a set of Source page paths.

    Two listings with one seal hold the same pages.
    """
    return hashlib.sha256("\n".join(sorted(set(paths))).encode("utf-8")).hexdigest()


def read_import_entry(root: Path, page: Path) -> EarlierImport | None:
    """The entry a Source page's parsed front matter gives, or None when it names no original."""
    try:
        frontmatter = _leading_frontmatter(root, page)
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return None
    if frontmatter is None:
        return None
    spelled_from = frontmatter.get("imported_from")
    imported_from = _text_field(spelled_from)
    if imported_from is None:
        return None
    aliases = frontmatter.get("aliases")
    return EarlierImport(
        path=page.relative_to(root).as_posix(),
        imported_from=imported_from,
        import_collection=_text_field(frontmatter.get("import_collection")),
        collection_path=_text_field(frontmatter.get("collection_path")),
        original_sha256=_text_field(frontmatter.get("original_sha256")),
        captured=str(frontmatter.get("captured") or ""),
        aliases=tuple(
            nfc(a) for a in aliases if isinstance(a, str) and a.strip()
        ) if isinstance(aliases, list) else (),
        spelled_from=spelled_from,
    )


def read_import_index(root: Path) -> list[EarlierImport]:
    """Every Source page whose parsed front matter names its original.

    The decision is the parsed ``imported_from`` field, never a substring of the
    page, so a page that merely quotes a path does not count as an import.
    """
    entries = (read_import_entry(root, page) for page in _source_pages(root))
    return [entry for entry in entries if entry is not None]


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
    # A twin original, whose name differs only in Unicode normalisation, matches the
    # same pages. The pages spelled as this original is spelled are its own; a
    # collection staged again under another normalisation spells none of them so.
    spelled = [entry for entry in matches if entry.spelled_from == original_path]
    return max(spelled or matches, key=lambda entry: (entry.captured, entry.path))


def earlier_with_same_bytes(
    index: Iterable[EarlierImport],
    *,
    collection: str | None,
    collection_path: str,
    original_path: str,
    sha256: str,
) -> EarlierImport | None:
    """The earlier Source that already holds these exact bytes of this original, or None.

    The newest import under this collection label decides. When the label has none,
    an import of the same original path under another label counts: a subfolder
    imported first, then its parent.
    """
    entries = list(index)
    latest = latest_earlier_import(
        entries, collection=collection, collection_path=collection_path,
        original_path=original_path,
    )
    if latest is not None:
        return latest if latest.original_sha256 == sha256 else None
    twins = [
        entry for entry in entries
        if entry.imported_from == nfc(original_path) and entry.original_sha256 == sha256
    ]
    return max(twins, key=lambda entry: (entry.captured, entry.path)) if twins else None


def root_warnings(
    index: Iterable[EarlierImport], *, collection: str | None, collection_root: str
) -> list[dict[str, Any]]:
    """A warning naming each other root that earlier imports of this collection came from.

    Re-import matches by collection path, so a collection staged somewhere new still
    finds its earlier Sources. The warning only tells the reader that it did.
    """
    if collection is None:
        return []
    own = nfc(collection_root).rstrip("/")
    roots: set[str] = set()
    for entry in index:
        if entry.import_collection != nfc(collection) or not (
            entry.imported_from and entry.collection_path
        ):
            continue
        head = entry.imported_from[: len(entry.imported_from) - len(entry.collection_path)]
        if not entry.imported_from.endswith(entry.collection_path) or (
            head and not head.endswith("/")
        ):
            continue
        earlier = head.rstrip("/")
        if earlier != own:
            roots.add(earlier)
    if not roots:
        return []
    named = sorted(roots)
    return [
        {
            "code": "EARLIER_IMPORT_FROM_OTHER_ROOT",
            "reason": (
                f"earlier imports of collection {nfc(collection)!r} came from "
                + ", ".join(repr(root) for root in named)
                + f", not {own!r}; files match by collection path"
            ),
            "roots": named,
        }
    ]


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

    def taken(self, rel_path: str) -> bool:
        path = self.root / rel_path
        return path.name.casefold() in self._names(path.parent)

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
    # A note rule alone leaves the Source copy unclassified: it files under Imported.
    if not classification or not classification.get("source_kind"):
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
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part for part in raw.split(",")]
    return list(raw) if isinstance(raw, list) else [raw]


def normalize_tags(raw: Any) -> tuple[list[str], list[str]]:
    """Front-matter tags as the vault writes them, and those it cannot carry.

    The spelling rule is capture's own (`add._clean_tags`). A tag is carried only
    when the vault's tag grammar (`semantic_units._is_valid_tag`) accepts it, so it
    survives the inline YAML list unquoted and the vault reads it back.
    """
    from .add import _clean_tags
    from .semantic_units import _is_valid_tag

    kept: list[str] = []
    rejected: list[str] = []
    for value in _tag_values(raw):
        if not isinstance(value, str):
            rejected.append(str(value))
            continue
        text = value.strip().removeprefix("#")
        cleaned = _clean_tags([text])
        if cleaned and _is_valid_tag(cleaned[0]):
            if cleaned[0] not in kept:
                kept.append(cleaned[0])
        elif text:
            rejected.append(value.strip())
    return kept, rejected


def normalize_aliases(raw: Any) -> tuple[list[str], list[str]]:
    """Front-matter aliases as the page carries them, and those it cannot carry.

    A non-string or multi-line alias cannot sit in the inline YAML list, so it is
    reported instead of dropped. An empty alias carries nothing and is not reported.
    """
    values = _tag_values(raw)
    kept: list[str] = []
    rejected: list[str] = []
    for value in values:
        if not isinstance(value, str):
            rejected.append(str(value))
        elif "\n" in value:
            rejected.append(value.strip())
        elif value.strip():
            alias = nfc(value.strip())
            if alias not in kept:
                kept.append(alias)
    return kept, rejected


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
    seen: set[tuple[str, str, bool, str | None]] = set()
    refs: list[dict[str, Any]] = []
    for wiki in find_results.wikilink_references(body):
        key = ("wiki", wiki.target, wiki.embed, wiki.fragment)
        if wiki.target and key not in seen:
            seen.add(key)
            refs.append(
                {"raw": wiki.target, "embed": wiki.embed, "style": "wikilink",
                 "fragment": wiki.fragment}
            )
    for link in markdown_regions.local_links(body):
        key = ("md", link.target, link.image, link.fragment)
        if key not in seen:
            seen.add(key)
            refs.append(
                {"raw": link.target, "embed": link.image, "style": "markdown",
                 "fragment": link.fragment}
            )
    return refs


def source_failure(error: Exception) -> dict[str, str]:
    """Map the held-filesystem error protocol without exposing host paths."""
    code = getattr(error, "code", "IO_REFUSED")
    if isinstance(error, FileNotFoundError):
        code = "MISSING"
    # These are the held substrate's closed refusal codes, not a content vocabulary.
    mapped = {
        "MISSING": ("NOT_FOUND", "the original is missing"),
        "UNSAFE_PATH": ("INVALID_PATH", "the original has an unsafe path"),
        "CROSS_DEVICE": ("INVALID_PATH", "the original crosses a filesystem boundary"),
        "RESERVED_PATH": ("RESERVED_PATH", "the original is reserved or has multiple names"),
        "IDENTITY_CHANGED": ("SOURCE_CHANGED", "the original changed during import"),
        "PATH_GUARD_CONTENT": ("SOURCE_CHANGED", "the original bytes changed before publication"),
    }.get(code, (code, "the original could not be read safely"))
    return {"code": mapped[0], "reason": mapped[1]}


def read_facts(
    root: Path, original_path: str, *, ceiling: int, text_hint: bool, hash_artifact: bool = True,
    vouched: tuple[import_stage.Identity, str] | None = None,
) -> tuple[dict[str, Any], str | None] | dict[str, Any]:
    """Hash one original and decide how it imports.

    Returns ``(facts, text)`` (``text`` is None for an artifact) or a refusal dict
    ``{"code", "reason"}``. A text-like file that is not valid UTF-8, or is too big
    to hold, imports as an artifact so its bytes survive. With ``hash_artifact`` false an
    artifact is only sized and its ``original_sha256`` is None, for a caller whose copy hashes it.
    An artifact whose identity equals ``vouched`` (a stage manifest's identity and SHA-256) takes that
    SHA-256, is not bound by the ceiling and is not read: adoption files it by rename, not by copy.
    """
    from .add import AddError, artifact_identity

    text: str | None = None
    try:
        from .media_store import open_original

        with open_original(root, original_path, physical=True) as source:
            size = source.version.size
            reads_as_text = text_hint and size <= INLINE_TEXT_MAX_BYTES
            stands_for_bytes = (
                vouched is not None and not reads_as_text and import_stage.Identity.of_version(source.version) == vouched[0]
            )
            if size > ceiling and not stands_for_bytes:
                return {"code": OVER_CEILING, "reason": f"original exceeds the {ceiling:,} byte adoption ceiling"}
            if reads_as_text:
                data = source.stream.read(INLINE_TEXT_MAX_BYTES + 1)
                if len(data) != size:
                    return {"code": "SOURCE_CHANGED", "reason": "the original changed during import"}
                text = decode_text(data)
                digest, length = hashlib.sha256(data).hexdigest(), len(data)
            elif stands_for_bytes:
                digest, length = vouched[1], size
            elif hash_artifact:
                digest, length = artifact_identity(source.stream, ceiling=ceiling)
            else:
                digest, length = None, size
    except AddError as error:
        return {"code": error.code, "reason": error.reason}
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
        return source_failure(error)
    facts: dict[str, Any] = {
        "original_path": original_path,
        "original_sha256": digest,
        "original_bytes": length,
        "form": "text" if text is not None else "artifact",
    }
    return facts, text


def text_facts(original_path: str, text: str) -> dict[str, Any]:
    """Title, tags, aliases and raw link references of a text original.

    The tags are the front matter's and then the body's inline `#tags`, normalized together.
    """
    frontmatter, body, _raw = parse_frontmatter(text)
    tags, rejected_tags = normalize_tags(
        [*_tag_values(frontmatter.get("tags")), *markdown_regions.inline_tags(body)]
    )
    aliases, rejected_aliases = normalize_aliases(frontmatter.get("aliases"))
    return {
        "title": title_from_text(original_path, text),
        "tags": tags,
        "rejected_tags": rejected_tags,
        "aliases": aliases,
        "rejected_aliases": rejected_aliases,
        "_refs": link_references(body),
    }


# --------------------------------------------------------------------------- #
# own notes
# --------------------------------------------------------------------------- #
def _is_note_of(
    root: Path, rel_path: str, *, collection: str, collection_path: str
) -> bool:
    """Whether `rel_path` is a compiled page that carries this collection path's identity."""
    try:
        frontmatter = _leading_frontmatter(root, root / rel_path)
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return False
    if frontmatter is None:
        return False
    return (
        note_types.Basis(root).selects(frontmatter.get("type"), note_types.compiled)
        and _text_field(frontmatter.get("import_collection")) == nfc(collection)
        and _text_field(frontmatter.get("collection_path")) == collection_path
    )


@dataclass(frozen=True)
class _NotePage:
    """What one non-Source page told the note index, and the file state it was read at.

    `signature` is None when the page must be read again on the next refresh: it was
    unreadable, or it changed too recently for its timestamps to prove a later change.
    `identity` is `(import_collection, collection_path, type)`, or None for a page that
    carries no import identity.
    """

    signature: tuple[int, int, int, int] | None
    identity: tuple[str, str, Any] | None


# A page whose timestamps are this close to the refresh may change again within the same
# timestamp tick, which a later stat cannot see; FAT keeps timestamps at 2 s granularity.
_RACY_NS = 2_000_000_000
# The knowledge bases whose note pages this process remembers. A process serves one vault;
# a test process makes many, so the oldest is dropped.
_KNOWN_VAULTS = 4
_NOTE_PAGES: OrderedDict[str, dict[str, _NotePage]] = OrderedDict()
_NOTE_PAGES_LOCK = threading.Lock()


def _known_note_pages(root: Path) -> dict[str, _NotePage]:
    """The note pages this process last read under `root`, as of the last refresh."""
    with _NOTE_PAGES_LOCK:
        return _NOTE_PAGES.get(os.path.realpath(root), {})


def _remember_note_pages(root: Path, pages: dict[str, _NotePage]) -> None:
    key = os.path.realpath(root)
    with _NOTE_PAGES_LOCK:
        _NOTE_PAGES[key] = pages
        _NOTE_PAGES.move_to_end(key)
        while len(_NOTE_PAGES) > _KNOWN_VAULTS:
            _NOTE_PAGES.popitem(last=False)


def _page_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)


class NoteIndex:
    """The notes that earlier imports made, by import identity, as of this call's first lookup.

    A note carries the `import_collection` and `collection_path` of its original wherever
    the user has since moved it, so the index finds a moved note. The knowledge base is
    listed on the first lookup: a call without an own-note item never reads the notes.
    The process remembers what each page said with the page's file signature, so a later
    call reads again only the pages that were added or changed since, by any writer, and
    forgets the pages that are gone. A page is read again when its mtime, ctime, size or
    file id moved. On POSIX every write moves ctime. On native Windows `st_ctime` is the
    creation time, so the guarantee there is "mtime, size or file id moved": an in-place
    edit that keeps the size and restores the exact mtime is served as it was.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._paths: dict[tuple[str, str], str] | None = None

    def _refresh(self) -> dict[str, _NotePage]:
        """Every non-Source page and what it says, reading only pages whose signature moved."""
        from .find_corpus import walk_md

        kb = kb_root(self._root)
        if not kb.is_dir():
            return {}
        known = _known_note_pages(self._root)
        settled_before = time.time_ns() - _RACY_NS
        # A compiled page cannot be in Sources/: the mover refuses moves into it, and
        # `read_import_index` already reads its pages for their own identities.
        sources = kb / "Sources"
        pages: dict[str, _NotePage] = {}
        # Sorted, so the first of two pages that carry one identity is always the same page.
        for page in sorted(walk_md(kb)):
            if page.is_relative_to(sources):
                continue
            rel = page.relative_to(self._root).as_posix()
            try:
                info = page.lstat()
            except OSError:
                continue
            signature = _page_signature(info)
            seen = known.get(rel)
            if seen is not None and seen.signature == signature:
                pages[rel] = seen
                continue
            # The signature is taken before the read, so a change during the read shows next time.
            settled = max(info.st_mtime_ns, info.st_ctime_ns) < settled_before
            try:
                frontmatter = _leading_frontmatter(self._root, page)
            except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
                pages[rel] = _NotePage(None, None)
                continue
            identity = None
            if frontmatter is not None:
                collection = _text_field(frontmatter.get("import_collection"))
                collection_path = _text_field(frontmatter.get("collection_path"))
                if collection is not None and collection_path is not None:
                    identity = (collection, collection_path, frontmatter.get("type"))
            pages[rel] = _NotePage(signature if settled else None, identity)
        _remember_note_pages(self._root, pages)
        return pages

    def _read(self) -> dict[tuple[str, str], str]:
        found: dict[tuple[str, str], str] = {}
        basis = note_types.Basis(self._root)
        for rel, page in self._refresh().items():
            # The type is judged on every call, because the vault's registry can change.
            if page.identity is not None and basis.selects(page.identity[2], note_types.compiled):
                found.setdefault(page.identity[:2], rel)
        return found

    def find(self, *, collection: str, collection_path: str) -> str | None:
        if self._paths is None:
            self._paths = self._read()
        return self._paths.get((nfc(collection), collection_path))


def find_note(
    root: Path,
    notes: NoteIndex,
    *,
    collection: str,
    collection_path: str,
    frozen: str | None = None,
) -> str | None:
    """The note an earlier run created for this collection path, or None.

    The frozen target is checked first: it holds a note committed after `notes` was
    read, such as one whose commit raised. Otherwise the note is found by the import
    identity it carries, wherever it now is.
    """
    if frozen and _is_note_of(root, frozen, collection=collection, collection_path=collection_path):
        return frozen
    return notes.find(collection=collection, collection_path=collection_path)


def is_markdown(original_path: str) -> bool:
    return original_path.lower().endswith(_MARKDOWN_SUFFIXES)


def note_title(text: str, fallback: str) -> str:
    """The note's title: the original's front-matter `title`, else the title its Source has."""
    title = parse_frontmatter(text)[0].get("title")
    if isinstance(title, str) and title.strip() and "\n" not in title:
        return nfc(title.strip())
    return fallback


def plan_note(
    root: Path,
    item: Mapping[str, Any],
    text: str,
    rule: Mapping[str, Any],
    *,
    destinations: Destinations,
    date_iso: str,
) -> dict[str, Any] | dict[str, str]:
    """The frozen note target and inputs of one own-note item, or a refusal `{code, reason}`.

    The leaf names the folder and file under the plan's day; this only keeps two notes
    of one plan apart.
    """
    from .note import NoteError, planned_destination
    from .vault import InvalidSlugError

    title = note_title(text, item["title"])
    note: dict[str, Any] = {
        "note_type": rule["note_type"],
        "title": title,
        "status": "draft",
    }
    for field in ("project", "domain"):
        if rule.get(field):
            note[field] = rule[field]
    slug: str | None = None
    attempt = 1
    while True:
        try:
            target = planned_destination(
                root, note_type=rule["note_type"], title=title, slug=slug,
                project=rule.get("project"), domain=rule.get("domain"), date_iso=date_iso,
            )
        except (NoteError, InvalidSlugError, ValueError) as error:
            return {"code": getattr(error, "code", "WRITE_REFUSED"),
                    "reason": f"the note target is unavailable: {error}"}
        if not destinations.taken(target):
            break
        attempt += 1
        slug = slugify_with_truncation_check(f"{title} {attempt}")[0]
    reason = access.writable_reason(root, target)
    if reason is not None:
        return {"code": "WRITE_REFUSED", "reason": reason}
    destinations.reserve(target)
    note["target_path"] = target
    if slug is not None:
        note["slug"] = slug
    return note


def note_extra_frontmatter(item: Mapping[str, Any], collection: str | None) -> dict[str, Any]:
    """The fields the note carries beyond what the note leaf renders: aliases and import identity."""
    extra: dict[str, Any] = {}
    if item.get("aliases"):
        extra["aliases"] = list(item["aliases"])
    if collection is not None:
        extra["import_collection"] = nfc(collection)
        extra["collection_path"] = item["collection_path"]
    return extra


def note_frontmatter(item: Mapping[str, Any], collection: str | None) -> dict[str, Any]:
    """The front matter the note will carry, as previewed in the plan."""
    note = item["note"]
    frontmatter: dict[str, Any] = {"type": note["note_type"], "title": note["title"]}
    if note.get("project"):
        frontmatter["project"] = note["project"]
    frontmatter["status"] = note["status"]
    frontmatter["sources"] = [wikilink_for(item["target_path"])]
    frontmatter["tags"] = list(item.get("tags", []))
    frontmatter.update(note_extra_frontmatter(item, collection))
    return frontmatter


# --------------------------------------------------------------------------- #
# one item
# --------------------------------------------------------------------------- #
def collection_path_of(original_path: str, collection_root: str) -> str:
    """The path an original has inside its collection: the vault path without the collection root."""
    prefix = f"{collection_root}/" if collection_root else ""
    relative = original_path[len(prefix) :] if prefix and original_path.startswith(prefix) else original_path
    return nfc(relative)


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
    notes: NoteIndex,
    ceiling: int,
    vouched: tuple[import_stage.Identity, str] | None = None,
) -> tuple[dict[str, Any], str | None] | dict[str, Any]:
    """Facts, identity and a frozen destination for one original; or a refusal dict."""
    read = read_facts(root, original_path, ceiling=ceiling, text_hint=text_hint, vouched=vouched)
    if isinstance(read, dict):
        return {"path": original_path, **read}
    facts, text = read
    item: dict[str, Any] = {
        **facts,
        "collection_path": collection_path_of(original_path, collection_root),
        "action": "copy-as-source",
        "classification": dict(classification) if classification else None,
    }
    if text is not None:
        item.update(text_facts(original_path, text))
    else:
        item.update(
            title=Path(original_path).stem or original_path,
            tags=[], rejected_tags=[], aliases=[], rejected_aliases=[], _refs=[],
        )

    # Only a Markdown original that read as text becomes a note: an image, a PDF or a
    # non-UTF-8 file under a note rule stays a Source. A note needs a collection label
    # to carry the identity the next run finds it by.
    note_rule = (classification or {}).get("note")
    owes_note = bool(note_rule) and text is not None and collection is not None and is_markdown(original_path)
    earlier = earlier_with_same_bytes(
        index, collection=collection, collection_path=item["collection_path"],
        original_path=original_path, sha256=item["original_sha256"],
    )
    if earlier is not None:
        item["action"] = "already-imported"
        item["target_path"] = earlier.path
        item["target_ref"] = context_refs.source_ref(earlier.path)
        item["existing_ref"] = item["target_ref"]
        item["artifact_path"] = artifact_of(root, earlier.path)
        if owes_note:
            # The Source exists: the note may too (a re-run), or may be owed (a crash, or
            # an earlier import that had no note rule).
            existing = find_note(
                root, notes, collection=collection, collection_path=item["collection_path"]
            )
            if existing is not None:
                item["existing_note"] = existing
            else:
                planned_note = plan_note(
                    root, item, text, note_rule, destinations=destinations, date_iso=date_iso
                )
                if "code" in planned_note:
                    return {"path": original_path, **planned_note}
                item["note"] = planned_note
        return item, text
    changed = latest_earlier_import(
        index, collection=collection, collection_path=item["collection_path"],
        original_path=original_path,
    )
    if changed is not None:
        item["previous_import"] = context_refs.source_ref(changed.path)
        if owes_note:
            previous = find_note(
                root, notes, collection=collection, collection_path=item["collection_path"]
            )
            if previous is not None:
                # The original changed: the earlier note stays as it is, and only a new Source is made.
                item["previous_note"] = previous
                owes_note = False

    folder = _folder_for(root, classification, taxonomy)
    if isinstance(folder, str):
        return {"path": original_path, "code": folder,
                "reason": "the Source taxonomy no longer accepts this rule's kind or domain"}
    slug, slug_warning = slugify_with_truncation_check(item["title"])
    suffix = None
    if item["form"] == "artifact":
        from .preserve import _sanitize_filename

        suffix = Path(_sanitize_filename(Path(original_path).name) or "").suffix
        if suffix.lower() == ".md":
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
    if owes_note:
        planned_note = plan_note(
            root, item, text, note_rule, destinations=destinations, date_iso=date_iso
        )
        if "code" in planned_note:
            return {"path": original_path, **planned_note}
        item["note"] = planned_note
    return item, text


def artifact_of(root: Path, page_path: str) -> str | None:
    """The artifact beside an earlier Source page, when there is one."""
    candidate = page_path[: -len(".md")]
    if candidate != page_path:
        try:
            from .originals import original_state

            if not original_state(root, candidate).present:
                return None
        except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
            return None
        return candidate
    return None


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
class LinkResolver:
    """Resolves a link target to a Source page of the collection.

    Order: the exact collection path, the path relative to the note, a unique
    basename for a bare name, then a unique front-matter alias. A step that finds
    more than one candidate leaves the link unresolved; nothing is guessed.

    A paged collection passes ``whole_scan``, the collection path of every file the scan holds. A name
    is then unique only when no other file of the scan has it, and the alias step is skipped, because
    the aliases of later pages are not known yet. A target that no import holds yet stays unresolved.
    """

    def __init__(
        self, entries: Iterable[Mapping[str, Any]], *, whole_scan: Iterable[str] | None = None
    ) -> None:
        self._by_path: dict[str, set[str]] = {}
        self._by_name: dict[str, set[str]] = {}
        self._by_alias: dict[str, set[str]] = {}
        self._paged = whole_scan is not None
        self.pages: dict[str, dict[str, Any]] = {}
        for entry in entries:
            cp = entry["collection_path"]
            self.pages[cp] = dict(entry)
            self._add(cp)
            for alias in entry.get("aliases") or ():
                self._by_alias.setdefault(_key(alias), set()).add(cp)
        for cp in whole_scan or ():
            self._add(nfc(cp))

    def _add(self, cp: str) -> None:
        name = posixpath.basename(cp)
        for variant in {cp, _strip_markdown_suffix(cp)}:
            self._by_path.setdefault(_key(variant), set()).add(cp)
        for variant in {name, _strip_markdown_suffix(name)}:
            self._by_name.setdefault(_key(variant), set()).add(cp)

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
        if not self._paged:
            steps.append(("alias", self._by_alias.get(_key(target), set())))
        for via, candidates in steps:
            if len(candidates) == 1:
                found = next(iter(candidates))
                if found not in self.pages:
                    return None, via, _UNRESOLVED_LATER_PAGE
                return found, via, None
            if len(candidates) > 1:
                return None, via, _UNRESOLVED_AMBIGUOUS
        return None, "", _UNRESOLVED_NOT_FOUND


def resolve_links(
    items: list[dict[str, Any]],
    index: Iterable[EarlierImport],
    *,
    collection: str | None,
    whole_scan: Iterable[str] | None = None,
) -> None:
    """Fill every text item's ``links`` and drop its raw references.

    The namespace is the planned items plus the collection's earlier imports that
    this plan does not replace, each at its frozen or existing Source page.
    ``whole_scan`` is given for a paged collection (see ``LinkResolver``).
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
    resolver = LinkResolver(entries.values(), whole_scan=whole_scan)
    for item in items:
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for ref in item.pop("_refs", []):
            cp, via, reason = resolver.resolve(ref["raw"], item["collection_path"])
            if cp is None:
                unresolved.append({"raw": ref["raw"], "embed": ref["embed"], "reason": reason})
            elif cp != item["collection_path"]:
                link = {
                    "raw": ref["raw"], "embed": ref["embed"], "via": via,
                    "target_path": resolver.pages[cp]["target_path"],
                }
                if ref.get("fragment"):
                    link["fragment"] = ref["fragment"]
                resolved.append(link)
        item["links"] = {"resolved": resolved, "unresolved": unresolved}
    # A note's own links point at the notes of the items it links to, not at their Sources.
    note_of = {
        item["target_path"]: (item.get("note") or {}).get("target_path") or item["existing_note"]
        for item in items
        if item.get("note") or item.get("existing_note")
    }
    for item in items:
        if item.get("note"):
            item["note"]["links"] = {
                "resolved": [
                    {**link, "target_path": note_of.get(link["target_path"], link["target_path"])}
                    for link in item["links"]["resolved"]
                ],
                "unresolved": item["links"]["unresolved"],
            }


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
    if item.get("previous_note"):
        frontmatter["previous_note"] = context_refs.vault_ref(item["previous_note"])
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
    "NoteIndex",
    "OVER_CEILING",
    "item_digest",
    "artifact_of",
    "collection_path_of",
    "decode_text",
    "earlier_import_of",
    "earlier_with_same_bytes",
    "find_note",
    "is_markdown",
    "item_frontmatter",
    "latest_earlier_import",
    "normalize_aliases",
    "normalize_tags",
    "note_extra_frontmatter",
    "note_frontmatter",
    "plan_item",
    "plan_note",
    "plan_total_bytes",
    "read_import_entry",
    "read_import_index",
    "resolve_links",
    "source_page_paths",
    "sources_seal",
    "title_from_text",
    "wikilink_for",
]
