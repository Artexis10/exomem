"""The alias/anchor family, `upkeep_alias` (design §8).

Other notes link a page by a spelling the page does not carry: a bare wikilink
the published graph leaves unresolved whose shared `vocabulary_fold.fold_term`
equals one of the page's names. The proposal routes to `edit_memory` on the
page's `aliases`. A fold key two pages carry is an identity ambiguity and is
never proposed over; the unresolved link stays with the audit category that
owns it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import dreamer, dreamer_families, dreamer_store, freshness, upkeep

LATER = time.time() + 3 * 3600
ALIAS_ID = dreamer_store.candidate_id("anchor.alias", fx.ENTITY, fx.VARIANT_KEY)


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()


def _quiet(vault: Path, *, now: float | None = None) -> None:
    results = fx.run_to_quiet(vault, now=now)
    assert all(result.stop_reason != "error" for result in results), results


def _row(vault: Path, cid: str = ALIAS_ID) -> dict | None:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        return dreamer_store.DreamerStore.candidate(conn, cid)
    finally:
        conn.close()


def _open(vault: Path, cid: str = ALIAS_ID) -> dict | None:
    row = _row(vault, cid)
    return row if row is not None and row["state"] == "open" else None


def _served(vault: Path) -> list[dict]:
    dreamer_store.clear_reader_memo()
    return [
        item
        for item in upkeep.review(vault, limit=50)["items"]
        if item["family"] == dreamer_families.ALIAS_FAMILY
    ]


def _entity_with(frontmatter: str) -> str:
    return fx.entity().replace("updated: 2026-01-10\n", f"updated: 2026-01-10\n{frontmatter}")


def test_notes_naming_a_page_by_a_variant_spelling_make_one_proposal(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    row = _open(vault)
    assert row is not None
    assert row["family"] == dreamer_families.ALIAS_FAMILY == "upkeep_alias"
    assert row["kind"] == dreamer_families.ALIAS_KIND == "anchor.alias"
    assert row["subject_path"] == fx.ENTITY
    assert row["reason_code"] == "variant_reference"
    roles = {item["path"]: item["role"] for item in row["evidence"]}
    assert roles == {
        fx.ENTITY: "subject",
        fx.REFERRER_ONE: "referrer",
        fx.REFERRER_TWO: "referrer",
    }
    assert {item.get("spelling") for item in row["evidence"] if item["role"] == "referrer"} == {
        fx.VARIANT
    }
    assert all(item["sig"] for item in row["evidence"])
    route = row["route"]
    assert route["tool"] == "edit_memory"
    assert route["args"]["path"] == fx.ENTITY
    assert route["args"]["operation"] == {"kind": "patch_frontmatter", "field": "aliases"}
    # Only one alias proposal: a referrer is never a subject of its own.
    view = dreamer_store.read_view(vault)
    alias_rows = [
        r
        for r in view.candidates
        if r["family"] == dreamer_families.ALIAS_FAMILY and r["state"] == "open"
    ]
    assert [r["id"] for r in alias_rows] == [ALIAS_ID]


def test_the_served_item_names_the_page_the_referrers_and_the_spelling(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    [item] = _served(vault)
    assert item["ref"] == upkeep.upkeep_ref(ALIAS_ID)
    assert item["kind"] == "anchor.alias"
    assert item["label"] == upkeep.LABELS["anchor.alias"]
    assert item["subject"]["title"] == "Orbit Pump"
    assert item["evidence_count"] == 2
    assert {entry["title"] for entry in item["evidence"]} == {"Pump restart", "Pump drain"}
    assert {entry["spelling"] for entry in item["evidence"]} == {fx.VARIANT}
    assert fx.VARIANT in item["why"]
    assert item["route"]["tool"] == "edit_memory"
    # item and context revalidate to the same fingerprint the list served.
    assert upkeep.item(vault, item["ref"])["item"]["fingerprint"] == item["fingerprint"]
    context = upkeep.context(vault, item["ref"], expected_fingerprint=item["fingerprint"])
    assert context["subject"]["path"] == fx.ENTITY
    assert context["subject"]["content_hash"]
    assert {entry["path"] for entry in context["evidence"]} == {fx.REFERRER_ONE, fx.REFERRER_TWO}


def test_the_page_gaining_the_alias_resolves_the_proposal(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    assert _open(vault) is not None
    fx.edit(vault, fx.ENTITY, _entity_with(f'aliases:\n  - "{fx.VARIANT}"\n'))
    _quiet(vault)
    assert _open(vault) is None
    assert _row(vault)["state"] == "resolved"


def test_a_second_page_gaining_the_name_withholds_it_as_an_ambiguity(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    assert _served(vault)
    rival = f"{fx.KB}/Notes/Entities/orbit-pump-rig.md"
    fx.edit(
        vault,
        rival,
        fx.entity()
        .replace("title: Orbit Pump", "title: Orbit Pump Rig")
        .replace("updated: 2026-01-10\n", 'updated: 2026-01-10\naliases:\n  - "orbit pump"\n'),
    )
    _quiet(vault)
    # Never proposed over: nothing is served for the fold key...
    assert _served(vault) == []
    # ...and the unresolved link is reported under the audit category that owns it.
    dreamer_store.clear_reader_memo()
    assert upkeep.review(vault)["integrity"] == {"forward_reference": 1}
    # The rival giving the name up restores the proposal and clears the finding.
    fx.remove(vault, rival)
    _quiet(vault)
    assert len(_served(vault)) == 1
    dreamer_store.clear_reader_memo()
    assert upkeep.review(vault)["integrity"] == {}


def test_a_learned_name_confirmed_by_the_corpus_is_offered_for_promotion(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path, with_graph=False)
    fx.write(vault, fx.ENTITY, _entity_with("learned_aliases:\n  - orbit pumps\n"))
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    row = _open(vault)
    assert row is not None
    assert row["reason_code"] == "learned_alias_referenced"
    # The link-resolving field, never `learned_aliases`.
    assert row["route"]["args"]["operation"] == {"kind": "patch_frontmatter", "field": "aliases"}


def test_the_result_does_not_depend_on_processing_order(tmp_path: Path) -> None:
    """Referrers processed before the page exists, then the page arrives."""
    vault = fx.build_vocabulary(tmp_path, with_graph=False)
    entity_text = (vault / fx.ENTITY).read_text("utf-8")
    (vault / fx.ENTITY).unlink()
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    assert _row(vault) is None
    fx.edit(vault, fx.ENTITY, entity_text)
    _quiet(vault)
    assert _open(vault) is not None
    # And the other way round: the page first, a referrer later.
    fx.remove(vault, fx.REFERRER_TWO)
    _quiet(vault)
    assert {item["path"] for item in _open(vault)["evidence"]} == {fx.ENTITY, fx.REFERRER_ONE}
    fx.edit(vault, fx.REFERRER_TWO, fx.note("Pump drain", links=f"Drain the [[{fx.VARIANT}]]."))
    _quiet(vault)
    assert {item["path"] for item in _open(vault)["evidence"]} == {
        fx.ENTITY,
        fx.REFERRER_ONE,
        fx.REFERRER_TWO,
    }


def test_a_path_shaped_link_is_not_a_variant_name(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path, with_graph=False)
    for rel, title in ((fx.REFERRER_ONE, "Pump restart"), (fx.REFERRER_TWO, "Pump drain")):
        fx.write(vault, rel, fx.note(title, links="See [[Notes/Entities/Orbit Pumps]]."))
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    assert _row(vault) is None


def test_an_unchanged_rerun_adds_no_row_and_keeps_the_fingerprint(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    _quiet(vault, now=LATER)
    before = _open(vault)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        generation = store.generation(conn)
        idle = dreamer.run_once(vault, clock=dreamer.Clock(time=lambda: LATER))
        assert idle.stop_reason == "idle"
        assert store.generation(conn) == generation
    finally:
        conn.close()
    # A same-content rewrite of a referrer is processed again and changes nothing.
    fx.edit(vault, fx.REFERRER_ONE, (vault / fx.REFERRER_ONE).read_text("utf-8"), graph=False)
    _quiet(vault, now=LATER)
    after = _open(vault)
    assert after["fingerprint"] == before["fingerprint"]
    assert after["id"] == before["id"]


def test_a_new_referrer_moves_the_fingerprint_not_the_identity(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    before = _open(vault)
    third = f"{fx.KB}/Notes/Insights/pump-flush.md"
    fx.edit(vault, third, fx.note("Pump flush", links=f"Flush the [[{fx.VARIANT}]] monthly."))
    _quiet(vault)
    after = _open(vault)
    assert after["id"] == before["id"]
    assert after["fingerprint"] != before["fingerprint"]
    assert third in {item["path"] for item in after["evidence"]}


def test_a_reseed_delivers_no_alias_item_until_it_drains(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    _quiet(vault, now=LATER)
    assert _open(vault)["deliverable"] is True
    # The first enable was itself a reseed: once drained, evidence is complete.
    dreamer_store.clear_reader_memo()
    assert upkeep.review(vault)["evidence_complete"][dreamer_families.ALIAS_FAMILY] is True
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        with store.write(conn):
            store.set_meta(conn, "reseeding", True)
            store.pending_add(conn, [fx.TAG_ONE])
            ctx = dreamer_families.Context(vault_root=vault, store=store, conn=conn, now=LATER)
            dreamer_families.precompute_deliverable(ctx)
        assert store.candidate(conn, ALIAS_ID)["deliverable"] is False
        with store.write(conn):
            store.pending_remove(conn, fx.TAG_ONE)
            ctx = dreamer_families.Context(vault_root=vault, store=store, conn=conn, now=LATER)
            dreamer_families.precompute_deliverable(ctx)
        assert store.candidate(conn, ALIAS_ID)["deliverable"] is True
    finally:
        conn.close()


def test_the_sidecar_holds_page_contributions_under_schema_three(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    assert dreamer_store.SCHEMA_VERSION == 3
    conn = sqlite3.connect(dreamer_store.sidecar_path(vault))
    try:
        names = conn.execute(
            "SELECT path, source FROM name_keys WHERE fold_key=? ORDER BY path, source",
            (fx.VARIANT_KEY,),
        ).fetchall()
        refs = conn.execute(
            "SELECT path, raw FROM name_refs WHERE fold_key=? ORDER BY path",
            (fx.VARIANT_KEY,),
        ).fetchall()
    finally:
        conn.close()
    assert {path for path, _source in names} == {fx.ENTITY}
    assert {source for _path, source in names} >= {"title", "stem"}
    assert refs == sorted([(fx.REFERRER_ONE, fx.VARIANT), (fx.REFERRER_TWO, fx.VARIANT)])


def test_an_older_sidecar_is_wiped_and_reseeded(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    conn = sqlite3.connect(dreamer_store.sidecar_path(vault))
    try:
        conn.execute("UPDATE meta SET value='2' WHERE key='schema'")
        conn.commit()
    finally:
        conn.close()
    dreamer_store.clear_reader_memo()
    assert dreamer_store.read_view(vault) is None
    _quiet(vault)
    assert _open(vault) is not None
