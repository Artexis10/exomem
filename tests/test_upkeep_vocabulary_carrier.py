"""Alias and convention items ride the session-start carrier like every family.

The carrier, its caps and its delivery ledger are unchanged (design §8). What
is new is that these items are recomputed per request from the sidecar's page
contributions, so the carrier serves and records the served fingerprint.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import commands, dreamer, dreamer_families, dreamer_store, freshness, upkeep

LATER = time.time() + 3 * 3600
MINUTE = 60.0
DAY = 86400.0


class _Clock:
    def __init__(self) -> None:
        self.mono = 1_000_000.0
        self.wall = LATER

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(upkeep, "_monotonic", lambda: clock.mono)
    monkeypatch.setattr(upkeep, "_wall", lambda: clock.wall)
    monkeypatch.setattr(dreamer, "delivering", lambda: True)
    return clock


def _ready(tmp_path: Path) -> Path:
    vault = fx.build_vocabulary(tmp_path)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    return vault


def _items(packet: dict) -> list[dict]:
    return list((packet.get("upkeep") or {}).get("items") or [])


def _carry(vault: Path, session: str) -> list[dict]:
    packet = {
        "recent_context": [],
        "anchors": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "abstention": {"reason": "unresolved"},
    }
    upkeep.for_packet(vault, packet, session=session)
    return _items(packet)


def test_alias_then_convention_items_arrive_at_successive_session_starts(
    tmp_path: Path, clock: _Clock
) -> None:
    vault = _ready(tmp_path)
    packet = commands.op_activate_context(vault, turn="how is the orbit pump doing")
    [first] = _items(packet)
    # Naming ranks before conventions.
    assert first["family"] == dreamer_families.ALIAS_FAMILY
    assert first["kind"] == "anchor.alias"
    assert first["permission"] == dreamer_families.PERMISSION
    assert len(upkeep.item_text(first)) <= upkeep.MAX_ITEM_CHARS
    kinds = []
    for session in ("two", "three"):
        clock.advance(11 * MINUTE)
        [item] = _carry(vault, session)
        kinds.append(item["kind"])
    assert sorted(kinds) == ["convention.category", "convention.tag"]
    clock.advance(11 * MINUTE)
    assert _carry(vault, "four") == []


def test_the_carrier_records_the_served_fingerprint(tmp_path: Path, clock: _Clock) -> None:
    vault = _ready(tmp_path)
    [item] = _carry(vault, "one")
    cid = item["ref"].rsplit("/", 1)[-1]
    assert upkeep.pending_deliveries()[0][:2] == (cid, item["fingerprint"])
    # Recorded by the worker, it counts: a week later another session gets it
    # a second time, and says so.
    results = fx.run_to_quiet(vault, now=clock.wall)
    assert all(result.stop_reason != "error" for result in results), results
    clock.advance(8 * DAY)
    again = [entry for entry in _carry(vault, "two") if entry["ref"] == item["ref"]]
    assert again and again[0]["disposition"]["delivered_before"] == 1


def test_a_dismissed_item_is_not_delivered(tmp_path: Path, clock: _Clock) -> None:
    vault = _ready(tmp_path)
    ref = upkeep.upkeep_ref(dreamer_store.candidate_id("anchor.alias", fx.ENTITY, fx.VARIANT_KEY))
    commands.op_triage_memory(vault, ref=ref, action="dismiss", why="handled: a nickname")
    delivered = []
    for session in ("one", "two", "three"):
        delivered.extend(item["ref"] for item in _carry(vault, session))
        clock.advance(11 * MINUTE)
    assert ref not in delivered
    assert len(delivered) == 2
