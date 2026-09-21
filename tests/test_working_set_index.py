"""Task 1.1 — the derived activation index, built from governed structure only.

The index is a disposable sidecar: rebuildable from the vault alone, updated
incrementally on the recall freshness checkpoint, never authoritative, and never
read by ordinary recall.  These tests pin the anchor rows it derives, the
generation token that invalidates its caches, the schema-mismatch wipe, and the
kill switch's refusal to create the file at all.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from exomem import working_set_index


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_structure(vault: Path) -> None:
    """Entities (+aliases), a hub, Products/Systems pages, Planning, Records, keys."""
    kb = vault / "Knowledge Base"
    _write(
        kb / "Entities" / "People" / "Marit Solheim.md",
        """---
type: entity
entity_type: person
status: active
aliases: [Marit, M. Solheim]
tags: [logistics]
---

# Marit Solheim

## Summary

Freight coordinator for the northern corridor.
""",
    )
    _write(
        kb / "Notes" / "Insights" / "northern-corridor-hub.md",
        """---
type: insight
status: active
tags: [hub]
updated: 2026-09-01
---

# Northern corridor

## Summary

Everything about the northern freight corridor.

## Open questions

Which depot owns the winter schedule?
""",
    )
    _write(
        kb / "Products" / "Cargo Sled.md",
        """---
type: note
status: active
updated: 2026-09-02
---

# Cargo Sled

## Summary

A towed cargo sled rated for 400 kg.

## Constraints

Never exceed 400 kg.
""",
    )
    _write(
        kb / "Systems" / "Depot Ledger.md",
        """---
type: note
status: active
updated: 2026-09-03
---

# Depot Ledger

## Summary

The ledger that tracks depot stock.
""",
    )
    _write(
        kb / "_Schema" / "project-keys.yaml",
        """projects:
  northern-corridor:
    folder: Northern Corridor
    category: logistics
""",
    )
    _write(
        kb / "Records" / "Depot Stock" / "_collection.md",
        """---
type: collection
exomem_id: 6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c11
title: Depot stock
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
claims:
  terms: [depot, stock, sled]
item_schema:
  natural_key: [observed_on, asset]
  fields:
    observed_on:
      type: date
      required: true
    asset:
      type: string
      required: true
    state:
      type: string
---

Depot stock observations.
""",
    )
    (kb / "Records" / "Depot Stock" / "Items").mkdir(parents=True, exist_ok=True)


def _seed_planning(vault: Path) -> str:
    from test_planning_mutation import _manifest

    from exomem import planning

    manifest_path = "Knowledge Base/Planning/Corridor/_collection.md"
    planning.create_collection(
        vault,
        manifest_path,
        _manifest() + "\nCorridor planning items.\n",
        why="seed activation-index planning fixture",
    )
    planning.add(
        vault,
        manifest_path,
        plan_id=str(UUID(int=7)),
        item={"title": "Winter schedule for the northern corridor", "kind": "outcome"},
        why="seed activation-index planning item",
    )
    return manifest_path


@pytest.fixture
def seeded(vault: Path) -> Path:
    _seed_structure(vault)
    _seed_planning(vault)
    return vault


def _kinds(rows) -> set[str]:
    return {row.kind for row in rows}


def _by_title(rows, title: str):
    for row in rows:
        if row.title == title:
            return row
    raise AssertionError(f"no anchor titled {title!r} in {[r.title for r in rows]}")


def test_collect_caps_to_distinct_anchor_ids_not_list_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 4, MINOR: `_collect`'s cap must keep `MAX_ANCHORS`
    DISTINCT anchor ids, never merely the first `MAX_ANCHORS` LIST
    POSITIONS. Anchor ids are always distinct in practice (built from each
    page's own path), so a real duplicate id never occurs -- this test
    forces one to make the invariant explicit rather than assumed. Without a
    dedupe before the slice, a duplicate id consumes two "positions" while
    contributing only one distinct anchor, silently starving a genuinely
    different anchor that had room under the cap.
    """
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base" / "Products"
    kb.mkdir(parents=True)
    (kb / "r0.md").write_text(
        "---\ntitle: R Zero\nstatus: active\nupdated: 2026-09-01\n---\n\n# R Zero\n\nBody.\n",
        encoding="utf-8",
    )
    (kb / "r1.md").write_text(
        "---\ntitle: R One\nstatus: active\nupdated: 2026-09-01\n---\n\n# R One\n\nBody.\n",
        encoding="utf-8",
    )
    real_raw, outbound, names, project_members = working_set_index._walk_page_entries(vault)
    r0, r1 = real_raw
    # A copy of r0 whose id collides with r1's -- the forced duplicate.
    r0_dup = dict(r0)
    r0_dup["anchor_id"] = r1["anchor_id"]
    r2 = dict(r0)
    r2["anchor_id"] = "Knowledge Base/Products/r2.md"
    r2["path"] = r2["anchor_id"]
    r2["title"] = "R Two"

    monkeypatch.setattr(
        working_set_index,
        "_walk_page_entries",
        lambda vault_root: ([r0_dup, r1, r2], outbound, names, project_members),
    )
    monkeypatch.setattr(working_set_index, "_collection_candidates", lambda vault_root: ([], []))
    monkeypatch.setattr(
        working_set_index, "_project_candidates", lambda vault_root, member_paths: ([], {})
    )
    monkeypatch.setattr(working_set_index, "MAX_ANCHORS", 2)

    index = working_set_index.WorkingSetIndex(vault)
    candidates, _edges, _names, _term_counts = index._collect()

    # Two DISTINCT ids kept under a cap of 2, not one id counted twice.
    assert {c.anchor_id for c in candidates} == {r1["anchor_id"], r2["anchor_id"]}


def test_build_derives_anchor_rows_from_governed_structure(seeded: Path) -> None:
    index = working_set_index.WorkingSetIndex(seeded)
    report = index.rebuild()

    assert report["anchors"] > 0
    rows = index.anchors()
    assert _kinds(rows) >= {"entity", "hub", "resource", "plan", "collection", "project"}

    person = _by_title(rows, "Marit Solheim")
    assert person.kind == "entity"
    assert "marit" in person.aliases
    assert "m. solheim" in person.aliases
    assert person.lifecycle == "active"
    # Structural signature only: title, lede and headline sections. No prose the
    # server generated, because the server generates no prose.
    assert "Marit Solheim" in person.signature
    assert "Freight coordinator" in person.signature

    sled = _by_title(rows, "Cargo Sled")
    assert sled.kind == "resource"
    assert "Constraints" in sled.signature
    assert "constraint" in sled.categories

    hub = _by_title(rows, "Northern corridor")
    assert hub.kind == "hub"

    collection = _by_title(rows, "Depot stock")
    assert collection.kind == "collection"
    # Records manifests contribute their claims and their item field names.
    assert "depot" in collection.terms
    assert "observed_on" in collection.signature

    plan = _by_title(rows, "Winter schedule for the northern corridor")
    assert plan.kind == "plan"

    project = _by_title(rows, "Northern Corridor")
    assert project.kind == "project"


def test_typed_links_are_recorded_between_anchors(seeded: Path) -> None:
    _write(
        seeded / "Knowledge Base" / "Notes" / "Insights" / "sled-load-limit.md",
        """---
type: insight
status: active
updated: 2026-09-04
---

# Sled load limit

Related: [[Cargo Sled]] and [[Marit Solheim]].
""",
    )
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    sled = _by_title(index.anchors(), "Cargo Sled")
    neighbours = {target for target, _relation, _direction in sled.links}
    assert any("Marit Solheim" in target or "sled-load-limit" in target for target in neighbours)


def test_rebuild_equals_incremental_after_writes(seeded: Path) -> None:
    incremental = working_set_index.WorkingSetIndex(seeded)
    incremental.rebuild()

    _write(
        seeded / "Knowledge Base" / "Products" / "Winter Tarp.md",
        """---
type: note
status: active
updated: 2026-09-05
---

# Winter Tarp

## Summary

A tarp rated to minus thirty.
""",
    )
    (seeded / "Knowledge Base" / "Systems" / "Depot Ledger.md").unlink()
    incremental.update()

    def _comparable(rows):
        return sorted(
            (r.path, r.kind, r.title, r.lifecycle, r.aliases, r.categories, r.signature)
            for r in rows
        )

    incremental_rows = _comparable(incremental.anchors())
    incremental.close()

    rebuilt = working_set_index.WorkingSetIndex(seeded)
    rebuilt.reset()
    rebuilt.rebuild()

    assert incremental_rows == _comparable(rebuilt.anchors())


def test_generation_bumps_on_upsert_and_delete(seeded: Path) -> None:
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    after_build = index.generation()
    assert after_build >= 1

    _write(
        seeded / "Knowledge Base" / "Products" / "Spare Runner.md",
        """---
type: note
status: active
updated: 2026-09-06
---

# Spare Runner

## Summary

A spare runner for the sled.
""",
    )
    index.update()
    after_upsert = index.generation()
    assert after_upsert > after_build

    (seeded / "Knowledge Base" / "Products" / "Spare Runner.md").unlink()
    index.update()
    assert index.generation() > after_upsert

    # An update that changes nothing does not move the token.
    unchanged = index.generation()
    index.update()
    assert index.generation() == unchanged


# --------------------------------------------------------------------------- #
# Project anchors carry their member pages as links (`close-memory-loop`)
# --------------------------------------------------------------------------- #


def _seed_project_membership(vault: Path) -> None:
    """A project key with one declared member page. Deliberately its OWN key
    ("harbor-survey"), distinct from `_seed_structure`'s "northern-corridor"
    (which names no page): that fixture's project shares its title with a
    HUB ("Northern corridor"), and mixing the two here would make a test
    unable to tell which anchor kind actually supplied a result.
    """
    _write(
        vault / "Knowledge Base" / "_Schema" / "project-keys.yaml",
        """projects:
  harbor-survey:
    folder: Harbor Survey
    category: logistics
""",
    )
    _write(
        vault
        / "Knowledge Base"
        / "Notes"
        / "Research"
        / "Harbor Survey"
        / "public-depth-notice.md",
        """---
type: research-note
project: harbor-survey
status: active
updated: 2026-09-10
---

# Public depth notice

## Constraints

Draft may not exceed 4 metres at low tide.
""",
    )


def test_a_resolved_project_anchor_carries_its_member_pages_as_links(
    tmp_path: Path,
) -> None:
    """`close-memory-loop` root cause 1: a project anchor built with no path
    and no links carries no material for any lane to read. At index time
    (never on the request path) the anchor's own member pages -- their own
    declared `project:`/`projects:` frontmatter, read the same way
    `find_corpus.passes_filters` already reads it for search filtering --
    become its `links`, so `AnchorFacts.neighbourhood` is non-empty and
    `_units_lane`'s `allowed_parent_paths` has something to read.
    """
    vault = tmp_path / "vault"
    _seed_project_membership(vault)

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    project = _by_title(index.anchors(), "Harbor Survey")
    assert project.kind == "project"
    member_path = "Knowledge Base/Notes/Research/Harbor Survey/public-depth-notice.md"
    assert member_path in project.neighbourhood


def test_a_raw_material_page_declaring_project_scope_is_not_a_member(
    tmp_path: Path,
) -> None:
    """`Sources/`/`Evidence/` are immutable raw material, excluded from the
    anchor set for the same reason (see `_page_anchor_kind`): admitting them
    as project material would let evidence ABOUT a project name itself as
    the project's own current material."""
    vault = tmp_path / "vault"
    _seed_project_membership(vault)
    _write(
        vault / "Knowledge Base" / "Sources" / "harbor-survey-clipping.md",
        """---
type: source
project: harbor-survey
status: active
updated: 2026-09-11
---

# Harbor survey clipping

## Summary

A captured article, not the project's own material.
""",
    )

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    project = _by_title(index.anchors(), "Harbor Survey")
    # A positive assertion alongside the negative one: the exclusion must be
    # deliberate (raw material specifically), not incidental to a neighbourhood
    # that happens to be empty altogether.
    assert (
        "Knowledge Base/Notes/Research/Harbor Survey/public-depth-notice.md"
        in project.neighbourhood
    )
    assert "Knowledge Base/Sources/harbor-survey-clipping.md" not in project.neighbourhood


def test_project_membership_is_capped_and_keeps_the_most_recently_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The member set is bounded by a named constant, most recently updated
    first, with a deterministic (path-ascending) tie-break."""
    vault = tmp_path / "vault"
    _write(
        vault / "Knowledge Base" / "_Schema" / "project-keys.yaml",
        """projects:
  harbor-survey:
    folder: Harbor Survey
    category: logistics
""",
    )
    monkeypatch.setattr(working_set_index, "PROJECT_ANCHOR_MEMBER_CAP", 2)
    for number, updated in ((0, "2026-09-01"), (1, "2026-09-05"), (2, "2026-09-03")):
        _write(
            vault
            / "Knowledge Base"
            / "Notes"
            / "Research"
            / "Harbor Survey"
            / f"reading-{number}.md",
            f"""---
type: research-note
project: harbor-survey
status: active
updated: {updated}
---

# Reading {number}

## Constraints

Body.
""",
        )

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    project = _by_title(index.anchors(), "Harbor Survey")
    assert project.neighbourhood == {
        "Knowledge Base/Notes/Research/Harbor Survey/reading-1.md",
        "Knowledge Base/Notes/Research/Harbor Survey/reading-2.md",
    }


def test_membership_change_republishes_the_project_anchor(tmp_path: Path) -> None:
    """The KEPT member set (post-cap) is part of what identifies this
    generation of the anchor: adding a member page must move the generation
    on an incremental `update()`, exactly as any other anchor's own change
    already does.
    """
    vault = tmp_path / "vault"
    _seed_project_membership(vault)

    index = working_set_index.WorkingSetIndex(vault)
    report = index.rebuild()
    after_build = report["generation"]

    _write(
        vault / "Knowledge Base" / "Notes" / "Research" / "Harbor Survey" / "second-reading.md",
        """---
type: research-note
project: harbor-survey
status: active
updated: 2026-09-12
---

# Second reading

## Constraints

Draft may not exceed 3 metres at neap tide.
""",
    )
    report = index.update()

    assert report.get("unchanged") is not True
    assert report["generation"] > after_build
    project = _by_title(index.anchors(), "Harbor Survey")
    assert (
        "Knowledge Base/Notes/Research/Harbor Survey/second-reading.md"
        in project.neighbourhood
    )


def test_schema_mismatch_wipes_and_rebuilds(seeded: Path) -> None:
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    expected = len(index.anchors())
    index.close()

    import sqlite3

    conn = sqlite3.connect(working_set_index.sidecar_path(seeded))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (working_set_index.SCHEMA_VERSION + 99,),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = working_set_index.WorkingSetIndex(seeded)
    assert reopened.anchors() == ()
    reopened.rebuild()
    assert len(reopened.anchors()) == expected


def test_kill_switch_builds_nothing(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    assert working_set_index.disabled() is True

    index = working_set_index.WorkingSetIndex(seeded)
    assert index.available() is False
    assert index.rebuild() == {"anchors": 0, "generation": 0, "disabled": True}
    assert index.anchors() == ()
    assert not working_set_index.sidecar_path(seeded).exists()


def test_signature_embeddings_are_absent_without_an_embedder(seeded: Path) -> None:
    # `EXOMEM_DISABLE_EMBEDDINGS=1` is the suite default: `vector_band` must be
    # ABSENT, never faked, so the vectors table stays empty rather than holding
    # zero vectors that would score.
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    assert index.vectors() == {}


# --------------------------------------------------------------------------- #
# Task 1.3 — recall isolation (identity proof (a))
# --------------------------------------------------------------------------- #


def _recall_bytes(vault: Path) -> str:
    import json

    from exomem import commands, find

    find.clear_cache()
    result = commands.op_ask_memory(
        vault,
        query="depot sled winter corridor",
        limit=8,
        rerank=False,
        mode="hybrid",
    )
    hits = result["hits"] if isinstance(result, dict) else result
    from exomem.governance import egress

    return json.dumps(egress.project_hits(hits), ensure_ascii=False, sort_keys=True)


def test_ask_memory_is_byte_identical_with_and_without_the_index(seeded: Path) -> None:
    """The compiler is additive: recall must not notice that the index exists."""
    before = _recall_bytes(seeded)
    assert not working_set_index.sidecar_path(seeded).exists()

    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    assert working_set_index.sidecar_path(seeded).exists()
    assert index.anchors()

    assert _recall_bytes(seeded) == before


def test_ask_memory_is_byte_identical_with_and_without_the_kill_switch(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working_set_index.WorkingSetIndex(seeded).rebuild()
    enabled = _recall_bytes(seeded)

    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    assert _recall_bytes(seeded) == enabled


def test_planning_and_records_items_never_become_recall_candidates(seeded: Path) -> None:
    """An anchor is not a hit: structured items stay out of ordinary recall."""
    from exomem import recall_policy

    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    plan_rows = [row for row in index.anchors() if row.kind == "plan"]
    assert plan_rows

    for row in plan_rows:
        if not row.path or row.path.endswith("_collection.md"):
            continue
        assert not recall_policy.is_recall_candidate(seeded, seeded / row.path)
        assert recall_policy.is_structured_only_path(seeded, row.path)


# --------------------------------------------------------------------------- #
# Freshness: a stale index is refreshed or reported, never silently served
# --------------------------------------------------------------------------- #


def test_the_index_stamps_the_freshness_key_it_was_built_at(seeded: Path) -> None:
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild(freshness_stamp="stamp-1")

    assert index.freshness_stamp() == "stamp-1"


def test_a_governed_write_invalidates_the_packet_and_moves_the_generation(
    seeded: Path,
) -> None:
    from exomem import working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    turn = "what do I know about M. Solheim"

    first = working_set_runtime.serve(
        seeded, turn=turn, max_chars=2000, freshness_key="k1"
    )
    first_generation = first["generation"]["index_generation"]
    assert first_generation >= 1

    # A governed write adds an alias the next turn uses.
    page = seeded / "Knowledge Base" / "Entities" / "People" / "Marit Solheim.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "aliases: [Marit, M. Solheim]", "aliases: [Marit, M. Solheim, Solheim]"
        ),
        encoding="utf-8",
    )

    second = working_set_runtime.serve(
        seeded, turn="what do I know about Solheim", max_chars=2000, freshness_key="k2"
    )

    assert second["generation"]["index_generation"] > first_generation
    index = working_set_index.WorkingSetIndex(seeded)
    solheim = _by_title(index.anchors(), "Marit Solheim")
    assert "solheim" in solheim.aliases


def test_a_managed_runtime_abstains_with_index_warming_and_warms_once(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, **_kwargs: scheduled.append(root)
    )

    packet = working_set_runtime.serve(seeded, turn="the cargo sled", max_chars=2000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "index_warming"}
    assert packet["units"] == []
    assert len(scheduled) == 1

    # A managed runtime never builds inline: the sidecar may exist (it is opened
    # to read the generation) but it holds no anchor rows yet.
    assert working_set_index.WorkingSetIndex(seeded).anchors() == ()


def test_cold_activation_abstains_before_starting_retrieval(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, readiness, working_set_runtime

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, **_kwargs: scheduled.append(root)
    )
    retrieval_calls: list[dict] = []

    def costly_retrieval(*args, **kwargs):
        retrieval_calls.append(kwargs)
        return []

    monkeypatch.setattr(commands.find_module, "find", costly_retrieval)
    packet = commands.op_activate_context(
        seeded, turn="the cargo sled", continuity="old-token", include_timings=True
    )

    assert packet["abstention"] == {"reason": "index_warming"}
    assert retrieval_calls == [], "a warming response must not load hybrid resources"
    assert scheduled == [seeded]
    assert packet["units"] == []
    assert packet["generation"]["continuity"] == "stale"
    assert "working_set.readiness" in packet["timings"]["stages"]


def test_activation_readiness_failure_does_not_start_retrieval(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, working_set_runtime

    def unreadable_index(*args, **kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(working_set_runtime, "ensure_index", unreadable_index)
    retrieval_calls: list[dict] = []
    monkeypatch.setattr(
        commands.find_module, "find", lambda *args, **kwargs: retrieval_calls.append(kwargs) or []
    )
    packet = commands.op_activate_context(seeded, turn="the cargo sled")

    assert packet["abstention"] == {"reason": "unavailable"}
    assert retrieval_calls == []
    assert packet["units"] == []


@pytest.mark.parametrize("broken_kind", ["directory", "corrupt"])
def test_broken_activation_sidecar_is_unavailable_not_warming(
    seeded: Path, monkeypatch: pytest.MonkeyPatch, broken_kind: str
) -> None:
    from exomem import commands, readiness, working_set_runtime

    path = working_set_index.sidecar_path(seeded)
    path.parent.mkdir(parents=True, exist_ok=True)
    if broken_kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, **_kwargs: scheduled.append(root)
    )
    retrieval_calls: list[dict] = []
    monkeypatch.setattr(
        commands.find_module, "find", lambda *args, **kwargs: retrieval_calls.append(kwargs) or []
    )

    packet = commands.op_activate_context(seeded, turn="the cargo sled")

    assert packet["abstention"] == {"reason": "unavailable"}
    assert scheduled == []
    assert retrieval_calls == []


def test_a_managed_runtime_reports_a_stale_index_rather_than_walking(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(seeded).rebuild(freshness_stamp="old")

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        working_set_runtime,
        "_schedule_build",
        lambda root, *, freshness_stamp="": scheduled.append((root, freshness_stamp)),
    )

    packet = working_set_runtime.serve(
        seeded, turn="the cargo sled", max_chars=2000, freshness_key="new"
    )

    assert packet["generation"]["index_stale"] is True
    assert packet["generation"]["index_generation"] >= 1
    # The build is scheduled FOR the key the request was stale against.
    assert [stamp for _root, stamp in scheduled] == ["new"]


def _join_scheduled_builds() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "exomem-working-set-warm":
            thread.join(timeout=30)
            assert not thread.is_alive(), "the scheduled index build did not finish"


def test_a_scheduled_build_records_the_stamp_so_the_next_request_stops_asking(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed runtime hands the stale walk to a background build.

    That build has to record the freshness key it was scheduled for. Without it
    the catalogue reads as stale forever: every request reports `index_stale`
    and, with no build in flight, schedules another whole-vault walk.
    """
    from exomem import readiness, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild(freshness_stamp="old")
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)

    first = working_set_runtime.serve(
        seeded, turn="the cargo sled", max_chars=2000, freshness_key="new"
    )
    assert first["generation"]["index_stale"] is True
    _join_scheduled_builds()
    assert working_set_index.WorkingSetIndex(seeded).freshness_stamp() == "new"

    real_schedule = working_set_runtime._schedule_build
    scheduled: list[Path] = []

    def recording(root: Path, **kwargs: object) -> None:
        scheduled.append(root)
        real_schedule(root, **kwargs)

    monkeypatch.setattr(working_set_runtime, "_schedule_build", recording)
    second = working_set_runtime.serve(
        seeded, turn="the cargo sled", max_chars=2000, freshness_key="new"
    )
    _join_scheduled_builds()

    # The marker is only ever present as True; its absence is "current".
    assert "index_stale" not in second["generation"]
    assert scheduled == []


def test_a_cold_scheduled_build_records_the_stamp_too(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold-start build is the same gate: it records the key it ran for.

    Otherwise the first request after a managed cold start finds a fresh
    catalogue with no key, reads it as stale, and pays for a second whole-vault
    walk that finds nothing to do.
    """
    from exomem import readiness, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    assert not working_set_index.WorkingSetIndex(seeded).anchors()

    state, _index, _stale = working_set_runtime.ensure_index(seeded, freshness_stamp="cold-key")
    assert state == working_set_runtime.WARMING
    _join_scheduled_builds()
    assert working_set_index.WorkingSetIndex(seeded).freshness_stamp() == "cold-key"

    real_schedule = working_set_runtime._schedule_build
    scheduled: list[Path] = []

    def recording(root: Path, **kwargs: object) -> None:
        scheduled.append(root)
        real_schedule(root, **kwargs)

    monkeypatch.setattr(working_set_runtime, "_schedule_build", recording)
    state, _index, stale = working_set_runtime.ensure_index(seeded, freshness_stamp="cold-key")

    assert (state, stale) == (working_set_runtime.READY, False)
    assert scheduled == []


# --------------------------------------------------------------------------- #
# Review round: hub scope and single-flight cold build
# --------------------------------------------------------------------------- #


def test_hub_tagged_raw_material_is_not_an_anchor(seeded: Path) -> None:
    """`Sources/` and `Evidence/` are immutable raw material, not anchors.

    A captured article that happens to carry `tags: [hub]` is evidence about the
    world, not a durable anchor of the user's own structure, and admitting it
    would let raw material name itself as the subject of a turn.
    """
    _write(
        seeded / "Knowledge Base" / "Evidence" / "receipt-hub.md",
        """---
type: evidence
status: active
tags: [hub]
---

# Receipt hub

A preserved receipt that happens to be tagged as a hub.
""",
    )
    _write(
        seeded / "Knowledge Base" / "Sources" / "Articles" / "2026-09-01-corridor.md",
        """---
type: source
status: active
tags: [hub]
---

# Corridor article

A captured article that happens to be tagged as a hub.
""",
    )

    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    paths = {row.path for row in index.anchors()}
    assert not any("/Evidence/" in path for path in paths)
    assert not any("/Sources/" in path for path in paths)
    # The genuine hub outside raw material is still indexed.
    assert _by_title(index.anchors(), "Northern corridor").kind == "hub"


def test_a_cold_unmanaged_build_is_single_flight(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent cold reads must not both walk the vault."""
    import threading

    from exomem import working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(seeded).reset()

    rebuilds: list[int] = []
    real_rebuild = working_set_index.WorkingSetIndex.rebuild
    started = threading.Barrier(2, timeout=30)

    def counting_rebuild(self, **kwargs):
        rebuilds.append(1)
        return real_rebuild(self, **kwargs)

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "rebuild", counting_rebuild)

    results: list[dict] = []
    errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            started.wait()
            results.append(
                working_set_runtime.serve(seeded, turn="the cargo sled", max_chars=2000)
            )
        except BaseException as error:  # noqa: BLE001 - reported, not swallowed
            errors.append(error)

    threads = [threading.Thread(target=serve_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(results) == 2
    assert len(rebuilds) == 1, f"the cold build ran {len(rebuilds)} times"
    for packet in results:
        reason = (packet.get("abstention") or {}).get("reason")
        assert reason != "unavailable", packet.get("abstention")


def test_aliases_are_indexed_as_page_names(seeded: Path) -> None:
    """A frontmatter alias is a spelling the vault resolves, so the map holds it.

    Two consumers read this map: `_resolve_links` (typed edges) and the egress
    guard (prose references). Indexing the alias serves both — an alias-spelled
    wikilink now becomes an edge as well as a decidable reference.
    """
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    entity = "Knowledge Base/Entities/People/Marit Solheim.md"
    for spelling in ("Marit", "M. Solheim", "Marit Solheim", "marit solheim"):
        resolved = index.resolve_names([spelling])
        assert resolved.get(working_set_index.normalize(spelling)) == (entity,), spelling


def test_an_alias_spelled_wikilink_becomes_a_typed_edge(seeded: Path) -> None:
    _write(
        seeded / "Knowledge Base" / "Notes" / "Insights" / "alias-linker.md",
        """---
type: insight
status: active
updated: 2026-09-08
---

# Alias linker

Spoke to [[Marit]] about the corridor.
""",
    )
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    person = _by_title(index.anchors(), "Marit Solheim")
    assert "Knowledge Base/Notes/Insights/alias-linker.md" in person.neighbourhood


def _hub(vault: Path, name: str, title: str, body: str) -> str:
    rel = f"Knowledge Base/Notes/Insights/{name}.md"
    _write(
        vault / rel,
        f"""---
type: insight
status: active
tags: [hub]
updated: 2026-09-10
---

# {title}

## Summary

{body}
""",
    )
    return rel


def _two_hubs_over_one_page(vault: Path, *, alias: bool) -> tuple[str, str, str]:
    """Two competing hubs that both link one page, reachable by alias or not.

    The bridge page is an ordinary note, not an anchor. With `aliases: [Ops]` the
    `[[Ops]]` wikilinks resolve to it and both hubs gain it as a typed neighbour;
    without the alias they resolve to nothing and the neighbourhoods stay bare.
    """
    left = _hub(vault, "search-feature", "Search feature", "Implementation hub. See [[Ops]].")
    right = _hub(vault, "search-market", "Search market", "Market-research hub. See [[Ops]].")
    bridge = "Knowledge Base/Notes/operations-handbook.md"
    _write(
        vault / bridge,
        """---
type: insight
status: active
updated: 2026-09-10
"""
        + ("aliases: [Ops]\n" if alias else "")
        + """---

# Operations handbook

How we run things, linked from everywhere.
""",
    )
    return left, right, bridge


def test_a_bridge_page_that_is_not_an_anchor_is_outside_the_anchor_neighbourhood(
    seeded: Path,
) -> None:
    _left, _right, bridge = _two_hubs_over_one_page(seeded, alias=True)
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()
    rows = index.anchors()

    assert bridge not in {row.path for row in rows}, "the bridge page must not be an anchor"
    left_row = _by_title(rows, "Search feature")
    right_row = _by_title(rows, "Search market")
    # The alias made it a typed neighbour of both hubs ...
    assert bridge in left_row.neighbourhood
    assert bridge in right_row.neighbourhood
    # ... but it is not an anchor, so it joins neither anchor neighbourhood.
    assert bridge not in left_row.anchor_neighbourhood
    assert bridge not in right_row.anchor_neighbourhood
    assert not (left_row.anchor_neighbourhood & right_row.anchor_neighbourhood)


def test_a_linked_anchor_is_in_the_anchor_neighbourhood(seeded: Path) -> None:
    _hub(seeded, "corridor-ops", "Corridor ops", "Run by [[Marit Solheim]].")
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    hub = _by_title(index.anchors(), "Corridor ops")
    person = "Knowledge Base/Entities/People/Marit Solheim.md"
    assert person in hub.neighbourhood
    assert person in hub.anchor_neighbourhood


@pytest.mark.parametrize("alias", [False, True])
def test_an_alias_bridge_does_not_suppress_the_ambiguous_verdict(
    seeded: Path, alias: bool
) -> None:
    """The whole point of the anchor-neighbourhood rule, end to end over sqlite.

    Both hubs are named by one turn. Without the alias their neighbourhoods are
    disjoint and the turn abstains; with it they share a page that is not an
    anchor, and the turn must still abstain rather than serve a packet carrying
    two competing senses with no ambiguity block.
    """
    from exomem import working_set_resolve

    _two_hubs_over_one_page(seeded, alias=alias)
    index = working_set_index.WorkingSetIndex(seeded)
    index.rebuild()

    analysis = working_set_resolve.analyze_turn("search feature and search market")
    candidates = working_set_resolve.candidates_for(
        analysis, working_set_resolve.facts_from_rows(index.anchors())
    )
    resolution = working_set_resolve.resolve(
        working_set_resolve.add_graph_corroboration(candidates)
    )

    assert resolution.status == "ambiguous", resolution.as_dict()
    assert {item["title"] for item in resolution.ambiguity} == {
        "Search feature",
        "Search market",
    }


def _resolution_for(vault: Path, turn: str):
    """Resolve one turn end to end over a freshly built index."""
    from exomem import working_set_resolve

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    candidates = working_set_resolve.candidates_for(
        working_set_resolve.analyze_turn(turn),
        working_set_resolve.facts_from_rows(index.anchors()),
    )
    return working_set_resolve.resolve(working_set_resolve.add_graph_corroboration(candidates))


def test_fold_plural_folds_simple_and_ies_plurals() -> None:
    assert working_set_index.fold_plural("posts") == working_set_index.fold_plural("post")
    assert working_set_index.fold_plural("batteries") == working_set_index.fold_plural("battery")
    # A short word or a genuine double-s ending is left alone.
    assert working_set_index.fold_plural("ss") == "ss"
    assert working_set_index.fold_plural("glass") == "glass"


# MAJOR 5 (review round 3): `fold_plural` was broken for a majority of
# ordinary plurals -- "notes"/"note" did not fold, nor did
# releases/files/names/pages/changes/sources/services, "alias"/"aliases"
# folded to DIFFERENT strings, and "bus"/"gas" were corrupted to "bu"/"ga" by
# a blind single-`s` strip. Table-driven so the whole required set is one
# assertion loop, not one test per pair.
def test_fold_plural_folds_the_required_pairs_to_the_same_form() -> None:
    for singular, plural in (
        ("note", "notes"),
        ("file", "files"),
        ("name", "names"),
        ("page", "pages"),
        ("change", "changes"),
        ("source", "sources"),
        ("service", "services"),
        ("alias", "aliases"),
        ("class", "classes"),
        ("box", "boxes"),
        ("process", "processes"),
        ("bus", "buses"),
        ("gas", "gases"),
    ):
        assert working_set_index.fold_plural(singular) == working_set_index.fold_plural(
            plural
        ), f"{singular!r} vs {plural!r}"


def test_fold_plural_never_corrupts_these_words() -> None:
    for word in ("status", "analysis", "class", "gas", "bus", "its", "has", "glass", "ss"):
        assert working_set_index.fold_plural(word) == word, word


def test_fold_plural_documents_the_release_releases_residual_collision() -> None:
    """A deliberate, documented tradeoff (review round 3, MAJOR 5): no
    suffix-only rule can fold "release"/"releases" together while ALSO
    folding "alias"/"aliases" and "class"/"classes" correctly, because
    "alias" + "es" and "release" + "s" both end in the literal letters
    "-ses". This function chooses the sibilant-stem reading (the pairs the
    ruling names explicitly), so "release" and "releases" do NOT converge --
    exactly like the ruling's own named collisions (news/new, means/mean,
    lens/len), just not yet named there. Asserted here rather than left to
    surprise a future reader.
    """
    assert working_set_index.fold_plural("release") != working_set_index.fold_plural(
        "releases"
    )
    # The three named residual collisions from the canonical spec/ruling.
    assert working_set_index.fold_plural("news") == working_set_index.fold_plural("new")
    assert working_set_index.fold_plural("means") == working_set_index.fold_plural("mean")
    assert working_set_index.fold_plural("lens") == working_set_index.fold_plural("len")


def test_fold_plural_documents_both_residual_collision_classes() -> None:
    """Review round 4, MINOR: both residual classes named with examples, not
    just "release"/"releases". Class 1 -- a singular ending in a silent
    `-se` (no trailing `s` alone, so neither rule ever touches it) whose
    plural's word-final `-ses` is folded as an `s`-final singular's `-es`
    plural instead, colliding with the singular rather than matching it.
    Class 2 -- a Greek-derived singular ending in `-is` (excluded from the
    trailing-`s` strip on purpose, so it stays whole) whose irregular plural
    ends in `-es` and gets stripped as if it were that same `s`-final
    singular's `-es` plural, again colliding rather than matching.
    """
    for singular in (
        "release",
        "case",
        "base",
        "use",
        "phase",
        "response",
        "database",
        "license",
        "increase",
        "purchase",
        "house",
    ):
        assert working_set_index.fold_plural(singular) != working_set_index.fold_plural(
            singular + "s"
        ), singular

    for singular, plural in (
        ("analysis", "analyses"),
        ("basis", "bases"),
        ("crisis", "crises"),
        ("thesis", "theses"),
    ):
        assert working_set_index.fold_plural(singular) != working_set_index.fold_plural(
            plural
        ), (singular, plural)


def test_derived_short_name_reads_a_trailing_parenthetical_or_dash() -> None:
    # The resolver's own tokeniser normalises and casefolds (review round 3,
    # BLOCKER 3): the derived name is the lead's TOKENS joined by single
    # spaces, never the raw substring, so it is always what a turn's own
    # tokenised phrase could actually match.
    assert working_set_index.derived_short_name("Bike (Trek 520, 2019)") == "bike"
    assert working_set_index.derived_short_name("Bike - Trek 520") == "bike"
    assert working_set_index.derived_short_name("Bike — Trek 520") == "bike"
    assert working_set_index.derived_short_name("Northern corridor") is None


def test_derived_short_name_rejects_invalid_leads() -> None:
    """Review round 3, BLOCKER 3: none of these leads is a name."""
    # Filename-like.
    assert working_set_index.derived_short_name("config.yaml - settings") is None
    assert working_set_index.derived_short_name("_internal (notes)") is None
    # Only stopwords.
    assert working_set_index.derived_short_name("How — my method") is None
    # Only digits.
    assert working_set_index.derived_short_name("2026 — plan") is None
    # Fewer than three characters once joined (a single letter).
    assert working_set_index.derived_short_name("X — the platform") is None
    assert working_set_index.derived_short_name("A (b)") is None
    # More than three words.
    assert working_set_index.derived_short_name("One two three four (qualifier)") is None


def test_derived_short_name_rejects_a_lead_with_any_under_length_token() -> None:
    """Review round 4, MINOR: each TOKEN needs at least two letters, not just
    the joined name overall. "A (b) — c" joined to "a b" (3 characters, past
    the whole-name floor) even though neither "a" nor "b" is a name
    fragment; "2026 Q3 — tail" joined to "2026 q3" even though "q3" is barely
    a letter with a digit stapled on. Neither is a name a turn could ever
    say.

    A third example used to live here: "élève (note)" joined to "l ve" once
    the accented letters -- outside the pre-task-4a tokeniser's `[a-z0-9]`
    alphabet -- fragmented the word into unmatchable single letters. Task
    4a's Unicode-aware `tokens_of` now reads "élève" as one five-letter
    term, so it no longer belongs to this under-length-token case at all —
    see `test_a_derived_short_name_from_an_accented_title_is_the_accented_lead`
    in `test_working_set_unicode_terms.py` for its new expected result.
    """
    assert working_set_index.derived_short_name("A (b) — c") is None
    assert working_set_index.derived_short_name("2026 Q3 — tail") is None


def test_derived_short_name_collapses_multiple_spaces_and_drops_unmatchable_glyphs() -> None:
    """MINOR 10: a multi-space or emoji-led lead derives a name a turn's own
    tokeniser could actually produce, never one that can only occupy a name
    slot without ever matching.
    """
    assert working_set_index.derived_short_name("Multi   Spaces - qualifier") == "multi spaces"
    assert working_set_index.derived_short_name("\U0001f3af Goal - notes") == "goal"


# --------------------------------------------------------------------------- #
# Task 3 — derived short names and the title/alias term->anchor-count table
# --------------------------------------------------------------------------- #


def _resource(vault: Path, rel: str, title: str, body: str = "A resource page.") -> str:
    _write(
        vault / rel,
        f"---\ntype: note\nstatus: active\nupdated: 2026-09-11\n---\n\n# {title}\n\n{body}\n",
    )
    return rel


def test_a_unique_derived_short_name_is_admitted_as_an_alias(vault: Path) -> None:
    _resource(vault, "Knowledge Base/Products/Bike.md", "Bike (Trek 520, 2019)")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    bike = _by_title(index.anchors(), "Bike (Trek 520, 2019)")
    assert "bike" in bike.aliases


def test_a_second_anchor_with_the_same_leading_name_retires_the_alias(vault: Path) -> None:
    _resource(vault, "Knowledge Base/Products/Bike.md", "Bike (Trek 520, 2019)")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert "bike" in _by_title(index.anchors(), "Bike (Trek 520, 2019)").aliases

    _resource(vault, "Knowledge Base/Products/Bike2.md", "Bike (Cannondale, 2021)")
    index.update()

    rows = index.anchors()
    assert "bike" not in _by_title(rows, "Bike (Trek 520, 2019)").aliases
    assert "bike" not in _by_title(rows, "Bike (Cannondale, 2021)").aliases


def test_a_derived_short_name_already_owned_by_another_anchor_is_not_admitted(
    vault: Path,
) -> None:
    _resource(vault, "Knowledge Base/Products/Bike.md", "Bike")
    _resource(vault, "Knowledge Base/Products/Trek.md", "Bike (Trek 520, 2019)")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    trek = _by_title(index.anchors(), "Bike (Trek 520, 2019)")
    assert "bike" not in trek.aliases


def test_a_dash_title_whose_lead_names_a_common_topic_is_not_admitted(tmp_path: Path) -> None:
    """The "Orchard" shape (real-vault correction): a topic prefix shared by
    many pages derives a name no OTHER anchor's names literally include, yet
    the word identifies far more than one anchor. Four anchors share "orchard"
    (over `RARE_TERM_MAX_ANCHORS` = 3), so the dash-titled page's derived
    name is withheld even though (1)'s uniqueness check alone would admit it.

    Uses a bare `tmp_path`, not the `vault` fixture: `vault` copies the real,
    non-empty fixture tree, and this test's exact term-count assertions need
    a vault with nothing in it but what this test writes.
    """
    vault = tmp_path / "vault"
    _resource(vault, "Knowledge Base/Products/orchard-strategy.md", "Orchard Strategy")
    _resource(vault, "Knowledge Base/Products/orchard-roadmap.md", "Orchard Roadmap")
    _resource(vault, "Knowledge Base/Products/orchard-architecture.md", "Orchard platform architecture")
    _resource(vault, "Knowledge Base/Products/orchard-search.md", "Orchard — Agentic Search")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    rows = index.anchors()
    search = _by_title(rows, "Orchard — Agentic Search")
    assert "orchard" not in search.aliases
    orchard_term = working_set_index.fold_plural("orchard")
    assert index.term_anchor_counts().get(orchard_term) == 4

    from exomem import working_set_resolve

    analysis = working_set_resolve.analyze_turn("where are we with Orchard?")
    candidates = working_set_resolve.candidates_for(
        analysis,
        working_set_resolve.facts_from_rows(rows),
        term_anchor_counts=index.term_anchor_counts(),
    )
    by_path = {c.path: c for c in candidates}
    search_candidate = by_path.get(search.path)
    assert search_candidate is None or "exact_alias" not in search_candidate.evidence


def test_a_dash_title_whose_lead_names_at_most_three_anchors_is_admitted(tmp_path: Path) -> None:
    """The other half of the same guard: a genuinely rare dash-derived name
    still keeps its alias (canonical spec's "A title's leading name resolves
    while it is unique" scenario, on a dash qualifier rather than a
    parenthetical one).
    """
    vault = tmp_path / "vault"
    _resource(vault, "Knowledge Base/Products/widget.md", "Widget - Blue trim")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    widget = _by_title(index.anchors(), "Widget - Blue trim")
    assert "widget" in widget.aliases
    assert index.term_anchor_counts().get("widget") == 1


_LEDGER_COLLECTION = """---
type: collection
exomem_id: {exomem_id}
title: Ledger
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
claims:
  terms: [ledger]
item_schema:
  natural_key: [observed_on, asset]
  fields:
    observed_on:
      type: date
      required: true
    asset:
      type: string
      required: true
---

Ledger collection.
"""


def _write_ledger_collection(vault: Path, exomem_id: str) -> None:
    _write(
        vault / "Knowledge Base/Records/Ledger/_collection.md",
        _LEDGER_COLLECTION.format(exomem_id=exomem_id),
    )
    (vault / "Knowledge Base/Records/Ledger/Items").mkdir(parents=True, exist_ok=True)


def test_a_derived_page_alias_yields_to_a_collections_own_title(tmp_path: Path) -> None:
    """MAJOR 6 (review round 3): the derived-name uniqueness gate must see
    every anchor KIND before deciding a derived alias, not pages alone. A
    page titled "Ledger (spare copy)" would derive "ledger" as an alias if
    only OTHER PAGES were checked for uniqueness -- but a Records collection
    is titled exactly "Ledger", so the derived name is withheld on the same
    uniqueness ground a second PAGE named "Ledger" would withhold it.
    """
    vault = tmp_path / "vault"
    _resource(vault, "Knowledge Base/Products/ledger-spare.md", "Ledger (spare copy)")
    _write_ledger_collection(vault, "6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c22")

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    rows = index.anchors()
    assert "collection" in _kinds(rows)
    spare = _by_title(rows, "Ledger (spare copy)")
    assert "ledger" not in spare.aliases


def test_term_anchor_counts_cover_every_kind_not_pages_alone(tmp_path: Path) -> None:
    """MAJOR 7 (review round 3): `term_anchor_counts` must cover exactly the
    anchors held (post-cap), across EVERY kind -- the comment once claimed
    this while pages alone were computed uncapped and every other kind
    capped separately. A term that appears ONLY in a Records collection's
    title must be counted, at exactly the count of anchors that hold it.
    """
    vault = tmp_path / "vault"
    _resource(vault, "Knowledge Base/Products/unrelated-page.md", "Unrelated page")
    _write_ledger_collection(vault, "6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c33")

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    rows = index.anchors()
    ledger_collection = _by_title(rows, "Ledger")
    assert ledger_collection.kind == "collection"
    assert index.term_anchor_counts().get("ledger") == 1


def test_the_term_count_table_matches_authored_names_alone(tmp_path: Path) -> None:
    """(d): the persisted term-count table is identical whether or not any
    anchor has a derived alias -- computed here independently, from raw
    frontmatter title/alias data only, and compared against the index's own
    table over a vault where one derived alias IS admitted (widget/blue-trim)
    and one is deliberately withheld (the Orchard cluster).
    """
    vault = tmp_path / "vault"
    _resource(vault, "Knowledge Base/Products/widget.md", "Widget - Blue trim")
    _resource(vault, "Knowledge Base/Products/orchard-strategy.md", "Orchard Strategy")
    _resource(vault, "Knowledge Base/Products/orchard-roadmap.md", "Orchard Roadmap")
    _resource(vault, "Knowledge Base/Products/orchard-architecture.md", "Orchard platform architecture")
    _resource(vault, "Knowledge Base/Products/orchard-search.md", "Orchard — Agentic Search")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    expected: dict[str, set[str]] = {}
    for row in index.anchors():
        # Authored names only: `row.aliases` already includes any admitted
        # derived alias, so a leak would inflate this reference computation
        # the same way it would inflate the real table -- the point is that
        # neither does, not that this recomputation happens to dodge it.
        authored = row.aliases
        for term in {
            working_set_index.fold_plural(t)
            for t in working_set_index.tokens_of(" ".join((row.title, *authored)))
        }:
            expected.setdefault(term, set()).add(row.anchor_id)
    # "orchard" IS an authored title term for all four pages (never a derived
    # alias -- withheld above), and "widget"/"blue"/"trim" are authored title
    # terms for the one widget page; "widget" is ALSO its (admitted) derived
    # alias, contributing no second owner since it is the same anchor.
    expected_counts = {term: len(ids) for term, ids in expected.items()}

    assert index.term_anchor_counts() == expected_counts
    assert expected_counts[working_set_index.fold_plural("orchard")] == 4
    assert expected_counts["widget"] == 1


def test_term_anchor_counts_are_measured_over_title_and_alias_terms_only(
    vault: Path,
) -> None:
    _resource(vault, "Knowledge Base/Products/Bike.md", "Bike (Trek 520, 2019)")
    _write(
        vault / "Knowledge Base" / "Notes" / "shared-word.md",
        """---
type: insight
status: active
updated: 2026-09-11
---

# Shared word note

## Bike

Body mentions bike only in a heading, never in the title or an alias.
""",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    counts = index.term_anchor_counts()
    # "bike" names exactly one anchor's title/alias (the derived alias adds a
    # second name, "bike", but it is the SAME anchor, so the count stays 1) --
    # the second page's heading is body vocabulary, never counted here.
    assert counts.get("bike") == 1


def test_term_anchor_counts_equal_under_rebuild_and_incremental_update(vault: Path) -> None:
    _resource(vault, "Knowledge Base/Products/Bike.md", "Bike (Trek 520, 2019)")
    incremental = working_set_index.WorkingSetIndex(vault)
    incremental.rebuild()

    _resource(vault, "Knowledge Base/Products/Trek2.md", "Bike (Cannondale, 2021)")
    incremental.update()
    incremental_counts = incremental.term_anchor_counts()
    incremental.close()

    rebuilt = working_set_index.WorkingSetIndex(vault)
    rebuilt.reset()
    rebuilt.rebuild()

    assert incremental_counts == rebuilt.term_anchor_counts()


@pytest.mark.parametrize(
    ("turn", "titles"),
    [
        (
            "What did Ada Lovelace and Grace Hopper each contribute?",
            {"Ada Lovelace", "Grace Hopper"},
        ),
        (
            "How do Envelope and Write-Ahead Log relate?",
            {"Envelope", "Write-Ahead Log"},
        ),
    ],
)
def test_two_directly_linked_anchors_are_complementary(
    vault: Path, turn: str, titles: set[str]
) -> None:
    """A typed link between the two anchors IS the relatedness the rule looks for.

    Both turns name two same-kind anchors of the real fixture vault that link to
    each other. Asking only whether they share a THIRD anchor abstains on exactly
    the turns the packet exists to serve — and abstains while both anchors carry
    `graph_corroboration` earned from the very edge that makes them related.
    """
    resolution = _resolution_for(vault, turn)

    assert resolution.status == "resolved", resolution.as_dict()
    assert resolution.ambiguity == ()
    resolved = {
        anchor.title: anchor for anchor in resolution.anchors if anchor.status == "resolved"
    }
    assert titles <= set(resolved), sorted(resolved)
    for title in titles:
        assert "graph_corroboration" in resolved[title].evidence, resolved[title]


def test_managed_activation_abstains_while_the_identity_inventory_is_cold(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cold private-identity inventory is built by a whole-vault walk under the
    # all-domain identity gate. An interactive turn must not be the caller that
    # pays for it: it abstains as warming and the build runs in the background.
    from exomem import readiness, reserved_paths, working_set_runtime

    working_set_index.WorkingSetIndex(seeded).rebuild()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(reserved_paths, "identity_catalogue_ready", lambda root: False)
    warmed: list[Path] = []
    monkeypatch.setattr(reserved_paths, "schedule_identity_catalogue_warm", warmed.append)

    state, index, stale = working_set_runtime.ensure_index(seeded)

    assert (state, index, stale) == (working_set_runtime.WARMING, None, False)
    assert warmed == [seeded]


def test_managed_activation_serves_once_the_identity_inventory_is_ready(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, reserved_paths, working_set_runtime

    working_set_index.WorkingSetIndex(seeded).rebuild()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(reserved_paths, "identity_catalogue_ready", lambda root: True)
    warmed: list[Path] = []
    monkeypatch.setattr(reserved_paths, "schedule_identity_catalogue_warm", warmed.append)

    state, index, _stale = working_set_runtime.ensure_index(seeded)

    assert state == working_set_runtime.READY
    assert index is not None
    assert warmed == []


def test_unmanaged_activation_ignores_identity_inventory_readiness(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, reserved_paths, working_set_runtime

    working_set_index.WorkingSetIndex(seeded).rebuild()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    monkeypatch.setattr(reserved_paths, "identity_catalogue_ready", lambda root: False)
    warmed: list[Path] = []
    monkeypatch.setattr(reserved_paths, "schedule_identity_catalogue_warm", warmed.append)

    state, index, _stale = working_set_runtime.ensure_index(seeded)

    assert state == working_set_runtime.READY
    assert index is not None
    assert warmed == []
