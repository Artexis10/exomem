"""D1-T10: triage of upkeep items, bound to the current fingerprint.

Decisions live in the portable review state under the candidate's own id, so
they survive a rebuilt sidecar. Link items share the relation queue's identity:
triaging them there or here writes one record.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    freshness,
    review_state,
    upkeep,
)

LATER = time.time() + 3 * 3600


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _settled(tmp_path: Path) -> Path:
    vault = fx.build(tmp_path)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    return vault


def _hydration(vault: Path) -> dict:
    view = dreamer_store.read_view(vault)
    return next(
        row for row in view.candidates if row["family"] == dreamer_families.HYDRATION_FAMILY
    )


def _ref(row: dict) -> str:
    return upkeep.upkeep_ref(row["id"])


def _open_ids(vault: Path, state: str = "open") -> set[str]:
    return {item["ref"] for item in upkeep.review(vault, state=state)["items"]}


def test_dismiss_snooze_reopen_bind_to_the_current_fingerprint(tmp_path: Path) -> None:
    vault = _settled(tmp_path)
    row = _hydration(vault)
    ref = _ref(row)
    assert ref in _open_ids(vault)

    result = commands.op_triage_memory(vault, ref=ref, action="dismiss", why="false_positive: x")
    assert result["ref"] == ref
    assert result["fingerprint"] == row["fingerprint"]
    assert result["state"] == "dismissed"
    assert ref not in _open_ids(vault)
    assert ref in _open_ids(vault, "dismissed")
    decision = review_state.ReviewStateStore(vault).decision(row["id"], row["fingerprint"])
    assert decision is not None and decision.action == "dismiss"

    until = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    commands.op_triage_memory(vault, ref=ref, action="snooze", until=until)
    assert ref in _open_ids(vault, "snoozed")
    commands.op_triage_memory(vault, ref=ref, action="reopen")
    assert ref in _open_ids(vault)
    with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
        commands.op_triage_memory(vault, ref=ref, action="competing")


def test_stale_expected_fingerprint_is_refused(tmp_path: Path) -> None:
    vault = _settled(tmp_path)
    row = _hydration(vault)
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        commands.op_triage_memory(
            vault, ref=_ref(row), action="dismiss", expected_fingerprint="f" * 24
        )
    assert review_state.ReviewStateStore(vault).decision(row["id"], row["fingerprint"]) is None
    accepted = commands.op_triage_memory(
        vault, ref=_ref(row), action="dismiss", expected_fingerprint=row["fingerprint"]
    )
    assert accepted["state"] == "dismissed"


def test_family_refs_are_registered_and_listed_in_dispositions(tmp_path: Path) -> None:
    vault = _settled(tmp_path)
    registered = review_state.registered_families()
    assert {"upkeep_link", "upkeep_hydration"} <= registered
    commands.op_triage_memory(
        vault, ref="exomem://review/family/upkeep_link", action="quiet", why="too_frequent: x"
    )
    listed = commands.op_review_memory(vault, mode="dispositions")
    families = {row["family"]: row for row in listed["dispositions"]}
    assert "upkeep_hydration" in listed["registered_families"]
    assert families["upkeep_link"]["disposition"] == "quiet"


def test_triage_stops_delivery_in_process_at_once(tmp_path: Path) -> None:
    vault = _settled(tmp_path)
    row = _hydration(vault)
    view = dreamer_store.read_view(vault)
    assert row["id"] in {r["id"] for r in upkeep.deliverable_rows(vault, view)}
    commands.op_triage_memory(vault, ref=_ref(row), action="dismiss", why="handled: done")
    # No worker pass in between: the same stored view is no longer deliverable.
    assert dreamer_store.read_view(vault) is view
    assert row["id"] not in {r["id"] for r in upkeep.deliverable_rows(vault, view)}


def test_link_items_triage_through_the_relation_namespace(tmp_path: Path) -> None:
    vault = _settled(tmp_path)
    listed = upkeep.review(vault, categories=["upkeep_link"])["items"]
    link = next(
        item for item in listed if item["dispose"]["args"].get("source_path") == fx.CAVITATION
    )
    assert link["ref"].startswith("exomem://review/relation/")
    assert link["dispose"]["args"]["ref"] == link["ref"]
    assert link["route"]["args"]["ref"] == link["ref"]
    assert link["context_route"]["args"]["ref"].startswith(upkeep.UPKEEP_PREFIX)
    view = dreamer_store.read_view(vault)
    cid = link["ref"].rsplit("/", 1)[-1]
    assert cid in {r["id"] for r in upkeep.deliverable_rows(vault, view)}

    commands.op_triage_memory(
        vault,
        ref=link["ref"],
        action="dismiss",
        why="false_positive: unrelated",
        source_path=fx.CAVITATION,
    )
    # One record, keyed on the relation identity the upkeep row shares.
    decision = review_state.ReviewStateStore(vault).decision(cid, link["fingerprint"])
    assert decision is not None and decision.action == "dismiss"
    assert link["ref"] not in {i["ref"] for i in upkeep.review(vault)["items"]}
    # A decision made outside this module is picked up by the token re-check.
    assert cid not in {r["id"] for r in upkeep.deliverable_rows(vault, view)}
