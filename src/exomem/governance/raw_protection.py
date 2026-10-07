"""Intrinsic release floor for explicitly protected immutable artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import yaml

from .. import memory_refs, reserved_paths, vault
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    store,
)
from .principal import HOSTED_GATEWAY_ISSUER_FAMILY, OWNER_AUDIENCE, RequestPrincipal

PREFIX = "__exomem_raw_v1__"
COMPATIBILITY_ID = "raw-protection-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def marked(path: str) -> bool:
    """Recognize the copy-stable floor without reading any file or registry."""
    return any(part.casefold().startswith(PREFIX) for part in unquote(path).replace("\\", "/").split("/"))


def required_compatibility(root: Path) -> frozenset[str]:
    """Bounded launch-only name probe; missing companions/state do not erase the floor."""
    pending = [Path(root)]
    visited = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if marked(entry.name) or visited > 250_000:
                        return frozenset({COMPATIBILITY_ID})
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except FileNotFoundError:
            continue
        except OSError:
            # A reader unable to prove absence must understand the format.
            return frozenset({COMPATIBILITY_ID})
    return frozenset()


def is_owner(who: RequestPrincipal) -> bool:
    """The owner on any surface: local, a remote connector, the REST key or the
    transfer bearer. Every other audience needs a whole-artifact release."""
    return who.resolved and who.audience_id == OWNER_AUDIENCE


def applies_to(who: RequestPrincipal) -> bool:
    """Whether RAW governs `who`: it withholds from it and accepts its RAW captures.

    A hosted cell has no owner binding, so RAW there would protect the tenant's data from the tenant.
    """
    return not (
        who.resolved and who.surface == "hosted" and who.issuer_family == HOSTED_GATEWAY_ISSUER_FAMILY
    )


#: Why a surface that cannot identify the vault's owner refuses a RAW capture,
#: and what the person can do instead.
UNAVAILABLE_MESSAGE = "this cell cannot identify its owner, so it cannot keep an original owner-only"
UNAVAILABLE_REMEDIATION = (
    "Capture it without raw protection, or from a surface that identifies the vault's owner."
)
UNAVAILABLE_REASON = f"{UNAVAILABLE_MESSAGE}. {UNAVAILABLE_REMEDIATION}"


def protect(page: str, *, artifact_path: str, digest: str) -> str:
    """Bind the companion's existing identity to its immutable original."""
    block = {
        "version": 1,
        "original_ref": memory_refs.ref_from_markdown(page),
        "artifact_path": artifact_path,
        "artifact_sha256": digest,
        "revision": uuid.uuid4().hex,
    }
    return page.replace("---\n", "---\n" + yaml.safe_dump({"raw_protection": block}, sort_keys=False), 1)


def binding(root: Path, path: str) -> tuple[dict, str, str] | None:
    """Resolve only the named pair; an orphan never acquires a release."""
    candidates = [path] if path.endswith(".md") else []
    candidates.append(path + ".md")
    if path.endswith(".md"):
        candidates.append(path[:-3] + "-notes.md")
    for candidate in candidates:
        try:
            raw = reserved_paths.read_generic_bytes(root, candidate).data
            fm, _, _ = vault.parse_frontmatter(raw.decode("utf-8"), strict=True)
            block = fm.get("raw_protection")
            if not isinstance(block, dict) or set(block) != {
                "version", "original_ref", "artifact_path", "artifact_sha256", "revision"
            }:
                continue
            original = block["artifact_path"]
            if (type(block["version"]) is not int or block["version"] != 1
                    or not isinstance(original, str) or not marked(original)
                    or path not in {original, candidate}
                    or memory_refs.ref_from_markdown(raw.decode("utf-8")) != block["original_ref"]
                    or memory_refs.parse_memory_ref(block["original_ref"]) is None
                    or not isinstance(block["artifact_sha256"], str)
                    or not _DIGEST.fullmatch(block["artifact_sha256"])
                    or not isinstance(block["revision"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", block["revision"])):
                continue
            # The companion must be beside the bytes, not a foreign identity.
            if candidate not in {original + ".md", original.removesuffix(".md") + "-notes.md"}:
                continue
            return block, candidate, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, vault.FrontmatterError,
                reserved_paths.ReservedPathLeafError):
            continue
    return None


def permits(
    root: Path, path: str, who: RequestPrincipal, *,
    snapshot: bytes | None = None, derived: bool = False,
) -> bool:
    """Meet with ordinary policy; never let a caller-declared purpose widen RAW."""
    if not marked(path) or not applies_to(who):
        return True
    if not who.resolved or not who.issuer_family:
        return False
    trusted_purpose = None
    context = who.verified_authorization_session
    if who.authorization_session_id is not None and context is None:
        return False
    if context is not None:
        connection = None
        try:
            if context.issuer_family != who.issuer_family:
                return False
            connection = store.open_authorization_session_connection(root)
            now = int(time.time())
            custody = authorization_custody.load_authorization_custody(root, now=now)
            authorization_session_lifecycle.status_verified_session(
                connection, custody=custody, context=context, now=now,
            )
            trusted_purpose = authorization_session_authority.active_session_purpose(
                connection, context=context, audience=who.audience_id, now=now,
            )
        except (OSError, sqlite3.Error, store.UnsupportedGovernanceSchema,
                authorization_custody.AuthorizationCustodyUnavailable,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            return False
        finally:
            if connection is not None:
                connection.close()
    if is_owner(who):
        return True
    # An original's release never approves newly extracted pixels or aliases.
    if derived:
        return False
    from . import policy

    current = policy.load(root)
    if current.blocked or current.conflicted:
        return False
    grants = [grant for grant in current.release_grants
              if grant.raw_protection is not None and grant.to_audience == who.audience_id]
    if not grants:
        return False
    found = binding(root, path)
    if found is None:
        return False
    block, companion, companion_hash = found
    for grant in grants:
        spec = grant.raw_protection
        if (grant.ref != block["original_ref"] or grant.path != companion
                or grant.content_hash != companion_hash
                or spec["artifact_sha256"] != block["artifact_sha256"]
                or spec["revision"] != block["revision"]
                or spec["surface"] != who.surface or spec["issuer_family"] != who.issuer_family
                or spec["purpose"] != trusted_purpose):
            continue
        try:
            original = reserved_paths.read_generic_bytes(root, block["artifact_path"]).data
            if hashlib.sha256(original).hexdigest() != block["artifact_sha256"]:
                return False
            if snapshot is not None:
                expected = block["artifact_sha256"] if path == block["artifact_path"] else companion_hash
                if hashlib.sha256(snapshot).hexdigest() != expected:
                    return False
        except (OSError, reserved_paths.ReservedPathLeafError):
            return False
        return True
    return False
