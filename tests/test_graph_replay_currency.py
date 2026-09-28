"""A replayed path outside the recall delta is proved against the graph first.

On the live 0.96.0 worker the periodic reconcile replayed deferred full-index
receipts through the graph's standalone refresh. Every replayed page had changed
long before the graph's stored checkpoint, so it lay outside the recall delta, the
refresh fell back with `caller_path_outside_delta`, and a standalone caller paid an
in-process whole-vault rebuild -- 59-101 s each, one per isolated receipt, with
concurrent requests at 4-5 s p95.

These tests pin the proof that replaces that fallback: a page the registry records
exactly as the disk has it is either already reflected by its stored row (a no-op)
or stale work the drain repairs incrementally; only a page the registry does not
vouch for still rebuilds the vault. Every case is compared edge for edge with a
fresh whole-vault rebuild.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from test_graph_handoff_convergence import (
    GENERATED,
    _assert_matches_a_fresh_rebuild,
    _build_small,
    _graph_rows,
    _note,
    _titled,
)

from exomem import deferred_index, epistemic_graph, freshness, index_sync
from exomem.epistemic_graph import EpistemicGraphIndex

REPLAYED = f"{GENERATED}/generated-note-0003.md"
RETITLED = f"{GENERATED}/retitled.md"
# One page per title, so the retitle drops one edge and adds the other: a drain
# that does not widen to the retitle's dependants leaves both visibly wrong.
OLD_LINKER = f"{GENERATED}/old-linker.md"
NEW_LINKER = f"{GENERATED}/new-linker.md"


@pytest.fixture
def whole_vault_passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every whole-vault pass, whichever caller starts it."""
    passes: list[str] = []
    real = EpistemicGraphIndex._rebuild_all_off_boundary

    def counted(self, **kwargs):
        passes.append("pass")
        return real(self, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_off_boundary", counted, raising=True)
    return passes


def _built(vault: Path, whole_vault_passes: list[str]) -> Path:
    root = _build_small(
        vault,
        {
            OLD_LINKER: _note(500, []) + "\nSee [[Old Title]].\n",
            NEW_LINKER: _note(501, []) + "\nSee [[New Title]].\n",
            RETITLED: _titled("Old Title", ""),
        },
    )
    whole_vault_passes.clear()
    return root


def _publish_past_a_stale_row(root: Path) -> None:
    """Leave `retitled.md` with a stale row under a checkpoint that already includes it.

    The 7.6 shape: the registry records the retitle, and a drain of another page
    publishes the marker at the registry's checkpoint, so the retitle is behind
    the stored checkpoint -- outside the recall delta -- while its own receipt
    has not drained.
    """
    (root / RETITLED).write_text(_titled("New Title", ""), encoding="utf-8")
    other = root / GENERATED / "generated-note-0007.md"
    other.write_text(other.read_text(encoding="utf-8") + "\n- edit\n", encoding="utf-8")
    freshness.rebaseline(root)
    report = EpistemicGraphIndex(root).drain_paths([other])
    assert report["published"] is True


def test_a_replayed_path_the_graph_already_reflects_rebuilds_nothing(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    before = _graph_rows(root)
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    result = epistemic_graph.upsert_after_write(root, [root / REPLAYED])

    assert whole_vault_passes == [], "a replay the graph already reflects rebuilt the vault"
    assert "caller_path_outside_delta" not in caplog.text
    assert result.outcome in {"completed", "not_required"}, result
    assert _graph_rows(root) == before
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_full_index_receipt_the_graph_reflects_rebuilds_nothing(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The live path: a deferred full-index receipt replayed by the drain."""
    root = _built(vault, whole_vault_passes)
    deferred_index.add_full_receipts(root, [REPLAYED])

    assert index_sync.drain_deferred_work(root, paths=[root / REPLAYED]) == 1

    assert deferred_index.snapshot_full(root) == []
    assert whole_vault_passes == []
    _assert_matches_a_fresh_rebuild(root)


def test_a_current_replayed_path_does_not_hold_back_the_delta(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    root = _built(vault, whole_vault_passes)
    edited = root / GENERATED / "generated-note-0010.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n- edited\n", encoding="utf-8")
    # Recorded as an event, so the edited page is in the recall delta while the
    # replayed page is not; the refresh must still derive the delta.
    freshness.on_files_changed(root, changed=[edited])
    # A serving worker holds its recall resolver resident; a cold one is a
    # separate, queued deferral this test is not about.
    from exomem import find as find_module

    find_module.recall_resolver_snapshot(root)

    epistemic_graph.upsert_after_write(root, [root / REPLAYED, edited])

    assert whole_vault_passes == []
    assert EpistemicGraphIndex(root).available() is True
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_is_drained_incrementally(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    assert whole_vault_passes == [], "a stale page the registry vouches for rebuilt the vault"
    assert "caller_path_outside_delta" not in caplog.text
    assert EpistemicGraphIndex(root).available() is True
    # The retitle moves an edge from one linker to the other; the drain widens to both.
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_page_the_registry_does_not_vouch_for_still_rebuilds(
    vault: Path, whole_vault_passes: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    # Changed again behind the registry's back: its record no longer matches disk.
    (root / RETITLED).write_text(_titled("Newer Title", "moved again"), encoding="utf-8")
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    assert "reason=caller_path_outside_delta" in caplog.text
    assert len(whole_vault_passes) == 1
    _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_does_not_publish_past_a_standing_full_marker(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The drain's first rule: a standing full marker drains the queue with it.

    The replay's repair is the drain's repair, so it must not serve reads as
    current while whole-vault debt is outstanding: either the graph stays
    unavailable or the marker's debt is paid.
    """
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    deferred_index.advance_graph_full_rebuild(root)
    EpistemicGraphIndex(root)._mark_unavailable()
    whole_vault_passes.clear()

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    if EpistemicGraphIndex(root).available():
        assert deferred_index.graph_full_rebuild_pending(root) is None, (
            "reads served as current while the whole-vault debt stands"
        )
        assert whole_vault_passes, "the full marker's debt was never paid"
        assert deferred_index.snapshot_graph(root) == [], "left receipts for a duplicate drain"
        _assert_matches_a_fresh_rebuild(root)
    else:
        assert deferred_index.graph_full_rebuild_pending(root) is not None


def test_a_replayed_stale_page_is_not_repaired_against_an_unsettled_epoch(
    vault: Path, whole_vault_passes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain's second rule: per-path repair needs a lineage it can classify."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    drained: list[list[Path]] = []
    real_drain = EpistemicGraphIndex.drain_paths

    def recorded(self, paths, *args, **kwargs):
        drained.append(list(paths))
        return real_drain(self, paths, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "drain_paths", recorded, raising=True)
    monkeypatch.setattr(
        EpistemicGraphIndex, "epoch_admits_incremental_repair", lambda self: False, raising=True
    )

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    assert drained == [], "repaired paths against a lineage it could not classify"
    if EpistemicGraphIndex(root).available():
        assert whole_vault_passes, "published without repairing the stale row"
        _assert_matches_a_fresh_rebuild(root)


def test_a_replayed_stale_page_clears_its_own_receipts(
    vault: Path, whole_vault_passes: list[str]
) -> None:
    """The drain's third rule: receipts the repair covered clear by CAS."""
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    assert whole_vault_passes == []
    assert deferred_index.snapshot_graph(root) == [], "left receipts for a duplicate drain"
    _assert_matches_a_fresh_rebuild(root)


def test_the_oracle_sees_a_replay_drain_that_does_not_widen(
    vault: Path, whole_vault_passes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the stale-page test must be able to see missing widening.

    The retitle drops one linker's edge and gives the other one; a drain that
    re-derives only the retitled page leaves both linkers as they were, and the
    comparison with a fresh rebuild has to say so.
    """
    root = _built(vault, whole_vault_passes)
    _publish_past_a_stale_row(root)
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_topology_affected_sources",
        lambda self, conn, rels, *, resolver: set(),
        raising=True,
    )

    epistemic_graph.upsert_after_write(root, [root / RETITLED])

    assert whole_vault_passes == [], "a rebuild healed what the control must expose"
    with pytest.raises(AssertionError):
        _assert_matches_a_fresh_rebuild(root)
