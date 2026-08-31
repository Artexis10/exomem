"""Out-of-band upload/download routes for Exomem."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import cf_access, reserved_paths, upload_tokens
from .governance import egress
from .governance import principal as principal_module
from .vault import VaultPathError, resolve_under_vault

DEFAULT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
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
) -> Any:
    """Capture an out-of-band upload as a Source, under vault authority.

    The bytes are spooled to a private temporary file first because `add` copies
    from a path: it writes the artifact and its page in one operation, and a
    half-consumed request stream cannot be replayed if that operation refuses.
    """
    import tempfile

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
    )


def download_principal(
    request: Request, config: TransferConfig
) -> principal_module.RequestPrincipal:
    """Canonical audience for a `/download` caller (design D5).

    Two credentials reach this route and they are NOT the same human:
    `EXOMEM_UPLOAD_TOKEN` (and tokens minted from it) is the vault owner's own
    key, while a Cloudflare Access assertion carries a real third-party
    identity. Resolving both to `owner` would let a CF-Access downloader
    inherit the owner's ceiling — so the CF claims are folded into the same id
    space `server_rest._rest_principal` uses, and a grant authored for that
    human on MCP or REST applies here too.

    Module-level (not a closure over the route) so the resolution contract is
    directly testable without reaching through a registered endpoint.
    """
    if config.upload_token is not None:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            presented = header[len("Bearer ") :].strip()
            if secrets.compare_digest(
                presented, config.upload_token
            ) or upload_tokens.verify(presented, config.upload_token, scope="download"):
                return principal_module.owner_principal(surface="transfer")
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
        every upload landed before lanes existed.
        """
        if config.upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if not secrets.compare_digest(presented, config.upload_token):
                    lane = upload_tokens.lane_for_token(presented, config.upload_token)
                    if lane is not None:
                        return lane
        return "evidence"

    def _authorized(request: Request, *, scope: str = "upload") -> bool:
        if config.upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented, config.upload_token):
                    return True
                if upload_tokens.verify(presented, config.upload_token, scope=scope):
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

    async def _upload_admitted(request: Request) -> JSONResponse:
        from .cli_ops import OpError, error_dict, http_status_for
        from .writer_lease import get_manager

        try:
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
        preserve_module = _preserve_module()
        lane = _upload_lane(request)
        try:
            manager = get_manager()
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
                    max_bytes=config.upload_max_bytes,
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
                    max_bytes=config.upload_max_bytes,
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

    @mcp_app.custom_route("/upload", methods=["POST"])
    async def _upload(request: Request) -> JSONResponse:
        if not config.enabled:
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
        from .governance import consolidation_runtime

        try:
            with consolidation_runtime.admit_upload(vault_root):
                return await _upload_admitted(request)
        except OpError as exc:
            error = error_dict(exc)
            return JSONResponse(
                {"code": error["code"], "reason": error["message"]},
                status_code=http_status_for(error["code"]),
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
        from .cli_ops import OpError, error_dict, http_status_for
        from .governance import consolidation_runtime

        try:
            with consolidation_runtime.admit_transfer(vault_root):
                path = request.query_params.get("path", "")
                if not path.strip():
                    return JSONResponse(
                        {
                            "code": "INVALID_PATH",
                            "reason": "query param `path` (vault-relative) is required",
                        },
                        status_code=400,
                    )
                try:
                    abs_path, rel = resolve_under_vault(
                        vault_root, path, must_exist=True, must_be_file=True
                    )
                    # A download hands over complete bytes, so only full disclosure
                    # authorizes it. Refusal remains byte-identical to absence.
                    if not egress.release_allows_download(
                        vault_root,
                        rel,
                        principal=download_principal(request, config),
                    ):
                        raise VaultPathError("NOT_FOUND", f"path does not exist: {rel}")
                    try:
                        snapshot = reserved_paths.read_generic_bytes(vault_root, rel)
                    except reserved_paths.ReservedPathLeafError:
                        raise VaultPathError(
                            "NOT_FOUND", f"path does not exist: {rel}"
                        ) from None
                except VaultPathError as exc:
                    status = 404 if exc.code == "NOT_FOUND" else 400
                    return JSONResponse(
                        {"code": exc.code, "reason": exc.reason}, status_code=status
                    )
                filename = quote(abs_path.name, safe="")
                return Response(
                    snapshot.data,
                    media_type="application/octet-stream",
                    headers={
                        "content-disposition": f"attachment; filename*=utf-8''{filename}"
                    },
                )
        except OpError as exc:
            error = error_dict(exc)
            return JSONResponse(
                {"code": error["code"], "reason": error["message"]},
                status_code=http_status_for(error["code"]),
            )

    return config
