"""Template registry: importing this package registers every template."""

from __future__ import annotations

from membench.templates import t00_mini_smoke  # noqa: F401  (registration)
from membench.templates.base import Template, registry

__all__ = ["Template", "registry"]
