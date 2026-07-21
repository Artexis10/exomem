"""Public asset and OAuth metadata routes for the FastMCP server."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import mcp.types
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from . import runtime_readiness as runtime_readiness_module
from . import tool_surface as tool_surface_module
from .session_oauth import OAUTH_AUTHORIZATION_SCOPES, OAUTH_RESOURCE_SCOPES

_STUDIO_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self'; manifest-src 'self'; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _studio_dir() -> Path:
    return Path(__file__).parent / "studio"


def _studio_manifest() -> dict[str, str]:
    """Load the packaged allowlist as ``asset name -> media type``."""
    manifest_path = _studio_dir() / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = data.get("assets")
    if not isinstance(assets, dict) or "index.html" not in assets:
        raise ValueError("Studio asset manifest is invalid")
    clean: dict[str, str] = {}
    for name, media_type in assets.items():
        if (
            not isinstance(name, str)
            or not isinstance(media_type, str)
            or not name
            or Path(name).name != name
            or name.startswith(".")
        ):
            raise ValueError("Studio asset manifest contains an unsafe entry")
        clean[name] = media_type
    return clean


def _studio_error(message: str, *, status_code: int = 503) -> JSONResponse:
    return JSONResponse(
        {
            "error": "STUDIO_ASSETS_UNAVAILABLE",
            "message": message[:240],
            "remediation": "Reinstall Exomem and restart the service.",
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", **_STUDIO_SECURITY_HEADERS},
    )


def server_icons() -> list[mcp.types.Icon]:
    """Load the packaged SVG icon as an MCP initialize icon."""
    icon_path = Path(__file__).parent / "icon.svg"
    if not icon_path.exists():
        return []
    svg_bytes = icon_path.read_bytes()
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    return [
        mcp.types.Icon(
            src=f"data:image/svg+xml;base64,{b64}",
            mimeType="image/svg+xml",
            sizes=["any"],
        )
    ]


def register_asset_routes(mcp_app: FastMCP) -> None:
    """Serve inert public assets outside MCP auth; vault data stays behind REST."""
    asset_dir = Path(__file__).parent

    @mcp_app.custom_route("/health", methods=["GET"])
    async def _health(request: Request) -> JSONResponse:  # noqa: ARG001
        """Unauthenticated liveness probe for tunnels/orchestrators. Reports that
        the process is up, its version, and where that code was installed from —
        no vault data, no auth required.

        Install provenance is included so an operator can tell a wheel-backed
        service from one running a local checkout without inspecting the service
        manager. Host-identifying detail (interpreter path, checkout location) is
        deliberately withheld here because this route is publicly reachable; use
        the local `provenance` command for that."""
        payload: dict[str, object] = {"status": "ok", "service": "exomem"}
        try:
            from . import deploy_provenance

            payload.update(deploy_provenance.provenance(include_local=False))
        except Exception:  # noqa: BLE001 — provenance must never fail the probe
            payload["version"] = "unknown"
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @mcp_app.custom_route("/health/ready", methods=["GET"])
    async def _runtime_ready(request: Request) -> JSONResponse:  # noqa: ARG001
        """Content-free admission probe; liveness remains the separate /health route."""
        digest = getattr(mcp_app, "_exomem_tool_surface_sha256", None)
        if digest is None:
            try:
                live = await tool_surface_module.live_contract(mcp_app)
                digest = live["sha256"]
                mcp_app._exomem_tool_surface_sha256 = digest
            except Exception:  # noqa: BLE001 - readiness must stay structured
                digest = None
        snapshot = runtime_readiness_module.runtime_readiness(
            mcp_tool_surface_sha256=digest
        )
        status_code = 200 if snapshot["status"] == "ready" else 503
        return JSONResponse(
            snapshot,
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @mcp_app.custom_route("/favicon.ico", methods=["GET"])
    async def _favicon_ico(request: Request):  # noqa: ARG001
        return FileResponse(
            asset_dir / "favicon.ico",
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @mcp_app.custom_route("/favicon.svg", methods=["GET"])
    async def _favicon_svg(request: Request):  # noqa: ARG001
        return FileResponse(
            asset_dir / "icon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @mcp_app.custom_route("/studio", methods=["GET"])
    async def _studio_redirect(request: Request):  # noqa: ARG001
        return RedirectResponse("/studio/", status_code=307)

    @mcp_app.custom_route("/studio/", methods=["GET"])
    async def _studio_shell(request: Request):  # noqa: ARG001
        try:
            manifest = _studio_manifest()
            shell = _studio_dir() / "index.html"
            if not shell.is_file():
                raise FileNotFoundError("Studio shell is missing")
            return FileResponse(
                shell,
                media_type=manifest["index.html"],
                headers={"Cache-Control": "no-store", **_STUDIO_SECURITY_HEADERS},
            )
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            return _studio_error(str(exc))

    @mcp_app.custom_route("/studio/assets/{asset_path:path}", methods=["GET"])
    async def _studio_asset(request: Request):
        asset_name = request.path_params.get("asset_path", "")
        try:
            manifest = _studio_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _studio_error(str(exc))
        if asset_name == "index.html" or asset_name not in manifest:
            return _studio_error("Studio asset is not in the packaged manifest", status_code=404)
        asset = _studio_dir() / asset_name
        if not asset.is_file():
            return _studio_error(f"Studio asset {asset_name!r} is missing")
        return FileResponse(
            asset,
            media_type=manifest[asset_name],
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                **_STUDIO_SECURITY_HEADERS,
            },
        )


def register_oauth_metadata_route(
    mcp_app: FastMCP, *, base_url: str, auth_enabled: bool
) -> None:
    """Expose compatibility aliases for OAuth/OIDC discovery."""
    if not auth_enabled:
        return

    base_url = base_url.rstrip("/")
    resource_url = f"{base_url}/mcp"
    issuer_url = f"{base_url}/"

    @mcp_app.custom_route("/.well-known/openid-configuration", methods=["GET"])
    async def _openid_configuration(request: Request) -> JSONResponse:  # noqa: ARG001
        # Some MCP clients probe the OIDC alias after a successful OAuth token
        # exchange. Exomem uses OAuth (not ID tokens), so return the same RFC
        # 8414 authorization-server metadata shape as the canonical endpoint.
        return JSONResponse(
            {
                "issuer": issuer_url,
                "authorization_endpoint": f"{base_url}/authorize",
                "token_endpoint": f"{base_url}/token",
                "registration_endpoint": f"{base_url}/register",
                "scopes_supported": list(OAUTH_AUTHORIZATION_SCOPES),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                    "private_key_jwt",
                    "none",
                ],
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @mcp_app.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def _oauth_protected_resource_bare(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "resource": resource_url,
                "authorization_servers": [issuer_url],
                "scopes_supported": list(OAUTH_RESOURCE_SCOPES),
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )
