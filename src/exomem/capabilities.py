"""Immutable, context-local descriptions of Exomem's active command surface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveSurfaceDescriptor:
    """Trusted adapter identity and the exact commands it exports."""

    surface: str
    profile: str
    tier2_enabled: bool
    product_commands: tuple[str, ...]
    exported_aliases: tuple[str, ...] = ()
    hand_registered_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "product_commands",
            "exported_aliases",
            "hand_registered_tools",
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
            (*self.product_commands, *self.exported_aliases, *self.hand_registered_tools)
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
            "active_capability_sha256": self.fingerprint,
        }


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
