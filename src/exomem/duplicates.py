"""Exact-bytes duplicate identity: what a page holds, and the one lookup of an earlier copy.

A duplicate is identical bytes and nothing near. The lexical catalogue keeps a derived column of
payload hashes (`lexstore.payload_candidates`) that only proposes candidates; `find_duplicate`
re-derives each candidate's payload from the stored bytes before it counts as a hit, and applies the
caller's read admission. A stale or damaged index therefore costs a missed reuse, never a wrong one.
"""

from __future__ import annotations

import hashlib
import http.client
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import memory_refs, originals, reserved_paths, vault
from .governance import raw_protection
from .source_coverage import CAPTURE_HEADING, fenced_capture

#: The two lanes that hold originals. The layout fixes them (`vault.APPEND_ONLY_KB_SUBPATHS`).
SOURCES, EVIDENCE = vault.APPEND_ONLY_KB_SUBPATHS

def text_payload_sha256(text: str) -> str:
    """The payload hash of captured text, exactly as `add` stores it: stripped, UTF-8, outside the page frame."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str | None:
    """`url` with its scheme and host lowercased and its default port and fragment dropped.

    The path and query stay exactly as written. None when `url` is not an absolute URL with a host.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    # The default ports that the HTTP specification fixes for these two URL schemes.
    default_port = (
        http.client.HTTPS_PORT if scheme == "https" else http.client.HTTP_PORT if scheme == "http" else None
    )
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (f"{userinfo}@" if userinfo else "") + host + (f":{port}" if port not in (None, default_port) else "")
    return urllib.parse.urlunsplit((scheme, netloc, parts.path, parts.query, ""))


@dataclass(frozen=True, slots=True)
class PagePayload:
    """What one live Sources or Evidence page holds as its identity."""

    sha256: str
    lane: str
    protected: bool
    #: The vault path of the original that `sha256` hashes, or None when the page's own captured text is the payload.
    artifact_path: str | None
    #: The canonical form of a Source's URL, or None.
    source_url: str | None
    ref: str | None
    frontmatter: Mapping[str, Any]
    text: str


def _stored_text(body: str) -> str | None:
    """The captured text of a Source page, or None when the page has no `## Capture` section."""
    marker = f"\n{CAPTURE_HEADING}\n"
    at = body.find(marker)
    return None if at == -1 else body[at + len(marker):].strip()


def derive_page_payload(rel: str, raw: bytes) -> PagePayload | None:
    """The payload identity of the page at `rel` whose bytes are `raw`, or None when it holds none.

    One function decides it for the catalogue's column (every build and upsert) and for the lookup's
    re-derivation, so the two cannot drift. A text Source's payload is its captured text; an imported text
    file's is its fenced text exactly as stored, which equals the file's bytes; an original's is the digest
    its page records, which `find_duplicate` re-hashes against the file itself. A superseded page holds no
    payload, because it is a retired copy.
    """
    lane = vault.in_append_only_tree(rel)
    if lane is None or not rel.lower().endswith(".md"):
        return None
    try:
        text = raw.decode("utf-8")
        frontmatter, body, marker = vault.parse_frontmatter(text, strict=True)
    except (UnicodeDecodeError, vault.FrontmatterError):
        return None
    if marker is None:
        return None
    # A superseded page is a retired copy, so it is no payload holder (`duplicate_fold` retires copies this way).
    if str(frontmatter.get("status") or "").casefold() == "superseded":
        return None
    protected = raw_protection.marked(rel)
    source_url = None
    if lane == EVIDENCE:
        holder = frontmatter.get("governance_companion")
        if not isinstance(holder, Mapping):
            return None
        artifact, digest = holder.get("artifact_path"), holder.get("artifact_sha256")
    else:
        # The recap recorder revises its one Source in place, so its text is not a captured payload.
        if "episode" in frontmatter:
            return None
        url = frontmatter.get("url")
        source_url = canonical_url(url) if isinstance(url, str) else None
        artifact, digest = frontmatter.get("evidence_file"), frontmatter.get("binary_sha256")
        if artifact is None and digest is None:
            if "original_sha256" in frontmatter:
                # An imported text file: its bytes are the file's, so its fenced text is hashed exactly as stored.
                imported = fenced_capture(text)
                if imported is None:
                    return None
                payload_sha256 = hashlib.sha256(imported.encode("utf-8")).hexdigest()
            else:
                captured = _stored_text(body)
                if captured is None:
                    return None
                payload_sha256 = text_payload_sha256(captured)
            return PagePayload(
                sha256=payload_sha256, lane=lane, protected=protected, artifact_path=None,
                source_url=source_url, ref=memory_refs.ref_from_markdown(text), frontmatter=frontmatter, text=text,
            )
    if not (isinstance(artifact, str) and artifact and originals.is_sha256(digest)):
        return None
    return PagePayload(
        sha256=digest, lane=lane, protected=protected, artifact_path=artifact, source_url=source_url,
        ref=memory_refs.ref_from_markdown(text), frontmatter=frontmatter, text=text,
    )


@dataclass(frozen=True, slots=True)
class Duplicate:
    """An earlier page whose stored bytes were re-derived to the requested payload."""

    path: str
    ref: str
    payload: PagePayload


@dataclass(frozen=True, slots=True)
class DuplicateLookup:
    """The answer of one lookup. `available` False means the index could not answer, which is not "no copy"."""

    hit: Duplicate | None = None
    available: bool = True


def withheld(vault_root, rel: str) -> bool:
    """Whether the current caller may not read the page at `rel` in full."""
    from .governance import egress
    from .governance.policy import DISCLOSURE_MAX

    return egress.release_level_for(vault_root, rel) != DISCLOSURE_MAX


def stored_payload(vault_root, rel: str) -> PagePayload | None:
    """The payload of the page at `rel` as it is stored, or None when it holds none. The caller has checked `withheld`."""
    try:
        raw = reserved_paths.read_generic_bytes(vault_root, rel).data
    except (OSError, ValueError, reserved_paths.ReservedPathLeafError):
        return None
    return derive_page_payload(rel, raw)


def readable_page(vault_root, rel: str) -> PagePayload | None:
    """The payload of `rel` when the current caller may read the page in full, else None."""
    return None if withheld(vault_root, rel) else stored_payload(vault_root, rel)


def binding_unproven(vault_root, rel: str, payload: PagePayload) -> bool:
    """Whether the page at `rel` is raw-protected and lacks the raw-protection binding that proves its original.

    A release grant binds to the page's content hash, so a protected page whose binding is missing or names other
    bytes cannot be verified, whatever the original on disk holds.
    """
    if not payload.protected:
        return False
    found = raw_protection.binding(vault_root, rel)
    if found is None:
        return True
    block, companion, _ = found
    return (
        companion != rel or block["original_ref"] != payload.ref
        or block["artifact_path"] != payload.artifact_path or block["artifact_sha256"] != payload.sha256
    )


def original_size(vault_root, rel: str, payload: PagePayload) -> int | None:
    """The size of the original that the page at `rel` names when it still hashes to the payload it records, else None."""
    artifact = payload.artifact_path
    # The page is vault data. An original outside its own folder is some other file's bytes.
    if artifact is None or PurePosixPath(artifact).parent != PurePosixPath(rel).parent:
        return None
    if binding_unproven(vault_root, rel, payload):
        return None
    try:
        with reserved_paths.open_generic_binary(vault_root, artifact) as original:
            digest, size = hashlib.sha256(), 0
            while chunk := original.stream.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
    except (OSError, reserved_paths.ReservedPathLeafError):
        return None
    return size if digest.hexdigest() == payload.sha256 else None


def original_intact(vault_root, rel: str, payload: PagePayload) -> bool:
    """Whether the original that the page at `rel` names still hashes to the payload it records."""
    return original_size(vault_root, rel, payload) is not None


def find_duplicate(
    vault_root, *, sha256: str, lane: str, protected: bool, keeps_original: bool | None = None,
    prefer: Callable[[Duplicate], bool] | None = None,
) -> DuplicateLookup:
    """The earliest live page of `lane` and protection class whose stored payload hashes to `sha256`.

    The one call every storing door makes under its mutation boundary. A candidate counts only after its
    bytes are read again and re-derived (a text payload) or re-hashed (an original), and only when the
    current caller may read the page in full. A candidate that fails either test reads as absent, so the
    answer equals the answer of a vault without that page. `available` is False when the catalogue cannot
    answer; the door then stores the content as new and carries `duplicate_check_unavailable`.

    `keeps_original` narrows the hit to one form of page: True admits only a page that keeps the bytes as a file
    (a file hand-over has no use for a text Source whose captured text happens to hash alike), False only a page
    that holds them as captured text, and None either. `prefer` names the hit a door would rather have
    when several live copies exist (older vaults hold one per family); the earliest hit stands when none
    qualifies.
    """
    from . import lexstore

    if not originals.is_sha256(sha256):
        return DuplicateLookup()
    candidates = lexstore.payload_candidates(vault_root, sha256)
    if candidates is None:
        return DuplicateLookup(available=False)
    first: Duplicate | None = None
    for rel in candidates:
        hit = named_duplicate(
            vault_root, rel, sha256=sha256, lane=lane, protected=protected, keeps_original=keeps_original
        )
        if hit is None:
            continue
        if prefer is None or prefer(hit):
            return DuplicateLookup(hit)
        first = first or hit
    return DuplicateLookup(first)


def named_duplicate(
    vault_root, rel: str, *, sha256: str, lane: str, protected: bool, keeps_original: bool | None = None
) -> Duplicate | None:
    """The page at `rel` when it still stores `sha256` in `lane` and the protection class, else None.

    The one re-verification of a candidate, shared by `find_duplicate` and by a door that already holds the
    page it means (an apply of a plan that named it) and so needs no catalogue. The page's bytes are read
    again and re-derived (a text payload) or re-hashed (an original), and only a caller who may read the page in
    full gets it; a page that fails any test reads as absent. `keeps_original` narrows the form as in
    `find_duplicate`.
    """
    if vault.in_append_only_tree(rel) != lane or raw_protection.marked(rel) != protected:
        return None
    payload = readable_page(vault_root, rel)
    if (payload is None or payload.sha256 != sha256 or payload.lane != lane
            or payload.protected != protected or payload.ref is None):
        return None
    if keeps_original is not None and (payload.artifact_path is not None) != keeps_original:
        return None
    if payload.artifact_path is not None and not original_intact(vault_root, rel, payload):
        return None
    return Duplicate(path=rel, ref=payload.ref, payload=payload)


def unavailable_warning(subject: str) -> str:
    """The warning a door carries when the duplicate index could not answer, so `subject` was stored unchecked."""
    return (
        f"duplicate_check_unavailable: the duplicate index could not answer, so {subject} was stored without "
        "a check for an earlier copy; the audit reports any copy later"
    )


@dataclass(frozen=True, slots=True)
class SameUrl:
    """Live Sources the caller can read that share one canonical URL: the newest few and the total."""

    sources: tuple[Duplicate, ...]
    count: int


def same_url_sources(vault_root, url: str, *, limit: int = 5) -> SameUrl | None:
    """The readable Sources that share `url`'s canonical form, or None when there are none or the index is out.

    Nothing here reuses, supersedes or changes a page: it feeds an advisory only. A candidate counts after its
    page is read again and its own URL re-derives to the same canonical form.
    """
    from . import lexstore

    canonical = canonical_url(url)
    if canonical is None:
        return None
    candidates = lexstore.source_url_candidates(vault_root, canonical)
    found: list[Duplicate] = []
    for rel in candidates or ():
        if vault.in_append_only_tree(rel) != SOURCES or raw_protection.marked(rel):
            continue
        payload = readable_page(vault_root, rel)
        if payload is None or payload.source_url != canonical or payload.ref is None:
            continue
        found.append(Duplicate(path=rel, ref=payload.ref, payload=payload))
    if not found:
        return None
    found.reverse()  # the newest capture reads first
    return SameUrl(sources=tuple(found[:limit]), count=len(found))
