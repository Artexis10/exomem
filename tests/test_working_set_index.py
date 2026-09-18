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
        working_set_runtime, "_schedule_build", lambda root: scheduled.append(root)
    )

    packet = working_set_runtime.serve(seeded, turn="the cargo sled", max_chars=2000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "index_warming"}
    assert packet["units"] == []
    assert len(scheduled) == 1

    # A managed runtime never builds inline: the sidecar may exist (it is opened
    # to read the generation) but it holds no anchor rows yet.
    assert working_set_index.WorkingSetIndex(seeded).anchors() == ()


def test_a_managed_runtime_reports_a_stale_index_rather_than_walking(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(seeded).rebuild(freshness_stamp="old")

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root: scheduled.append(root)
    )

    packet = working_set_runtime.serve(
        seeded, turn="the cargo sled", max_chars=2000, freshness_key="new"
    )

    assert packet["generation"]["index_stale"] is True
    assert packet["generation"]["index_generation"] >= 1
    assert len(scheduled) == 1


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
