"""The incident runner plays several user turns and a follow-up session, and checks the collections a session made.

The structured-capture cases score what an agent starts on its own: a collection that nothing asked for. A
check that misread the vault, or an actor that replayed the turns in separate sessions, would pass or fail every
unit of those cases for a reason that says nothing about the product.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from membench.outcome import billing
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import openai_actor
from membench.outcome.incidents import store as store_module
from membench.outcome.incidents.server import loopback_mcp
from membench.outcome.incidents.store import ENROLMENT_PATH
from membench.outcome.isolation import REPO_ROOT, child_env, fresh_vault, lay_instruction_block
from membench.outcome.judging import Judgement

# Each test that seeds starts the product in fresh interpreters, which takes a while on a busy machine.
pytestmark = pytest.mark.timeout(300)

PLANTS, CANDIDATES = "Records/Plants/_collection.md", "Records/Candidates/_collection.md"


def manifest(title: str, exomem_id: str, key: list[str], fields: dict, terms: list[str]) -> dict:
    return {
        "type": "collection",
        "exomem_id": exomem_id,
        "title": title,
        "semantic_profile": "records",
        "collection_version": 1,
        "schema_version": 1,
        "lifecycle": "active",
        "storage": {"strategy": "markdown-items", "source": "Entries", "format_version": 1},
        "item_schema": {"natural_key": key, "fields": fields},
        "claims": {"terms": terms},
    }


#: What the seed leaves: a collection of an unrelated kind, with one row.
PLANTS_SPEC = {
    "manifest_path": PLANTS,
    "manifest": manifest(
        "Plants",
        "6a1e9c30-52b7-4d84-a3f1-0c9d7e2b5a48",
        ["plant"],
        {"plant": {"type": "string", "required": True}, "pot_cm": {"type": "integer"}},
        ["pot", "soil", "root"],
    ),
    "rows": [{"plant": "Fig", "pot_cm": 25}],
}
#: What the stubbed session creates through the product's own commands: four fields, keyed by candidate.
CANDIDATES_SPEC = {
    "manifest_path": CANDIDATES,
    "manifest": manifest(
        "Candidates",
        "f2b84d17-9e60-4c35-8a7d-31c5e0a9b6d4",
        ["candidate"],
        {
            "candidate": {"type": "string", "required": True},
            "reaction": {"type": "string"},
            "reacted_on": {"type": "date"},
            "source": {"type": "string"},
        },
        ["candidate"],
    ),
    "rows": [
        {
            "candidate": "Alder",
            "reaction": "too loud",
            "reacted_on": "2026-11-14",
            "source": "user",
        },
        {"candidate": "Birch", "reaction": "quiet", "reacted_on": "2026-11-18", "source": "user"},
        {"candidate": "Cedar", "reaction": "discontinued", "source": "outside"},
    ],
}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
    "additionalProperties": False,
}
JUDGE = {"schema": JUDGE_SCHEMA, "require": {"ok": True}}


def has(text: str) -> list[dict]:
    return [{"field": "*", "op": "contains", "value": text}]


class StubJudge:
    """A judge that never calls a model: it records the prompts it reads and answers ``ok`` from ``verdicts``."""

    def __init__(self, *, fails_on: str | None = None) -> None:
        self.prompts: list[str] = []
        self.fails_on = fails_on

    def call(self, prompt, schema, *, out, timeout):
        self.prompts.append(prompt)
        ok = self.fails_on is None or self.fails_on not in prompt
        return Judgement({"ok": ok, "reason": "stub"}, 0.0, [], "")


def play_stub(monkeypatch, act, **played) -> None:
    """Replace the Claude actor with one that does ``act(root)`` on the unit's real vault and answers as given."""

    def player(case, arm, root, work, record, **_session):
        act(root)
        return {"fault": None, "extra": {}, "tools": [], "calls": [], **played}

    monkeypatch.setitem(incidents.PLAYERS, "claude", player)


def create_through_the_product(root: Path, *specs: dict) -> None:
    """What an agent's create and append calls leave in the vault, written by the arm's own records commands."""
    incidents.run_arm_side(
        REPO_ROOT, root, "seed_collections", str(root / "vault"), stdin=json.dumps(list(specs))
    )


def run(case: dict, root: Path, judge: StubJudge) -> dict:
    return incidents.run_unit(
        case, REPO_ROOT, root, judge=judge, model="m", executable="e", max_turns=1
    )


@pytest.fixture(scope="module")
def played_unit(tmp_path_factory) -> dict:
    """One unit on a real vault: the seed leaves a plants collection, a stubbed session creates a candidates
    collection with three rows through the product's commands, and the follow-up answers."""
    root = tmp_path_factory.mktemp("unit")
    case = {
        "id": "evaluation",
        "turns": ["I'm choosing between Alder, Birch and Cedar.", "Cedar is discontinued."],
        "follow_up": "Where do things stand?",
        "seed_collections": [PLANTS_SPEC],
        "pass": {
            "new_collection": {
                "max_fields": 4,
                "min_rows": 3,
                "where": [{"field": "semantic_profile", "op": "eq", "value": "records"}],
                "rows_each": [has("alder"), has("birch"), has("cedar")],
            },
            "judge": {**JUDGE, "include_rows": True},
            "follow_up_judge": {**JUDGE, "key": "Cedar is out; Birch is quiet."},
        },
    }
    receipt = json.dumps(
        {"collection_id": CANDIDATES_SPEC["manifest"]["exomem_id"], "automatic": True}
    )
    judge = StubJudge()
    with pytest.MonkeyPatch.context() as patch:
        play_stub(
            patch,
            lambda unit_root: create_through_the_product(unit_root, CANDIDATES_SPEC),
            answer="Cedar is out.",
            answers=["Started a table of the three.", "Cedar is out."],
            calls=[{"name": "record_memory", "input": {}, "result": receipt, "is_error": False}],
            follow_up={"answer": "Birch is the quiet one.", "tools": [], "calls": []},
        )
        result = run(case, root, judge)
    facts = incidents.run_arm_side(REPO_ROOT, root, "collections", str(root / "vault"))
    return {"result": result, "judge": judge, "root": root, "facts": facts, "case": case}


def test_a_single_prompt_case_reaches_the_judge_exactly_as_it_always_did(tmp_path) -> None:
    """The judge's input is the instrument of every earlier run: a case with one `prompt` must not read differently."""
    case = {"id": "x", "prompt": "Why did it stall?", "pass": {"judge": JUDGE}}
    judge = StubJudge()
    assert incidents.judge_answer(case, "Because.", tmp_path, judge) == (True, None)
    assert judge.prompts == [
        "You grade one assistant answer. Read the user's turn and the answer, then fill the schema.\n\n"
        "User turn: Why did it stall?\n\nAnswer:\n<<<\nBecause.\n>>>"
    ]
    assert (tmp_path / "verdict.json").exists()


def test_a_unit_whose_session_started_the_expected_collection_passes_every_check(
    played_unit,
) -> None:
    result = played_unit["result"]
    assert result["checks"] == {"new_collection": True, "judge": True, "follow_up_judge": True}
    assert result["passed"] is True and result["reason"] is None


def test_the_collection_the_seed_left_is_not_one_the_session_started(played_unit) -> None:
    before = played_unit["result"]["collections_before"]
    assert [c["title"] for c in played_unit["facts"]["collections"] if c["path"] in before] == [
        "Plants"
    ]
    assert [c["title"] for c in incidents.gained_collections(played_unit["facts"], before)] == [
        "Candidates"
    ]


def test_the_unit_records_what_the_receipt_and_the_inventory_say_about_automatic_creation(
    played_unit,
) -> None:
    """Recorded and never gated on, so a base arm without the flag still compares on the outcome."""
    created = played_unit["result"]["new_collections"]
    assert [c["title"] for c in created] == ["Candidates"]
    assert created[0]["automatic_in_receipt"] is True
    # The seed, not an agent, made this collection: an arm that reports the flag says false; one that does not, null.
    assert created[0]["automatic_in_inventory"] in (None, False)
    assert created[0]["natural_key"] == ["candidate"] and len(created[0]["fields"]) == 4


def test_the_main_judge_reads_every_turn_and_answer_and_the_schema_beside_the_rows(
    played_unit,
) -> None:
    main, follow_up = played_unit["judge"].prompts
    assert "[turn 1] I'm choosing" in main and "[turn 2] Cedar is discontinued." in main
    assert (
        "[answer 1]\nStarted a table of the three." in main and "[answer 2]\nCedar is out." in main
    )
    assert '"natural_key": [' in main and '"candidate": "Alder"' in main
    # The follow-up is a different session, so the judge reads its own turn, its answer and the case key.
    assert "User turn: Where do things stand?" in follow_up
    assert "Birch is the quiet one." in follow_up and "Cedar is out; Birch is quiet." in follow_up
    assert "Started a table" not in follow_up


def test_the_follow_up_verdict_is_kept_beside_the_main_one(played_unit) -> None:
    root = played_unit["root"]
    assert (root / "verdict.json").exists() and (root / "verdict.follow-up.json").exists()
    assert json.loads((root / "verdict.follow-up.json").read_text())["id"].endswith(".follow-up")


def test_a_seeded_row_that_now_holds_the_conditions_passes_and_one_that_does_not_fails(
    played_unit,
) -> None:
    """A corrected value keeps its row's id, so it is no new row; the check reads the row that stayed."""
    root = played_unit["root"]
    listed = incidents.run_arm_side(REPO_ROOT, root, "records", str(root / "vault"), PLANTS)
    left = {PLANTS: [row["record_id"] for row in listed[PLANTS]["rows"]]}
    corrected = {
        "collection": PLANTS,
        "equals": 0,
        "seed_row_now": [{"field": "pot_cm", "op": "eq", "value": "25"}],
    }
    stale = {**corrected, "seed_row_now": [{"field": "pot_cm", "op": "eq", "value": "30"}]}
    assert incidents.collection_row(listed, left, corrected) is True
    assert incidents.collection_row(listed, left, stale) is False


def facts_with(played_unit, edit) -> tuple[dict, list[str]]:
    """The real facts of the played unit, changed by ``edit`` on the new collection, and what the seed left."""
    facts = copy.deepcopy(played_unit["facts"])
    created = next(c for c in facts["collections"] if c["title"] == "Candidates")
    edit(created)
    return facts, played_unit["result"]["collections_before"]


SPEC = {"max_fields": 4, "min_rows": 3, "rows_each": [has("alder"), has("birch"), has("cedar")]}


def test_new_collection_passes_the_collection_that_meets_every_condition(played_unit) -> None:
    facts, before = facts_with(played_unit, lambda created: None)
    assert incidents.new_collection(facts, before, SPEC) is True


def duplicate_the_first_row(created: dict) -> None:
    created["rows"].append({**created["rows"][0], "record_id": "another-record-id"})


def merge_alder_and_birch(created: dict) -> None:
    """One row for both names; the query returns rows in no promised order, so pick them by name."""
    by_name = {row["candidate"]: row for row in created["rows"]}
    merged = {
        **by_name["Alder"],
        "candidate": "Alder and Birch",
        "reaction": "Alder too loud, Birch quiet",
    }
    created["rows"] = [merged, by_name["Cedar"]]


@pytest.mark.parametrize(
    ("edit", "spec", "expected"),
    [
        (lambda c: None, {**SPEC, "equals": 2}, False),
        (
            lambda c: None,
            {**SPEC, "where": [{"field": "title", "op": "contains", "value": "plants"}]},
            False,
        ),
        (lambda c: None, {**SPEC, "max_fields": 3}, False),
        (lambda c: None, {**SPEC, "min_rows": 4}, False),
        (
            lambda c: None,
            {
                **SPEC,
                "row_where": [{"field": "source", "op": "eq", "value": "user"}],
                "min_rows": 3,
            },
            False,
        ),
        (lambda c: None, {**SPEC, "rows_each": [has("alder"), has("dogwood")]}, False),
        # One row that names two candidates cannot stand for both of them.
        (
            merge_alder_and_birch,
            {**SPEC, "min_rows": 2, "rows_each": [has("alder"), has("birch")]},
            False,
        ),
        (duplicate_the_first_row, {**SPEC, "min_rows": 3}, False),
        (lambda c: c.update(natural_key=[]), SPEC, False),
        (lambda c: c.update(rows={"error": "unreadable"}), SPEC, None),
        (lambda c: c.update(rows=None), SPEC, False),
    ],
    ids=[
        "wrong-count",
        "collection-condition-unmet",
        "too-many-fields",
        "too-few-rows",
        "row-condition-unmet",
        "named-item-has-no-row",
        "two-items-merged-in-one-row",
        "duplicate-natural-key",
        "no-natural-key",
        "rows-unreadable-is-unknown",
        "not-a-records-collection",
    ],
)
def test_new_collection_fails_each_way_the_collection_can_miss_the_case(
    played_unit, edit, spec, expected
) -> None:
    facts, before = facts_with(played_unit, edit)
    assert incidents.new_collection(facts, before, spec) is expected


def test_an_unlisted_vault_is_unknown_and_never_an_empty_one(played_unit) -> None:
    before = played_unit["result"]["collections_before"]
    assert incidents.new_collection({"error": "unreadable"}, before, SPEC) is None
    assert incidents.no_new_collection({"error": "unreadable"}, before) is None


def test_no_new_collection_passes_when_only_the_seeds_collections_exist_and_fails_when_one_is_added(
    played_unit,
) -> None:
    facts, everything = (
        played_unit["facts"],
        [c["path"] for c in played_unit["facts"]["collections"]],
    )
    assert incidents.no_new_collection(facts, everything) is True
    assert incidents.no_new_collection(facts, played_unit["result"]["collections_before"]) is False


@pytest.mark.parametrize(
    ("result", "calls_input", "expected"),
    [
        ({"collection_id": "ID-1", "automatic": True}, {}, True),
        ({"outcome": {"receipt": {"collection_id": "ID-1", "automatic": False}}}, {}, False),
        ({"collection_id": "ID-1"}, {}, None),
        # A receipt for another collection says nothing about this one.
        ({"collection_id": "ID-2", "automatic": True}, {}, None),
        # The call that sent the manifest path names it when the receipt does not carry the id.
        ({"automatic": True}, {"manifest_path": "Records/Candidates/_collection.md"}, True),
    ],
)
def test_the_receipt_flag_is_read_from_the_call_that_created_the_collection(
    result, calls_input, expected
) -> None:
    collection = {"collection_id": "ID-1", "kb_path": "Records/Candidates/_collection.md"}
    call = {
        "name": "record_memory",
        "input": calls_input,
        "result": json.dumps(result),
        "is_error": False,
    }
    assert incidents.receipt_says_automatic([call], collection) is expected


def test_a_refused_creation_says_nothing_about_automatic_creation() -> None:
    refusal = json.dumps(
        {"error": {"code": "AUTOMATIC_CREATION_REFUSED", "automatic": True, "id": "ID-1"}}
    )
    call = {"name": "record_memory", "input": {}, "result": refusal, "is_error": False}
    assert (
        incidents.receipt_says_automatic([call], {"collection_id": "ID-1", "kb_path": None}) is None
    )


def test_a_session_that_started_a_collection_fails_a_case_that_expects_none(
    tmp_path, monkeypatch
) -> None:
    case = {
        "id": "passing-mention",
        "prompt": "Trying a new oat milk this week.",
        "follow_up": "How is the oat milk going?",
        "pass": {
            "no_new_collection": True,
            "follow_up_judge": {**JUDGE, "key": "The user has said nothing more."},
        },
    }
    play_stub(
        monkeypatch,
        lambda root: create_through_the_product(root, CANDIDATES_SPEC),
        answer="Noted.",
        follow_up={"answer": "Sorry, I don't know.", "tools": [], "calls": []},
    )
    result = run(case, tmp_path, StubJudge(fails_on="Sorry, I don't know."))
    assert result["checks"] == {"no_new_collection": False, "follow_up_judge": False}
    assert result["passed"] is False
    assert result["collections_before"] == [] and [
        c["title"] for c in result["new_collections"]
    ] == ["Candidates"]


def test_a_session_that_started_nothing_passes_a_case_that_expects_none(
    tmp_path, monkeypatch
) -> None:
    case = {"id": "off", "prompt": "Trying a new oat milk.", "pass": {"no_new_collection": True}}
    play_stub(monkeypatch, lambda root: None, answer="Try a lower temperature.")
    result = run(case, tmp_path, StubJudge())
    assert result["checks"] == {"no_new_collection": True} and result["passed"] is True
    assert result["new_collections"] == []


def call_of(name: str, result: dict | None = None) -> dict:
    return {"name": name, "input": {}, "result": json.dumps(result or {}), "is_error": False}


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([call_of("search_memory"), call_of("query_records")], True),
        ([call_of("search_memory"), call_of("create_note")], False),
        # The arm refused this write, and the session still tried it: `off` asks for no attempt.
        ([call_of("add_record", {"error": {"code": "NOT_SAVED"}})], False),
    ],
)
def test_a_session_passes_no_write_only_when_it_tried_no_tool_that_writes(calls, expected) -> None:
    assert incidents.no_write(calls, ["add_record", "create_note", "manage_memory"]) is expected


def test_the_arm_names_the_tools_that_write_and_not_the_ones_that_read(tmp_path) -> None:
    """The check reads the arm's own classification, so a tool the arm adds later is counted without a change here."""
    named = incidents.run_arm_side(REPO_ROOT, tmp_path, "write_tools")["tools"]
    assert {"add_record", "create_note", "save_episode", "manage_memory"} <= set(named)
    assert not {"search_memory", "query_records", "read_page", "inspect_memory"} & set(named)


def test_a_session_that_wrote_to_the_knowledge_base_fails_a_case_that_expects_no_write(
    tmp_path, monkeypatch
) -> None:
    case = {
        "id": "off",
        "prompt": "I'm choosing between Alder and Birch.",
        "pass": {"no_new_collection": True, "no_write": True},
    }
    play_stub(
        monkeypatch,
        lambda root: None,
        answer="Birch.",
        tools=["create_note"],
        calls=[call_of("create_note", {"path": "Notes/Alder or Birch.md"})],
    )
    result = run(case, tmp_path, StubJudge())
    assert result["checks"] == {"no_new_collection": True, "no_write": False}
    assert result["passed"] is False


def test_the_judge_reads_the_writes_a_case_names_and_marks_a_refused_one(
    tmp_path, monkeypatch
) -> None:
    refused = json.dumps(
        {"error": {"code": "RELATION_DISPOSITION_MISSING", "message": "Nothing was saved."}}
    )
    calls = [
        {
            "name": "create_note",
            "input": {"title": "Swap rule"},
            "result": refused,
            "is_error": False,
        },
        {
            "name": "add_observation",
            "input": {"text": "No same-day swaps"},
            "result": "{}",
            "is_error": False,
        },
        {"name": "search_memory", "input": {"query": "rule"}, "result": "{}", "is_error": False},
    ]
    spec = {**JUDGE, "include_calls": ["create_note", "add_observation"]}
    case = {
        "id": "decision",
        "turns": ["Let's agree a rule.", "Agreed."],
        "pass": {"called_any": ["create_note"], "judge": spec},
    }
    play_stub(
        monkeypatch,
        lambda root: None,
        answer="Saved.",
        answers=["Yes?", "Saved."],
        calls=calls,
        tools=["create_note"],
    )
    judge = StubJudge()
    run(case, tmp_path, judge)
    assert judge.prompts[0].count('"refused": true') == 1 and '"refused": false' in judge.prompts[0]
    assert "search_memory" not in judge.prompts[0] and "No same-day swaps" in judge.prompts[0]


def valid_case() -> dict:
    return {
        "id": "x",
        "turns": ["a", "b"],
        "follow_up": "c",
        "pass": {"new_collection": {"max_fields": 12}, "follow_up_judge": copy.deepcopy(JUDGE)},
    }


def test_a_well_formed_multi_turn_case_with_a_follow_up_is_accepted() -> None:
    incidents.validate_case(valid_case())


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda c: c.update(prompt="p"), "not both or neither"),
        (lambda c: c.pop("turns"), "not both or neither"),
        (lambda c: c.update(turns=[]), "non-empty list"),
        (lambda c: c.update(turns=["a", ""]), "non-empty list"),
        (lambda c: c.pop("follow_up"), "come together"),
        (lambda c: c["pass"].pop("follow_up_judge"), "come together"),
        (lambda c: c.update(follow_up=""), "non-empty user turn"),
        (lambda c: c["pass"]["follow_up_judge"].update(require={}), "follow_up_judge `require`"),
        (lambda c: c["pass"]["follow_up_judge"].update(include_rows=True), "takes no"),
        (lambda c: c["pass"].update(no_new_collection=True), "contradict"),
        (lambda c: c["pass"]["new_collection"].pop("max_fields"), "needs `max_fields`"),
        (lambda c: c["pass"]["new_collection"].update(equals=0), "integer, 1 or more"),
        (lambda c: c["pass"]["new_collection"].update(min_rows="2"), "integer, 1 or more"),
        (lambda c: c["pass"]["new_collection"].update(colour="red"), "needs `max_fields`"),
        (
            lambda c: c["pass"]["new_collection"].update(
                where=[{"field": "title", "op": "like", "value": "x"}]
            ),
            "ops among",
        ),
        (
            lambda c: c["pass"]["new_collection"].update(
                where=[{"field": "colour", "op": "eq", "value": "x"}]
            ),
            "`where` reads one of",
        ),
        (lambda c: c["pass"]["new_collection"].update(rows_each=[[]]), "non-empty condition lists"),
        (
            lambda c: c["pass"]["new_collection"].update(
                row_where=[{"field": "*", "op": "like", "value": "x"}]
            ),
            "ops among",
        ),
        (lambda c: c["pass"].update(judge={**JUDGE, "include_calls": []}), "include_calls"),
        # A store case needs the loopback server, the one long-lived service that the unit's coordinator guards.
        (lambda c: c.update(store=True), "`store` is a boolean of an http case"),
        (lambda c: c.update(store="yes", transport="http"), "`store` is a boolean of an http case"),
        # `include_rows` shows what `collection_row` or `new_collection` read; with neither it shows nothing.
        (
            lambda c: (
                c["pass"].pop("new_collection"),
                c["pass"].update(judge={**JUDGE, "include_rows": True}),
            ),
            "include_rows",
        ),
    ],
)
def test_a_case_whose_turns_follow_up_or_collection_checks_could_not_be_read_is_refused(
    edit, message
) -> None:
    case = valid_case()
    edit(case)
    with pytest.raises(SystemExit, match=message):
        incidents.validate_case(case)


def test_no_write_takes_only_true() -> None:
    case = {"id": "x", "prompt": "p", "pass": {"no_write": False}}
    with pytest.raises(SystemExit, match="`no_write` is true"):
        incidents.validate_case(case)


def test_an_http_case_may_ask_for_a_store_vault() -> None:
    case = valid_case() | {"transport": "http", "store": True}
    incidents.validate_case(case)


def test_no_new_collection_takes_only_true() -> None:
    case = {"id": "x", "prompt": "p", "pass": {"no_new_collection": False}}
    with pytest.raises(SystemExit, match="`no_new_collection` is true"):
        incidents.validate_case(case)


def test_seed_row_now_takes_conditions_the_comparison_can_read() -> None:
    case = {
        "id": "x",
        "prompt": "p",
        "seed_collections": [PLANTS_SPEC],
        "pass": {
            "collection_row": {
                "collection": PLANTS,
                "seed_row_now": [{"field": "pot_cm", "op": "like", "value": 1}],
            }
        },
    }
    with pytest.raises(SystemExit, match="ops among"):
        incidents.validate_case(case)


# --- the actors play the turns in order, in one session, and the follow-up in a new one -------------------------
# The Claude actor lays the arm's published instruction block, which `docs/prominence.md` holds; tests that play it
# carry `reads_repository_docs` so that a docs-only pull request still runs them.


def stream(
    answer: str, *, model: str = "m", tool: tuple[str, str] | None = None, cost: float = 0.25
) -> str:
    """One turn's ``claude -p`` stream: the init event, an optional Exomem tool call with its result, the result."""
    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": model,
            "apiKeySource": billing.KEY_SOURCES[billing.declared()],
            "mcp_servers": [{"name": "exomem", "status": "connected"}],
        }
    ]
    if tool:
        events += [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool[0],
                            "name": f"mcp__exomem__{tool[0]}",
                            "input": {"q": 1},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool[0],
                            "content": tool[1],
                            "is_error": False,
                        }
                    ]
                },
            },
        ]
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": answer,
            "total_cost_usd": cost,
        }
    )
    return "\n".join(json.dumps(event) for event in events)


class FakeClaude:
    """Stands in for the ``claude`` executable only: every other subprocess the runner starts is the real one."""

    def __init__(self, replies: list[tuple[int, str]]) -> None:
        self.replies, self.runs = list(replies), []
        self.real_run = subprocess.run

    def __call__(self, argv, **kwargs):
        if argv[0] != "fake-claude":
            return self.real_run(argv, **kwargs)
        self.runs.append({"argv": argv, "env": kwargs["env"]})
        code, stdout = self.replies[len(self.runs) - 1]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")


def play_claude(tmp_path, monkeypatch, case: dict, replies: list[tuple[int, str]]):
    work = tmp_path / "work"
    work.mkdir()
    (work / "CLAUDE.md").write_text("learning instruction")
    fake = FakeClaude(replies)
    monkeypatch.setattr(incidents.subprocess, "run", fake)
    record: dict = {"transport": "stdio"}
    played = incidents.play_claude(
        case,
        REPO_ROOT,
        tmp_path,
        work,
        record,
        executable=Path("fake-claude"),
        model="m",
        max_turns=20,
    )
    return played, record, fake


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


@pytest.mark.reads_repository_docs
def test_one_turn_runs_unpersisted_with_its_stream_where_it_always_was(
    tmp_path, monkeypatch
) -> None:
    case = {"id": "x", "prompt": "only turn", "pass": {"called_any": ["recall_context"]}}
    played, record, fake = play_claude(
        tmp_path, monkeypatch, case, [(0, stream("done", tool=("recall_context", "{}")))]
    )
    argv = fake.runs[0]["argv"]
    assert argv[2] == "only turn" and "--no-session-persistence" in argv and "--resume" not in argv
    assert (tmp_path / "stream.jsonl").exists()
    assert (
        played["answer"] == "done" and played["answers"] == ["done"] and played["follow_up"] is None
    )
    assert played["tools"] == ["recall_context"] and record["notional_usd"] == 0.25


@pytest.mark.reads_repository_docs
def test_several_turns_are_one_session_and_the_follow_up_is_a_new_one(
    tmp_path, monkeypatch
) -> None:
    case = {"id": "x", "turns": ["first", "second"], "follow_up": "later", "pass": {}}
    replies = [
        (0, stream("a1", tool=("recall_context", "{}"))),
        (0, stream("a2")),
        (0, stream("a3", tool=("query_records", '{"rows": []}'))),
    ]
    played, record, fake = play_claude(tmp_path, monkeypatch, case, replies)
    first, second, later = (run["argv"] for run in fake.runs)
    assert [first[2], second[2], later[2]] == ["first", "second", "later"]
    session = option(first, "--session-id")
    assert session and option(second, "--resume") == session and "--session-id" not in second
    assert session not in later and "--resume" not in later and "--no-session-persistence" in later
    # The follow-up starts from the hook's initial state, not the main session's cooldowns.
    assert fake.runs[2]["env"]["EXOMEM_HOOK_HOME"] != fake.runs[0]["env"]["EXOMEM_HOOK_HOME"]
    assert fake.runs[1]["env"]["EXOMEM_HOOK_HOME"] == fake.runs[0]["env"]["EXOMEM_HOOK_HOME"]
    assert played["answers"] == ["a1", "a2"] and played["answer"] == "a2"
    assert [c["name"] for c in played["calls"]] == ["recall_context"]
    assert played["follow_up"]["answer"] == "a3" and played["follow_up"]["tools"] == [
        "query_records"
    ]
    assert record["notional_usd"] == 0.75
    assert {p.name for p in tmp_path.glob("stream-*.jsonl")} == {
        "stream-1.jsonl",
        "stream-2.jsonl",
        "stream-follow-up.jsonl",
    }


@pytest.mark.reads_repository_docs
def test_a_fault_in_a_turn_ends_the_unit_before_the_next_turn_or_the_follow_up(
    tmp_path, monkeypatch
) -> None:
    case = {"id": "x", "turns": ["first", "second"], "follow_up": "later", "pass": {}}
    replies = [(0, stream("a1")), (1, "")]
    played, _record, fake = play_claude(tmp_path, monkeypatch, case, replies)
    assert len(fake.runs) == 2 and "exit code 1" in played["fault"]


@pytest.mark.reads_repository_docs
def test_a_turn_whose_model_differs_from_the_requested_one_is_a_harness_fault(
    tmp_path, monkeypatch
) -> None:
    case = {"id": "x", "turns": ["first", "second"], "pass": {}}
    played, _record, fake = play_claude(
        tmp_path, monkeypatch, case, [(0, stream("a1")), (0, stream("a2", model="other"))]
    )
    assert len(fake.runs) == 2 and "other" in played["fault"]


def sdk_result(answer: str, *, tag: str) -> SimpleNamespace:
    """What the Agents SDK returns for one turn: its answer, responses, tool-call items and the conversation so far."""
    item = SimpleNamespace(
        type="tool_call_item",
        raw_item=SimpleNamespace(name=f"tool_{tag}", arguments="{}", call_id=tag),
    )
    usage = SimpleNamespace(input_tokens=10, output_tokens=2, input_tokens_details=None)
    return SimpleNamespace(
        final_output=answer,
        raw_responses=[SimpleNamespace(usage=usage)],
        new_items=[item],
        to_input_list=lambda: [f"conversation through {tag}"],
    )


def test_the_openai_actor_carries_the_conversation_into_each_later_turn_and_keeps_every_turns_trace() -> (
    None
):
    import asyncio

    seen, results = [], iter([sdk_result("a1", tag="t1"), sdk_result("a2", tag="t2")])

    async def run(conversation):
        seen.append(conversation)
        return next(results)

    trace = {"responses": [], "items": []}
    answers = asyncio.run(openai_actor.run_turns(run, ["first", "second"], trace))
    assert answers == ["a1", "a2"]
    assert seen[0] == "first"
    assert seen[1] == ["conversation through t1", {"role": "user", "content": "second"}]
    assert len(trace["responses"]) == 2 and [i.raw_item.name for i in trace["items"]] == [
        "tool_t1",
        "tool_t2",
    ]


def test_a_turn_that_fails_leaves_the_trace_of_the_turns_before_it() -> None:
    import asyncio

    results = iter([sdk_result("a1", tag="t1")])

    async def run(_conversation):
        try:
            return next(results)
        except StopIteration:
            raise TimeoutError("provider timed out") from None

    trace = {"responses": [], "items": []}
    with pytest.raises(TimeoutError):
        asyncio.run(openai_actor.run_turns(run, ["first", "second"], trace))
    assert len(trace["responses"]) == 1 and len(trace["items"]) == 1


def test_the_openai_actor_plays_the_follow_up_as_a_second_session_and_meters_both(
    tmp_path, monkeypatch
) -> None:
    sessions, spent = [], []

    async def converse(spec, root, instructions, turns, model, max_turns, record, trace):
        sessions.append(list(turns))
        record["warm_seconds"] = 1.0 * len(sessions)
        tag = f"s{len(sessions)}"
        trace["responses"] += [
            SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, input_tokens_details=None)
            )
        ]
        trace["items"] += [
            SimpleNamespace(
                type="tool_call_item",
                raw_item=SimpleNamespace(name=f"tool_{tag}", arguments="{}", call_id=tag),
            )
        ]
        return [f"answer {tag}.{n}" for n, _ in enumerate(turns)]

    monkeypatch.setattr(openai_actor, "converse", converse)
    monkeypatch.setattr(openai_actor, "mcp_server", lambda *_: {})
    monkeypatch.setattr(
        openai_actor, "record_spend", lambda model, usage: spent.append(usage) or "recorded"
    )
    case = {"id": "x", "turns": ["first", "second"], "follow_up": "later", "pass": {}}
    record: dict = {}
    played = openai_actor.play(
        case,
        REPO_ROOT,
        tmp_path,
        record,
        server_instructions="x",
        model="m",
        max_turns=5,
        timeout=5,
    )
    assert sessions == [["first", "second"], ["later"]]
    assert (
        played["answers"] == ["answer s1.0", "answer s1.1"]
        and played["follow_up"]["answer"] == "answer s2.0"
    )
    assert played["tools"] == ["tool_s1"] and played["follow_up"]["tools"] == ["tool_s2"]
    # One metering call covers both sessions, and the follow-up's wait does not overwrite the main session's.
    assert spent == [{"input": 20, "cached": 0, "output": 4}]
    assert record["warm_seconds"] == 1.0 and record["follow_up_warm_seconds"] == 2.0


def test_a_failed_follow_up_is_a_fault_of_the_unit_and_still_meters_the_main_session(
    tmp_path, monkeypatch
) -> None:
    calls, spent = [], []

    async def converse(spec, root, instructions, turns, model, max_turns, record, trace):
        calls.append(turns)
        if len(calls) == 2:
            raise TimeoutError("provider timed out")
        trace["responses"] += [
            SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, input_tokens_details=None)
            )
        ]
        return ["answer"]

    monkeypatch.setattr(openai_actor, "converse", converse)
    monkeypatch.setattr(openai_actor, "mcp_server", lambda *_: {})
    monkeypatch.setattr(
        openai_actor, "record_spend", lambda model, usage: spent.append(usage) or "recorded"
    )
    record: dict = {}
    played = openai_actor.play(
        {"id": "x", "prompt": "hi", "follow_up": "later", "pass": {}},
        REPO_ROOT,
        tmp_path,
        record,
        server_instructions="x",
        model="m",
        max_turns=5,
        timeout=5,
    )
    assert "TimeoutError" in played["fault"] and spent == [{"input": 10, "cached": 0, "output": 2}]
    assert record["usage_partial"]


# --- a store vault ----------------------------------------------------------------------------------------------

STORE_CASE = {
    "id": "store",
    "prompt": "I'm choosing between Alder, Birch and Cedar.",
    "transport": "http",
    "remote_owner": True,
    "store": True,
    "seed_collections": [PLANTS_SPEC],
    "pass": {"no_new_collection": True},
}


async def add_row_to(server: dict, collection: str, item: dict) -> dict:
    """One ``add_record`` call to the unit's loopback server, as an agent's client sends it."""
    from fastmcp import Client

    async with Client({"mcpServers": {"exomem": server}}) as client:
        reply = await client.call_tool(
            "add_record",
            {"collection": collection, "item": item, "why": "test"},
            raise_on_error=False,
        )
    return json.loads(reply.content[0].text)


def test_a_store_unit_enrols_the_vault_and_its_server_commits_a_write_to_the_store(
    tmp_path,
) -> None:
    """Without enrolment, or with a server that does not serve the store, the arm refuses a store write; the
    seed's own collections and the enrolment collection are the vault's before the session, never the session's."""
    root = tmp_path / "unit"
    with incidents.store_unit(root, REPO_ROOT) as store:
        _, record, _ = incidents.seed_unit(STORE_CASE, REPO_ROOT, root, None, store)
        assert sorted(record["collections_before"]) == sorted(
            [f"Knowledge Base/{PLANTS}", ENROLMENT_PATH]
        )
        env = store.environment(None)

        def mint(session: dict) -> str:
            return incidents.run_arm_side(
                REPO_ROOT, root, "mint", stdin=json.dumps(session), arm_env=env
            )["token"]

        with loopback_mcp(root, REPO_ROOT, mint, remote_owner=True, arm_env=env) as server:
            written = asyncio.run(add_row_to(server, ENROLMENT_PATH, {"title": "Dune"}))
        facts = incidents.run_arm_side(
            REPO_ROOT, root, "collections", str(root / "vault"), arm_env=env
        )
    assert written["operation"] == "append" and "error" not in written
    loans = next(item for item in facts["collections"] if item["path"] == ENROLMENT_PATH)
    assert [row["title"] for row in loans["rows"]] == ["Dune"]
    assert incidents.no_new_collection(facts, record["collections_before"]) is True


#: What an agent's create and append calls leave in a store vault: the same dispatcher call that its tools make,
#: made in a process that serves the store.
CREATE_IN_THE_STORE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arm_side
from exomem.governance.principal import owner_principal, request_scope
vault, path, text, names = Path(sys.argv[2]), sys.argv[3], sys.argv[4], json.loads(sys.argv[5])
with arm_side.served_store(), request_scope(owner_principal(surface="mcp")):
    arm_side.record_memory(vault, action="create", manifest_path=path, manifest_text=text, why="test")
    for name in names:
        arm_side.record_memory(vault, action="append", collection=path, item={"title": name}, why="test")
"""
SHORTLIST_PATH = "Knowledge Base/Records/Shortlist/_collection.md"


def test_a_store_unit_scores_a_collection_the_session_started_in_the_store(
    tmp_path, monkeypatch
) -> None:
    """The unit's checks must read the store that the session wrote to: a read in a process that does not
    serve it would answer "unavailable", and every store unit would be unknown."""
    names = ["Alder", "Birch", "Cedar"]
    manifest_text = (
        store_module.ENROLMENT_MANIFEST.replace("Library loans", "Shortlist")
        .replace("6f1c2d01-5c0f-4f8e-9a51-4a438d0b6b4a", "0d5b7a64-83c1-4e29-b6f0-9a41c8e2d537")
        .replace("borrowed: {type: string}", "note: {type: string}")
    )

    def start_the_collection(root: Path, arm_env: dict) -> None:
        arm_side = Path(incidents.__file__).with_name("arm_side.py")
        env = child_env(root, REPO_ROOT, arm_env)
        subprocess.run(
            [
                sys.executable,
                "-c",
                CREATE_IN_THE_STORE,
                str(arm_side.parent),
                str(root / "vault"),
                SHORTLIST_PATH,
                manifest_text,
                json.dumps(names),
            ],
            env=env,
            cwd=root,
            check=True,
            capture_output=True,
            timeout=300,
        )

    def player(case, arm, root, work, record, **session):
        start_the_collection(root, session["arm_env"])
        return {"fault": None, "extra": {}, "tools": [], "calls": [], "answer": "Alder."}

    monkeypatch.setitem(incidents.PLAYERS, "claude", player)
    case = {
        **STORE_CASE,
        "pass": {
            "new_collection": {
                "max_fields": 4,
                "min_rows": 3,
                "rows_each": [has("alder"), has("birch"), has("cedar")],
            }
        },
    }
    result = run(case, tmp_path, StubJudge())
    assert result["checks"] == {"new_collection": True} and result["passed"] is True
    assert [item["title"] for item in result["new_collections"]] == ["Shortlist"]
    assert result["store"]["collection"] == ENROLMENT_PATH


def test_a_unit_whose_store_cannot_be_enrolled_stops_before_the_session(
    tmp_path, monkeypatch
) -> None:
    """A refused enrolment is the harness's fault: the unit raises, which the run records as unknown."""
    real = incidents.store_unit

    @contextlib.contextmanager
    def with_the_wrong_operator(root, arm, arm_env=None):
        with real(root, arm, arm_env) as store:
            yield dataclasses.replace(store, operator="not-the-operator")

    monkeypatch.setattr(incidents, "store_unit", with_the_wrong_operator)
    monkeypatch.setitem(
        incidents.PLAYERS, "claude", lambda *a, **k: pytest.fail("the session must not start")
    )
    with pytest.raises(RuntimeError, match="enrolling the collection store failed"):
        run(STORE_CASE, tmp_path, StubJudge())


def test_each_store_variant_is_its_base_case_on_a_store_vault() -> None:
    cases = {case["id"]: case for case in incidents.load_cases(STRUCTURED)}
    for base in ("evaluation-of-named-candidates", "reuse-compatible-collection"):
        variant = cases[f"{base}-store"]
        assert variant["store"] is True
        assert {k: v for k, v in variant.items() if k not in ("id", "about", "store")} == {
            k: v for k, v in cases[base].items() if k not in ("id", "about")
        }


# --- the shipped cases ----------------------------------------------------------------------------------------

STRUCTURED = [
    "evaluation-of-named-candidates",
    "equipment-maintenance",
    "reuse-compatible-collection",
    "correct-a-stored-value",
    "ambiguous-identity",
    "passing-mention",
    "capture-off",
    "decision-agreed",
    "evaluation-of-named-candidates-store",
    "reuse-compatible-collection-store",
]
#: A prompt that asks for the saving would measure instruction following, not whether the agent recognised the work.
SAVING_TALK = re.compile(
    r"\b(records?|collections?|save[sd]?|saving|logs?|logged|logging|exomem|remember)\b", re.I
)


def test_each_structured_capture_case_loads_and_no_user_turn_asks_for_the_saving() -> None:
    cases = incidents.load_cases(STRUCTURED)
    assert [case["id"] for case in cases] == STRUCTURED
    for case in cases:
        turns = [*incidents.user_turns(case), *([case["follow_up"]] if "follow_up" in case else [])]
        assert not [turn for turn in turns if SAVING_TALK.search(turn)], case["id"]


def test_every_collection_a_structured_capture_case_seeds_is_accepted_by_the_product(
    tmp_path,
) -> None:
    """A manifest the product refuses would make every unit of its case unknown."""
    # A store variant seeds the collections of its base case again, and one vault holds each only once.
    seeds = list(
        {
            collection["manifest_path"]: collection
            for case in incidents.load_cases(STRUCTURED)
            for collection in case.get("seed_collections", ())
        }.values()
    )
    assert len(seeds) == 5
    vault = fresh_vault(tmp_path, REPO_ROOT)
    written = incidents.run_arm_side(
        REPO_ROOT, tmp_path, "seed_collections", str(vault), stdin=json.dumps(seeds)
    )
    assert [item["path"] for item in written] == [
        f"Knowledge Base/{seed['manifest_path']}" for seed in seeds
    ]


@pytest.mark.reads_repository_docs
def test_capture_off_is_a_prominence_the_instruction_block_can_be_laid_for(tmp_path) -> None:
    case = incidents.load_cases(["capture-off"])[0]
    assert case["prominence"] == "off"
    assert (
        lay_instruction_block(tmp_path / "block.txt", REPO_ROOT, case["prominence"])["prominence"]
        == "off"
    )
