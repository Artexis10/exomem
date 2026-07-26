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
from copy import copy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from starlette.middleware import Middleware as ASGIMiddleware

from . import capabilities, edit_operations, guards, multi_edit
from . import commands as commands_module
from .access_log import AccessLogMiddleware
from .edge_ingress import EdgeIngressMiddleware
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
        from .command_surface import mcp_request_context, mcp_request_id, pop_tool_failure

        tool_name = _extract_tool_name(context.message)
        if tool_name == "edit_memory":
            context = _translated_edit_context(context)
        request_id = mcp_request_id()
        with mcp_request_context(request_id) as call_token:
            guarded_fields = _GUARDED_WRITE_FIELDS.get(tool_name)
            if guarded_fields:
                args = _extract_tool_args(context.message)
                for field in guarded_fields:
                    guards.guard_text_content(args.get(field), tool=tool_name, field=field)
                if tool_name == "edit_memory":
                    operation = args.get("operation") or {}
                    for field in ("new_body", "new_string"):
                        guards.guard_text_content(
                            operation.get(field), tool=tool_name, field=f"operation.{field}"
                        )
                    batch_items = operation.get("edits") or []
                    for item in batch_items:
                        normalized = multi_edit.normalize_edit_item(item)
                        guards.guard_text_content(
                            normalized.get("new_string"),
                            tool=tool_name,
                            field="operation.edits[].new_string",
                        )
                elif tool_name == "edit":
                    for item in args.get("edits") or []:
                        normalized = multi_edit.normalize_edit_item(item)
                        guards.guard_text_content(
                            normalized.get("new_string"),
                            tool=tool_name,
                            field="edits[].new_string",
                        )

            extras = (
                _find_call_summary(context.message)
                if tool_name == "ask_memory" and not self.hosted
                else ""
            )
            event_prefix = "hosted_call kind=" if self.hosted else ""
            _call_log.info(
                f"event={event_prefix}tool_start tool={tool_name} "
                f"request_id={request_id}{extras}"
            )
            t0 = time.perf_counter()
            try:
                result = await call_next(context)
                dur = round((time.perf_counter() - t0) * 1000, 2)
                # A ContextVar set inside the synchronous tool wrapper (which
                # runs in FastMCP's anyio threadpool) does not propagate back
                # here, so the wrapper leaves this bounded, locked breadcrumb
                # instead, keyed by this call's unique token. Pop
                # unconditionally: a call the wrapper never failed simply pops
                # `None`.
                failure = pop_tool_failure(call_token)
                if failure is not None:
                    _call_log.info(
                        f"event={event_prefix}tool_failure tool={tool_name} "
                        f"request_id={request_id} duration_ms={dur} "
                        f"code={failure.get('code')}{extras}"
                    )
                else:
                    _call_log.info(
                        f"event={event_prefix}tool_success tool={tool_name} "
                        f"request_id={request_id} duration_ms={dur}{extras}"
                    )
                return result
            except Exception as exc:
                pop_tool_failure(call_token)
                dur = round((time.perf_counter() - t0) * 1000, 2)
                _call_log.error(
                    f"event={event_prefix}tool_error tool={tool_name} "
                    f"request_id={request_id} duration_ms={dur} "
                    f"err={type(exc).__name__}{extras}"
                )
                raise


def _translated_edit_context(context: MiddlewareContext) -> MiddlewareContext:
    """Copy one edit call with legacy arguments translated before validation."""
    arguments = _extract_tool_args(context.message)
    translated = edit_operations.normalize_edit_surface_arguments(arguments)
    message = context.message
    if isinstance(message, dict):
        new_message = dict(message)
        params = dict(new_message.get("params") or {})
        params["arguments"] = translated
        new_message["params"] = params
    elif hasattr(message, "model_copy"):
        if hasattr(message, "params"):
            params = message.params.model_copy(update={"arguments": translated})
            new_message = message.model_copy(update={"params": params})
        else:
            new_message = message.model_copy(update={"arguments": translated})
    else:
        new_message = copy(message)
        params = copy(new_message.params)
        params.arguments = translated
        new_message.params = params
    if hasattr(context, "copy"):
        return context.copy(message=new_message)
    new_context = copy(context)
    new_context.message = new_message
    return new_context


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
        product_commands = commands_module.product_commands_for(
            "mcp", expose_tier2=expose_tier2
        )
        legacy_commands = (
            _legacy_mcp_commands(expose_tier2=expose_tier2)
            if _legacy_mcp_compat_enabled()
            else ()
        )
        surface_descriptor = capabilities.ActiveSurfaceDescriptor(
            surface="mcp",
            profile=(
                "product-with-legacy-aliases" if legacy_commands else "product"
            ),
            tier2_enabled=expose_tier2,
            product_commands=tuple(command.name for command in product_commands),
            exported_aliases=tuple(command.name for command in legacy_commands),
            hand_registered_tools=tuple(
                sorted(commands_module.HAND_REGISTERED_EXCEPTIONS)
            ),
        )
        for cmd in product_commands:
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
                    surface_descriptor=surface_descriptor,
                ),
                annotations=cmd.mcp_annotations,
            )

        if legacy_commands:
            _register_legacy_mcp_tools(
                mcp,
                vault_root=runtime.vault_root,
                source_schema=runtime.source_schema,
                legacy_commands=legacy_commands,
                surface_descriptor=surface_descriptor,
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


def _gated_adoption_egress(vault_root: Path, command_name: str, payload: Any) -> Any:
    """Bind the MCP principal and run the terminal egress filter over `payload`.

    Design D1 names these handlers as an explicit residual: `@mcp.resource` and
    `@mcp.prompt` are registered by hand, so they never travel through
    `commands.bind_vault` (which binds the principal) nor
    `writer_lease.invoke_command` (which runs `postfilter`). Without this call
    an adoption run document — inventory rows and `selection.paths` naming real
    vault items, plus `handoff.prompt_text` — reaches whoever is on the other
    end of the connector with no principal, no withheld cross-check and no
    credential scrubber. That is the entire release plane bypassed by a
    decorator.

    The principal is resolved the same way `bind_vault` resolves it, so a
    grant authored against the connector's identity matches here too; stdio
    still resolves to the owner.
    """
    from .governance import egress as egress_module
    from .governance import principal as principal_module

    with principal_module.request_scope(principal_module.resolve_mcp_principal()):
        # A bare string has no entries for the dispatcher's walker to filter
        # (finding N3) — `continue_adoption` returns `handoff.prompt_text`, so
        # a withheld path and its wikilink rode straight out past a principal
        # binding and a credential scrubber that were both working and simply
        # had nothing to grip. Reference-aware redaction first, then the
        # ordinary postfilter for credentials.
        with egress_module.disclosure_boundary(vault_root, command_name) as collector:
            if isinstance(payload, str):
                payload = egress_module.redact_withheld_references(vault_root, payload)
            result = egress_module.postfilter(command_name, payload, vault_root)
            egress_module.emit_boundary_receipt(collector)
            return result


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
            text = doc["handoff"]["prompt_text"]
        except Exception:  # noqa: BLE001 - fall back to a minimal, still-useful prompt
            run_id = row.get("run_id", "")
            text = (
                f"Continue my Exomem adoption run {run_id}. Call "
                f'adoption_studio(action="work-item", run_id="{run_id}") to load the '
                "bounded, read-only context, then submit structured proposals via "
                f'adoption_studio(action="propose", run_id="{run_id}").'
            )
        return _gated_adoption_egress(vault_root, "continue_adoption", text)

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
        return _gated_adoption_egress(vault_root, "adoption_runs", {"runs": open_rows})

    @mcp.resource(
        "exomem://adoption/run/{run_id}",
        name="adoption_run",
        description="One durable Adoption Studio run document, read by its stable id.",
        mime_type="application/json",
    )
    def adoption_run_resource(run_id: str) -> dict:
        try:
            doc = adoption_run_module.status(vault_root, run_id=run_id)
        except adoption_run_module.AdoptionRunError as exc:
            doc = {"error": {"code": exc.code, "reason": exc.reason}, "run_id": run_id}
        return _gated_adoption_egress(vault_root, "adoption_run", doc)


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
    legacy_commands: tuple[commands_module.Command, ...],
    surface_descriptor: capabilities.ActiveSurfaceDescriptor,
    project_keys_hint: str,
) -> None:
    for cmd in legacy_commands:
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
                surface_descriptor=surface_descriptor,
            ),
            annotations=cmd.mcp_annotations,
        )


def _legacy_mcp_commands(
    *, expose_tier2: bool
) -> tuple[commands_module.Command, ...]:
    """Return the exact deprecated aliases the MCP adapter will register."""

    product_names = {
        command.name
        for command in commands_module.product_commands_for(
            "mcp", expose_tier2=expose_tier2
        )
    }
    candidates = [
        *commands_module.commands_for("mcp", expose_tier2=expose_tier2),
        *(command for command in commands_module.COMMANDS if command.name == "note"),
    ]
    return tuple(
        command
        for command in candidates
        if command.name not in product_names
        and ("mcp" in command.surfaces or command.name == "note")
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

    configure_logging(
        log_dir if log_dir is not None else resolve_log_dir(), process="server"
    )

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
            # Edge-ingress enforcement runs first so a Cloudflare-transited
            # bypass is refused before SSE priming or MCP/REST routing ever
            # see the request (design.md Decision 1).
            middleware=[
                ASGIMiddleware(EdgeIngressMiddleware),
                ASGIMiddleware(AccessLogMiddleware),
                ASGIMiddleware(PrimeMcpSSEMiddleware),
            ],
            # Remote clients may be routed to another replica or outlive this
            # process.  A process-local Mcp-Session-Id turns either event into
            # a 404/reconnect cascade; each Exomem operation is already an
            # independently authenticated request, so use FastMCP's transport
            # mode designed for horizontally scaled/restartable servers.
            stateless_http=True,
        )
