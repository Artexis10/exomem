"""The first-surfaced ledger: when a signal first reached a served surface.

The third capture primitive. Nag rate and recovery fraction are computable only
against the moment a signal was first *shown to somebody*, which the runtime has
never recorded. The ledger starts empty, is never backfilled, never records
anything egress withheld or a disposition removed, is never written by audit
measurement, and is failure-isolated from the read that populates it.

The first test is the GAP PROOF: today no surface stamps anything.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from _nag_governance_helpers import (
    SCRATCH,
    advisory_candidate,
    overdue_prediction,
    scratch_page,
    seed_page,
)

from exomem import attention as attention_module
from exomem import commands, corpus_aware, review_state

FAMILY_REF = "exomem://review/family/prediction_window"


def _parsed(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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


def test_a_delivered_carrier_block_stamps_the_carrier_surface(vault: Path) -> None:
    """PRODUCING a block is not surfacing it; DELIVERING it is.

    The stamp lives with `mark_emitted`, the one place a block is recorded as
    handed over, because everything between production and delivery can drop it:
    a batch scope, the change-only governor, a `legacy` detail level, terminal
    validation. Stamping at production recorded a first surfacing for references
    nobody was ever shown.
    """
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reset_emission_state()

    block = due_state_module.served(vault)
    assert block
    assert _ledger(vault) == {}, "production alone must record nothing"

    assert due_state_module.should_emit(block, vault_root=vault)
    ledger = _ledger(vault)
    assert ledger
    assert {row["surface"] for row in ledger.values()} == {"carrier"}


def test_a_block_produced_inside_a_batch_scope_is_never_stamped(vault: Path) -> None:
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reset_emission_state()

    with due_state_module.batch_scope(vault):
        block = due_state_module.served(vault)
        assert block
        assert not due_state_module.should_emit(block, vault_root=vault)
    assert _ledger(vault) == {}


def test_a_resurfacing_signal_keeps_its_original_stamp(vault: Path) -> None:
    """An IDEMPOTENCE pin, which is all this can be — named as such.

    It was written as "the governor keeps the second block quiet, so nothing is
    stamped", and that framing cannot fail: the ledger records a FIRST
    surfacing, so a second delivery of the same refs would leave it byte-
    identical too. The stamp not moving is therefore evidence about the ledger's
    idempotence, not about the governor, and the honest thing is to assert the
    property that actually holds.

    What it pins is worth pinning: a signal that keeps coming back keeps the
    date it was first shown. A ledger that re-stamped on every surfacing would
    make `first_surfaced_at` mean "last seen", and every age computed from it
    would read zero forever.
    """
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reset_emission_state()

    block = due_state_module.served(vault)
    assert due_state_module.should_emit(block, vault_root=vault)
    first = dict(_ledger(vault))
    assert first

    # The governor declines the identical second block...
    assert not due_state_module.should_emit(
        due_state_module.served(vault), vault_root=vault
    )
    assert _ledger(vault) == first
    # ...and so does the ledger, when the delivery is forced past the governor.
    due_state_module.reset_emission_state()
    assert due_state_module.should_emit(due_state_module.served(vault), vault_root=vault)
    assert _ledger(vault) == first, "a resurfacing re-stamped its first-surfaced date"


def test_a_legacy_response_that_strips_the_block_never_stamps(vault: Path) -> None:
    """`legacy` drops the block at the terminal, so nothing was surfaced.

    Driven through `project_terminal` at both detail levels on the same leaf,
    because the earlier version of this test asserted `hasattr(...)` and an
    empty ledger — which is true of a test that does nothing at all. The A/B is
    what makes it a claim: same block, same vault, one detail level delivers and
    stamps, the other returns the bare leaf and stamps nothing.
    """
    from exomem import due_state as due_state_module
    from exomem import mutation_terminal

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reset_emission_state()

    block = due_state_module.served(vault)
    assert block and block["total"] >= 1

    def terminal() -> dict:
        return mutation_terminal.committed_terminal(
            # `_vault` rides INSIDE the block: that is where the terminal
            # reads it from, and it is a server-internal key the projection
            # strips before the block reaches the wire.
            {"path": SCRATCH, "due_state": {**block, "_vault": str(vault)}},
            request_id="request",
            receipt_id="receipt",
            idempotency_key=None,
        )

    legacy = mutation_terminal.project_terminal(terminal(), detail="legacy")
    assert "due_state" not in legacy, legacy
    assert _ledger(vault) == {}, "a legacy response stamped the ledger"

    compact = mutation_terminal.project_terminal(terminal(), detail="compact")
    assert compact["due_state"]["total"] == block["total"], compact
    stamped = _ledger(vault)
    assert stamped, "the delivered block did not stamp the ledger"
    assert {row["surface"] for row in stamped.values()} == {"carrier"}


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


# ==========================================================================
# the general invariant: a surface that shows nothing records nothing
# ==========================================================================


def _dispositions_view(vault: Path) -> None:
    commands.op_review_memory(vault, mode="dispositions")


def _resolve_every_reference(vault: Path) -> None:
    for ref in _LISTED_REFS:
        commands.op_review_item_context(vault, ref=ref)


def _fallback_lookup(vault: Path) -> None:
    for ref in _LISTED_REFS:
        attention_module._item_by_ref_fallback(
            vault, review_state.parse_review_ref(ref)
        )


#: Filled by the fixture below, so each lookup asks for references that exist.
_LISTED_REFS: list[str] = []


@pytest.mark.parametrize(
    "lookup",
    [_dispositions_view, _resolve_every_reference, _fallback_lookup],
    ids=["dispositions-view", "item-by-ref", "fallback-lookup"],
)
def test_a_surface_that_returns_no_items_records_nothing(vault: Path, lookup) -> None:
    """The general invariant, not one caller's version of it.

    Three call sites resolve or count by running the whole fusion at
    ``limit=0, state="all"`` and then reading one thing out of the result. None
    of them SHOWS a review surface, so none of them may stamp one — and the
    ledger measures what reached a person, so a stamp from any of them is a
    false measurement, not a harmless extra row. Each was written separately and
    the flag has to be remembered at each; the invariant is what catches the
    next one.

    The store is deleted after the listing that populates ``_LISTED_REFS``, so
    the assertion cannot be satisfied by rows the listing itself had recorded.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    listed = commands.op_attention(vault, limit=0)["items"]
    assert listed
    _LISTED_REFS[:] = [item["ref"] for item in listed]
    review_state.state_path(vault).unlink()

    lookup(vault)

    assert not review_state.state_path(vault).exists() or _ledger(vault) == {}


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


def test_a_withheld_page_is_never_stamped_by_a_listing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attention consults egress ITSELF, because it is not the boundary.

    The governance plane projects attention's payload after it returns, so at
    the moment the ledger is written nothing has yet decided what this audience
    may see. Without `_egress_keep` the ledger would record a first surfacing
    for a page the requesting audience is about to be told nothing about — a
    row that both leaks the page's existence into a durable file and records a
    surfacing that never happened.
    """
    from exomem.governance import egress as egress_module

    overdue_prediction(vault)
    scratch_page(vault)
    monkeypatch.setattr(
        egress_module,
        "release_walk_filter",
        lambda *args, **kwargs: (lambda _path: False),
    )

    commands.op_attention(vault, limit=0)

    assert _ledger(vault) == {}


def test_removing_the_egress_consult_records_the_withheld_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism-removal pair for `_egress_keep`."""
    from exomem.governance import egress as egress_module

    overdue_prediction(vault)
    scratch_page(vault)
    monkeypatch.setattr(
        egress_module,
        "release_walk_filter",
        lambda *args, **kwargs: (lambda _path: False),
    )
    commands.op_attention(vault, limit=0)
    assert _ledger(vault) == {}

    # `None` is `_stamp_first_surfaced`'s "no filter, keep everything" value, so
    # this is exactly the code with the consult taken out.
    monkeypatch.setattr(attention_module, "_egress_keep", lambda _vault: None)
    commands.op_attention(vault, limit=0)
    assert _ledger(vault)


def test_an_undecidable_release_plane_records_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail CLOSED: a ledger row is cheap to miss and impossible to unsee."""
    from exomem.governance import egress as egress_module

    overdue_prediction(vault)
    scratch_page(vault)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("release plane unavailable")

    monkeypatch.setattr(egress_module, "release_walk_filter", unavailable)

    listed = commands.op_attention(vault, limit=0)["items"]
    assert listed, "the surface still answers; only the ledger is withheld"
    assert _ledger(vault) == {}


def _govern(vault: Path) -> None:
    """A real (permissive) release policy, so the egress filter actually runs.

    Without a policy `release_walk_filter` takes its empty-policy fast path and
    records nothing, which would make any receipt assertion vacuous.
    """
    from exomem.governance import egress as egress_module, membership, policy

    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Patterns\n"
        'paths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "notes-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 3\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress_module.clear_decision_memo()


def test_the_ledgers_egress_consult_stays_out_of_an_enclosing_receipt(
    vault: Path,
) -> None:
    """The ledger's own disclosure boundary, measured as an A/B.

    `release_walk_filter` records one decision per page it judges, so consulting
    it inside somebody else's collector attaches every item on the review
    surface to THEIR receipt. A one-page write whose receipt lists twelve pages
    it never touched is a false governance record, which is worse than a missing
    one. `due_state.block_for_write` already opens its own boundary for exactly
    this; the ledger now does too.

    The A/B is the same shape as the carrier's: the enclosing receipt must be
    the same size whether the surface inside it had items to stamp or not.
    """
    from exomem.governance import egress as egress_module
    from exomem.governance.principal import owner_principal, request_scope

    _govern(vault)
    assert egress_module.release_walk_filter(vault) is not None, (
        "an ungoverned vault takes the empty-policy fast path and records nothing, "
        "which would make this assertion vacuous"
    )

    with request_scope(owner_principal(surface="cli")):
        with egress_module.disclosure_boundary(vault, "caller") as light:
            few = commands.op_attention(vault, limit=0)["items"]
            before = len(light.outcomes)
        assert few, "the light leg must still stamp something"
        light_rows = len(_ledger(vault))

        for slug in ("nag-one", "nag-two", "nag-three", "nag-four", "nag-five"):
            overdue_prediction(vault, slug)
        scratch_page(vault)
        with egress_module.disclosure_boundary(vault, "caller") as heavy:
            many = commands.op_attention(vault, limit=0)["items"]
            after = len(heavy.outcomes)

    assert len(many) > len(few), "the heavy leg must judge strictly more pages"
    assert len(_ledger(vault)) > light_rows, "and must actually have stamped them"
    assert after == before, (
        f"the caller's receipt grew from {before} to {after} outcomes because the "
        "ledger's egress consult joined its collector"
    )


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
    """A signal older than the ledger is stamped NOW, not at its authored date.

    The assertion is the two bounds that make "not backfilled" mean something:
    the stamp is inside the window of the call that produced it, and it is
    strictly later than the page's own `created` date. Asserting the stamp ends
    in `Z` — which every stamp this module can produce does — would pass against
    a backfill just as happily.
    """
    authored = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    overdue_prediction(vault)
    scratch_page(vault)
    assert _ledger(vault) == {}

    before = dt.datetime.now(dt.UTC)
    items = commands.op_attention(vault, limit=0)["items"]
    after = dt.datetime.now(dt.UTC)

    stamps = [_parsed(item["first_surfaced_at"]) for item in items]
    assert stamps
    for stamp in stamps:
        assert before - dt.timedelta(seconds=5) <= stamp <= after + dt.timedelta(seconds=5), stamp
        assert stamp > authored, stamp


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


# ==========================================================================
# the carrier's ref -> fingerprint derivation, and the coupling it rests on
# ==========================================================================


def test_a_ref_carrying_two_fingerprints_stamps_neither(vault: Path) -> None:
    """A collision is dropped, not resolved by whichever row came first.

    The wire block carries refs; the ledger keys on fingerprints; the carrier
    bridges them by reading `ref -> fingerprint` off the projection. That map is
    only well defined because every projection category except
    `unfinished_experiments` stamps a `review_partition` into its ref, which
    makes the ref per-finding rather than per-page. Nothing enforces that — it
    is a property of how refs are composed — so a future category that omits
    the partition would make the map ambiguous.

    First-category-wins would then stamp a real identity with the wrong
    fingerprint, which is a false record in the one ledger whose entire purpose
    is to say what a person was shown. Dropping the ref leaves a measurement
    gap instead, which is recoverable and honest.
    """
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reconcile(vault)

    projection = due_state_module.load(vault)
    rows = [
        entry
        for pages in (projection.get("categories") or {}).values()
        for entries in pages.values()
        for entry in due_state_module._unbucket(entries)
        if entry.get("ref") and entry.get("fingerprint")
    ]
    assert rows, projection
    ref = rows[0]["ref"]
    finger = rows[0]["fingerprint"]
    assert due_state_module._fingerprints_by_ref(projection)[ref] == finger

    # The same ref appearing again under a DIFFERENT fingerprint.
    collided = {
        **projection,
        "categories": {
            **projection["categories"],
            "unfinished_experiments": {
                "Knowledge Base/Notes/Experiments/collide.md": {
                    "open": [
                        {
                            "ref": ref,
                            "fingerprint": "0" * 24,
                            "due_since": rows[0].get("due_since") or "2026-01-01",
                            "path": "Knowledge Base/Notes/Experiments/collide.md",
                        }
                    ]
                }
            },
        },
    }

    assert ref not in due_state_module._fingerprints_by_ref(collided)


def test_a_delivered_block_whose_refs_resolve_to_nothing_stamps_nothing(
    vault: Path,
) -> None:
    """A ref the map cannot resolve is a measurement gap, never a wrong stamp.

    This is the CONSEQUENCE of the guard above, not the guard itself: it empties
    the map rather than colliding anything, so what it pins is that the carrier
    treats an unresolvable ref as "do not stamp" instead of falling back to some
    other identity. The collision itself is
    `test_a_ref_carrying_two_fingerprints_stamps_neither`, which is where the
    dropping happens; between them they cover both halves — the map drops a
    conflicted ref, and the carrier does nothing with a ref the map has no
    answer for.
    """
    from exomem import due_state as due_state_module

    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reconcile(vault)
    block = due_state_module.served(vault)
    assert block

    real = due_state_module._fingerprints_by_ref
    due_state_module._fingerprints_by_ref = lambda _payload: {}
    try:
        due_state_module.reset_emission_state()
        assert due_state_module.should_emit(block, vault_root=vault)
    finally:
        due_state_module._fingerprints_by_ref = real

    assert _ledger(vault) == {}
