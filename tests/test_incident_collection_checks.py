"""The incident runner's Records checks read what the arm's own code committed, and refuse what they cannot read.

A seeded collection, a row check or a receipt check that was wrong would pass or fail every unit of the
capture-routing cases the same way, so a case's outcome would say nothing about the product.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import openai_actor
from membench.outcome.isolation import REPO_ROOT, fresh_vault

# Each test that seeds starts the product in a fresh interpreter, which takes a while on a busy machine.
pytestmark = pytest.mark.timeout(240)

INCIDENTS, WINS = "Records/Incidents/_collection.md", "Records/Wins/_collection.md"
METHOD_V1 = "## Decision\n\nFile a row first. Version one.\n"
METHOD_V2 = "## Decision\n\nFile a row first and link the diagnosis note. Version two.\n"


def manifest(title: str, exomem_id: str, key: str, match: dict | None) -> dict:
    return {
        "type": "collection",
        "exomem_id": exomem_id,
        "title": title,
        "semantic_profile": "records",
        "collection_version": 1,
        "schema_version": 1,
        "lifecycle": "active",
        "storage": {"strategy": "markdown-items", "source": "Entries", "format_version": 1},
        "item_schema": {
            "natural_key": [key],
            "fields": {
                key: {"type": "string", "required": True},
                "related_notes": {"type": "array", "items": {"type": "link"}},
            },
        },
        "claims": {"terms": ["gizmo"], **({"match": match} if match else {})},
    }


COLLECTIONS = [
    {
        "manifest_path": INCIDENTS,
        "manifest": manifest(
            "Incidents", "11111111-1111-4111-8111-111111111111", "incident", {"type": ["failure"]}
        ),
        "rows": [
            {"incident": "first"},
            {"incident": "linked", "related_notes": ["[[Incident method]]"]},
        ],
        "method_page": {
            "title": "Incident method",
            "note_type": "insight",
            "content": METHOD_V1,
            "revisions": [METHOD_V2],
        },
    },
    {
        "manifest_path": WINS,
        "manifest": manifest("Wins", "22222222-2222-4222-8222-222222222222", "win", None),
        "rows": [{"win": "one"}],
    },
]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> dict:
    """One vault, seeded and listed by the arm's own code; the tests vary only which rows the seed 'left'."""
    root = tmp_path_factory.mktemp("collections")
    vault = fresh_vault(root, REPO_ROOT)
    written = incidents.run_arm_side(
        REPO_ROOT, root, "seed_collections", str(vault), stdin=json.dumps(COLLECTIONS)
    )
    listed = incidents.run_arm_side(REPO_ROOT, root, "records", str(vault), INCIDENTS, WINS)
    ids = {
        name: {
            row["incident" if name == INCIDENTS else "win"]: row["record_id"]
            for row in rows["rows"]
        }
        for name, rows in listed.items()
    }
    return {"vault": vault, "written": written, "listed": listed, "ids": ids}


def test_the_method_page_is_written_first_and_its_last_revision_is_the_one_the_manifest_resolves(
    seeded,
) -> None:
    """A manifest that declares a method nothing resolves, or the first version of a revised page, would make
    the changed-method case test the wrong requirement."""
    method = seeded["written"][0]
    assert method["method_declared"].startswith("exomem://memory/")
    page = (seeded["vault"] / method["method_page"]).read_text(encoding="utf-8")
    assert METHOD_V2.strip() in page and "Version one" not in page


def test_an_arm_that_parses_capture_declares_it_and_the_unit_records_that(seeded, tmp_path) -> None:
    assert [item["capture_supported"] for item in seeded["written"]] == [True, True]
    seed = incidents.load_cases(["existing-workflow"])[0]
    seed = {"id": "x", "pass": seed["pass"], "seed_collections": seed["seed_collections"]}
    record = incidents.seed_unit(seed, REPO_ROOT, tmp_path)[1]
    assert record["capture_supported"] is True


@pytest.fixture
def arm_without_capture(tmp_path):
    """An arm whose manifest does not carry `capture`, as the revision before the declaration shipped it.

    The arm is this checkout's own product code, linked file by file, with the two modules that hold the
    manifest field and its one reader on the seeding path copied minus `capture_method`: the harness meets a
    real product that lacks the feature, and not a stub of its own seeding.
    """
    package = tmp_path / "arm" / "src" / "exomem"
    package.mkdir(parents=True)
    without = {
        "structured_collections.py": (
            "    capture_method: str | None = None\n",
            "        capture_method=capture_method,\n",
        ),
        "due_state.py": ("                capture_method=manifest.capture_method,\n",),
    }
    for entry in (REPO_ROOT / "src" / "exomem").iterdir():
        if entry.name not in (*without, "__pycache__"):
            (package / entry.name).symlink_to(entry)
    for name, lines in without.items():
        text = (REPO_ROOT / "src" / "exomem" / name).read_text(encoding="utf-8")
        for line in lines:
            assert line in text, f"{name} no longer carries {line.strip()!r}"
            text = text.replace(line, "")
        (package / name).write_text(text, encoding="utf-8")
    return tmp_path / "arm"


def test_an_arm_that_does_not_parse_capture_is_seeded_without_the_declaration_and_the_unit_says_so(
    arm_without_capture, tmp_path
) -> None:
    """Seeding a `capture` block that the arm ignores would measure an arm that cannot read the method, and
    reading it back would crash the seed of every unit of that arm."""
    case = incidents.load_cases(["existing-workflow"])[0]
    case = {"id": "x", "pass": case["pass"], "seed_collections": case["seed_collections"]}
    record = incidents.seed_unit(case, arm_without_capture, tmp_path / "unit")[1]
    declaring = record["collections"][0]
    assert record["capture_supported"] is False
    assert declaring["method_page"] and declaring["method_declared"] is None
    manifest = (tmp_path / "unit" / "vault" / declaring["path"]).read_text(encoding="utf-8")
    assert "capture:" not in manifest


@pytest.mark.parametrize(
    ("left", "spec", "expected"),
    [
        # The seed left the first row; the linked row is the session's, and it links a note.
        (["first"], {"collection": INCIDENTS, "absent_in": [WINS]}, True),
        (
            ["first"],
            {
                "collection": INCIDENTS,
                "where": [{"field": "incident", "op": "eq", "value": "LINKED"}],
            },
            True,
        ),
        (
            ["first"],
            {
                "collection": INCIDENTS,
                "where": [{"field": "incident", "op": "eq", "value": "first"}],
            },
            False,
        ),
        # A row whose link list is empty or missing must not read as a link.
        (
            ["linked"],
            {
                "collection": INCIDENTS,
                "where": [
                    {"field": "related_notes", "op": "ne", "value": ""},
                    {"field": "related_notes", "op": "ne", "value": "[]"},
                ],
            },
            False,
        ),
        (
            ["first"],
            {
                "collection": INCIDENTS,
                "where": [
                    {"field": "related_notes", "op": "ne", "value": ""},
                    {"field": "related_notes", "op": "ne", "value": "[]"},
                ],
            },
            True,
        ),
        # Rows the seed left are not the session's: no new row fails, and so does a second one.
        (["first", "linked"], {"collection": INCIDENTS}, False),
        ([], {"collection": INCIDENTS}, False),
        ([], {"collection": INCIDENTS, "equals": 2}, True),
    ],
)
def test_collection_row_counts_the_rows_the_session_added_and_checks_each_condition(
    seeded, left, spec, expected
) -> None:
    before = {
        INCIDENTS: [seeded["ids"][INCIDENTS][name] for name in left],
        WINS: list(seeded["ids"][WINS].values()),
    }
    assert incidents.collection_row(seeded["listed"], before, spec) is expected


def test_a_row_in_a_decoy_collection_fails_the_check_however_good_the_target_row_is(seeded) -> None:
    before = {
        INCIDENTS: [seeded["ids"][INCIDENTS]["first"]],
        WINS: [],
    }  # the session "added" the win
    assert (
        incidents.collection_row(
            seeded["listed"], before, {"collection": INCIDENTS, "absent_in": [WINS]}
        )
        is False
    )


def test_a_collection_the_arm_could_not_list_is_unknown_and_not_an_empty_one(seeded) -> None:
    listed = {**seeded["listed"], WINS: {"error": "unreadable"}}
    assert (
        incidents.collection_row(
            listed, {INCIDENTS: [], WINS: []}, {"collection": INCIDENTS, "absent_in": [WINS]}
        )
        is None
    )


REFUSED = json.dumps(
    {"error": {"code": "RELATION_DISPOSITION_MISSING", "message": "Nothing was saved."}}
)
SAVED = json.dumps({"user_summary": "Saved -> A note; not filed in Incidents (personal Evermatter)"})
UNFILED = json.dumps({"user_summary": "Saved -> A note (personal Evermatter)"})


def call(name: str, result: str | None, is_error: bool | None = False) -> dict:
    return {"name": name, "input": {}, "result": result, "is_error": is_error}


SPEC = {"tools": ["create_note"], "before": ["add_record"], "contains": "not filed in Incidents"}


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([call("create_note", SAVED), call("add_record", "{}")], True),
        # The note the product did not flag is the incident: the agent was never pointed at the Record.
        ([call("create_note", UNFILED), call("add_record", "{}")], False),
        # A refused write saved nothing, on either transport's way of saying so.
        (
            [call("create_note", REFUSED), call("create_note", SAVED), call("add_record", "{}")],
            True,
        ),
        ([call("create_note", "refused", is_error=True), call("create_note", SAVED)], True),
        # Filing first, or a note after the row, owes no pointer; no note at all owes none.
        ([call("add_record", "{}"), call("create_note", UNFILED)], True),
        ([call("recall_context", "{}")], True),
        # A write with no result to read proves nothing either way.
        ([call("create_note", None, is_error=None), call("add_record", "{}")], None),
    ],
)
def test_the_write_receipt_check_reads_every_note_receipt_that_came_before_the_row(
    calls, expected
) -> None:
    assert incidents.write_receipt(calls, SPEC) is expected


def test_the_openai_actors_tool_calls_pair_with_their_results_by_id_and_read_the_servers_text() -> (
    None
):
    """Outputs of parallel calls come back out of order, and the SDK wraps the server's text in a block; a
    check that read the wrong result, or a repr of the block, could never see the receipt."""

    def called(call_id: str, name: str):
        return SimpleNamespace(
            type="tool_call_item",
            raw_item=SimpleNamespace(name=name, arguments='{"k": 1}', call_id=call_id),
        )

    def returned(call_id: str, output: object):
        return SimpleNamespace(
            type="tool_call_output_item", raw_item={"call_id": call_id}, output=output
        )

    items = [
        called("a", "create_note"),
        called("b", "search_memory"),
        returned("b", {"type": "text", "text": "search result"}),
        returned("a", {"type": "text", "text": SAVED}),
    ]
    calls = openai_actor.tool_calls(items)
    assert [(c["name"], c["input"]) for c in calls] == [
        ("create_note", {"k": 1}),
        ("search_memory", {"k": 1}),
    ]
    assert incidents.write_receipt(calls, SPEC) is True and calls[1]["result"] == "search result"


def valid_case() -> dict:
    case = copy.deepcopy(incidents.load_cases(["existing-workflow"])[0])
    case["seed_collections"][0]["manifest_path"] = INCIDENTS
    return case


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda c: c.update(prominence="loud"), "prominence"),
        (lambda c: c.update(remote_owner="yes"), "remote_owner"),
        (lambda c: c.update(transport="stdio"), "remote_owner"),
        (
            lambda c: c["pass"]["collection_row"].update(collection="Records/Typo/_collection.md"),
            "collection_row names",
        ),
        (
            lambda c: c["pass"]["collection_row"].update(absent_in=["Records/Typo/_collection.md"]),
            "collection_row names",
        ),
        (lambda c: c["pass"]["collection_row"].update(equals=-1), "integer"),
        (
            lambda c: c["pass"]["collection_row"].update(
                where=[{"field": "x", "op": "like", "value": "y"}]
            ),
            "ops among",
        ),
        (lambda c: c["pass"]["write_receipt"].update(contains=""), "write_receipt needs"),
        (lambda c: c["seed_collections"][0].pop("manifest"), "seed collection has"),
        (lambda c: c["pass"].pop("collection_row"), "include_rows"),
    ],
)
def test_a_case_whose_collection_checks_could_not_read_what_they_name_is_refused(
    edit, message
) -> None:
    case = incidents.load_cases(["existing-workflow"])[0]
    edit(case)
    with pytest.raises(SystemExit, match=message):
        incidents.validate_case(case)


def test_a_provider_error_makes_the_openai_unit_unknown_and_not_a_failed_one(
    tmp_path, monkeypatch
) -> None:
    async def outage(*_args, **_kwargs):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(openai_actor, "converse", outage)
    monkeypatch.setattr(openai_actor, "mcp_server", lambda *_: {})
    record: dict = {}
    played = openai_actor.play(
        {"prompt": "hi"},
        REPO_ROOT,
        tmp_path,
        record,
        server_instructions="x",
        model="m",
        max_turns=1,
        timeout=5,
    )
    # The runner turns a fault into an unknown unit (`run_unit`), and no check reads an empty session.
    assert "TimeoutError" in played["fault"] and played["calls"] == []
    assert record["usage"].startswith("unknown")


def test_a_run_that_stops_part_way_still_meters_its_tokens_and_keeps_its_tool_calls(
    tmp_path, monkeypatch
) -> None:
    """A session that hits its turn limit spent real tokens; an unmetered unit would understate the eval's cost."""
    spent = []
    monkeypatch.setattr(
        openai_actor, "record_spend", lambda model, usage: spent.append(usage) or "recorded"
    )
    monkeypatch.setattr(openai_actor, "mcp_server", lambda *_: {})

    class MaxTurns(Exception):
        run_data = SimpleNamespace(
            raw_responses=[
                SimpleNamespace(
                    usage=SimpleNamespace(
                        input_tokens=10, output_tokens=2, input_tokens_details=None
                    )
                )
            ],
            new_items=[
                SimpleNamespace(
                    type="tool_call_item",
                    raw_item=SimpleNamespace(name="recall_context", arguments="{}", call_id="a"),
                )
            ],
        )

    async def stopped(*_args, **_kwargs):
        raise MaxTurns("Max turns (12) exceeded")

    monkeypatch.setattr(openai_actor, "converse", stopped)
    record: dict = {}
    played = openai_actor.play(
        {"prompt": "hi"},
        REPO_ROOT,
        tmp_path,
        record,
        server_instructions="x",
        model="m",
        max_turns=12,
        timeout=5,
    )
    assert "Max turns" in played["fault"] and played["tools"] == ["recall_context"]
    assert (
        spent == [{"input": 10, "cached": 0, "output": 2}] and record["spend_receipt"] == "recorded"
    )
