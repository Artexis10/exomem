"""A keyless caller's conversation: the echoed thread in the continuity token.

A remote connector passes neither `session` nor `workspace`, and the HTTP
server is stateless, so before the token carried a thread such a caller was
ranked over the whole vault: whatever ANY session touched last. In one thread
that meant a follow-up abstained, and a later one resolved pages another
session had just been editing.

Three rules, end to end through `commands.op_activate_context`:

1. Every packet returns a token naming a salted, bounded-lifetime thread; a
   valid one passed back is that caller's session tier.
2. A caller with no key and no valid thread never anchors on the vault's heat:
   it may fill `recent_context`, never resolve or carry.
3. A short anaphoric follow-up is carried from the caller's OWN thread when
   that thread holds one dominant page, as a `partial` anchor.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_carry import CARRY_PAGE, _seed_carry_pages
from test_working_set_hot_projection import (
    MARIT,
    MARIT_TURN,
    NONSENSE_TURN,
    SLED,
    _edit,
    _live,
    _one_old_tick,
    _resolved,
)
from test_working_set_index import _seed_planning, _seed_structure

from exomem import commands, lexstore, working_set_heat, working_set_index, working_set_runtime
from exomem.governance.principal import request_scope

FOLLOW_UP = "what about the second one?"


@pytest.fixture
def heat_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`test_working_set_hot_projection.heat_vault`, copied rather than
    imported: a cross-file fixture import used as a parameter reads to ruff
    as a redefinition (F811)."""
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _activate(vault: Path, turn: str, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, **kwargs)


def _thread(token: str) -> str:
    payload = working_set_runtime.decode_continuity(token)
    assert payload is not None, token
    return str(payload.get("thread") or "")


def _another_session_works_on_the_sled(vault: Path) -> None:
    """Session A, keyed, works hard on one topic: edits and picks."""
    other = "session-a-" + "a1" * 8
    _edit(vault, SLED, "A towed cargo sled", "A towed freight sled")
    for _ in range(3):
        commands.op_activate_context(vault, turn=NONSENSE_TURN, anchor=SLED, session=other)


# --------------------------------------------------------------------------- #
# Rule 2: a keyless caller never anchors on other sessions' heat
# --------------------------------------------------------------------------- #


def test_a_keyless_turn_that_names_nothing_never_resolves_another_sessions_topic(
    heat_vault: Path,
) -> None:
    _another_session_works_on_the_sled(heat_vault)
    working_set_runtime.reset_caches_for_tests()

    packet = _activate(heat_vault, "continue")

    assert packet["abstained"] is True, packet["anchors"]
    assert SLED not in _resolved(packet)
    assert packet["units"] == [] and packet.get("generation", {}).get("carried_by") is None
    # The vault tier still fills the recent-context pointers.
    assert SLED in [item["path"] for item in packet["recent_context"]]


def test_a_keyed_session_still_falls_through_to_the_vault(heat_vault: Path) -> None:
    """Unchanged for a caller that supplied its own key (a hook): a fresh
    session with an empty tier is still answered from the vault's work."""
    _another_session_works_on_the_sled(heat_vault)
    working_set_runtime.reset_caches_for_tests()

    packet = _activate(heat_vault, "continue", session="hook-session-" + "b2" * 8)

    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])


# --------------------------------------------------------------------------- #
# Rule 1: every packet echoes a thread; a valid one is the caller's session
# --------------------------------------------------------------------------- #


def test_every_packet_returns_a_thread_and_a_valid_one_continues_it(heat_vault: Path) -> None:
    first = _activate(heat_vault, NONSENSE_TURN)

    assert first["abstained"] is True
    token = first["continuity"]
    nonce = _thread(token)
    assert nonce
    assert working_set_runtime.decode_continuity(token)["refs"] == []
    assert first["generation"]["continuity_thread"] == "absent"

    second = _activate(heat_vault, MARIT_TURN, continuity=token)

    assert second["generation"]["continuity_thread"] == "applied"
    assert _thread(second["continuity"]) == nonce
    # A token with no refs strengthens nothing, exactly as no token.
    assert second["generation"]["continuity"] == "absent"
    # Only a salted derivation is stored, never the thread itself.
    sidecar = working_set_heat.sidecar_path(heat_vault).read_bytes()
    assert nonce.encode("utf-8") not in sidecar


def test_an_expired_or_unreadable_thread_degrades_to_keyless(heat_vault: Path) -> None:
    first = _activate(heat_vault, MARIT_TURN)
    payload = working_set_runtime.decode_continuity(first["continuity"])
    long_ago = time.time_ns() - 7 * 3600 * 1_000_000_000
    expired = working_set_runtime.encode_continuity(
        identity=payload["identity"],
        roles_hash=payload["roles_hash"],
        conventions_hash=payload["conventions_hash"],
        generation=payload["generation"],
        refs=payload["refs"],
        roles=payload["roles"],
        minted_ns=long_ago,
        thread=payload["thread"],
        thread_ns=long_ago,
    )

    stale = _activate(heat_vault, FOLLOW_UP, continuity=expired)
    garbage = _activate(heat_vault, FOLLOW_UP, continuity="not-a-token")

    assert stale["generation"]["continuity_thread"] == "stale"
    assert _thread(stale["continuity"]) not in ("", payload["thread"])
    assert stale["abstained"] is True and MARIT not in _resolved(stale)
    assert garbage["generation"]["continuity_thread"] == "stale"
    assert garbage["abstained"] is True


# --------------------------------------------------------------------------- #
# Rule 3: an anaphoric follow-up is carried from the caller's own thread
# --------------------------------------------------------------------------- #


def test_a_follow_up_carries_the_threads_one_page(heat_vault: Path) -> None:
    named = _activate(heat_vault, MARIT_TURN)
    assert MARIT in _resolved(named)
    _another_session_works_on_the_sled(heat_vault)
    working_set_runtime.reset_caches_for_tests()

    carried = _activate(heat_vault, FOLLOW_UP, continuity=named["continuity"])
    keyless = _activate(heat_vault, FOLLOW_UP)

    assert carried["abstained"] is False, (carried.get("abstention"), carried["anchors"])
    (anchor,) = carried["anchors"]
    assert anchor["path"] == MARIT and anchor["status"] == "partial"
    assert carried["generation"]["carried_by"] == "follow_up"
    assert keyless["abstained"] is True
    assert keyless["abstention"] == {"reason": "unresolved"}
    assert MARIT not in [item["path"] for item in keyless["anchors"]]
    assert SLED not in [item["path"] for item in keyless["anchors"]]


def test_a_keyed_follow_up_carries_the_sessions_one_page(heat_vault: Path) -> None:
    session = "hook-session-" + "c3" * 8
    _activate(heat_vault, MARIT_TURN, session=session)
    _another_session_works_on_the_sled(heat_vault)
    working_set_runtime.reset_caches_for_tests()

    carried = _activate(heat_vault, FOLLOW_UP, session=session)

    (anchor,) = carried["anchors"]
    assert anchor["path"] == MARIT and anchor["status"] == "partial"


def test_a_follow_up_over_two_equal_pages_abstains_and_lists_them(heat_vault: Path) -> None:
    session = "hook-session-" + "d4" * 8
    for rel in (MARIT, SLED):
        commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, anchor=rel, session=session)
    # Both picks at one instant: an honest tie in the session's own tier.
    derived = working_set_heat.attribution_for(heat_vault, session=session)
    now = time.time_ns()
    assert working_set_heat.append(
        heat_vault,
        [
            working_set_heat.HeatEvent(now, rel, "pick", origin="pick", session=derived.session)
            for rel in (MARIT, SLED)
        ],
    )
    working_set_runtime.reset_caches_for_tests()

    packet = _activate(heat_vault, FOLLOW_UP, session=session)

    assert packet["abstained"] is True
    listed = {item["ref"] for item in packet["ambiguity"]} | {
        item.get("path") for item in packet["anchors"]
    }
    assert packet["units"] == []
    assert any(MARIT in str(ref) for ref in listed) and any(SLED in str(ref) for ref in listed)


def test_a_turn_that_names_something_is_not_a_follow_up(heat_vault: Path) -> None:
    named = _activate(heat_vault, MARIT_TURN)

    packet = _activate(
        heat_vault, "what about the quarterly zqxwvu plonktastic budget?", continuity=named["continuity"]
    )

    assert packet["generation"].get("carried_by") != "follow_up"


# --------------------------------------------------------------------------- #
# Governance: withheld equals absent for the thread too
# --------------------------------------------------------------------------- #


def _guest(vault: Path, turn: str, token: str) -> dict:
    working_set_runtime.reset_caches_for_tests()
    with request_scope(_external()):
        packet = _activate(vault, turn, continuity=token)
    return {key: value for key, value in packet.items() if key not in ("continuity", "timings")}


def test_a_withheld_page_in_a_guests_thread_answers_as_an_absent_one(heat_vault: Path) -> None:
    write_scope(heat_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(heat_vault, ceiling=0)
    _reset_caches()
    with request_scope(_external()):
        first = _activate(heat_vault, MARIT_TURN)
    token = first["continuity"]
    with request_scope(_external()):
        derived = working_set_heat.attribution_for(heat_vault, thread=_thread(token))
    assert derived.session
    now = time.time_ns()
    # The guest's thread touched Marit Solheim, which it may see.
    assert working_set_heat.append(
        heat_vault,
        [working_set_heat.HeatEvent(now, MARIT, "pick", origin="pick", session=derived.session)],
    )

    absent = {turn: _guest(heat_vault, turn, token) for turn in ("continue", FOLLOW_UP)}
    # ...and, at the same instant, a page it may not see.
    assert working_set_heat.append(
        heat_vault,
        [working_set_heat.HeatEvent(now, CARRY_PAGE, "pick", origin="pick", session=derived.session)],
    )
    withheld = {turn: _guest(heat_vault, turn, token) for turn in ("continue", FOLLOW_UP)}

    assert withheld == absent
    assert absent[FOLLOW_UP]["generation"]["continuity_thread"] == "applied"
    (anchor,) = absent[FOLLOW_UP]["anchors"]
    assert anchor["path"] == MARIT
