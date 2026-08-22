"""Command-surface metadata shared by MCP, REST, and CLI adapters."""

from __future__ import annotations

import hashlib
import inspect
import logging
import threading
import time
import types
import typing
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field

from mcp.types import ToolAnnotations
from pydantic import Field, WithJsonSchema

from . import call_spans, capabilities, reserved_paths
from .call_spans import (  # noqa: F401 - re-exported for existing importers
    pop_call_spans,
    record_span,
    span,
)
from .governance import operations as governance_operations
from .mutation_terminal import ResponseDetail

_log = logging.getLogger(__name__)

# Signal channel from the synchronous MCP tool wrapper (below, runs in
# FastMCP's anyio threadpool) to `CallTraceMiddleware` (server.py, runs in the
# async request task): a ContextVar set inside the wrapper does NOT propagate
# back to the middleware once the threadpool call returns, so the wrapper
# leaves a bounded, lock-protected breadcrumb here instead, keyed by request
# id. `mcp_request_id()` accepts a client-supplied `x-exomem-request-id`
# verbatim once it is UUIDv4-shaped — that shape is validated, not uniqueness
# — so two concurrent calls can legitimately share one request id. Each
# request id therefore maps to a FIFO list of failures rather than a single
# entry: the middleware pops the oldest one per completed call, so N
# concurrent failing calls sharing an id each get their own failure logged
# instead of the second silently clobbering the first. The middleware pops
# unconditionally (whether or not an entry is present) so nothing can leak
# indefinitely; a TTL sweep and a total-size bound are further independent
# guards in case a pop is ever missed (e.g. a caller that never reaches the
# middleware, such as a direct test harness).
_TOOL_FAILURES_LOCK = threading.Lock()
_TOOL_FAILURES: dict[str, list[dict[str, object]]] = {}
_TOOL_FAILURE_TTL_SECONDS = 300.0
_TOOL_FAILURES_MAX_TOTAL = 1000

# Per-call signal key, defined in `call_spans` so the phase timers and this
# failure breadcrumb key off one token rather than two definitions that must
# agree. Re-exported under the original private name: every reader of this
# module already knows it by that name.
_MCP_CALL_TOKEN = call_spans.MCP_CALL_TOKEN


def _sweep_tool_failures_locked(now: float) -> None:
    empty_keys = []
    for request_id, entries in _TOOL_FAILURES.items():
        entries[:] = [entry for entry in entries if now - entry["at"] <= _TOOL_FAILURE_TTL_SECONDS]
        if not entries:
            empty_keys.append(request_id)
    for request_id in empty_keys:
        _TOOL_FAILURES.pop(request_id, None)


def _evict_oldest_entry_locked() -> None:
    """Drop the globally oldest recorded failure to bound total memory."""
    oldest_key = None
    oldest_at = None
    for request_id, entries in _TOOL_FAILURES.items():
        if entries and (oldest_at is None or entries[0]["at"] < oldest_at):
            oldest_key, oldest_at = request_id, entries[0]["at"]
    if oldest_key is not None:
        entries = _TOOL_FAILURES[oldest_key]
        entries.pop(0)
        if not entries:
            _TOOL_FAILURES.pop(oldest_key, None)


def _record_tool_failure(request_id: str, code: str) -> None:
    try:
        now = time.monotonic()
        with _TOOL_FAILURES_LOCK:
            _sweep_tool_failures_locked(now)
            total = sum(len(entries) for entries in _TOOL_FAILURES.values())
            if total >= _TOOL_FAILURES_MAX_TOTAL:
                _evict_oldest_entry_locked()
            _TOOL_FAILURES.setdefault(request_id, []).append({"code": code, "at": now})
    except Exception:  # noqa: BLE001 - the signal channel must never break a tool call
        pass


def pop_tool_failure(request_id: str) -> dict[str, object] | None:
    """Pop and return the OLDEST recorded failure for `request_id`, if any.

    FIFO per request id: two concurrent calls sharing a (client-supplied)
    request id each pop their own failure in the order they were recorded.
    Unconditional: call this exactly once per completed MCP call regardless
    of outcome, so a call that never failed simply pops `None`.
    """
    try:
        with _TOOL_FAILURES_LOCK:
            _sweep_tool_failures_locked(time.monotonic())
            entries = _TOOL_FAILURES.get(request_id)
            if not entries:
                return None
            entry = entries.pop(0)
            if not entries:
                _TOOL_FAILURES.pop(request_id, None)
            return entry
    except Exception:  # noqa: BLE001 - the signal channel must never break a tool call
        return None


def _scope_kind(retry_scope: str | None) -> str:
    """A content-free descriptor of the caller identity kind, never the value."""
    if not retry_scope:
        return "none"
    return retry_scope.split(":", 1)[0]


def _retry_scope_hash(retry_scope: str | None) -> str | None:
    """A short, stable correlation hash for `retry_scope` — never the value.

    `retry_scope` is already itself a privacy-safe hash-based identifier
    (e.g. `bearer:<sha256>`), so this is a hash-of-a-hash purely to give log
    readers a short, consistent token for spotting repeated retries of the
    same (tool, scope) pair without re-deriving or exposing the identity.
    """
    if not retry_scope:
        return None
    return hashlib.sha256(retry_scope.encode("utf-8")).hexdigest()[:16]


def _log_tool_failure(
    *,
    tool: str,
    request_id: str,
    code: str,
    duration_ms: float,
    message: str,
    retry_scope: str | None = None,
) -> None:
    try:
        from . import metrics
        from .log_events import log_event

        log_event(
            _log,
            logging.WARNING,
            "tool_failure",
            fields={
                "tool": tool,
                "request_id": request_id,
                "code": code,
                "duration_ms": duration_ms,
                "scope": _scope_kind(retry_scope),
                "retry_scope_hash": _retry_scope_hash(retry_scope),
            },
            content={"message": str(message)[:300]},
        )
        metrics.inc_counter("exomem_tool_calls_total", {"tool": tool, "outcome": "failure"})
        metrics.inc_counter("exomem_tool_failures_total", {"tool": tool, "code": code})
        metrics.observe_duration_ms("exomem_tool_duration_ms", duration_ms, {"tool": tool})
    except Exception:  # noqa: BLE001 - observability must never break a tool call
        pass
    _record_tool_failure(_MCP_CALL_TOKEN.get() or request_id, code)


def _log_tool_success(
    *, tool: str, duration_ms: float, retry_scope: str | None = None
) -> None:
    """Count a successful MCP tool call. Exactly one of `_log_tool_success`
    or `_log_tool_failure` runs per call, both from this same wrapper, so a
    call is counted exactly once."""
    try:
        from . import metrics
        from .log_events import log_event

        log_event(
            _log,
            logging.DEBUG,
            "tool_success",
            fields={
                "tool": tool,
                "duration_ms": duration_ms,
                "retry_scope_hash": _retry_scope_hash(retry_scope),
            },
        )
        metrics.inc_counter("exomem_tool_calls_total", {"tool": tool, "outcome": "success"})
        metrics.observe_duration_ms("exomem_tool_duration_ms", duration_ms, {"tool": tool})
    except Exception:  # noqa: BLE001 - observability must never break a tool call
        pass

# Text-write ops -> the argument field(s) whose value must not be a base64 binary
# blob. The model pays for those characters as output tokens before the request
# arrives, so they are rejected at every write boundary (MCP middleware + REST
# coercion) and the caller is pointed at /upload.
GUARDED_WRITE_FIELDS: dict[str, tuple[str, ...]] = {
    "add": ("content",),
    "note": ("content",),
    "edit": ("new_body", "new_string"),
    "replace": ("content",),
    "create_file": ("content",),
    "append_to_file": ("content",),
    "preserve": ("content",),
    "remember": ("content",),
    "capture_source": ("content",),
    "preserve_evidence": ("content",),
    "edit_memory": ("new_body", "new_string"),
    "observe_memory": ("content",),
    "replace_memory": ("content",),
    "manage_memory_file": ("content",),
    "record_memory": ("manifest_text", "body"),
    "plan_memory": ("manifest_text", "body"),
}


_MCP_REQUEST_ID: ContextVar[str | None] = ContextVar(
    "exomem_mcp_request_id", default=None
)


def governance_tool_is_destructive(
    registry: Mapping[str, governance_operations.OperationSpec] = governance_operations.OPERATION_SPECS,
) -> bool:
    """Whether any registered governance operation overwrites policy state."""
    return any(
        spec.destructive or any(variant.destructive for variant in spec.variants)
        for spec in registry.values()
    )


# Write ops whose mutation OVERWRITES or REMOVES existing vault content, as opposed
# to purely additive writes (add / note / create_file / append_to_file / link /
# preserve / recover_from_trash / reconcile). Drives the MCP `destructiveHint` so a
# cautious client does not badge an append as destructive.
DESTRUCTIVE_OPS: frozenset[str] = frozenset(
    {
        "edit",
        "replace",
        "delete",
        "move_file",
        "audit_fix",
        "edit_memory",
        "observe_memory",
        "replace_memory",
        "manage_memory_file",
        "maintain_memory",
        "schema_memory",
        "record_memory",
        "plan_memory",
        *({"govern_memory"} if governance_tool_is_destructive() else set()),
    }
)


def mcp_tool_annotations(
    name: str, *, read_only: bool, open_world: bool = False, idempotent: bool = False
) -> ToolAnnotations:
    """MCP behaviour hints for one tool — what cautious clients render as badges."""
    return ToolAnnotations(
        title=name.replace("_", " ").title(),
        readOnlyHint=read_only,
        destructiveHint=False if read_only else (name in DESTRUCTIVE_OPS),
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


@dataclass(frozen=True)
class Param:
    """One operation parameter, surface-agnostic."""

    name: str
    type: str = "str"
    required: bool = False
    help: str = ""
    cli_positional: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Command:
    name: str
    leaf: Callable
    params: tuple[Param, ...]
    surfaces: frozenset
    tier: int = 1
    cli_writes: bool = False
    needs_schema: bool = False
    description: str = ""
    product_surface: str = "advanced"
    product_actions: tuple[str, ...] = ()
    first_run_safe: bool = False
    routes: tuple[str, ...] = ()
    response_detail: ResponseDetail | None = None
    path_roles: tuple[reserved_paths.PathRole, ...] = ()
    mcp_meta: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: types.MappingProxyType({}), hash=False
    )

    @property
    def doc(self) -> str:
        """The full description Claude reads — the leaf's own docstring."""
        return self.description or (self.leaf.__doc__ or "")

    @property
    def guarded_fields(self) -> tuple[str, ...]:
        """Text fields whose value must not be a base64 binary blob."""
        return GUARDED_WRITE_FIELDS.get(self.name, ())

    @property
    def read_only(self) -> bool:
        """True for non-mutating ops (search / get / list)."""
        return not self.cli_writes

    @property
    def mcp_annotations(self) -> ToolAnnotations:
        """MCP behaviour hints for this command's generated tool."""
        return mcp_tool_annotations(self.name, read_only=self.read_only)


def bind_vault(
    leaf: Callable,
    *injected: object,
    name: str | None = None,
    description: str | None = None,
    command: Command | None = None,
    surface_descriptor: capabilities.ActiveSurfaceDescriptor | None = None,
) -> Callable:
    """Return a callable FastMCP introspects exactly like a hand-written wrapper."""
    sig = inspect.signature(leaf)
    params = list(sig.parameters.values())
    visible = params[len(injected):]

    try:
        resolved = typing.get_type_hints(leaf, include_extras=True)
    except Exception:  # noqa: BLE001 - fall back to inspect's annotations
        resolved = {}

    help_text = parse_args_help(description if description is not None else leaf.__doc__)
    if command is not None:
        help_text.update(
            {
                param.name: param.help
                for param in getattr(command, "params", ())
                if param.help
            }
        )
    visible = [
        p.replace(
            annotation=_annotate_description(
                resolved.get(p.name, p.annotation), help_text.get(p.name, "")
            )
        )
        for p in visible
    ]
    if command is not None and command.name == "edit_memory":
        from .edit_operations import EditOperation, public_edit_operation_schema

        operation_schema = public_edit_operation_schema()
        operation_help = help_text.get("operation", "")
        if operation_help:
            operation_schema = {**operation_schema, "description": operation_help}
        visible = [
            parameter.replace(
                default=inspect.Parameter.empty,
                annotation=typing.Annotated[
                    EditOperation,
                    WithJsonSchema(operation_schema),
                    Field(description=operation_help),
                ],
            )
            if parameter.name == "operation"
            else parameter
            for parameter in visible
            if parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ]
    default_response_detail = getattr(command, "response_detail", None)
    if default_response_detail is not None:
        response_detail_help = help_text["response_detail"]
        response_detail = inspect.Parameter(
            "response_detail",
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=default_response_detail,
            annotation=typing.Annotated[
                ResponseDetail,
                Field(description=response_detail_help),
            ],
        )
        insert_at = next(
            (
                index
                for index, parameter in enumerate(visible)
                if parameter.kind is inspect.Parameter.VAR_KEYWORD
            ),
            len(visible),
        )
        visible.insert(insert_at, response_detail)
    if command is not None and (
        surface_descriptor is None or surface_descriptor.surface == "mcp"
    ):
        authorization_credential = inspect.Parameter(
            "authorization_session_credential",
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=typing.Annotated[
                str | None,
                Field(
                    description=(
                        "Optional authorization-session bearer. Consumed by the raw "
                        "MCP boundary before tool validation."
                    )
                ),
            ],
        )
        insert_at = next(
            (
                index
                for index, parameter in enumerate(visible)
                if parameter.kind is inspect.Parameter.VAR_KEYWORD
            ),
            len(visible),
        )
        visible.insert(insert_at, authorization_credential)
    new_sig = sig.replace(parameters=visible)

    def wrapper(**kwargs):
        from .governance import authorization_request
        from .governance import principal as principal_module

        if kwargs.pop("authorization_session_credential", None) is not None:
            raise authorization_request.AuthorizationContextUnavailable
        principal = principal_module.current_principal()
        if principal is None:
            raise authorization_request.AuthorizationContextUnavailable

        context = (
            capabilities.active_surface(surface_descriptor)
            if surface_descriptor is not None
            else nullcontext()
        )
        # Resolve the canonical audience at the MCP boundary, beside the
        # existing `mcp_retry_scope()` identity derivation (design D5). Bound
        # for the whole invocation so every read leaf under it decides against
        # the same principal; stdio resolves to `owner`.
        with context, principal_module.request_scope(principal):
            if command is None:
                return leaf(*injected, **kwargs)
            from .commands import invocation_is_read_only
            from .governance.egress import SelectorCoverageError
            from .writer_lease import invoke_command

            try:
                invocation_read_only = invocation_is_read_only(command, kwargs)
            except SelectorCoverageError:
                # The dispatcher will acquire writer authority and reject the
                # uncovered selector before its leaf. This preliminary result
                # exists only to keep retry/admission metadata conservative.
                invocation_read_only = False
            request_id = mcp_request_id()
            tool_name = command.name
            # Computed at most once per call, and only for a mutation: a
            # read-only invocation must never call `mcp_retry_scope()` at
            # all (a live dependency lookup), so the same value is reused
            # here for logging instead of a second, independent call.
            retry_scope = None if invocation_read_only else mcp_retry_scope()
            t0 = time.perf_counter()
            try:
                result = invoke_command(
                    command,
                    *injected,
                    mutation_request_id=request_id,
                    implicit_idempotency_scope=retry_scope,
                    **kwargs,
                )
                # MCP-layer second pass (design D1): defense in depth where
                # the FastMCP context is live. `postfilter` is idempotent —
                # an already-replaced credential matches nothing — so running
                # it again after the dispatcher's pass costs a walk, not a
                # second rewrite.
                #
                # Ordered BEFORE the success log on purpose: the timing the
                # log reports should include the egress pass, and nothing
                # should be recorded as successfully served until the result
                # has actually cleared the boundary.
                from .governance.egress import is_vault_root, postfilter

                if injected and is_vault_root(injected[0]):
                    result = postfilter(command.name, result, injected[0])
                _log_tool_success(
                    tool=tool_name,
                    duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                    retry_scope=retry_scope,
                )
                return result
            except Exception as error:
                from . import cli_ops

                duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                if isinstance(error, cli_ops.OpError):
                    _log_tool_failure(
                        tool=tool_name,
                        request_id=request_id,
                        code=error.code,
                        duration_ms=duration_ms,
                        message=error.message,
                        retry_scope=retry_scope,
                    )
                    return cli_ops.envelope(False, error=error.as_public_dict())
                semantic_error = cli_ops.semantic_validation_error_dict(error)
                if semantic_error is None:
                    raise
                _log_tool_failure(
                    tool=tool_name,
                    request_id=request_id,
                    code=str(semantic_error.get("code") or "OP_ERROR"),
                    duration_ms=duration_ms,
                    message=str(semantic_error.get("message") or ""),
                    retry_scope=retry_scope,
                )
                return cli_ops.envelope(False, error=semantic_error)

    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    wrapper.__name__ = name or leaf.__name__
    wrapper.__qualname__ = wrapper.__name__
    wrapper.__doc__ = description if description is not None else leaf.__doc__
    ann = {
        p.name: p.annotation
        for p in visible
        if p.annotation is not inspect.Parameter.empty
    }
    if "return" in resolved:
        ann["return"] = resolved["return"]
    wrapper.__annotations__ = ann
    return wrapper


def mcp_retry_scope() -> str | None:
    """Return a stable, privacy-safe principal scope for bounded MCP replay."""
    try:
        from fastmcp.server.dependencies import (
            get_access_token,
            get_context,
            get_http_headers,
        )

        access_token = get_access_token()
        claims = getattr(access_token, "claims", None) or {}
        subject = str(claims.get("sub") or "").strip()
        if subject:
            issuer = str(claims.get("iss") or "verified-principal").strip()
            digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
            return f"principal:{digest}"

        headers = get_http_headers(include={"authorization"})
        authorization = headers.get("authorization", "").strip()
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and credential.strip():
            digest = hashlib.sha256(credential.strip().encode("utf-8")).hexdigest()
            return f"bearer:{digest}"
        return f"session:{get_context().session_id}"
    except (LookupError, RuntimeError):
        return None


def mcp_request_id() -> str:
    """Return one canonical correlation ID for the active MCP tool call."""
    active = _MCP_REQUEST_ID.get()
    if active is not None:
        return active
    try:
        from fastmcp.server.dependencies import get_http_headers

        value = get_http_headers(include={"x-exomem-request-id"}).get(
            "x-exomem-request-id", ""
        )
        canonical = canonical_request_id(value)
        if canonical is not None:
            return canonical
    except (LookupError, RuntimeError):
        pass
    return str(uuid.uuid4())


def mcp_caller_identity() -> dict[str, str | None]:
    """Who is calling: MCP client name/version, transport, and session.

    The MCP initialize handshake carries `clientInfo`, so the server already
    knows whether a call came from claude.ai, ChatGPT, Codex, or a local CLI --
    and then discards it. Every existing journal is therefore one
    undifferentiated stream across every connected client, which is exactly the
    distinction an operator needs first when one client's calls are failing.

    Deliberately *not* hashed, unlike `mcp_retry_scope`. A client's product name
    and version identify software, not a person: it is the same class of value
    as a `User-Agent`, and hashing it would destroy the only field that answers
    "which client?" while protecting nothing. The per-principal identity stays
    hashed and separate.

    Never raises. Outside an MCP call every field is simply `None`.
    """
    identity: dict[str, str | None] = {
        "client_name": None,
        "client_version": None,
        "transport": None,
        "session_id": None,
    }
    try:
        from fastmcp.server.dependencies import get_context, get_http_headers
    except ImportError:
        return identity
    try:
        context = get_context()
    except (LookupError, RuntimeError):
        return identity
    try:
        params = getattr(context.session, "client_params", None)
        info = getattr(params, "clientInfo", None)
        if info is not None:
            identity["client_name"] = str(getattr(info, "name", "") or "") or None
            identity["client_version"] = str(getattr(info, "version", "") or "") or None
    except (AttributeError, LookupError, RuntimeError, ValueError):
        pass
    try:
        identity["session_id"] = str(context.session_id)
    except (AttributeError, LookupError, RuntimeError, ValueError):
        pass
    try:
        # Headers exist only on the HTTP transports, so their presence *is* the
        # transport signal; there is no separate flag to read.
        headers = get_http_headers()
        identity["transport"] = "http" if headers else "stdio"
        agent = str(headers.get("user-agent", "") or "").strip()
        if agent and not identity["client_name"]:
            identity["client_name"] = agent
    except (LookupError, RuntimeError):
        identity["transport"] = identity["transport"] or "stdio"
    return identity


def peek_request_id() -> str | None:
    """Return the active MCP request id if one is bound, without minting.

    Unlike `mcp_request_id()`, this never falls back to reading headers or
    generating a fresh uuid — it is for best-effort correlation (e.g. an
    additive JSONL field) from code that may run outside any MCP call at
    all, where minting an id would fabricate a false correlation.
    """
    return _MCP_REQUEST_ID.get()


def canonical_request_id(value: object) -> str | None:
    """Return a canonical UUIDv4 request ID or reject caller-controlled log text."""
    clean = str(value or "").strip()
    try:
        parsed = uuid.UUID(clean)
    except (AttributeError, ValueError):
        return None
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != clean:
        return None
    return clean


@contextmanager
def mcp_request_context(request_id: str):
    """Bind the middleware correlation ID through the synchronous tool wrapper.

    Yields the minted per-call token: the failure-signal key that stays unique
    even when concurrent calls share a client-supplied request id.
    """
    token = _MCP_REQUEST_ID.set(request_id)
    call_token = uuid.uuid4().hex
    call_reset = _MCP_CALL_TOKEN.set(call_token)
    try:
        yield call_token
    finally:
        _MCP_CALL_TOKEN.reset(call_reset)
        _MCP_REQUEST_ID.reset(token)


def _annotate_description(annotation: object, description: str) -> object:
    if (
        not description
        or annotation is inspect.Parameter.empty
        or typing.get_origin(annotation) is typing.Annotated
    ):
        return annotation
    return typing.Annotated[annotation, Field(description=description)]


def parse_args_help(doc: str | None) -> dict[str, str]:
    """Best-effort `{param: one-line help}` from a Google-style `Args:` block."""
    if not doc:
        return {}
    lines = inspect.cleandoc(doc).splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Args:")
    except StopIteration:
        return {}
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        stripped = ln.strip()
        head, sep, rest = stripped.partition(":")
        if sep and head and head.replace("_", "").isalnum() and " " not in head:
            if cur is not None:
                out[cur] = " ".join(buf).strip()
            cur, buf = head, [rest.strip()]
        elif cur is not None:
            buf.append(stripped)
    if cur is not None:
        out[cur] = " ".join(buf).strip()
    return out


def type_tag(annotation: object) -> str:
    """Map a resolved type annotation to a REST/CLI coercion tag."""
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        values = typing.get_args(annotation)
        if values and all(isinstance(value, str) for value in values):
            return "str"
        return "json"
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return type_tag(non_none[0])
        return "json"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is str:
        return "str"
    if annotation is dict or origin is dict:
        return "dict"
    if annotation is list or origin is list:
        args = typing.get_args(annotation)
        return "list[str]" if args in ((), (str,)) else "json"
    return "json"


def _literal_string_values(annotation: object) -> tuple[str, ...]:
    """Return registry-style string Literal values, including Optional aliases."""
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        values = typing.get_args(annotation)
        return tuple(values) if all(isinstance(value, str) for value in values) else ()
    if origin is typing.Union or origin is types.UnionType:
        non_none = [item for item in typing.get_args(annotation) if item is not type(None)]
        if len(non_none) == 1:
            return _literal_string_values(non_none[0])
    return ()


def derive_params(
    leaf: Callable, *, skip: int, positional: str | None = None
) -> tuple[Param, ...]:
    """Derive the declarative `Param` tuple from a leaf signature + docstring."""
    sig = inspect.signature(leaf)
    try:
        hints = typing.get_type_hints(leaf)
    except Exception:  # noqa: BLE001
        hints = {}
    helps = parse_args_help(leaf.__doc__)
    params: list[Param] = []
    for p in list(sig.parameters.values())[skip:]:
        ann = hints.get(p.name, p.annotation)
        literal_values = _literal_string_values(ann)
        params.append(
            Param(
                name=p.name,
                type=type_tag(ann),
                required=p.default is inspect.Parameter.empty,
                help=helps.get(p.name, ""),
                cli_positional=(p.name == positional),
                choices=(
                    tuple(literal_values)
                    if literal_values
                    and all(isinstance(value, str) for value in literal_values)
                    else ()
                ),
            )
        )
    return tuple(params)
