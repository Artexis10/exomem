"""The source-only direct row delegates to the existing leak-free adapter."""

from __future__ import annotations

from .base import DirectProvider
from ..adapter import LmeExomemAdapter


class ExomemDirectProvider(LmeExomemAdapter):
    """Registry marker preserving the adapter's established behaviour unchanged."""

    def variant_id(self) -> str:
        return "exomem-source-only"
