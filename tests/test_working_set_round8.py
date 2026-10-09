"""Disclosed round-eight escapes and the settled local-material/ledger rules."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from test_working_set_conversation_carry import THREAD, cvault as cvault
from test_working_set_egress import _release
from test_governance_egress import OPEN_PATH, RESTRICTED_PATH, _external, write_rule, write_scope

from exomem import (
    commands,
    working_set,
    working_set_anaphora,
    working_set_conversation,
    working_set_index,
    working_set_resolve,
)
from exomem.governance import egress, policy
from exomem.governance.principal import request_scope


LOCAL_TURNS = (
    "i have a new plan; can you review it",
    "can you review it? i have a new plan",
    "i have an idea, is it worth it",
    "we got some time; can you move it",
    "i have two reviews; which one is ready",
    "i got 30; is that right",
    "there is a deadline: can you change it",
    "here is the plan — what about it",
    "i have the time. can you move it",
    "i got a new one; is it ready",
    "there are two; which one is right",
    "i have something; is it important",
    "i have a plan\n- can you review it",
    "a new review; is it ready",
    "i have a new plan; how did her results compare with last year",
    'is "they are ready" correct',
    "is ‘it is ready’ correct",
    'can you review "the final plan" and change it',
    "is 'they are ready' correct",
    "Tuesday or Thursday: which one is better",
    "30 or 40; which of those is right",
    "the choices are morning and evening; which one do you prefer",
    "first: 30\nsecond: 40\nwhich one is right",
)


def test_quote_heavy_turn_analysis_does_not_exhaust_the_activation_budget() -> None:
    # A valid-size malformed turn must not spend seconds repeatedly looking
    # for the same missing closer. CPU time avoids co-tenant scheduling noise.
    started = time.process_time()
    analysis = working_set_resolve.analyze_turn("‘" * 8192 + " can you review it?")
    elapsed = time.process_time() - started
    assert elapsed < 1.0, f"turn analysis consumed {elapsed:.3f} CPU seconds"
    assert analysis.points_back

ASSIGNED_TURNS = (
    "Today's plan is below. Can you review it?",
    "The result I got is 42; can you explain it?",
    "My next day is Friday. Is that okay?",
    "Today / tomorrow: which of those is right?",
    "Task: review. Can you change that?",
    "Tomorrow’s review is here; can you change it?",
    "The total that we got was forty; is that right?",
    "Our final time is noon. Can you move it?",
    "The result they gave me is 17. Is that correct?",
    "## Result: 42\nCan you explain it?",
    "Plan = review; can you change that?",
    "30/40: which one is right?",
    "plan / review: which of those is better?",
    "The result I got is 42; any update on them?",
    "The result I got is ready; can you explain it?",
    "The plan we have is final; can you review it?",
    "The reply I received; can you change it?",
    "My plan is ready; can you review it?",
    "Today's review is final; can you change it?",
    "Result: thirty. Is that right?",
    "Reply: yes. Can you change it?",
    "Can you review the task that I have made?",
    "Can you review the person that I got?",
    "Result -> 81. Can you explain it?",
    "This is the task I made: review. Can you do it?",
    "My review follows. Explain that.",
    "Result → 81. Can you explain it?",
    "Monday | Tuesday: which of those?",
    "Can you review the task that I got?",
    "One result is 6; the other is 12. Which one?",
    "This is the review that I said I would make. Can you check it?",
    "This week's review. Please change that.",
    "First = 8; next = 16. Which one is correct?",
    "12 / 24: can you compare those?",
    "Morning, noon, evening — which of these is better?",
    "Before Friday / after Sunday: which one should we use?",
    "Can you review the result that he and I got?",
    "The review we said we would do is ready. Can you check it?",
    "My current and final reply; could you change it?",
    "The end of this week's review. Can you change it?",
    "One of my reviews is ready; could you check it?",
    "One more reply is ready. Can you change it?",
    "The first: 9. The second: 27. Which of these is correct?",
    "Review: will do. Is that okay?",
    "Result = not ready. Can you explain that?",
    "Final reply: we agree. Can you review that?",
    "Ready / not ready: which of these is right?",
    "This is my final review. Can you change it?",
    "The reply is as follows: yes. Can you change it?",
    "The review that is ready; can you check it?",
)

CONTAINER_CODE = (
    "> ```\n> it\n> ````",
    "> ~~~\n> that\n> ~~~~~",
    "> > ```text\n> > it\n> > ````",
    "- ~~~\n  that\n  ~~~~~",
    "10. ```\n    it\n    ````",
    "> - ~~~text\n>   it\n>   ~~~~~",
    "- > ```\n  > that\n  > ````",
    "- ready\n\n  ~~~\n  it\n  ~~~~~",
    "10. ready\n\n    ```\n    that\n    ````",
    "> ~~~~\n> ~~~\n> that\n> ~~~~~",
    "> ```\n> ~~~~\n> it\n> ````",
    "> ~~~\n> - ~~~\n> it\n> ~~~~~",
    "10. ready\ncontinued\n\n    ~~~\n    it\n    ~~~~~",
    "> 10. ready\n> continued\n>\n>     ```\n>     it\n>     ````",
    "10. - ready\n    continued\n\n      ~~~\n      it\n      ~~~~~",
)


@pytest.mark.parametrize("turn", ASSIGNED_TURNS)
def test_assigned_nominals_and_delimited_values_veto_history(turn: str) -> None:
    analysis = working_set_resolve.analyze_turn(turn)
    assert analysis.local_material, turn
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


@pytest.mark.parametrize("turn", CONTAINER_CODE)
def test_container_code_keeps_its_whole_body_opaque(turn: str) -> None:
    tokens = working_set_anaphora.anaphora_tokens(turn)
    assert tokens.count("`code`") == 1
    assert not any(word in tokens for word in ("it", "that", "text"))
    analysis = working_set_resolve.analyze_turn(turn)
    assert not analysis.points_back
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


@pytest.mark.parametrize("turn", ASSIGNED_TURNS[:5] + ASSIGNED_TURNS[-26:] + CONTAINER_CODE[:7])
@pytest.mark.parametrize("leaf", (False, True), ids=("compiler", "command"))
def test_structural_local_material_abstains_at_both_public_paths(
    cvault: Path, monkeypatch: pytest.MonkeyPatch, turn: str, leaf: bool
) -> None:
    write_scope(cvault)
    write_rule(cvault, ceiling=0)
    pol = policy.load(cvault)
    assert not pol.empty and not pol.blocked
    guards = []
    original = egress.guard_working_set

    def guard(root, packet, release, **kwargs):
        guards.append(release.active)
        return original(root, packet, release, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", guard)
    with request_scope(_external()):
        packet = (commands.op_activate_context if leaf else working_set.compile_packet)(
            cvault,
            turn=turn,
            conversation=THREAD if leaf else working_set_conversation.bound(THREAD),
        )
        if not leaf:
            packet = egress.guard_working_set(cvault, packet, _release())
    assert guards == [True]
    assert packet is not None
    assert packet["generation"].get("carried_by") != "conversation"
    assert not any(item.get("origin") == "conversation" for item in packet["anchors"])


@pytest.mark.parametrize(
    "turn",
    (
        "the next one is on Friday; can you review it",
        "was that your final reply",
        "its result is ready; can you review it",
        "what about today's plan",
        "is the plan ready; can you review it",
        "on Friday / after the weekend, is it ready",
        "which of those is on Friday / after the weekend",
        "the plan is ready; can you review it",
        "the next one is Friday; can you review it",
        "Plan: is it ready?",
        "Is the next one ready for tomorrow?",
        "the next one is ready and I got it",
        "the plan is ready and the review is due; which one is final",
    ),
)
def test_structural_assignment_boundaries_preserve_historical_requests(turn: str) -> None:
    # One admitted unit supports the request words, so only local material can veto.
    support = "The plan's final reply is ready for review; its result is due."
    assert working_set_conversation.may_carry(
        working_set_resolve.analyze_turn(turn), supporting_text=support,
    ), turn


@pytest.mark.parametrize(
    "turn",
    (
        "> ~~~\n> that\n> ~~~~~\nit",
        "> ~~~\n> that\nit",
        "- ~~~\n  that\n  ~~~~~\nit",
        "- ~~~\n  that\nit",
    ),
)
def test_container_fences_end_at_the_close_or_the_container_boundary(turn: str) -> None:
    assert working_set_anaphora.anaphora_tokens(turn) == ("`code`", "it")


@pytest.mark.parametrize("turn", LOCAL_TURNS)
def test_local_material_vetoes_even_an_empty_title_licensed_residue(turn: str) -> None:
    analysis = working_set_resolve.analyze_turn(turn)
    assert not working_set_conversation.may_carry(analysis, subject_title=turn), turn


@pytest.mark.parametrize("turn", LOCAL_TURNS[:3] + LOCAL_TURNS[-8:])
def test_local_material_never_attaches_a_historical_subject(cvault: Path, turn: str) -> None:
    packet = commands.op_activate_context(cvault, turn=turn, conversation=THREAD)
    assert packet["generation"].get("carried_by") != "conversation"
    assert not any(item.get("origin") == "conversation" for item in packet["anchors"])


@pytest.mark.parametrize(
    "turn",
    (
        "was that your final reply",
        "what about the next one",
        "is it on Tuesday",
        "is it ready or should we review it",
        "is it in the morning or the evening",
        "it's still on track",
        "it’s still on track",
        "how did her results compare",
        "what about its owner's plan",
        "what about its owner’s plan",
        "who is handling that plan",
        "is it ready after we said we agree",
    ),
)
def test_historical_controls_and_apostrophes_keep_their_licence(turn: str) -> None:
    # The unit has no apostrophe, so a contraction or possessive must split to be licensed.
    support = "Her results compare well: the final reply is ready for review, still on track, and the team handling it agrees."
    analysis = working_set_resolve.analyze_turn(turn)
    assert working_set_conversation.may_carry(analysis, subject_title="owner plan", supporting_text=support), turn


@pytest.mark.parametrize(
    "turn",
    (
        '"it"',
        "‘they are ready’",
        "'that'",
        "`it`",
        "``it``",
        "```\nit is ready\n````",
        "~~~\nit is ready\n~~~~",
    ),
)
def test_internal_quote_and_code_pointers_never_point(turn: str) -> None:
    assert not working_set_resolve.analyze_turn(turn).points_back


@pytest.mark.parametrize(
    "turn",
    (
        "can you explain\n```\nit is ready\n````",
        "it is ready\n```\nreview it\n````",
        "is it ready\n~~~text\nit is ready\n~~~~",
        "can you explain ``a ` it ` b``",
    ),
)
def test_opaque_code_cannot_gain_a_title_licence(turn: str) -> None:
    analysis = working_set_resolve.analyze_turn(turn)
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


def test_inline_runs_require_an_exact_match() -> None:
    assert not working_set_resolve.analyze_turn("``it ` ready``").points_back
    assert working_set_resolve.analyze_turn("``ready` it").points_back


def _subject_chars(packet: dict) -> int:
    # Independent recount of fixture payloads: these are the only exempt
    # categorical/date fields the fixtures emit; authored strings count.
    structural = {
        "kind": {"entity", "page", "hub", "resource", "project"},
        "status": {"partial", "resolved"},
        "origin": {"conversation", "turn"},
        "evidence": {"conversation"},
        "lifecycle": {"active", "superseded"},
        "level": {"unit", "page"},
        "source": {"profile", "records"},
        "role": {role["id"] for role in packet.get("roles", ())},
        "reason": {"budget", "role_cap"},
    }

    def cost(value, key=""):
        if key in {
            "ref",
            "path",
            "anchor",
            "neighbourhood",
            "anchor_neighbourhood",
            "superseded_by",
        }:
            return 0
        if isinstance(value, str):
            if value in structural.get(key, set()) or (
                key in {"updated", "as_of"} and value == "2026-09-01"
            ):
                return 0
            return len(value)
        if isinstance(value, dict):
            return sum(cost(item, name) for name, item in value.items())
        if isinstance(value, (list, tuple)):
            return sum(cost(item, key) for item in value)
        return 0

    return sum(
        cost(packet.get(section, []))
        for section in ("anchors", "ambiguity", "current_state", "units", "pointers")
    )


def _assert_ledger(packet: dict) -> None:
    subject = _subject_chars(packet)
    recent = sum(
        len(item.get("title", "")) + len(item.get("statement", ""))
        for item in packet["recent_context"]
    )
    assert subject <= packet["budget"]["limit_chars"] // 3
    assert packet["budget"]["used_chars"] == recent + subject
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def _packet(**kwargs) -> dict:
    return working_set.build_packet(
        items=kwargs.pop("items", ()),
        anchors=kwargs.pop("anchors", ({"ref": OPEN_PATH, "title": "Open", "kind": "page"},)),
        roles=kwargs.pop(
            "roles", ({"id": "identity", "lane": "entity", "source": "anchor_default"},)
        ),
        current_state=kwargs.pop("current_state", ()),
        ambiguity=(),
        missing=(),
        max_chars=kwargs.pop("max_chars", 1200),
        generation={"carried_by": "conversation"},
        status="resolved",
        **kwargs,
    )


@pytest.mark.parametrize("limit", (500, 501, 1199, 1200, 8000))
def test_titles_spend_the_inclusive_boundary(limit: int) -> None:
    title = "x" * (limit // 3)
    packet = _packet(anchors=({"ref": OPEN_PATH, "title": title, "kind": "page"},), max_chars=limit)
    assert packet["anchors"][0]["title"] == title
    _assert_ledger(packet)
    overflow = _packet(
        anchors=({"ref": OPEN_PATH, "title": title + "x", "kind": "page"},), max_chars=limit
    )
    assert overflow["anchors"][0]["title"] == ""
    assert overflow["anchors"][0]["ref"] == OPEN_PATH
    _assert_ledger(overflow)


def test_unaffordable_authored_header_metadata_is_empty_without_losing_identity() -> None:
    packet = _packet(
        anchors=(
            {
                "ref": OPEN_PATH,
                "title": "Open",
                "kind": "page",
                "status": "partial",
                "evidence": ["conversation"],
                "lifecycle": "unvalidated prose " * 100,
            },
        )
    )
    assert packet["anchors"][0]["title"] == "Open"
    assert packet["anchors"][0]["lifecycle"] == ""
    assert packet["anchors"][0]["ref"] == OPEN_PATH
    assert packet["anchors"][0]["evidence"] == ["conversation"]
    _assert_ledger(packet)


@pytest.mark.parametrize(
    "metadata",
    (
        {"category": "category prose " * 80},
        {"source": "authored source " * 80},
        {"as_of": "unvalidated date " * 80},
        {"custom": "other metadata " * 80},
    ),
)
def test_complete_authored_metadata_payloads_must_fit(metadata: dict) -> None:
    item = working_set.LaneItem(
        ref=OPEN_PATH + "#unit-a",
        path=OPEN_PATH,
        anchor=OPEN_PATH,
        role="identity",
        level="unit",
        text="A small claim.",
        lifecycle="active",
        updated="2026-09-01",
        provenance=metadata,
        title="Pointer",
        why="why",
    )
    packet = _packet(items=(item,))
    assert not packet["units"]
    _assert_ledger(packet)


def test_state_and_overflow_pointer_occurrences_are_charged() -> None:
    item = working_set.LaneItem(
        ref=OPEN_PATH + "#unit-a",
        path=OPEN_PATH,
        anchor=OPEN_PATH,
        role="identity",
        level="unit",
        text="x" * 360,
        lifecycle="unvalidated lifecycle " * 30,
        updated="unvalidated date " * 30,
        provenance={},
        title="Pointer",
        why="why",
    )
    packet = _packet(
        items=(item,),
        current_state=(
            {"anchor": OPEN_PATH, "statement": "State", "source": "records", "as_of": "bad date"},
        ),
        recent_context=({"ref": OPEN_PATH, "title": "Recent", "statement": "continuity"},),
    )
    assert packet["pointers"]
    _assert_ledger(packet)


@pytest.mark.parametrize("material", (True, False))
def test_real_indexed_long_titles_are_bounded_including_no_material_fallback(
    cvault: Path, monkeypatch, material: bool
) -> None:
    page = cvault / "Knowledge Base/Entities/People/Ottilie Marsh.md"
    title = "Ottilie Marsh " + "long title " * 500
    page.write_text(
        "---\ntype: entity\nentity_type: person\nstatus: active\naliases: [Ottilie]\n---\n\n# "
        + title
        + ("\n\n## Summary\n\nA small claim.\n" if material else "\n"),
        encoding="utf-8",
    )
    working_set_index.WorkingSetIndex(cvault).rebuild()
    if not material:
        monkeypatch.setattr(working_set, "run_lanes", lambda *a, **k: ([], []))
    packet = commands.op_activate_context(
        cvault, turn="what about the next one", conversation=THREAD, max_chars=1200
    )
    assert packet["anchors"][0]["title"] == ""
    assert packet["anchors"][0]["origin"] == "conversation"
    _assert_ledger(packet)
    assert "conversation_inferred" not in json.dumps(packet)


def test_real_indexed_ambiguity_keeps_every_identity_with_budgeted_titles(cvault: Path) -> None:
    write_scope(cvault)
    write_rule(cvault, ceiling=0)
    for name in ("Ottilie Marsh", "Bram Quillfeather"):
        page = cvault / f"Knowledge Base/Entities/People/{name}.md"
        page.write_text(
            f"---\ntype: entity\nentity_type: person\nstatus: active\naliases: [{name}]\n---\n\n# {name} "
            + "long title " * 500
            + "\n",
            encoding="utf-8",
        )
    working_set_index.WorkingSetIndex(cvault).rebuild()
    with request_scope(_external()):
        packet = commands.op_activate_context(
            cvault,
            turn="what about the next one",
            max_chars=1200,
            conversation={
                "recent": [{"role": "user", "text": "Ottilie Marsh and Bram Quillfeather"}]
            },
        )
    assert packet["abstention"]["reason"] == "ambiguous"
    assert len(packet["ambiguity"]) == 2
    assert [item["title"] for item in packet["ambiguity"]] == ["", ""]
    _assert_ledger(packet)
    assert type(packet) is working_set_conversation.InferredPacket
    assert set(json.loads(json.dumps(packet))) == set(packet)


def test_egress_recounts_retained_subject_prose_without_refill(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    items = tuple(
        working_set.LaneItem(
            ref=path + "#unit-a",
            path=path,
            anchor=path,
            role="identity",
            level="unit",
            text="A small claim.",
            lifecycle="active",
            updated="2026-09-01",
            provenance={"category": "fact"},
            title="Pointer",
            why="why",
        )
        for path in (OPEN_PATH, RESTRICTED_PATH)
    )
    packet = _packet(items=items)
    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())
    assert guarded is not None and len(guarded["units"]) == 1
    _assert_ledger(guarded)


@pytest.mark.parametrize("material", (True, False))
def test_command_leaf_preserves_inferred_marker_through_egress(
    cvault: Path, monkeypatch, material: bool
) -> None:
    write_scope(cvault)
    write_rule(cvault, ceiling=0)
    subject = "Knowledge Base/Entities/People/Ottilie Marsh.md"
    items = tuple(
        working_set.LaneItem(
            ref=path + "#unit-a",
            path=path,
            anchor=subject,
            role="identity",
            level="unit",
            text="A small claim.",
            lifecycle="active",
            updated="2026-09-01",
            provenance={"category": "fact"},
            title="Pointer",
            why="why",
        )
        for path in (OPEN_PATH, RESTRICTED_PATH)
    )
    monkeypatch.setattr(working_set, "run_lanes", lambda *a, **k: (items if material else (), []))
    with request_scope(_external()):
        packet = commands.op_activate_context(
            cvault, turn="what about the next one", conversation=THREAD
        )
    assert len(packet["units"]) == (1 if material else 0)
    if not material:
        assert packet["generation"].get("carried_by") != "conversation"
    _assert_ledger(packet)
    assert type(packet) is working_set_conversation.InferredPacket
    assert set(json.loads(json.dumps(packet))) == set(packet)


def test_selected_custom_role_identities_are_exempt_at_each_occurrence() -> None:
    role = "custom_role_" + "r" * 100
    item = working_set.LaneItem(
        ref=OPEN_PATH + "#unit-a",
        path=OPEN_PATH,
        anchor=OPEN_PATH,
        role=role,
        level="unit",
        text="A small claim.",
        lifecycle="active",
        updated="2026-09-01",
        provenance={"category": "fact"},
        title="Pointer",
        why="why",
    )
    packet = _packet(items=(item,), roles=({"id": role, "lane": "units", "source": "turn_cue"},))
    assert packet["units"][0]["role"] == role
    _assert_ledger(packet)


@pytest.mark.parametrize(
    "turn",
    (
        "i have enough time; can you move it",
        "i just got the plan; can you review it",
        "i have received the plan; can you review it",
        "i had got 30; is that right",
        "i have a very important plan; can you review it",
        "plan or review: which one is right",
        "idea or budget; which one do you prefer",
        "with a new plan, can you review it",
        "ready or not ready: which of those",
    ),
)
def test_additional_introductions_and_nominal_alternatives_stay_local(turn: str) -> None:
    analysis = working_set_resolve.analyze_turn(turn)
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


def test_a_perfect_auxiliary_does_not_introduce_a_nominal() -> None:
    analysis = working_set_resolve.analyze_turn("i have decided to review it")
    assert working_set_conversation.may_carry(analysis, supporting_text="We decided to review the plan.")


def test_a_prepositional_bare_noun_keeps_its_local_relative() -> None:
    turn = "Can you compare it to work that is ready?"
    analysis = working_set_resolve.analyze_turn(turn)
    assert analysis.local_material
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


def test_a_quantified_demonstrative_complement_keeps_its_local_noun() -> None:
    turn = "Given all of that review, what should we change?"
    analysis = working_set_resolve.analyze_turn(turn)
    assert analysis.local_material
    assert not working_set_conversation.may_carry(analysis, subject_title=turn)


@pytest.mark.parametrize(
    "turn",
    (
        "any update on them",
        "any latest update about it",
        "some numbers from them?",
        "any update on them on Tuesday",
        "please any latest update about it now",
    ),
)
def test_quantified_information_requests_keep_the_historical_relation(turn: str) -> None:
    assert working_set_conversation.may_carry(
        working_set_resolve.analyze_turn(turn), supporting_text="The latest numbers are in.",
    ), turn


@pytest.mark.parametrize(
    "turn",
    (
        "i have a plan; any update on them",
        "i have some news about them; is it right",
        'any update on them; is "they are ready" correct',
        "30 or 40: which one; any update on them",
        "can you review a plan for it",
        "is a plan with it ready",
        "do you have any news on her",
        "is there any progress on that",
        "any update on them and a new plan",
        "any update on them is ready",
    ),
)
def test_a_historical_information_request_never_cancels_other_local_material(turn: str) -> None:
    assert not working_set_conversation.may_carry(working_set_resolve.analyze_turn(turn)), turn


@pytest.mark.parametrize("turn", ("can you review a plan for it", "is a plan with it ready"))
def test_a_finite_local_nominal_never_uses_the_request_exemption(cvault: Path, turn: str) -> None:
    packet = commands.op_activate_context(cvault, turn=turn, conversation=THREAD)
    assert packet["generation"].get("carried_by") != "conversation"
    assert not any(item.get("origin") == "conversation" for item in packet["anchors"])


def test_a_local_veto_does_not_walk_any_historical_subject(cvault: Path, monkeypatch) -> None:
    calls = []
    original = working_set_conversation.carry

    def carry(entries):
        calls.append(entries)
        return original(entries)

    monkeypatch.setattr(working_set_conversation, "carry", carry)
    packet = commands.op_activate_context(
        cvault,
        turn="i have a new plan; can you review it",
        conversation={
            "recent": [{"role": "user", "text": "Kestrel Hiring Plan"}, *THREAD["recent"]]
        },
    )
    assert calls == []
    assert packet["generation"].get("carried_by") != "conversation"


@pytest.mark.parametrize("carry_kind", (None, "agent_choice", "recency", "follow_up", "retrieval"))
def test_ordinary_packet_accounting_keeps_its_shipped_fields(carry_kind: str | None) -> None:
    item = working_set.LaneItem(
        ref=OPEN_PATH + "#unit-a",
        path=OPEN_PATH,
        anchor=OPEN_PATH,
        role="identity",
        level="unit",
        text="A small claim.",
        lifecycle="authored lifecycle",
        updated="authored date",
        provenance={"category": "large category " * 100},
        title="Pointer",
        why="why",
    )
    packet = working_set.build_packet(
        items=(item,),
        anchors=({"ref": OPEN_PATH, "title": "long title " * 100, "origin": "conversation"},),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=500,
        generation={} if carry_kind is None else {"carried_by": carry_kind},
        status="resolved",
    )
    assert type(packet) is dict
    assert packet["anchors"][0]["title"] == "long title " * 100
    assert packet["units"][0]["provenance"]["category"] == "large category " * 100
    assert packet["budget"]["used_chars"] == len(item.text)


@pytest.mark.parametrize("focus", (False, True))
def test_current_turn_and_focus_resolution_precede_the_local_veto(
    cvault: Path, focus: bool
) -> None:
    turn = "i have a new plan; can you review it"
    conversation = {**THREAD, "focus": "Ottilie Marsh"} if focus else THREAD
    packet = commands.op_activate_context(
        cvault,
        turn=turn if focus else turn + " for Ottilie Marsh",
        conversation=conversation,
        max_chars=500,
    )
    assert type(packet) is dict
    assert packet["anchors"][0]["title"] == "Ottilie Marsh"
    assert packet["anchors"][0]["origin"] == ("focus" if focus else "turn")
    assert packet["generation"].get("carried_by") != "conversation"
    ordinary = (
        sum(
            len(item.get("title", "")) + len(item.get("statement", ""))
            for item in packet["recent_context"]
        )
        + sum(len(item["text"]) for item in packet["units"])
        + sum(len(item["statement"]) for item in packet["current_state"])
        + sum(len(item["title"]) + len(item["why"]) for item in packet["pointers"])
    )
    assert packet["budget"]["used_chars"] == ordinary
