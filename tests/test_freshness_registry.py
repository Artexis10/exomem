"""Event-maintained freshness registry (OpenSpec: event-maintained-indexes, D1).

Pins `freshness.py`'s contract: the live registry's derived triple must be
byte-identical to a fresh stat-walk (`find._walk_freshness_key` over
`find._walk_md` / `vault.walk_vault_md`) at every point in its lifecycle —
seeded, patched incrementally (create/modify/move/delete), reconciled after a
missed event, or falling back when not live / kill-switched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness
from exomem import vault as vault_module


@pytest.fixture(autouse=True)
def _clear_freshness():
    freshness.clear()
    yield
    freshness.clear()


def _fresh_walk_triple(vault_root: Path, scope: str) -> tuple[int, int, str]:
    """Independent ground truth: a fresh walk, computed the same way the
    not-live fallback would compute it."""
    if scope == "kb":
        kb_dir = vault_root / "Knowledge Base"
        return find_module._walk_freshness_key(find_module._walk_md(kb_dir))
    return find_module._walk_freshness_key(vault_module.walk_vault_md(vault_root))


def _seed_both_scopes(vault_root: Path) -> None:
    kb_dir = vault_root / "Knowledge Base"
    freshness.seed(
        vault_root, "kb",
        ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb_dir)),
    )
    freshness.seed(
        vault_root, "vault",
        ((str(p), p.stat().st_mtime_ns) for p in vault_module.walk_vault_md(vault_root)),
    )


# ---------------- triple_from_entries ----------------


def test_triple_from_entries_deterministic_and_order_independent() -> None:
    entries_a = [("b.md", 200), ("a.md", 100), ("c.md", 300)]
    entries_b = [("c.md", 300), ("a.md", 100), ("b.md", 200)]
    entries_c = [("a.md", 100), ("b.md", 200), ("c.md", 300)]
    triple_a = freshness.triple_from_entries(entries_a)
    triple_b = freshness.triple_from_entries(entries_b)
    triple_c = freshness.triple_from_entries(entries_c)
    assert triple_a == triple_b == triple_c
    count, latest, digest = triple_a
    assert count == 3
    assert latest == 300
    assert isinstance(digest, str) and digest


# ---------------- seed / triple parity with the walk ----------------


def test_registry_seeded_from_walk_equals_walk_triple_both_scopes(vault: Path) -> None:
    kb_dir = vault / "Knowledge Base"
    kb_ground_truth = find_module._walk_freshness_key(find_module._walk_md(kb_dir))
    freshness.seed(
        vault, "kb",
        ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb_dir)),
    )
    assert freshness.triple(vault, "kb") == kb_ground_truth

    vault_ground_truth = find_module._walk_freshness_key(vault_module.walk_vault_md(vault))
    freshness.seed(
        vault, "vault",
        ((str(p), p.stat().st_mtime_ns) for p in vault_module.walk_vault_md(vault)),
    )
    assert freshness.triple(vault, "vault") == vault_ground_truth


# ---------------- FreshnessSnapshot: live vs fallback ----------------


def test_freshness_snapshot_reads_live_registry_when_seeded(vault: Path) -> None:
    _seed_both_scopes(vault)
    kb_truth = _fresh_walk_triple(vault, "kb")
    vault_truth = _fresh_walk_triple(vault, "vault")

    snap = find_module.FreshnessSnapshot(vault)
    assert snap.kb() == kb_truth
    assert snap.vault() == vault_truth


def test_freshness_snapshot_falls_back_to_walk_when_not_seeded(vault: Path) -> None:
    # freshness.clear() already ran in the autouse fixture — nothing seeded.
    kb_truth = _fresh_walk_triple(vault, "kb")
    vault_truth = _fresh_walk_triple(vault, "vault")

    snap = find_module.FreshnessSnapshot(vault)
    assert snap.kb() == kb_truth
    assert snap.vault() == vault_truth


# ---------------- kill switch ----------------


def test_kill_switch_disables_live_triple_but_snapshot_still_matches_walk(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_both_scopes(vault)
    assert freshness.triple(vault, "kb") is not None

    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    assert freshness.triple(vault, "kb") is None

    kb_truth = _fresh_walk_triple(vault, "kb")
    snap = find_module.FreshnessSnapshot(vault)
    assert snap.kb() == kb_truth


# ---------------- incremental parity: create/modify/move/delete ----------------


def test_incremental_parity_create_kb_note(vault: Path) -> None:
    _seed_both_scopes(vault)

    new_file = vault / "Knowledge Base" / "Notes" / "Insights" / "probe-fresh-create.md"
    new_file.write_text("# Probe create\n\nbody\n", encoding="utf-8")

    freshness.on_files_changed(vault, changed=[new_file], deleted=[])

    assert freshness.triple(vault, "kb") == _fresh_walk_triple(vault, "kb")
    assert freshness.triple(vault, "vault") == _fresh_walk_triple(vault, "vault")


def test_incremental_parity_modify_kb_note(vault: Path) -> None:
    _seed_both_scopes(vault)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    freshness.on_files_changed(vault, changed=[target], deleted=[])

    assert freshness.triple(vault, "kb") == _fresh_walk_triple(vault, "kb")
    assert freshness.triple(vault, "vault") == _fresh_walk_triple(vault, "vault")


def test_incremental_parity_move_kb_note(vault: Path) -> None:
    _seed_both_scopes(vault)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    dest = target.with_name("moved-" + target.name)
    os.replace(target, dest)

    freshness.on_files_changed(vault, changed=[dest], deleted=[target])

    assert freshness.triple(vault, "kb") == _fresh_walk_triple(vault, "kb")
    assert freshness.triple(vault, "vault") == _fresh_walk_triple(vault, "vault")


def test_incremental_parity_delete_kb_note(vault: Path) -> None:
    _seed_both_scopes(vault)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    target.unlink()

    freshness.on_files_changed(vault, changed=[], deleted=[target])

    assert freshness.triple(vault, "kb") == _fresh_walk_triple(vault, "kb")
    assert freshness.triple(vault, "vault") == _fresh_walk_triple(vault, "vault")


# ---------------- scope boundaries ----------------


def test_scope_boundary_sibling_folder_updates_only_vault(vault: Path) -> None:
    _seed_both_scopes(vault)
    kb_before = freshness.triple(vault, "kb")

    ref_dir = vault / "Reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    new_file = ref_dir / "probe-sibling.md"
    new_file.write_text("# Sibling probe\n", encoding="utf-8")

    freshness.on_files_changed(vault, changed=[new_file], deleted=[])

    assert freshness.triple(vault, "kb") == kb_before
    assert freshness.triple(vault, "vault") == _fresh_walk_triple(vault, "vault")


def test_scope_boundary_schema_dir_updates_neither(vault: Path) -> None:
    _seed_both_scopes(vault)
    kb_before = freshness.triple(vault, "kb")
    vault_before = freshness.triple(vault, "vault")

    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    new_file = schema_dir / "probe-schema.md"
    new_file.write_text("# Schema probe\n", encoding="utf-8")

    freshness.on_files_changed(vault, changed=[new_file], deleted=[])

    assert freshness.triple(vault, "kb") == kb_before
    assert freshness.triple(vault, "vault") == vault_before


# ---------------- reconcile() ----------------


def test_reconcile_detects_and_heals_a_missed_event(vault: Path) -> None:
    _seed_both_scopes(vault)
    stale_triple = freshness.triple(vault, "kb")

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))
    # No on_files_changed call here — simulates a missed watchdog event.

    fresh_truth = _fresh_walk_triple(vault, "kb")
    assert stale_triple != fresh_truth
    assert freshness.triple(vault, "kb") == stale_triple  # still stale before reconcile

    kb_dir = vault / "Knowledge Base"
    fresh_entries = ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb_dir))
    drifted = freshness.reconcile(vault, "kb", fresh_entries)

    assert drifted is True
    assert freshness.triple(vault, "kb") == fresh_truth


def test_reconcile_with_no_drift_returns_false(vault: Path) -> None:
    _seed_both_scopes(vault)

    kb_dir = vault / "Knowledge Base"
    fresh_entries = ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb_dir))
    drifted = freshness.reconcile(vault, "kb", fresh_entries)

    assert drifted is False
    assert freshness.triple(vault, "kb") == _fresh_walk_triple(vault, "kb")


# ---------------- invalidate() / clear() ----------------


def test_invalidate_drops_live_state_for_one_vault(vault: Path) -> None:
    _seed_both_scopes(vault)
    assert freshness.triple(vault, "kb") is not None
    assert freshness.triple(vault, "vault") is not None

    freshness.invalidate(vault)

    assert freshness.triple(vault, "kb") is None
    assert freshness.triple(vault, "vault") is None


def test_clear_drops_everything(vault: Path) -> None:
    _seed_both_scopes(vault)
    assert freshness.triple(vault, "kb") is not None

    freshness.clear()

    assert freshness.triple(vault, "kb") is None
    assert freshness.triple(vault, "vault") is None
