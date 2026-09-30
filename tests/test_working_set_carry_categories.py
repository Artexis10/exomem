"""A carried page is read through the lenses its own units answer.

The retrieval carry names a page and reads it through the first six `units`
lenses by priority. A page whose units are filed under a category only a
later lens selects (a risk, an open problem) read as nothing, the carry fell
through to an abstention, and a conclusion the turn had plainly named was not
served. The lenses are now those that select what the page holds, so the
ceiling is spent on lenses that can answer.

Invented page, the carry suite's corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import _seed_prose_corpus

from exomem import lexstore, working_set, working_set_index, working_set_runtime

PAGE = "Knowledge Base/Notes/Research/harrow-bellwether-audit.md"
TURN = "what is the exposure on the harrow bellwether audit"


@pytest.fixture
def risk_vault(vault: Path) -> Path:
    _seed_prose_corpus(vault)
    path = vault / PAGE
    path.write_text(
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-12\n---\n\n"
        "# Harrow bellwether audit\n\n## Summary\n\n"
        "- [risk] The harrow bellwether audit exposes the ledger to a double count if the "
        "bellwether is rerun without its lock. ^h-risk\n",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def test_a_named_page_holding_only_a_late_lens_category_is_served(risk_vault: Path) -> None:
    packet = working_set.compile_packet(risk_vault, turn=TURN, max_chars=4000)

    assert packet["abstained"] is False, packet["anchors"]
    assert [a["path"] for a in packet["anchors"]] == [PAGE]
    assert any("double count" in u["text"] for u in packet["units"])


def test_the_lenses_are_the_ones_that_select_what_the_page_holds() -> None:
    from exomem import context_roles, working_set_resolve

    registry = context_roles.load_roles(Path("."))
    analysis = working_set_resolve.analyze_turn(TURN)

    chosen = working_set._carry_roles(registry, analysis, frozenset({"risk"}))

    assert [role["id"] for role in chosen] == ["open_questions"]
