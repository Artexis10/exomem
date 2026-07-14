"""Adoption Studio durable run lifecycle (Lane A backend core)."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from exomem import adoption_run

TODAY = dt.date(2026, 7, 14)


def _snapshot(root: Path, *, subdir: str | None = None) -> dict[str, bytes]:
    """Byte snapshot of files under root (optionally a subtree)."""
    base = root if subdir is None else root / subdir
    out: dict[str, bytes] = {}
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _non_run_snapshot(root: Path) -> dict[str, bytes]:
    """Every file except the durable run objects under _Adoption/runs/."""
    runs = (root / "Knowledge Base" / "_Adoption" / "runs").resolve()
    out: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            p.resolve().relative_to(runs)
            continue
        except ValueError:
            pass
        out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _legacy_vault(root: Path, *, kb: bool = True) -> Path:
    vault = root / "vault"
    old = vault / "Old Notes"
    old.mkdir(parents=True)
    (old / "quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nShip the adoption studio this quarter.\n",
        encoding="utf-8",
    )
    (old / "standup.txt").write_text("standup: nothing blocking\n", encoding="utf-8")
    (old / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n binary-not-text")
    if kb:
        kb_root = vault / "Knowledge Base"
        (kb_root / "Notes").mkdir(parents=True)
        sources = kb_root / "Sources"
        sources.mkdir(parents=True)
        (sources / "index.md").write_text(
            "# Sources - Index\n\n## By type\n\n## Recent captures\n\n",
            encoding="utf-8",
        )
        (kb_root / "index.md").write_text(
            "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n",
            encoding="utf-8",
        )
        (kb_root / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    return vault


def _select_all(vault: Path, run_id: str) -> dict:
    return adoption_run.select(vault, run_id=run_id, include=["Old Notes"])


# --- 1 ---
def test_start_requires_kb_and_initialize_kb_flag_bootstraps(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    before = _snapshot(vault, subdir="Old Notes")

    with pytest.raises(adoption_run.AdoptionRunError) as ei:
        adoption_run.start(vault, today=TODAY)
    assert ei.value.code == "KB_NOT_INITIALIZED"
    assert not (vault / "Knowledge Base").exists()

    run = adoption_run.start(vault, initialize_kb=True, today=TODAY)
    assert (vault / "Knowledge Base").is_dir()
    assert run["phase"] == "selecting"
    assert run["run_id"].startswith("adr-")
    # Originals untouched by bootstrap + scan.
    assert _snapshot(vault, subdir="Old Notes") == before


# --- 2 ---
def test_start_snapshots_candidate_inventory_and_fingerprint(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    before = _snapshot(vault, subdir="Old Notes")

    run_a = adoption_run.start(vault, today=TODAY)
    run_b = adoption_run.start(vault, today=TODAY)

    inv = {row["path"]: row for row in run_a["inventory"]}
    assert inv["Old Notes/quarterly-planning.md"]["eligible"] is True
    assert inv["Old Notes/standup.txt"]["eligible"] is True
    png = inv["Old Notes/diagram.png"]
    assert png["eligible"] is False
    assert png["reason"] == "UNSUPPORTED_IMPORT_TYPE"
    # Governed KB files are never adoption candidates.
    assert not any(p.startswith("Knowledge Base/") for p in inv)
    # Deterministic stat fingerprint across two scans of an unchanged subtree.
    assert run_a["inventory_fingerprint"] == run_b["inventory_fingerprint"]
    assert run_a["inventory_fingerprint"]
    # Scan wrote nothing outside the run object.
    assert _snapshot(vault, subdir="Old Notes") == before


# --- 3 ---
def test_select_validates_paths_and_invalidates_plan(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]

    ok = adoption_run.select(vault, run_id=run_id, include=["Old Notes"])
    assert set(ok["selection"]["paths"]) == {
        "Old Notes/quarterly-planning.md",
        "Old Notes/standup.txt",
    }
    assert ok["phase"] == "selecting"
    assert ok["rejected"] == []

    rejected = adoption_run.select(
        vault,
        run_id=run_id,
        include=["Old Notes"],
        overrides=[
            "Old Notes/diagram.png",  # unsupported
            "Old Notes/missing.md",  # not in inventory
            "Knowledge Base/index.md",  # already governed
        ],
    )
    codes = {row["path"]: row["code"] for row in rejected["rejected"]}
    assert codes["Old Notes/diagram.png"] == "UNSUPPORTED_IMPORT_TYPE"
    assert codes["Old Notes/missing.md"] == "NOT_IN_INVENTORY"
    assert codes["Knowledge Base/index.md"] == "ALREADY_GOVERNED"

    # Plan then a selection change clears the plan and returns to selecting.
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    assert planned["phase"] == "planned"
    changed = adoption_run.select(
        vault, run_id=run_id, include=["Old Notes/quarterly-planning.md"]
    )
    assert changed["phase"] == "selecting"
    assert changed["plan"] is None
    assert changed["selection"]["paths"] == ["Old Notes/quarterly-planning.md"]


# --- 4 ---
def test_plan_previews_exact_targets_frontmatter_and_hashes(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)

    before = _non_run_snapshot(vault)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    after = _non_run_snapshot(vault)
    # plan writes nothing outside the run object (targets previewed, not created).
    assert before == after

    items = {it["original_path"]: it for it in planned["plan"]["items"]}
    q = items["Old Notes/quarterly-planning.md"]
    expected_hash = hashlib.sha256(
        (vault / "Old Notes/quarterly-planning.md").read_bytes()
    ).hexdigest()
    assert q["original_sha256"] == expected_hash
    assert q["target_path"] == (
        "Knowledge Base/Sources/Imported/2026-07-14-quarterly-planning-notes.md"
    )
    assert q["title"] == "Quarterly Planning Notes"
    assert q["frontmatter"]["imported_from"] == "Old Notes/quarterly-planning.md"
    assert q["frontmatter"]["original_sha256"] == expected_hash

    # Stable plan_id for identical inputs.
    planned2 = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    assert planned2["plan"]["plan_id"] == planned["plan"]["plan_id"]


# --- 5 ---
def test_apply_requires_current_plan_id_and_selection_hash(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    good_plan_id = planned["plan"]["plan_id"]

    before = _non_run_snapshot(vault)
    with pytest.raises(adoption_run.AdoptionRunError) as ei:
        adoption_run.apply(vault, run_id=run_id, plan_id="deadbeefdeadbeef", today=TODAY)
    assert ei.value.code == "PLAN_STALE"
    assert _non_run_snapshot(vault) == before

    # Changing the selection invalidates the plan; the old plan_id is now stale.
    adoption_run.select(
        vault, run_id=run_id, include=["Old Notes/quarterly-planning.md"]
    )
    with pytest.raises(adoption_run.AdoptionRunError) as ei2:
        adoption_run.apply(vault, run_id=run_id, plan_id=good_plan_id, today=TODAY)
    assert ei2.value.code in {"PLAN_STALE", "INVALID_PHASE"}
    assert _non_run_snapshot(vault) == before


# --- 6 ---
def test_apply_copies_with_provenance_and_updates_indexes(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    originals_before = _snapshot(vault, subdir="Old Notes")
    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)

    assert applied["phase"] == "applied"
    # Originals byte-identical.
    assert _snapshot(vault, subdir="Old Notes") == originals_before
    # Post-apply verification is honest and complete.
    assert applied["verified_unchanged"] == applied["verified_total"]
    assert applied["verified_total"] == 2

    imported = vault / "Knowledge Base/Sources/Imported/2026-07-14-quarterly-planning-notes.md"
    assert imported.exists()
    text = imported.read_text(encoding="utf-8")
    assert "imported_from: Old Notes/quarterly-planning.md" in text
    expected_hash = hashlib.sha256(
        (vault / "Old Notes/quarterly-planning.md").read_bytes()
    ).hexdigest()
    assert f"original_sha256: {expected_hash}" in text
    # Sources index + log updated.
    log = (vault / "Knowledge Base/log.md").read_text(encoding="utf-8")
    assert "adopt-copy" in log
    outcomes = applied["outcomes"]
    assert outcomes["Old Notes/quarterly-planning.md"]["status"] == "applied"


# --- 7 ---
def test_apply_partial_failure_records_per_item_outcomes(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    # Mutate one selected original after plan but before apply.
    (vault / "Old Notes/standup.txt").write_text("standup: CHANGED\n", encoding="utf-8")

    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert applied["phase"] == "partial"
    outcomes = applied["outcomes"]
    assert outcomes["Old Notes/quarterly-planning.md"]["status"] == "applied"
    assert outcomes["Old Notes/standup.txt"]["status"] == "failed"
    assert outcomes["Old Notes/standup.txt"]["code"] == "SOURCE_CHANGED"


# --- 8 ---
def test_apply_is_idempotent_and_retry_failed_replans_subset(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    original_standup = (vault / "Old Notes/standup.txt").read_bytes()
    (vault / "Old Notes/standup.txt").write_text("standup: CHANGED\n", encoding="utf-8")
    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert applied["phase"] == "partial"

    # Re-apply the same plan: already-applied item is an idempotent skip, no dup.
    imported_dir = vault / "Knowledge Base/Sources/Imported"
    before_files = sorted(p.name for p in imported_dir.iterdir())
    reapplied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert (
        reapplied["outcomes"]["Old Notes/quarterly-planning.md"]["status"]
        == "already-applied"
    )
    assert sorted(p.name for p in imported_dir.iterdir()) == before_files

    # Restore the failed original and retry only the failed subset.
    (vault / "Old Notes/standup.txt").write_bytes(original_standup)
    retried = adoption_run.apply(
        vault,
        run_id=run_id,
        plan_id=plan_id,
        retry_failed=True,
        only_paths=["Old Notes/standup.txt"],
        today=TODAY,
    )
    assert retried["phase"] == "applied"
    assert retried["outcomes"]["Old Notes/standup.txt"]["status"] == "applied"


# --- 9 ---
def test_interrupted_apply_is_visible_and_recoverable(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    # Simulate a crash mid-apply: persist phase `applying` with no outcomes.
    store = adoption_run.AdoptionRunStore(vault)
    doc = store.load(run_id)
    doc["phase"] = "applying"
    doc["outcomes"] = {}
    store.save(doc)

    status = adoption_run.status(vault, run_id=run_id)
    assert status["interrupted"] is True

    recovered = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert recovered["phase"] == "applied"
    assert (
        recovered["outcomes"]["Old Notes/quarterly-planning.md"]["status"] == "applied"
    )


# --- 10 ---
def test_cancel_rules(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)

    # Allowed while selecting.
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    cancelled = adoption_run.cancel(vault, run_id=run_id, why="changed my mind")
    assert cancelled["phase"] == "cancelled"
    assert cancelled["cancel"]["why"] == "changed my mind"

    # Refused during applying.
    run2 = adoption_run.start(vault, today=TODAY)
    rid2 = run2["run_id"]
    _select_all(vault, rid2)
    adoption_run.plan(vault, run_id=rid2, today=TODAY)
    store = adoption_run.AdoptionRunStore(vault)
    doc = store.load(rid2)
    doc["phase"] = "applying"
    store.save(doc)
    with pytest.raises(adoption_run.AdoptionRunError) as ei:
        adoption_run.cancel(vault, run_id=rid2, why="stop")
    assert ei.value.code == "CANCEL_DURING_APPLY"

    # Refused after applied; applied Sources survive.
    run3 = adoption_run.start(vault, today=TODAY)
    rid3 = run3["run_id"]
    _select_all(vault, rid3)
    p3 = adoption_run.plan(vault, run_id=rid3, today=TODAY)
    adoption_run.apply(vault, run_id=rid3, plan_id=p3["plan"]["plan_id"], today=TODAY)
    imported = vault / "Knowledge Base/Sources/Imported/2026-07-14-quarterly-planning-notes.md"
    assert imported.exists()
    with pytest.raises(adoption_run.AdoptionRunError) as ei3:
        adoption_run.cancel(vault, run_id=rid3, why="undo")
    assert ei3.value.code == "ALREADY_APPLIED"
    assert imported.exists()


# --- 11 ---
def test_status_flags_stale_selection(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    adoption_run.plan(vault, run_id=run_id, today=TODAY)

    originals_before = _snapshot(vault, subdir="Old Notes")
    # Touch a selected original after plan.
    (vault / "Old Notes/quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nEdited after plan.\n", encoding="utf-8"
    )
    status = adoption_run.status(vault, run_id=run_id)
    assert "Old Notes/quarterly-planning.md" in status["stale_paths"]
    # status is read-only for the originals it probes (it only changed because we did).
    assert _snapshot(vault, subdir="Old Notes") != originals_before  # our edit only


# --- 12 ---
def test_finish_runs_recall_check_and_first_question(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    adoption_run.apply(vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY)

    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    assert finished["phase"] == "done"
    recall = finished["finish"]["recall_check"]
    assert recall["ok"] is True
    assert "Quarterly Planning Notes" in finished["finish"]["first_question"]
    assert finished["finish"]["route"]["tool"] == "ask_memory"
    manifest_path = finished["finish"]["manifest_path"]
    assert manifest_path is not None
    assert manifest_path.startswith("Knowledge Base/_Adoption/")
    assert (vault / manifest_path).exists()


# --- fix round: run-level ADOPTION_SOURCE_CHANGED, distinct from PLAN_STALE ---
def test_apply_refuses_with_adoption_source_changed_when_every_source_drifted(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Old Notes/quarterly-planning.md"])
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    # The sole selected/planned original drifts after plan but before apply — a
    # correct plan_id and selection_hash are echoed (NOT a PLAN_STALE case), but
    # write-time re-validation finds nothing left to commit.
    (vault / "Old Notes/quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nRewritten entirely after plan.\n", encoding="utf-8"
    )
    before = _non_run_snapshot(vault)

    with pytest.raises(adoption_run.AdoptionRunError) as ei:
        adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert ei.value.code == "ADOPTION_SOURCE_CHANGED"

    # Refused before any write: run.json and every vault file are untouched, and
    # the still-valid selection survives so the client can re-scan/re-plan.
    assert _non_run_snapshot(vault) == before
    status = adoption_run.status(vault, run_id=run_id)
    assert status["phase"] == "planned"
    assert status["selection"]["paths"] == ["Old Notes/quarterly-planning.md"]


def test_apply_partial_retry_of_still_failing_item_does_not_raise(tmp_path: Path) -> None:
    """A retry that still fails for one item, while others are already applied,
    stays a normal partial response — ADOPTION_SOURCE_CHANGED is reserved for a
    total washout, not a retry of one stubborn item."""
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    (vault / "Old Notes/standup.txt").write_text("standup: CHANGED\n", encoding="utf-8")
    first = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert first["phase"] == "partial"

    # Retry the still-failing item without restoring it: it fails again, but
    # since quarterly-planning is already applied this must NOT raise.
    retried = adoption_run.apply(
        vault,
        run_id=run_id,
        plan_id=plan_id,
        retry_failed=True,
        only_paths=["Old Notes/standup.txt"],
        today=TODAY,
    )
    assert retried["phase"] == "partial"
    assert retried["outcomes"]["Old Notes/standup.txt"]["status"] == "failed"


# --- fix round: persisted verify counts, never a live re-hash fabrication ---
def test_apply_persists_verify_block_and_status_reuses_recorded_counts(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]
    _select_all(vault, run_id)
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert applied["verified_unchanged"] == 2
    assert applied["verified_total"] == 2

    store = adoption_run.AdoptionRunStore(vault)
    persisted = store.load(run_id)
    assert persisted["verify"]["verified_unchanged"] == 2
    assert persisted["verify"]["verified_total"] == 2
    assert persisted["verify"]["at"]

    # Mutate an already-applied original AFTER apply committed. A later status()
    # call must NOT silently re-hash live and report fewer unchanged — it must
    # surface the recorded verify block from apply time, honestly frozen.
    (vault / "Old Notes/quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nEdited after apply.\n", encoding="utf-8"
    )
    status = adoption_run.status(vault, run_id=run_id)
    assert status["verified_unchanged"] == 2
    assert status["verified_total"] == 2

    # finish reuses the same recorded counts rather than a fresh re-hash.
    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    assert finished["verified_unchanged"] == 2
    assert finished["verified_total"] == 2
    assert finished["finish"]["verified_unchanged"] == 2
    assert finished["finish"]["verified_total"] == 2


def test_verify_counts_absent_before_any_apply(tmp_path: Path) -> None:
    """Never fabricate: a run with no applied outcomes carries no verify fields."""
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    assert "verified_unchanged" not in run
    assert "verified_total" not in run
    status = adoption_run.status(vault, run_id=run["run_id"])
    assert "verified_unchanged" not in status
    assert "verified_total" not in status
