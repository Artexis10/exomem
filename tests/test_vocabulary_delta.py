"""The vocabulary delta: who is told about new vocabulary, once, and what they never learn.

Every case drives the real registry writer and the real activation and mutation
paths; only the caller's session identity is chosen by the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_connector_boundary import (
    configured_boundary as configured_boundary,  # noqa: F401, PLC0414
)
from test_private_vocabulary_instances import SCOPE, private_instances  # noqa: F401
from test_recent_promotions import _as
from test_vocabulary_registries import _served

from exomem import commands, vocabulary_delta
from exomem import due_state as due_state_module
from exomem.governance.principal import library_scope


@pytest.fixture(autouse=True)
def _clean_state():
    due_state_module.reset_emission_state()
    vocabulary_delta._OWED_ROWS.clear()
    yield
    due_state_module.reset_emission_state()
    vocabulary_delta._OWED_ROWS.clear()


def _promote(call, key: str) -> None:
    inspected = call("schema_memory", {"subject": "relations", "operation": "inspect"})
    saved = call("schema_memory", {
        "subject": "relations", "operation": "save", "expected_hash": inspected["content_hash"],
        "proposal": {"upsert": {key: {"parent": "relates_to", "description": f"Synthetic relation {key}."}}},
        "why": "Register a synthetic relation.",
    })
    assert saved["status"] == "committed", saved


def _keys(block: dict | None) -> list[str]:
    return [entry["key"] for entry in (block or {}).get("entries", [])]


def _write(call, name: str, **extra) -> dict:
    """One ordinary committed write: a distinct entity page."""
    return call("connect_memory", {
        "operation": "create-entity", "entity_type": "concept", "name": name,
        "summary": f"A synthetic concept named {name}.", **extra,
    })


def test_another_session_is_told_once_per_door_and_a_new_promotion_tells_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _promote(call, "vault.hosts")

    _as(monkeypatch, "session-b")
    activation = call("recall_context", {"turn": "which relations exist in this vault?"})
    assert _keys(activation.get("vocabulary_delta")) == ["vault.hosts"]
    entry = activation["vocabulary_delta"]["entries"][0]
    assert entry == {
        "registry": "relations", "key": "vault.hosts", "label": "vault.hosts",
        "description": "Synthetic relation vault.hosts.", "parent": "relates_to",
    }
    assert "vocabulary_delta" not in call("recall_context", {"turn": "which relations exist in this vault?"})
    assert "vocabulary_delta" not in _write(call, "Tidal Ledger")

    _as(monkeypatch, "session-a")
    _promote(call, "vault.runs")

    # The mutating door carries the new entry alone, and the activation after it carries nothing.
    _as(monkeypatch, "session-b")
    assert _keys(_write(call, "Harbour Reconciliation").get("vocabulary_delta")) == ["vault.runs"]
    assert "vocabulary_delta" not in _write(call, "Quiet Ferry")
    assert "vocabulary_delta" not in call("recall_context", {"turn": "which relations exist in this vault?"})


def test_a_legacy_response_neither_carries_nor_spends_the_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _promote(call, "vault.hosts")

    _as(monkeypatch, "session-b")
    legacy = _write(call, "Legacy Ledger", response_detail="legacy")
    assert "vocabulary_delta" not in legacy
    assert _keys(_write(call, "Compact Ledger").get("vocabulary_delta")) == ["vault.hosts"]


def test_the_delta_is_cut_to_five_entries_and_counted_inside_limit_chars(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    _as(monkeypatch, "session-a")
    for number in range(7):
        _save_relation(vault, f"vault.rel{number}", parent="relates_to")

    _as(monkeypatch, "session-b")
    cramped = {"budget": {"limit_chars": 1000, "used_chars": 990}}
    with library_scope():
        vocabulary_delta.for_packet(vault, cramped)
        assert "vocabulary_delta" not in cramped, "a block that does not fit is omitted whole"
        assert cramped["budget"]["used_chars"] == 990

        packet = {"budget": {"limit_chars": 4000, "used_chars": 1000}}
        vocabulary_delta.for_packet(vault, packet)

    block = packet["vocabulary_delta"]
    assert _keys(block) == [f"vault.rel{number}" for number in (6, 5, 4, 3, 2)], "newest first, five at most"
    assert block["more"] == 2
    assert vocabulary_delta.size(block) <= vocabulary_delta.MAX_CHARS
    assert packet["budget"]["used_chars"] == 1000 + vocabulary_delta.size(block)
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]

    # The cramped packet delivered nothing, so the entries stayed owed to the same conversation.
    _as(monkeypatch, "session-c")
    with library_scope():
        late = {"budget": {"limit_chars": 1000, "used_chars": 990}}
        vocabulary_delta.for_packet(vault, late)
        roomy = {"budget": {"limit_chars": 4000, "used_chars": 0}}
        vocabulary_delta.for_packet(vault, roomy)
    assert "vocabulary_delta" not in late
    assert _keys(roomy["vocabulary_delta"]) == _keys(block)


def test_the_delta_in_a_real_activation_updates_the_serialized_length(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    _as(monkeypatch, "session-a")
    _save_relation(vault, "vault.hosts", parent="relates_to")

    _as(monkeypatch, "session-b")
    with library_scope():
        packet = commands.op_activate_context(vault, "which relations exist in this vault?")

    assert _keys(packet["vocabulary_delta"]) == ["vault.hosts"]
    budget = packet["budget"]
    assert budget["used_chars"] <= budget["limit_chars"]
    without_field = {**packet, "budget": {key: value for key, value in budget.items() if key != "serialized_chars"}}
    from exomem import context_views

    assert budget["serialized_chars"] == context_views.serialized_chars(without_field)


def test_the_hook_door_is_one_conversation_by_the_session_it_sends(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REST and CLI doors carry no transport session; the hook's own `session` argument stands in for it."""
    from test_recent_promotions import _save_relation

    _as(monkeypatch, "session-a")
    _save_relation(vault, "vault.hosts", parent="relates_to")

    _as(monkeypatch, None)
    turn = "which relations exist in this vault?"
    with library_scope():
        first = commands.op_activate_context(vault, turn, session="hook-conversation")
        again = commands.op_activate_context(vault, turn, session="hook-conversation")
        other = commands.op_activate_context(vault, turn, session="another-conversation")
        anonymous = commands.op_activate_context(vault, turn)

    assert _keys(first.get("vocabulary_delta")) == ["vault.hosts"]
    assert "vocabulary_delta" not in again
    assert _keys(other.get("vocabulary_delta")) == ["vault.hosts"]
    assert "vocabulary_delta" not in anonymous, "a call that names no conversation cannot be told once"


def test_bootstrap_new_and_the_delta_list_the_same_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two readers of one producer cannot drift: what bootstrap marks `new` is what the delta carries."""
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    for key in ("vault.hosts", "vault.runs", "vault.audits"):
        _promote(call, key)

    new = set(call("bootstrap", {"section": "vocabulary"})["vocabulary"]["relations"]["new"])
    _as(monkeypatch, "session-b")
    packet = {"budget": {"limit_chars": 4000, "used_chars": 0}}
    with library_scope():
        vocabulary_delta.for_packet(vault, packet)

    assert new == {"vault.hosts", "vault.runs", "vault.audits"}
    assert set(_keys(packet["vocabulary_delta"])) == new


def test_a_restricted_session_learns_nothing_of_a_private_promotion(private_instances, vault, monkeypatch) -> None:  # noqa: F811
    from exomem.governance.principal import request_scope

    def packet_for(principal, session: str) -> dict:
        _as(monkeypatch, session)
        packet = {"budget": {"limit_chars": 4000, "used_chars": 0}}
        with request_scope(principal):
            vocabulary_delta.for_packet(vault, packet)
        return packet

    def save_type(principal, *, scope, key: str) -> None:
        with request_scope(principal):
            inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=scope)
            saved = commands.op_schema_memory(
                vault, "save", subject="entity-types", registry_scope=scope,
                expected_hash=inspected["content_hash"], why="Add a synthetic entity type.",
                proposal={"upsert": {key: {
                    "label": key.title(), "guidance": "A synthetic place.", "parent": "concept",
                    "attributes": {"folder": key.title()},
                }}},
            )
            assert saved["valid"], saved

    full, limited = private_instances("full"), private_instances("limited")
    # The limited session is told about the public baseline first, so later changes are measured against it.
    save_type(full, scope=None, key="depot")
    assert _keys(packet_for(limited, "limited-session").get("vocabulary_delta")) == ["depot"]

    # A private promotion changes nothing the limited session can see: no block, no count, no ledger move.
    for number in range(6):
        save_type(full, scope=SCOPE, key=f"hidden{number}")
    assert "vocabulary_delta" not in packet_for(limited, "limited-session")

    # A public promotion made in the same stretch is told without any trace of the six private ones.
    save_type(full, scope=None, key="harbour")
    told = packet_for(limited, "limited-session")["vocabulary_delta"]
    assert told == {"entries": [{
        "registry": "entity-types", "key": "harbour", "label": "Harbour",
        "description": "A synthetic place.", "parent": "concept",
    }]}

    # The audience that may read the private instance does see it.
    seen = packet_for(full, "full-session")["vocabulary_delta"]
    assert _keys(seen) == ["harbour", "hidden5", "hidden4", "hidden3", "hidden2"]
    assert seen["more"] == 3
