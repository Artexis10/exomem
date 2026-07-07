"""FastMCP server composition root for Exomem.

The transport-specific wiring lives here. Startup/runtime setup, OAuth, public
asset routes, transfer routes, and the REST facade are split into sibling
modules so this file stays focused on composing the server and registering MCP
tools from the command registry.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext

from . import commands as commands_module
from . import guards, upload_tokens
from .server_assets import (
    register_asset_routes,
    register_oauth_metadata_route,
    server_icons,
)
from .server_auth import SingleUserGitHubVerifier, build_oauth
from .server_rest import register_rest_facade
from .server_runtime import initialize_runtime
from .server_transfer import register_transfer_routes

log = logging.getLogger(__name__)
_call_log = logging.getLogger("exomem.calls")

_GUARDED_WRITE_FIELDS = commands_module.GUARDED_WRITE_FIELDS
_link_summary = commands_module._link_summary


class CallTraceMiddleware(Middleware):
    """Per-call traceability: log every tool invocation with name + duration."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = _extract_tool_name(context.message)
        guarded_fields = _GUARDED_WRITE_FIELDS.get(tool_name)
        if guarded_fields:
            args = _extract_tool_args(context.message)
            for field in guarded_fields:
                guards.guard_text_content(args.get(field), tool=tool_name, field=field)
            if tool_name == "edit":
                for item in args.get("edits") or []:
                    if isinstance(item, dict):
                        guards.guard_text_content(
                            item.get("new_string"),
                            tool=tool_name,
                            field="edits[].new_string",
                        )

        extras = _find_call_summary(context.message) if tool_name == "find" else ""
        _call_log.info(f"event=tool_start tool={tool_name}{extras}")
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            dur = round((time.perf_counter() - t0) * 1000, 2)
            _call_log.info(f"event=tool_success tool={tool_name} duration_ms={dur}{extras}")
            return result
        except Exception as exc:
            dur = round((time.perf_counter() - t0) * 1000, 2)
            _call_log.error(
                f"event=tool_error tool={tool_name} duration_ms={dur} "
                f"err={type(exc).__name__}{extras}"
            )
            raise


def _extract_tool_name(message) -> str:
    """Pull the tool name out of a tools/call request payload, defensively."""
    for accessor in (
        lambda m: m.params.name,
        lambda m: m.name,
        lambda m: m["params"]["name"],
        lambda m: m["name"],
    ):
        try:
            value = accessor(message)
            if value:
                return str(value)
        except (AttributeError, KeyError, TypeError):
            continue
    return "?"


def _extract_tool_args(message) -> dict:
    """Pull the tool-call arguments out of a request payload, defensively."""
    for accessor in (
        lambda m: m.params.arguments,
        lambda m: m["params"]["arguments"],
        lambda m: m.arguments,
    ):
        try:
            value = accessor(message)
            if isinstance(value, dict):
                return value
        except (AttributeError, KeyError, TypeError):
            continue
    return {}


def _find_call_summary(message) -> str:
    """One-line summary of find()'s key args for the service call log."""
    args = _extract_tool_args(message)
    if not args:
        return ""
    query = str(args.get("query", ""))
    if len(query) > 120:
        query = query[:117] + "..."
    query = query.replace('"', "'")
    mode = args.get("mode", "hybrid")
    scope = args.get("scope", "kb")
    return f' query="{query}" mode={mode} scope={scope}'


def build_server(*, require_auth: bool) -> FastMCP:
    """Construct and return the FastMCP app, ready to run."""
    runtime = initialize_runtime(load_dotenv_func=load_dotenv)
    auth = build_oauth(require_auth=require_auth, base_url=runtime.base_url)

    mcp = FastMCP("exomem", auth=auth, icons=server_icons())
    mcp.add_middleware(CallTraceMiddleware())

    register_asset_routes(mcp)
    register_oauth_metadata_route(
        mcp, base_url=runtime.base_url, auth_enabled=auth is not None
    )
    transfer_config = register_transfer_routes(
        mcp, vault_root=runtime.vault_root, media_worker=runtime.media_worker
    )
    expose_tier2 = register_rest_facade(
        mcp,
        vault_root=runtime.vault_root,
        source_schema=runtime.source_schema,
        transfer_config=transfer_config,
    )

    for cmd in commands_module.commands_for("mcp", expose_tier2=expose_tier2):
        if cmd.name in commands_module.HAND_REGISTERED_EXCEPTIONS:
            continue
        injected = (
            (runtime.vault_root, runtime.source_schema)
            if cmd.needs_schema
            else (runtime.vault_root,)
        )
        mcp.tool(
            commands_module.bind_vault(
                cmd.leaf, *injected, name=cmd.name, description=cmd.doc
            ),
            annotations=cmd.mcp_annotations,
        )

    mcp.tool(
        commands_module.bind_vault(
            commands_module.op_note,
            runtime.vault_root,
            name="note",
            description=commands_module.note_description(runtime.project_keys_hint),
        ),
        annotations=commands_module.mcp_tool_annotations("note", read_only=False),
    )

    @mcp.tool(
        annotations=commands_module.mcp_tool_annotations(
            "mint_upload_token", read_only=False
        )
    )
    def mint_upload_token() -> dict:
        """Mint a short-lived bearer token for the HTTP `/upload` endpoint.

        This is how you file BINARY evidence (images, PDFs, any file) from a
        claude.ai web session — the bytes travel out-of-band, never through the
        model. Steps:

        1. Call this tool → `{token, ttl_seconds, upload_url[, large_upload_url]}`.
        2. In the code sandbox, multipart-POST the user's ATTACHED files to
           `upload_url` with header `Authorization: Bearer <token>` and form
           fields `file`, `scope`, `category` (optional `filename`, `description`,
           `text`). Files must be ATTACHMENTS — inline-pasted images never reach
           the sandbox disk and cannot be sent.
        3. To make the binary SEARCHABLE, extract its text in the sandbox (OCR an
           image/scan, read a PDF, transcribe audio/video) and pass it as the
           `text` field above — it lands in an embedded sidecar so the otherwise-
           opaque file is findable by its content.

        **Large files (>100 MB):** `upload_url` is fronted by Cloudflare, which
        413s any body over ~100 MB at the edge. If a file exceeds ~100 MB (or a
        POST to `upload_url` returns 413), send it to `large_upload_url` instead
        when that field is present — same token, same form fields, an alternate
        host with no edge cap. If `large_upload_url` is absent, only `upload_url`
        is available and >100 MB must go desk-side.

        The token is Evidence-write only and expires after `ttl_seconds`; the
        server's long-lived secret never leaves the server.

        Returns: {token, ttl_seconds, upload_url, large_upload_url?}.
        Raises: UPLOAD_DISABLED if the server has no upload token configured.
        """
        return upload_tokens.mint_for_endpoint(
            transfer_config.upload_token,
            runtime.base_url,
            large_base_url=transfer_config.large_upload_base,
        )

    @mcp.tool(
        annotations=commands_module.mcp_tool_annotations(
            "mint_download_token", read_only=True
        )
    )
    def mint_download_token() -> dict:
        """Mint a short-lived bearer token for the HTTP `/download` endpoint.

        Use to PULL a vault file into the sandbox — a dataset, an evidence
        binary, a scan — so you can analyze it. Call this, then from the code
        sandbox GET `download_url?path=<vault-relative path>` with header
        `Authorization: Bearer <token>`. Read-only; the token is download-scoped
        (can't write) and expires after `ttl_seconds`.

        Returns: {token, ttl_seconds, download_url}.
        Raises: DOWNLOAD_DISABLED if the server has no upload token configured.
        """
        return upload_tokens.mint_for_endpoint(
            transfer_config.upload_token, runtime.base_url, scope="download"
        )

    return mcp


def run(
    *,
    transport: str = "stdio",
    host: str | None = None,
    port: int = 8765,
    log_dir: Path | None = None,
) -> None:
    """CLI entry: configure logging, build the server, run it."""
    from .logging_config import configure_logging, resolve_log_dir

    configure_logging(log_dir if log_dir is not None else resolve_log_dir())

    require_auth = transport != "stdio"
    mcp = build_server(require_auth=require_auth)

    if transport == "stdio":
        log.info("exomem starting on stdio")
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("EXOMEM_HOST") or host or "127.0.0.1"
        log.info("exomem starting on %s host=%s port=%s", transport, host, port)
        mcp.run(transport=transport, host=host, port=port)
