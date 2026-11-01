"""Out-of-band upload/download routes for Exomem."""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import mimetypes
import os
import secrets
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from . import (
    artifact_recovery,
    auth_sessions,
    cf_access,
    local_ingress,
    media_store,
    originals,
    reserved_paths,
    upload_handoff,
    upload_sessions,
    upload_tokens,
)
from .governance import egress
from .governance import principal as principal_module
from .vault import VaultPathError, resolve_under_vault

DEFAULT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
#: Loopback uploads never cross the Cloudflare edge, whose ~100 MB cap sets the public
#: default. A request is spooled to temporary storage before the copy-time check, so this
#: bound is refused early from Content-Length; a whole export fits, a runaway client stops.
DEFAULT_LOCAL_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
#: Multipart framing and form fields that ride with the file part.
_FORM_OVERHEAD_BYTES = 1024 * 1024
#: tus 1.0 (tus.io resumable-upload protocol): the extensions served. `upload_sessions`
#: holds the version, the secret header and the route, which an upload handoff advertises.
_TUS_EXTENSIONS = "creation,expiration,termination"
_TUS_PATCH_CONTENT_TYPE = "application/offset+octet-stream"
_SESSION_WRITE_BYTES = 1024 * 1024
#: The one `archive` mode: keep a zip as its members (`archive_members`).
ARCHIVE_MEMBERS = "members"
log = logging.getLogger(__name__)


_header_count = upload_sessions.header_count


def _flag(value: str) -> bool | None:
    """An `/upload` yes-or-no field: empty is no, `1` or `true` is yes, anything else is None."""
    value = value.strip()
    # nosemgrep: ep-word-membership -- `/upload` fixes these flag spellings.
    return bool(value) if value in ("", "1", "true") else None


def _upload_flags(raw_flag: str, archive: str) -> tuple[bool, bool, str | None]:
    """`raw_protection` and `archive` as `/upload` and a session take them.

    Returns whether raw protection is asked for, whether the archive is kept as
    its members, and the refusal when either value is malformed.
    """
    raw_protection, archive = _flag(raw_flag), archive.strip()
    if raw_protection is None:
        return False, False, "`raw_protection` must be 1"
    if archive and archive != ARCHIVE_MEMBERS:
        return False, False, f"`archive` must be {ARCHIVE_MEMBERS}"
    return raw_protection, bool(archive), None


def _preserve_module():
    from . import preserve as preserve_module

    return preserve_module


def _media_processing_module():
    from . import media_processing

    return media_processing


def _preserve_under_guard(
    manager: Any,
    vault_root: Path,
    preserve_stream: Any,
    **kwargs: Any,
) -> Any:
    """Run the complete upload read-plan-write path under vault authority.

    The stream is read only once the guard is held, so only the acquire waits out contention.
    """
    with manager.absorbing_mutation_guard(vault_root, operation="upload_preserve"):
        return preserve_stream(vault_root, **kwargs)


def _preserve_members_under_guard(manager: Any, vault_root: Path, **kwargs: Any) -> Any:
    """Expand an archive's members outside the guard; commit its manifest under it."""
    from . import archive_members

    return archive_members.preserve_members(
        vault_root,
        guard=lambda: manager.absorbing_mutation_guard(vault_root, operation="upload_manifest"),
        **kwargs,
    )


def _capture_source_under_guard(
    manager: Any,
    vault_root: Path,
    source_schema: Any,
    *,
    title: str,
    filename: str,
    stream: Any,
    content_type: str | None,
    source_type: str | None,
    domain: str | None,
    add_module: Any,
    max_bytes: int,
    raw_protection: bool = False,
) -> Any:
    """Capture an out-of-band upload as a Source, under vault authority.

    The bytes are spooled to a private temporary file first because `add` copies
    from a path: it writes the artifact and its page in one operation, and a
    half-consumed request stream cannot be replayed if that operation refuses.

    A person may post from the upload form with no agent to ask, so a missing
    kind is recorded as `unclassified` rather than refused.
    """
    import tempfile

    source_type = add_module.capture_kind(vault_root, source_type, unattended=True)
    with tempfile.TemporaryDirectory(prefix="exomem-upload-") as staging:
        staged = Path(staging) / (Path(filename).name or "upload.bin")
        written = 0
        with staged.open("wb") as sink:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("TOO_LARGE: upload exceeds the configured limit")
                sink.write(chunk)
        with manager.absorbing_mutation_guard(vault_root, operation="upload_capture"):
            return add_module.add(
                vault_root,
                source_schema,
                content="",
                title=title,
                source_type=source_type,
                domain=domain,
                artifact=add_module.SourceArtifact(
                    staged_path=staged,
                    filename=filename,
                    content_type=content_type,
                ),
                raw_protection=raw_protection,
            )


def _reconcile_under_guard(
    manager: Any,
    vault_root: Path,
    binary_path: Path,
) -> Any:
    media_processing = _media_processing_module()
    if media_processing.classify_media(binary_path) is None:
        return None
    # The upload has committed and a failed reconcile only logs, so it does not wait out contention.
    with manager.mutation_guard(vault_root, operation="upload_reconcile"):
        return media_processing.reconcile_media(vault_root, binary_path, explicit=False)


def _source_session_destination(vault_root: Path, target: dict, *, size: int, sha256: str) -> str:
    """Use the canonical Source validation before byte receipt and again before publication."""
    from . import add, schema, temporal, vocabulary_gate

    safe_name = add.source_artifact_name(target["filename"])
    vocabulary_gate.artifact_admission(
        vault_root, size=size, is_markdown=add.source_artifact_suffix(target["filename"]).endswith(".md"),
    )
    prepared = add.prepare_source_metadata(
        vault_root, schema.load_source_schema(vault_root),
        content=add._describe_artifact(safe_name, sha256, size),
        title=target["title"], source_type=target["source_type"],
        domain=target.get("domain"), url=target.get("url"),
    )
    folder = add.admit_source_destination(vault_root, prepared, temporal.render_date(temporal.now()))
    return folder.relative_to(vault_root).as_posix()


def _source_upload_receipt(vault_root: Path, result: dict) -> dict:
    """Return custody without exposing the canonical writer's corpus advisories."""
    from . import source_coverage

    # These fields are the closed upload protocol; corpus advisories require governed command release.
    receipt = {key: result[key] for key in ("path", "ref", "artifact_path", "hash", "hash_algorithm", "size")
               if key in result}
    receipt["semantic_compilation"] = source_coverage.compilation_guidance(vault_root, [result["path"]])
    return receipt


def _commit_source_session(vault_root: Path, part: Path, record: dict) -> dict:
    """Publish one Source through the maintained mutation replay protocol."""
    from . import add, schema
    from .cli_ops import OpError
    from .command_surface import Command
    from .vault import BatchWriteError
    from .writer_lease import _PostCommitOutcomeUncertain, invoke_command

    def publish(root: Path, *, target: dict, length: int, sha256: str, session_id: str) -> dict:
        if _source_session_destination(root, target, size=length, sha256=sha256) != target["destination"]:
            raise upload_sessions.CommitFailed(
                "SOURCE_DESTINATION_CHANGED", "the Source destination changed; uploaded bytes remain held", retryable=True,
            )
        try:
            result = add.add(
                root, schema.load_source_schema(root), content="", title=target["title"],
                source_type=target["source_type"], domain=target.get("domain"), url=target.get("url"),
                artifact=add.SourceArtifact(part, target["filename"], target.get("content_type")),
                raw_protection=target["raw_protection"], expected_sha256=sha256,
            ).as_dict()
        except BatchWriteError as error:
            if error.code == "BATCH_ROLLBACK_INCOMPLETE":
                # Canonical bytes may remain, so the replay store must never execute this leaf again.
                raise _PostCommitOutcomeUncertain() from error
            raise
        return _source_upload_receipt(root, result)

    try:
        result = invoke_command(
            Command(name="upload_source_session", leaf=publish, params=(), surfaces=frozenset(),
                    cli_writes=True, response_detail="legacy"),
            vault_root,
            target=record["target"], length=record["length"],
            sha256=record["sha256"], session_id=record["id"],
            idempotency_key=record["id"], public_idempotency_key=None,
        )
    except OpError as error:
        raise upload_sessions.CommitFailed(
            error.code, "the Source mutation is not settled; uploaded bytes remain held", retryable=True,
        ) from error
    if not isinstance(result, dict) or not result.get("path") or not result.get("artifact_path") or not result.get("hash"):
        # Signed receipt recovery can prove a commit without recovering its complete leaf result.
        raise upload_sessions.CommitFailed(
            "SOURCE_OUTCOME_UNKNOWN", "the Source outcome is unknown; uploaded bytes remain held", retryable=True,
        )
    # Also project older cached results before they leave the transfer principal's scope.
    return _source_upload_receipt(vault_root, result)


def commit_session(vault_root: Path, part: Path, record: dict) -> dict:
    """Preserve a verified session's bytes exactly as `/upload` would; return the receipt.

    Idempotent, so a commit that a stop interrupted can run again: a single file
    already stored at its target with the session's SHA-256 is `already_stored`,
    as an archive whose manifest is recorded already is. A refusal of the bytes
    or the target fails the session; anything else leaves it to a retry.
    """
    # A lane token carries no caller identity. Every commit runs under the same transfer floor:
    # the first attempt, a re-attempt by the server's own timer and startup recovery alike.
    with principal_module.request_scope(principal_module.most_restrictive_principal(surface="transfer")):
        return _commit_session(vault_root, part, record)


def _commit_session(vault_root: Path, part: Path, record: dict) -> dict:
    from . import archive_members
    from .cli_ops import OpError, error_dict
    from .writer_lease import get_manager

    preserve_module = _preserve_module()
    target = record["target"]
    manager = get_manager()
    try:
        lane = target.get("lane", upload_sessions.LANE)
        if lane not in upload_tokens.UPLOAD_LANES:
            raise upload_sessions.CommitFailed("INVALID_UPLOAD", "the upload session lane is invalid")
        if lane == "source":
            receipt = _commit_source_session(vault_root, part, record)
        else:
            receipt = _commit_evidence_session(vault_root, part, record, manager, preserve_module)
    except archive_members.ArchiveError as exc:
        raise upload_sessions.CommitFailed(
            exc.code, exc.reason, retryable=exc.code == archive_members.NO_SPACE
        ) from exc
    except preserve_module.PreserveError as exc:
        raise upload_sessions.CommitFailed(exc.code, exc.reason) from exc
    except OpError as exc:
        error = error_dict(exc)
        raise upload_sessions.CommitFailed(error["code"], error["message"], retryable=True) from exc
    try:
        # The Source receipt names its page; its artifact is what needs extraction.
        _reconcile_under_guard(manager, vault_root, vault_root / receipt.get("artifact_path", receipt["path"]))
    except Exception:  # noqa: BLE001 - preserved originals remain recoverable
        log.warning("media reconciliation failed after upload; the original remains recoverable", exc_info=True)
    return receipt


def _commit_evidence_session(vault_root: Path, part: Path, record: dict, manager: Any, preserve_module: Any) -> dict:
    target = record["target"]
    try:
        with part.open("rb") as stream:
            if target.get("archive") == ARCHIVE_MEMBERS:
                receipt, _stored = _preserve_members_under_guard(
                    manager,
                    vault_root,
                    scope=target["scope"],
                    category=target["category"],
                    filename=target["filename"],
                    stream=stream,
                    max_bytes=int(record["length"]),
                    verified="session",
                    description=target.get("description"),
                    sha256=record["sha256"],
                )
                return receipt
            try:
                result = _preserve_under_guard(
                    manager,
                    vault_root,
                    preserve_module.preserve_stream,
                    scope=target["scope"],
                    category=target["category"],
                    filename=target["filename"],
                    stream=stream,
                    description=target.get("description"),
                    max_bytes=int(record["length"]),
                    raw_protection=bool(target.get("raw_protection")),
                )
            except preserve_module.PreserveError as exc:
                stored = _stored_already(vault_root, record) if exc.code == "ARTIFACT_EXISTS" else None
                if stored is None:
                    raise
                receipt = stored
            else:
                receipt = result.as_dict()
    except preserve_module.PreserveError as exc:
        raise upload_sessions.CommitFailed(exc.code, exc.reason) from exc
    return receipt


def _stored_already(vault_root: Path, record: dict) -> dict | None:
    """The `already_stored` receipt when the session's target holds exactly its bytes."""
    target = record["target"]
    stored = _preserve_module().stored_artifact(
        vault_root, scope=target["scope"], category=target["category"], filename=target["filename"],
        sha256=record["sha256"], raw_protection=bool(target.get("raw_protection")),
    )
    if stored is None:
        return None
    return stored.receipt(size=int(record["length"]), content_type=mimetypes.guess_type(stored.path)[0])


def resume_upload_sessions(vault_root: Path) -> None:
    """Drop expired upload sessions and resume the commits that a stop interrupted.

    Only the serving HTTP runtime's activation calls this. A standby owns nothing
    until promotion, and a stdio server serves no session route, so neither may
    commit an upload beside the worker that owns it.
    """
    committer = functools.partial(commit_session, vault_root)
    for pending in upload_sessions.startup_sweep(vault_root):
        upload_sessions.start_commit(pending, committer)


@dataclass(frozen=True)
class TransferConfig:
    upload_token: str | None
    upload_max_bytes: int
    large_upload_base: str | None
    cf_team: str | None
    cf_aud: str | None
    cf_jwks: Any | None
    principal_signing_root: str | None = None
    local_upload_max_bytes: int = DEFAULT_LOCAL_UPLOAD_MAX_BYTES

    @property
    def enabled(self) -> bool:
        return self.upload_token is not None or self.cf_jwks is not None


def load_transfer_config() -> TransferConfig:
    """Read upload/download auth and sizing config from the environment."""
    upload_token = os.environ.get("EXOMEM_UPLOAD_TOKEN", "").strip() or None
    upload_max_bytes = int(
        os.environ.get("EXOMEM_UPLOAD_MAX_BYTES", str(DEFAULT_UPLOAD_MAX_BYTES))
    )
    large_upload_base = (
        os.environ.get("EXOMEM_LARGE_UPLOAD_BASE_URL", "").strip().rstrip("/") or None
    )
    cf_team = os.environ.get("EXOMEM_CF_ACCESS_TEAM_DOMAIN", "").strip() or None
    cf_aud = os.environ.get("EXOMEM_CF_ACCESS_AUD", "").strip() or None
    cf_jwks = cf_access.make_jwks_client(cf_team) if (cf_team and cf_aud) else None
    return TransferConfig(
        upload_token=upload_token,
        upload_max_bytes=upload_max_bytes,
        large_upload_base=large_upload_base,
        cf_team=cf_team,
        cf_aud=cf_aud,
        cf_jwks=cf_jwks,
        principal_signing_root=upload_tokens.private_signing_root(
            upload_token, os.environ.get("EXOMEM_JWT_SIGNING_KEY"),
        ),
        local_upload_max_bytes=int(
            os.environ.get("EXOMEM_LOCAL_UPLOAD_MAX_BYTES", str(DEFAULT_LOCAL_UPLOAD_MAX_BYTES))
        ),
    )


def _download_target(vault_root: Path, path: str) -> tuple[Path, str]:
    """Resolve a download path; an evicted original resolves by its binding (D1)."""
    try:
        return resolve_under_vault(vault_root, path, must_exist=True, must_be_file=True)
    except VaultPathError as missing:
        if missing.code != "NOT_FOUND" or originals.binding_for(vault_root) is None:
            raise
        try:
            abs_path, rel = resolve_under_vault(vault_root, path, must_be_file=True)
            if originals.evicted(vault_root, rel):
                return abs_path, rel
        except (VaultPathError, reserved_paths.ReservedPathLeafError):
            pass
        raise missing


def download_principal(
    request: Request, config: TransferConfig
) -> principal_module.RequestPrincipal:
    """Canonical audience for a `/download` caller (design D5).

    The public bearer and legacy v2 tokens carry an audience, nothing more. V3
    preserves the minting principal, its session and purpose included, using
    the server-private signing root. Cloudflare Access identities use the same normalized audience
    as REST. Every form still meets the live release decision for the bytes sent.

    Module-level (not a closure over the route) so the resolution contract is
    directly testable without reaching through a registered endpoint.
    """
    if config.upload_token is not None:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            presented = header[len("Bearer ") :].strip()
            # Bytes, not str: `compare_digest` raises on a non-ASCII str, and a
            # header is whatever bytes the caller sent.
            if secrets.compare_digest(presented.encode(), config.upload_token.encode()):
                return principal_module.owner_principal(surface="transfer")
            bound = upload_tokens.bound_principal(presented, upload_tokens.private_signing_root(
                config.upload_token, config.principal_signing_root,
            ))
            if bound is not None:
                return bound
            audience = upload_tokens.bound_audience(presented, config.upload_token)
            if audience == principal_module.OWNER_AUDIENCE:
                return principal_module.owner_principal(surface="transfer")
            if audience is not None:
                # The reserved `\x00` ids (the fail-closed floor, the unnamed
                # probe) are no grant target: a token carrying one decides as
                # the floor rather than as a resolved identity.
                if audience.startswith("\x00"):
                    return principal_module.most_restrictive_principal(surface="transfer")
                return principal_module.RequestPrincipal(
                    audience_id=audience, surface="transfer"
                )
    if config.cf_jwks is not None:
        claims = cf_access.verified_claims(
            request.headers.get("cf-access-jwt-assertion"),
            jwks_client=config.cf_jwks,
            team_domain=config.cf_team,
            audience=config.cf_aud,
        )
        if claims is not None:
            subject = str(claims.get("sub") or claims.get("email") or "").strip()
            issuer = str(claims.get("iss") or "").strip()
            if subject and issuer:
                return principal_module.RequestPrincipal(
                    audience_id=principal_module.normalize_audience(
                        subject=subject, issuer=issuer
                    ),
                    surface="transfer",
                )
    # Authorized (the route already checked) but unresolvable: an identity was
    # expected and did not resolve, so fail closed rather than open.
    return principal_module.most_restrictive_principal(surface="transfer")



_RECOVERY_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}


def _hydration_too_large(error: BaseException | None) -> JSONResponse | None:
    """The final refusal for an original larger than the whole hydration capacity (D3, M4).

    A 4xx status and no `retry-after`: waiting never clears it, so no client retries it.
    """
    if not isinstance(error, media_store.MediaStoreError) or error.code != media_store.HYDRATION_TOO_LARGE_CODE:
        return None
    return JSONResponse({"code": media_store.HYDRATION_TOO_LARGE_CODE,
                         "reason": "the original is larger than this cell can bring back from its store"},
                        status_code=409, headers=_RECOVERY_HEADERS)


class _HeldFileResponse(FileResponse):
    """A `FileResponse` that reads a held original from its handle, with no private copy (task 5.4.12).

    It keeps Starlette's header and range logic and replaces only the three body writers.
    `read_held` checks the file's version after each read, before the bytes are sent. A file that
    changes during the stream therefore stops the response, and the chunk that completes the body
    is sent only after its check passes, so a stopped response never looks complete.
    """

    def __init__(self, held: reserved_paths.GenericFileCopy, *, media_type: str,
                 headers: dict[str, str] | None = None, filename: str | None = None):
        super().__init__(held.relative_path, headers=headers, media_type=media_type, filename=filename,
                         stat_result=held.source_stat)
        self.held = held

    async def _send_span(self, send, start: int, end: int, *, more_after: bool) -> None:
        """Send bytes `start` up to `end`; the last chunk ends the body unless `more_after`."""
        while True:
            length = min(self.chunk_size, end - start)
            chunk = await run_in_threadpool(reserved_paths.read_held, self.held, start, length)
            start += length
            done = start >= end
            await send({"type": "http.response.body", "body": chunk, "more_body": more_after or not done})
            if done:
                return

    async def _handle_simple(self, send, send_header_only: bool, send_pathsend: bool) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await self._send_span(send, 0, self.held.size, more_after=False)

    async def _handle_single_range(self, send, start: int, end: int, file_size: int, send_header_only: bool) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await self._send_span(send, start, end, more_after=False)

    async def _handle_multiple_ranges(self, send, ranges: list[tuple[int, int]], file_size: int,
                                      send_header_only: bool) -> None:
        boundary = secrets.token_hex(13)
        content_length, header_for = self.generate_multipart(ranges, boundary, file_size, self.headers["content-type"])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        for start, end in ranges:
            await send({"type": "http.response.body", "body": header_for(start, end), "more_body": True})
            await self._send_span(send, start, end, more_after=True)
            await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
        await send({"type": "http.response.body", "body": f"--{boundary}--".encode("latin-1"), "more_body": False})


class _OriginalDownloadResponse(Response):
    """Starlette sends only a held, verified original; this response owns its lifetime."""

    def __init__(self, vault_root: Path, capability: str):
        super().__init__(headers=_RECOVERY_HEADERS)
        self.vault_root = vault_root
        self.capability = capability

    async def __call__(self, scope, receive, send):
        started = False
        try:
            descriptor, who, _ = artifact_recovery.claim(
                self.vault_root, self.capability, delivery=upload_tokens.ARTIFACT_HTTPS)
            if not await auth_sessions.origin_session_active(who):
                raise artifact_recovery.RecoveryUnavailable
            async with artifact_recovery.staged(
                self.vault_root, descriptor["path"], who, expected=descriptor
            ) as (held, _, proofs):
                response = _HeldFileResponse(held, filename=descriptor["file_name"],
                    media_type=descriptor["mime_type"], headers=_RECOVERY_HEADERS)
                # Only a verified original receives an attachment disposition.
                del response.headers["content-disposition"]
                response.headers["content-disposition"] = "attachment; filename*=utf-8''" + quote(descriptor["file_name"], safe="")

                async def checked_send(message):
                    nonlocal started
                    if message["type"] == "http.response.start":
                        artifact_recovery.claim(self.vault_root, self.capability, delivery=upload_tokens.ARTIFACT_HTTPS)
                        if not await auth_sessions.origin_session_active(who):
                            raise artifact_recovery.RecoveryUnavailable
                        revision = await run_in_threadpool(artifact_recovery.require_current, self.vault_root, held, who, byte_proofs=proofs)
                        # Expiry can pass during rehashing a large original.
                        artifact_recovery.claim(self.vault_root, self.capability, delivery=upload_tokens.ARTIFACT_HTTPS)
                        if not await auth_sessions.origin_session_active(who):
                            raise artifact_recovery.RecoveryUnavailable
                        artifact_recovery.require_revision(self.vault_root, who, held, revision, byte_proofs=proofs)
                        artifact_recovery.claim(self.vault_root, self.capability, delivery=upload_tokens.ARTIFACT_HTTPS)
                        headers = dict(message.get("headers", []))
                        for key, value in _RECOVERY_HEADERS.items():
                            headers[key.encode()] = value.encode()
                        headers[b"content-disposition"] = response.headers["content-disposition"].encode("latin-1")
                        message = {**message, "headers": list(headers.items())}
                        started = True
                    await send(message)

                # A pathsend extension may consume the file after ASGI returns; cleanup must await the actual stream.
                extensions = {key: value for key, value in scope.get("extensions", {}).items() if key != "http.response.pathsend"}
                await response({**scope, "extensions": extensions}, receive, checked_send)
        except (artifact_recovery.RecoveryUnavailable, reserved_paths.ReservedPathLeafError):
            if started:
                raise
            await JSONResponse({"code": "ORIGINAL_UNAVAILABLE", "reason": "original recovery is unavailable"},
                status_code=404, headers=_RECOVERY_HEADERS)(scope, receive, send)


async def _receive_ticket_upload(vault_root: Path, request: Request, ticket: dict) -> JSONResponse:
    """Receive one claimed ticket's file into a held upload; the ticket fixes lane and limit."""
    from . import held_uploads

    maximum = ticket["max_bytes"]
    received = 0
    receive = request.receive

    async def bounded_receive():
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > maximum + _FORM_OVERHEAD_BYTES:
                # MultiPartException makes Starlette close any partial spool files.
                raise MultiPartException("upload body exceeds the handoff limit")
        return message

    bounded = Request(request.scope, receive=bounded_receive)
    declared = _header_count(request.headers.get("content-length", ""))
    if declared is not None and declared > maximum + _FORM_OVERHEAD_BYTES:
        return JSONResponse({"code": "TOO_LARGE", "reason": "upload exceeds the handoff limit"}, status_code=413)
    try:
        async with bounded.form(max_files=1, max_fields=3, max_part_size=_FORM_OVERHEAD_BYTES) as form:
            # The ticket-upload protocol fixes these fields; none can select a commit target.
            if (str(form.get("lane") or "") != ticket["lane"]
                    or _flag(str(form.get("hold") or "")) is not True
                    or set(form) - {"file", "lane", "hold", "filename"}
                    or any(len(form.getlist(key)) != 1 for key in form)):
                return JSONResponse({"code": "INVALID_UPLOAD", "reason": "handoff fields cannot change its lane or hold mode"}, status_code=400)
            upload = form.get("file")
            if not hasattr(upload, "file"):
                return JSONResponse({"code": "INVALID_UPLOAD", "reason": "multipart field `file` is required"}, status_code=400)
            if not await upload_handoff._active(vault_root, ticket["principal"]):
                return JSONResponse({"code": "UPLOAD_HANDOFF_UNAVAILABLE", "reason": upload_handoff.UNAVAILABLE_REASON}, status_code=401)
            with principal_module.request_scope(ticket["principal"]):
                held = await run_in_threadpool(
                    held_uploads.hold, vault_root, upload.file,
                    lane=ticket["lane"],
                    filename=str(form.get("filename") or upload.filename or ""),
                    content_type=upload.content_type, max_bytes=maximum,
                    expected_sha256=ticket["expected_sha256"],
                )
        return JSONResponse(held, status_code=201)
    except (MultiPartException, HTTPException, ClientDisconnect, ValueError):
        return JSONResponse({"code": "TOO_LARGE", "reason": "upload exceeds the handoff limit or is malformed"}, status_code=413)
    except held_uploads.HeldUploadError as exc:
        return JSONResponse({"code": exc.code, "reason": exc.reason},
            status_code={"TOO_LARGE": 413, "HELD_UPLOAD_NO_SPACE": 507}.get(exc.code, 400))


def _handoff_unavailable() -> JSONResponse:
    return JSONResponse({"code": "UPLOAD_HANDOFF_UNAVAILABLE", "reason": upload_handoff.UNAVAILABLE_REASON}, status_code=401)


def register_cloud_upload_route(mcp_app: FastMCP, *, vault_root: Path, cell_bearer: Any) -> None:
    """Register a Cloud cell's one byte route, which receives held-upload tickets from the gateway.

    Custom routes sit outside the MCP bearer check, so this route checks the
    cell bearer itself. It then requires the gateway's fresh proof for this
    exact ticket, and only then claims the ticket or reads a body byte.
    """
    from . import cloud_cell, cloud_origin

    @mcp_app.custom_route("/upload", methods=["POST"])
    async def _cloud_upload(request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not presented or not cell_bearer.matches(presented):
            return JSONResponse({"code": "UNAUTHORIZED", "reason": "missing or invalid cell credential"}, status_code=401)
        ticket = request.headers.get(cloud_origin.TICKET_HEADER, "")
        if upload_handoff.parse_ticket(ticket) is None:
            return _handoff_unavailable()
        request_origin = cloud_origin.verify_request(request.headers, ticket=ticket)
        if request_origin is None:
            return _handoff_unavailable()
        # Refused before the claim, so a read-only window leaves the ticket usable.
        if cloud_cell.cloud_read_only_enabled():
            return JSONResponse({"code": "CLOUD_CELL_READ_ONLY", "reason": "this Exomem Cloud cell is serving read-only"}, status_code=503)
        with cloud_origin.bound(request_origin):
            try:
                claimed = await upload_handoff.claim_upload(vault_root, ticket)
            except ValueError:
                return _handoff_unavailable()
            return await _receive_ticket_upload(vault_root, request, claimed)


class _CloudOriginalDownloadResponse(Response):
    """Send one verified original to the login the gateway proved; this response owns the held handle.

    The route returns before the body is sent, so this response binds the
    request's origin again for the checks that run while it streams.
    """

    def __init__(self, vault_root: Path, path: str, request_origin):
        super().__init__(headers=_RECOVERY_HEADERS)
        self.vault_root = vault_root
        self.path = path
        self.request_origin = request_origin

    async def __call__(self, scope, receive, send):
        from starlette.datastructures import Headers

        from . import cloud_origin

        started = False
        try:
            with cloud_origin.bound(self.request_origin):
                who = cloud_origin.with_origin(
                    principal_module.cloud_owner_principal(self.request_origin.origin.cell_id, surface="mcp"))
                if not await auth_sessions.origin_session_active(who):
                    raise artifact_recovery.RecoveryUnavailable
                async with artifact_recovery.staged(self.vault_root, self.path, who) as (held, descriptor, proofs):
                    # Every answer, including a refused range, names the whole file's identity.
                    identity = {
                        **_RECOVERY_HEADERS,
                        "x-exomem-sha256": descriptor["sha256"],
                        "x-exomem-size": str(descriptor["size"]),
                        "content-disposition": "attachment; filename*=utf-8''" + quote(descriptor["file_name"], safe=""),
                    }
                    ranges = Headers(scope=scope).getlist("range")
                    if len(ranges) > 1 or "," in "".join(ranges):
                        # One slice is the contract; repeated Range headers or a multipart answer are not offered.
                        response = PlainTextResponse(status_code=416, headers={"content-range": f"bytes */{descriptor['size']}"})
                    else:
                        response = _HeldFileResponse(held, media_type=descriptor["mime_type"])

                    async def checked_send(message):
                        nonlocal started
                        if message["type"] == "http.response.start":
                            if not await auth_sessions.origin_session_active(who):
                                raise artifact_recovery.RecoveryUnavailable
                            revision = await run_in_threadpool(
                                artifact_recovery.require_current, self.vault_root, held, who, byte_proofs=proofs)
                            artifact_recovery.require_revision(self.vault_root, who, held, revision, byte_proofs=proofs)
                            headers = dict(message.get("headers", []))
                            headers.update({key.encode(): value.encode("latin-1") for key, value in identity.items()})
                            message = {**message, "headers": list(headers.items())}
                            started = True
                        await send(message)

                    # A pathsend extension may consume the file after ASGI returns; cleanup must await the actual stream.
                    extensions = {key: value for key, value in scope.get("extensions", {}).items() if key != "http.response.pathsend"}
                    await response({**scope, "extensions": extensions}, receive, checked_send)
        except (artifact_recovery.RecoveryUnavailable, reserved_paths.ReservedPathLeafError) as error:
            if started:
                raise
            # An evicted original whose store cannot supply it is retryable; anything else says nothing about the path.
            if isinstance(error, artifact_recovery.RecoveryBusy):
                refusal = JSONResponse({"code": "ORIGINAL_BUSY", "reason": "other downloads are in progress; retry shortly"},
                    status_code=503, headers={**_RECOVERY_HEADERS, "retry-after": "5"})
            elif too_large := _hydration_too_large(error.__cause__):
                refusal = too_large
            elif isinstance(error.__cause__, media_store.MediaStoreError):
                refusal = JSONResponse({"code": "ORIGINAL_UNAVAILABLE", "reason": "the original is stored off this cell and the store is unreachable"},
                    status_code=503, headers=_RECOVERY_HEADERS)
            else:
                refusal = JSONResponse({"code": "ORIGINAL_NOT_FOUND", "reason": "no preserved original is readable at that path"},
                    status_code=404, headers=_RECOVERY_HEADERS)
            await refusal(scope, receive, send)


def register_cloud_download_route(mcp_app: FastMCP, *, vault_root: Path, cell_bearer: Any) -> None:
    """Register a Cloud cell's exact-original download, which only the gateway can reach.

    A custom route sits outside the MCP bearer check, so this route checks the
    cell bearer itself. It then requires the gateway's proof for this exact
    path, and only then resolves the path or reads a byte.
    """
    from . import cloud_origin

    def unavailable() -> JSONResponse:
        return JSONResponse({"code": "DOWNLOAD_UNAVAILABLE", "reason": "this download is not available"},
                            status_code=401, headers=_RECOVERY_HEADERS)

    @mcp_app.custom_route("/download", methods=["GET"])
    async def _cloud_download(request: Request) -> Response:
        header = request.headers.get("authorization", "")
        presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        paths = request.query_params.getlist("path")
        if not presented or not cell_bearer.matches(presented) or len(paths) != 1 or not paths[0]:
            return unavailable()
        request_origin = cloud_origin.verify_request(request.headers, download_path=paths[0])
        if request_origin is None:
            return unavailable()
        return _CloudOriginalDownloadResponse(vault_root, paths[0], request_origin)


def register_transfer_routes(
    mcp_app: FastMCP,
    *,
    vault_root: Path,
    media_worker: Any | None,
) -> TransferConfig:
    """Register /upload and /download routes and return their config."""
    config = load_transfer_config()
    artifact_recovery.install_log_redaction()

    def _upload_lane(request: Request) -> str:
        """The lane a request's upload capability is bound to.

        Read off the token rather than the form, so the destination is whatever
        was fixed at mint time. A shared static secret or a Cloudflare Access
        identity carries no lane and falls back to evidence, which is where
        every upload landed before lanes existed. A local client token is not
        a lane capability either.
        """
        if local_ingress.current_grant() is not None:
            return "evidence"
        if config.upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if not secrets.compare_digest(presented.encode(), config.upload_token.encode()):
                    lane = upload_tokens.lane_for_token(presented, config.upload_token)
                    if lane is not None:
                        return lane
        return "evidence"

    def _authorized(request: Request, *, scope: str = "upload") -> bool:
        if scope == "upload" and local_ingress.current_grant() is not None:
            # Local ingress: the gate in front of this route verified this
            # request's local client token, the only credential it accepts.
            return True
        if config.upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented.encode(), config.upload_token.encode()):
                    local_ingress.note_owner_credential("upload_token", request.headers)
                    return True
                if scope == "download":
                    # Only a capability naming its minting principal opens
                    # `/download`. One minted before that binding names nobody,
                    # so it is refused rather than guessed to be the owner.
                    if (upload_tokens.bound_audience(presented, config.upload_token)
                            or upload_tokens.bound_principal(presented, config.principal_signing_root) is not None):
                        return True
                elif upload_tokens.verify(presented, config.upload_token, scope=scope):
                    return True
                if scope == "upload" and upload_tokens.lane_for_token(
                    presented, config.upload_token
                ):
                    return True
        if config.cf_jwks is not None:
            if cf_access.verify(
                request.headers.get("cf-access-jwt-assertion"),
                jwks_client=config.cf_jwks,
                team_domain=config.cf_team,
                audience=config.cf_aud,
            ):
                return True
        return False

    @mcp_app.custom_route("/upload", methods=["POST"])
    async def _upload(request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if presented.startswith(upload_handoff.TOKEN_PREFIX):
            try:
                ticket = await upload_handoff.claim_upload(vault_root, presented)
            except ValueError:
                return _handoff_unavailable()
            return await _receive_ticket_upload(vault_root, request, ticket)
        if not config.enabled and local_ingress.current_grant() is None:
            return JSONResponse(
                {
                    "code": "UPLOAD_DISABLED",
                    "reason": "uploads are off: set EXOMEM_UPLOAD_TOKEN (or configure "
                    "Cloudflare Access via EXOMEM_CF_ACCESS_TEAM_DOMAIN + EXOMEM_CF_ACCESS_AUD)",
                },
                status_code=503,
            )
        if not _authorized(request):
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "missing or invalid upload credential"},
                status_code=401,
            )
        from .cli_ops import OpError, error_dict, http_status_for
        from .writer_lease import get_manager, record_caller_refusal

        max_bytes = (
            config.local_upload_max_bytes
            if local_ingress.current_grant() is not None
            else config.upload_max_bytes
        )
        declared = _header_count(request.headers.get("content-length", ""))
        if declared is not None and declared > max_bytes + _FORM_OVERHEAD_BYTES:
            # Refuse before the multipart parser spools the whole body to disk.
            return JSONResponse(
                {"code": "TOO_LARGE", "reason": f"upload exceeds the {max_bytes:,}-byte limit"},
                status_code=413,
            )
        try:
            # `max_part_size` bounds form fields only; the copy loops bound the file.
            form = await request.form(max_part_size=config.upload_max_bytes)
        except MultiPartException as exc:
            return JSONResponse(
                {
                    "code": "TOO_LARGE",
                    "reason": f"upload rejected (exceeds {config.upload_max_bytes:,}-byte "
                    f"limit or malformed): {exc}",
                },
                status_code=413,
            )
        upload = form.get("file")
        if not hasattr(upload, "read"):
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "multipart field `file` is required"},
                status_code=400,
            )
        scope = str(form.get("scope") or "").strip()
        category = str(form.get("category") or "").strip()
        description = str(form.get("description") or "").strip() or None
        text = str(form.get("text") or "").strip() or None
        filename = str(form.get("filename") or "").strip() or (
            getattr(upload, "filename", "") or ""
        )
        raw_protection, archive, flag_refusal = _upload_flags(
            str(form.get("raw_protection") or ""), str(form.get("archive") or "")
        )
        if flag_refusal is not None:
            return JSONResponse({"code": "INVALID_UPLOAD", "reason": flag_refusal}, status_code=400)
        hold = _flag(str(form.get("hold") or ""))
        if hold is None:
            return JSONResponse({"code": "INVALID_UPLOAD", "reason": "`hold` must be 1"}, status_code=400)
        if archive and (hold or _upload_lane(request) == "source"):
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "archive=members preserves Evidence directly"},
                status_code=400,
            )
        if hold:
            # `preserve-attachment-originals`: hold the bytes for a file-handle
            # command instead of preserving them. Only a verified local grant
            # can hold; nothing reaches the vault here.
            from . import held_uploads

            if raw_protection:
                return JSONResponse(
                    {
                        "code": "INVALID_UPLOAD",
                        "reason": "a held upload takes `raw_protection` when its command redeems it",
                    },
                    status_code=400,
                )
            from .client_artifacts import fetch_budget

            try:
                held = await run_in_threadpool(
                    held_uploads.hold,
                    vault_root,
                    upload.file,
                    lane=str(form.get("lane") or "evidence").strip(),
                    filename=filename,
                    content_type=getattr(upload, "content_type", None),
                    # A hold is only worth what its redeeming command can fetch.
                    max_bytes=min(max_bytes, fetch_budget().max_file_bytes),
                )
            except held_uploads.HeldUploadError as exc:
                return JSONResponse(
                    {"code": exc.code, "reason": exc.reason},
                    status_code={"TOO_LARGE": 413, "HELD_UPLOAD_NO_SPACE": 507}.get(exc.code, 400),
                )
            return JSONResponse(held, status_code=201)
        from . import archive_members

        preserve_module = _preserve_module()
        lane = _upload_lane(request)
        try:
            manager = get_manager()
            if archive:
                receipt, stored = await run_in_threadpool(
                    _preserve_members_under_guard,
                    manager,
                    vault_root,
                    scope=scope,
                    category=category,
                    filename=filename,
                    stream=upload.file,
                    max_bytes=max_bytes,
                    verified="upload",
                    description=description,
                )
                return JSONResponse(receipt, status_code=201 if stored else 200)
            if lane == "source":
                # The lane came off the token; the title is ordinary data and may
                # come off the form, falling back to the filename so a capture is
                # never refused for want of a label.
                from . import add as add_module
                from . import schema as schema_module

                title = str(form.get("title") or "").strip() or filename
                result = await run_in_threadpool(
                    _capture_source_under_guard,
                    manager,
                    vault_root,
                    schema_module.load_source_schema(vault_root),
                    title=title,
                    filename=filename,
                    stream=upload.file,
                    content_type=getattr(upload, "content_type", None),
                    source_type=str(form.get("source_kind") or "").strip() or None,
                    domain=str(form.get("domain") or "").strip() or None,
                    add_module=add_module,
                    max_bytes=max_bytes,
                    raw_protection=raw_protection,
                )
            else:
                result = await run_in_threadpool(
                    _preserve_under_guard,
                    manager,
                    vault_root,
                    preserve_module.preserve_stream,
                    scope=scope,
                    category=category,
                    filename=filename,
                    stream=upload.file,
                    content_type=getattr(upload, "content_type", None),
                    description=description,
                    text=text,
                    max_bytes=max_bytes,
                    raw_protection=raw_protection,
                )
        except preserve_module.PreserveError as exc:
            status = {
                "ARTIFACT_EXISTS": 409,
                "TOO_LARGE": 413,
                "INVALID_PRESERVE": 400,
            }.get(exc.code, 400)
            return JSONResponse(
                {"code": exc.code, "reason": exc.reason, "missing": exc.missing},
                status_code=status,
            )
        except archive_members.ArchiveError as exc:
            return JSONResponse(
                {"code": exc.code, "reason": exc.reason},
                status_code={archive_members.TOO_LARGE: 413, archive_members.NO_SPACE: 507}.get(exc.code, 400),
            )
        except (OpError, ValueError) as exc:
            record_caller_refusal(exc, tool="upload", targets=[str(vault_root)])
            error = error_dict(exc)
            return JSONResponse(
                {"code": error["code"], "reason": error["message"]},
                status_code=http_status_for(error["code"]),
            )

        try:
            await run_in_threadpool(
                _reconcile_under_guard,
                manager,
                vault_root,
                vault_root / (getattr(result, "artifact_path", None) or result.path),
            )
        except Exception:  # noqa: BLE001 - preserved evidence remains recoverable
            log.warning(
                "media reconciliation failed for %s; evidence remains recoverable",
                result.path,
                exc_info=True,
            )
        receipt = result.as_dict()
        if lane == "source":
            receipt = _source_upload_receipt(vault_root, receipt)
        return JSONResponse(receipt, status_code=201)

    def _credential_binding(request: Request) -> str:
        """Which credential opened a session, for its per-binding quota; never the credential."""
        grant = local_ingress.current_grant()
        if grant is not None:
            return f"mcp-local:{grant.session_id}"
        if config.cf_jwks is not None:
            claims = cf_access.verified_claims(
                request.headers.get("cf-access-jwt-assertion"),
                jwks_client=config.cf_jwks,
                team_domain=config.cf_team,
                audience=config.cf_aud,
            )
            if claims:
                identity = str(claims.get("sub") or claims.get("email") or "")
                return "cf-access:" + hashlib.sha256(identity.encode()).hexdigest()
        header = request.headers.get("authorization", "")
        return "bearer:" + hashlib.sha256(header.encode("utf-8", "replace")).hexdigest()

    session_max_bytes = upload_sessions.max_bytes_from_env()

    def _tus_headers(
        session: upload_sessions.Session | None = None, **extra: str
    ) -> dict[str, str]:
        headers = {
            "Tus-Resumable": upload_sessions.TUS_VERSION, "Cache-Control": "no-store", **extra
        }
        if session is not None:
            headers["Upload-Expires"] = formatdate(session.expires, usegmt=True)
        return headers

    def _tus_refusal(code: str, reason: str, status: int, **extra: str) -> JSONResponse:
        return JSONResponse(
            {"code": code, "reason": reason}, status_code=status, headers=_tus_headers(**extra)
        )

    def _tus_options() -> Response:
        return Response(
            status_code=204,
            headers=_tus_headers(
                **{
                    "Tus-Version": upload_sessions.TUS_VERSION,
                    "Tus-Extension": _TUS_EXTENSIONS,
                    "Tus-Max-Size": str(session_max_bytes),
                }
            ),
        )

    def _tus_version_refused(request: Request) -> JSONResponse | None:
        if request.headers.get("tus-resumable") == upload_sessions.TUS_VERSION:
            return None
        return _tus_refusal(
            "TUS_VERSION_UNSUPPORTED",
            f"`Tus-Resumable: {upload_sessions.TUS_VERSION}` is required",
            412,
            **{"Tus-Version": upload_sessions.TUS_VERSION},
        )

    def _tus_metadata(header: str) -> dict[str, str] | None:
        """`Upload-Metadata`: comma-separated `key base64(value)` pairs; None if malformed."""
        pairs: dict[str, str] = {}
        for item in header.split(","):
            if not item.strip():
                continue
            key, _, encoded = item.strip().partition(" ")
            if not key or key in pairs:
                return None
            try:
                pairs[key] = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        return pairs

    commit = functools.partial(commit_session, vault_root)

    @mcp_app.custom_route(upload_sessions.SESSIONS_PATH, methods=["POST", "OPTIONS"])
    async def _create_upload_session(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _tus_options()
        if not config.enabled and local_ingress.current_grant() is None:
            return _tus_refusal("UPLOAD_DISABLED", "uploads are off", 503)
        if not _authorized(request):
            return _tus_refusal("UNAUTHORIZED", "missing or invalid upload credential", 401)
        if (refused := _tus_version_refused(request)) is not None:
            return refused
        declared = _header_count(request.headers.get("upload-length", ""))
        metadata = _tus_metadata(request.headers.get("upload-metadata", ""))
        if declared is None or metadata is None:
            return _tus_refusal(
                "INVALID_UPLOAD", "`Upload-Length` and a well-formed `Upload-Metadata` are required", 400
            )
        # The keys an upload handoff advertises: one list, so the two never disagree.
        lane = _upload_lane(request)
        missing = [key for key in upload_sessions.required_metadata(lane) if not metadata.get(key, "").strip()]
        if missing:
            return _tus_refusal(
                "INVALID_UPLOAD", f"`Upload-Metadata` lacks {', '.join(missing)}", 400
            )
        preserve_module = _preserve_module()
        from . import add
        from .cli_ops import OpError, error_dict

        raw_protection, members, flag_refusal = _upload_flags(
            metadata.get("raw_protection", ""), metadata.get("archive", "")
        )
        target = {
            "lane": lane,
            "filename": metadata.get("filename", "").strip(),
            "scope": metadata.get("scope", "").strip(),
            "category": metadata.get("category", "").strip(),
            "description": metadata.get("description", "").strip() or None,
            "raw_protection": raw_protection,
            "archive": ARCHIVE_MEMBERS if members else None,
        }
        refusals = [
            refusal
            for refusal in (
                preserve_module.destination_segment_refusal(target["scope"], field="scope") if lane == "evidence" else None,
                preserve_module.destination_segment_refusal(target["category"], field="category") if lane == "evidence" else None,
                flag_refusal,
                "archive=members preserves Evidence directly" if members and lane == "source" else None,
            )
            if refusal
        ]
        if refusals:
            return _tus_refusal("INVALID_UPLOAD", "; ".join(refusals), 400)
        try:
            if lane == "source":
                target = {
                    "lane": lane, "filename": target["filename"],
                    "title": metadata.get("title", "").strip() or target["filename"],
                    "source_type": add.capture_kind(vault_root, metadata.get("source_kind") or None, unattended=True),
                    "domain": metadata.get("domain", "").strip() or None,
                    "url": metadata.get("url", "").strip() or None,
                    "content_type": metadata.get("content_type", "").strip() or None,
                    "raw_protection": raw_protection,
                }
                target["destination"] = _source_session_destination(
                    vault_root, target, size=declared, sha256=metadata.get("sha256", "").strip(),
                )
            elif members:
                folder = preserve_module.evidence_folder(vault_root, target["scope"], target["category"])
                preserve_module.admit_destination(vault_root, folder, directory=True, members=True)
                preserve_module.validate_raw_capture(target["filename"], raw_protection=True)
            else:
                # Before any byte is sent: a name the commit cannot store fails after the whole upload.
                artifact, _ = preserve_module.evidence_artifact(
                    vault_root, scope=target["scope"], category=target["category"],
                    filename=target["filename"], raw_protection=raw_protection,
                )
                # The destination decision comes before existence, which it never reveals.
                preserve_module.admit_destination(vault_root, artifact)
                if artifact.exists():
                    return _tus_refusal(
                        "ARTIFACT_EXISTS",
                        f"{artifact.relative_to(vault_root).as_posix()!r} already exists; "
                        "Evidence is append-only, so pick a new filename",
                        409,
                    )
            session, secret = await run_in_threadpool(
                upload_sessions.create,
                vault_root,
                binding=_credential_binding(request),
                length=declared,
                sha256=metadata.get("sha256", "").strip(),
                target=target,
                max_bytes=session_max_bytes,
            )
        except preserve_module.PreserveError as exc:
            return _tus_refusal(exc.code, exc.reason, 400)
        except (OpError, ValueError) as exc:
            error = error_dict(exc)
            return _tus_refusal(error["code"], error["message"], 400)
        except add.AddError as exc:
            return _tus_refusal(exc.code, exc.reason, 400)
        except upload_sessions.SessionError as exc:
            return _tus_refusal(exc.code, exc.reason, exc.status)
        await run_in_threadpool(upload_sessions.start_commit, session, commit)
        return Response(
            status_code=201,
            headers=_tus_headers(
                session,
                **{
                    "Location": f"{upload_sessions.SESSIONS_PATH}/{session.id}",
                    upload_sessions.SECRET_HEADER: secret,
                },
            ),
        )

    @mcp_app.custom_route(
        f"{upload_sessions.SESSIONS_PATH}/{{session_id}}",
        methods=["HEAD", "PATCH", "DELETE", "GET", "OPTIONS"],
    )
    async def _upload_session(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _tus_options()
        if request.method != "GET" and (refused := _tus_version_refused(request)) is not None:
            return refused
        try:
            session = await run_in_threadpool(
                upload_sessions.open_session,
                vault_root,
                request.path_params["session_id"],
                request.headers.get(upload_sessions.SECRET_HEADER),
            )
        except upload_sessions.SessionError as exc:
            return _tus_refusal(exc.code, exc.reason, exc.status)
        # A commit that a stop interrupted, or that can be retried, resumes when its session is read.
        await run_in_threadpool(upload_sessions.start_commit, session, commit)
        state = session.record["state"]
        if request.method == "GET":
            return JSONResponse(session.view(), headers=_tus_headers(session))
        if request.method == "HEAD":
            if state == upload_sessions.FAILED:
                return Response(status_code=410, headers=_tus_headers(session))
            try:
                held = await run_in_threadpool(upload_sessions.held_offset, session)
            except upload_sessions.SessionError as exc:
                return _tus_refusal(exc.code, exc.reason, exc.status)
            return Response(
                status_code=200,
                headers=_tus_headers(
                    session, **{"Upload-Offset": str(held), "Upload-Length": str(session.length)}
                ),
            )
        if request.method == "DELETE":
            try:
                await run_in_threadpool(upload_sessions.delete, session)
            except upload_sessions.SessionError as exc:
                return _tus_refusal(exc.code, exc.reason, exc.status)
            return Response(status_code=204, headers=_tus_headers())
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != _TUS_PATCH_CONTENT_TYPE:
            return _tus_refusal(
                "INVALID_UPLOAD", f"a part is sent as `{_TUS_PATCH_CONTENT_TYPE}`", 415
            )
        offset = _header_count(request.headers.get("upload-offset", ""))
        raw_length = request.headers.get("content-length", "")
        length = _header_count(raw_length) if raw_length else None
        if offset is None or (raw_length and length is None):
            return _tus_refusal("INVALID_UPLOAD", "`Upload-Offset` is required", 400)
        try:
            patch = await run_in_threadpool(upload_sessions.Patch, session, offset, length)
        except upload_sessions.SessionError as exc:
            return _tus_refusal(exc.code, exc.reason, exc.status)
        refusal: upload_sessions.SessionError | None = None
        buffered = bytearray()
        try:
            try:
                async for chunk in request.stream():
                    buffered += chunk
                    if len(buffered) >= _SESSION_WRITE_BYTES:
                        await run_in_threadpool(patch.write, bytes(buffered))
                        buffered.clear()
            except ClientDisconnect:
                pass  # keep what arrived: the client resumes from the offset it is told
            if buffered:
                await run_in_threadpool(patch.write, bytes(buffered))
        except upload_sessions.SessionError as exc:
            refusal = exc
        finally:
            new_offset, complete = await run_in_threadpool(patch.close)
        if refusal is not None:
            return _tus_refusal(
                refusal.code, refusal.reason, refusal.status, **{"Upload-Offset": str(new_offset)}
            )
        if complete:
            await run_in_threadpool(upload_sessions.start_commit, session, commit)
        return Response(
            status_code=204, headers=_tus_headers(session, **{"Upload-Offset": str(new_offset)})
        )

    @mcp_app.custom_route("/upload", methods=["GET"])
    async def _upload_form(request: Request) -> HTMLResponse:
        q = request.query_params

        def _attr(name: str) -> str:
            return (q.get(name) or "").replace('"', "&quot;")

        html = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>exomem upload</title>
<style>body{{font:16px system-ui;max-width:34rem;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.75rem 0 .2rem}}input,textarea{{width:100%;padding:.5rem;font:inherit}}
button{{margin-top:1rem;padding:.6rem 1rem;font:inherit}}#out{{margin-top:1rem;white-space:pre-wrap}}</style>
<h1>Add evidence to the KB</h1>
<form id=f>
<label>File <small>(max {config.upload_max_bytes // (1024 * 1024)} MB; a public link may be capped lower by the proxy)</small></label><input type=file name=file required>
<label>Scope</label><input name=scope value="{_attr('scope')}" placeholder="e.g. Rental" required>
<label>Category</label><input name=category value="{_attr('category')}" placeholder="e.g. 01 - Check-in" required>
<label>Filename (optional)</label><input name=filename value="{_attr('filename')}">
<label>Description (optional)</label><input name=description value="{_attr('description')}">
<label>Extracted text (optional - makes the file searchable)</label><textarea name=text rows=4 placeholder="OCR / transcribed text"></textarea>
<label>Upload token (blank if behind Cloudflare Access)</label><input name=token type=password>
<button type=submit>Upload</button></form>
<div id=out></div>
<script>
f.onsubmit=async e=>{{e.preventDefault();const fd=new FormData(f);const t=fd.get('token');fd.delete('token');
const h={{}};if(t)h['Authorization']='Bearer '+t;out.textContent='Uploading...';
try{{const r=await fetch('/upload',{{method:'POST',body:fd,headers:h}});
out.textContent=r.status+' '+await r.text();}}catch(err){{out.textContent='Error: '+err}}}};
</script>"""
        return HTMLResponse(html)

    @mcp_app.custom_route("/download", methods=["GET"])
    async def _download(request: Request):
        if "grant" in request.query_params:
            grants = request.query_params.getlist("grant")
            if len(grants) != 1 or "path" in request.query_params:
                return JSONResponse({"code": "ORIGINAL_UNAVAILABLE", "reason": "original recovery is unavailable"},
                    status_code=404, headers=_RECOVERY_HEADERS)
            return _OriginalDownloadResponse(vault_root, grants[0])
        response = await _download_legacy(request)
        for key, value in _RECOVERY_HEADERS.items():
            response.headers[key] = value
        return response

    async def _download_legacy(request: Request):
        if not config.enabled:
            return JSONResponse(
                {
                    "code": "DOWNLOAD_DISABLED",
                    "reason": "downloads are off: set EXOMEM_UPLOAD_TOKEN (or configure "
                    "Cloudflare Access via EXOMEM_CF_ACCESS_TEAM_DOMAIN + EXOMEM_CF_ACCESS_AUD)",
                },
                status_code=503,
            )
        if not _authorized(request, scope="download"):
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "missing or invalid download credential"},
                status_code=401,
            )
        who = download_principal(request, config)
        if not await auth_sessions.origin_session_active(who):
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "missing or invalid download credential"},
                status_code=401,
            )
        path = request.query_params.get("path", "")
        if not path.strip():
            return JSONResponse(
                {"code": "INVALID_PATH", "reason": "query param `path` (vault-relative) is required"},
                status_code=400,
            )
        # Spelled exactly as the resolver spells a path it cannot find.
        requested = path.strip().replace("\\", "/").lstrip("/")
        try:
            abs_path, rel = _download_target(vault_root, path)
            # Release gate on the download TARGET after path resolution but before
            # the response snapshot is opened or its bytes are read (design D1: the
            # transfer routes bypass `invoke_command`, so they carry the gate
            # explicitly). A download hands over the item's COMPLETE bytes, so only
            # full disclosure authorizes one — otherwise any ceiling could be
            # escaped by asking for the artifact instead of the text.
            #
            # Raised as the route's own NOT_FOUND rather than a new code, so a
            # withheld artifact is byte-identical to one that never existed:
            # a distinct "forbidden" reply would itself be an existence oracle.
            if not egress.release_allows_download(
                vault_root, rel, principal=who
            ):
                raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}")
            try:
                # The grant route's pin: hydrate only after the release gate, and
                # keep eviction out until the bytes are read.
                with media_store.local_originals(vault_root, (rel,), read=True):
                    snapshot = reserved_paths.read_generic_bytes(vault_root, rel)
            except reserved_paths.ReservedPathLeafError:
                raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}") from None
            except media_store.MediaStoreError as error:
                return _hydration_too_large(error) or JSONResponse(
                    {"code": "ORIGINAL_UNAVAILABLE", "reason": "the stored original is unavailable; retry later"},
                    status_code=503,
                )
            if not await auth_sessions.origin_session_active(who):
                return JSONResponse(
                    {"code": "UNAUTHORIZED", "reason": "missing or invalid download credential"},
                    status_code=401,
                )
            if not egress.release_allows_download(
                vault_root, rel, principal=who, snapshot=snapshot.data
            ):
                raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}")
        except VaultPathError as exc:
            if exc.code in ("NOT_FOUND", "NOT_A_FILE"):
                # Missing, withheld, reserved and folder all answer with ONE
                # body built from the request. On a case-insensitive filesystem
                # the resolver re-spells an existing path to its on-disk casing,
                # so echoing its spelling would tell a withheld file from a
                # missing one — and reveal what the withheld file is really
                # called. A folder is never a download, and naming it one would
                # confirm that a folder inside a withheld scope exists.
                return JSONResponse(
                    {"code": "NOT_FOUND", "reason": f"path does not exist: {requested}"},
                    status_code=404,
                )
            # A fixed reason: the resolver's own names the absolute server path
            # a traversal reached, or the target an escaping symlink points at.
            return JSONResponse(
                {"code": "INVALID_PATH", "reason": "path is not a vault-relative file path"},
                status_code=400,
            )
        filename = quote(abs_path.name, safe="")
        return Response(
            snapshot.data,
            media_type="application/octet-stream",
            headers={"content-disposition": f"attachment; filename*=utf-8''{filename}"},
        )

    return config
