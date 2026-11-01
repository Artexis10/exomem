"""The turn slice lists what the turn's words reached, beside a map that no turn changes."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conversation_vault import seed as seed_conversation_vault
from test_activate_context_maps import _activate
from test_context_catalogue import INSIGHTS, _built, _seed, _write
from test_context_views import OWNER, _cold, _second_vault
from test_governance_egress import _external
from test_working_set_index import _seed_structure
from test_working_set_withheld_twins import _withhold

from exomem import (
    bm25,
    commands,
    context_views,
    lexstore,
    working_set,
    working_set_heat,
    working_set_resolve,
    working_set_runtime,
)
from exomem.governance import egress, principal

KB = "Knowledge Base"
CARGO_SLED = f"{KB}/Products/Cargo Sled.md"
HIDDEN_SLED = f"{KB}/Systems/Cargo Sled.md"
HIDDEN_NOTE = f"{KB}/Notes/Insights/cargo-sled-winter-hazards.md"
SECRET = f"{KB}/Notes/Restricted/secret-cluster.md"
SLED_TURN = "What are the Cargo Sled constraints?"
IRRELEVANT_TURN = "What is a good way to spend a quiet afternoon?"
NAMING_TURN = "What are the Cargo Sled constraints? Also the Cargo Sled winter hazards."
POINTING_TURN = "what are the risks with that?"


def _structured(vault: Path) -> None:
    _seed_structure(vault)
    _built(vault).close()
    lexstore.ensure_fresh(vault)


def _with_hidden_pages(root: Path) -> None:
    """A withheld page that shares a visible anchor's name, and a withheld page that the turn names by title."""
    _write(root, HIDDEN_SLED, "---\ntype: note\nstatus: active\nupdated: 2026-09-04\n---\n\n# Cargo Sled\n\n## Summary\n\nA second sled.\n")
    _write(root, HIDDEN_NOTE, "---\ntype: insight\nstatus: active\nupdated: 2026-09-05\n---\n\n# Cargo Sled winter hazards\n\n## Summary\n\nIce on the runners.\n")


def test_the_slice_lists_what_the_turn_reached(vault: Path) -> None:
    """A turn that resolves an anchor gets it as a lead, with how it was reached and the evidence behind it."""
    _structured(vault)
    packet = _activate(vault, turn=SLED_TURN)
    block = packet["turn_slice"]
    assert block["state"] == "ready"
    assert block["description"] == context_views.SLICE_DESCRIPTION
    (item,) = [item for item in block["items"] if item["kind"] == "anchor"]
    assert item["ref"] == CARGO_SLED
    assert item["match"] == "resolved"
    assert item["evidence"] and set(item["evidence"]) <= set(working_set_resolve.EVIDENCE_KINDS)
    assert item["profile"] == context_views.anchor_profile_ref(CARGO_SLED)
    assert "A towed cargo sled" in item["description"]
    assert packet["map"]["items"]
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_an_irrelevant_turn_gets_an_empty_slice_and_the_same_map(vault: Path) -> None:
    """The turn changes the slice alone: not the map, its generation or the continuity token."""
    _structured(vault)
    resolving = _activate(vault, turn=SLED_TURN)
    irrelevant = _activate(vault, turn=IRRELEVANT_TURN)
    blank = _activate(vault, turn="")
    assert resolving["turn_slice"]["state"] == "ready"
    for packet in (irrelevant, blank):
        assert packet["turn_slice"]["state"] == "empty"
        assert packet["turn_slice"]["items"] == []
        assert "description" not in packet["turn_slice"]
        assert packet["map"] == resolving["map"]
        assert packet["continuity"] == resolving["continuity"]
        assert packet["generation"]["map_generation"] == resolving["generation"]["map_generation"]


def test_a_withheld_namesake_and_titled_page_change_no_byte_for_a_restricted_caller(vault: Path, tmp_path: Path) -> None:
    """A withheld anchor of a visible anchor's name must not turn that anchor into a question, and the
    withheld page that the turn names by title must not become a lead: the packet is the twin's packet."""
    twin = _second_vault(tmp_path)
    for root, hidden in ((vault, True), (twin, False)):
        _seed_structure(root)
        if hidden:
            _with_hidden_pages(root)
        _withhold(root, HIDDEN_SLED, HIDDEN_NOTE, ceiling=0)
        _built(root).close()
        lexstore.ensure_fresh(root)
    owners, guests = [], []
    for root in (vault, twin):
        _cold()
        owners.append(_activate(root, OWNER, turn=NAMING_TURN))  # fills the owner caches before a restricted read
        guests.append(_activate(root, _external(), turn=NAMING_TURN))
    assert json.dumps(guests[0], sort_keys=True) == json.dumps(guests[1], sort_keys=True)
    visible = [item["ref"] for item in guests[0]["turn_slice"]["items"]]
    assert visible == [CARGO_SLED]  # the control: the guest does reach the anchor that it may see
    assert owners[0]["turn_slice"] != owners[1]["turn_slice"]  # the control: the owner's slice does see the extra pages
    assert HIDDEN_SLED in json.dumps(owners[0]["turn_slice"])
    assert HIDDEN_SLED not in json.dumps(guests[0]) and HIDDEN_NOTE not in json.dumps(guests[0])


#: Withheld notes that repeat the turn's words, so withheld pages would move the slice's lexical statistics.
HAULING_LOGS = tuple(f"{KB}/Notes/Hauling/hauling-log-{index:02d}.md" for index in range(20))
#: A turn with more distinct words than the lexical stage keeps, so how rare each word is decides which ones it asks.
LONG_TURN = (
    "Does Marit Solheim's freight coordinator ledger for the northern corridor depot track the cargo sled stock"
    " and the winter schedule owner?"
)


def _hauling_twins(vault: Path, tmp_path: Path) -> tuple[Path, Path]:
    """`vault` with the twenty withheld hauling logs, and its twin without them, both past the lexical corpus floor."""
    from test_working_set_carry import seed_ordinary_notes

    twin = _second_vault(tmp_path)
    for root, hidden in ((vault, True), (twin, False)):
        _seed_structure(root)
        seed_ordinary_notes(root)
        if hidden:
            for index, path in enumerate(HAULING_LOGS):
                _write(root, path, (
                    f"---\ntype: note\nstatus: active\nupdated: 2026-08-02\n---\n\n# Hauling log {index:02d}\n\n"
                    f"## Summary\n\n- [note] The cargo sled went out on the runners again. ^h-{index}\n"
                ))
        _withhold(root, *HAULING_LOGS, ceiling=0)
        _built(root).close()
        lexstore.ensure_fresh(root)
    return vault, twin


def test_withheld_pages_move_no_lexical_statistic_of_a_restricted_callers_slice(vault: Path, tmp_path: Path) -> None:
    """Twenty withheld notes that say "cargo sled", in a vault past the lexical corpus floor, change no byte of
    a restricted caller's packet: its slice ranks and selects the turn's words over the pages it may see."""
    vault, twin = _hauling_twins(vault, tmp_path)
    guests = []
    for root in (vault, twin):
        _cold()
        guests.append(_activate(root, _external(), turn=LONG_TURN))
    assert json.dumps(guests[0], sort_keys=True) == json.dumps(guests[1], sort_keys=True)
    # The control: leads whose evidence the statistics decide.
    assert any("retrieval" in item["evidence"] for item in guests[0]["turn_slice"]["items"])


def full_compile_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compile the turn slice with every lane of the anchors compile, which the leads-only compile must agree with."""
    serve = working_set_runtime.serve
    monkeypatch.setattr(working_set_runtime, "serve", lambda *args, **kwargs: serve(*args, **{**kwargs, "leads_only": False}))


#: A thread the compile must read: it qualifies the long turn's lead, and it carries the pointing turn.
SLED_THREAD = {"recent": [{"role": "user", "text": "Is the Cargo Sled still rated for winter?"}]}


@pytest.mark.parametrize(
    "cases",
    [
        pytest.param(((SLED_TURN, None), (LONG_TURN, None), (IRRELEVANT_TURN, None)), id="turn_alone"),
        pytest.param(((LONG_TURN, SLED_THREAD), (POINTING_TURN, SLED_THREAD)), id="with_thread"),
    ],
)
def test_the_leads_only_compile_gives_the_full_compiles_slice_on_the_lexical_twins(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: tuple,
) -> None:
    """The slice skips the resolved anchors' lanes, whose material it never serves. Every lead, match, evidence
    kind and state is still the one the full compile gives, for the owner and a restricted caller on both twins."""
    roots = _hauling_twins(vault, tmp_path)
    lanes = working_set.run_lanes
    runs: list[int] = []

    def counted(*args, **kwargs):
        runs[-1] += 1
        return lanes(*args, **kwargs)

    monkeypatch.setattr(working_set, "run_lanes", counted)
    for root in roots:
        for caller in (OWNER, _external()):
            for turn, conversation in cases:
                _cold()
                runs[:] = [0]
                leads_only = _activate(root, caller, turn=turn, conversation=conversation)["turn_slice"]
                with monkeypatch.context() as scope:
                    full_compile_slices(scope)
                    _cold()
                    runs.append(0)
                    full = _activate(root, caller, turn=turn, conversation=conversation)["turn_slice"]
                assert leads_only == full, (root.name, turn, conversation)
                if turn == SLED_TURN:
                    # The control: the turn resolves an anchor, whose lanes only the full compile runs.
                    assert full["state"] == "ready" and runs[0] < runs[1], (root.name, runs, full)
                if turn == POINTING_TURN:
                    # The control: only the thread says what the turn points at, so the leads-only compile read it.
                    assert [(item["ref"], item["evidence"]) for item in leads_only["items"]] == [(CARGO_SLED, ["conversation"])]


def full_admitted_rankings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rank every admitted query from each candidate's own text, the full ranking that kept postings must equal."""
    monkeypatch.setattr(bm25.CorpusPostings, "kept", lambda self, words: {})
    monkeypatch.setattr(bm25.CorpusPostings, "counted", lambda self, *args: None)


def postings_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A list that grows by one each time an admitted ranking scores from postings."""
    reads: list[int] = []
    documents = bm25.CorpusPostings.documents
    monkeypatch.setattr(bm25.CorpusPostings, "documents", lambda self, *args: reads.append(1) or documents(self, *args))
    return reads


def test_kept_postings_give_a_restricted_caller_the_full_rankings_slice_on_the_lexical_twins(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restricted caller's slice ranks the turn's words from kept postings. Its leads, their match and evidence,
    and its state are the ones that ranking each candidate's text gives, on both twins and for every turn."""
    reads = postings_reads(monkeypatch)
    for root in _hauling_twins(vault, tmp_path):
        for turn in (SLED_TURN, LONG_TURN, IRRELEVANT_TURN):
            _cold()
            pruned = _activate(root, _external(), turn=turn)["turn_slice"]
            with monkeypatch.context() as scope:
                full_admitted_rankings(scope)
                _cold()
                full = _activate(root, _external(), turn=turn)["turn_slice"]
            assert pruned == full, (root.name, turn)
    assert reads  # the control: the pruned slices were ranked from postings


def _guarded_world(vault: Path) -> None:
    """The structured vault plus a page that a restricted caller's view withholds."""
    _structured(vault)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# Restricted fixture\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault).close()


# The guard restores the items and the block's own fields separately; one forgery for each.
@pytest.mark.parametrize("forgery", ["item path", "block state"])
def test_the_guard_serves_the_compiled_slice_and_not_a_forged_one(vault: Path, forgery: str) -> None:
    """A client-supplied or tampered slice never reaches the wire: the guard restores what this invocation compiled."""
    _guarded_world(vault)
    secret = SECRET
    with principal.request_scope(_external()), principal.invocation_scope():
        packet = _activate(vault, _external(), turn=SLED_TURN)
        original = copy.deepcopy(packet["turn_slice"])
        assert original["state"] == "ready" and original["items"]
        block = packet["turn_slice"]
        if forgery == "item path":
            block["items"].append({"kind": "note", "path": secret, "ref": secret, "title": "Restricted fixture", "match": "resolved", "evidence": []})
        else:
            block["state"] = "empty"
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert guarded["turn_slice"] == original
    assert secret not in json.dumps(guarded)


def test_a_slice_with_no_record_is_replaced_and_leaves_the_map(vault: Path) -> None:
    """An unknown generation is an `unavailable` slice, and the map beside it is served as compiled."""
    _guarded_world(vault)
    with principal.request_scope(_external()), principal.invocation_scope():
        packet = _activate(vault, _external(), turn=SLED_TURN)
        compiled_map = copy.deepcopy(packet["map"])
        packet["turn_slice"]["generation"] = "0" * 32
        guarded = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert guarded["turn_slice"] == {"state": "unavailable", "items": []}
    assert guarded["map"] == compiled_map and not guarded.get("abstained")
    assert guarded["budget"]["used_chars"] == context_views.map_packet_chars(compiled_map)


def test_a_slice_compiled_for_a_larger_packet_is_never_served(vault: Path) -> None:
    """A 500-character packet carries the slice that the same request compiled for 8000 characters."""
    _guarded_world(vault)
    with principal.request_scope(_external()), principal.invocation_scope():
        small = _activate(vault, _external(), turn=SLED_TURN, max_chars=500)
        large = _activate(vault, _external(), turn=SLED_TURN, max_chars=8000)
        assert large["turn_slice"]["items"] and not small["turn_slice"]["items"]  # the control: two different slices
        forged = copy.deepcopy(small)
        forged["turn_slice"] = copy.deepcopy(large["turn_slice"])
        compiled_map = copy.deepcopy(small["map"])
        guarded = egress.guard_working_set(vault, forged, egress.AnnotatedHits(hits=[]))
    assert guarded is not None
    assert guarded["turn_slice"] == {"state": "unavailable", "items": []}
    assert guarded["map"] == compiled_map


def test_a_slice_beside_no_map_is_never_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard restores a slice only beside the map compiled with it, so a slice put on an anchors packet is dropped."""
    _guarded_world(vault)
    with principal.request_scope(_external()), principal.invocation_scope():
        compiled = _activate(vault, _external(), turn=SLED_TURN)["turn_slice"]
        monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "anchors")
        packet = _activate(vault, _external(), turn=SLED_TURN)
        assert "map" not in packet and compiled["items"]  # the control: an anchors packet, and a slice with leads
        guarded = egress.guard_working_set(vault, {**packet, "turn_slice": compiled}, egress.AnnotatedHits(hits=[]))
    assert guarded is not None and "turn_slice" not in guarded


def test_a_slice_that_cannot_be_built_keeps_the_map(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The map compiles first and at a fixed share, so a slice that fails sets its own state and nothing else."""
    _structured(vault)
    expected = _activate(vault, turn=IRRELEVANT_TURN)
    _cold()

    def broken(*args, **kwargs):
        raise RuntimeError("the anchors compile failed")

    monkeypatch.setattr(commands, "_compile_guarded_activation", broken)
    failed = _activate(vault, turn=SLED_TURN)
    assert failed["turn_slice"]["state"] == "unavailable" and failed["turn_slice"]["items"] == []
    assert failed["map"] == expected["map"]
    assert failed["continuity"] == expected["continuity"]
    assert failed["budget"]["used_chars"] == context_views.map_packet_chars(failed["map"])


def test_a_compile_that_abstains_inside_serve_is_an_unavailable_slice(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` answers a failed compile with an abstained packet; the slice reports the fault, not a turn that reached nothing."""
    _structured(vault)
    expected = _activate(vault, turn=IRRELEVANT_TURN)
    _cold()

    def broken(*args, **kwargs):
        raise RuntimeError("the compile failed")

    monkeypatch.setattr(working_set, "compile_packet", broken)
    failed = _activate(vault, turn=SLED_TURN)
    assert failed["turn_slice"]["state"] == "unavailable" and failed["turn_slice"]["items"] == []
    assert failed["map"] == expected["map"]


def test_a_slice_that_a_warming_band_left_empty_says_so(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The compile's evidence states ride on the slice, so an empty slice from a warming encoder is not a silent `empty`."""
    _structured(vault)
    monkeypatch.setattr(working_set, "signature_evidence", lambda index, turn: ({}, "warming", None))
    block = _activate(vault, turn=IRRELEVANT_TURN)["turn_slice"]
    assert block["state"] == "empty"
    assert block["semantic_evidence"] == "warming" and block["lexical_evidence"] == "available"


def test_a_compile_that_runs_out_of_budget_inside_serve_is_a_skipped_slice(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage inside the compile that the request budget cannot start skips the slice, as a stage before it does."""
    from exomem import request_budget

    _structured(vault)
    expected = _activate(vault, turn=IRRELEVANT_TURN)
    _cold()

    def exhausted(*args, **kwargs):
        # What `budget_exhausted` does at a compile stage boundary: record the skip, then stop.
        request_budget.current().note_skipped("working_set.semantic")
        raise working_set.BudgetExhausted("working_set.semantic")

    monkeypatch.setattr(working_set, "compile_packet", exhausted)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
    try:
        skipped = _activate(vault, turn=SLED_TURN)
    finally:
        request_budget.reset_current(token)
    assert skipped["turn_slice"]["state"] == "skipped" and skipped["turn_slice"]["items"] == []
    assert skipped["map"] == expected["map"]


def test_a_slice_that_runs_out_of_budget_is_skipped_and_keeps_the_map(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import request_budget

    _structured(vault)
    expected = _activate(vault, turn=IRRELEVANT_TURN)
    _cold()
    exhausted = working_set.budget_exhausted

    def release_exhausted(stage, *args, **kwargs):
        if stage != "working_set.release":
            return exhausted(stage, *args, **kwargs)
        request_budget.current().note_skipped(stage)
        return True

    monkeypatch.setattr(working_set, "budget_exhausted", release_exhausted)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
    try:
        skipped = _activate(vault, turn=SLED_TURN)
    finally:
        request_budget.reset_current(token)
    assert skipped["turn_slice"]["state"] == "skipped" and skipped["turn_slice"]["items"] == []
    assert skipped["map"] == expected["map"]


def test_a_slice_that_spends_the_request_budget_leaves_the_map_its_guard(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A synchronous slice stage can overrun its deadline; the built map still reaches the final guard."""
    from types import SimpleNamespace

    from exomem import request_budget

    _structured(vault)
    expected = _activate(vault, turn=IRRELEVANT_TURN)
    _cold()
    clock = [1000.0]
    monkeypatch.setattr(request_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(*args, **kwargs):
        clock[0] += 5.5
        working_set.budget_exhausted("working_set.semantic")
        raise working_set.BudgetExhausted("working_set.semantic")

    monkeypatch.setattr(working_set, "compile_packet", slow)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=5))
    try:
        packet = _activate(vault, turn=SLED_TURN)
    finally:
        request_budget.reset_current(token)
    assert not packet.get("abstained")
    assert packet["turn_slice"]["state"] == "skipped" and packet["map"] == expected["map"]


def test_a_carry_that_finds_the_budget_spent_skips_the_slice_and_names_the_stage(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline that passes while the carry runs ends the carry: the slice is `skipped`, the response and the
    ledger's reading of it name the cause, and the map is the one the unhurried call gives."""
    from types import SimpleNamespace

    from test_working_set_carry import (
        CARRY_PAGE,
        CARRY_TURN,
        _seed_carry_pages,
        seed_ordinary_notes,
    )

    from exomem import activation_result, request_budget

    _seed_carry_pages(vault)
    seed_ordinary_notes(vault)
    _structured(vault)
    _cold()
    clock = [1000.0]
    rare_terms = working_set_runtime.rare_turn_terms

    def spend_the_budget(*args, **kwargs):
        result = rare_terms(*args, **kwargs)
        clock[0] += 60.0  # the deadline passes during the carry's first lexical pass
        return result

    with monkeypatch.context() as patched:
        patched.setattr(request_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        patched.setattr(working_set_runtime, "rare_turn_terms", spend_the_budget)
        token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
        try:
            packet = _activate(vault, turn=CARRY_TURN)
        finally:
            request_budget.reset_current(token)
    assert not packet.get("abstained")
    assert packet["turn_slice"]["state"] == "skipped" and packet["turn_slice"]["items"] == []
    assert working_set.CARRY_STAGE in packet["request_budget"]["skipped"]
    observed = activation_result.Observation()
    observed.capture(packet)
    assert observed.result["budget_limited"] is True
    # The control: the same call with time to spare names the carried page, under the same map.
    _cold()
    unhurried = _activate(vault, turn=CARRY_TURN)
    assert [item["ref"] for item in unhurried["turn_slice"]["items"]] == [CARRY_PAGE]
    assert packet["map"] == unhurried["map"]


@pytest.mark.parametrize(
    ("caller", "hidden_target", "topics"),
    [(OWNER, False, ["harbour"]), (_external(), False, ["harbour"]), (_external(), True, [])],
)
def test_slice_leads_keep_admitted_relation_topics(vault: Path, caller, hidden_target: bool, topics: list[str]) -> None:
    """A lead inherits one admitted target's declarations; the target and further hops stay out of the slice."""
    _seed(vault)
    target = f"{INSIGHTS}/mooring-lines.md"
    lead = f"{INSIGHTS}/dock-roster.md"
    chain = f"{INSIGHTS}/chain-first.md"
    if hidden_target:
        _withhold(vault, target, ceiling=0)
    _built(vault).close()

    with principal.request_scope(caller), principal.invocation_scope():
        # Prime the owner's map cache with the same slice reserve, so its leads still receive the full caller view.
        context_views.activation_packet(
            vault, 4000, generation={}, turn_leads=lambda _caller: context_views.TurnLeads("empty"),
        )
        packet = context_views.activation_packet(
            vault, 4000, generation={},
            turn_leads=lambda _caller: context_views.TurnLeads("ready", (
                context_views.Lead(lead, "named", ()), context_views.Lead(chain, "named", ()),
            )),
        )
        packet = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))

    items = {item["path"]: item for item in packet["turn_slice"]["items"]}
    assert set(items) == {lead, chain}
    # An item with no topics omits the field.
    assert items[lead].get("topics", []) == topics
    assert "topics" not in items[chain]


def test_a_project_domain_edit_updates_a_warm_slice(vault: Path) -> None:
    """A cached map must not keep a lead's old membership after the project registry changes."""
    _seed(vault)
    _built(vault).close()
    lead = f"{INSIGHTS}/tide-work.md"

    def slice_topics() -> list[str]:
        with principal.request_scope(OWNER), principal.invocation_scope():
            packet = context_views.activation_packet(
                vault, 4000, generation={},
                turn_leads=lambda _caller: context_views.TurnLeads("ready", (
                    context_views.Lead(lead, "named", ()),
                )),
            )
            packet = egress.guard_working_set(vault, packet, egress.AnnotatedHits(hits=[]))
        (item,) = packet["turn_slice"]["items"]
        return item.get("topics", [])

    assert slice_topics() == slice_topics() == ["harbour"]
    registry = vault / KB / "_Schema/project-keys.yaml"
    registry.write_text(registry.read_text(encoding="utf-8").replace("domain: harbour", "domain: fleet"), encoding="utf-8")
    assert slice_topics() == slice_topics() == ["fleet"]


def test_a_budget_too_small_for_one_slice_line_holds_none_of_it_back(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where the slice's share cannot hold one line, the map compiles into the whole budget, as with the slice off."""
    _structured(vault)
    # Short, recent anchor lines lead the map, so a budget less the slice's share lists fewer of them.
    for name in ("Arve Lund", "Brit Haug", "Cato Rygg", "Dag Moen", "Eli Vik", "Frode Nes", "Gro Tveit", "Hallvard Bu"):
        _write(vault, f"{KB}/Entities/People/{name}.md", f"---\ntype: entity\nentity_type: person\nstatus: active\nupdated: 2026-09-30\n---\n\n# {name}\n")
    _built(vault).close()
    sliced = _activate(vault, turn=SLED_TURN, max_chars=500)
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    _cold()
    alone = _activate(vault, turn=SLED_TURN, max_chars=500)
    assert alone["map"].get("overflow")  # the control: the map has more than it can list, so a smaller budget lists less
    assert sliced["turn_slice"]["state"] == "skipped" and sliced["turn_slice"]["items"] == []
    assert sliced["map"] == alone["map"]


@pytest.mark.parametrize("max_chars", [500, 1000, 2000, 4000])
def test_the_packet_stays_within_its_limit_at_every_budget(vault: Path, max_chars: int) -> None:
    """The map and the slice share one limit, and the report charges both: even at the smallest budget."""
    _structured(vault)
    for caller in (OWNER, _external()):
        packet = _activate(vault, caller, turn=NAMING_TURN, max_chars=max_chars)
        used, limit = packet["budget"]["used_chars"], packet["budget"]["limit_chars"]
        assert limit == max_chars and used <= limit
        assert used == context_views.packet_chars(packet["map"], packet["turn_slice"])


def test_the_setting_off_restores_the_map_alone(vault: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """`off` is the rollback: no slice, no resolver, and the map compiles at the whole limit."""
    _structured(vault)
    with_slice = _activate(vault, turn=SLED_TURN)
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    _cold()
    off = _activate(vault, turn=SLED_TURN, include_timings=True)
    assert "turn_slice" not in off
    assert not any(stage.startswith("working_set.lexical") for stage in off["timings"]["stages"])
    assert off["budget"]["used_chars"] == context_views.map_packet_chars(off["map"])
    assert len(off["map"]["items"]) >= len(with_slice["map"]["items"])
    # An invalid value is not the rollback: it serves the slice and says so once.
    monkeypatch.setattr(working_set_runtime, "_SLICE_WARNING_EMITTED", False)
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "maybe")
    _cold()
    assert _activate(vault, turn=SLED_TURN)["turn_slice"]["state"] == "ready"
    _activate(vault, turn=SLED_TURN)
    assert sum("invalid EXOMEM_ACTIVATION_TURN_SLICE" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("setting", ["on", "off"])
def test_the_anchors_packet_ignores_the_setting(vault: Path, monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    _structured(vault)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "anchors")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", setting)
    packet = _activate(vault, turn=SLED_TURN)
    assert "turn_slice" not in packet and "map" not in packet
    assert [anchor["ref"] for anchor in packet["anchors"]] == [CARGO_SLED]


def test_due_advice_rides_on_a_resolved_lead(vault: Path) -> None:
    """A due prediction on the page that the turn resolved is raised for a maps user, as it is for an anchors user."""
    from exomem import due_state, working_set_index

    _structured(vault)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    page = vault / CARGO_SLED
    page.write_text(
        page.read_text(encoding="utf-8").replace("updated: 2026-09-02", "created: 2026-01-01\nupdated: 2026-01-01")
        + f"\n## Prediction\n\n- id: p1\n- check_by: {yesterday}\n\nThe sled is rated for 400 kg all winter.\n",
        encoding="utf-8",
    )
    due_state.reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _cold()
    due = [row for row in due_state.served_entries(vault) if row["path"] == CARGO_SLED]
    assert len(due) == 1, "the fixture page must owe one due item"
    packet = _activate(vault, turn=SLED_TURN)
    assert [item["match"] for item in packet["turn_slice"]["items"] if item["ref"] == CARGO_SLED] == ["resolved"]
    assert packet["relevant_due_refs"] == [{
        "ref": due[0]["ref"], "fingerprint": due[0]["fingerprint"],
        "category": due[0]["category"], "due_since": due[0]["due_since"], "anchor": CARGO_SLED,
    }]
    # The control: a turn that reaches nothing raises nothing.
    assert "relevant_due_refs" not in _activate(vault, turn=IRRELEVANT_TURN)


# --------------------------------------------------------------------------- #
# The conversation
# --------------------------------------------------------------------------- #

OTTILIE = f"{KB}/Entities/People/Ottilie Marsh.md"
GRANT_HUB = f"{KB}/Notes/Insights/tidewater-grant-hub.md"
#: Reaches Ottilie Marsh by her alias, and the Tidewater Grant hub only by the rare word "grant".
GRANT_TURN = "Ottilie asked about the grant"
GRANT_ENTRY = {"recent": [{"role": "user", "text": "we finished the Tidewater Grant call"}]}
#: The thread below names Ottilie Marsh, whom `POINTING_TURN` points back at.
POINTING_THREAD = {"recent": [{"role": "user", "text": "How did Ottilie Marsh do in the spring?"}]}


def _conversation_world(root: Path) -> None:
    seed_conversation_vault(root)
    _built(root).close()
    lexstore.ensure_fresh(root)


LEDGER = f"{KB}/Entities/Projects/Pelican Ledger.md"
LEDGER_NOTES = f"{KB}/Notes/Insights/pelican-ledger-notes.md"
#: An anchor and a titled page that a restricted caller may not see, both named by the thread below.
WITHHELD_THREAD = {
    "recent": [
        {"role": "user", "text": "How did Ottilie Marsh do in the spring?"},
        {"role": "user", "text": "we finished the Tidewater Grant call"},
        {"role": "user", "text": "is the Pelican Ledger notes page up to date for the Pelican Ledger?"},
    ],
    "refs": [LEDGER, LEDGER_NOTES],
}


def _conversation_twins(vault: Path, tmp_path: Path) -> tuple[Path, Path]:
    """`vault` with the withheld ledger anchor and page, and its twin without them."""
    twin = _second_vault(tmp_path)
    for root, hidden in ((vault, True), (twin, False)):
        seed_conversation_vault(root)
        if hidden:
            _write(root, LEDGER, "---\ntype: entity\nentity_type: project\nstatus: active\nupdated: 2026-09-01\n---\n\n# Pelican Ledger\n\n## Summary\n\nThe ledger of the survey boats.\n")
            _write(root, LEDGER_NOTES, "---\ntype: insight\nstatus: active\nupdated: 2026-09-05\n---\n\n# Pelican Ledger notes\n\n## Summary\n\nWhat the boat ledger holds.\n")
        _withhold(root, LEDGER, LEDGER_NOTES, ceiling=0)
        _built(root).close()
        lexstore.ensure_fresh(root)
    return vault, twin


def _leads(packet: dict) -> dict[str, dict]:
    return {item["ref"]: item for item in packet["turn_slice"]["items"]}


def test_the_conversation_promotes_a_lead_the_turn_reached_by_one_contact_kind(vault: Path) -> None:
    """The earlier entry names the Tidewater Grant hub; the follow-up reaches it only by the word "grant"."""
    _conversation_world(vault)
    alone = _leads(_activate(vault, turn=GRANT_TURN))
    assert alone[GRANT_HUB]["match"] == "partial" and "conversation" not in alone[GRANT_HUB]["evidence"]
    with_thread = _leads(_activate(vault, turn=GRANT_TURN, conversation=GRANT_ENTRY))
    assert with_thread[GRANT_HUB]["match"] == "resolved"
    assert "conversation" in with_thread[GRANT_HUB]["evidence"]
    assert alone[OTTILIE]["match"] == with_thread[OTTILIE]["match"] == "resolved"  # the control: the turn's own lead stays


def test_an_anaphoric_follow_up_is_carried_from_the_newest_earlier_user_entry(vault: Path) -> None:
    """The turn has no words of its own subject: only the thread says which page it points at."""
    _conversation_world(vault)
    alone = _activate(vault, turn=POINTING_TURN)
    assert alone["turn_slice"]["state"] == "empty"
    carried = _activate(vault, turn=POINTING_TURN, conversation=POINTING_THREAD)
    (lead,) = carried["turn_slice"]["items"]
    assert (lead["ref"], lead["match"], lead["evidence"]) == (OTTILIE, "partial", ["conversation"])
    assert carried["map"] == alone["map"]  # the thread changes the slice alone
    assert lead["origin"] == "conversation"


@pytest.mark.parametrize("trigger", ["user_turn", "task_refresh"])
def test_a_lead_that_only_the_agents_focus_reached_says_so(vault: Path, trigger: str) -> None:
    """The agent's focus is not the user's words: a lead it alone reached carries `origin: focus`, as an
    anchors packet's anchor does, and a lead the turn's own words reached carries no origin."""
    _conversation_world(vault)
    turn = "thoughts on this?"
    assert _activate(vault, turn=turn)["turn_slice"]["state"] == "empty"
    focused = _activate(vault, turn=turn, conversation={"focus": "Ottilie Marsh"}, trigger=trigger)
    (lead,) = focused["turn_slice"]["items"]
    assert (lead["ref"], lead["origin"]) == (OTTILIE, "focus")
    # The control: the user naming her gives a lead with no origin.
    (own,) = _activate(vault, turn="thoughts on Ottilie Marsh?")["turn_slice"]["items"]
    assert own["ref"] == OTTILIE and "origin" not in own


@pytest.mark.parametrize("turn", [f"{GRANT_TURN} and the ledger", POINTING_TURN])
def test_a_conversation_naming_withheld_pages_changes_no_byte_for_a_restricted_caller(
    vault: Path, tmp_path: Path, turn: str,
) -> None:
    """The thread names a withheld anchor and a withheld titled page in its text and its refs. A restricted
    caller's packet is its twin's, for a follow-up that the thread qualifies and for one that it carries."""
    roots = _conversation_twins(vault, tmp_path)
    owners, guests = [], []
    for root in roots:
        _cold()
        owners.append(_activate(root, OWNER, turn=turn, conversation=WITHHELD_THREAD))  # fills the owner caches first
        guests.append(_activate(root, _external(), turn=turn, conversation=WITHHELD_THREAD))
    assert json.dumps(guests[0], sort_keys=True) == json.dumps(guests[1], sort_keys=True)
    assert guests[0]["turn_slice"]["items"]  # the control: the guest does reach what it may see through the thread
    assert owners[0]["turn_slice"] != owners[1]["turn_slice"]  # the control: the owner's slice does see the extra pages
    assert LEDGER in json.dumps(owners[0]["turn_slice"])
    assert "Pelican" not in json.dumps(guests[0])


def _heat_store(vault: Path) -> list[str] | None:
    """Every statement of the heat store, which holds the picks and the sessions' last threads; `None` before it exists."""
    path = working_set_heat.sidecar_path(vault)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return list(conn.iterdump())


def test_a_maps_request_with_a_conversation_a_token_and_a_session_writes_no_heat(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slice compile gets the conversation and no attribution: not even a request that names a session and an
    `anchor` makes it record a pick or a session thread, as the same request does in anchors mode."""
    _structured(vault)
    token = _activate(vault, turn=SLED_TURN)["continuity"]
    keys = {"client": "claude", "session": "session-a", "workspace": "workspace-a"}
    thread = {"recent": [{"role": "user", "text": "Is the Cargo Sled still rated for winter?"}]}
    before = _heat_store(vault)
    packet = _activate(vault, turn=SLED_TURN, continuity=token, anchor=CARGO_SLED, conversation=thread, **keys)
    assert packet["generation"]["conversation"] == "applied" and packet["turn_slice"]["items"]
    assert _heat_store(vault) == before
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "anchors")
    _activate(vault, turn=SLED_TURN, anchor=CARGO_SLED, **keys)
    assert _heat_store(vault) != before  # the control: the anchors packet records the pick and the session


def test_a_slice_the_request_budget_stops_reports_no_conversation_read(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A compile that the budget stops inside `serve` has not given the conversation a state to report."""
    from exomem import request_budget

    _conversation_world(vault)

    def exhausted(*args, **kwargs):
        request_budget.current().note_skipped("working_set.semantic")
        raise working_set.BudgetExhausted("working_set.semantic")

    monkeypatch.setattr(working_set, "compile_packet", exhausted)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=50))
    try:
        packet = _activate(vault, turn=GRANT_TURN, conversation=GRANT_ENTRY)
    finally:
        request_budget.reset_current(token)
    assert packet["turn_slice"]["state"] == "skipped"
    assert packet["generation"]["conversation"] == "absent"
