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
from starlette.middleware import Middleware as ASGIMiddleware

from . import commands as commands_module
from . import guards
from .server_assets import (
    register_asset_routes,
    register_oauth_metadata_route,
    server_icons,
)
from .server_auth import (  # noqa: F401 - re-exported for compatibility
    SingleUserGitHubVerifier,
    build_oauth,
)
from .server_rest import register_rest_facade
from .server_runtime import initialize_runtime
from .server_transfer import register_transfer_routes
from .server_transport import PrimeMcpSSEMiddleware

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
            if tool_name in ("edit", "edit_memory"):
                for item in args.get("edits") or []:
                    if isinstance(item, dict):
                        guards.guard_text_content(
                            item.get("new_string"),
                            tool=tool_name,
                            field="edits[].new_string",
                        )

        extras = _find_call_summary(context.message) if tool_name == "ask_memory" else ""
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
    from .writer_lease import start_server_lifecycle

    start_server_lifecycle()
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

    for cmd in commands_module.product_commands_for("mcp", expose_tier2=expose_tier2):
        if cmd.name in commands_module.HAND_REGISTERED_EXCEPTIONS:
            continue
        injected = (
            (runtime.vault_root, runtime.source_schema)
            if cmd.needs_schema
            else (runtime.vault_root,)
        )
        description = cmd.doc
        if cmd.name == "remember":
            description = commands_module.remember_description(runtime.project_keys_hint)
        mcp.tool(
            commands_module.bind_vault(
                cmd.leaf, *injected, name=cmd.name, description=description, command=cmd
            ),
            annotations=cmd.mcp_annotations,
        )

    if _legacy_mcp_compat_enabled():
        _register_legacy_mcp_tools(
            mcp,
            vault_root=runtime.vault_root,
            source_schema=runtime.source_schema,
            expose_tier2=expose_tier2,
            project_keys_hint=runtime.project_keys_hint,
        )

    return mcp


def _legacy_mcp_compat_enabled() -> bool:
    """Register canonical MCP leaf names for stale connector caches.

    The product MCP surface is the default. This opt-in exists for clients that
    cached the old tool list and still call names such as `note` or
    `create_file` after a service upgrade. It is intentionally environment
    gated so fresh clients do not see the primitive leaves unless an operator
    chooses compatibility over a smaller tool list.
    """
    return os.environ.get("EXOMEM_MCP_LEGACY_COMPAT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _register_legacy_mcp_tools(
    mcp: FastMCP,
    *,
    vault_root: Path,
    source_schema: object,
    expose_tier2: bool,
    project_keys_hint: str,
) -> None:
    product_names = {
        c.name for c in commands_module.product_commands_for("mcp", expose_tier2=expose_tier2)
    }
    legacy = list(commands_module.commands_for("mcp", expose_tier2=expose_tier2))
    legacy += [c for c in commands_module.COMMANDS if c.name == "note"]

    for cmd in legacy:
        if cmd.name in product_names:
            continue
        if "mcp" not in cmd.surfaces and cmd.name != "note":
            continue
        injected = (
            (vault_root, source_schema)
            if cmd.needs_schema
            else (vault_root,)
        )
        description = cmd.doc
        if cmd.name == "note":
            description = commands_module.remember_description(project_keys_hint)
        mcp.tool(
            commands_module.bind_vault(
                cmd.leaf,
                *injected,
                name=cmd.name,
                description="[Deprecated compatibility alias; prefer product commands.] "
                + description,
                command=cmd,
            ),
            annotations=cmd.mcp_annotations,
        )


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
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            middleware=[ASGIMiddleware(PrimeMcpSSEMiddleware)],
        )
