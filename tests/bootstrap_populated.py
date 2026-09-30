"""A vault populated to the design's maximum bound, for the bootstrap byte budgets.

An empty vault understates the always-served core: the core carries vault-derived
blocks that grow with what the vault holds. This builds the worst case those blocks
can reach, so the ceiling is asserted against it and not against a best case.

The blocks and what bounds them in the core (the sections carry them in full):

- `due_state`: built here from more rows than `due_state.TOP_LIMIT` with long refs and
  every category present; the core serves a counts summary of at most 200 bytes.
- `latency`: `latency_watch` reports one row per watched (tool, deep) pair for the
  calling client, each with `DOMINANT_SPANS` spans; every pair breaches here. The core
  does not carry it.
- custom entity types: nothing in the code caps their count, so `CUSTOM_ENTITY_TYPES` is
  a stated bound (32, well over the core's `CORE_ENTITY_TYPE_CAP`, so the cap is
  exercised); the core lists at most the cap and points at the `entities` section.
"""

from __future__ import annotations

import pathlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from exomem import due_state, entity_types, latency_watch

CUSTOM_ENTITY_TYPES = 32
LONG_REF = "Knowledge Base/Predictions/" + "a-long-prediction-title-that-is-realistic-" * 2 + "2026-09-30"


def populated_root() -> pathlib.Path:
    """A vault with `CUSTOM_ENTITY_TYPES` custom entity types registered."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    proposal = {
        "schema_version": 1,
        "entity_types": {
            f"custom-entity-type-{index:05d}"[:24]: {
                "folder": f"Custom Folder {index}",
                "label": f"Custom Type {index}",
                "aliases": [],
                "capture_guidance": "A stable recurring identity of this kind.",
            }
            for index in range(CUSTOM_ENTITY_TYPES)
        },
    }
    entity_types.save_registry(root, proposal, expected_hash=None, observed_ids=())
    return root


def maximal_due_state() -> dict:
    rows = [
        {"category": category, "ref": f"{LONG_REF}-{index}", "due_since": "2026-09-01"}
        for index, category in enumerate(due_state.PROJECTION_CATEGORIES * 4)
    ]
    block = due_state.block(rows)
    assert block is not None and len(block["top"]) == due_state.TOP_LIMIT
    return block


def maximal_latency() -> list[dict]:
    watch = latency_watch.reset(started_at=0.0, clock=lambda: 1_000_000.0)
    names = [f"recall.stage.span-name-{index}" for index in range(latency_watch.DOMINANT_SPANS + 3)]
    for tool in sorted(latency_watch.WATCHED_TOOLS):
        for deep in (False, True):
            for _ in range(latency_watch.MIN_SAMPLES + 5):
                watch.observe(
                    tool=tool,
                    client="claude-code",
                    deep=deep,
                    total_ms=90_000.0,
                    spans=[(name, 10_000.0 + index) for index, name in enumerate(names)],
                    at=999_990.0,
                )
    rows = latency_watch.bootstrap_block("claude-code", now=1_000_000.0)
    latency_watch.reset()
    assert rows
    return rows


@contextmanager
def populated_blocks() -> Iterator[None]:
    """Serve the maximal `due_state` and `latency` blocks to `op_bootstrap`."""
    from exomem import commands

    block = maximal_due_state()
    latency = maximal_latency()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(due_state, "served", lambda *a, **k: block)
        patch.setattr(due_state, "mark_emitted", lambda *a, **k: None)
        patch.setattr(commands, "_bootstrap_latency_block", lambda: latency)
        yield
