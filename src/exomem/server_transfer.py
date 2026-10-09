"""Out-of-band upload/download routes for Exomem."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import cf_access, local_ingress, reserved_paths, upload_sessions, upload_tokens
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
#: tus 1.0 (tus.io resumable-upload protocol): the version, and the extensions served.
_TUS_VERSION = "1.0.0"
_TUS_EXTENSIONS = "creation,expiration,termination"
_TUS_PATCH_CONTENT_TYPE = "application/offset+octet-stream"
#: The session secret travels only in this header, never in a URL.
_SESSION_SECRET_HEADER = "Exomem-Upload-Secret"
_SESSION_WRITE_BYTES = 1024 * 1024
log = logging.getLogger(__name__)


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
    """Run the complete upload read-plan-write path under vault authority."""
    with manager.mutation_guard(vault_root):
        return preserve_stream(vault_root, **kwargs)


def _preserve_members_under_guard(manager: Any, vault_root: Path, **kwargs: Any) -> Any:
    """Expand an archive's members outside the guard; commit its manifest under it."""
    from . import archive_members

    return archive_members.preserve_members(
        vault_root, guard=lambda: manager.mutation_guard(vault_root), **kwargs
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
        with manager.mutation_guard(vault_root):
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
    with manager.mutation_guard(vault_root):
        return media_processing.reconcile_media(vault_root, binary_path, explicit=False)


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


def register_transfer_routes(
    mcp_app: FastMCP,
    *,
    vault_root: Path,
    media_worker: Any | None,
) -> TransferConfig:
    """Register /upload and /download routes and return their config."""
    config = load_transfer_config()

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
        from .writer_lease import get_manager

        max_bytes = (
            config.local_upload_max_bytes
            if local_ingress.current_grant() is not None
            else config.upload_max_bytes
        )
        declared = request.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > max_bytes + _FORM_OVERHEAD_BYTES:
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
        raw_flag = str(form.get("raw_protection") or "").strip()
        if raw_flag not in ("", "1", "true"):
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "`raw_protection` must be 1"}, status_code=400
            )
        raw_protection = bool(raw_flag)
        archive = str(form.get("archive") or "").strip()
        if archive and archive != "members":
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "`archive` must be members"}, status_code=400
            )
        if archive and (str(form.get("hold") or "").strip() or _upload_lane(request) == "source"):
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "archive=members preserves Evidence directly"},
                status_code=400,
            )
        if str(form.get("hold") or "").strip():
            # `preserve-attachment-originals`: hold the bytes for a file-handle
            # command instead of preserving them. Only a verified local grant
            # can hold; nothing reaches the vault here.
            from . import held_uploads

            if str(form.get("hold")).strip() not in ("1", "true"):
                return JSONResponse(
                    {"code": "INVALID_UPLOAD", "reason": "`hold` must be 1"}, status_code=400
                )
            if raw_protection:
                return JSONResponse(
                    {
                        "code": "INVALID_UPLOAD",
                        "reason": "a held upload takes `raw_protection` when its command redeems it",
                    },
                    status_code=400,
                )
            from .client_artifacts import MAX_FILE_BYTES

            try:
                held = await run_in_threadpool(
                    held_uploads.hold,
                    vault_root,
                    upload.file,
                    lane=str(form.get("lane") or "evidence").strip(),
                    filename=filename,
                    content_type=getattr(upload, "content_type", None),
                    # A hold is only worth what its redeeming command can fetch.
                    max_bytes=min(max_bytes, MAX_FILE_BYTES),
                )
            except held_uploads.HeldUploadError as exc:
                return JSONResponse(
                    {"code": exc.code, "reason": exc.reason},
                    status_code=413 if exc.code == "TOO_LARGE" else 400,
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
                status_code=413 if exc.code == archive_members.TOO_LARGE else 400,
            )
        except (OpError, ValueError) as exc:
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
                vault_root / result.path,
            )
        except Exception:  # noqa: BLE001 - preserved evidence remains recoverable
            log.warning(
                "media reconciliation failed for %s; evidence remains recoverable",
                result.path,
                exc_info=True,
            )
        return JSONResponse(result.as_dict(), status_code=201)

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
        headers = {"Tus-Resumable": _TUS_VERSION, "Cache-Control": "no-store", **extra}
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
                    "Tus-Version": _TUS_VERSION,
                    "Tus-Extension": _TUS_EXTENSIONS,
                    "Tus-Max-Size": str(session_max_bytes),
                }
            ),
        )

    def _tus_version_refused(request: Request) -> JSONResponse | None:
        if request.headers.get("tus-resumable") == _TUS_VERSION:
            return None
        return _tus_refusal(
            "TUS_VERSION_UNSUPPORTED",
            f"`Tus-Resumable: {_TUS_VERSION}` is required",
            412,
            **{"Tus-Version": _TUS_VERSION},
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

    def _commit_session(part: Path, record: dict) -> dict:
        """Preserve a verified session's bytes exactly as `/upload` would."""
        from . import archive_members
        from .cli_ops import OpError, error_dict
        from .writer_lease import get_manager

        preserve_module = _preserve_module()
        target = record["target"]
        manager = get_manager()
        try:
            with part.open("rb") as stream:
                if target.get("archive") == "members":
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
        except (preserve_module.PreserveError, archive_members.ArchiveError) as exc:
            raise upload_sessions.CommitFailed(exc.code, exc.reason) from exc
        except (OpError, ValueError) as exc:
            error = error_dict(exc)
            raise upload_sessions.CommitFailed(error["code"], error["message"]) from exc
        try:
            _reconcile_under_guard(manager, vault_root, vault_root / result.path)
        except Exception:  # noqa: BLE001 - preserved evidence remains recoverable
            log.warning("media reconciliation failed for %s; evidence remains recoverable",
                        result.path, exc_info=True)
        return result.as_dict()

    try:
        # Service start: drop expired sessions; finish commits a stop interrupted.
        for pending in upload_sessions.startup_sweep(vault_root):
            upload_sessions.start_commit(pending, _commit_session)
    except (OSError, ValueError):
        log.warning("upload sessions could not be swept at start", exc_info=True)

    @mcp_app.custom_route("/upload/sessions", methods=["POST", "OPTIONS"])
    async def _create_upload_session(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _tus_options()
        if not config.enabled and local_ingress.current_grant() is None:
            return _tus_refusal("UPLOAD_DISABLED", "uploads are off", 503)
        if not _authorized(request):
            return _tus_refusal("UNAUTHORIZED", "missing or invalid upload credential", 401)
        if (refused := _tus_version_refused(request)) is not None:
            return refused
        declared = request.headers.get("upload-length", "")
        metadata = _tus_metadata(request.headers.get("upload-metadata", ""))
        if not (declared.isascii() and declared.isdigit()) or metadata is None:
            return _tus_refusal(
                "INVALID_UPLOAD", "`Upload-Length` and a well-formed `Upload-Metadata` are required", 400
            )
        preserve_module = _preserve_module()
        raw_flag = metadata.get("raw_protection", "").strip()
        target = {
            "filename": metadata.get("filename", "").strip(),
            "scope": metadata.get("scope", "").strip(),
            "category": metadata.get("category", "").strip(),
            "description": metadata.get("description", "").strip() or None,
            "raw_protection": bool(raw_flag),
            "archive": metadata.get("archive", "").strip() or None,
        }
        refusals = [
            refusal
            for refusal in (
                preserve_module.destination_segment_refusal(target["scope"], field="scope"),
                preserve_module.destination_segment_refusal(target["category"], field="category"),
                None if target["filename"] else "`filename` is required",
                None
                if target["archive"] is None or target["archive"] == "members"
                else "`archive` must be members",
                # nosemgrep: ep-word-membership -- `/upload` fixes these flag spellings.
                None if raw_flag in ("", "1", "true") else "`raw_protection` must be 1",
                None
                if _upload_lane(request) == "evidence"
                else "an upload session preserves Evidence",
            )
            if refusal
        ]
        if refusals:
            return _tus_refusal("INVALID_UPLOAD", "; ".join(refusals), 400)
        try:
            if target["raw_protection"] or target["archive"]:
                preserve_module.validate_raw_capture(target["filename"], raw_protection=True)
            session, secret = await run_in_threadpool(
                upload_sessions.create,
                vault_root,
                binding=_credential_binding(request),
                length=int(declared),
                sha256=metadata.get("sha256", "").strip(),
                target=target,
                max_bytes=session_max_bytes,
            )
        except preserve_module.PreserveError as exc:
            return _tus_refusal(exc.code, exc.reason, 400)
        except upload_sessions.SessionError as exc:
            return _tus_refusal(exc.code, exc.reason, exc.status)
        upload_sessions.start_commit(session, _commit_session)
        return Response(
            status_code=201,
            headers=_tus_headers(
                session,
                **{
                    "Location": f"/upload/sessions/{session.id}",
                    _SESSION_SECRET_HEADER: secret,
                },
            ),
        )

    @mcp_app.custom_route(
        "/upload/sessions/{session_id}", methods=["HEAD", "PATCH", "DELETE", "GET", "OPTIONS"]
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
                request.headers.get(_SESSION_SECRET_HEADER),
            )
        except upload_sessions.SessionError as exc:
            return _tus_refusal(exc.code, exc.reason, exc.status)
        # A commit that a stop interrupted resumes the next time its session is read.
        upload_sessions.start_commit(session, _commit_session)
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
        offset = request.headers.get("upload-offset", "")
        length = request.headers.get("content-length", "")
        if not (offset.isascii() and offset.isdigit()) or (length and not (length.isascii() and length.isdigit())):
            return _tus_refusal("INVALID_UPLOAD", "`Upload-Offset` is required", 400)
        try:
            patch = await run_in_threadpool(
                upload_sessions.Patch, session, int(offset), int(length) if length else None
            )
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
            upload_sessions.start_commit(session, _commit_session)
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
<label>Scope</label><input name=scope value="{_attr('scope')}" placeholder="e.g. Yolo" required>
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
        path = request.query_params.get("path", "")
        if not path.strip():
            return JSONResponse(
                {"code": "INVALID_PATH", "reason": "query param `path` (vault-relative) is required"},
                status_code=400,
            )
        # Spelled exactly as the resolver spells a path it cannot find.
        requested = path.strip().replace("\\", "/").lstrip("/")
        try:
            abs_path, rel = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_file=True
            )
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
                vault_root, rel, principal=download_principal(request, config)
            ):
                raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}")
            try:
                snapshot = reserved_paths.read_generic_bytes(vault_root, rel)
            except reserved_paths.ReservedPathLeafError:
                raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}") from None
            if not egress.release_allows_download(
                vault_root, rel, principal=download_principal(request, config), snapshot=snapshot.data
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
