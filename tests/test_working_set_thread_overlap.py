"""Same-thread pages are candidate anchors for a turn that overlaps them.

A page this conversation just worked on, listed in `recent_context`, used to
be reachable only by a follow-up SHORT enough to name nothing of its own
("what about the second one?"). A follow-up that says what it is about, in the
page's own words, reached nothing: it named no anchor, its words did not pair
into a phrase for the carry, and the thread was never asked. Now the caller's
own session tier is a candidate for any turn that resolved nothing, gated by
how much of the page's own name the turn shares. A turn that shares nothing is
still not about it, and another conversation's work is never asked.

Invented pages; the carry suite's corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import CARRY_PAGE, _seed_carry_pages
from test_working_set_hot_projection import NONSENSE_TURN, _live, _one_old_tick
from test_working_set_index import _seed_planning, _seed_structure

from exomem import commands, lexstore, working_set_heat, working_set_index, working_set_runtime

SESSION = "thread-session-" + "e5" * 8
#: Shares two words of the page's title, said too far apart to read as its name.
OVERLAP_TURN = "is the vantry sizing still right, given how the quillon behaves lately for us all?"
UNRELATED_TURN = "how should the depot roster be sized for the next quarter?"


@pytest.fixture
def thread_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _pick(vault: Path, session: str = SESSION) -> None:
    commands.op_activate_context(vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE, session=session)
    working_set_runtime.reset_caches_for_tests()


def _turn(vault: Path, turn: str, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, **kwargs)


def test_a_turn_sharing_the_threads_page_name_is_served_that_page(thread_vault: Path) -> None:
    _pick(thread_vault)

    packet = _turn(thread_vault, OVERLAP_TURN, session=SESSION)

    assert packet["abstained"] is False, (packet.get("abstention"), packet["anchors"])
    (anchor,) = packet["anchors"]
    assert anchor["path"] == CARRY_PAGE and anchor["status"] == "partial"
    assert packet["generation"]["carried_by"] == "follow_up"
    assert packet["units"]


def test_a_turn_sharing_nothing_with_the_page_is_not_about_it(thread_vault: Path) -> None:
    _pick(thread_vault)

    packet = _turn(thread_vault, UNRELATED_TURN, session=SESSION)

    assert CARRY_PAGE not in [a.get("path") for a in packet["anchors"]]
    assert packet["generation"].get("carried_by") != "follow_up"


def test_another_conversations_page_is_never_the_thread(thread_vault: Path) -> None:
    _pick(thread_vault)

    packet = _turn(thread_vault, OVERLAP_TURN, session="other-session-" + "f6" * 8)

    assert CARRY_PAGE not in [a.get("path") for a in packet["anchors"]]
    assert packet["generation"].get("carried_by") != "follow_up"


def test_a_keyless_caller_has_no_thread_to_ask(thread_vault: Path) -> None:
    _pick(thread_vault)

    packet = _turn(thread_vault, OVERLAP_TURN)

    assert packet["abstained"] is True
    assert CARRY_PAGE not in [a.get("path") for a in packet["anchors"]]
