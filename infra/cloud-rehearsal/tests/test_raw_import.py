"""The raw-data import rehearsal's own decisions: per-item parity with local, the gaps that stop a pass, and the
pre-existing page check.

Pure logic over small invented keys. The run itself needs a K3s cell and is exercised by the `raw-import` workflow.
"""

from __future__ import annotations

from cloud_rehearsal import raw_import
from cloud_rehearsal.raw_import import FAIL, INCOMPLETE, PASS, Snapshot


def _key(*note_paths, junk=(), links=()):
    """Notes `note_paths`, each linking to the targets that `links` names as (source, target)."""
    notes = [{"kind": "note", "path": path, "lang": "en", "nfd_filename": False, "embeds": [],
              "links": [{"resolution": "resolvable", "target": target} for source, target in links if source == path]}
             for path in note_paths]
    return {"items": [*notes, *({"kind": "junk", "path": path} for path in junk)]}


def _arm(links=None, junk=None, **pages):
    """A scored arm's `per_item` block: key path -> number of pages it came out as."""
    return {"pages_per_item": pages, "junk_pages": junk or {}, "links_kept": links or {}, "embeds_kept": {}}


def _verdict(cloud, local):
    groups = raw_import.compare_arms(_key("a.md", "b.md"), cloud, local)
    return raw_import.target_verdict(groups), groups["notes"]


# --- parity ----------------------------------------------------------------------------------


def test_a_note_that_local_kept_and_cloud_did_not_is_a_loss():
    verdict, notes = _verdict(_arm(**{"a.md": 1, "b.md": 0}), _arm(**{"a.md": 1, "b.md": 1}))
    assert verdict == FAIL
    assert notes["loss"] == {"count": 1, "listed": ["b.md"]}


def test_a_note_that_only_cloud_kept_is_reported_but_does_not_fail_the_run():
    verdict, notes = _verdict(_arm(**{"a.md": 1, "b.md": 1}), _arm(**{"a.md": 1, "b.md": 0}))
    assert verdict == PASS
    assert notes["cloud_extra"] == {"count": 1, "listed": ["b.md"]}


def test_an_arm_that_was_not_scored_leaves_every_item_unknown_and_never_passes():
    verdict, notes = _verdict(None, _arm(**{"a.md": 1, "b.md": 1}))
    assert verdict == INCOMPLETE
    assert notes["unknown"]["count"] == 2


def test_a_loss_outranks_an_unknown_in_the_same_target():
    groups = raw_import.compare_arms(_key("a.md", "b.md"), _arm(**{"a.md": 0}), _arm(**{"a.md": 1, "b.md": 1}))
    assert raw_import.target_verdict(groups) == FAIL


def test_a_second_page_for_an_item_that_local_filed_once_is_a_loss():
    groups = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 2}), _arm(**{"a.md": 1}))
    assert groups["no_duplicate_pages"]["loss"]["count"] == 1
    assert groups["notes"]["loss"]["count"] == 0


def test_a_target_that_was_never_judged_keeps_the_run_from_passing():
    assert raw_import.worst([PASS, None]) == INCOMPLETE
    assert raw_import.worst([PASS, INCOMPLETE, FAIL]) == FAIL


def test_losses_are_grouped_by_what_the_key_path_shows():
    key = {"items": [{"kind": "note", "path": path, "lang": "en", "nfd_filename": False, "links": [], "embeds": []}
                     for path in ("a.md", "docs/.hidden.md", "docs/b.txt")]}
    groups = raw_import.compare_arms(key, _arm(**{"a.md": 1, "docs/.hidden.md": 0, "docs/b.txt": 0}),
                                     _arm(**{"a.md": 1, "docs/.hidden.md": 1, "docs/b.txt": 1}))
    assert {cause: row["count"] for cause, row in groups["notes"]["loss_causes"].items()} == {"hidden path": 1, "extension .txt": 1}


# --- junk: kept means kept out ---------------------------------------------------------------


def test_junk_that_cloud_let_in_and_local_refused_is_a_loss():
    groups = raw_import.compare_arms(_key(junk=[".obsidian/app.json"]), _arm(junk={".obsidian/app.json": 1}),
                                     _arm(junk={".obsidian/app.json": 0}))
    assert groups["junk_kept_out"]["loss"]["listed"] == [".obsidian/app.json"]
    assert raw_import.target_verdict(groups) == FAIL


def test_junk_that_cloud_refused_and_local_let_in_is_a_cloud_extra_and_passes():
    groups = raw_import.compare_arms(_key(junk=[".obsidian/app.json"]), _arm(junk={".obsidian/app.json": 0}),
                                     _arm(junk={".obsidian/app.json": 1}))
    assert groups["junk_kept_out"]["cloud_extra"]["count"] == 1
    assert raw_import.target_verdict(groups) == PASS


# --- links: one lost note is not many lost edges ---------------------------------------------


def test_a_link_is_dropped_only_when_both_notes_landed_and_is_endpoint_missing_otherwise():
    key = _key("a.md", "b.md", "c.md", links=[("a.md", "b.md"), ("a.md", "c.md")])
    both = {"a.md -> b.md": True, "a.md -> c.md": True}
    cloud = _arm(links={"a.md -> b.md": False, "a.md -> c.md": False}, **{"a.md": 1, "b.md": 1, "c.md": 0})
    groups = raw_import.compare_arms(key, cloud, _arm(links=both, **{"a.md": 1, "b.md": 1, "c.md": 1}))
    assert {cause: row["listed"] for cause, row in groups["links"]["loss_causes"].items()} == {
        "edge dropped, both landed": ["a.md -> b.md"], "endpoint missing": ["a.md -> c.md"]}


# --- gaps: a partial read never passes -------------------------------------------------------


def _scored(**extra):
    return {"measurement_gaps": {}, "per_item": _arm(**{"a.md": 1}), **extra}


def _gaps(metrics, stopped_at=None, jobs_idle=True):
    return raw_import.arm_gaps("local", metrics, _key("a.md"), stopped_at=stopped_at, jobs_idle=jobs_idle)


def test_a_complete_arm_has_no_gaps():
    assert _gaps(_scored()) == []


def test_a_failed_scorer_read_is_a_gap():
    assert _gaps(_scored(measurement_gaps={"read_memory": 3})) == ["local: read_memory read failed 3 times"]


def test_a_stopped_journey_is_a_gap():
    assert _gaps(_scored(), stopped_at="apply") == ["local: the journey stopped at apply"]


def test_running_media_jobs_are_a_gap_and_so_is_no_job_count():
    assert _gaps(_scored(), jobs_idle=False) == ["local: media jobs were still pending or running"]
    assert _gaps(_scored(), jobs_idle=None) == ["local: no media job counts were read"]


def test_blocked_jobs_and_undrained_index_upserts_leave_the_queue_idle():
    # A stdio server never drains deferred index upserts, and blocked jobs wait on an engine, not on time.
    status = {"counts": {"pending": 0, "running": 0, "blocked": 3, "failed": 0}, "index_refresh": {"count": 8}}
    assert raw_import.jobs_idle({"converged": False, "final_status": status}) is True


def test_a_pending_or_running_job_is_not_idle_and_a_status_without_counts_is_no_reading():
    assert raw_import.jobs_idle({"final_status": {"counts": {"pending": 1, "running": 0}}}) is False
    assert raw_import.jobs_idle({"final_status": {"counts": {"pending": 0, "running": 2}}}) is False
    assert raw_import.jobs_idle({"final_status": {"index_refresh": None}}) is None
    assert raw_import.jobs_idle({"final_status": None}) is None


def test_an_arm_that_landed_no_note_is_a_gap_so_two_empty_arms_do_not_pass():
    empty = {"measurement_gaps": {}, "per_item": _arm(**{"a.md": 0})}
    assert _gaps(empty) == ["local: no key note landed, so there is nothing to compare"]
    groups = raw_import.compare_arms(_key("a.md"), empty["per_item"], empty["per_item"])
    assert raw_import.target_verdict(groups, (), _gaps(empty)) == INCOMPLETE


def test_a_gap_keeps_a_target_from_passing_but_a_loss_still_fails_it():
    groups = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 1}), _arm(**{"a.md": 1}))
    assert raw_import.target_verdict(groups, (), ["local: the journey stopped at apply"]) == INCOMPLETE
    lost = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 0}), _arm(**{"a.md": 1}))
    assert raw_import.target_verdict(lost, (), ["cloud: the journey stopped at apply"]) == FAIL


# --- the vault that already held pages -------------------------------------------------------

IDENTITY = ("earlier", "notes/a.md", "sha-a")


def _before():
    return Snapshot(files=frozenset({"Knowledge Base/index.md", "Knowledge Base/Sources/a.md", "Knowledge Base/Sources/a.pdf"}),
                    sourced={"Knowledge Base/Sources/a.md": "h1"}, identities={"Knowledge Base/Sources/a.md": IDENTITY})


def test_managed_pages_may_change_and_new_pages_may_appear_beside_untouched_sources():
    after = Snapshot(files=_before().files | {"Knowledge Base/Sources/new.md"},
                     sourced={"Knowledge Base/Sources/a.md": "h1", "Knowledge Base/Sources/new.md": "h2"},
                     identities={"Knowledge Base/Sources/a.md": IDENTITY, "Knowledge Base/Sources/new.md": ("main", "x.md", "sha-x")})
    assert raw_import.check_existing_pages(_before(), after) == []


def test_a_changed_or_missing_source_page_is_a_problem():
    changed = Snapshot(files=_before().files, sourced={"Knowledge Base/Sources/a.md": "other"}, identities=_before().identities)
    assert raw_import.check_existing_pages(_before(), changed) == ["changed: Knowledge Base/Sources/a.md"]
    gone = Snapshot(files=frozenset({"Knowledge Base/index.md", "Knowledge Base/Sources/a.pdf"}))
    assert raw_import.check_existing_pages(_before(), gone) == ["missing: Knowledge Base/Sources/a.md"]


def test_a_missing_non_markdown_file_is_a_problem():
    after = Snapshot(files=_before().files - {"Knowledge Base/Sources/a.pdf"}, sourced=_before().sourced, identities=_before().identities)
    assert raw_import.check_existing_pages(_before(), after) == ["missing: Knowledge Base/Sources/a.pdf"]


def test_a_second_page_with_a_pre_existing_source_identity_is_a_duplicate():
    after = Snapshot(files=_before().files | {"Knowledge Base/Sources/a copy.md"}, sourced=_before().sourced,
                     identities={**_before().identities, "Knowledge Base/Sources/a copy.md": IDENTITY})
    assert raw_import.check_existing_pages(_before(), after) == [
        "duplicated: ['Knowledge Base/Sources/a copy.md', 'Knowledge Base/Sources/a.md']"]


def test_a_failed_page_check_fails_and_an_unrun_one_is_incomplete():
    groups = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 1}), _arm(**{"a.md": 1}))
    assert raw_import.target_verdict(groups, ["changed: Knowledge Base/a.md"]) == FAIL
    assert raw_import.target_verdict(groups, None) == INCOMPLETE


# --- where the product wrote the originals ---------------------------------------------------


def test_the_staged_folder_in_front_of_each_original_is_read_from_the_pages_even_for_decomposed_names():
    nfd_name = "résumé.md"
    values = [f"_Imports/abc/notes/{nfd_name}", "_Imports/abc/ideas/x.md", "unrelated/other.md"]
    assert raw_import.imported_from_prefix(values, ["notes/résumé.md", "ideas/x.md"]) == "_Imports/abc"
    assert raw_import.imported_from_prefix(["other.md"], ["ideas/x.md"]) is None
