"""A write must never rebuild the whole graph inside the mutation boundary.

Measured on CI before this fix: `upsert_after_write` -> `refresh_paths` took the
vault mutation boundary and, whenever the graph sidecar was missing or
schema-invalid, escalated to a full-vault `_rebuild_all_locked()` inside it --
`hold_ms=39092` over 2,000 pages and `hold_ms=172205` over 8,000. Every other
vault mutation was blocked for that whole window, and the writer's own client
could time out on a write that then landed anyway.

The healing path already exists elsewhere: `graph_drift` reports a missing or
schema-mismatched sidecar, and `reconcile` calls `rebuild_all` for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph
from exomem.kbdir import kb_dirname


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / kb_dirname() / "Notes").mkdir(parents=True)
    (root / kb_dirname() / "Notes" / "one.md").write_text(
        "---\ntitle: One\ntype: note\n---\n\n# One\n\nBody.\n", encoding="utf-8"
    )
    return root


def test_a_write_does_not_rebuild_when_the_sidecar_is_missing(vault, monkeypatch) -> None:
    """The regression: one write must not rebuild the entire vault graph."""

    index = epistemic_graph.EpistemicGraphIndex(vault)
    assert not index.available(), "precondition: no sidecar yet"

    rebuilds: list[int] = []
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_rebuild_all_locked",
        lambda self: rebuilds.append(1) or {},
    )

    result = index.refresh_paths([vault / kb_dirname() / "Notes" / "one.md"])

    assert rebuilds == [], "a write escalated to a full-vault rebuild"
    assert result["deferred"] == 1
    assert result["indexed_files"] == 0


def test_an_unavailable_graph_does_not_even_take_the_boundary(vault, monkeypatch) -> None:
    """Screened before the hold, so an unbuildable graph cannot block writers."""

    index = epistemic_graph.EpistemicGraphIndex(vault)
    holds: list[str] = []
    real_hold = index._mutation_coordinator.hold

    def tracking_hold(*args, **kwargs):
        holds.append(kwargs.get("operation", "unattributed"))
        return real_hold(*args, **kwargs)

    monkeypatch.setattr(index._mutation_coordinator, "hold", tracking_hold)
    index.refresh_paths([vault / kb_dirname() / "Notes" / "one.md"])

    assert holds == [], "an unavailable graph must not contend for the boundary"


def test_the_write_hook_stays_bounded_without_a_sidecar(vault, monkeypatch) -> None:
    """`upsert_after_write` is the real caller; it must not rebuild either."""

    rebuilds: list[int] = []
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_rebuild_all_locked",
        lambda self: rebuilds.append(1) or {},
    )

    epistemic_graph.upsert_after_write(vault, [vault / kb_dirname() / "Notes" / "one.md"])

    assert rebuilds == []


def test_drift_still_reports_the_missing_sidecar(vault) -> None:
    """The deferred work must remain visible, or the graph silently never builds."""

    drift = epistemic_graph.graph_drift(vault)

    assert drift, "a missing sidecar must register as drift for reconcile to heal"
    assert any("sidecar missing" in entry["reason"] for entry in drift)


def test_reconcile_still_owns_the_full_rebuild(vault) -> None:
    """Deferring from the write path is only safe because this path exists."""

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    assert index.available(), "rebuild_all must still build a usable sidecar"
    assert not epistemic_graph.graph_drift(vault), "and must clear the drift it heals"


def test_a_healthy_graph_still_indexes_the_written_path(vault) -> None:
    """Deferring must not disable ordinary incremental indexing."""

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    (vault / kb_dirname() / "Notes" / "two.md").write_text(
        "---\ntitle: Two\ntype: note\n---\n\n# Two\n\nBody.\n", encoding="utf-8"
    )
    result = index.refresh_paths([vault / kb_dirname() / "Notes" / "two.md"])

    assert "deferred" not in result
    assert result["indexed_files"] == 1
