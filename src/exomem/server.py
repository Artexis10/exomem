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
    HostedCellTokenVerifier,
    SingleUserGitHubVerifier,
    build_oauth,
)
from .server_hosted import register_hosted_routes
from .server_rest import register_rest_facade
from .server_runtime import initialize_runtime
from .server_transfer import register_transfer_routes
from .server_transport import PrimeMcpSSEMiddleware

log = logging.getLogger(__name__)
_call_log = logging.getLogger("exomem.calls")

_GUARDED_WRITE_FIELDS = commands_module.GUARDED_WRITE_FIELDS
_link_summary = commands_module._link_summary


class ExomemFastMCP(FastMCP):
    """FastMCP with stateless POST plus authenticated GET/SSE compatibility.

    FastMCP normally omits GET from a stateless Streamable HTTP route because
    stateless servers do not need server-initiated notifications. Codex and
    Claude still open the optional GET/SSE channel, though. The MCP SDK's
    stateless transport supports that channel without allocating a session ID,
    so expose the method on the same OAuth-protected route.
    """

    def http_app(self, *args, stateless_http=None, **kwargs):
        app = super().http_app(*args, stateless_http=stateless_http, **kwargs)
        if stateless_http:
            endpoint_found = False
            for route in app.routes:
                methods = getattr(route, "methods", None)
                if (
                    getattr(route, "path", None) == app.state.path
                    and methods is not None
                    and {"POST", "DELETE"}.issubset(methods)
                ):
                    methods.add("GET")
                    endpoint_found = True
                    break
            if not endpoint_found:
                raise RuntimeError("FastMCP stateless endpoint route was not found")
        return app


class CallTraceMiddleware(Middleware):
    """Per-call traceability: log every tool invocation with name + duration."""

    def __init__(self, *, hosted: bool = False) -> None:
        self.hosted = hosted

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

        extras = (
            _find_call_summary(context.message)
            if tool_name == "ask_memory" and not self.hosted
            else ""
        )
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
    hosted = runtime.hosted_config is not None
    if hosted:
        assert runtime.hosted_config is not None
        assert runtime.hosted_lifecycle is not None
        security_authority = runtime.hosted_security_authority
        if runtime.hosted_config.requires_dynamic_security and security_authority is None:
            raise RuntimeError("hosted security authority is required for a v2 cell")
        auth = HostedCellTokenVerifier(
            runtime.hosted_config,
            authenticator=security_authority,
        )
        mcp = ExomemFastMCP("exomem", auth=auth)
        mcp.add_middleware(CallTraceMiddleware(hosted=True))
        expose_tier2 = not os.environ.get("EXOMEM_DISABLE_TIER2")
        register_hosted_routes(
            mcp,
            config=runtime.hosted_config,
            lifecycle=runtime.hosted_lifecycle,
            source_schema=runtime.source_schema,
            expose_tier2=expose_tier2,
            private_authenticator=security_authority,
            transfer_security_authority=security_authority,
        )
    else:
        auth = build_oauth(require_auth=require_auth, base_url=runtime.base_url)
        mcp = ExomemFastMCP("exomem", auth=auth, icons=server_icons())
        mcp.add_middleware(CallTraceMiddleware())

        register_asset_routes(mcp)
        register_oauth_metadata_route(mcp, base_url=runtime.base_url, auth_enabled=auth is not None)
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
            mcp.tool(
                commands_module.bind_vault(
                    cmd.leaf,
                    *injected,
                    name=cmd.name,
                    description=description,
                    command=cmd,
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

        register_adoption_mcp(mcp, vault_root=runtime.vault_root)

    # Retain hosted lifetime ownership for exactly as long as the composed
    # server object can serve requests. Process exit releases the underlying FD.
    mcp._exomem_server_runtime = runtime
    return mcp


def _newest_open_adoption_run(vault_root: Path) -> dict | None:
    """The most recent adoption run that is neither `done` nor `cancelled`."""
    from .adoption_run import AdoptionRunStore

    try:
        rows = AdoptionRunStore(vault_root).list_runs()
    except Exception:  # noqa: BLE001 - discovery is best-effort; soft-fail to None
        return None
    for row in rows:  # list_runs is newest-first
        if row.get("phase") not in ("done", "cancelled"):
            return row
    return None


def register_adoption_mcp(mcp: FastMCP, *, vault_root: Path) -> None:
    """Register the progressive-enhancement Adoption Studio prompt and resources.

    These ride on top of the tool surface (the real handoff backbone) and are
    additive: a zero-argument `continue_adoption` prompt that infers the newest
    open run and surfaces the copyable handoff, plus MCP resources that read an
    adoption run by its stable ref. Everything soft-fails when no run exists so a
    fresh vault registers cleanly.

    Deviation (noted): resources/list does not emit a per-run `list_changed`
    notification on run creation — that would require `adoption_run` to publish an
    event into the server, coupling the engine to the transport (and touching
    forbidden internals). The always-fresh `exomem://adoption/runs` collection
    resource provides discovery instead; each read reflects current runs.
    """
    from . import adoption_run as adoption_run_module

    @mcp.prompt(
        name="continue_adoption",
        description=(
            "Resume the newest open Adoption Studio run: loads the bounded, "
            "read-only work item and hands you the copyable prompt to submit "
            "structured proposals. Takes no arguments — the server infers the run."
        ),
    )
    def continue_adoption() -> str:
        row = _newest_open_adoption_run(vault_root)
        if row is None:
            return (
                "No open Exomem adoption run was found. Start one with "
                'adoption_studio(action="start", path="<folder>").'
            )
        try:
            doc = adoption_run_module.status(vault_root, run_id=row["run_id"])
            return doc["handoff"]["prompt_text"]
        except Exception:  # noqa: BLE001 - fall back to a minimal, still-useful prompt
            run_id = row.get("run_id", "")
            return (
                f"Continue my Exomem adoption run {run_id}. Call "
                f'adoption_studio(action="work-item", run_id="{run_id}") to load the '
                "bounded, read-only context, then submit structured proposals via "
                f'adoption_studio(action="propose", run_id="{run_id}").'
            )

    @mcp.resource(
        "exomem://adoption/runs",
        name="adoption_runs",
        description="Open Adoption Studio runs (newest first), read on demand.",
        mime_type="application/json",
    )
    def adoption_runs() -> dict:
        from .adoption_run import AdoptionRunStore

        try:
            rows = AdoptionRunStore(vault_root).list_runs()
        except Exception:  # noqa: BLE001
            rows = []
        open_rows = [r for r in rows if r.get("phase") not in ("done", "cancelled")]
        return {"runs": open_rows}

    @mcp.resource(
        "exomem://adoption/run/{run_id}",
        name="adoption_run",
        description="One durable Adoption Studio run document, read by its stable id.",
        mime_type="application/json",
    )
    def adoption_run_resource(run_id: str) -> dict:
        try:
            return adoption_run_module.status(vault_root, run_id=run_id)
        except adoption_run_module.AdoptionRunError as exc:
            return {"error": {"code": exc.code, "reason": exc.reason}, "run_id": run_id}


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
        injected = (vault_root, source_schema) if cmd.needs_schema else (vault_root,)
        description = cmd.doc
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
            # Remote clients may be routed to another replica or outlive this
            # process.  A process-local Mcp-Session-Id turns either event into
            # a 404/reconnect cascade; each Exomem operation is already an
            # independently authenticated request, so use FastMCP's transport
            # mode designed for horizontally scaled/restartable servers.
            stateless_http=True,
        )
