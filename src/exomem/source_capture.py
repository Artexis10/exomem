"""Portable caller identity for canonical text Source captures."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import indexes, memory_refs, note_types, source_taxonomy, vault


class SourceIdentity(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    system: Annotated[str, Field(strict=True, min_length=1)]
    record_id: Annotated[str, Field(strict=True, min_length=1)]
    revision: Annotated[str, Field(strict=True, min_length=1)]


CaptureOutcome = Literal["created", "reused"]
_IDENTITY = TypeAdapter(SourceIdentity)


class SourceCaptureError(ValueError):
    """A coded refusal before a durable capture effect."""


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
                   projects: list[str] | None, slug: str | None, raw_protection: bool) -> str:
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
    return hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def find_capture(root: Path, identity: SourceIdentity, *, warnings: list[str]) -> tuple[str, str, str] | None:
    """Scan under the caller's mutation guard; derived state cannot prove absence."""
    from .client_artifacts import _expected_companion_path
    from .governance import companions, raw_protection
    from .preserve import _sidecar_path

    snapshots: dict[str, bytes] = {}
    parsed: dict[str, dict] = {}
    frontmatter_free: set[str] = set()
    errors: set[str] = set()
    edges: dict[str, set[str]] = {}
    match: tuple[str, str, str] | None = None
    types = note_types.Basis(root)

    def unavailable(_error: object = None) -> None:
        raise SourceCaptureError("SOURCE_IDENTITY_UNAVAILABLE: canonical Source lookup is incomplete")

    try:
        # These are the schema's two canonical raw-material lanes, not vocabulary.
        for lane in vault.APPEND_ONLY_KB_SUBPATHS:
            directory = vault.kb_root(root) / lane
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or vault._is_reparse(info):
                unavailable()
            for folder, directories, files in os.walk(directory, onerror=unavailable, followlinks=False):
                for name in directories:
                    info = (Path(folder) / name).lstat()
                    if stat.S_ISLNK(info.st_mode) or vault._is_reparse(info):
                        unavailable()
                for name in files:
                    page = Path(folder) / name
                    # Markdown is fixed by the canonical page format.
                    if page.suffix.lower() != ".md":
                        continue
                    relative = page.relative_to(root).as_posix()
                    edges[relative] = set()
                    try:
                        data, _guard = vault.read_bounded_guarded_bytes(root, relative, limit=page.lstat().st_size)
                        snapshots[relative] = data
                        text = data.decode("utf-8")
                        fields, _body, marker = vault.parse_frontmatter(text, strict=True)
                        if marker is None:
                            lines = text.removeprefix("\ufeff").splitlines()
                            # The fixed YAML frontmatter opener cannot become catalog prose.
                            if lines and lines[0] == "---":
                                errors.add(relative)
                            else:
                                frontmatter_free.add(relative)
                        else:
                            parsed[relative] = fields
                    except (OSError, UnicodeError, RecursionError, vault.PathGuardError, vault.FrontmatterError):
                        errors.add(relative)

        for owner, fields in parsed.items():
            if not types.selects(fields.get("type"), note_types.raw):
                continue
            declarations = []
            # These fields are the maintained Source companion byte-binding schema.
            if "evidence_file" in fields:
                declarations.append((fields["evidence_file"], fields.get("binary_sha256"), fields.get("binary_size"), False))
            descriptor = fields.get("governance_companion")
            if descriptor is not None:
                if not isinstance(descriptor, dict):
                    errors.add(owner)
                else:
                    declarations.append((descriptor.get("artifact_path"), descriptor.get("artifact_sha256"), descriptor.get("artifact_size"), True))
            for target, digest, size, governed in declarations:
                if not isinstance(target, str):
                    errors.add(owner)
                    continue
                # Non-Markdown originals cannot impersonate a candidate page.
                if Path(target).suffix.lower() != ".md":
                    continue
                # The Source and Evidence producers define these companion layouts.
                source_owner = _expected_companion_path(PurePosixPath(target), lane="source").as_posix()
                evidence_owner = _sidecar_path(Path(target), protected=raw_protection.marked(target)).as_posix()
                if owner != source_owner and owner != evidence_owner:
                    errors.add(owner)
                    continue
                if (target not in edges or not isinstance(digest, str)
                        or type(size) is not int or size < 0):
                    errors.add(owner)
                    continue
                try:
                    data, _guard = vault.read_bounded_guarded_bytes(root, target, limit=size, expected_hash=digest)
                    if len(data) != size or data != snapshots.get(target):
                        errors.add(owner)
                        continue
                    if governed:
                        companions.classify_proposed_sibling(target, artifact_sha256=digest,
                                                            artifact_size=size, companion=snapshots[owner])
                except (OSError, vault.PathGuardError, companions.CompanionClassificationError):
                    errors.add(owner)
                    continue
                edges[owner].add(target)

        pending = set(edges)
        canonical: set[str] = set()
        while pending:
            incoming = {target for owner in pending for target in edges[owner] if target in pending}
            owners = pending - incoming
            if not owners:
                unavailable()
            canonical.update(owners)
            originals = {target for owner in owners for target in edges[owner]}
            # Bound originals are bytes, so their outgoing declarations have no authority.
            pending.difference_update(owners | originals)

        if canonical & errors:
            unavailable()
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
