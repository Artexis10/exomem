"""Adoption Studio durable run lifecycle (Lane A backend core)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from exomem import adoption_run, capabilities, operation_entries

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


def test_scoped_status_counts_distinct_filed_originals_across_intersecting_runs(tmp_path: Path) -> None:
    store = adoption_run.AdoptionRunStore(tmp_path)
    for run_id, source_root, outcomes in [
        ("parent", "_Imports", {
            "_Imports/batch/a.md": {"status": "applied"},
            "_Imports/other/outside.md": {"status": "applied"},
        }),
        ("repeat", "_Imports/batch", {
            "_Imports/batch/a.md": {"status": "already-applied"},
            "_Imports/batch/child/b.md": {"status": "applied"},
            "_Imports/batch/owed.md": {"status": "source-applied"},
            "_Imports/batch/failed.md": {"status": "failed", "source": {"status": "applied"}},
        }),
        ("child", "_Imports/batch/child", {
            "_Imports/batch/child/b.md": {"status": "already-applied"},
            "_Imports/batch/child/c.md": {"status": "applied"},
        }),
        ("outside", "_Imports/batch-sibling", {"malformed": None}),
    ]:
        folder = store.run_dir(run_id)
        folder.mkdir(parents=True)
        (folder / "run.json").write_text(json.dumps({
            "run_id": run_id, "source_root": source_root, "outcomes": outcomes,
        }), encoding="utf-8")

    scoped = adoption_run.status(tmp_path, path="_Imports/batch")
    assert scoped["adopted_files"] == 3
    assert scoped["complete"] is True
    assert {row["source_root"] for row in scoped["runs"]} >= {"_Imports", "_Imports/batch"}
    child = adoption_run.status(tmp_path, path="_Imports/batch/child/../child")
    assert child["adopted_files"] == 2
    assert child["complete"] is True
    assert "adopted_files" not in adoption_run.status(tmp_path)
    assert "complete" not in adoption_run.status(tmp_path, path="")


@pytest.mark.parametrize("unreadable", [
    "{", "[]", '{"outcomes": {}}',
    '{"source_root": "../outside", "outcomes": {}}',
    '{"source_root": "_Imports/batch", "outcomes": null}',
    '{"source_root": "_Imports/batch", "outcomes": {"_Imports/batch/a.md": null}}',
    '{"source_root": "_Imports/batch", "outcomes": {"_Imports/batch/a.md": {"status": "unknown"}}}',
    None,
])
def test_scoped_status_never_reports_a_partial_count_as_zero(
    tmp_path: Path, unreadable: str | None,
) -> None:
    store = adoption_run.AdoptionRunStore(tmp_path)
    known = store.run_dir("known")
    known.mkdir(parents=True)
    (known / "run.json").write_text(json.dumps({
        "source_root": "_Imports/batch",
        "outcomes": {"_Imports/batch/known.md": {"status": "applied"}},
    }), encoding="utf-8")
    folder = store.run_dir("unreadable")
    folder.mkdir(parents=True)
    if unreadable is not None:
        (folder / "run.json").write_text(unreadable, encoding="utf-8")

    scoped = adoption_run.status(tmp_path, path="_Imports/batch")
    assert scoped["adopted_files"] is None
    assert scoped["complete"] is False


def test_scoped_status_has_a_complete_zero_before_any_adoption(tmp_path: Path) -> None:
    scoped = adoption_run.status(tmp_path, path=".")
    assert scoped["adopted_files"] == 0
    assert scoped["complete"] is True


@pytest.mark.parametrize(("source_root", "original", "valid_original"), [
    ("", "", "kept.md"),
    ("_Imports/batch", "_Imports/batch", "_Imports/batch/kept.md"),
    ("_Imports/batch", "_Imports/other/outside.md", "_Imports/batch/kept.md"),
])
def test_scoped_status_rejects_malformed_historical_original_paths(
    tmp_path: Path, source_root: str, original: str, valid_original: str,
) -> None:
    folder = adoption_run.AdoptionRunStore(tmp_path).run_dir("historical")
    folder.mkdir(parents=True)
    run = {"source_root": source_root, "outcomes": {
        valid_original: {"status": "applied"}, original: {"status": "applied"},
    }}
    (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")

    scoped = adoption_run.status(tmp_path, path=".")
    assert scoped["adopted_files"] is None
    assert scoped["complete"] is False

    del run["outcomes"][original]
    (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
    historical = adoption_run.status(tmp_path, path=".")
    assert historical["adopted_files"] == 1
    assert historical["complete"] is True


def test_run_directory_read_failure_stays_visible_to_legacy_and_scoped_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = adoption_run.AdoptionRunStore(tmp_path)
    store.base.mkdir(parents=True)
    iterdir = Path.iterdir

    def unreadable(folder: Path):
        if folder == store.base:
            raise PermissionError("cannot read adoption runs")
        return iterdir(folder)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(PermissionError, match="cannot read adoption runs"):
        adoption_run.status(tmp_path)
    scoped = adoption_run.status(tmp_path, path="_Imports/batch")
    assert scoped["adopted_files"] is None
    assert scoped["complete"] is False


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


def test_a_run_serves_its_pack_suggestions_in_the_spelling_of_the_surface_reading_it(
    tmp_path: Path,
) -> None:
    """A run started from the CLI and read over MCP must not name the tools MCP hides."""
    vault = _legacy_vault(tmp_path)
    grouped = set(operation_entries.OPERATIONS)
    run = adoption_run.start(vault, today=TODAY)
    nested = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape="nested"
    )

    with capabilities.active_surface(nested):
        served = adoption_run.status(vault, run_id=run["run_id"])

    assert {name for name in grouped if name in json.dumps(run["pack_suggestions"])}
    assert not {name for name in grouped if name in json.dumps(served["pack_suggestions"])}
    assert [pack["id"] for pack in served["pack_suggestions"]] == [
        pack["id"] for pack in run["pack_suggestions"]
    ]


# --- 2 ---
def test_start_snapshots_candidate_inventory_and_fingerprint(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    before = _snapshot(vault, subdir="Old Notes")

    run_a = adoption_run.start(vault, today=TODAY)
    run_b = adoption_run.start(vault, today=TODAY)

    inv = {row["path"]: row for row in run_a["inventory"]}
    assert inv["Old Notes/quarterly-planning.md"]["eligible"] is True
    assert inv["Old Notes/standup.txt"]["eligible"] is True
    # Every regular file is a candidate: a non-text file imports as an artifact.
    png = inv["Old Notes/diagram.png"]
    assert png["eligible"] is True
    assert png["form"] == "artifact"
    assert png["reason"] is None
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
        "Old Notes/diagram.png",
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
            "Old Notes/missing.md",  # not in inventory
            "Knowledge Base/index.md",  # already governed
        ],
    )
    codes = {row["path"]: row["code"] for row in rejected["rejected"]}
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
    assert applied["verified_total"] == 3

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
    assert finished["finish"]["route"]["tool"] == "search_memory"
    manifest_path = finished["finish"]["manifest_path"]
    assert manifest_path is not None
    assert manifest_path.startswith("Knowledge Base/_Adoption/")
    assert (vault / manifest_path).exists()


def test_staged_cloud_folder_resumes_into_searchable_sources_without_replacing_notes(
    tmp_path: Path,
) -> None:
    """A staged folder must become readable memory, not only a successful upload."""
    from exomem import find, get_page

    vault = _legacy_vault(tmp_path)
    folder = "_Imports/6619ea22-235a-40fb-9664-46cde7cde292"
    staged = vault / folder
    staged.mkdir(parents=True)
    original = f"{folder}/observations.md"
    (vault / original).write_text(
        "# Observations\n\nLibration changes the visible lunar surface. 月面の観測記録。\n",
        encoding="utf-8",
    )
    (staged / "procedure.txt").write_text(
        "Repeat the observation after sunset.\n", encoding="utf-8"
    )
    (staged / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    existing = vault / "Knowledge Base/Notes/existing.md"
    existing.write_text("# Existing conclusion\n\nKeep this conclusion.\n", encoding="utf-8")
    existing_bytes = existing.read_bytes()
    originals = _snapshot(vault, subdir=folder)

    run = adoption_run.start(vault, path=folder, today=TODAY)
    run_id = run["run_id"]
    inventory = {row["path"]: row for row in run["inventory"]}
    assert inventory[f"{folder}/diagram.png"]["form"] == "artifact"
    selected = adoption_run.select(vault, run_id=run_id, include=[folder])
    assert set(selected["selection"]["paths"]) == {
        original,
        f"{folder}/procedure.txt",
        f"{folder}/diagram.png",
    }
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    plan_id = planned["plan"]["plan_id"]

    first = adoption_run.apply(
        vault, run_id=run_id, plan_id=plan_id, only_paths=[original], today=TODAY
    )
    first_path = first["outcomes"][original]["target_path"]
    first_bytes = (vault / first_path).read_bytes()
    resumed = adoption_run.status(vault, run_id=run_id)
    assert resumed["source_root"] == folder
    assert resumed["outcomes"][original]["status"] == "applied"
    assert f"{folder}/procedure.txt" not in resumed["outcomes"]
    applied = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, today=TODAY)
    assert applied["phase"] == "applied"
    assert applied["outcomes"][original]["status"] == "already-applied"
    assert (vault / first_path).read_bytes() == first_bytes
    remaining = applied["outcomes"][f"{folder}/procedure.txt"]
    assert remaining["status"] == "applied"
    remaining_page = get_page.get_page(vault, path=remaining["target_path"])
    assert "Repeat the observation after sunset." in remaining_page.body
    assert remaining_page.frontmatter["imported_from"] == f"{folder}/procedure.txt"
    image = applied["outcomes"][f"{folder}/diagram.png"]
    assert (vault / image["artifact_path"]).read_bytes() == originals[f"{folder}/diagram.png"]

    hits = find.find(vault, query="libration", mode="keyword", graph=False)
    assert first_path in {hit.path for hit in hits}
    page = get_page.get_page(vault, path=first_path)
    assert "Libration changes the visible lunar surface." in page.body
    assert "月面の観測記録。" in page.body
    assert page.frontmatter["imported_from"] == original
    assert page.frontmatter["original_sha256"] == hashlib.sha256(originals[original]).hexdigest()
    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    assert finished["phase"] == "done"
    assert existing.read_bytes() == existing_bytes
    assert _snapshot(vault, subdir=folder) == originals


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
    assert applied["verified_unchanged"] == 3
    assert applied["verified_total"] == 3

    store = adoption_run.AdoptionRunStore(vault)
    persisted = store.load(run_id)
    assert persisted["verify"]["verified_unchanged"] == 3
    assert persisted["verify"]["verified_total"] == 3
    assert persisted["verify"]["at"]

    # Mutate an already-applied original AFTER apply committed. A later status()
    # call must NOT silently re-hash live and report fewer unchanged — it must
    # surface the recorded verify block from apply time, honestly frozen.
    (vault / "Old Notes/quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nEdited after apply.\n", encoding="utf-8"
    )
    status = adoption_run.status(vault, run_id=run_id)
    assert status["verified_unchanged"] == 3
    assert status["verified_total"] == 3

    # finish reuses the same recorded counts rather than a fresh re-hash.
    finished = adoption_run.finish(vault, run_id=run_id, today=TODAY)
    assert finished["verified_unchanged"] == 3
    assert finished["verified_total"] == 3
    assert finished["finish"]["verified_unchanged"] == 3
    assert finished["finish"]["verified_total"] == 3


def test_verify_counts_absent_before_any_apply(tmp_path: Path) -> None:
    """Never fabricate: a run with no applied outcomes carries no verify fields."""
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    assert "verified_unchanged" not in run
    assert "verified_total" not in run
    status = adoption_run.status(vault, run_id=run["run_id"])
    assert "verified_unchanged" not in status
    assert "verified_total" not in status


def _nested_vault(root: Path) -> Path:
    """A vault whose selection needs rule specificity: parent off, child on."""
    vault = _legacy_vault(root)
    work = vault / "Old Notes" / "work"
    work.mkdir()
    (work / "goals.md").write_text("# Goals\n\nShip it.\n", encoding="utf-8")
    recipes = vault / "Recipes"
    recipes.mkdir()
    (recipes / "carbonara.md").write_text("# Carbonara\n", encoding="utf-8")
    return vault


def test_select_rules_apply_by_specificity(tmp_path: Path) -> None:
    """A deeper rule overrides its ancestor regardless of include/exclude order.

    This is the UI's tri-state contract: unchecking a folder then re-checking a
    subfolder sends include=[child], exclude=[parent] and must keep the child.
    """
    vault = _nested_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    run_id = run["run_id"]

    ok = adoption_run.select(
        vault,
        run_id=run_id,
        include=["Old Notes/work", "Recipes"],
        exclude=["Old Notes"],
    )
    assert set(ok["selection"]["paths"]) == {
        "Old Notes/work/goals.md",
        "Recipes/carbonara.md",
    }

    # A file-path exclude is the deepest rule of all: it wins over its folder.
    ok = adoption_run.select(
        vault,
        run_id=run_id,
        include=["Old Notes"],
        exclude=["Old Notes/standup.txt"],
    )
    assert set(ok["selection"]["paths"]) == {
        "Old Notes/diagram.png",
        "Old Notes/quarterly-planning.md",
        "Old Notes/work/goals.md",
    }


def test_apply_commits_exactly_the_previewed_targets(tmp_path: Path) -> None:
    """Apply on a later date must commit the plan's persisted targets, not re-plan."""
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    rid = run["run_id"]
    adoption_run.select(vault, run_id=rid, include=["Old Notes"])
    planned = adoption_run.plan(vault, run_id=rid, today=TODAY)
    plan_targets = {
        it["original_path"]: it["target_path"] for it in planned["plan"]["items"]
    }
    applied = adoption_run.apply(
        vault,
        run_id=rid,
        plan_id=planned["plan"]["plan_id"],
        today=TODAY + dt.timedelta(days=1),
    )
    for op, outcome in applied["outcomes"].items():
        assert outcome["status"] == "applied"
        assert outcome["target_path"] == plan_targets[op]


def test_interrupted_apply_recovers_already_applied_without_duplicates(tmp_path: Path) -> None:
    """A crash after the batch commit but before the outcomes save must not duplicate."""
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    rid = run["run_id"]
    adoption_run.select(vault, run_id=rid, include=["Old Notes"])
    planned = adoption_run.plan(vault, run_id=rid, today=TODAY)
    pid = planned["plan"]["plan_id"]
    adoption_run.apply(vault, run_id=rid, plan_id=pid, today=TODAY)
    imported = vault / "Knowledge Base" / "Sources" / "Imported"
    before = sorted(p.name for p in imported.glob("*.md"))

    store = adoption_run.AdoptionRunStore(vault)
    raw = store.load(rid)
    raw["phase"] = "applying"
    raw["outcomes"] = {}
    raw.pop("verify", None)
    store.save(raw)

    recovered = adoption_run.apply(vault, run_id=rid, plan_id=pid, today=TODAY)
    assert recovered["outcomes"]
    assert all(
        o["status"] == "already-applied" for o in recovered["outcomes"].values()
    )
    assert sorted(p.name for p in imported.glob("*.md")) == before


def test_sync_conflicts_and_zero_byte_files_are_junk_by_default(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    conflict = "Old Notes/notes (conflicted copy).md"
    empty = "Old Notes/empty.md"
    (vault / conflict).write_text("duplicated text\n", encoding="utf-8")
    (vault / empty).write_text("", encoding="utf-8")
    run = adoption_run.start(vault, today=TODAY)
    rows = {r["path"]: r for r in run["inventory"]}
    assert rows[conflict]["junk"] is True
    assert rows[empty]["junk"] is True
    assert rows["Old Notes/quarterly-planning.md"]["junk"] is False

    sel = adoption_run.select(vault, run_id=run["run_id"], include=["Old Notes"])
    assert conflict not in sel["selection"]["paths"]
    assert empty not in sel["selection"]["paths"]
    sel = adoption_run.select(
        vault, run_id=run["run_id"], include=["Old Notes"], include_junk=True
    )
    assert conflict in sel["selection"]["paths"]


def test_handoff_claude_link_targets_the_new_chat_route(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    run = adoption_run.start(vault, today=TODAY)
    status = adoption_run.status(vault, run_id=run["run_id"])
    assert status["handoff"]["links"]["claude"].startswith("claude://claude.ai/new?")
