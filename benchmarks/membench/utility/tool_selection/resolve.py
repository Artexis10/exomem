"""Normalising a tool call to the product command it runs, for any tool shape.

Every arm lists different tool names and argument shapes for the same logical
operation, so scoring never reads a tool name. It asks a resolver, injected as a
parameter, what a call means: ``resolver(public_name, arguments, shape)`` returns the
grouped product command and the flat grouped arguments that the call stands for on
a server of that tool shape. Every shape reads a call's identity the same way, from
its operation alone (``operation_entries.unspell_call``: the tool, the selector and the
mode, never the fields), so one arm cannot score a call that another arm would not.
Whether the server then refused the fields is a separate readout, read from the
result codes. ``grouped_resolver`` reads today's grouped spelling alone and serves tests.

The product also owns the read-or-write decision (``invocation_is_read_only``)
and the selector name that carries the action, so neither is restated here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Resolver = Callable[[str, Mapping[str, Any], str], "tuple[Any, dict[str, Any]]"]


class UnresolvedTool(LookupError):
    """The name or the arguments mean no product operation (the server refuses them too)."""

    def __init__(self, name: str, command: Any = None, arguments: Mapping | None = None):
        super().__init__(name)
        self.command = command
        self.arguments = arguments


def product_resolver() -> Resolver:
    """Read a call's operation as a server of the arm's shape names it; imported at run time."""

    from exomem.cli_ops import OpError
    from exomem.command_surface import find_rendering, resolve_invocation
    from exomem.commands import product_commands_for

    commands = product_commands_for("mcp")

    def resolve(name: str, arguments: Mapping[str, Any], shape: str):
        try:
            return resolve_invocation(name, arguments, commands, shape=shape)
        except OpError as error:
            # The execution boundary retains its selected identity before business-field refusal.
            command = getattr(error, "command", None)
            flat = getattr(error, "arguments", None)
            if command is not None and isinstance(flat, Mapping):
                operation = arguments.get("operation")
                kind = operation.get("kind") if isinstance(operation, Mapping) else None
                if not isinstance(kind, str) or find_rendering(name, commands, shape=shape, kind=kind) is None:
                    raise UnresolvedTool(name, command, flat) from error
                return command, dict(flat)
            raise UnresolvedTool(name) from error

    return resolve


def grouped_resolver() -> Resolver:
    """Read today's grouped spelling alone, whatever shape the session names."""

    resolve = product_resolver()
    return lambda name, arguments, shape="grouped": resolve(name, arguments, "grouped")


RESOLVERS: Mapping[str, Callable[[], Resolver]] = {
    "product": product_resolver,
    "grouped": grouped_resolver,
}


@dataclass(frozen=True)
class ResolvedCall:
    """What one tool call meant. ``resolved`` is false when no operation matched."""

    resolved: bool
    command: str | None = None
    action: str | None = None
    mode: str | None = None
    #: ``None`` when the product could not classify the call (it is refused, not run).
    read_only: bool | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)


def resolve_call(
    resolver: Resolver, name: str, arguments: Mapping[str, Any] | None, shape: str = "grouped"
) -> ResolvedCall:
    from exomem.cli_ops import OpError
    from exomem.commands import invocation_is_read_only
    from exomem.governance import egress

    try:
        command, flat = resolver(name, arguments or {}, shape)
        resolved = True
    except UnresolvedTool as error:
        if error.command is None or error.arguments is None:
            return ResolvedCall(False)
        command, flat, resolved = error.command, error.arguments, False
    except (LookupError, OpError, ValueError, TypeError):
        # LookupError covers UnresolvedTool and the product's UnresolvedInvocation;
        # OpError is the command's own argument refusal, which the server returns too.
        return ResolvedCall(False)
    selector = egress.selector_for_command(command.name)
    action = flat.get(selector) if selector else None
    request = flat.get("import_request")
    # The Records command fixes `import_request.mode` as its second selector.
    mode = request.get("mode") if isinstance(request, dict) else None
    try:
        read_only: bool | None = invocation_is_read_only(command, flat)
    except Exception:  # noqa: BLE001 - an unclassifiable call is a reported state, never a guess
        read_only = None
    return ResolvedCall(
        resolved,
        command.name,
        action if isinstance(action, str) else None,
        mode if isinstance(mode, str) else None,
        read_only,
        flat,
    )
