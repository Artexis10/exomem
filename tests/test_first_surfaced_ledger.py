"""The first-surfaced ledger: when a signal first reached a served surface.

The third capture primitive. Nag rate and recovery fraction are computable only
against the moment a signal was first *shown to somebody*, which the runtime has
never recorded. The ledger starts empty, is never backfilled, never records
anything egress withheld or a disposition removed, is never written by audit
measurement, and is failure-isolated from the read that populates it.

The first test is the GAP PROOF: today no surface stamps anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _nag_governance_helpers import (
    advisory_candidate,
    overdue_prediction,
    scratch_page,
    seed_page,
)

from exomem import attention as attention_module
from exomem import commands, corpus_aware, review_state

FAMILY_REF = "exomem://review/family/prediction_window"


def _ledger(vault: Path) -> dict:
    payload = review_state.ReviewStateStore(vault).load()
    return payload.get("surfaced") or {}


def _write_advisories(vault: Path) -> list[str]:
    target = seed_page(vault, "nag-editable", "Repeated body.")
    return corpus_aware.emit_write_advisories(
        vault,
        self_path=target,
        kind="near-duplicate",
        candidates=[advisory_candidate(vault)],
    )


# ==========================================================================
# THE GAP PROOF
# ==========================================================================


def test_no_surface_records_a_first_surfacing_today(vault: Path) -> None:
    """RED-FIRST. Three served surfaces, nothing stamped, nothing exposed."""
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)

    report = commands.op_attention(vault, limit=0)
    due_state_module.served(vault)
    _write_advisories(vault)

    silent: list[str] = []
    if not _ledger(vault):
        silent.append("the review state holds no first-surfaced records at all")
    if not any("first_surfaced_at" in item for item in report["items"]):
        silent.append("no attention item carries `first_surfaced_at`")

    assert silent == [], "; ".join(silent)


# ==========================================================================
# stamping
# ==========================================================================


def test_the_first_listing_stamps_the_ledger_once(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)

    first = commands.op_attention(vault, limit=0)["items"]
    ledger_after_first = dict(_ledger(vault))
    second = commands.op_attention(vault, limit=0)["items"]

    stamped = {item["path"]: item["first_surfaced_at"] for item in first}
    again = {item["path"]: item["first_surfaced_at"] for item in second}
    assert stamped and stamped == again
    assert _ledger(vault) == ledger_after_first
    assert len(ledger_after_first) == len(first)
    assert {row["surface"] for row in ledger_after_first.values()} == {"review"}


def test_a_served_due_state_reference_stamps_the_carrier_surface(vault: Path) -> None:
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.served(vault)

    surfaces = {row["surface"] for row in _ledger(vault).values()}
    assert surfaces == {"carrier"}


def test_an_emitted_write_advisory_stamps_the_write_surface(vault: Path) -> None:
    assert _write_advisories(vault)
    surfaces = {row["surface"] for row in _ledger(vault).values()}
    assert surfaces == {"write"}


def test_a_suppressed_write_advisory_is_never_stamped(vault: Path) -> None:
    commands.op_triage_memory(
        vault,
        ref="exomem://review/family/near-duplicate",
        action="quiet",
        why="false_positive: deliberate near-duplicates here",
    )
    assert _write_advisories(vault) == []
    assert _ledger(vault) == {}


def test_resolving_one_reference_records_nothing(vault: Path) -> None:
    """A lookup is not a surfacing.

    `item_by_ref` scans every queue at `state="all"` to resolve ONE reference, so
    without the distinction a request that shows a single item would stamp a
    first surfacing for every item in the vault — and `review_item_context`,
    which is documented as not mutating the vault, would write to the store on
    every call. The store is removed after the listing so the assertion cannot
    be satisfied by entries the listing itself had already recorded.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    listed = commands.op_attention(vault, limit=0)["items"]
    assert listed
    review_state.state_path(vault).unlink()

    for item in listed:
        commands.op_review_item_context(vault, ref=item["ref"])

    assert _ledger(vault) == {}


def test_audit_never_records(vault: Path) -> None:
    overdue_prediction(vault)
    commands.op_audit(vault, categories=["prediction_window"], detail="full")
    assert _ledger(vault) == {}


def test_a_disposition_excluded_signal_is_never_recorded(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    listed = [
        item
        for item in commands.op_attention(vault, limit=0)["items"]
        if "nag-backlog" in item["path"]
    ][0]
    excluded_key = f"{listed['item_id']}:{listed['fingerprint']}"
    assert excluded_key in _ledger(vault)

    # A fresh store, so the earlier listing cannot be what the assertion sees.
    review_state.state_path(vault).unlink()
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="quiet", why="too_frequent: enough"
    )

    commands.op_attention(vault, limit=0)

    assert excluded_key not in _ledger(vault)


def test_a_withheld_signal_is_never_recorded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Egress decides before the ledger sees anything, on every audience."""
    from exomem import due_state as due_state_module
    from exomem.governance import egress as egress_module

    overdue_prediction(vault)
    scratch_page(vault)
    # Build the projection first, so the withheld run is the serve and not the
    # recompute — a recompute with everything withheld would be vacuous.
    due_state_module.reconcile(vault)

    monkeypatch.setattr(
        egress_module,
        "release_walk_filter",
        lambda *args, **kwargs: (lambda _path: False),
    )
    assert due_state_module.served_entries(vault) == []
    assert _ledger(vault) == {}


def test_an_unwritable_ledger_does_not_change_the_surface(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report's items, order, counts and states are what they always were.

    Not asserted byte-for-byte: the stamp itself is a wall clock and a store
    that cannot be written cannot carry one forward, so the honest invariant is
    that everything the reader ACTS on is unchanged and the request neither
    fails nor slows.
    """
    overdue_prediction(vault, "nag-one")
    overdue_prediction(vault, "nag-two")
    scratch_page(vault)

    healthy = commands.op_attention(vault, limit=0)

    def _refuse(*args, **kwargs):
        raise OSError("read-only store")

    monkeypatch.setattr(review_state.ReviewStateStore, "_write", _refuse)
    broken = commands.op_attention(vault, limit=0)

    def _skeleton(report: dict) -> str:
        return json.dumps(
            [
                {
                    key: item[key]
                    for key in ("path", "item_id", "fingerprint", "state", "categories")
                }
                for item in report["items"]
            ],
            sort_keys=True,
        )

    assert _skeleton(broken) == _skeleton(healthy)
    assert broken["total"] == healthy["total"]
    assert broken["shown"] == healthy["shown"]
    assert broken["note"] == healthy["note"]


def test_the_ledger_is_never_backfilled(vault: Path) -> None:
    """A signal that existed before the ledger did is stamped when it is next
    surfaced, not retroactively at the moment it was authored."""
    overdue_prediction(vault)
    scratch_page(vault)
    assert _ledger(vault) == {}

    items = commands.op_attention(vault, limit=0)["items"]
    stamps = {item["first_surfaced_at"] for item in items}
    assert all(stamp.endswith("Z") for stamp in stamps)


# ==========================================================================
# mechanism removal
# ==========================================================================


def test_removing_the_recorder_removes_the_stamp(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    assert all(
        "first_surfaced_at" in item
        for item in commands.op_attention(vault, limit=0)["items"]
    )

    monkeypatch.setattr(
        attention_module.review_state_module,
        "record_surfaced",
        lambda *args, **kwargs: {},
    )
    fresh = commands.op_attention(vault, limit=0)["items"]
    assert all("first_surfaced_at" not in item for item in fresh)
