"""A restructure that the agent starts on its own is bounded by the server.

`restructure` takes `automatic: true`. The flag is honoured only inside the bounds of `exomem.automatic_collection`; a
refusal changes nothing and the same call without the flag is unchanged. Cases drive the dispatcher that MCP, REST and
the CLI share, on a real temporary vault of each storage mode.
"""

from __future__ import annotations

import pytest
from conftest import initialize_vault_state_offline
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT
from test_automatic_collection import WHY, records_manifest
from test_automatic_collection import (
    silent_capture as silent_capture,  # noqa: F401 - autouse: capture is silent
)
from test_collection_store_service import service as service  # noqa: F401 - the fixture, by name
from test_collection_store_writer import manifest_path
from test_collection_store_writer import store as store  # noqa: F401 - the fixture, by name
from test_planning_mutation import _manifest as planning_manifest
from test_restructure import make_file_work, make_store_work
from test_restructure import mode as mode  # noqa: F401 - the fixture, by name
from test_restructure import work as work  # noqa: F401 - the fixture, by name
from test_single_purpose_tools_mcp import _call, _session
from test_single_purpose_tools_mcp import build as build  # noqa: F401 - the fixture, by name

from exomem import automatic_collection, envelope
from exomem.cli_ops import OpError
from exomem.plan_memory import plan_memory

MAX_FIELDS = automatic_collection.MAX_FIELDS
RETIRE_COUNT = [{"step": "retire_field", "field": "count"}]


def retitle(title: str) -> list[dict]:
    return [{"step": "retitle", "title": title}]


def history(work) -> list[dict]:
    return work.operate("inspect", collection=work.collection_id)["shape_history"]["entries"]


def test_an_automatic_restructure_commits_and_its_history_entry_says_the_agent_did_it(work):
    applied = work.restructure(steps=RETIRE_COUNT, automatic=True)

    assert applied["shape"]["origin"] == "automatic"
    assert history(work)[0]["id"] == applied["shape"]["id"]
    assert work.manifest()["item_schema"]["fields"]["count"]["retired"] is True


def test_an_automatic_restructure_names_no_origin(work):
    error = work.refused(steps=RETIRE_COUNT, automatic=True, origin="user_choice")

    assert error["code"] == "INVALID_RESTRUCTURE"


@pytest.mark.parametrize("level", ["advisory", "off"])
def test_an_automatic_restructure_needs_silent_capture(work, level):
    envelope.set_disposition("proactive_capture", level)
    manifest = work.manifest()

    error = work.refused(steps=RETIRE_COUNT, automatic=True)

    assert error["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
    assert error["bound"] == "proactive_capture" and error["disposition"] == level
    # Under advisory the agent proposes and restructures after the user confirms; under off only when the user asked.
    way = error["message"].split("; ")[-1]
    assert way == (
        "restructure without `automatic` only if the user asked for it" if level == "off"
        else "propose the change in plain words to the user, and restructure without `automatic` after they confirm"
    )
    assert work.manifest() == manifest
    # What the owner asked for stays served: the same steps, without the flag, still apply.
    assert work.restructure(steps=RETIRE_COUNT)["shape"]["origin"] == "user_choice"


@pytest.fixture
def agent_work(mode, tmp_path, request):
    """A two-row collection of this storage mode that the agent created on its own."""
    if mode == "store":
        return make_store_work(request.getfixturevalue("store"), automatic=True)
    return make_file_work(tmp_path, automatic=True)


def test_a_change_that_adds_fields_past_the_field_bound_is_refused_and_applies_without_the_flag(agent_work):
    work = agent_work
    room = MAX_FIELDS - len(work.manifest()["item_schema"]["fields"])
    over = [{"step": "add_field", "field": f"extra{n}", "type": "string"} for n in range(room + 1)]
    manifest = work.manifest()

    error = work.refused(steps=over, automatic=True)

    assert error["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED" and error["bound"] == "fields"
    assert error["fields"] == MAX_FIELDS + 1
    assert work.manifest() == manifest
    assert work.restructure(steps=over)["shape"]["origin"] == "user_choice"


def test_a_change_that_adds_fields_up_to_the_field_bound_is_accepted(agent_work):
    work = agent_work
    room = MAX_FIELDS - len(work.manifest()["item_schema"]["fields"])
    at_bound = [{"step": "add_field", "field": f"extra{n}", "type": "string"} for n in range(room)]

    assert work.restructure(steps=at_bound, automatic=True)["shape"]["origin"] == "automatic"


def test_a_collection_the_owner_made_takes_fields_past_the_bound_automatically(work):
    # The bound stops an agent's own collections from sprawling; an owner's collection is as wide as the owner made it.
    room = MAX_FIELDS - len(work.manifest()["item_schema"]["fields"])
    over = [{"step": "add_field", "field": f"extra{n}", "type": "string"} for n in range(room + 1)]

    applied = work.restructure(steps=over, automatic=True)

    assert applied["shape"]["origin"] == "automatic"
    assert len(work.manifest()["item_schema"]["fields"]) == MAX_FIELDS + 1


def test_a_user_design_protects_every_step_but_an_addition(mode, tmp_path, request):
    work = (
        make_store_work(request.getfixturevalue("store"), origin="user_design") if mode == "store"
        else make_file_work(tmp_path, origin="user_design")
    )
    manifest = work.manifest()

    error = work.refused(steps=RETIRE_COUNT, automatic=True)

    assert error["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
    assert error["bound"] == "decision" and error["decided_by"] == "user_design"
    assert work.manifest() == manifest
    added = work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}], automatic=True)
    assert added["shape"]["origin"] == "automatic"


def test_an_automatic_step_may_not_change_what_the_user_chose_or_undo_it(work):
    chosen = work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}])
    fields = work.manifest()["item_schema"]["fields"]

    own_field = work.refused(steps=[{"step": "retire_field", "field": "note"}], automatic=True)
    undo = work.refused(undo=chosen["shape"]["id"], automatic=True)

    assert own_field["bound"] == undo["bound"] == "decision"
    assert own_field["decision"] == undo["decision"] == chosen["shape"]["id"]
    assert work.manifest()["item_schema"]["fields"] == fields
    # What the user decided nothing about stays open to the agent.
    assert work.restructure(steps=RETIRE_COUNT, automatic=True)["shape"]["origin"] == "automatic"


def test_the_fourth_automatic_restructure_of_a_collection_in_a_day_is_refused_and_a_plain_one_is_not(work):
    for title in ["Plants", "Garden", "Yard"][: automatic_collection.MAX_RESTRUCTURES_PER_WINDOW]:
        work.restructure(steps=retitle(title), automatic=True)

    error = work.refused(steps=retitle("Beds"), automatic=True)

    assert error["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
    assert error["bound"] == "daily" and error["restructures"] == automatic_collection.MAX_RESTRUCTURES_PER_WINDOW
    assert work.restructure(steps=retitle("Beds"))["shape"]["origin"] == "user_choice"


def test_an_undo_and_a_field_addition_do_not_count_toward_the_daily_bound(work):
    applied = [work.restructure(steps=retitle(title), automatic=True) for title in ["Plants", "Garden"]]
    work.restructure(undo=applied[-1]["shape"]["id"], automatic=True)
    work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}], automatic=True)
    work.restructure(steps=[{"step": "add_field", "field": "more", "type": "string"}], automatic=True)

    # Two restructures counted, so a third still fits; the undo and both additions did not use the room.
    assert work.restructure(steps=retitle("Yard"), automatic=True)["shape"]["origin"] == "automatic"
    assert work.refused(steps=retitle("Beds"), automatic=True)["bound"] == "daily"


def test_a_restructure_older_than_a_day_leaves_room(work, monkeypatch):
    for title in ["Plants", "Garden", "Yard"]:
        work.restructure(steps=retitle(title), automatic=True)
    monkeypatch.setattr(automatic_collection, "window_start", lambda: "9999-01-01T00:00:00Z")

    assert work.restructure(steps=retitle("Beds"), automatic=True)["shape"]["origin"] == "automatic"


def test_a_split_that_creates_a_collection_passes_the_creation_bounds(store):
    work = make_store_work(store)
    for index, title in enumerate(["Ledger", "Notebook", "Roster"], start=1):
        store.create_collection(
            manifest_path().replace("Work", title), records_manifest(title, index), why=WHY, scaffold=False, automatic=True
        )
    split = [{"step": "split_out", "where": {"field": "title", "op": "eq", "value": "b"}, "title": "Later"}]
    rows = work.rows()

    error = work.refused(steps=split, automatic=True)

    assert error["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
    assert error["bound"] == "daily" and error["creation"] is True
    assert work.rows() == rows
    assert work.restructure(steps=split)["shape"]["origin"] == "user_choice"


SERVED_SPLIT = [{"step": "split_out", "where": {"field": "title", "op": "eq", "value": "Canonical"}, "title": "Apart"}]


def served_collections(root) -> list[str]:
    from contextlib import closing

    from exomem.collection_store import connection

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return sorted(cid for (cid,) in reader.execute("SELECT collection_id FROM collections"))


def test_a_served_automatic_split_may_be_the_last_automatic_creation_of_the_day(service):
    # The split's admitted create counts among the day's creations; the moves after it must not count it again.
    from test_collection_store_service import CID as SERVED
    from test_collection_store_service import call, titles

    root = service.root
    call(root, "record_memory", action="append", collection=SERVED, item={"title": "Kept"}, why=WHY)
    for index, title in enumerate(["Ledger", "Notebook"][: automatic_collection.MAX_PER_WINDOW - 1], start=1):
        call(root, "record_memory", action="create", manifest_path=manifest_path().replace("Work", title),
             manifest_text=records_manifest(title, index).replace("strategy: markdown-items\n", "strategy: sqlite\n"),
             why=WHY, scaffold=False, automatic=True)

    applied = call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, steps=SERVED_SPLIT,
                   automatic=True)

    (inverse,) = applied["shape"]["inverse"]
    assert applied["shape"]["origin"] == "automatic"
    assert (titles(root, SERVED), titles(root, inverse["target"])) == (["Kept"], ["Canonical"])


def test_a_served_automatic_split_that_a_bound_refuses_makes_no_collection(service):
    from test_collection_store_service import CID as SERVED
    from test_collection_store_service import call, titles

    root = service.root
    envelope.set_disposition("proactive_capture", "advisory")
    held = served_collections(root)

    with pytest.raises(OpError) as refused:
        call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, steps=SERVED_SPLIT,
             automatic=True)

    assert refused.value.as_public_dict()["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
    assert (served_collections(root), titles(root, SERVED)) == (held, ["Canonical"])


def test_the_public_door_takes_automatic_on_a_records_restructure(build, vault):
    arguments = {"collection": LEDGER_COLLECTION_PATH, "why": WHY}

    async def scenario(client):
        await _call(client, "manage_memory", {"operation": "records.create", "arguments": {
            "manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": LEDGER_MANIFEST_TEXT, "why": WHY}})
        return await _call(client, "manage_memory", {"operation": "records.restructure", "arguments": {
            **arguments, "steps": [{"step": "add_field", "field": "note", "type": "string"}], "automatic": True}})

    applied = _session(build(), scenario)

    assert applied["outcome"] == "committed" and applied["shape"]["origin"] == "automatic"


def test_a_planning_collection_restructures_automatically_inside_the_bounds_only(tmp_path):
    root = tmp_path / "planning-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="automatic planning restructure fixture")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    plan_memory(root, "create", manifest_path=collection, manifest_text=planning_manifest(), why=WHY)

    applied = plan_memory(
        root, "restructure", collection=collection, why=WHY, steps=[{"step": "retitle", "title": "Roadmap"}], automatic=True
    )
    envelope.set_disposition("proactive_capture", "advisory")
    with pytest.raises(OpError) as refused:
        plan_memory(
            root, "restructure", collection=collection, why=WHY, steps=[{"step": "retitle", "title": "Plan"}],
            automatic=True,
        )

    assert applied["shape"]["origin"] == "automatic"
    assert refused.value.as_public_dict()["code"] == "AUTOMATIC_RESTRUCTURE_REFUSED"
