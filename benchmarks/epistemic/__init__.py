"""Epistemic State Bench engine.

Scenario trajectories, a frozen assertion registry, neutral state snapshots,
and five-valued scoring. Concept vocabulary is anchored to external prior art
(PROV-O provenance, AGM belief revision, Toulmin claim/warrant/backing) rather
than to any product's folder names. See ``PREREGISTRATION.md``.

Import side effects are forbidden here so lean tests stay fast.
"""

from __future__ import annotations

__all__ = ["ENGINE_VERSION"]

ENGINE_VERSION = "0.1.0"
