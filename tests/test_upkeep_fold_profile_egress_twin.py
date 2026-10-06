"""Fold and profile upkeep: a withheld page equals an absent one.

Two vaults under the same governed policy. In the first, a recap from a third
episode and a referrer of a third origin are withheld from an external caller;
in the second they do not exist. The owner's views differ (three origins
against two), yet everything that caller can observe through item, context,
triage, the explicit list and the activation carrier is identical, served
fingerprints and counts included. Editing only a withheld member, which moves
the owner row's stored settle clock, changes nothing the caller sees either,
delivery timing and order included.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import dreamer_fixture as fx
import pytest
from test_governance_egress import _external
from test_upkeep_vocabulary_egress_twin import (
    LATER,
    _carry,
    _Clock,
    _cmd,
    _govern,
    _outcome,
    _reset,
    _reset_caches,
)

from exomem import dreamer, dreamer_families, dreamer_store, freshness, upkeep
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.writer_lease import invoke_command

REFS = {
    kind: upkeep.upkeep_ref(dreamer_store.candidate_id(kind, fx.ENTITY, ""))
    for kind in (dreamer_families.FOLD_KIND, dreamer_families.PROFILE_KIND)
}
WITHHELD = {
    f"{fx.EPISODES}/aa-restricted-session.md": fx.recap(
        "Restricted session",
        episode="ep-" + "c3" * 16,
        captured="2026-05-04",
        decided="Retire the [[Orbit Pump]] next year",
    ),
    f"{fx.KB}/Notes/A-Restricted/test-rig.md": fx.entity()
    .replace("title: Orbit Pump", "title: Test Rig")
    .replace("A circulation pump used in the test rig.", "Runs the [[Orbit Pump]]."),
}


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


def _build(root: Path, extra: dict[str, str]) -> Path:
    vault = fx.build(root, with_graph=False)
    for episode, day in (("ep-" + "a1" * 16, "02"), ("ep-" + "b2" * 16, "03")):
        fx.write(vault, *fx.entity_recap(episode, day))
    for rel, text in extra.items():
        fx.write(vault, rel, text)
    _govern(vault, ["Sources/Episodes/aa-restricted-*", "Notes/A-Restricted/**"])
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    _reset_caches()
    return vault


def _observe(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Everything an external caller can see, in a fixed order of calls."""
    # Building the other twin cleared this vault's freshness map: seed it again,
    # as the running service's watcher keeps it.
    freshness.clear()
    fx.seed(vault)
    _reset_caches()
    clock = _Clock()
    monkeypatch.setattr(upkeep, "_monotonic", lambda: clock.mono)
    monkeypatch.setattr(upkeep, "_wall", lambda: clock.wall)
    monkeypatch.setattr(dreamer, "delivering", lambda: True)
    seen: dict[str, object] = {}
    with request_scope(_external()):
        seen["review"] = _outcome(lambda: upkeep.review(vault, limit=50))
        for kind, ref in REFS.items():
            seen[f"item:{kind}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_memory"), vault, mode="item", ref=ref)
            )
            seen[f"context:{kind}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_item_context"), vault, ref=ref)
            )
        seen["carrier"] = _carry(vault, clock, 4, "before")
        for kind, ref in REFS.items():
            seen[f"triage:{kind}"] = _outcome(
                lambda ref=ref: invoke_command(
                    _cmd("triage_memory"), vault, ref=ref, action="dismiss", why="handled: probe"
                )
            )
        clock.advance(8 * 86400)
        seen["carrier_after"] = _carry(vault, clock, 4, "after")
        seen["review_after"] = _outcome(lambda: upkeep.review(vault, state="all", limit=50))
    return seen


#: An edit to one withheld member only: its text changes, its links do not.
EDITS = {
    "recap": (f"{fx.EPISODES}/aa-restricted-session.md", "next year", "next quarter"),
    "referrer": (f"{fx.KB}/Notes/A-Restricted/test-rig.md", "Runs the", "Drives the"),
}


@pytest.mark.parametrize("edited", [None, *EDITS])
def test_a_withheld_recap_or_referrer_equals_an_absent_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edited: str | None
) -> None:
    withheld = _build(tmp_path / "withheld", WITHHELD)
    absent = _build(tmp_path / "absent", {})
    if edited is not None:
        rel, old, new = EDITS[edited]
        fx.edit(withheld, rel, WITHHELD[rel].replace(old, new))
        for vault in (withheld, absent):
            results = fx.run_to_quiet(vault, now=LATER + 7200)
            assert all(result.stop_reason != "error" for result in results), results
    # Not vacuous: the owner's view of the withheld twin counts the third origin.
    owner = {
        row["kind"]: row["measures"]
        for row in dreamer_store.read_view(withheld).candidates
        if row["kind"] in REFS
    }
    assert owner == {
        dreamer_families.FOLD_KIND: {"episodes": 3},
        dreamer_families.PROFILE_KIND: {"origins": 3, "referrers": 3},
    }
    left = _observe(withheld, monkeypatch)
    right = _observe(absent, monkeypatch)
    assert left == right
    # Not vacuous either: the caller is served both items and the carrier offers them.
    for kind in REFS:
        assert right[f"item:{kind}"][0] == "ok", right[f"item:{kind}"]
    offered = {
        item["kind"] for block in right["carrier"] if block for item in block.get("items") or ()
    }
    assert set(REFS) <= offered, offered


def test_a_withheld_source_that_merges_two_origins_reads_as_absent(tmp_path: Path) -> None:
    """Referrers declare {S1, SX}, {S2, SX} and {S3}. SX merges the first two,
    so the owner counts two origins; a caller who may not see SX counts three."""
    shared = f"{fx.KB}/Sources/A-Restricted/shared-batch.md"
    vault = fx.build(tmp_path, with_graph=False)
    fx.write(vault, shared, fx.source("Shared batch"))
    for rel, title, sources in (
        (fx.CAVITATION, "Pump cavitation", ["field-report-one", "A-Restricted/shared-batch"]),
        (fx.SEAL_WEAR, "Pump seal wear", ["field-report-two", "A-Restricted/shared-batch"]),
        (f"{fx.KB}/Notes/Insights/pump-noise.md", "Pump noise", ["field-report-three"]),
    ):
        fx.write(
            vault,
            rel,
            fx.insight(
                title,
                sources=sources,
                updated="2026-05-02",
                links="Seen on the [[Notes/Entities/orbit-pump]].",
            ),
        )
    _govern(vault, ["Sources/A-Restricted/**"])
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    _reset_caches()

    def profile_item() -> dict:
        items = upkeep.review(vault, limit=50)["items"]
        return next(item for item in items if item["kind"] == dreamer_families.PROFILE_KIND)

    with request_scope(owner_principal()):
        owner = profile_item()
    _reset_caches()
    with request_scope(_external()):
        caller = profile_item()
    assert owner["why"].startswith("pages from 2 independent sources"), owner["why"]
    assert caller["why"].startswith("pages from 3 independent sources"), caller["why"]
    # The owner is served the stored row, so its dismissals and the delivery
    # ledger bind to the fingerprint the worker stored.
    stored = next(
        row
        for row in dreamer_store.read_view(vault).candidates
        if row["kind"] == dreamer_families.PROFILE_KIND
    )
    assert owner["fingerprint"] == stored["fingerprint"]
    assert caller["fingerprint"] != stored["fingerprint"]


def test_an_owner_session_start_recomputes_no_fold_or_profile_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing can be withheld from the owner, so its session start is served the
    stored fold and profile rows; only a restricted caller has them recomputed.
    The request-path ceiling suite plants a hydration row only, so it cannot
    see this cost."""
    # Two recaps and two referrers of two origins link the entity: one fold and
    # one profile row, under a policy that withholds a folder neither row uses.
    vault = _build(tmp_path, {})
    recomputed: list[str] = []

    def counting(family: dreamer_families.Family) -> dreamer_families.Family:
        def release(ctx, row, keep):
            recomputed.append(row["kind"])
            return family.release(ctx, row, keep)

        return dataclasses.replace(family, release=release)

    monkeypatch.setattr(
        dreamer_families,
        "REGISTRY",
        [
            counting(family)
            if family in (dreamer_families.FOLD, dreamer_families.PROFILE)
            else family
            for family in dreamer_families.REGISTRY
        ],
    )
    monkeypatch.setattr(dreamer, "delivering", lambda: True)
    monkeypatch.setattr(upkeep, "_wall", lambda: LATER + 7200)

    def session_start(principal, session: str) -> list[str]:
        packet = {
            "recent_context": [{"path": fx.ENTITY, "title": "Orbit Pump", "why": "edited"}],
            "anchors": [],
            "budget": {"limit_chars": 4000, "used_chars": 0},
            "abstention": {"reason": "unresolved"},
        }
        with request_scope(principal):
            upkeep.for_packet(vault, packet, session=session)
        return [item["kind"] for item in (packet.get("upkeep") or {}).get("items") or ()]

    owner_items = session_start(owner_principal(), "owner")
    owner_recomputed = list(recomputed)
    recomputed.clear()
    _reset_caches()
    caller_items = session_start(_external(), "external")

    assert owner_recomputed == []
    assert sorted(recomputed) == sorted([dreamer_families.FOLD_KIND, dreamer_families.PROFILE_KIND])
    assert owner_items == caller_items == [dreamer_families.FOLD_KIND]


def test_a_remote_owner_session_start_recomputes_no_fold_or_profile_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner ruling: the owner is the owner on any surface, so the owner's OAuth
    connector is served the stored fold and profile rows at session start, as
    the local owner is, and recomputes neither. A guest, the positive control,
    still has both recomputed."""
    vault = _build(tmp_path, {})
    recomputed: list[str] = []

    def counting(family: dreamer_families.Family) -> dreamer_families.Family:
        def release(ctx, row, keep):
            recomputed.append(row["kind"])
            return family.release(ctx, row, keep)

        return dataclasses.replace(family, release=release)

    monkeypatch.setattr(
        dreamer_families,
        "REGISTRY",
        [
            counting(family)
            if family in (dreamer_families.FOLD, dreamer_families.PROFILE)
            else family
            for family in dreamer_families.REGISTRY
        ],
    )
    monkeypatch.setattr(dreamer, "delivering", lambda: True)
    monkeypatch.setattr(upkeep, "_wall", lambda: LATER + 7200)

    def session_start(principal, session: str) -> list[str]:
        packet = {
            "recent_context": [{"path": fx.ENTITY, "title": "Orbit Pump", "why": "edited"}],
            "anchors": [],
            "budget": {"limit_chars": 4000, "used_chars": 0},
            "abstention": {"reason": "unresolved"},
        }
        with request_scope(principal):
            upkeep.for_packet(vault, packet, session=session)
        return [item["kind"] for item in (packet.get("upkeep") or {}).get("items") or ()]

    connector = RequestPrincipal(
        audience_id="owner", surface="mcp", resolved=True,
        issuer_family="mcp-oauth:" + "c" * 64, remote_owner=True,
    )
    owner_items = session_start(owner_principal(), "owner")
    _reset_caches()
    connector_items = session_start(connector, "connector")
    connector_recomputed = list(recomputed)
    _reset_caches()
    session_start(_external(), "external")

    assert connector_recomputed == []
    assert connector_items == owner_items == [dreamer_families.FOLD_KIND]
    assert sorted(recomputed) == sorted([dreamer_families.FOLD_KIND, dreamer_families.PROFILE_KIND])
