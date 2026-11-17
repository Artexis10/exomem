"""The person's recorded positions reach the agent from the pages a turn's task context serves."""

import json
import sqlite3
from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _page, _write
from test_context_views import OWNER
from test_governance_egress import _external
from test_working_set_withheld_twins import _withhold

from exomem import (
    activation_conventions,
    lexstore,
    positions,
    semantic_language_registry,
    served_context,
)
from exomem._hooks import exomem_retrieve_nudge as hook
from exomem.governance import principal
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
