"""D1-T11: the activation carrier.

At a caller's session start, an activation packet may carry one upkeep item.
The rule is in memory: a caller's key (U6's `session` when sent, else the
capture sweep's ledger key) is at a session start when it is new or has been
quiet for 30 minutes. Across all keys a vault gets at most one delivery per 10
minutes, a key at most 3 per UTC day, and one `(id, fingerprint)` at most two
deliveries, the second a week later to another session; then it is held.

The carrier reads the sidecar read-only, never waits on it, passes every item
through egress, and attaches nothing on any error.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import dreamer_fixture as fx
import pytest
from test_governance_egress import SCOPE_ID, _external, write_rule

from exomem import (
    capture_sweep,
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    envelope,
    freshness,
    upkeep,
    working_set,
)
from exomem.governance import egress, membership, policy
from exomem.governance.principal import request_scope

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
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(upkeep, "_monotonic", lambda: clock.mono)
    monkeypatch.setattr(upkeep, "_wall", lambda: clock.wall)
    return clock


@pytest.fixture
def serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process serves the worker: the carrier is live."""
    monkeypatch.setattr(dreamer, "delivering", lambda: True)


def _quiet(vault: Path, now: float) -> None:
    results = fx.run_to_quiet(vault, now=now)
    assert all(result.stop_reason != "error" for result in results), results


def _ready(tmp_path: Path) -> Path:
    vault = fx.build(tmp_path)
    _quiet(vault, time.time())
    _quiet(vault, LATER)
    return vault


def _packet(*, limit: int = 4000, used: int = 0, recent: tuple[str, ...] = ()) -> dict:
    return {
        "recent_context": [{"path": path, "title": "t", "why": "edited"} for path in recent],
        "anchors": [],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": limit, "used_chars": used},
        "generation": {},
        "abstained": True,
        "abstention": {"reason": "unresolved"},
    }


def _carry(vault: Path, *, session: str | None = None, packet: dict | None = None) -> dict:
    packet = _packet() if packet is None else packet
    upkeep.for_packet(vault, packet, session=session)
    return packet


def _items(packet: dict) -> list[dict]:
    return list((packet.get("upkeep") or {}).get("items") or [])


def _family(packet: dict) -> str | None:
    items = _items(packet)
    return items[0]["family"] if items else None


def test_first_activation_of_a_session_carries_one_item(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    packet = commands.op_activate_context(vault, turn="how is the orbit pump doing")
    items = _items(packet)
    assert len(items) == 1
    item = items[0]
    # Hydration ranks before link.
    assert item["family"] == dreamer_families.HYDRATION_FAMILY
    assert item["ref"].startswith(upkeep.UPKEEP_PREFIX)
    assert item["permission"] == dreamer_families.PERMISSION
    assert set(item) >= {
        "ref",
        "family",
        "fingerprint",
        "kind",
        "label",
        "subject",
        "evidence",
        "evidence_count",
        "why",
        "disposition",
        "route",
        "context_route",
        "dispose",
    }
    assert len(upkeep.item_text(item)) <= upkeep.MAX_ITEM_CHARS


def test_second_activation_in_a_session_carries_none(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    first = commands.op_activate_context(vault, turn="how is the orbit pump doing")
    assert _items(first)
    clock.advance(20 * MINUTE)
    second = commands.op_activate_context(vault, turn="and the seals?")
    assert "upkeep" not in second


def test_hook_door_and_connector_in_one_session_get_one_item_between_them(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    hook = _carry(vault, session="hook-conversation")
    assert _items(hook)
    # The agent in the same conversation calls the connector as a principal.
    monkeypatch.setattr(
        capture_sweep, "ledger_key", lambda root=None: ("principal:abc", "chatgpt", str(root))
    )
    clock.advance(2 * MINUTE)
    connector = _carry(vault)
    assert "upkeep" not in connector
    # The vault-wide spacing held it back. Past the spacing the connector is
    # mid-session: its first activation was its session start.
    clock.advance(11 * MINUTE)
    assert "upkeep" not in _carry(vault)


def test_thirty_minutes_of_quiet_is_a_new_session(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    assert _family(_carry(vault)) == dreamer_families.HYDRATION_FAMILY
    clock.advance(20 * MINUTE)
    assert "upkeep" not in _carry(vault)
    clock.advance(29 * MINUTE)
    assert "upkeep" not in _carry(vault)
    clock.advance(capture_sweep.QUIET_SECONDS)
    # A new session: the hydration item was delivered once already, less than
    # a week ago, so the link is offered.
    assert _family(_carry(vault)) == dreamer_families.LINK_FAMILY


def test_u6_session_key_separates_two_hook_sessions(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    assert "session" in commands.op_activate_context.__code__.co_varnames, "U6 is required"
    vault = _ready(tmp_path)
    assert _family(_carry(vault, session="conversation-a")) == dreamer_families.HYDRATION_FAMILY
    clock.advance(11 * MINUTE)
    # Session a is mid-session; session b starts now, past the vault spacing.
    assert "upkeep" not in _carry(vault, session="conversation-a")
    assert _family(_carry(vault, session="conversation-b")) == dreamer_families.LINK_FAMILY


def test_a_session_string_is_bound_to_the_principal_that_sends_it(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that sends another principal's session string opens only its
    own session: the other principal's session start still receives."""
    vault = _ready(tmp_path)

    def as_principal(name: str) -> None:
        monkeypatch.setattr(
            capture_sweep,
            "ledger_key",
            lambda root=None, _name=name: (f"principal:{_name}", "chatgpt", str(root)),
        )

    as_principal("a")
    assert upkeep.delivery_key(vault, "session-of-b") is not None
    a_key = upkeep.delivery_key(vault, "session-of-b")
    as_principal("b")
    assert upkeep.delivery_key(vault, "session-of-b") != a_key
    # A sends B's session string first.
    as_principal("a")
    _carry(vault, session="session-of-b")
    clock.advance(11 * MINUTE)
    # B's own first activation of that session is still a session start.
    as_principal("b")
    assert _items(_carry(vault, session="session-of-b"))


def test_unkeyable_stateless_http_is_never_pushed(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    monkeypatch.setattr(capture_sweep, "ledger_key", lambda root=None: None)
    assert "upkeep" not in _carry(vault)
    clock.advance(DAY)
    assert "upkeep" not in _carry(vault)
    # The explicit list still has it.
    assert upkeep.review(vault)["items"]


def test_structural_suggestions_off_suppresses_items(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    real_active = envelope.active
    monkeypatch.setattr(
        envelope,
        "active",
        lambda level=None: {**real_active(level), "structural_suggestions": "off"},
    )
    assert "upkeep" not in _carry(vault)


def test_recent_context_overlap_is_offered_first(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    # Without overlap hydration ranks first; the recent page makes the link first.
    packet = _carry(vault, packet=_packet(recent=(fx.INLET,)))
    assert _family(packet) == dreamer_families.LINK_FAMILY


def test_item_counts_in_used_chars_and_is_omitted_whole_when_it_does_not_fit(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    packet = _carry(vault, packet=_packet(limit=4000, used=100))
    item = _items(packet)[0]
    assert packet["budget"]["used_chars"] == 100 + len(upkeep.item_text(item))

    upkeep.reset_delivery_state()
    tight = _carry(vault, packet=_packet(limit=500, used=480))
    assert "upkeep" not in tight
    assert tight["budget"]["used_chars"] == 480


def _withhold(vault: Path, rel: str) -> None:
    target = vault / "Knowledge Base" / "_Governance" / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    glob = rel.removeprefix("Knowledge Base/")
    target.write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["{glob}"]\n',
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def test_withheld_subject_is_skipped_and_nothing_leaks(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    _withhold(vault, fx.ENTITY)
    with request_scope(_external()):
        packet = _carry(vault)
    encoded = json.dumps(packet)
    assert _family(packet) == dreamer_families.LINK_FAMILY
    assert "orbit-pump" not in encoded
    assert "Orbit Pump" not in encoded


def test_stale_evidence_signature_is_never_served(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    # A contributor changes after the worker's pass: the stored signature no
    # longer matches the live one, so hydration is not served; the link is.
    fx.edit(vault, fx.SEAL_WEAR, (vault / fx.SEAL_WEAR).read_text("utf-8") + "\nA later note.\n")
    assert dreamer_store.read_view(vault) is not None
    packet = _carry(vault)
    assert _family(packet) == dreamer_families.LINK_FAMILY

    # Freshness that cannot vouch for a page serves nothing about it.
    upkeep.reset_delivery_state()
    freshness.clear()
    assert "upkeep" not in _carry(vault)


def test_spent_door_budget_skips_upkeep(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    stages: list[str] = []
    monkeypatch.setattr(
        working_set,
        "budget_exhausted",
        lambda stage, **_kw: stages.append(stage) or stage == "working_set.upkeep",
    )
    assert "upkeep" not in _carry(vault)
    assert stages == ["working_set.upkeep"]


def test_a_missing_or_locked_sidecar_attaches_nothing_and_does_not_wait(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    missing = fx.build(tmp_path / "missing")
    assert "upkeep" not in _carry(missing)

    vault = _ready(tmp_path / "locked")
    dreamer_store.clear_reader_memo()
    holder = sqlite3.connect(dreamer_store.sidecar_path(vault), timeout=0)
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        packet = _carry(vault)
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert "upkeep" not in packet
    assert waited < 1.0


def test_delivered_twice_then_held_until_the_fingerprint_changes(
    tmp_path: Path, clock: _Clock, serving: None
) -> None:
    vault = _ready(tmp_path)
    # Only the link family is delivered here.
    commands.op_triage_memory(
        vault,
        ref=upkeep.upkeep_ref(
            dreamer_store.candidate_id(dreamer_families.HYDRATION_KIND, fx.ENTITY, "")
        ),
        action="dismiss",
        why="handled: covered elsewhere",
    )
    first = _items(_carry(vault, session="one"))
    assert first and first[0]["family"] == dreamer_families.LINK_FAMILY
    assert first[0]["disposition"]["delivered_before"] == 0

    # Another session inside the week does not get it a second time.
    clock.advance(2 * DAY)
    assert "upkeep" not in _carry(vault, session="two")
    # The worker records the delivery; a week on, another session gets it once more.
    _quiet(vault, clock.wall)
    clock.advance(6 * DAY)
    second = _items(_carry(vault, session="three"))
    assert second and second[0]["ref"] == first[0]["ref"]
    assert second[0]["disposition"]["delivered_before"] == 1
    _quiet(vault, clock.wall)

    # Held: nobody gets it again while its fingerprint stands.
    clock.advance(14 * DAY)
    assert "upkeep" not in _carry(vault, session="four")

    # The evidence changes: a new fingerprint, delivered again once settled.
    for rel, title, observation in (
        (fx.CAVITATION, "Pump cavitation", "Cavitation starts above 40 litres a minute."),
        (fx.INLET, "Pump inlet pressure", "Inlet pressure falls before cavitation begins."),
    ):
        fx.edit(
            vault,
            rel,
            fx.insight(
                title, sources=["field-report-three"], updated="2026-05-06", observation=observation
            ),
            graph=False,
        )
    fx.publish_graph(vault)
    _quiet(vault, clock.wall)
    clock.advance(2 * 3600)
    _quiet(vault, clock.wall)
    again = _items(_carry(vault, session="five"))
    assert again and again[0]["ref"] == first[0]["ref"]
    assert again[0]["fingerprint"] != first[0]["fingerprint"]


def test_failed_worker_status_block_on_session_start_only(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = fx.build(tmp_path)
    monkeypatch.setattr(
        dreamer,
        "failure",
        lambda: {"since": "2026-09-20T08:00:00Z"},
    )
    first = _carry(missing)
    assert first["upkeep"] == {"status": "failed", "since": "2026-09-20T08:00:00Z"}
    clock.advance(5 * MINUTE)
    assert "upkeep" not in _carry(missing)


def test_activation_with_upkeep_never_touches_the_due_state_ledger(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import due_state

    vault = _ready(tmp_path)
    touched: list[str] = []
    for name in dir(due_state):
        if "emission" in name.lower() or name.startswith("mark_emitted"):
            original = getattr(due_state, name)
            if callable(original):
                monkeypatch.setattr(
                    due_state,
                    name,
                    lambda *a, _n=name, _o=original, **k: touched.append(_n) or _o(*a, **k),
                )
    ledger = dict(due_state._EMISSION)
    packet = commands.op_activate_context(vault, turn="how is the orbit pump doing")
    assert _items(packet)
    assert "due_state" not in packet
    assert touched == []
    assert due_state._EMISSION == ledger


def test_off_attaches_nothing_and_reads_nothing(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)
    monkeypatch.setattr(
        dreamer_store, "read_view", lambda *_a: pytest.fail("the carrier read the sidecar")
    )
    assert dreamer.delivering() is False
    assert "upkeep" not in _carry(vault)


def test_any_carrier_error_attaches_nothing(
    tmp_path: Path, clock: _Clock, serving: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _ready(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("sidecar exploded")

    monkeypatch.setattr(dreamer_store, "read_view", boom)
    packet = commands.op_activate_context(vault, turn="how is the orbit pump doing")
    assert "upkeep" not in packet
    assert packet["recent_context"] is not None


def test_policy_constants_are_the_decided_caps() -> None:
    assert upkeep.VAULT_SPACING_SECONDS == 600
    assert upkeep.PER_KEY_DAILY_CAP == 3
    assert upkeep.SECOND_DELIVERY_AFTER_SECONDS == 7 * DAY
    assert dreamer_families.MAX_DELIVERIES == 2
    assert upkeep.MAX_ITEM_CHARS == 400
    assert upkeep.MAX_TRIED == 4
