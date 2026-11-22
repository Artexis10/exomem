"""The person's recorded positions reach the agent from the pages a turn's task context serves."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _page, _write
from test_context_views import OWNER
from test_governance_egress import _external
from test_working_set_withheld_twins import _withhold

from exomem import (
    activation_conventions,
    commands,
    lexstore,
    positions,
    request_budget,
    semantic_language_registry,
    served_context,
    working_set,
    working_set_index,
)
from exomem._hooks import exomem_retrieve_nudge as hook
from exomem.governance import egress, principal
from exomem.semantic_language_registry import validate_proposal

TURN = "Which glaze firing schedule suits the kiln this week?"
# Filler shares no content word with the turn, so the long page is reached only through the lead's link.
FILLER = "Shelf inventory entry: three cordierite batts, eight posts and a spare thermocouple sheath.\n\n"


def _lead_linking_to_a_long_page(vault: Path, long_body: str) -> tuple[str, str]:
    """A short lead page about the turn, linking to a long page; returns both paths."""
    long_page = _page(vault, "studio-equipment-log", body=long_body)
    lead = _page(
        vault, "glaze-firing-schedule",
        body=f"Glaze firing schedule for the kiln: candle overnight, then ramp.\n\nSee [[{long_page.removesuffix('.md')}]].\n",
    )
    return lead, long_page


def _ready(vault: Path) -> None:
    _built(vault).close()
    lexstore.ensure_fresh(vault)


def test_a_position_late_on_a_linked_long_page_reaches_the_agent(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The position sat in the last fifth of a page the lead linked to; the agent never read that far, so it was never weighed."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    position = "I prefer a slow cool below 600 degrees for celadon, a working choice and not proven"
    lead, long_page = _lead_linking_to_a_long_page(
        vault, FILLER * 40 + f"- [preference] {position} (2026-03-02) ^slow-cool\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert lead in {item["source_ref"] for item in packet["task_context"]["candidates"]}
    assert long_page not in {item["source_ref"] for item in packet["task_context"]["candidates"]}
    block = packet["positions"]
    assert block["state"] == "ready"
    assert block["about"] == positions.ABOUT
    [item] = block["items"]
    assert item["ref"].endswith("/studio-equipment-log.md#slow-cool")
    assert {key: value for key, value in item.items() if key != "ref"} == {
        "text": position, "title": "studio-equipment-log", "date": "2026-03-02",
    }


def test_a_unit_of_an_undeclared_category_is_not_a_position(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Text that says "my position" must not make a unit a position; only a category or kind trait does."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    lead, _long_page = _lead_linking_to_a_long_page(
        vault, "- [insight] My position is that the shelf layout matters more than the ramp (2026-03-05) ^layout\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert lead in {item["source_ref"] for item in packet["task_context"]["candidates"]}
    assert packet["positions"]["items"] == []
    assert packet["positions"]["overflow_refs"] == []


def test_a_page_the_caller_may_not_see_contributes_nothing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked page withheld from a guest must not leak its position, its ref or its title through the block."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    lead, hidden = _lead_linking_to_a_long_page(
        vault, "- [preference] I prefer reduction firing for the studio's own work (2026-02-11) ^reduction\n",
    )
    _withhold(vault, hidden, ceiling=0)
    _ready(vault)

    owner = _activate(vault, turn=TURN, max_chars=8000)
    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)

    assert [item["ref"].rpartition("/")[2] for item in owner["positions"]["items"]] == ["studio-equipment-log.md#reduction"]
    # The guest is served the lead (the turn slice routes it), so the block had a page to read from.
    assert lead in str(guest["turn_slice"]) + str(guest["task_context"])
    assert guest["positions"]["items"] == []
    assert hidden.removesuffix(".md") not in str(guest["positions"])
    assert "studio-equipment-log" not in str(guest["positions"])


def test_a_position_served_or_held_as_stance_is_not_repeated(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing line repeated as a position doubles its prompt cost and reads as two separate views.

    From the second turn the caller holds the stance, so the packet's stance block carries no lines; a skip read
    from those lines skipped nothing, and the stance line came back as a position on every later turn.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _page(
        vault, "studio-equipment-log", "stance: true\n",
        body="- [preference] I prefer witness cones on every shelf (2026-02-20) ^cones\n",
    )
    _page(
        vault, "glaze-firing-schedule",
        body="Glaze firing schedule for the kiln: candle overnight, then ramp.\n\n"
        "- [preference] I prefer a slow candle for thick glaze (2026-03-01) ^candle\n\n"
        "See [[studio-equipment-log]].\n",
    )
    _ready(vault)

    first = _activate(vault, turn=TURN, max_chars=8000)
    second = _activate(vault, turn=TURN, continuity=first["continuity"], max_chars=8000)

    assert [item["ref"].rpartition("#")[2] for item in first["stance"]["items"]] == ["cones"]
    assert (second["stance"]["state"], second["stance"]["items"]) == ("unchanged", [])
    for packet in (first, second):
        assert [item["ref"].rpartition("#")[2] for item in packet["positions"]["items"]] == ["candle"]
        assert packet["positions"]["overflow_refs"] == []


def test_a_stance_or_profile_page_the_turn_did_not_serve_is_not_read_as_positions(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standing line framed as a dated view reads as an opinion to weigh, not a rule to apply.

    A profile of a domain the turn never touched served its line as a position on a glaze turn, and so did the
    stance line that the stance bound left out.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _write(vault, "Knowledge Base/_Schema/source-taxonomy.yaml", "schema_version: 1\ndomains:\n  cooking: {label: Cooking}\n")
    # The short stance line fits 220 characters; the long one does not, so the stance block omits it.
    _write(vault, "Knowledge Base/_Schema/activation-conventions.yaml", "schema_version: 1\nbudgets:\n  stance_max_chars: 220\n")
    _write(
        vault, "Knowledge Base/Notes/Profiles/cooking-profile.md",
        "---\ntype: profile\nstatus: active\ntopics: [cooking]\n---\n\n# cooking-profile\n\n"
        "- [preference] I prefer slow braises and never deep frying (2026-01-10) ^braises\n",
    )
    _page(
        vault, "studio-standing-rules", "stance: true\n",
        body="- [preference] I prefer witness cones on every shelf (2026-02-20) ^cones\n"
        "- [preference] I prefer the kiln room kept below thirty degrees, with both extractor fans running and the door "
        "propped open, whenever a firing passes cone six, and I log the room temperature beside the firing chart every "
        "hour until the kiln has cooled below two hundred degrees (2026-02-21) ^ventilation\n",
    )
    _page(
        vault, "glaze-firing-schedule",
        body="Glaze firing schedule for the kiln: candle overnight, then ramp.\n\n"
        "- [preference] I prefer a slow candle for thick glaze (2026-03-01) ^candle\n\n"
        "See [[studio-standing-rules]] and [[cooking-profile]].\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert "braises" not in str(packet.get("domain_profiles"))
    assert [item["ref"].rpartition("#")[2] for item in packet["stance"]["items"]] == ["cones"]
    assert packet["stance"]["omitted"] == 1
    assert [item["ref"].rpartition("#")[2] for item in packet["positions"]["items"]] == ["candle"]
    assert (packet["positions"]["overflow_refs"], packet["positions"]["omitted"]) == ([], 0)


def test_the_block_keeps_its_own_bound_and_never_costs_the_task_leads(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier block drew on the turn allowance and task leads fell from 25 to 16; overflow must become refs, not cut units."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # Two lines and one reference line fit 560 characters; a third line or a second reference does not.
    _write(vault, "Knowledge Base/_Schema/activation-conventions.yaml", "schema_version: 1\nbudgets:\n  positions_max_chars: 560\n")
    units = "".join(
        f"- [preference] For glaze batch {number} I prefer a ten minute hold at peak before the cool (2026-03-{number:02d}) ^hold-{number}\n"
        for number in range(1, 9)
    )
    _lead_linking_to_a_long_page(vault, units)
    _ready(vault)

    served = _activate(vault, turn=TURN, max_chars=4000)
    monkeypatch.setattr(positions, "compile_block", lambda *args, **kwargs: positions._empty())
    without = _activate(vault, turn=TURN, max_chars=4000)

    block = served["positions"]
    assert (len(block["items"]), len(block["overflow_refs"]), block["omitted"]) == (2, 1, 5)
    assert positions.chars(block) <= 560
    assert all(item["text"].endswith("before the cool") for item in block["items"])
    # Newest first: the dates descend through the served lines and on into the references.
    assert [item["date"] for item in block["items"]] == sorted((item["date"] for item in block["items"]), reverse=True)
    assert block["overflow_refs"][0].endswith("#hold-6")
    assert served["task_context"]["candidates"] == without["task_context"]["candidates"]
    assert served["budget"]["used_chars"] == without["budget"]["used_chars"]


def test_a_bound_of_zero_turns_the_block_off(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault that sets 0 to silence the block still got the framing passage and a line of references."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _write(vault, "Knowledge Base/_Schema/activation-conventions.yaml", "schema_version: 1\nbudgets:\n  positions_max_chars: 0\n")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert packet["positions"] == {"state": "empty", "reason": "disabled", "items": [], "overflow_refs": [], "omitted": 0}


def test_the_position_that_answers_the_turn_outranks_a_hub_of_newer_unrelated_ones(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store listed position units newest page first and kept 32 of them. A linked hub page of 40 newer,
    unrelated preferences pushed out the one that answered the turn, which sat last on an older long page."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hub = "".join(
        f"- [preference] For the gallery wall I prefer oak frame style {number} (2026-04-{number % 28 + 1:02d}) ^frame-{number}\n"
        for number in range(40)
    )
    _page(vault, "framing-preferences", "updated: 2026-04-30\n", body=hub)
    position = "I prefer a glaze firing schedule that holds at cone six before the kiln cools"
    _page(
        vault, "studio-equipment-log", "updated: 2025-01-10\n",
        body=FILLER * 40 + f"- [preference] {position} (2025-01-10) ^cone-hold\n",
    )
    _page(
        vault, "glaze-firing-schedule",
        body="Glaze firing schedule for the kiln: candle overnight, then ramp.\n\n"
        "See [[studio-equipment-log]] and [[framing-preferences]].\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    block = packet["positions"]
    assert block["state"] == "ready"
    assert (block["items"][0]["ref"].rpartition("/")[2], block["items"][0]["text"]) == ("studio-equipment-log.md#cone-hold", position)
    # The rest are the hub's, and every one of the 41 is served, referenced or counted where the agent reads it.
    assert len(block["items"]) + len(block["overflow_refs"]) + block["omitted"] == 41
    assert hook._format_positions_block(block).splitlines()[-1] == positions.overflow_line(block["overflow_refs"], block["omitted"])


def test_a_turn_without_leads_reports_an_empty_block_not_an_unavailable_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An early return was never pinned, so terminal egress replaced it with `unavailable`: every quiet turn read
    as an outage, on the wire and in the served record."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    packet = _activate(vault, turn="Qwzx plorvent glimbaxe?", max_chars=8000)
    with principal.request_scope(OWNER):
        record = served_context.read(vault, served_context_id=packet["served_context_id"])["record"]

    assert packet["task_context"]["candidates"] == []
    assert packet["positions"] == {"state": "empty", "items": [], "overflow_refs": [], "omitted": 0}
    assert record["positions"] == {"state": "empty", "refs": [], "overflow_refs": []}


def _starve(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    """The request deadline arrives at `stage` of task discovery, and at no other stage."""
    real = working_set.budget_exhausted

    def exhausted(name: str, **kwargs) -> bool:
        if name == stage:
            request_budget.current().note_skipped(name)
            return True
        return real(name, **kwargs)

    monkeypatch.setattr(working_set, "budget_exhausted", exhausted)


def test_a_turn_whose_task_discovery_ran_out_of_time_reports_the_block_unavailable_not_empty(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the installed service a recall spent its request budget before task discovery ran, so the block had no
    pages to read and said `empty`: the agent read "no recorded position" where none had been looked for."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)
    _starve(monkeypatch, "working_set.task_context")

    token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
    try:
        packet = _activate(vault, turn=TURN, max_chars=8000)
    finally:
        request_budget.reset_current(token)

    assert packet["task_context"]["candidates"] == []
    assert packet["positions"] == {"state": "unavailable", "items": [], "overflow_refs": [], "omitted": 0, "reason": "budget"}


def test_a_turn_whose_task_discovery_stopped_early_reports_the_positions_it_read_as_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline cut the leads' links, so the linked page that holds the position was never read; the block
    said `empty`, as if the pages the turn reaches held no position."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)
    _starve(monkeypatch, "working_set.task_context_links")

    token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
    try:
        packet = _activate(vault, turn=TURN, max_chars=8000)
    finally:
        request_budget.reset_current(token)

    # Other leads were served, so the block read some pages: what it found is partial, not none.
    assert packet["task_context"]["candidates"]
    assert packet["positions"] == {"state": "partial", "items": [], "overflow_refs": [], "omitted": 0, "reason": "budget"}


def test_a_turn_whose_link_lookup_failed_reports_the_positions_it_read_as_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection graph was unavailable, so the linked page that holds the position was never read; the task
    context said `lane_failed` and the block still read as complete."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    def unavailable(*args, **kwargs):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(working_set, "read_connections", unavailable)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert {"role": "task_context", "reason": "lane_failed"} in packet["task_context"]["missing"]
    assert packet["positions"] == {"state": "partial", "items": [], "overflow_refs": [], "omitted": 0, "reason": "lane_failed"}


def test_a_turn_whose_link_reasons_could_not_be_read_reports_the_positions_it_read_as_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graph listed the link, but its reason could not be read, so the link and the linked page that holds the
    position were dropped after discovery; the block still read as complete."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)
    read_connections = working_set.read_connections

    def unreadable() -> str:
        raise egress.ReaderViewUnavailable("reader view unavailable")

    def with_unreadable_reasons(*args, **kwargs):
        return {
            seed: (tuple(replace(connection, build_why=unreadable) for connection in listed), omitted)
            for seed, (listed, omitted) in read_connections(*args, **kwargs).items()
        }

    monkeypatch.setattr(working_set, "read_connections", with_unreadable_reasons)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert {"role": "task_context", "reason": "lane_failed"} in packet["task_context"]["missing"]
    assert packet["positions"] == {"state": "partial", "items": [], "overflow_refs": [], "omitted": 0, "reason": "lane_failed"}


def test_a_turn_whose_collection_routing_could_not_be_read_reports_its_positions_as_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable manifest index read as "no collection to route to", so a lead only routing finds was missing
    and the block read as complete."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    def unreadable(*args, **kwargs):
        raise OSError("manifest index unreadable")

    monkeypatch.setattr(working_set_index, "records_manifests", unreadable)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert {"role": "task_context", "reason": "lane_failed"} in packet["task_context"]["missing"]
    assert packet["positions"]["state"] == "partial" and packet["positions"]["reason"] == "lane_failed"
    # The lanes that still ran served the lead and its link, so the position they reach is served.
    assert [item["ref"].rpartition("/")[2] for item in packet["positions"]["items"]] == ["studio-equipment-log.md#slow-cool"]


def test_a_position_on_a_superseded_page_is_not_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page its author retired for a newer one was served as a ready position, presenting a withdrawn view as current."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _write(
        vault, "Knowledge Base/Notes/Insights/studio-equipment-log.md",
        "---\ntype: insight\nstatus: superseded\nsuperseded_by: \"[[kiln-notes]]\"\n---\n\n# studio-equipment-log\n\n"
        "- [preference] I prefer an oxidation glaze firing for the kiln (2025-06-01) ^oxidation\n",
    )
    _page(vault, "kiln-notes", body="- [preference] I prefer a reduction glaze firing for the kiln (2026-02-01) ^reduction\n")
    _page(
        vault, "glaze-firing-schedule",
        body="Glaze firing schedule for the kiln: candle overnight, then ramp.\n\nSee [[studio-equipment-log]] and [[kiln-notes]].\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    served = [item["ref"].rpartition("/")[2] for item in packet["positions"]["items"]] + packet["positions"]["overflow_refs"]
    assert served == ["kiln-notes.md#reduction"]


def test_a_long_position_is_served_whole_beside_a_short_one_within_the_shipped_bound(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A careful causal attribution runs past the working set's per-unit cap; capped, it only ever became a ref."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    long_view = (
        "I attribute the crazing on the spring batch to the faster cool, not to the glaze recipe. "
        + "The same recipe fired on the slow schedule in March showed no crazing, the kiln log puts the cool below "
        "600 degrees at twice the usual rate, and the crazed pieces sat nearest the vent. " * 7
        + "This is my working view from the March and April firings and the kiln log, and it is not proven."
    )
    short_view = "I prefer cone 6 for the stoneware body"
    _lead_linking_to_a_long_page(
        vault, f"- [preference] {long_view} (2026-03-02) ^crazing\n\n- [preference] {short_view} (2026-03-01) ^cone-six\n",
    )
    _ready(vault)

    block = _activate(vault, turn=TURN, max_chars=8000)["positions"]

    assert len(long_view) > working_set.MAX_UNIT_HARD_CHARS
    assert sorted(item["text"] for item in block["items"]) == sorted([long_view, short_view])
    assert block["overflow_refs"] == []


def _reach_page_as_a_served_leads_link_would(monkeypatch: pytest.MonkeyPatch, page: str, after_compile=None) -> None:
    """This fixture's guest task context serves no leads, so `page` joins the reach as a served lead's link would.

    `after_compile` runs once the block is compiled and before terminal egress decides it.
    """
    compile_block = positions.compile_block

    def compile_with_page(root, caller, *, reach, **kwargs):
        block = compile_block(root, caller, reach={*reach, page}, **kwargs)
        if after_compile is not None:
            after_compile()
        return block

    monkeypatch.setattr(positions, "compile_block", compile_with_page)


def _eight_short_positions() -> tuple[list[str], str]:
    """Eight dated positions of about 97 characters each: any one fits a 600-character excerpt, all eight do not."""
    views = [f"I prefer cone {cone} for the stoneware body, view number {cone}, a fairly short line of text here to count" for cone in range(6, 14)]
    body = "".join(f"- [preference] {view} (2026-03-0{day}) ^cone-{day + 5}\n\n" for day, view in enumerate(views, start=1))
    return views, body


def test_a_caller_served_page_excerpts_gets_a_long_position_counted_not_named(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest whose ceiling serves a page as a 600-character excerpt was handed a 1,792-character unit of it whole.

    Its unit refs are dead ends: that caller reads a page's excerpt, never one of its units, so a left-out unit is counted.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    long_view = (
        "I attribute the crazing on the spring batch to the faster cool, not to the glaze recipe. "
        + "The same recipe fired on the slow schedule in March showed no crazing, and the crazed pieces sat nearest the vent. " * 12
        + "This is my working view from the spring firings, not yet tested."
    )
    short_view = "I prefer cone 6 for the stoneware body"
    _lead, excerpted = _lead_linking_to_a_long_page(
        vault, f"- [preference] {long_view} (2026-03-02) ^crazing\n\n- [preference] {short_view} (2026-03-01) ^cone-six\n",
    )
    _withhold(vault, excerpted, ceiling=egress.LEVEL_EXCERPT)
    _ready(vault)
    _reach_page_as_a_served_leads_link_would(monkeypatch, excerpted)

    owner = _activate(vault, turn=TURN, max_chars=8000)["positions"]
    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)["positions"]

    assert len(long_view) > 1_500 > egress.PAGE_EXCERPT_MAX
    assert sorted(item["text"] for item in owner["items"]) == sorted([long_view, short_view])
    assert [item["text"] for item in guest["items"]] == [short_view]
    assert guest["overflow_refs"] == []
    assert guest["omitted"] == 1
    assert positions.overflow_line([], 1) in hook._format_positions_block(guest).splitlines()


def test_several_short_positions_on_an_excerpted_page_share_its_excerpt_bound(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each of eight 97-character positions fit the 600-character excerpt bound alone; together they handed the guest 776 characters of the page."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    views, body = _eight_short_positions()
    _lead, excerpted = _lead_linking_to_a_long_page(vault, body)
    _withhold(vault, excerpted, ceiling=egress.LEVEL_EXCERPT)
    _ready(vault)
    _reach_page_as_a_served_leads_link_would(monkeypatch, excerpted)

    owner = _activate(vault, turn=TURN, max_chars=8000)["positions"]
    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)["positions"]

    assert sum(len(view) for view in views) > egress.PAGE_EXCERPT_MAX
    assert len(owner["items"]) == len(views)
    served = sum(len(item["text"]) for item in guest["items"])
    assert 0 < served <= egress.PAGE_EXCERPT_MAX
    assert len(guest["items"]) + guest["omitted"] == len(views)
    assert guest["overflow_refs"] == []


def test_a_page_the_caller_may_read_no_text_of_lists_no_unit_line_or_ref(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit on a page with a text bound of 0 was named by ref, a ref to a page whose text the caller may not read.

    Admission already withholds a page below the release floor, so a bound of 0 reaches the compile only when the two
    decisions part, as they can when a policy changes between them. The compile's own read of the bounds says 0 here.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    long_view = "I attribute the crazing on the spring batch to the faster cool, not to the glaze recipe. " * 40
    _lead, page = _lead_linking_to_a_long_page(
        vault, f"- [preference] {long_view} (2026-03-02) ^crazing\n\n- [preference] I prefer cone 6 (2026-03-01) ^cone-six\n",
    )
    _withhold(vault, page, ceiling=egress.LEVEL_FULL)
    _ready(vault)
    _reach_page_as_a_served_leads_link_would(monkeypatch, page)
    page_text_bounds, reads = egress.page_text_bounds, []

    def bounds(root, paths, **kwargs):
        reads.append(1)
        return dict.fromkeys(paths, 0) if len(reads) == 1 else page_text_bounds(root, paths, **kwargs)

    monkeypatch.setattr(egress, "page_text_bounds", bounds)

    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)

    assert reads
    # The guest's task discovery does not run in this fixture, so the one page read is only part of the reach.
    assert guest["task_context"]["state"] == "unavailable"
    assert guest["positions"] == {
        "state": "partial", "items": [], "overflow_refs": [], "omitted": 0, "reason": "task_context_unavailable",
    }


def test_a_policy_that_tightens_before_terminal_egress_does_not_serve_more_than_the_new_excerpt_bound(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block was compiled for a guest released the whole page, then the page's ceiling dropped to an excerpt."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    views, body = _eight_short_positions()
    _lead, page = _lead_linking_to_a_long_page(vault, body)
    _withhold(vault, page, ceiling=egress.LEVEL_FULL)
    _ready(vault)
    _reach_page_as_a_served_leads_link_would(monkeypatch, page, after_compile=lambda: _withhold(vault, page, ceiling=egress.LEVEL_EXCERPT))

    packet = _activate(vault, _external(), turn=TURN, max_chars=8000)

    assert packet["positions"]["state"] == "unavailable"
    assert packet["positions"]["items"] == []
    assert all(view not in json.dumps(packet) for view in views)


def test_a_count_of_units_left_out_of_a_page_the_policy_then_withholds_is_not_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit counted but not named has no served line, so the terminal recheck skipped its page and `omitted: 1` still said it existed.

    The one position here is longer than the guest's 600-character excerpt bound; the page's ceiling then drops to none.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    long_view = "I attribute the crazing on the spring batch to the faster cool, not to the glaze recipe. " * 12
    _lead, page = _lead_linking_to_a_long_page(vault, f"- [preference] {long_view} (2026-03-02) ^crazing\n")
    _withhold(vault, page, ceiling=egress.LEVEL_EXCERPT)
    _ready(vault)
    _reach_page_as_a_served_leads_link_would(monkeypatch, page, after_compile=lambda: _withhold(vault, page, ceiling=0))

    packet = _activate(vault, _external(), turn=TURN, max_chars=8000)

    assert len(long_view) > egress.PAGE_EXCERPT_MAX
    assert packet["positions"]["state"] == "unavailable"
    assert packet["positions"]["omitted"] == 0


def test_the_positions_block_leads_the_maps_response_and_opens_with_its_framing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later refactor that puts the block back at the end or mid-packet loses the first-answer effect measured in replays."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert "served_context_id" in packet
    assert list(packet)[0] == "positions"
    assert list(packet["positions"]) == ["about", "state", "items", "overflow_refs", "omitted"]
    # The packet's reported size is still the size of what is sent once the block has moved.
    sent = json.dumps(
        {**packet, "budget": {key: value for key, value in packet["budget"].items() if key != "serialized_chars"}},
        ensure_ascii=False, separators=(",", ":"),
    )
    assert packet["budget"]["serialized_chars"] == len(sent)


def test_the_block_that_is_served_asks_for_the_position_first_and_never_for_agreement(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverting the framing to the older passage still equals `positions.ABOUT`, so the served words are checked for the contract."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _lead_linking_to_a_long_page(vault, "- [preference] I prefer a slow cool below 600 degrees for celadon (2026-03-02) ^slow-cool\n")
    _ready(vault)

    about = _activate(vault, turn=TURN, max_chars=8000)["positions"]["about"]

    assert about == positions.ABOUT
    assert "open your first answer with it" in about
    assert "state the recorded position before you answer yes or no" in about
    # Replies softened a plain contradiction into "weakens" and kept the position alive; the passage must ask for the contradiction.
    assert "whether it supports the position, leaves it open or contradicts it" in about
    assert "When it contradicts the position, say that it does" in about
    assert "do not keep the position alive with a possibility the evidence gives no reason for" in about
    assert "agree" not in about.lower()


def test_a_vault_category_that_declares_the_trait_is_selected(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewed vault save is how a person marks their own position category; the projection must read it, not a name."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _write(
        vault, "Knowledge Base/_Schema/semantic-language-registry.yaml",
        "schema_version: 1\ncategories:\n  working_hypothesis:\n"
        "    description: A view the person holds provisionally.\n    traits: [position]\nkinds: {}\n",
    )
    hypothesis = "My working model is that the new shelf spacing, not the clay body, explains the uneven glaze; not proven"
    _lead_linking_to_a_long_page(
        vault, f"- [working_hypothesis] {hypothesis} (2026-03-02) ^spacing\n",
    )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert semantic_language_registry.load_registry(vault).categories_with("position") == {"preference", "working_hypothesis"}
    assert [(item["ref"].rpartition("/")[2], item["text"]) for item in packet["positions"]["items"]] == [
        ("studio-equipment-log.md#spacing", hypothesis),
    ]


def test_an_unknown_category_trait_is_refused_with_a_named_finding() -> None:
    """A misspelt trait would otherwise save cleanly and silently select nothing."""
    findings = validate_proposal({
        "schema_version": 1,
        "categories": {"working_hypothesis": {"description": "A provisional view.", "traits": ["positon"]}},
        "kinds": {},
    })

    assert [(item["code"], item["path"]) for item in findings] == [("unknown_trait", "categories.working_hypothesis.traits")]


def test_a_vault_adds_the_position_trait_to_a_core_category_through_the_governed_save(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault whose positions sit under the core `insight` category could not mark it: the entry was inert and refused."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    reading = "My reading is that the crawling comes from glaze applied too thick, not from dust; not proven"
    _lead_linking_to_a_long_page(vault, f"- [insights] {reading} (2026-03-02) ^crawling\n")
    with principal.library_scope():
        current = commands.op_schema_memory(vault, subject="categories", operation="inspect")
        saved = commands.op_schema_memory(
            vault, subject="categories", operation="save",
            proposal={"upsert": {"insight": {"attributes": {"traits": ["position"]}}}},
            expected_hash=current["content_hash"], why="my own readings are recorded as insights",
        )
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)

    assert saved.get("findings", []) == []
    written = yaml.safe_load((vault / "Knowledge Base/_Schema/semantic-language-registry.yaml").read_text(encoding="utf-8"))
    assert written["categories"] == {"insight": {"traits": ["position"]}}
    registry = semantic_language_registry.load_registry(vault)
    assert registry.findings == ()
    assert registry.categories_with("position") == {"preference", "insight"}
    assert registry.core_categories["insight"].description == "A non-obvious realization or interpretation."
    assert [(item["ref"].rpartition("/")[2], item["text"]) for item in packet["positions"]["items"]] == [
        ("studio-equipment-log.md#crawling", reading),
    ]


def test_a_core_category_entry_that_redefines_more_than_traits_keeps_the_rest_inert() -> None:
    """A hand edit must not rewrite a code-owned category's meaning, but the trait it adds still applies."""
    registry = semantic_language_registry.load_registry(proposal={
        "schema_version": 1,
        "categories": {"insight": {"description": "Anything I happen to think.", "traits": ["position"]}},
        "kinds": {},
    })

    assert [(item.code, item.path, item.severity) for item in registry.findings] == [
        ("core_category_shadowed", "categories.insight", "warning"),
    ]
    assert registry.resolve_category("insight").definition.description == "A non-obvious realization or interpretation."
    assert registry.categories_with("position") == {"preference", "insight"}


def test_an_unknown_trait_on_a_core_category_is_refused_with_a_named_finding() -> None:
    """A misspelt trait under a core key would otherwise save cleanly and silently select nothing."""
    findings = validate_proposal({"schema_version": 1, "categories": {"insight": {"traits": ["positon"]}}, "kinds": {}})

    assert [(item["code"], item["path"]) for item in findings] == [("unknown_trait", "categories.insight.traits")]


def test_the_served_record_names_the_positions_it_served_and_no_text(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The private record is how a later audit learns which positions reached the agent; it must never keep their prose."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    position = "I prefer a slow cool below 600 degrees for celadon"
    _lead_linking_to_a_long_page(vault, f"- [preference] {position} (2026-03-02) ^slow-cool\n")
    _ready(vault)

    packet = _activate(vault, turn=TURN, max_chars=8000)
    with principal.request_scope(OWNER):
        record = served_context.read(vault, served_context_id=packet["served_context_id"])["record"]

    assert record["positions"] == {
        "state": "ready", "refs": [item["ref"] for item in packet["positions"]["items"]], "overflow_refs": [],
    }
    assert len(record["positions"]["refs"]) == 1
    assert position not in json.dumps(record)


def test_a_record_from_before_positions_reads_as_unrecorded_not_empty(vault: Path) -> None:
    """An older row read as an empty block would claim the response served no positions."""
    packet = {"abstained": False, "map": {"generation": "map-one", "items": []}}
    with principal.request_scope(OWNER):
        before = served_context.append(vault, packet)
        with sqlite3.connect(served_context.sidecar_path(vault)) as connection:
            connection.execute("ALTER TABLE served_context DROP COLUMN positions")
        old = served_context.read(vault, served_context_id=before)["record"]
        after = served_context.append(vault, {**packet, "positions": {"state": "empty", "items": [], "overflow_refs": []}})
        new = served_context.read(vault, served_context_id=after)["record"]

    assert old["positions"] == {"state": "unknown", "reason": "not_recorded"}
    assert new["positions"] == {"state": "empty", "refs": [], "overflow_refs": []}


def test_the_hook_renders_every_served_position_whole_under_its_framing() -> None:
    """The hook cannot import the service; a line it renders longer than the service counted, or a lower cap, drops a served position."""
    block = {
        "state": "ready", "about": positions.ABOUT, "overflow_refs": ["exomem://vault/log.md#third"], "omitted": 2,
        "items": [
            {"ref": "exomem://vault/log.md#first", "text": "I prefer a slow cool below 600 degrees", "title": "Kiln log", "date": "2026-03-02"},
            {"ref": "exomem://vault/log.md#second", "text": "An undated view on shelf spacing", "title": "Kiln log", "review_due": "2026-01-01"},
        ],
    }

    text, *_ = hook._format_map_with_slice({"map": {"generation": "map", "ref": "exomem://map", "items": []}, "positions": block}, 200, "")

    lines = text.splitlines()
    assert f"- positions: {positions.ABOUT}" in lines
    assert all(positions.item_line(item) in lines for item in block["items"])
    assert positions.overflow_line(block["overflow_refs"], block["omitted"]) in lines
    assert hook._POSITIONS_MAX_CHARS == activation_conventions.BUDGET_BOUNDS["positions_max_chars"][1]


def test_the_hook_renders_the_positions_before_the_map_and_the_task_leads() -> None:
    """The service leads its response with the block; a hook that renders it after the map and the leads buries it again."""
    packet = {
        "map": {"generation": "map", "ref": "exomem://map", "items": [{"title": "Kiln log", "ref": "exomem://vault/log.md"}]},
        "task_context": {
            "state": "ready", "methods": {"reset": False, "items": []},
            "candidates": [{"ref": "exomem://vault/log.md", "source_ref": "exomem://vault/log.md", "title": "Kiln log", "reason": "Named.", "read_ref": "exomem://vault/log.md"}],
        },
        "positions": {
            "state": "ready", "about": positions.ABOUT, "overflow_refs": [], "omitted": 0,
            "items": [{"ref": "exomem://vault/log.md#first", "text": "I prefer a slow cool below 600 degrees", "title": "Kiln log", "date": "2026-03-02"}],
        },
    }

    text, *_ = hook._format_map_with_slice(packet, 4000, "")

    lines = text.splitlines()
    leading = next(index for index, line in enumerate(lines) if line.startswith("- positions:"))
    assert leading < next(index for index, line in enumerate(lines) if line.startswith("- map:"))
    assert leading < next(index for index, line in enumerate(lines) if line.startswith("- task lead:"))


def test_the_hook_says_when_the_positions_block_is_not_the_whole() -> None:
    """The hook dropped an unavailable block and a partial one without items, so a hook-fed agent read a position the
    service never read as none: the same text as a turn with no recorded position."""
    unread = {"state": "unavailable", "reason": "budget", "items": [], "overflow_refs": [], "omitted": 0}
    part = {
        "state": "partial", "reason": "lane_failed", "about": positions.ABOUT, "overflow_refs": [], "omitted": 0,
        "items": [{"ref": "exomem://vault/log.md#first", "text": "I prefer a slow cool below 600 degrees", "title": "Kiln log", "date": "2026-03-02"}],
    }
    none = {"state": "empty", "items": [], "overflow_refs": [], "omitted": 0}

    assert hook._format_positions_block(unread) == (
        "- positions unavailable (budget): a recorded position may exist that is not shown; search before you treat one as absent"
    )
    assert hook._format_positions_block(part).splitlines() == [
        f"- positions: {positions.ABOUT}",
        positions.item_line(part["items"][0]),
        "- positions partial (lane_failed): a recorded position may exist that is not shown; search before you treat one as absent",
    ]
    assert hook._format_positions_block(none) == ""
