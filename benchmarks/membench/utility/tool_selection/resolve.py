"""Normalising a tool call to the product command it runs, for any tool shape.

Every arm lists different tool names and argument shapes for the same logical
operation, so scoring never reads a tool name. It asks a resolver, injected as a
parameter, what a call means: ``resolver(public_name, arguments)`` returns the
grouped product command and the flat grouped arguments, as the server's own
translation does. The production resolver is the product's
``command_surface.resolve_invocation``; ``grouped_resolver`` is the same
contract for today's grouped spelling alone and serves tests and the grouped arm
until the product ships ``resolve_invocation``.

The product also owns the read-or-write decision (``invocation_is_read_only``)
and the selector name that carries the action, so neither is restated here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

Resolver = Callable[[str, Mapping[str, Any]], "tuple[Any, dict[str, Any]]"]


class UnresolvedTool(LookupError):
    """The name or the arguments mean no product operation (the server refuses them too)."""


def product_resolver() -> Resolver:
    """The product's rendering-aware resolver; imported at run time, never at load."""

    try:
        from exomem.command_surface import resolve_invocation
    except ImportError as error:
        raise RuntimeError(
            "exomem.command_surface.resolve_invocation is not in this build; "
            "use --resolver grouped for the grouped arm only"
        ) from error
    return resolve_invocation


def grouped_resolver() -> Resolver:
    """Resolve today's grouped spelling: the command registry's own lookup, arguments as sent."""

    from exomem.command_surface import resolve_command
    from exomem.commands import product_commands_for

    commands = product_commands_for("mcp")

    def resolve(name: str, arguments: Mapping[str, Any]):
        command = resolve_command(name, commands)
        if command is None:
            raise UnresolvedTool(name)
        return command, dict(arguments)

    return resolve


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


def resolve_call(resolver: Resolver, name: str, arguments: Mapping[str, Any] | None) -> ResolvedCall:
    from exomem.commands import invocation_is_read_only
    from exomem.governance import egress

    try:
        command, flat = resolver(name, arguments or {})
    except (UnresolvedTool, KeyError, ValueError, TypeError):
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
        True,
        command.name,
        action if isinstance(action, str) else None,
        mode if isinstance(mode, str) else None,
        read_only,
    )
