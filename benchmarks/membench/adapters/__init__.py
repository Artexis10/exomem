"""Provider adapters: capability-declaring, isolated, never faking support."""

from __future__ import annotations

from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    MemoryAdapter,
    OpResult,
    Profile,
    StateExport,
    StateExportPage,
    adapter_factories,
    create_adapter,
    register_adapter,
)

__all__ = [
    "AdapterEnvironmentError",
    "AdapterUnsupported",
    "Capability",
    "Hit",
    "MemoryAdapter",
    "OpResult",
    "Profile",
    "StateExport",
    "StateExportPage",
    "adapter_factories",
    "create_adapter",
    "register_adapter",
]


def _register_builtin() -> None:
    from membench.adapters import exomem_local  # noqa: F401  (registration)


_register_builtin()
