"""Public asset and OAuth metadata routes for the FastMCP server."""

from __future__ import annotations

import base64
from pathlib import Path

import mcp.types
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse


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
    """Serve public favicon assets outside MCP auth."""
    asset_dir = Path(__file__).parent

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


def register_oauth_metadata_route(
    mcp_app: FastMCP, *, base_url: str, auth_enabled: bool
) -> None:
    """Mirror OAuth protected-resource metadata at the bare well-known path."""
    if not auth_enabled:
        return

    resource_url = f"{base_url}/mcp"
    issuer_url = f"{base_url}/"

    @mcp_app.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def _oauth_protected_resource_bare(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "resource": resource_url,
                "authorization_servers": [issuer_url],
                "scopes_supported": [],
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )
