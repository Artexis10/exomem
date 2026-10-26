"""Recover canonical originals through the existing custody and disclosure owners."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from . import request_budget, reserved_paths, source_corrections, upload_tokens, vault
from .governance import companions, connector_boundary, egress, policy
from .governance.principal import effective_principal, request_scope

RESOURCE_PREFIX = "exomem://original/"


@dataclass(slots=True, repr=False)
class _NativeGrantState:
    """Middleware-owned custody and completion for one native resource request."""

    grant: str | None
    completed: bool = False


_NATIVE_GRANT: ContextVar[_NativeGrantState | None] = ContextVar("exomem_native_original_grant", default=None)
# Two simultaneous private spools bound process disk use; oversized files use a configurable byte budget.
_SLOTS = threading.BoundedSemaphore(2)
# This matches only the fixed signed protocol syntax, never a user's vocabulary or intent.
_CAPABILITY = re.compile(r"v4\.[0-9]{1,12}\.[0-9a-f]{1,32768}\.[0-9a-f]{64}")


class RecoveryUnavailable(ValueError):
    """Missing, changed and withheld originals share one content-free refusal."""

    def __init__(self):
        super().__init__(
            "ORIGINAL_UNAVAILABLE: original recovery is unavailable; retry or request a fresh handoff"
        )


class _CapabilityRedaction(logging.Filter):
    def filter(self, record):
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            if isinstance(target, str):
                try:
                    # ASGI query strings retain '#' as query data rather than a browser fragment.
                    parsed = urlsplit(target, allow_fragments=False)
                    if any(key == "grant" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
                        target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "grant=artifact-capability-removed", ""))
                except ValueError:
                    target = "artifact-request-target-removed"
                # Uvicorn's AccessFormatter unpacks these five arguments positionally.
                record.args = tuple(
                    _CAPABILITY.sub("artifact-capability-removed", item) if isinstance(item, str) else item
                    for item in (client, method, target, version, status)
                )
                return True
        record.msg = _CAPABILITY.sub("artifact-capability-removed", record.getMessage())
        record.args = ()
        return True


REDACTED_LOGGERS = ("fastmcp.server.server", "uvicorn.access", "uvicorn.error")


def install_log_redaction() -> None:
    """FastMCP errors and Uvicorn access records must not retain delegated authority."""
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _CapabilityRedaction) for item in logger.filters):
            logger.addFilter(_CapabilityRedaction())


def signing_root() -> str | None:
    return upload_tokens.private_signing_root(
        os.environ.get("EXOMEM_UPLOAD_TOKEN"), os.environ.get("EXOMEM_JWT_SIGNING_KEY")
    )


def deployment(root: Path) -> str:
    # A grant cannot cross cells or another installation sharing signing configuration.
    binding = str(root.resolve()) + "\0" + os.environ.get("EXOMEM_BASE_URL", "").rstrip("/")
    return hashlib.sha256(binding.encode()).hexdigest()


def native_max_bytes() -> int:
    # Base64 and JSON expand this payload; keep the default response comfortably below MCP's ingress budget.
    return max(0, int(os.environ.get("EXOMEM_RECOVERY_NATIVE_MAX_BYTES", str(4 * 1024 * 1024))))


def _companion(root: Path, path: str) -> tuple[str, dict]:
    from .governance import raw_protection
    from .preserve import _sidecar_paths

    source = Path(path).is_relative_to(vault.kb_root(root).relative_to(root) / "Sources")
    for candidate in _sidecar_paths(Path(path), protected=raw_protection.marked(path), source=source):
        companion = candidate.as_posix()
        try:
            raw = reserved_paths.read_generic_bytes(root, companion).data
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            continue
        frontmatter, _, marker = vault.parse_frontmatter(raw.decode("utf-8"), strict=True)
        if marker is None:
            raise RecoveryUnavailable
        return companion, frontmatter
    raise reserved_paths.ReservedPathLeafError("MISSING")


def _resolve(root: Path, selected: str) -> tuple[str, dict]:
    path = reserved_paths.resolve_physical_relative(root, selected)
    try:
        _, metadata = _companion(root, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING" or not path.lower().endswith(".md"):
            raise RecoveryUnavailable from None
        try:
            path = source_corrections.resolve_current(root, path).selected_path
        except source_corrections.SourceCorrectionUnavailable:
            raise RecoveryUnavailable from None
        raw = reserved_paths.read_generic_bytes(root, path).data
        metadata, _, marker = vault.parse_frontmatter(raw.decode("utf-8"), strict=True)
        descriptor = metadata.get("governance_companion", {})
        artifact = descriptor.get("artifact_path") if isinstance(descriptor, dict) else None
        artifact = artifact or metadata.get("data_file") or metadata.get("evidence_file")
        if marker is None or not isinstance(artifact, str):
            raise RecoveryUnavailable from None
        artifact = reserved_paths.resolve_physical_relative(root, artifact)
        companion, metadata = _companion(root, artifact)
        if companion != path:
            raise RecoveryUnavailable from None
        path = artifact
    return path, metadata


def _descriptor(path: str, metadata: dict, copied: reserved_paths.GenericFileCopy) -> dict:
    binding = metadata.get("governance_companion")
    if isinstance(binding, dict):
        if (
            binding.get("artifact_path") != path
            or binding.get("artifact_sha256") != copied.sha256
            or binding.get("artifact_size") != copied.size
        ):
            raise RecoveryUnavailable
    elif metadata.get("data_file") != path and metadata.get("evidence_file") != path:
        raise RecoveryUnavailable
    if metadata.get("binary_sha256") != copied.sha256 or metadata.get("binary_size") != copied.size:
        raise RecoveryUnavailable
    name = metadata.get("original_filename")
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or any(ord(c) < 32 for c in name)
    ):
        raise RecoveryUnavailable
    # Older custody pages stored MIME only in transcription/adoption provenance. Keep inference explicit.
    provenance = metadata.get("transcription") or metadata.get("adoption") or {}
    mime = provenance.get("content_type") if isinstance(provenance, dict) else None
    source = "stored" if isinstance(mime, str) and mime else "filename"
    mime = mime if source == "stored" else mimetypes.guess_type(name)[0]
    if not mime:
        mime, source = "application/octet-stream", "fallback"
    if not isinstance(mime, str) or any(ord(c) < 32 or ord(c) > 126 for c in mime):
        raise RecoveryUnavailable
    return {
        "path": path,
        "sha256": copied.sha256,
        "size": copied.size,
        "file_name": name,
        "mime_type": mime,
        "mime_type_source": source,
    }


def _admission_revision(root: Path, who, copied: reserved_paths.GenericFileCopy) -> tuple:
    try:
        companion, _ = _companion(root, copied.relative_path)
        snapshot = reserved_paths.read_generic_bytes(root, companion)
        source_version = reserved_paths.require_generic_copy_source_version(copied)
        compiled = policy.load(root)
        return (
            compiled.fingerprint,
            compiled.blocked,
            compiled.conflicted,
            connector_boundary.cache_identity(root, who, compiled),
            snapshot.identity,
            hashlib.sha256(snapshot.data).hexdigest(),
            source_version,
        )
    except (
        reserved_paths.ReservedPathLeafError,
        OSError,
        UnicodeError,
        vault.FrontmatterError,
        connector_boundary.BoundaryUnavailable,
    ) as error:
        raise RecoveryUnavailable from error


def _release(root: Path, copied: reserved_paths.GenericFileCopy, who, *, byte_proofs: dict,
             allow_capture: bool) -> tuple:
    def fresh():
        for proof in byte_proofs.values():
            reserved_paths.require_generic_copy_source_version(proof)
        before = _admission_revision(root, who, copied)
        with request_scope(who), companions.byte_proof_scope(byte_proofs, allow_capture=allow_capture):
            allowed = egress.release_allows_download(
                root, copied.relative_path, principal=who, artifact_copy=copied
            )
        after = _admission_revision(root, who, copied)
        for proof in byte_proofs.values():
            reserved_paths.require_generic_copy_source_version(proof)
        if not allowed or before != after:
            raise RecoveryUnavailable
        return after

    # Nested resolution scopes share a memo. An empty context forces this final pass to read live host authority.
    try:
        return Context().run(fresh)
    except reserved_paths.ReservedPathLeafError as error:
        raise RecoveryUnavailable from error


def require_revision(
    root: Path, who, copied: reserved_paths.GenericFileCopy, expected: tuple, *, byte_proofs: dict
) -> None:
    current = Context().run(_admission_revision, root, who, copied)
    if current != expected:
        raise RecoveryUnavailable
    # Run full release after the asynchronous origin check: grant expiry and purpose are live authority too.
    if _release(root, copied, who, byte_proofs=byte_proofs, allow_capture=False) != expected:
        raise RecoveryUnavailable


def require_current(root: Path, copied: reserved_paths.GenericFileCopy, who, *, byte_proofs: dict | None = None) -> tuple:
    try:
        reserved_paths.require_current_generic_copy(copied)
    except (reserved_paths.ReservedPathLeafError, OSError) as error:
        raise RecoveryUnavailable from error
    return _release(root, copied, who, byte_proofs={} if byte_proofs is None else byte_proofs, allow_capture=True)


@contextmanager
def stage(
    root: Path,
    selected: str,
    who,
    *,
    expected_sha256: str | None = None,
    expected: dict | None = None,
    byte_proofs: dict | None = None,
):
    """Own private storage for one request, including refusal and cancellation cleanup."""
    if not who.resolved or not _SLOTS.acquire(blocking=False):
        raise RecoveryUnavailable
    try:
        with reserved_paths.private_copy_directory() as temporary:
            try:
                seconds = request_budget.budget_seconds()
                budget = request_budget.current()
                deadline = (
                    min(time.monotonic() + seconds, budget.deadline)
                    if budget is not None
                    else time.monotonic() + seconds
                )
                path, metadata = _resolve(root, selected)
                available = shutil.disk_usage(temporary).free
                copied = reserved_paths.copy_generic_file(
                    root,
                    path,
                    Path(temporary) / "original",
                    max_bytes=available // 2,
                    deadline=deadline,
                )
                descriptor = _descriptor(path, metadata, copied)
                if (
                    expected_sha256 is not None
                    and copied.sha256 != expected_sha256
                    or expected is not None
                    and descriptor != expected
                ):
                    raise RecoveryUnavailable
                require_current(root, copied, who, byte_proofs=byte_proofs)
                if _resolve(root, selected) != (path, metadata):
                    raise RecoveryUnavailable
                yield copied, descriptor
            except (
                reserved_paths.ReservedPathLeafError,
                OSError,
                UnicodeError,
                vault.FrontmatterError,
                connector_boundary.BoundaryUnavailable,
            ) as error:
                raise RecoveryUnavailable from error
    finally:
        _SLOTS.release()


@asynccontextmanager
async def staged(root: Path, selected: str, who, **kwargs):
    """Hand off entered storage once; an abandoned worker owns its eventual cleanup."""
    from starlette.concurrency import run_in_threadpool

    proofs: dict[str, reserved_paths.GenericFileCopy] = {}
    manager = stage(root, selected, who, byte_proofs=proofs, **kwargs)
    ownership = threading.Lock()
    entered = False
    abandoned = False

    def enter():
        nonlocal entered
        result = manager.__enter__()
        with ownership:
            cleanup = abandoned
            entered = not cleanup
        if cleanup:
            manager.__exit__(None, None, None)
        return result

    try:
        copied, descriptor = await run_in_threadpool(enter)
        yield copied, descriptor, proofs
    finally:
        with ownership:
            abandoned = True
            cleanup = entered
            entered = False
        if cleanup:
            manager.__exit__(None, None, None)


def claim(root: Path, capability: str, *, delivery: str):
    result = upload_tokens.artifact_claim(
        capability, signing_root(), deployment=deployment(root), delivery=delivery
    )
    if result is None:
        raise RecoveryUnavailable
    return result


def native_principal(root: Path, uri: str, caller):
    if not uri.startswith(RESOURCE_PREFIX):
        raise RecoveryUnavailable
    _, issuer, _ = claim(root, uri[len(RESOURCE_PREFIX) :], delivery=upload_tokens.ARTIFACT_NATIVE)
    if (
        not caller.resolved
        or caller.audience_id != issuer.audience_id
        or caller.issuer_family != issuer.issuer_family
        or caller.client_binding != issuer.client_binding
        or caller.origin_session != issuer.origin_session
        or caller.session_id != issuer.session_id
    ):
        raise RecoveryUnavailable
    return issuer


@contextmanager
def native_grant_scope(grant: str | None):
    """Carry authority beside a sanitized resource URI so framework spans never see it."""
    state = _NativeGrantState(grant)
    token = _NATIVE_GRANT.set(state)
    try:
        yield state
    finally:
        _NATIVE_GRANT.reset(token)


def current_native_grant() -> str:
    state = _NATIVE_GRANT.get()
    if state is None or state.grant is None:
        raise RecoveryUnavailable
    return state.grant


def complete_native_grant(grant: str) -> None:
    """Record the leaf's final live session, release, version and expiry fence."""
    state = _NATIVE_GRANT.get()
    if state is None or state.grant is None or state.grant != grant:
        raise RecoveryUnavailable
    state.completed = True


def prepare(root: Path, selected: str, *, expected_sha256: str | None = None) -> dict:
    root_key = signing_root()
    if root_key is None:
        raise ValueError("ORIGINAL_RECOVERY_UNAVAILABLE: private signing authority is unavailable")
    from dataclasses import replace

    who = replace(effective_principal(), administrative_ingress=False)
    base = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    url = urlsplit(base)
    https = (
        url.scheme == "https"
        and bool(url.netloc)
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
    )
    proofs: dict[str, reserved_paths.GenericFileCopy] = {}
    with stage(root, selected, who, expected_sha256=expected_sha256, byte_proofs=proofs) as (copied, descriptor):
        deliveries = {}
        if https:
            token, expires = upload_tokens.mint_artifact(
                root_key,
                who,
                descriptor,
                deployment=deployment(root),
                delivery=upload_tokens.ARTIFACT_HTTPS,
            )
            deliveries["https"] = {
                "url": f"{base}/download?grant={quote(token, safe='')}",
                "expires_at": expires,
            }
        if who.surface == "mcp" and copied.size <= native_max_bytes():
            token, expires = upload_tokens.mint_artifact(
                root_key,
                who,
                descriptor,
                deployment=deployment(root),
                delivery=upload_tokens.ARTIFACT_NATIVE,
            )
            deliveries["native_resource"] = {
                "uri": RESOURCE_PREFIX + token,
                "expires_at": expires,
                "interface": "MCP resources/read blob; client support is required",
            }
        if not deliveries:
            raise ValueError(
                "ORIGINAL_RECOVERY_UNAVAILABLE: configure HTTPS for originals above the native response budget"
            )
        require_current(root, copied, who, byte_proofs=proofs)
        return {
            "handoff_status": "handoff_prepared",
            "committed": False,
            "artifact": descriptor,
            "deliveries": deliveries,
            "preferred_delivery": "native_resource" if "native_resource" in deliveries else "https",
            "ttl_seconds": upload_tokens.DEFAULT_TTL,
        }
