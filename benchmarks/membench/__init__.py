"""membench — neutral memory-benchmark package (corpus, adapters, scoring).

Lives outside ``src/`` on purpose: it is never shipped in the wheel/sdist and
may name competitor products. Import side effects are forbidden here so lean
tests stay fast.
"""

from __future__ import annotations

__version__ = "0.1.0"

GENERATOR_VERSION = "membench-gen/0.1.0"
