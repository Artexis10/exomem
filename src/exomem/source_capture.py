"""Portable caller identity for canonical text Source captures."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import indexes, memory_refs, note_types, source_taxonomy, vault
from .kbdir import managed_join


class SourceIdentity(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    system: Annotated[str, Field(strict=True, min_length=1)]
    record_id: Annotated[str, Field(strict=True, min_length=1)]
    revision: Annotated[str, Field(strict=True, min_length=1)]


CaptureOutcome = Literal["created", "reused"]
_IDENTITY = TypeAdapter(SourceIdentity)
MAX_PASSAGES = 2000
MAX_PASSAGE_KEY_BYTES = 128


class SourceCaptureError(ValueError):
    """A coded refusal before a durable capture effect."""


class RawEnumerationMoved(SourceCaptureError):
    """A staged directory or page vanished mid-enumeration.

    Still `SOURCE_IDENTITY_UNAVAILABLE`: a concurrent mutation may have moved
    the vanished entry to a final path the walk already passed, so the
    remaining enumeration cannot prove complete. Callers that are only
    planning (nothing committed yet) may retry; everything else keeps the
    fail-closed `SourceCaptureError` contract.
    """


class DeclaredPassage(TypedDict):
    key: str
    text: str


def validate_passages(value: object) -> list[DeclaredPassage]:
    """Keep caller text exact; reject declarations the Source cannot preserve."""
    if not isinstance(value, list) or not value:
        raise SourceCaptureError("INVALID_PASSAGES: passage 1 field passages must be a nonempty list of key/text objects")
    if len(value) > MAX_PASSAGES:
        raise SourceCaptureError(f"INVALID_PASSAGES: passage {MAX_PASSAGES + 1} field passages exceeds the limit of {MAX_PASSAGES}; split the capture")
    seen: dict[str, int] = {}
    result: list[DeclaredPassage] = []
    for ordinal, passage in enumerate(value, 1):
        if not isinstance(passage, dict) or set(passage) != {"key", "text"}:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} fields must be exactly key and text")
        key, text = passage["key"], passage["text"]
        try:
            size = len(key.encode("utf-8")) if isinstance(key, str) else 0
        except UnicodeError:
            size = 0
        if not 1 <= size <= MAX_PASSAGE_KEY_BYTES:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field key must be a string of 1 to {MAX_PASSAGE_KEY_BYTES} UTF-8 bytes")
        if key in seen:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field key repeats passage {seen[key]}; supply a unique key")
        seen[key] = ordinal
        if not isinstance(text, str) or not text:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field text must be a nonempty string")
        if text != text.strip():
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field text has edge whitespace; trim the text")
        if "\r" in text:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field text contains a carriage return; use LF line endings")
        if any(not line.strip() for line in text.split("\n")):
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field text contains a blank line; split it into separate passages")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise SourceCaptureError(f"INVALID_PASSAGES: passage {ordinal} field text must contain valid UTF-8 text") from None
        result.append({"key": key, "text": text})
    return result


def validate_sha256(value: str) -> str:
    """Validate the caller's SHA-256 assertion without changing its meaning."""
    # SHA-256 hexadecimal is a fixed wire format, not a semantic vocabulary.
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise SourceCaptureError("INVALID_SOURCE_HASH: expected_sha256 must contain 64 lowercase hexadecimal characters")
    return value


def verify_sha256(expected: str | None, actual: str) -> None:
    """Refuse altered original bytes before the writer publishes anything."""
    if expected is not None and not hmac.compare_digest(validate_sha256(expected), actual):
        raise SourceCaptureError("SOURCE_HASH_MISMATCH: original bytes differ from expected_sha256; send the original file through an upload handoff or file handle")


def validate_identity(value: object) -> SourceIdentity:
    try:
        return _IDENTITY.validate_python(value)
    except ValidationError:
        raise SourceCaptureError("INVALID_SOURCE_IDENTITY: supply system, record_id and revision as nonempty strings") from None


def require_authority(root: Path) -> None:
    from .governance import (
        authorization_session_lifecycle,
        decisions,
        egress,
        policy,
        principal,
    )

    # Existence and conflict responses would otherwise reveal hidden captures.
    # Restricted callers retain ordinary capture without durable identity.
    forbidden = "SOURCE_IDENTITY_FORBIDDEN: whole-namespace content authority is required"
    who = principal.effective_principal()
    context = who.verified_authorization_session
    if context is not None and not isinstance(
        context, authorization_session_lifecycle.AuthorizationSessionContext
    ):
        raise SourceCaptureError(forbidden)
    if not egress.unrestricted_content_access(root, who):
        raise SourceCaptureError(forbidden)
    compiled = policy.load(root)
    if compiled.blocked:
        raise SourceCaptureError(forbidden)
    if compiled.empty:
        return
    try:
        purpose = egress._declared_purpose(root, who, None)
    except egress.AuthorizationSessionDecisionUnavailable:
        raise SourceCaptureError(forbidden) from None
    # All scopes conservatively prove namespace authority without selector analysis.
    if decisions.decide(tuple(compiled.scopes), audience=who.audience_id, purpose=purpose,
                        policy=compiled, active_grants=compiled.grants).level < egress.LEVEL_FULL:
        raise SourceCaptureError(forbidden)


def capture_digest(root: Path, *, content: str, title: str, source_type: str, url: str | None,
                   tags: list[str] | None, why_captured: str | None, domain: str | None,
                   projects: list[str] | None, slug: str | None, raw_protection: bool,
                   passages: list[DeclaredPassage] | None = None) -> str:
    from .add import _clean_tags

    taxonomy = source_taxonomy.load_taxonomy(root)
    # This closed field map is the authored capture-input contract, not vocabulary.
    original = {
        "content": content,
        "title": title.strip(),
        "source_kind": taxonomy.resolve_kind(source_type).key,
        "url": url or None,
        "tags": _clean_tags(tags),
        "why_captured": why_captured.strip() if why_captured else None,
        "domain": taxonomy.resolve_domain(domain).key if domain is not None else None,
        "projects": list(dict.fromkeys(projects or ())),
        "slug": vault.resolve_filename_slug(title, slug, vault_root=root)[0] if slug is not None else None,
        "raw_protection": raw_protection,
    }
    if passages is not None:
        original["passages"] = passages
    return hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalCapturePages:
    canonical: frozenset[str]
    snapshots: dict[str, bytes]
    parsed: dict[str, dict]
    frontmatter_free: frozenset[str]
    store_revision: int | None = None


@dataclass(frozen=True, slots=True)
class CapturePageInput:
    """Original-byte companion inputs; no rendered body or duplicate passage text."""

    sha256: str
    size: int
    fields: Mapping
    frontmatter_free: bool
    invalid: bool
    bindings: tuple[tuple[str, str, int, bool], ...]


def capture_page_input(root: Path, path: str, data: bytes) -> CapturePageInput:
    """Parse one publisher-owned original, including companion descriptor validation."""
    from .client_artifacts import _expected_companion_paths
    from .governance import companions, raw_protection
    from .preserve import _sidecar_path
    from .semantic_contract import _freeze

    fields: dict = {}
    free = invalid = False
    bindings = []
    try:
        text = data.decode("utf-8")
        fields, _body, marker = vault.parse_frontmatter(text, strict=True)
        free = marker is None
        if free:
            lines = text.removeprefix("\ufeff").splitlines()
            invalid = bool(lines and lines[0] == "---")  # Fixed YAML frontmatter opener.
        if note_types.Basis(root).selects(fields.get("type"), note_types.raw):
            declarations = []
            # These fields are the maintained companion byte-binding schema.
            if "evidence_file" in fields:
                declarations.append((fields["evidence_file"], fields.get("binary_sha256"), fields.get("binary_size"), False))
            descriptor = fields.get("governance_companion")
            if descriptor is not None:
                if not isinstance(descriptor, dict):
                    invalid = True
                else:
                    declarations.append((descriptor.get("artifact_path"), descriptor.get("artifact_sha256"), descriptor.get("artifact_size"), True))
            for target, digest, size, governed in declarations:
                if not isinstance(target, str):
                    invalid = True
                    continue
                if Path(target).suffix.lower() != ".md":  # Canonical page format.
                    continue
                owners = tuple(p.as_posix() for p in _expected_companion_paths(PurePosixPath(target), lane="source"))
                evidence_owner = _sidecar_path(Path(target), protected=raw_protection.marked(target)).as_posix()
                valid = (path in owners or path == evidence_owner) and isinstance(digest, str) and type(size) is int and size >= 0
                if valid and governed:
                    try:
                        companions.classify_proposed_sibling(target, artifact_sha256=digest, artifact_size=size, companion=data)
                    except companions.CompanionClassificationError:
                        valid = False
                bindings.append((target, digest if isinstance(digest, str) else "", size if type(size) is int else -1, valid))
    except (UnicodeError, RecursionError, vault.FrontmatterError, ValidationError):
        invalid = True
    try:
        fields = _freeze(fields)
    except RecursionError:
        fields = _freeze({})
        invalid = True
    return CapturePageInput(hashlib.sha256(data).hexdigest(), len(data), fields, free, invalid, tuple(bindings))


def classify_capture_inputs(inputs: dict[str, CapturePageInput]) -> tuple[frozenset[str], frozenset[str]]:
    """The canonical companion classifier over publisher-owned original-byte metadata."""
    edges: dict[str, set[str]] = {}
    errors = {path for path, item in inputs.items() if item.invalid}
    for owner, item in inputs.items():
        targets = edges[owner] = set()
        for target, digest, size, valid in item.bindings:
            original = inputs.get(target)
            if not valid or original is None or (original.sha256, original.size) != (digest, size):
                errors.add(owner)
            else:
                targets.add(target)
    pending = set(inputs)
    canonical: set[str] = set()
    while pending:
        incoming = {target for owner in pending for target in edges[owner] if target in pending}
        owners = pending - incoming
        if not owners:
            return frozenset(canonical), frozenset(errors | pending)
        canonical.update(owners)
        originals = {target for owner in owners for target in edges[owner]}
        # Bound originals are bytes; their declarations carry no authority.
        pending.difference_update(owners | originals)
    return frozenset(canonical), frozenset(canonical & errors)


def raw_markdown_files(root: Path, *, subtrees: tuple[str, ...] = ()):
    """Canonical raw-lane membership and alias checks, shared by capture and its publisher."""
    def unavailable(error=None):
        if isinstance(error, FileNotFoundError):
            raise RawEnumerationMoved(
                "SOURCE_IDENTITY_UNAVAILABLE: canonical raw enumeration is incomplete"
            ) from error
        raise SourceCaptureError("SOURCE_IDENTITY_UNAVAILABLE: canonical raw enumeration is incomplete") from error

    roots = tuple(managed_join(lane) for lane in vault.APPEND_ONLY_KB_SUBPATHS)
    selected = set()
    for lane in roots:
        if not subtrees:
            selected.add(lane)
        for prefix in subtrees:
            if prefix == lane or lane.startswith(prefix.rstrip("/") + "/") or not prefix:
                selected.add(lane)
            elif prefix.startswith(lane + "/"):
                selected.add(prefix)
    selected = {path for path in selected if not any(path.startswith(other + "/") for other in selected if other != path)}
    for relative in sorted(selected):
        directory = root / relative
        # Every ancestor is a canonical directory, even for an absent deleted subtree.
        current = root
        missing = False
        for component in Path(relative).parts:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                missing = True
                break
            if stat.S_ISLNK(info.st_mode) or vault._is_reparse(info):
                unavailable()
            if (current != directory or relative in roots) and not stat.S_ISDIR(info.st_mode):
                unavailable()
        if missing:
            continue
        if stat.S_ISREG(info.st_mode):
            if directory.suffix.lower() == ".md":  # Fixed canonical Markdown format.
                yield relative, info
            continue
        if not stat.S_ISDIR(info.st_mode):
            unavailable()
        for folder, directories, files in os.walk(directory, onerror=unavailable, followlinks=False):
            for name in directories:
                try:
                    info = (Path(folder) / name).lstat()
                except FileNotFoundError as error:
                    unavailable(error)
                if stat.S_ISLNK(info.st_mode) or vault._is_reparse(info):
                    unavailable()
            for name in files:
                page = Path(folder) / name
                if page.suffix.lower() != ".md":  # Fixed canonical Markdown format.
                    continue
                try:
                    info = page.lstat()
                except FileNotFoundError as error:
                    unavailable(error)
                if not stat.S_ISREG(info.st_mode) or vault._is_reparse(info):
                    unavailable()
                yield page.relative_to(root).as_posix(), info


def canonical_pages(root: Path, *, join_request: bool = True) -> CanonicalCapturePages:
    """Classify raw-lane pages without trusting declarations from bound originals."""
    from .collection_store import connection
    from .vault_store import mode, read

    snapshots: dict[str, bytes] = {}
    guards: dict[str, vault.PathGuard] = {}
    parsed: dict[str, dict] = {}
    frontmatter_free: set[str] = set()
    errors: set[str] = set()
    store_revision = None

    def unavailable(_error: object = None) -> None:
        raise SourceCaptureError("SOURCE_IDENTITY_UNAVAILABLE: canonical Source lookup is incomplete")

    try:
        if mode.store_mode_for_reads(root):
            with read.snapshot(root, join_request=join_request) as view:
                for lane in vault.APPEND_ONLY_KB_SUBPATHS:
                    prefix = managed_join(f"{lane}/")
                    metadata = {item.path: item for item in view.pages(prefix) if Path(item.path).suffix.lower() == ".md"}
                    bodies = view.bodies(metadata)
                    if bodies.keys() != metadata.keys():
                        unavailable()
                    for relative, data in bodies.items():
                        meta = metadata[relative]
                        if (not isinstance(data, bytes) or len(data) != meta.size
                                or hashlib.sha256(data).hexdigest() != meta.sha256):
                            unavailable()
                    snapshots.update(bodies)
                store_revision = view.revision
        else:
            for relative, info in raw_markdown_files(root):
                try:
                    data, guard = vault.read_bounded_guarded_bytes(root, relative, limit=info.st_size)
                    snapshots[relative] = data
                    guards[relative] = guard
                except (OSError, vault.PathGuardError):
                    errors.add(relative)
        for relative, data in snapshots.items():
            try:
                text = data.decode("utf-8")
                fields, _body, marker = vault.parse_frontmatter(text, strict=True)
                if marker is None:
                    lines = text.removeprefix("\ufeff").splitlines()
                    if lines and lines[0] == "---":  # Fixed YAML frontmatter opener.
                        errors.add(relative)
                    else:
                        frontmatter_free.add(relative)
                else:
                    parsed[relative] = fields
            except (OSError, UnicodeError, RecursionError, vault.PathGuardError, vault.FrontmatterError):
                errors.add(relative)
        inputs = {path: capture_page_input(root, path, data) for path, data in snapshots.items()}
        canonical, incomplete = classify_capture_inputs(inputs)
        for item in inputs.values():
            for target, _digest, _size, valid in item.bindings:
                if valid and target in guards:
                    guards[target].recheck(root)
        # A failed read supplies no classifier input; that omission cannot prove noncanonical membership.
        if incomplete or errors.difference(inputs) or canonical & errors:
            unavailable()
        return CanonicalCapturePages(canonical, snapshots, parsed, frozenset(frontmatter_free), store_revision)
    except (OSError, UnicodeError, sqlite3.Error, connection.CollectionStoreError,
            vault.PathGuardError, vault.FrontmatterError, ValidationError):
        unavailable()


def find_capture(root: Path, identity: SourceIdentity, *, warnings: list[str]) -> tuple[str, str, str] | None:
    """Scan under the caller's mutation guard; derived state cannot prove absence."""
    # Capture holds the mutation guard; its preflight snapshot can predate another capture's commit.
    classified = canonical_pages(root, join_request=False)
    canonical, snapshots, parsed, frontmatter_free = classified.canonical, classified.snapshots, classified.parsed, classified.frontmatter_free
    match: tuple[str, str, str] | None = None
    types = note_types.Basis(root)

    def unavailable(_error: object = None) -> None:
        raise SourceCaptureError("SOURCE_IDENTITY_UNAVAILABLE: canonical Source lookup is incomplete")

    try:
        for page in canonical:
            if page in frontmatter_free:
                lines = snapshots[page].decode("utf-8").splitlines()
                # These headers are the fixed Sources catalog producer format.
                if root / page == indexes.sources_index_path(root):
                    if indexes.SOURCES_BY_TYPE_HEADER in lines and indexes.SOURCES_RECENT_HEADER in lines:
                        continue
                    # Capture updates this reserved path; unknown bytes cannot be overwritten as a catalog.
                    unavailable()
                warnings.append(f"Legacy page has no frontmatter and was skipped during Source identity lookup: {page}; curate its metadata separately")
                continue
            fields = parsed[page]
            if "source_identity" not in fields:
                if "capture_input_digest" in fields:
                    unavailable()
                continue
            if not types.selects(fields.get("type"), note_types.raw):
                unavailable()
            stored_identity = _IDENTITY.validate_python(fields["source_identity"])
            digest = fields.get("capture_input_digest")
            reference = memory_refs.ref_from_markdown(snapshots[page].decode("utf-8"))
            if not isinstance(digest, str) or len(digest) != 64 or reference is None:
                unavailable()
            try:
                if len(bytes.fromhex(digest)) != 32:
                    unavailable()
            except ValueError:
                unavailable()
            if stored_identity == identity:
                if match is not None:
                    unavailable()
                match = (page, reference, digest)
    except (OSError, UnicodeError, vault.PathGuardError, vault.FrontmatterError, ValidationError):
        unavailable()
    return match
