"""Silent-degradation alarm: process counter + the find `degraded` envelope marker.

The three semantic lanes each soft-fall-back on failure (vector→BM25, CLIP→skip,
all-empty→keyword). Those fallbacks used to be invisible — a log line and nothing
else. This pins the two signals that now make a POST-WARM failure observable:

- `find.degradation_counts()` — a process-lifetime per-lane counter, bumped in
  each fallback branch.
- `op_find`'s `degraded: [...lane names...]` envelope marker — distinct from the
  transient `warming` marker (which means "deferred while a preload is loading",
  not "the lane broke").

No real models: the vector lane is failed deterministically by stubbing
`embeddings.embed_texts` to raise, so this runs in the lean suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands
from exomem import embeddings as embeddings_module
from exomem import find as find_module


@pytest.fixture(autouse=True)
def _reset_counts() -> None:
    find_module.reset_degradation_counts()
    yield
    find_module.reset_degradation_counts()


def test_vector_lane_failure_bumps_counter_and_sets_degraded_marker(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-warm vector-lane exception → counter += 1 and envelope `degraded`."""

    def _boom(*_a, **_k):
        raise RuntimeError("sidecar corrupt")

    monkeypatch.setattr(embeddings_module, "embed_texts", _boom)

    # scope="kb-only" skips the auto-widen scan (which would also embed), so the
    # only vector attempt is the main lane — isolating a single failure.
    result = commands.op_find(vault, query="metabolic health", mode="hybrid", scope="kb-only")

    # Envelope, not a bare list: the degraded marker forces the dict shape.
    assert isinstance(result, dict), f"expected degraded envelope, got {type(result)}"
    assert result["degraded"] == ["vector"]
    # BM25 still produced hits — the request degraded, it did not go empty.
    assert result["hits"], "BM25 fallback should still return hits"

    counts = find_module.degradation_counts()
    assert counts.get("vector", 0) >= 1, counts


def test_healthy_keyword_find_has_no_degraded_marker(vault: Path) -> None:
    """A clean find (no lane failure) returns the bare list — no false alarm.

    keyword mode never touches the vector lane, so nothing can degrade.
    """
    result = commands.op_find(vault, query="metabolic", mode="keyword", scope="kb-only")

    assert isinstance(result, list), f"clean find should be a bare list, got {type(result)}"
    assert find_module.degradation_counts() == {}


def test_degradation_counts_snapshot_is_isolated() -> None:
    """The accessor returns a copy — a caller can't mutate the live counter, and
    reset zeroes it."""
    find_module._record_degradation("vector")
    find_module._record_degradation("vector")
    find_module._record_degradation("clip")

    snap = find_module.degradation_counts()
    assert snap == {"vector": 2, "clip": 1}
    snap["vector"] = 999  # mutating the snapshot must not touch the live counter
    assert find_module.degradation_counts() == {"vector": 2, "clip": 1}

    find_module.reset_degradation_counts()
    assert find_module.degradation_counts() == {}
