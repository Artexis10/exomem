"""Residual graph lag is a reported dimension (task 7.3).

A catch-up publication or a truncated drain leaves the graph behind the
canonical checkpoint with every skipped generation queued as repair. Readers
still refuse there -- the rows behind the queue are stale -- but the refusal
is convergence in progress, not a fault, and the surfaces that report it must
say which: `graph_context` names it and carries the lag, and the doctor warns
rather than failing while the lag is receipt-covered and young. An uncovered
gap still fails. Reading the lag is cheap enough for the refusal path: no
vault walk.

OpenSpec `graph-incremental-convergence`, "Residual graph lag is reported".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from exomem import deferred_index, doctor, epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/lag-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/lag-b.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(root / "Knowledge Base")
        ),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Any:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[lag-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    # A canonical checkpoint the graph acknowledges, so the next one is a gap.
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(root / PAGE_B, _page("B", "B is revised once."))],
        vault_root=root,
    )
    assert graph_sync.status(root)["state"] == "current"
    yield root
    epistemic_graph.clear_publication_memos()


def _fall_behind(root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """One canonical batch left to the queue: receipt-covered lag, one generation."""
    with monkeypatch.context() as patch:
        patch.setattr(epistemic_graph, "graph_scheduling_enabled", lambda: False)
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(root / PAGE_A, _page("A", "A is revised."))],
            vault_root=root,
        )
    checkpoint = graph_sync.read_checkpoint(root)
    assert checkpoint is not None
    assert graph_sync.status(root)["state"] == "recovery_required"
    return int(checkpoint.generation)


def test_graph_context_reports_catching_up_with_its_lag(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = _fall_behind(vault, monkeypatch)

    context = epistemic_graph.graph_context(vault, path=PAGE_B)

    assert context["available"] is False
    assert context["reason"] == "graph catching up"
    lag = context["lag"]
    assert lag["committed_generation"] == committed
    assert lag["generations_behind"] == 1
    assert lag["queued_paths"] >= 1
    assert lag["gap_receipt_covered"] is True
    assert lag["catching_up"] is True


def test_doctor_warns_rather_than_fails_on_receipt_covered_lag(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fall_behind(vault, monkeypatch)

    check = doctor._check_graph_sync_state(vault)

    assert check.status == "warn", check.message
    assert check.details is not None
    assert check.details["lag"]["catching_up"] is True


def test_doctor_still_fails_an_uncovered_gap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation neither queued nor debt-recorded is divergence, not lag."""
    committed = _fall_behind(vault, monkeypatch)
    # The batch's enqueue lost, receipts and debt record both.
    deferred_index.clear_graph(vault, [PAGE_A])
    with sqlite3.connect(deferred_index.store_path(vault)) as conn:
        conn.execute("DELETE FROM graph_debt_generations WHERE generation = ?", (committed,))

    check = doctor._check_graph_sync_state(vault)
    context = epistemic_graph.graph_context(vault, path=PAGE_B)

    assert check.status == "fail"
    assert context["reason"] == "graph sidecar unavailable"
    assert "lag" not in context


def test_graph_lag_is_cheap_to_read(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal path reads it on every refused read: no vault walk."""
    _fall_behind(vault, monkeypatch)
    walks: list[str] = []

    def forbid(name: str) -> Any:
        def walk(*_args: Any, **_kwargs: Any) -> Any:
            walks.append(name)
            raise AssertionError(f"graph_lag walked the vault through {name}")

        return walk

    monkeypatch.setattr(vault_module, "walk_vault_md", forbid("walk_vault_md"))
    monkeypatch.setattr(find_module, "_walk_md", forbid("_walk_md"))
    monkeypatch.setattr(epistemic_graph, "_disk_vault_freshness", forbid("_disk_vault_freshness"))

    lag = epistemic_graph.graph_lag(vault)

    assert walks == []
    assert lag["catching_up"] is True
