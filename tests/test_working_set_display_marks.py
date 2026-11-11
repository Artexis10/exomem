"""Display marks on the units that `recall_context` serves.

Slice 5b gave a search's dimension groups their marks: `open`, `replaced` and `origin`, and the
order each group's display behaviours set. This module tests the same marks and order on the
units of a context packet, which carries them with no request parameter.

The module began as the before-picture: `test_the_packet_this_vault_serves` recorded, on the code
before the marks, the units one `recall_context` call serves, their order and their fields. A line
marked `# FLIPS marks` is one that the marks changed on purpose, and its comment names the value
it held before. Every other line of that test held before and still holds.

The fixture vault is invented. A hub page holds a decision that a later decision supersedes, two
open questions of which another page answers one, a claim with its holder and four sources, and
two timeline events written in the reverse order of their dates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    commands,
    epistemic_graph,
    freshness,
    lexstore,
    working_set_index,
    working_set_marks,
    working_set_runtime,
)
from exomem import find as find_module
from exomem import vault as vault_module

pytestmark = pytest.mark.usefixtures("anchors_packet")  # marks ride on the anchors packet's units

NOTES = "Knowledge Base/Notes"
HUB = f"{NOTES}/Orchard launch.md"
HUB_REF = "exomem://memory/11111111-1111-4111-8111-111111111111"
SOURCES = ("Orchard survey", "Orchard interviews", "Orchard tickets", "Orchard forum")
HUB_PAGE = (
    "---\ntype: insight\nstatus: active\ntags: [hub]\n"
    "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n# Orchard launch\n\n"
    "The orchard launch brings the cider app to stores.\n\n"
    "## Decision\n- id: before\n\nThe orchard launch ships in January.\n\n"
    "## Decision\n- id: after\n- relations: supersedes: [[#before]]\n\n"
    "The orchard launch ships in March.\n\n"
    "## Open question\n- id: beta\n\nDoes the orchard launch need a beta?\n\n"
    "## Open question\n- id: support\n\nWho owns orchard launch support?\n\n"
    "## Claim\n- id: exports\n- asserted_by: Research team\n"
    + "".join(f"- relations: derived_from: [[{source}]]\n" for source in SOURCES)
    + "\nOrchard launch exports come first on the roadmap.\n\n"
    "## Timeline event\n- id: retro\n- occurred_at: 2026-03-01\n\n"
    "The kickoff retrospective is held.\n\n"
    "## Timeline event\n- id: kickoff\n- occurred_at: 2026-01-10\n\n"
    "The kickoff meeting opens the work.\n"
)
ANSWER_PAGE = (
    "---\ntype: insight\n---\n# Orchard answers\n\n"
    "## Finding\n- relations: answers: [[Orchard launch#beta]]\n\n"
    "The orchard launch needs a two week beta.\n"
)
#: An answer the packet does not serve: the page answers the question in its relations section,
#: so it holds no unit that a lane could select.
UNSERVED_ANSWER_PAGE = (
    "---\ntype: insight\n---\n# Beta review\n\nThe review agreed on a beta.\n\n"
    "## Relations\n\n- answers [[Orchard launch#beta]]\n"
)
TURN = "Remind me about the orchard launch: what changed since kickoff, and what about exports?"


def _vault(
    tmp_path: Path,
    *,
    answer: str | None = f"{NOTES}/Orchard answers.md",
    answer_page: str = ANSWER_PAGE,
    hub_page: str = HUB_PAGE,
    name: str = "vault",
    graph: bool = True,
) -> Path:
    """The fixture vault, with `hub_page` as its hub and `answer_page` at `answer`, or without
    an answering page when `answer` is None, and every index the packet reads built: the
    relation graph only when `graph` is true."""
    from conftest import initialize_vault_state_offline

    vault = tmp_path / name
    vault.mkdir(parents=True)
    initialize_vault_state_offline(vault, source="display marks fixture")
    pages = {HUB: hub_page, **{
        f"{NOTES}/{source}.md": (
            f"---\ntype: insight\n---\n# {source}\n\n## Claim\n\n{source} were read in May.\n"
        )
        for source in SOURCES
    }}
    if answer is not None:
        pages[answer] = answer_page
    for rel, body in pages.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(body, encoding="utf-8")
    find_module.clear_cache()
    lexstore.ensure_fresh(vault)
    if graph:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _recall(vault: Path, turn: str = TURN, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, max_chars=6000, **kwargs)


def _units(packet: dict) -> dict[str, dict]:
    """Each served unit, by its text."""
    return {unit["text"]: unit for unit in packet["units"]}


def test_the_packet_this_vault_serves(tmp_path: Path) -> None:
    """The units, order and fields one `recall_context` call serves on the fixture vault.

    Catches a mark that removes, adds or moves a unit it should not, or changes a field the
    packet served before the marks.
    """
    packet = _recall(_vault(tmp_path))

    assert packet["abstained"] is False
    assert [unit["text"] for unit in packet["units"]] == [
        "The orchard launch ships in March.",
        "The orchard launch needs a two week beta.",
        "The orchard launch ships in January.",
        "The orchard launch brings the cider app to stores.",
        "Orchard launch exports come first on the roadmap.",
        "The kickoff meeting opens the work.",  # FLIPS marks: was the retrospective
        "The kickoff retrospective is held.",  # FLIPS marks: was the kickoff meeting
        "Does the orchard launch need a beta?",
        "Who owns orchard launch support?",
    ]
    units = _units(packet)
    assert units["The orchard launch ships in January."]["lifecycle"] == "superseded"
    assert units["The orchard launch ships in January."]["history"] is True
    assert [unit["role"] for unit in packet["units"]] == [
        "recent_change", "recent_change", "recent_change", "identity",
        "material", "material", "material", "open_questions", "open_questions",
    ]
    assert {unit["text"]: sorted(unit) for unit in packet["units"]} == {
        "The orchard launch ships in March.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch needs a two week beta.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch ships in January.": [  # FLIPS marks: was without "display"
            "display", "history", "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch brings the cider app to stores.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Orchard launch exports come first on the roadmap.": [  # FLIPS marks: was without "display"
            "display", "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The kickoff retrospective is held.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The kickoff meeting opens the work.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Does the orchard launch need a beta?": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Who owns orchard launch support?": [  # FLIPS marks: was without "display"
            "display", "lifecycle", "provenance", "ref", "role", "text", "updated"],
    }
    assert [pointer["ref"] for pointer in packet["pointers"]] == [f"{HUB_REF}#before"]
    assert "display_edges" not in packet["generation"]


def _unmarked(vault: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> dict:
    """The packet `vault` serves with the marks switched off: the packet before the marks."""
    with monkeypatch.context() as patch:
        patch.setattr(working_set_marks, "compile", lambda *args, **kw: None)
        working_set_runtime.reset_caches_for_tests()
        return _recall(vault, **kwargs)


@pytest.mark.parametrize(
    ("answer", "answer_page"),
    [
        (f"{NOTES}/Orchard answers.md", ANSWER_PAGE),
        (f"{NOTES}/Beta review.md", UNSERVED_ANSWER_PAGE),
    ],
    ids=["served-answer", "unserved-answer"],
)
def test_a_question_is_open_until_a_page_the_caller_sees_answers_it(
    tmp_path: Path, answer: str, answer_page: str
) -> None:
    """Catches `open` read from the served units alone: a question that a page the packet does
    not serve has answered would tell the agent it is still open."""
    packet = _recall(_vault(tmp_path, answer=answer, answer_page=answer_page))

    units = _units(packet)
    served = answer_page == ANSWER_PAGE
    assert ("The orchard launch needs a two week beta." in units) is served
    assert "display" not in units["Does the orchard launch need a beta?"]
    assert units["Who owns orchard launch support?"]["display"] == {"open": True}


def _withheld_answer_twin(tmp_path: Path, *, withheld: bool) -> Path:
    """The fixture vault with its answering page in a folder that an egress rule withholds from
    an external caller, or as if it never had that page."""
    from test_governance_egress import _reset_caches, write_rule, write_scope

    from exomem.governance import egress

    vault = _vault(
        tmp_path,
        answer=f"{NOTES}/Patterns/Orchard answers.md" if withheld else None,
        name="with-page" if withheld else "without-page",
    )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()
    # A restricted caller's marks borrow the link resolver resident at the live checkpoint, as a
    # running service holds it; they never build one.
    freshness.seed(
        vault, "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    find_module.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.live_recall_checkpoint(vault, "vault")
    )
    return vault


def test_a_caller_who_may_not_see_the_answer_gets_the_packet_of_a_vault_without_it(
    tmp_path: Path,
) -> None:
    """Catches a mark read from an edge the caller may not see: the question would lose `open`
    and tell the caller that a withheld page answers it."""
    from test_governance_egress import _external

    from exomem.governance import principal

    replies = []
    for withheld in (True, False):
        vault = _withheld_answer_twin(tmp_path, withheld=withheld)
        with principal.request_scope(_external()):
            packet = _recall(vault)
        # These describe the request and the index instance, not released content.
        packet.pop("timings", None)
        packet.pop("continuity", None)
        packet["generation"].pop("freshness_key", None)
        replies.append(packet)
    with_page, without_page = replies

    assert with_page == without_page
    units = _units(with_page)
    assert units["Does the orchard launch need a beta?"]["display"] == {"open": True}
    assert units["Who owns orchard launch support?"]["display"] == {"open": True}


def test_a_replaced_unit_follows_its_replacement_and_names_it(tmp_path: Path) -> None:
    """Catches a replaced decision served as if it were current: ahead of the decision that
    replaced it, or with nothing that names the replacement."""
    packet = _recall(_vault(tmp_path))

    texts = [unit["text"] for unit in packet["units"]]
    units = _units(packet)
    assert texts.index("The orchard launch ships in March.") < texts.index(
        "The orchard launch ships in January."
    )
    assert units["The orchard launch ships in January."]["display"] == {
        "replaced": {"by": f"{HUB_REF}#after"}
    }
    assert "display" not in units["The orchard launch ships in March."]
    # The working set's own lifecycle reading stays as it was.
    assert units["The orchard launch ships in January."]["lifecycle"] == "superseded"


def test_dated_events_are_served_earliest_first_and_other_units_keep_their_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches events served in the order the page lists them, which tells the agent the
    retrospective came before the kickoff, and a time order that moves units it does not
    cover."""
    vault = _vault(tmp_path)
    before = [unit["text"] for unit in _unmarked(vault, monkeypatch)["units"]]
    working_set_runtime.reset_caches_for_tests()

    after = [unit["text"] for unit in _recall(vault)["units"]]

    events = ("The kickoff meeting opens the work.", "The kickoff retrospective is held.")
    assert [text for text in before if text in events] == list(reversed(events))
    assert sorted(after) == sorted(before)
    assert [text for text in after if text in events] == list(events)
    # Every other unit keeps its place, so the events fill the places they held.
    assert {i: text for i, text in enumerate(after) if text not in events} == {
        i: text for i, text in enumerate(before) if text not in events
    }


def test_a_sourced_claim_names_its_holder_and_at_most_three_sources(tmp_path: Path) -> None:
    """Catches a claim served with no word of whose it is or where it came from, and an origin
    that lists every source and crowds the packet."""
    packet = _recall(_vault(tmp_path))

    claim = _units(packet)["Orchard launch exports come first on the roadmap."]
    origin = claim["display"]["origin"]
    source_refs = {
        f"exomem://vault/{NOTES.replace(' ', '%20')}/{source.replace(' ', '%20')}.md"
        for source in SOURCES
    }
    assert origin["by"] == "Research team"
    assert len(origin["from"]) == 3
    assert set(origin["from"]) < source_refs
    assert origin["more"] == 1


def test_the_marks_are_charged_to_the_packet_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches marks that reach the agent without counting toward the packet's character
    budget, so a packet can carry more than the ceiling its caller asked for."""
    vault = _vault(tmp_path)
    unmarked = _unmarked(vault, monkeypatch)
    working_set_runtime.reset_caches_for_tests()

    marked = _recall(vault)

    marks = [unit["display"] for unit in marked["units"] if "display" in unit]
    assert marks
    assert [unit["ref"] for unit in marked["units"]] != [unit["ref"] for unit in unmarked["units"]]
    assert {unit["ref"] for unit in marked["units"]} == {unit["ref"] for unit in unmarked["units"]}
    assert marked["budget"]["used_chars"] == unmarked["budget"]["used_chars"] + sum(
        working_set_marks.chars(mark) for mark in marks
    )
    assert marked["budget"]["used_chars"] <= marked["budget"]["limit_chars"]


#: A hub whose units no displaying dimension holds: decisions and a claim with no replacement,
#: source, holder, question or date.
PLAIN_HUB_PAGE = (
    "---\ntype: insight\nstatus: active\ntags: [hub]\n"
    "exomem_id: 22222222-2222-4222-8222-222222222222\n---\n# Orchard launch\n\n"
    "The orchard launch brings the cider app to stores.\n\n"
    "## Decision\n- id: march\n\nThe orchard launch ships in March.\n\n"
    "## Claim\n- id: exports\n\nOrchard launch exports come first on the roadmap.\n"
)


def test_a_packet_with_nothing_to_mark_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a packet that the marks change when it has nothing to mark: an empty `display`,
    a reordered unit, a charged character or a diagnostics key."""
    vault = _vault(tmp_path, answer=None, hub_page=PLAIN_HUB_PAGE)
    unmarked = _unmarked(vault, monkeypatch)
    working_set_runtime.reset_caches_for_tests()

    marked = _recall(vault)

    assert marked["units"]
    for packet in (unmarked, marked):
        # These describe the request, not the packet: each call mints its own thread.
        packet.pop("timings", None)
        packet.pop("continuity", None)
    assert marked == unmarked


def test_unread_edges_mark_nothing_open_and_the_packet_says_they_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a packet that takes a graph it could not read for one with no answers, so every
    question reads as open, and a cached packet that goes on saying the edges are unknown after
    the graph caught up."""
    monkeypatch.setattr(epistemic_graph, "schedule_background_rebuild", lambda *a, **k: None)
    vault = _vault(tmp_path, graph=False)

    packet = _recall(vault)

    assert packet["generation"]["display_edges"] == "unknown"
    units = _units(packet)
    assert "Who owns orchard launch support?" in units
    assert not any("open" in unit.get("display", {}) for unit in packet["units"])

    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    caught_up = _recall(vault)

    assert "display_edges" not in caught_up["generation"]
    assert _units(caught_up)["Who owns orchard launch support?"]["display"] == {"open": True}


def test_a_packet_compiled_while_the_graph_was_unreadable_is_not_served_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a packet compiled while a built graph could not be read that the cache goes on
    serving, so its questions stay unmarked until the vault next changes."""
    vault = _vault(tmp_path)
    with monkeypatch.context() as busy:
        busy.setattr(
            epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", lambda *a, **k: None
        )
        assert _recall(vault)["generation"]["display_edges"] == "unknown"

    readable = _recall(vault)

    assert "display_edges" not in readable["generation"]
    assert _units(readable)["Who owns orchard launch support?"]["display"] == {"open": True}
