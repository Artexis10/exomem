"""The incident runner scores agent-managed structure: what a restructure left in the vault and what the user was told.

Each unit below plays a stubbed session on a vault seeded from a real case of ``agent-managed-structure.json``:
the stub makes the writes that an agent would make, through the arm's own records commands, and the runner's
checks then read the vault as they do after a real session. Merge, rekey and undo are not in the product at this
head, so the history that they leave is written by hand in the tests that follow a merge or an undo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import structure
from membench.outcome.isolation import REPO_ROOT, child_env
from membench.outcome.judging import Judgement

# Each unit that seeds starts the product in fresh interpreters, which takes a while on a busy machine.
pytestmark = pytest.mark.timeout(300)

#: Calls made in a process that serves the unit's store (a plain file vault needs no serving), as an agent's tools make them.
WRITE_THROUGH_THE_PRODUCT = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arm_side
from exomem.governance.principal import owner_principal, request_scope
vault, calls = Path(sys.argv[2]), json.loads(sys.argv[3])
with arm_side.served_store(), request_scope(owner_principal(surface="mcp")):
    for call in calls:
        arm_side.record_memory(vault, **call)
"""
QUOTES_VAULT_PATH = "Knowledge Base/Records/Joinery quotes/_collection.md"
LOOM_VAULT_PATH = "Knowledge Base/Records/Loom servicing/_collection.md"


def case_of(case_id: str) -> dict:
    return incidents.load_cases([case_id])[0]


class SchemaJudge:
    """A judge that never calls a model: it fills each property of the schema it is given (true, 0 or text).

    ``overrides`` set named properties, such as ``questions_asked``. It records the prompts it reads.
    """

    def __init__(self, **overrides) -> None:
        self.overrides, self.prompts = overrides, []

    def call(self, prompt, schema, *, out, timeout):
        self.prompts.append(prompt)
        fill = {"boolean": True, "integer": 0, "string": "stub"}
        verdict = {name: fill[spec["type"]] for name, spec in schema["properties"].items()}
        return Judgement(verdict | self.overrides, 0.0, [], "")


def write_through_the_product(root: Path, arm_env: dict | None, *calls: dict) -> None:
    arm_side = Path(incidents.__file__).with_name("arm_side.py")
    done = subprocess.run(  # noqa: S603 - fixed argv; the interpreter and arm are the runner's own
        [
            sys.executable,
            "-c",
            WRITE_THROUGH_THE_PRODUCT,
            str(arm_side.parent),
            str(root / "vault"),
            json.dumps(list(calls)),
        ],
        env=child_env(root, REPO_ROOT, arm_env),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-600:]


def play_stub(monkeypatch, act, *answers: str) -> None:
    """Replace the Claude actor with one that does ``act(root, arm_env)`` on the unit's real vault and answers as given."""

    def player(case, arm, root, work, record, **session):
        act(root, session.get("arm_env"))
        return {
            "fault": None,
            "extra": {},
            "tools": [],
            "calls": [],
            "answer": answers[-1],
            "answers": list(answers),
        }

    monkeypatch.setitem(incidents.PLAYERS, "claude", player)


def run(case: dict, root: Path, judge: SchemaJudge) -> dict:
    return incidents.run_unit(
        case, REPO_ROOT, root, judge=judge, model="m", executable="e", max_turns=1
    )


def keep_the_price_of_a_new_quote(root: Path, arm_env: dict | None) -> None:
    """What an agent does for a quote that carries a price: one append that adds the price field, as `new_fields` does."""
    item = {
        "customer": "Alder Lane Books",
        "quoted_on": "2026-11-14",
        "job": "Oak reading nook",
        "price": 1450,
    }
    write_through_the_product(
        root,
        arm_env,
        {
            "action": "append",
            "collection": QUOTES_VAULT_PATH,
            "item": item,
            "new_fields": ["price"],
            "why": "test",
        },
    )


def do_nothing(root: Path, arm_env: dict | None) -> None:
    return None


def test_a_session_that_kept_a_new_detail_in_plain_words_passes_every_structure_check(
    tmp_path, monkeypatch
) -> None:
    play_stub(
        monkeypatch,
        keep_the_price_of_a_new_quote,
        "I'll keep the price on your quotes from now on. 1450 for oak is fair.",
    )
    judge = SchemaJudge()
    result = run(case_of("quote-price-detail"), tmp_path, judge)
    assert result["checks"] == {
        "no_new_collection": True,
        "restructure": True,
        "rows_preserved": True,
        "plain_reply": True,
        "judge": True,
    }
    assert result["passed"] is True and result["reason"] is None
    # The judge reads the replies and the user's turn, and is told nothing of where the rows went.
    reads_plain_reply = next(p for p in judge.prompts if "You grade what an assistant said" in p)
    assert (
        "I'll keep the price on your quotes" in reads_plain_reply
        and "Alder Lane" in reads_plain_reply
    )
    assert (tmp_path / "verdict.plain-reply.json").exists()


def test_a_session_that_kept_nothing_and_named_a_path_fails_each_structure_check(
    tmp_path, monkeypatch
) -> None:
    """A judge that likes the reply cannot rescue it from a path: the check reads the vault's own path forms."""
    play_stub(monkeypatch, do_nothing, "Fair price. I filed it under Records/Joinery quotes.")
    result = run(case_of("quote-price-detail"), tmp_path, SchemaJudge())
    assert result["checks"]["restructure"] is False  # no shape-history entry
    assert result["checks"]["rows_preserved"] is False  # the new quote is not a row
    assert result["checks"]["plain_reply"] is False
    assert "Records/Joinery quotes" in result["reason"] and result["passed"] is False


def test_a_session_that_changed_nothing_passes_an_advisory_case(tmp_path, monkeypatch) -> None:
    play_stub(monkeypatch, do_nothing, "Want me to keep each service of a part as its own entry?")
    result = run(case_of("service-log-advisory"), tmp_path, SchemaJudge(questions_asked=1))
    assert result["checks"] == {
        "no_write": True,
        "restructure": True,
        "rows_preserved": True,
        "plain_reply": True,
    }
    (before,) = result["shape_before"].values()
    assert len(before.pop("entry_ids")) == 1  # the entry that the seed's create wrote
    assert result["shape_before"] == {
        "Records/Loom servicing/_collection.md": {
            "title": "Loom servicing",
            "natural_key": ["part"],
            "fields": ["part", "serviced_on", "work_done"],
            "lifecycle": "active",
        }
    }


def test_a_session_that_reshaped_a_collection_it_was_told_to_leave_fails_the_off_case(
    tmp_path, monkeypatch
) -> None:
    """Every row is still there, so only the shape history and the title can show that the shape was touched."""

    def retitle(root: Path, arm_env: dict | None) -> None:
        step = {"step": "retitle", "title": "Loom service log"}
        write_through_the_product(
            root,
            arm_env,
            {
                "action": "restructure",
                "collection": LOOM_VAULT_PATH,
                "steps": [step],
                "why": "test",
            },
        )

    play_stub(monkeypatch, retitle, "Every 3 months or so.")
    result = run(case_of("service-log-off"), tmp_path, SchemaJudge())
    assert result["checks"] == {"no_write": True, "restructure": False, "rows_preserved": True}
    assert result["passed"] is False


def test_a_store_unit_reads_the_shape_history_and_rows_of_a_store_collection(
    tmp_path, monkeypatch
) -> None:
    """The store's history and rows answer only in a process that serves it; a plain read would leave every store unit unknown."""
    case = {
        **case_of("service-log-new-key"),
        "pass": {
            "restructure": {
                "collections": ["Records/Loom servicing/_collection.md"],
                "entries": [{"origin": "automatic", "step": "add_field"}],
            },
            "rows_preserved": {"equals": 4, "added": [["Reed hook", "2026-11-12"]]},
        },
    }

    def add_a_row_and_a_field(root: Path, arm_env: dict | None) -> None:
        item = {
            "part": "Reed hook",
            "serviced_on": "2026-11-12",
            "work_done": "Replaced",
            "technician": "Ines",
        }
        write_through_the_product(
            root,
            arm_env,
            {
                "action": "append",
                "collection": LOOM_VAULT_PATH,
                "item": item,
                "new_fields": ["technician"],
                "why": "test",
            },
        )

    play_stub(monkeypatch, add_a_row_and_a_field, "Done.")
    result = run(case, tmp_path, SchemaJudge())
    assert result["checks"] == {"restructure": True, "rows_preserved": True}
    # The seed made the collection in the store, not as item files: a store case that seeded files would test nothing.
    assert "strategy: sqlite" in (tmp_path / "vault" / LOOM_VAULT_PATH).read_text()


# --- what the checks read when the history is written by hand (merge, rekey and undo are not in the product yet) -------


WINDOW_PATH, BALCONY_PATH = (
    "Records/Window plants/_collection.md",
    "Records/Balcony plants/_collection.md",
)
WINDOW_ID, BALCONY_ID = (
    "8d2f6b19-4a73-4c0e-b5d1-7e9a3c2f0d48",
    "f1a7c5e3-0b94-4d62-8a3e-6c2d9b1e5f07",
)
PLANTS = case_of("plant-lists-merge")["seed_collections"]
PLANT_SPEC = {"equals": 4, "retired": {"collections": [WINDOW_PATH, BALCONY_PATH], "equals": 1}}


def entry(
    entry_id: str,
    collection_id: str,
    steps: list[dict],
    *,
    origin: str = "automatic",
    undoes: str | None = None,
    at: str = "2026-11-14T10:00:00Z",
) -> dict:
    call = {"collection": collection_id, "undo": entry_id}
    return {
        "id": entry_id,
        "collection_id": collection_id,
        "at": at,
        "origin": origin,
        "steps": steps,
        "undoes": undoes,
        "undo_call": call,
    }


def read(
    path: str,
    collection_id: str,
    title: str,
    rows: list[dict],
    *,
    history=(),
    lifecycle="active",
    fields=("plant", "water_every_days"),
) -> dict:
    """What `arm_side shape` reports: identity, shape, history (newest first) and rows."""
    return {
        "path": f"Knowledge Base/{path}",
        "collection_id": collection_id,
        "title": title,
        "lifecycle": lifecycle,
        "natural_key": ["plant"],
        "fields": list(fields),
        "history": list(history),
        "rows": rows,
    }


WINDOW_ROWS = [
    {"plant": "Fern", "water_every_days": 7, "light": "Bright shade"},
    {"plant": "Pothos", "water_every_days": 10, "light": "Indirect"},
    {"plant": "Peace lily", "water_every_days": 5, "light": "Low light"},
]
BALCONY_ROWS = [
    {"plant": "Pothos", "water_every_days": 10, "spot": "Rail"},
    {"plant": "Fern", "water_every_days": 7, "spot": "Corner"},
    {"plant": "Lavender", "water_every_days": 4, "spot": "Rail"},
]
#: The two lists after the window list merged into the balcony list: the shared plants are folded once, and the
#: window list's `light` is carried to the field `sun` by the step's mapping.
MERGED_ROWS = [
    {"plant": "Fern", "water_every_days": 7, "spot": "Corner", "sun": "Bright shade"},
    {"plant": "Pothos", "water_every_days": 10, "spot": "Rail", "sun": "Indirect"},
    {"plant": "Peace lily", "water_every_days": 5, "sun": "Low light"},
    {"plant": "Lavender", "water_every_days": 4, "spot": "Rail"},
]
MERGE = [{"step": "merge_into", "target": "Balcony plants", "mapping": {"light": "sun"}}]


def merged(rows: list[dict]) -> dict:
    """The reads after a merge: the history sits on the window list, which is retired and holds no rows."""
    history = [entry("m1", WINDOW_ID, MERGE)]
    return {
        WINDOW_PATH: read(
            WINDOW_PATH, WINDOW_ID, "Window plants", [], history=history, lifecycle="retired"
        ),
        BALCONY_PATH: read(
            BALCONY_PATH,
            BALCONY_ID,
            "Balcony plants",
            rows,
            fields=("plant", "water_every_days", "spot", "sun"),
        ),
    }


def preserved(reads: dict, spec: dict, seeded: list[dict] = PLANTS) -> bool | None:
    # The seed's rows are the case's own; the plain lists above are the same rows with the case's field names.
    return structure.rows_preserved(reads, seeded, structure.refs_of(reads), spec)


def test_a_merge_is_followed_to_the_target_under_its_mapping() -> None:
    assert preserved(merged(MERGED_ROWS), {**PLANT_SPEC}) is True


def test_a_value_that_the_mapping_did_not_carry_fails_rows_preserved() -> None:
    lost = [
        {k: v for k, v in row.items() if not (row["plant"] == "Fern" and k == "sun")}
        for row in MERGED_ROWS
    ]
    assert preserved(merged(lost), PLANT_SPEC) is False


def test_a_merge_the_user_asked_to_undo_leaves_the_rows_in_their_own_lists() -> None:
    merge, undo = (
        entry("m1", WINDOW_ID, MERGE),
        entry("u1", WINDOW_ID, [{"step": "restore"}], undoes="m1", at="2026-11-14T10:05:00Z"),
    )
    back = {
        WINDOW_PATH: read(
            WINDOW_PATH,
            WINDOW_ID,
            "Window plants",
            WINDOW_ROWS,
            history=[undo, merge],
            fields=("plant", "water_every_days", "light"),
        ),
        BALCONY_PATH: read(
            BALCONY_PATH,
            BALCONY_ID,
            "Balcony plants",
            BALCONY_ROWS,
            fields=("plant", "water_every_days", "spot"),
        ),
    }
    apart = {"equals": 6, "retired": {"collections": [WINDOW_PATH, BALCONY_PATH], "equals": 0}}
    assert preserved(back, apart) is True
    # The same rows with no undo on record are a merge whose rows never arrived at the target.
    still_merged = {**back, WINDOW_PATH: {**back[WINDOW_PATH], "history": [merge]}}
    assert preserved(still_merged, apart) is False


def test_a_merge_into_a_collection_that_no_read_answers_for_is_unknown_not_a_loss() -> None:
    reads = merged(MERGED_ROWS)
    reads[WINDOW_PATH]["history"] = [
        entry("m1", WINDOW_ID, [{"step": "merge_into", "target": "Orchard plants"}])
    ]
    assert preserved(reads, PLANT_SPEC) is None


def test_a_renamed_field_is_read_under_its_new_name() -> None:
    rename = entry("r1", WINDOW_ID, [{"step": "rename_field", "field": "light", "to": "sun_needs"}])
    rows = [
        {"plant": r["plant"], "water_every_days": r["water_every_days"], "sun_needs": r["light"]}
        for r in WINDOW_ROWS
    ]
    window = read(
        WINDOW_PATH,
        WINDOW_ID,
        "Window plants",
        rows,
        history=[rename],
        fields=("plant", "water_every_days", "sun_needs"),
    )
    balcony = read(
        BALCONY_PATH,
        BALCONY_ID,
        "Balcony plants",
        BALCONY_ROWS,
        fields=("plant", "water_every_days", "spot"),
    )
    reads, spec = {WINDOW_PATH: window, BALCONY_PATH: balcony}, {"equals": 6}
    assert preserved(reads, spec) is True
    dropped = [{k: v for k, v in row.items() if k != "sun_needs"} for row in rows]
    assert preserved({**reads, WINDOW_PATH: {**window, "rows": dropped}}, spec) is False


def test_an_answer_the_user_gave_corrects_the_seed_it_is_compared_with() -> None:
    """The balcony list's fern said 10 and the user's answer was 7: the merged row holds 7, and 10 is not a loss."""
    disagreeing = case_of("plant-lists-disagree")["seed_collections"]
    spec = {
        **PLANT_SPEC,
        "corrected": [
            {
                "collection": BALCONY_PATH,
                "row": {"plant": "Fern"},
                "field": "water_every_days",
                "value": 7,
            }
        ],
    }
    assert preserved(merged(MERGED_ROWS), spec, disagreeing) is True
    # Merged with the old value, the user's answer is not what the list holds.
    wrong = [
        {**row, "water_every_days": 10} if row["plant"] == "Fern" else row for row in MERGED_ROWS
    ]
    assert preserved(merged(wrong), spec, disagreeing) is False


# --- the shape history ------------------------------------------------------------------------------------------------


def shaped(history: list[dict] | None, **facts) -> dict:
    return read(BALCONY_PATH, BALCONY_ID, "Balcony plants", [], history=history or (), **facts) | (
        {"history": None} if history is None else {}
    )


def before() -> dict:
    return {
        "entry_ids": [],
        "title": "Balcony plants",
        "natural_key": ["plant"],
        "fields": ["plant", "water_every_days"],
        "lifecycle": "active",
    }


def wants(*entries: dict, **spec) -> dict:
    return {"entries": list(entries), **spec}


def test_an_entry_counts_only_with_the_expected_origin_and_its_undo_call() -> None:
    merge = entry("m1", BALCONY_ID, MERGE, origin="user_choice")
    expected = wants({"origin": "user_choice", "step": "merge_into"})
    assert structure.restructure([shaped([merge])], [before()], expected) is True
    assert (
        structure.restructure([shaped([{**merge, "origin": "automatic"}])], [before()], expected)
        is False
    )
    assert (
        structure.restructure(
            [shaped([{k: v for k, v in merge.items() if k != "undo_call"}])], [before()], expected
        )
        is False
    )


def test_an_entry_the_seed_left_is_not_the_sessions() -> None:
    seeded = entry("s0", BALCONY_ID, [{"step": "add_field", "field": "spot", "type": "string"}])
    after = shaped([seeded])
    kept = {**before(), "entry_ids": ["s0"]}
    assert structure.restructure([after], [kept], {"none": True}) is True
    assert structure.restructure([after], [before()], {"none": True}) is False


def test_a_creation_entry_is_no_change_of_shape_and_carries_no_undo_call() -> None:
    """A collection's create writes an entry with no steps; the product offers no undo for it."""
    created = {k: v for k, v in entry("c0", BALCONY_ID, [], origin="user_design").items() if k != "undo_call"}
    assert structure.restructure([shaped([created])], [before()], {"none": True}) is True
    assert structure.restructure([shaped([created])], [before()], wants({"origin": "user_design"})) is True


def test_an_undone_entry_needs_the_undo_that_names_it() -> None:
    merge = entry("m1", BALCONY_ID, MERGE)
    undo = entry("u1", BALCONY_ID, [{"step": "restore"}], undoes="m1")
    expected = wants({"origin": "automatic", "step": "merge_into", "undone": True})
    assert structure.restructure([shaped([undo, merge])], [before()], expected) is True
    assert structure.restructure([shaped([merge])], [before()], expected) is False


def test_an_automatic_merge_fails_a_case_that_lets_automatic_changes_only_add_fields() -> None:
    add = entry("a1", BALCONY_ID, [{"step": "add_field", "field": "spot", "type": "string"}])
    choice = entry("c1", BALCONY_ID, MERGE, origin="user_choice")
    spec = wants(
        {"origin": "user_choice", "step": "merge_into"}, automatic_steps_among=["add_field"]
    )
    assert structure.restructure([shaped([choice, add])], [before()], spec) is True
    assert (
        structure.restructure([shaped([entry("a2", BALCONY_ID, MERGE), choice])], [before()], spec)
        is False
    )


def test_a_merge_made_before_the_user_answered_fails_the_order_check() -> None:
    """A merge without `automatic` records the user's choice by default, so only the turn shows that nobody chose."""
    steps = [{"step": "merge_into", "target": WINDOW_PATH}]
    merge = {"name": "manage_memory", "input": {"operation": "records.restructure",
                                                "arguments": {"collection": BALCONY_PATH, "steps": steps}}}
    # A door may carry its arguments as a JSON string; the check parses it.
    merge_as_text = {"name": "manage_memory", "input": {"operation": "records.restructure",
                                                        "arguments": json.dumps({"collection": BALCONY_PATH, "steps": steps})}}
    look = {"name": "query_records", "input": {"collection": WINDOW_PATH}}

    assert structure.step_order([[look], [merge]], {"merge_into": 2}) is True
    assert structure.step_order([[merge], [look]], {"merge_into": 2}) is False
    assert structure.step_order([[merge_as_text], [look]], {"merge_into": 2}) is False


def test_an_arm_with_no_shape_history_has_no_entry_and_a_collection_that_cannot_be_read_is_unknown() -> (
    None
):
    """The base arm answers no history: an expected entry is missing there, and no change leaves it unchanged."""
    assert (
        structure.restructure([shaped(None)], [before()], wants({"origin": "automatic"})) is False
    )
    assert structure.restructure([shaped(None)], [before()], {"none": True}) is True
    # A manifest rewritten by the old route leaves no entry, so the facts of the shape are what show the change.
    rewritten = shaped(None, fields=("plant", "water_every_days", "spot"))
    assert structure.restructure([rewritten], [before()], {"none": True}) is False
    assert (
        structure.restructure([{"error": "CollectionError: gone"}], [before()], {"none": True})
        is None
    )


# --- the reply ---------------------------------------------------------------------------------------------------------


def verdict(**changes) -> dict:
    return {
        "names_the_users_things_in_their_terms": True,
        "no_internal_names": True,
        "questions_asked": 0,
        "every_question_about_the_users_things": True,
        "reason": "stub",
    } | changes


NONE, ONE = {"questions": {"equals": 0}}, {"questions": {"equals": 1}}


def test_a_path_in_a_reply_is_the_vaults_own_path_and_the_users_words_for_the_list_are_not() -> (
    None
):
    forms = structure.path_forms(
        "/units/u1/vault",
        [
            {
                "path": "Knowledge Base/Records/Loom servicing/_collection.md",
                "kb_path": "Records/Loom servicing/_collection.md",
            }
        ],
    )
    assert (
        "/units/u1/vault" in forms
        and "Records/Loom servicing" in forms
        and "Knowledge Base/Records/Loom servicing" in forms
    )
    assert structure.leaked_paths(["I added it to your Loom servicing list."], forms) == []
    assert structure.leaked_paths(["It lives in Records/Loom servicing now."], forms) == [
        "Records/Loom servicing"
    ]


def test_the_judges_findings_and_the_question_count_decide_a_reply_without_a_path() -> None:
    assert structure.plain_reply([], verdict(), NONE) == (True, [])
    ok, problems = structure.plain_reply([], verdict(no_internal_names=False), NONE)
    assert ok is False and problems == ["the judge found `no_internal_names` false"]
    ok, problems = structure.plain_reply([], verdict(questions_asked=2), ONE)
    assert ok is False and "asked 2 questions" in problems[0]
    assert structure.plain_reply([], verdict(questions_asked=2), {"questions": {"at_most": 2}}) == (
        True,
        [],
    )


def test_a_path_fails_a_reply_even_when_the_judge_gave_no_verdict_and_a_clean_reply_is_then_unknown() -> (
    None
):
    assert structure.plain_reply(["Records/Loom servicing"], None, NONE)[0] is False
    assert structure.plain_reply([], None, NONE) == (None, [])
