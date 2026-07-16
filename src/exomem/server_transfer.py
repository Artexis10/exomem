"""Out-of-band upload/download routes for Exomem."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

from . import cf_access, upload_tokens
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


def register_transfer_routes(
    mcp_app: FastMCP,
    *,
    vault_root: Path,
    media_worker: Any | None,
) -> TransferConfig:
    """Register /upload and /download routes and return their config."""
    config = load_transfer_config()

    def _authorized(request: Request, *, scope: str = "upload") -> bool:
        if config.upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented, config.upload_token):
                    return True
                if upload_tokens.verify(presented, config.upload_token, scope=scope):
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
        try:
            manager = get_manager()
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

        if media_worker is not None:
            try:
                _media_processing_module().reconcile_media(
                    vault_root,
                    vault_root / result.path,
                    explicit=False,
                )
            except Exception:  # noqa: BLE001 - preserved evidence remains recoverable
                log.warning(
                    "media reconciliation failed for %s; evidence remains recoverable",
                    result.path,
                    exc_info=True,
                )
        return JSONResponse(result.as_dict(), status_code=201)

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
        try:
            abs_path, _rel = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_file=True
            )
        except VaultPathError as exc:
            status = 404 if exc.code == "NOT_FOUND" else 400
            return JSONResponse({"code": exc.code, "reason": exc.reason}, status_code=status)
        return FileResponse(abs_path, filename=abs_path.name)

    return config
