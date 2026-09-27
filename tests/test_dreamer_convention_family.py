"""The convention/category family, `upkeep_convention` (design §8).

`convention.tag`: notes carry fold-equal tags in two or more spellings. One
proposal per fold-key cluster, its identity from the key alone, served on a
note carrying the strictly outnumbered spelling; the server never declares the
vault's spelling.

`convention.category`: a unit's category label is unregistered but folds to
exactly one registered category, or names a definition with `replaced_by`.
One proposal per page and label, anchored on the registry's content hash.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import dreamer, dreamer_families, dreamer_store, freshness, upkeep

LATER = time.time() + 3 * 3600
TAG_ID = dreamer_store.candidate_id("convention.tag", "", fx.TAG_KEY)
CATEGORY_ID = dreamer_store.candidate_id("convention.category", fx.CATEGORY_NOTE, fx.CATEGORY_KEY)


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


def _row(vault: Path, cid: str) -> dict | None:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        return dreamer_store.DreamerStore.candidate(conn, cid)
    finally:
        conn.close()


def _open(vault: Path, cid: str) -> dict | None:
    row = _row(vault, cid)
    return row if row is not None and row["state"] == "open" else None


def _served(vault: Path, kind: str) -> list[dict]:
    dreamer_store.clear_reader_memo()
    return [item for item in upkeep.review(vault, limit=50)["items"] if item["kind"] == kind]


# ----------------------------------------------------------------------
# convention.tag
# ----------------------------------------------------------------------


def test_tag_spellings_drifting_across_notes_make_one_cluster_proposal(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    row = _open(vault, TAG_ID)
    assert row is not None
    assert row["family"] == dreamer_families.CONVENTION_FAMILY == "upkeep_convention"
    assert row["kind"] == dreamer_families.TAG_KIND == "convention.tag"
    assert row["reason_code"] == "tag_spelling_variant"
    # The identity is the fold key alone: no member page's path is in the ref.
    assert row["ref"] == upkeep.upkeep_ref(TAG_ID)
    for member in (fx.TAG_ONE, fx.TAG_TWO, fx.TAG_MINOR):
        assert member not in row["ref"]
    [cluster] = [
        r
        for r in dreamer_store.read_view(vault).candidates
        if r["kind"] == "convention.tag" and r["state"] == "open"
    ]
    assert cluster["id"] == TAG_ID


def test_the_tag_item_is_served_on_the_minority_note_with_counts(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    [item] = _served(vault, "convention.tag")
    assert item["ref"] == upkeep.upkeep_ref(TAG_ID)
    assert item["label"] == upkeep.LABELS["convention.tag"]
    assert item["subject"]["title"] == "Pump care checklist"
    assert {entry["title"] for entry in item["evidence"]} == {
        "Pump care log",
        "Pump care schedule",
    }
    assert {entry["spelling"] for entry in item["evidence"]} == {fx.TAG_MAJORITY}
    assert item["evidence_count"] == 2
    # The released count of each spelling, and no canonical spelling declared.
    assert f'"{fx.TAG_MAJORITY}" on 2' in item["why"]
    assert f'"{fx.TAG_MINORITY}" on 1' in item["why"]
    assert item["route"] == {
        "tool": "edit_memory",
        "args": {"path": fx.TAG_MINOR, "operation": {"kind": "replace_tags"}},
    }
    current = upkeep.item(vault, item["ref"])["item"]
    assert current["fingerprint"] == item["fingerprint"]
    context = upkeep.context(vault, item["ref"])
    assert context["subject"]["path"] == fx.TAG_MINOR


def test_an_even_split_is_stored_but_not_served(tmp_path: Path) -> None:
    """The served subject's spelling must be strictly outnumbered."""
    vault = fx.build_vocabulary(tmp_path)
    fx.remove(vault, fx.TAG_TWO)
    _quiet(vault)
    assert _open(vault, TAG_ID) is not None
    assert _served(vault, "convention.tag") == []


def test_a_cluster_collapsing_to_one_spelling_resolves(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    assert _open(vault, TAG_ID) is not None
    fx.edit(vault, fx.TAG_MINOR, fx.note("Pump care checklist", tags=(fx.TAG_MAJORITY,)))
    _quiet(vault)
    assert _open(vault, TAG_ID) is None
    assert _row(vault, TAG_ID)["state"] == "resolved"


def test_a_member_changing_spelling_moves_the_fingerprint_not_the_identity(
    tmp_path: Path,
) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    before = _open(vault, TAG_ID)
    extra = f"{fx.KB}/Notes/Insights/pump-care-audit.md"
    fx.edit(vault, extra, fx.note("Pump care audit", tags=("pump_care",)))
    _quiet(vault)
    after = _open(vault, TAG_ID)
    assert after["id"] == before["id"]
    assert after["fingerprint"] != before["fingerprint"]
    # Unchanged content rewritten: the same fingerprint again.
    fx.edit(vault, extra, (vault / extra).read_text("utf-8"), graph=False)
    _quiet(vault)
    assert _open(vault, TAG_ID)["fingerprint"] == after["fingerprint"]


def test_a_deleted_member_is_dropped_from_the_cluster(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    fx.remove(vault, fx.TAG_MINOR)
    _quiet(vault)
    assert _open(vault, TAG_ID) is None


# ----------------------------------------------------------------------
# convention.category
# ----------------------------------------------------------------------


def test_a_label_folding_to_one_registered_category_is_proposed(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    row = _open(vault, CATEGORY_ID)
    assert row is not None
    assert row["kind"] == dreamer_families.CATEGORY_KIND == "convention.category"
    assert row["reason_code"] == "category_fold_registered"
    assert row["subject_path"] == fx.CATEGORY_NOTE
    assert [item["path"] for item in row["evidence"]] == [fx.CATEGORY_NOTE]
    assert row["route"]["tool"] == "edit_memory"
    assert row["route"]["args"]["path"] == fx.CATEGORY_NOTE
    [item] = _served(vault, "convention.category")
    assert "field_trial" in item["why"]
    assert fx.CATEGORY_LABEL in item["why"]


def test_a_registry_change_moves_the_category_fingerprint(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    ref = upkeep.upkeep_ref(CATEGORY_ID)
    before = upkeep.item(vault, ref)["item"]["fingerprint"]
    stored = _open(vault, CATEGORY_ID)["fingerprint"]
    assert before == stored
    fx.write(
        vault,
        fx.REGISTRY,
        fx.registry(extra="  bench_note:\n    description: A note from the bench.\n"),
    )
    assert upkeep.item(vault, ref)["item"]["fingerprint"] != before
    # The worker notices the registry moved and revalidates the stored row.
    _quiet(vault)
    assert _open(vault, CATEGORY_ID)["fingerprint"] != stored


def test_a_label_named_by_a_replaced_definition_is_proposed(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path, with_graph=False)
    fx.write(
        vault,
        fx.REGISTRY,
        fx.registry(
            extra=(
                "  bench_test:\n"
                "    description: A bench test.\n"
                "    status: deprecated\n"
                "    replaced_by: field_trial\n"
            )
        ),
    )
    fx.write(
        vault,
        fx.CATEGORY_NOTE,
        fx.note("Pump bench results", category="bench test", observation="The seal held."),
    )
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    row = _open(vault, dreamer_store.candidate_id("convention.category", fx.CATEGORY_NOTE, "bench-test"))
    assert row is not None
    assert row["reason_code"] == "category_replaced"
    assert row["measures"]["target"] == "field_trial"


def test_a_label_becoming_registered_resolves_the_proposal(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path)
    _quiet(vault)
    assert _open(vault, CATEGORY_ID) is not None
    fx.edit(
        vault,
        fx.CATEGORY_NOTE,
        fx.note("Pump bench results", category="field_trial", observation="The seal held."),
    )
    _quiet(vault)
    assert _open(vault, CATEGORY_ID) is None


def test_a_label_reaching_two_registered_categories_is_not_proposed(tmp_path: Path) -> None:
    vault = fx.build_vocabulary(tmp_path, with_graph=False)
    fx.write(vault, fx.REGISTRY, fx.registry(extra="  batch:\n    description: One run.\n"))
    fx.write(
        vault,
        fx.CATEGORY_NOTE,
        fx.note("Pump bench results", category="batchs", observation="The seal held."),
    )
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    batch_id = dreamer_store.candidate_id("convention.category", fx.CATEGORY_NOTE, "batch")
    assert _open(vault, batch_id)["measures"]["target"] == "batch"
    # `batches` folds alike: the label now reaches two registered categories.
    fx.write(
        vault,
        fx.REGISTRY,
        fx.registry(
            extra="  batch:\n    description: One run.\n  batches:\n    description: Runs.\n"
        ),
    )
    _quiet(vault)
    assert _open(vault, batch_id) is None
