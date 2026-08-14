"""Read-only projectors: provider state in, neutral snapshot out.

A projector may use documented provider surfaces only, must never write, and
must publish its own size and endpoint count so gross asymmetry between
projectors is visible rather than hidden.
"""

from __future__ import annotations

__all__: list[str] = []
