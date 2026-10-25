"""Immutable, context-local descriptions of Exomem's active command surface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from . import operation_entries


@dataclass(frozen=True, slots=True)
class ActiveSurfaceDescriptor:
    """Trusted adapter identity and the exact commands it exports."""

    surface: str
    profile: str
    tier2_enabled: bool
    product_commands: tuple[str, ...]
    exported_aliases: tuple[str, ...] = ()
    hand_registered_tools: tuple[str, ...] = ()
    unavailable_reasons: tuple[tuple[str, str], ...] = ()
    compatibility_commands: tuple[str, ...] = ()
    #: How this surface spells a converted command (see `operation_entries.TOOL_SHAPES`).
    #: Only an MCP surface lists renderings; every other surface keeps the grouped spelling.
    tool_shape: str = "grouped"
    #: The exact filtered registry used by discovery and invocation resolution.
    contributing_commands: tuple[Any, ...] = field(default=(), repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.tool_shape not in operation_entries.TOOL_SHAPES:
            raise ValueError(f"unknown tool shape: {self.tool_shape!r}")
        for field_name in (
            "product_commands",
            "exported_aliases",
            "hand_registered_tools",
            "compatibility_commands",
        ):
            values = tuple(getattr(self, field_name))
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} contains duplicate command names")
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            object.__setattr__(self, field_name, values)
        if not self.surface or not self.profile:
            raise ValueError("surface and profile must be non-empty")

    @property
    def callable_commands(self) -> frozenset[str]:
        return frozenset(
            (*self.product_commands, *self.compatibility_commands, *self.exported_aliases, *self.hand_registered_tools)
        )

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "surface": self.surface,
                "profile": self.profile,
                "tier2_policy": "enabled" if self.tier2_enabled else "disabled",
                "available_product_tools": sorted(self.product_commands),
                "exported_aliases": sorted(self.exported_aliases),
                "hand_registered_tools": sorted(self.hand_registered_tools),
                **self._shape_metadata(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def as_metadata(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "profile": self.profile,
            "tier2_policy": "enabled" if self.tier2_enabled else "disabled",
            "available_product_tools": sorted(self.product_commands),
            "exported_aliases": sorted(self.exported_aliases),
            "hand_registered_tools": sorted(self.hand_registered_tools),
            **self._shape_metadata(),
            "active_capability_sha256": self.fingerprint,
        }

    def _shape_metadata(self) -> dict[str, Any]:
        """The tool shape, stated only when it is not the grouped spelling every other surface uses.

        Two shapes list the same names and take different arguments, so the fingerprint
        has to tell them apart; leaving the default out keeps every grouped surface's
        fingerprint as it was.
        """
        if self.tool_shape == "grouped":
            return {}
        routes = {}
        for command in self.contributing_commands:
            for rendering in command.renderings:
                routes.setdefault(rendering.name, []).extend(rendering.kinds)
        return {"tool_shape": self.tool_shape,
                **({"available_operations": {name: sorted(set(kinds)) for name, kinds in sorted(routes.items())}}
                   if routes else {})}


_ACTIVE_SURFACE: ContextVar[ActiveSurfaceDescriptor | None] = ContextVar(
    "exomem_active_surface", default=None
)


def current_active_surface() -> ActiveSurfaceDescriptor | None:
    """Return the trusted descriptor bound to this invocation, if any."""

    return _ACTIVE_SURFACE.get()


@contextmanager
def active_surface(descriptor: ActiveSurfaceDescriptor) -> Iterator[None]:
    """Bind one descriptor for a nested, task-local adapter invocation."""

    token = _ACTIVE_SURFACE.set(descriptor)
    try:
        yield
    finally:
        _ACTIVE_SURFACE.reset(token)


def current_tool_shape() -> str:
    """The tool shape of the surface serving this call.

    Where no surface is bound (a direct Python call, a test, a script) nothing lists a
    rendering, so the answer is the grouped spelling that the command registry executes.
    """
    active = _ACTIVE_SURFACE.get()
    return "grouped" if active is None else active.tool_shape


def active_spell_call(command: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The tool and arguments that call a grouped command on the surface serving this call."""
    return operation_entries.spell_call(command, arguments, current_tool_shape())


def active_call_text(command: str, arguments: Mapping[str, Any]) -> str:
    """A grouped-command call written the way the surface serving this call spells it."""
    return operation_entries.call_text(command, arguments, current_tool_shape())


def active_tools(command: str, effect: operation_entries.Effect | None = None) -> tuple[str, ...]:
    """The tools the surface serving this call lists for a grouped command, one side if asked."""
    active = current_active_surface()
    if active is not None and active.tool_shape != "grouped" and active.contributing_commands:
        source = next((item for item in active.contributing_commands if command in item.callable_names), None)
        if source is None:
            return ()
        return tuple(rendering.name for rendering in source.renderings if effect in (None, rendering.effect)) if source.renderings else (source.public_name,)
    return operation_entries.listed_tools(command, current_tool_shape(), effect)


def active_call_route(command: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """A grouped-command call as a `{"tool", "args"}` route, spelled for the serving surface."""
    return operation_entries.call_route(command, arguments, current_tool_shape())


def active_tools_text(command: str, effect: operation_entries.Effect | None = None) -> str:
    """The tools the serving surface lists for a grouped command, for one line of prose."""
    return "/".join(active_tools(command, effect))


def project_call_routes(value: Any) -> Any:
    """Spell authored structured routes for the serving surface using operation entries."""
    if current_tool_shape() == "grouped":
        return value
    if isinstance(value, list | tuple):
        return [project_call_routes(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: project_call_routes(item) for key, item in value.items()}
    tool = result.get("tool")
    operations = operation_entries.OPERATIONS.get(tool) if isinstance(tool, str) else None
    if operations is None:
        return result
    argument_key = "arguments" if "arguments" in result else "args"
    arguments = result.get(argument_key)
    if isinstance(arguments, Mapping) and isinstance(arguments.get("operation"), Mapping):
        return result
    if arguments is None:
        fields = operations.every_field() | ({operations.selector} if operations.selector else set())
        arguments = {key: result.pop(key) for key in list(result) if key in fields}
    if operations.selector and operations.selector not in arguments:
        result.pop("tool")
        result["tools"] = list(active_tools(tool))
        return result
    name, arguments = active_spell_call(tool, arguments)
    result.update(tool=name, **{argument_key: arguments})
    return result
