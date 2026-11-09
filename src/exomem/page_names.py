"""Name a page by its title or a short id, wherever a tool takes a page path.

`resolve_page` is the one resolver for every parameter that names a page. A path, a memory ref and
a KB shorthand keep the meaning they had. Two more spellings now name a page:

* a title, found through the writer resolver (`find.shared_resolver`), which every governed write
  patches in its own fan-out, so a page created a second ago resolves;
* a short id, `<7 base32 of exomem_id>.<4 base32 of the content hash>`: the head names the page and
  survives edits and moves, the tail names the version the caller read.

Disclosure comes first. The visibility filter runs before anything is counted or read, so a withheld
duplicate neither causes an ambiguity nor appears as a candidate, and a title that only withheld
pages hold resolves to nothing, exactly like an unknown title.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import find, find_corpus, memory_refs, reserved_paths, semantic_contract, vault
from .cli_ops import OpError
from .governance import egress
from .kbdir import kb_dirname, kb_prefix

#: A refusal names at most this many pages to choose from.
MAX_CANDIDATES = 5
#: Newest-first ordering reads the frontmatter of the candidates, so the read is bounded.
_MAX_CANDIDATES_READ = 100
#: The retry names the id of the page the caller picks; the caller fills it in.
ID_PLACEHOLDER = "<id>"

# A format check on a value this module itself mints (fixed-width RFC 4648 base32 alphabet), not a
# reading of what a caller meant, so it is sound under registry C6.
_ID_PATTERN = re.compile(r"[a-z2-7]{7}\.[a-z2-7]{4}")
_HEAD_CHARS = 7
_VERSION_CHARS = 4


def is_page_id(value: object) -> bool:
    """Whether `value` has the shape of a short page id."""
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def _base32(raw: bytes, chars: int) -> str:
    return base64.b32encode(raw).decode("ascii").lower()[:chars]


def id_head(exomem_id: str | None, rel_path: str) -> str:
    """7 base32 characters of `exomem_id`; a page without one takes its path hash."""
    if exomem_id:
        return _base32(uuid.UUID(exomem_id).bytes[:5], _HEAD_CHARS)
    return _base32(hashlib.sha256(rel_path.encode("utf-8")).digest()[:5], _HEAD_CHARS)


def id_version(content_hash: str) -> str:
    """4 base32 characters of the page's content hash."""
    return _base32(bytes.fromhex(content_hash)[:3], _VERSION_CHARS)


def page_id(exomem_id: str | None, rel_path: str, content_hash: str) -> str:
    return f"{id_head(exomem_id, rel_path)}.{id_version(content_hash)}"


@dataclass(frozen=True, slots=True)
class PageIdentity:
    """What one page's current bytes say about it."""

    path: str
    title: str
    type: str | None
    date: str | None
    id: str
    content_hash: str

    def row(self) -> dict[str, Any]:
        return {"title": self.title, "type": self.type, "date": self.date, "id": self.id}


def read_identity(root: Path, rel_path: str) -> PageIdentity | None:
    """The identity of the Markdown page at `rel_path`, read from its bytes; None if unreadable."""
    if not rel_path.endswith(".md"):
        return None
    try:
        raw = (Path(root) / rel_path).read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    digest = vault.content_hash(text)
    frontmatter, body, _ = vault.parse_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))
    row = memory_refs.identity_row(Path(root), rel_path, raw)
    exomem_id = row[1] if row is not None and row[4] == "valid" else None
    stamp = frontmatter.get("updated") or frontmatter.get("created")
    date = stamp.isoformat() if hasattr(stamp, "isoformat") else stamp
    kind = frontmatter.get("type")
    return PageIdentity(
        path=rel_path,
        title=vault.resolve_display_title(frontmatter, body, rel_path),
        type=str(kind) if kind else None,
        date=str(date) if date else None,
        id=page_id(exomem_id, rel_path, digest),
        content_hash=digest,
    )


@dataclass(frozen=True, slots=True)
class NamedPage:
    """The page a caller named. `id` is set only when the caller named it by short id."""

    path: str
    id: str | None = None

    @property
    def version(self) -> str | None:
        return None if self.id is None else self.id.split(".", 1)[1]


class AmbiguousTitle(OpError):
    """Several pages the caller may see share a title; the refusal lists up to `MAX_CANDIDATES`."""


def resolve_reference(root: Path, value: str, *, serve_stale: bool = False) -> str:
    """Resolve a path or memory ref to the page a caller may see, as every page parameter did.

    A command that goes on to write from the answer keeps the default. A read passes `serve_stale`,
    so the lookup answers from the identity projection even while the watcher publishes an outside
    edit, rather than scanning the vault.
    """
    try:
        resolved = egress.resolve_visible_identifier(root, value, serve_stale=serve_stale)
    except memory_refs.ReferenceError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc
    if reserved_paths.classify_logical(resolved).blocked:
        raise ValueError("NOT_FOUND: memory identifier is unavailable")
    return resolved


def _names_a_file(root: Path, raw: str) -> bool:
    """Whether `raw` already names a file as a vault-relative or KB-relative path.

    Such a call keeps its meaning and needs no resolver: a path that exists is never a title.
    """
    stem = raw.removesuffix(".md")
    for base in ("", kb_prefix()):
        for candidate in (f"{base}{raw}", f"{base}{stem}.md"):
            try:
                if (root / candidate).is_file():
                    return True
            except (OSError, ValueError):
                return False
    return False


def _identity_owners(root: Path, serve_stale: bool) -> Iterable[str]:
    """Every well-formed `exomem_id` the vault holds, from the identity projection or one scan."""
    snapshot = semantic_contract.current_reference_identity_snapshot(
        root, tolerate_external_pending=serve_stale
    )
    if snapshot is not None:
        return tuple(snapshot.reference_owners)
    owners = memory_refs.scan_reference_owners(root, root / kb_dirname())
    return tuple(owner for _path, owner in owners)


def _paths_for_head(root: Path, head: str, *, serve_stale: bool) -> list[str]:
    """The pages this caller may see whose id head is `head`."""
    # The head holds the first 35 bits of an id, and its first 8 hex digits are all inside them, so
    # they narrow the owners before each survivor is checked in full.
    leading = base64.b32decode((head + "a").upper()).hex()[:8]
    paths: set[str] = set()
    for owner in _identity_owners(root, serve_stale):
        if not owner.startswith(leading) or id_head(owner, "") != head:
            continue
        try:
            paths.add(
                egress.resolve_visible_identifier(
                    root, memory_refs.memory_ref(owner), serve_stale=serve_stale
                )
            )
        except memory_refs.ReferenceError as exc:
            if exc.code == "AMBIGUOUS_REFERENCE":
                raise ValueError(f"{exc.code}: {exc.reason}") from exc
            # Held only by pages this caller may not see, or by none that exists now.
            continue
    if paths:
        return sorted(paths)
    # A page with no id of its own is named by a hash of its path.
    resolver = find.shared_resolver(root)
    visible = egress.visible_page_filter(root)
    return sorted(
        candidate.path
        for candidates in resolver.candidates_by_key.values()
        for candidate in candidates
        if id_head(None, candidate.path) == head
        and (visible is None or visible(candidate.path))
        and _carries_head(root, candidate.path, head)
    )


def _carries_head(root: Path, rel_path: str, head: str) -> bool:
    """Whether the page at `rel_path` has no id of its own, so that its path hash is its head."""
    found = read_identity(root, rel_path)
    return found is not None and found.id.split(".", 1)[0] == head


def _ambiguous(root: Path, shown: str, paths: list[str], field: str) -> AmbiguousTitle:
    """The refusal for several visible pages that one title or id head names."""
    identities = [
        found
        for found in (read_identity(root, path) for path in paths[:_MAX_CANDIDATES_READ])
        if found is not None
    ]
    identities.sort(key=lambda found: found.date or "", reverse=True)
    return AmbiguousTitle(
        "AMBIGUOUS_REFERENCE",
        f"{len(paths)} pages are titled \u201c{shown}\u201d.",
        details={
            "candidates": [found.row() for found in identities[:MAX_CANDIDATES]],
            "retry_with": {field: ID_PLACEHOLDER},
        },
    )


def prefer_editable(rel_path: str) -> bool:
    """Whether an edit can change the page at `rel_path`: not raw material or navigation."""
    name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    return (
        vault.in_append_only_tree(rel_path) is None
        and name.casefold() not in find_corpus.NAVIGATION_BASENAMES
    )


def resolve_page(
    root: Path,
    value: str,
    *,
    prefer: Callable[[str], bool] | None = None,
    field: str = "path",
    serve_stale: bool = False,
) -> NamedPage:
    """The page `value` names, as a title or a short id when it is neither a ref nor a path.

    Order: a memory ref; an existing file path; a short id; a title. A value that names nothing is
    returned as the path it always was, so today's NOT_FOUND and create-missing paths still apply.
    `prefer` narrows several title matches to the pages it accepts (an edit prefers a page it can
    edit) before the call refuses. `field` is the command argument that held `value`, which the
    refusal's retry replaces with the id of the page the caller picks.
    """
    root = Path(root)
    raw = str(value or "").strip()
    if not raw or raw.lower().startswith("exomem://") or _names_a_file(root, raw):
        return NamedPage(resolve_reference(root, value, serve_stale=serve_stale))
    if is_page_id(raw):
        paths = _paths_for_head(root, raw.split(".", 1)[0], serve_stale=serve_stale)
        if len(paths) > 1:
            # Two pages share a 35-bit head; the version the caller read tells them apart.
            paths = [
                path
                for path in paths
                if (found := read_identity(root, path)) is not None and found.id == raw
            ] or paths
        if len(paths) > 1:
            raise _ambiguous(root, raw, paths, field)
        if paths:
            return NamedPage(resolve_reference(root, paths[0]), id=raw)
        # No page holds that id: the value may still be a title of the same shape.
    key = vault.title_key(raw)
    resolver = find.shared_resolver(root)
    matches = vault.wikilink_candidates(
        resolver.candidates_by_key,
        resolver.titles.get(key, ()),
        title_key=key,
        visible=egress.visible_page_filter(root),
    )
    # A page the vault never serves is no page: it counts for nothing, as an unknown title does.
    paths = [
        match.path for match in matches if not reserved_paths.classify_logical(match.path).blocked
    ]
    if len(paths) > 1 and prefer is not None:
        paths = [path for path in paths if prefer(path)] or paths
    if len(paths) > 1:
        raise _ambiguous(root, raw, paths, field)
    return NamedPage(resolve_reference(root, paths[0] if paths else raw))


def resolve_ref(root: Path, value: str, *, field: str = "ref") -> str:
    """The stable memory ref of the page `value` names, for a parameter that takes only a ref.

    A value that is a ref, or names no page that holds an id, is returned unchanged, so the
    operation refuses it as before.
    """
    raw = str(value or "").strip()
    if raw.lower().startswith("exomem://"):
        return value
    path = resolve_page(Path(root), raw, field=field).path
    owner = memory_refs.read_reference_owner(Path(root), Path(root) / path)
    return memory_refs.memory_ref(owner) if owner else value


def resolve_hint(root: Path, value: str) -> str:
    """The path of the one page `value` names as a title or short id, else `value` unchanged.

    For a value that only hints (a read page the conversation held): a hint that half-missed is
    still a hint, so nothing refuses and nothing is said about why it matched no page.
    """
    raw = str(value or "").strip()
    if raw.lower().startswith("exomem://"):
        return value
    try:
        return resolve_page(Path(root), raw).path
    except (AmbiguousTitle, ValueError):
        return value


def stale_edit(identity: PageIdentity, *, path_field: str, guard_field: tuple[str, ...]) -> OpError:
    """The refusal for a write guarded by a version the page has moved past; it names its id now."""
    guard: Any = identity.id
    for name in reversed(guard_field):
        guard = {name: guard}
    return OpError(
        "STALE_EDIT",
        f"The page changed since that id was issued; its current id is {identity.id}.",
        details={"retry_with": {path_field: identity.id, **guard}},
    )


def guard_hash(
    root: Path,
    named: NamedPage,
    expected_hash: str | None,
    *,
    required: bool,
    path_field: str = "path",
    guard_field: tuple[str, ...] = ("expected_hash",),
) -> str | None:
    """The full content hash a leaf guard compares, translated from a short id where one stands.

    A short id in `expected_hash` is read against the page's current id. A page named by id whose
    operation requires `expected_hash` and got none is guarded by the id it was named by. Every
    other value passes through, so an optional guard stays optional. A match returns the page's
    full current hash, which the leaf still compares at write time; a stale id refuses with the
    current id. `path_field` and `guard_field` spell that refusal's retry in the command's terms.
    """
    given = expected_hash.strip() if isinstance(expected_hash, str) else None
    filled = named.id if expected_hash is None and required else None
    wanted = given if is_page_id(given) else filled
    if wanted is None:
        return expected_hash
    current = read_identity(Path(root), named.path)
    if current is None:
        return expected_hash
    if current.id != wanted:
        raise stale_edit(current, path_field=path_field, guard_field=guard_field)
    return current.content_hash


def id_of_read(reply: Mapping[str, Any]) -> str | None:
    """The short id of the Markdown page a read reply carries, from its path, ref and content hash.

    A page with no `ref` has no id of its own and takes its path hash. None when the reply holds
    no path or hash, or names a file that is not a page.
    """
    path, ref, digest = reply.get("path"), reply.get("ref"), reply.get("content_hash")
    if not (isinstance(path, str) and path.endswith(".md") and isinstance(digest, str)):
        return None
    owner = memory_refs.parse_memory_ref(ref) if isinstance(ref, str) else None
    try:
        return page_id(owner, path, digest)
    except ValueError:
        return None


#: A reply that lists pages is bounded by what it was asked for; a write that reports more pages
#: than this keeps their paths, since naming each reads its bytes.
_MAX_NAMED_PAGES = 20


def _renamed(item: Mapping[str, Any], swaps: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """`item` with each key in `swaps` replaced, in place, by that mapping's entries."""
    out: dict[str, Any] = {}
    for key, value in item.items():
        out.update(swaps.get(key, {key: value}))
    return out


def _name_hit(root: Path, hit: Any, *, keep_paths: bool) -> Any:
    if not isinstance(hit, Mapping) or not isinstance(hit.get("path"), str):
        return hit
    found = read_identity(root, hit["path"])
    if found is None:
        return hit
    return {**hit, "id": found.id} if keep_paths else _renamed(hit, {"path": {"id": found.id}})


def _name_write(root: Path, reply: dict[str, Any]) -> dict[str, Any]:
    source = reply.get("source")
    path = reply.get("path")
    paths = reply.get("paths")
    wanted = [path] if isinstance(path, str) else []
    if isinstance(paths, list) and paths and len(paths) <= _MAX_NAMED_PAGES:
        wanted += [item for item in paths if isinstance(item, str)]
    if isinstance(source, Mapping) and isinstance(source.get("path"), str):
        wanted.append(source["path"])
    found = {item: read_identity(root, item) for item in dict.fromkeys(wanted)}
    named = {item: identity for item, identity in found.items() if identity is not None}
    if not named:
        return reply
    swaps: dict[str, Mapping[str, Any]] = {}
    moved: dict[str, Any] = {}
    if isinstance(path, str) and path in named:
        swaps["path"] = {"title": named[path].title, "id": named[path].id}
        moved["path"] = path
    if isinstance(paths, list) and len(named) and all(item in named for item in paths):
        swaps["paths"] = {
            "pages": [{"title": named[item].title, "id": named[item].id} for item in paths]
        }
        moved["paths"] = paths
    if isinstance(source, Mapping) and source.get("path") in named:
        picked = named[source["path"]]
        swaps["source"] = {"source": {"title": picked.title, "id": picked.id}}
        moved["source_path"] = source["path"]
    out = _renamed(reply, swaps)
    summary = out.get("user_summary")
    if isinstance(summary, str):
        # The summary names each page once, by the exact path `_user_summary` was given; the longest
        # goes first so that no path is cut by another that is part of it.
        for item in sorted(named, key=len, reverse=True):
            summary = summary.replace(item, named[item].title)
        out["user_summary"] = summary
    diagnostics = out.get("diagnostics")
    if moved and isinstance(diagnostics, Mapping):
        operational = diagnostics.get("operational")
        out["diagnostics"] = {
            **diagnostics,
            "operational": {**(operational if isinstance(operational, Mapping) else {}), **moved},
        }
    return out


def name_released_reply(
    root: Path, reply: dict[str, Any], *, keep_paths: bool = False
) -> dict[str, Any]:
    """Name each page a released MCP reply reports by title and short id, not by vault path.

    A write reply swaps `path` for `title` and `id`; `response_detail="full"` still carries the
    path under `diagnostics.operational`. A search reply swaps each page hit's `path` for its `id`
    (the hit keeps its `title` and `ref`). `keep_paths`, set for `detail="full"`, adds the `id` and
    keeps the path, for a harness that reads vault files. A page that cannot be read keeps its
    path. This runs after the egress release, so it names only what that filter let through. A
    reply that is none of these comes back unchanged.
    """
    root = Path(root)
    hits = reply.get("hits")
    if isinstance(hits, list):
        return {**reply, "hits": [_name_hit(root, hit, keep_paths=keep_paths) for hit in hits]}
    return _name_write(root, reply)
