"""Bounded ASiC-E inspection, exact payload custody and portable parent bindings."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import BinaryIO

import yaml

from . import archive_members, originals, reserved_paths, state_paths, vault
from .vault import (
    MISSING_CONTENT_HASH,
    FrontmatterError,
    PathGuard,
    PlannedWrite,
    PreparedBinaryContent,
    content_hash,
    parse_frontmatter,
    read_frontmatter_header,
)

MAX_EXPANDED_BYTES = 256 * 1024**2
MAX_ENTRIES = 1024
MAX_PAYLOADS = 64  # Each payload holds one spool until the atomic custody batch ends.
_MIMETYPE = b"application/vnd.etsi.asic-e+zip"
# This adapter's supported file formats are fixed by its custody/extraction contract.
_PAYLOAD_SUFFIXES = (".docx", ".pdf")
# These fields define the version 1 portable container-member protocol.
_BINDING_FIELDS = frozenset((
    "version", "parent_path", "parent_page", "parent_sha256", "parent_size", "member_path",
    "artifact_path", "artifact_sha256", "artifact_size",
))
_OVERLAY: ContextVar[Mapping[str, str | PreparedBinaryContent]] = ContextVar("container_overlay", default=MappingProxyType({}))


@dataclass
class Member:
    path: str
    stream: BinaryIO
    sha256: str
    size: int


@dataclass
class Inspection:
    state: str = "inspected"
    members: list[Member] = field(default_factory=list)
    code: str | None = None
    reason: str | None = None
    unsupported: list[str] = field(default_factory=list)

    def metadata(self) -> dict:
        result = {
            "version": 1,
            "format": "asic-e",
            "inspection_state": self.state,
            "signature_verification": "not_verified",
            "supported_payload_count": len(self.members) if self.state == "inspected" else None,
            "next_action": (
                "Process the preserved document payloads with media process; signatures need an external verifier."
                if self.state == "inspected" else
                "Download the unchanged container and inspect or convert its documents with a compatible local tool."
            ),
        }
        if self.code:
            result["code"] = self.code
        if self.reason:
            result["reason"] = self.reason
        if self.unsupported:
            result["unsupported_members"] = self.unsupported
        return result


def is_container(path: str | Path) -> bool:
    return Path(path).suffix.casefold() == ".asice"


@contextmanager
def inspect(stream: BinaryIO) -> Iterator[Inspection]:
    """Inspect all members before returning any payload; never interpret signature XML."""
    position = stream.tell()
    result = Inspection()
    with ExitStack() as held:
        try:
            with archive_members.open_zip(stream, max_entries=MAX_ENTRIES) as archive:
                entries = archive_members.central_directory(archive, max_entries=MAX_ENTRIES, portable_paths=True)
                if sum(entry.info.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                    raise archive_members.ArchiveError(archive_members.TOO_LARGE, "expanded container exceeds the aggregate byte limit")
                total = 0
                mimetype = None
                for entry in entries:
                    suffix = PurePosixPath(entry.path).suffix.casefold()
                    metadata = entry.path == "mimetype" or entry.path.startswith("META-INF/")
                    supported = not metadata and suffix in _PAYLOAD_SUFFIXES
                    if supported and len(result.members) >= MAX_PAYLOADS:
                        raise archive_members.ArchiveError(archive_members.TOO_LARGE, "the container exceeds the payload count limit")
                    target = held.enter_context(tempfile.TemporaryFile(mode="w+b")) if supported else None
                    digest = hashlib.sha256()
                    size = 0
                    marker = bytearray()
                    for chunk in archive_members.member_chunks(archive, entry):
                        total += len(chunk)
                        size += len(chunk)
                        if total > MAX_EXPANDED_BYTES:
                            raise archive_members.ArchiveError(archive_members.TOO_LARGE, "expanded container exceeds the streaming byte limit")
                        digest.update(chunk)
                        if target is not None:
                            target.write(chunk)
                        if entry.path == "mimetype" and len(marker) <= len(_MIMETYPE):
                            marker.extend(chunk[:len(_MIMETYPE) + 1])
                    if entry.path == "mimetype":
                        mimetype = bytes(marker)
                    elif not metadata:
                        if target is None:
                            result.unsupported.append(entry.info.filename)
                        else:
                            target.seek(0)
                            result.members.append(Member(entry.info.filename, target, digest.hexdigest(), size))
                if mimetype != _MIMETYPE:
                    raise archive_members.ArchiveError(archive_members.INVALID, "ASiC-E requires its exact mimetype entry")
                if result.unsupported or not result.members:
                    result.state = "unsupported"
                    result.reason = "Only DOCX and PDF payloads are supported; the unchanged container remains available."
                    result.members.clear()
        except archive_members.ArchiveError as error:
            result.state, result.code, result.reason = "failed", error.code, error.reason
            result.members.clear()
        except OSError:
            result.state = "failed"
            result.code = "CONTAINER_INSPECTION_UNAVAILABLE"
            result.reason = "Temporary inspection storage is unavailable; free local resources, then run media process again."
            result.members.clear()
        finally:
            stream.seek(position)
        if result.state != "inspected":
            held.close()  # Opaque custody must not compete with payload spools after failed or unsupported inspection.
        yield result


def in_payload_tree(path: str) -> bool:
    """The version 1 custody layout reserves this ASiC-E child subtree."""
    # The reserved layout follows generic NFKC/separator spelling even when the full candidate is invalid.
    spelling = unicodedata.normalize("NFKC", path).replace("\\", "/")
    return any(part.casefold().endswith(".asice.documents") for part in PurePosixPath(spelling).parts)


def child_path(parent: str, member: str) -> str:
    """Member names remain provenance; their hashes name collision-free custody paths."""
    return f"{parent}.documents/{hashlib.sha256(member.encode('utf-8')).hexdigest()}{PurePosixPath(member).suffix.casefold()}"


def annotate(markdown: str, field_name: str, value: Mapping) -> str:
    return markdown.replace("---\n", "---\n" + yaml.safe_dump({field_name: dict(value)}, sort_keys=False, allow_unicode=True), 1)


@dataclass
class Plan:
    inspection: Inspection
    writes: list[PlannedWrite]
    children: list[str]
    guards: list[PathGuard]

    def metadata(self) -> dict:
        return {**self.inspection.metadata(), "payloads": list(self.children)}


@contextmanager
def plan(
    vault_root: Path, *, parent: str, parent_page: str, digest: str, size: int,
    stream: BinaryIO, date_iso: str, existing: bool = False,
) -> Iterator[Plan]:
    """Plan child pairs once for initial custody and explicit, guarded recovery."""
    from . import media_types, preserve
    from .governance import raw_protection

    with inspect(stream) as inspection:
        result = Plan(inspection, [], [], [])
        tree, scope, category = preserve.artifact_location(parent)
        for member in inspection.members:
            artifact = child_path(parent, member.path)
            page = artifact + ".md"
            binding = {
                "version": 1, "parent_path": parent, "parent_page": parent_page,
                "parent_sha256": digest, "parent_size": size, "member_path": member.path,
                "artifact_path": artifact, "artifact_sha256": member.sha256, "artifact_size": member.size,
            }
            rendered = preserve._render_sidecar(
                artifact_name=Path(artifact).name, scope=scope, category=category, tree=tree,
                date_iso=date_iso, media_type=media_types.media_type_for(artifact),
                evidence_file=artifact, extracted_by="pending", binary_sha256=member.sha256,
                binary_size=member.size, governance_artifact_path=artifact,
                governance_artifact_sha256=member.sha256, governance_artifact_size=member.size,
            )
            rendered = annotate(rendered, "container_member", binding)
            if raw_protection.marked(parent):
                rendered = raw_protection.protect(rendered, artifact_path=artifact, digest=member.sha256)
            binary_path = vault_root / artifact
            page_path = vault_root / page
            if existing and binary_path.exists():
                _require_identity(vault_root, artifact, member.sha256, member.size)
                result.guards.append(PathGuard.capture(vault_root, artifact, leaf_policy="content", expected_content_hash=member.sha256))
            else:
                result.writes.append(PlannedWrite(binary_path, PreparedBinaryContent(member.stream, member.size, member.sha256), create_only=True, expected_hash=MISSING_CONTENT_HASH))
            if existing and page_path.exists():
                before = reserved_paths.read_generic_bytes(vault_root, page).data.decode("utf-8")
                fm = parse_frontmatter(before, strict=True)[0]
                result.guards.append(PathGuard.capture(vault_root, page, leaf_policy="content", expected_content_hash=content_hash(before)))
                previous = fm.get("container_member")
                if previous is not None and previous != binding:
                    raise ValueError("CONTAINER_CONFLICT: existing payload has different parent provenance")
                if originals.binary_binding(fm) != originals.BinaryBinding(artifact, member.sha256, member.size):
                    # Some historical pending companions omit binary identity; their exact governance descriptor remains authoritative.
                    descriptor = fm.get("governance_companion", {})
                    if not isinstance(descriptor, dict) or any(descriptor.get(key) != value for key, value in (
                        ("artifact_path", artifact), ("artifact_sha256", member.sha256), ("artifact_size", member.size),
                    )):
                        raise ValueError("CONTAINER_CONFLICT: existing payload companion has different bytes")
                if previous is None:
                    result.writes.append(PlannedWrite(page_path, annotate(before, "container_member", binding), expected_hash=content_hash(before)))
            else:
                result.writes.append(PlannedWrite(page_path, rendered, create_only=True, expected_hash=MISSING_CONTENT_HASH))
            result.children.append(artifact)
        yield result


@contextmanager
def planned_scope(vault_root: Path, writes) -> Iterator[None]:
    """Make staged parents available to catalog membership before atomic publication."""
    overlay = {write.path.relative_to(vault_root).as_posix(): write.content for write in writes}
    token = _OVERLAY.set(overlay)
    try:
        yield
    finally:
        _OVERLAY.reset(token)


def _require_identity(root: Path, path: str, digest: str, size: int, *, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> originals.BinaryBinding:
    root = state_paths.resolved_vault_path(root, expanduser=False)
    staged = _OVERLAY.get().get(path)
    if isinstance(staged, PreparedBinaryContent):
        actual = staged.sha256, staged.size
    elif isinstance(staged, str):
        encoded = staged.encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest(), len(encoded)
    else:
        from .governance import companions

        try:
            proof = companions.prove_original(root, path, artifact_copy=artifact_copy)
            actual = proof.sha256, proof.size
        except reserved_paths.ReservedPathLeafError:
            stored = originals.original_state(root, path)
            if stored.state != "evicted" or stored.reference is None:
                raise ValueError("container original is missing or unsafe") from None
            actual = stored.binary_sha256, stored.binary_size
    if actual != (digest, size):
        raise ValueError("container original identity does not match its provenance")
    return originals.BinaryBinding(path, actual[0], actual[1])


def _canonical_path(value: object) -> str:
    if not isinstance(value, str) or PurePosixPath(value).is_absolute() or "\\" in value or PureWindowsPath(value).drive:
        raise ValueError("container binding requires a relative path")
    if not value or str(PurePosixPath(value)) != value or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("container binding has an unsafe path")
    return value


def _parent_binding_for_page(root: Path, path: str, frontmatter: Mapping, *, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> originals.BinaryBinding | None:
    """Validate a child binding against location, parent companion and current byte identities."""
    root = state_paths.resolved_vault_path(root, expanduser=False)
    binding = frontmatter.get("container_member")
    # The reserved subtree cannot become public by deleting the provenance block.
    in_subtree = in_payload_tree(path)
    if "container_member" not in frontmatter and not in_subtree:
        return None
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS or type(binding["version"]) is not int or binding["version"] != 1:
        raise ValueError("container member provenance is missing or invalid")
    parent = _canonical_path(binding["parent_path"])
    parent_page = _canonical_path(binding["parent_page"])
    artifact = _canonical_path(binding["artifact_path"])
    member = binding["member_path"]
    try:
        normalized_member = archive_members.validated_member(member)
    except archive_members.ArchiveError as error:
        raise ValueError("container member path is invalid") from error
    if normalized_member != member or not is_container(parent) or in_payload_tree(parent) or PurePosixPath(member).suffix.casefold() not in _PAYLOAD_SUFFIXES:
        raise ValueError("container member path is invalid")
    expected_page = originals.sidecar_path(root / parent).relative_to(root).as_posix()
    if artifact != child_path(parent, member) or path != artifact + ".md" or parent_page != expected_page:
        raise ValueError("container member location is invalid")
    for key in ("parent_sha256", "artifact_sha256"):
        if not originals.is_sha256(binding[key]):
            raise ValueError("container member digest is invalid")
    for key in ("parent_size", "artifact_size"):
        if type(binding[key]) is not int or binding[key] < 0:
            raise ValueError("container member size is invalid")
    staged_parent = _OVERLAY.get().get(parent_page)
    parent_text = staged_parent if isinstance(staged_parent, str) else reserved_paths.read_generic_bytes(root, parent_page).data.decode("utf-8")
    parent_fm = parse_frontmatter(parent_text, strict=True)[0]
    if parent_fm.get("evidence_file") != parent:
        descriptor = parent_fm.get("governance_companion", {})
        if not isinstance(descriptor, dict) or descriptor.get("artifact_path") != parent:
            raise ValueError("container parent companion names different bytes")
    parent_binding = _require_identity(root, parent, binding["parent_sha256"], binding["parent_size"])
    _require_identity(root, artifact, binding["artifact_sha256"], binding["artifact_size"], artifact_copy=artifact_copy)
    return parent_binding


def parent_for_page(root: Path, path: str, frontmatter: Mapping, *, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> tuple[str, str] | None:
    """Return both current parent paths after validating the member binding."""
    binding = _parent_binding_for_page(root, path, frontmatter, artifact_copy=artifact_copy)
    return None if binding is None else (binding.path, originals.sidecar_path(Path(binding.path)).as_posix())


# Remembers ordinary notes that were read and carry no container claim, so a restricted path
# decision need not re-read and re-parse the note each time. It stores no bytes, only the file
# version it saw, and a hit re-checks that version with one lstat. Bounded; `clear_unclaimed_notes`
# empties it.
_UNCLAIMED_MAX = 65_536
_UNCLAIMED_LOCK = threading.Lock()
_UNCLAIMED: OrderedDict[tuple[Path, str], tuple[str, tuple[int, int], tuple[int, int, int]]] = OrderedDict()


def clear_unclaimed_notes() -> int:
    """Forget every remembered note; the idle RAM release calls this. Returns how many entries it dropped."""
    with _UNCLAIMED_LOCK:
        dropped = len(_UNCLAIMED)
        _UNCLAIMED.clear()
    return dropped


def _page_version(page: str) -> tuple[tuple[int, int], tuple[int, int, int], os.stat_result] | None:
    """Identity and stat generation of one regular, singly linked page at an absolute path, or None."""
    try:
        info = os.lstat(page)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    return (info.st_dev, info.st_ino), vault.stat_generation(info), info


def _remembered_unclaimed(root: Path, path: str) -> bool:
    key = (root, path)
    with _UNCLAIMED_LOCK:
        entry = _UNCLAIMED.get(key)
        if entry is None:
            return False
        _UNCLAIMED.move_to_end(key)
    version = _page_version(entry[0])
    if version is None or version[:2] != entry[1:]:
        # A changed note is read again; forget it so later checks do not stat a version that cannot match.
        with _UNCLAIMED_LOCK:
            if _UNCLAIMED.get(key) == entry:
                del _UNCLAIMED[key]
        return False
    return True


def _remember_unclaimed(
    root: Path, path: str, page: str, read: reserved_paths.GenericFileSnapshot, started_ns: int,
) -> None:
    # The read's generation comes from its own held handle after the bytes were read. It binds those bytes
    # only when both timestamps were already older than the racy window when the read started: a write during
    # the read, however long the read takes, carries a later timestamp, and a same-tick rewrite keeps
    # (size, mtime, ctime). Recording costs no stat; the next hit's lstat proves the path still names this
    # inode and version. A claim, an in-tree path or a missing file is never remembered, so the memo can only
    # skip a read that found no claim; it never feeds a byte observation.
    if read.generation is None or read.identity.kind != "file" or read.identity.link_count != 1:
        return
    if started_ns - max(read.generation[1], read.generation[2]) <= vault.GENERATION_RACY_WINDOW_NS:
        return
    with _UNCLAIMED_LOCK:
        _UNCLAIMED[(root, path)] = (page, (read.identity.device, read.identity.inode), read.generation)
        _UNCLAIMED.move_to_end((root, path))
        while len(_UNCLAIMED) > _UNCLAIMED_MAX:
            _UNCLAIMED.popitem(last=False)


def parent_binding_for_path(root: Path, path: str, *, content: bytes | None = None, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> originals.BinaryBinding | None:
    """Resolve direct pages, binary originals and extraction parts through one binding validator."""
    if not isinstance(path, str):
        raise ValueError("container member path must be a string")
    root = state_paths.resolved_vault_path(root, expanduser=False)
    # Only an ORDINARY spelling outside a payload tree is ever remembered. Its classification depends on the
    # spelling and the process's fixed knowledge-base directory, so a hit needs none of the normalization below.
    # A hit proves only that this file version makes no claim; the read that serves the page re-proves path
    # safety. Supplied or staged bytes decide instead.
    if content is None and not _OVERLAY.get() and _remembered_unclaimed(root, path):
        return None
    spelling = path
    page_path = path if path.lower().endswith(".md") else path + ".md"
    staged = _OVERLAY.get().get(page_path)
    raw = content if page_path == path and content is not None else (staged.encode("utf-8") if isinstance(staged, str) else None)
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    classified = reserved_paths.classify_logical(path)
    if classified.disposition is reserved_paths.PathDisposition.ORDINARY:
        parent, leaf = reserved_paths._leaf_spelling(path)
        path = PurePosixPath(parent, leaf).as_posix()
        classified = reserved_paths.classify_logical(path)
    page_path = path if path.lower().endswith(".md") else path + ".md"
    staged = _OVERLAY.get().get(page_path)
    if raw is None:
        raw = content if page_path == path and content is not None else (staged.encode("utf-8") if isinstance(staged, str) else None)
    in_subtree = in_payload_tree(path)
    remembered_read = None
    if raw is None:
        if classified.disposition is not reserved_paths.PathDisposition.ORDINARY:
            if in_subtree:
                raise ValueError("container member path is invalid")
            return None
        # Reaching here means no supplied bytes and no staged overlay, so this is a disk read.
        memoable = content is None and not _OVERLAY.get() and not in_subtree and vault.STAT_GENERATION_TRUSTED
        # A cold inventory cannot classify an absent ordinary companion.
        if not in_subtree and not reserved_paths.identity_catalogue_ready(root):
            try:
                (root / page_path).lstat()
            except FileNotFoundError:
                return None
        try:
            started_ns = time.time_ns()
            read = reserved_paths.read_generic_bytes(root, page_path)
            raw = read.data
            remembered_read = (read, started_ns) if memoable else None
        except (reserved_paths.ReservedPathLeafError, RuntimeError):
            # Inventory loss can precede a typed read error; only physical absence clears an ordinary claim.
            if not in_subtree:
                try:
                    (root / page_path).lstat()
                except FileNotFoundError:
                    return None
            raise
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        if in_subtree:
            raise
        # An undecodable body cannot erase a valid provenance header.
        try:
            decoded = read_frontmatter_header(io.BytesIO(raw), max(8, len(raw))).decode("utf-8")
        except (UnicodeDecodeError, FrontmatterError):
            return None
    fm, _, _ = parse_frontmatter(decoded, strict=in_subtree)
    if not in_subtree:
        # Ordinary malformed notes keep path-only handling; a parsed custody claim requires strict validation.
        if "container_member" not in fm:
            if remembered_read is not None:
                _remember_unclaimed(root, spelling, str(root / page_path), *remembered_read)
            return None
        fm, _, _ = parse_frontmatter(decoded, strict=True)
    if "part_of" in fm:
        from . import extraction_parts
        source_path = extraction_parts.source_page_for(Path(page_path))
        # Parts are one level below their payload page; authored links cannot redirect custody.
        if source_path is None or extraction_parts.part_number(Path(page_path)) is None:
            raise ValueError("container extraction part has an invalid source")
        page_path = source_path.as_posix()
        fm = parse_frontmatter(reserved_paths.read_generic_bytes(root, page_path).data.decode("utf-8"), strict=True)[0]
    return _parent_binding_for_page(root, page_path, fm, artifact_copy=artifact_copy)


def parent_for_path(root: Path, path: str, *, content: bytes | None = None, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> tuple[str, str] | None:
    """Return both current parent paths after resolving one member binding."""
    binding = parent_binding_for_path(root, path, content=content, artifact_copy=artifact_copy)
    return None if binding is None else (binding.path, originals.sidecar_path(Path(binding.path)).as_posix())



def staged_page(root: Path, path: str):
    """Read one staged Markdown parent without requiring its future inode."""
    from . import find_corpus
    staged = _OVERLAY.get().get(path)
    if not isinstance(staged, str):
        return None
    return find_corpus.parse_page(root / path, 0, root, content=staged.encode("utf-8"))
