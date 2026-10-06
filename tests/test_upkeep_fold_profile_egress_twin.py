"""Fold and profile upkeep: a withheld page equals an absent one.

Two vaults under the same governed policy. In the first, a recap from a third
episode and a referrer of a third origin are withheld from an external caller;
in the second they do not exist. The owner's views differ (three origins
against two), yet everything that caller can observe through item, context,
triage, the explicit list and the activation carrier is identical, served
fingerprints and counts included.
"""

from __future__ import annotations

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
from exomem.governance.principal import request_scope
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


def test_a_withheld_recap_or_referrer_equals_an_absent_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    withheld = _build(tmp_path / "withheld", WITHHELD)
    absent = _build(tmp_path / "absent", {})
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
