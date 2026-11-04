"""Replay of the shipped promotion flow, end to end, on an invented corpus.

A scripted decider (no model) writes one rich turn about a person's sailing boat and the harbour
where it is kept. It follows the receipt's `undeclared_referents` block: it saves one entity
type through the vocabulary door, creates the boat through the block's own route and authors the
ownership and location edges. The real publication pipeline then runs, and a fresh session
activates context. Two twin names sit in the same turn and the script declines them.

Every call goes through the in-process MCP server, so the receipt, the routes and the packet are
read exactly as a client reads them. The decider reads response fields only; the checks at the end
read the vault. Every name and place here is invented.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from test_recent_promotions import _as
from test_undeclared_referents import _committed, _served_with_graph

from exomem import (
    due_state,
    graph_sync,
    lexstore,
    review_state,
    working_set_index,
    working_set_runtime,
)

PERSON = "Imogen Hale"
HARBOUR = "Fenwick Quay"
BOAT = "Curlew"
INCIDENTAL_TWIN = "Corin Vale"
ONE_OFF_TWIN = "Open Day"

#: The script's judgment. Which name deserves an entity is the agent's call, so the script holds
#: it as data: the boat is promoted, and a name with no entry here is declined.
PROMOTIONS = {
    BOAT: {
        "entity_type": "vessel",
        "summary": f"A sailing boat that {PERSON} keeps at {HARBOUR}.",
    }
}
VESSEL_TYPE = {
    "vessel": {
        "label": "Vessel",
        "guidance": "A boat or ship that a person or organisation keeps.",
        "parent": "concept",
        "attributes": {"folder": "Vessels"},
    }
}
PERSON_SUMMARY = "A synthetic sailor who keeps a logbook of her seasons."


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    for name in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(name, raising=False)
    due_state.reset_emission_state()
    yield
    due_state.reset_emission_state()


def _remember(call, title: str, body: str) -> dict:
    """Commit one note through the validate-then-commit door, as a client does."""
    arguments = {"title": title, "note_type": "insight", "content": body}
    draft = call("remember", {**arguments, "validate_only": True})
    return _committed(call, "remember", {
        **arguments, "draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"],
        "draft_token": draft["draft_token"],
    }, draft["draft_hash"])


def _author_edge(call, page: str, after: str, relation_line: str) -> dict:
    """Add one relation line under a new `## Relations` section that follows the text `after`."""
    arguments = {"path": page, "why": "Record the relation the turn stated."}
    operation = {
        "kind": "replace_string", "old_string": after,
        "new_string": f"{after}\n\n## Relations\n\n{relation_line}",
    }
    semantic = call("edit_note", {**arguments, "operation": {**operation, "validate_only": True}})
    operation["transition_token"] = semantic["semantic"]["transition_token"]
    edited = call("edit_note", {**arguments, "operation": operation})
    assert edited.get("status") == "committed", edited
    return edited


def _publish(vault: Path) -> None:
    """The publication steps after a write: graph sync, lexical index, activation index."""
    graph_sync.drain_active_rebuilds(timeout=60)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def _relations_naming(context: dict, name: str) -> list[str]:
    """The relation types of the context's edges that have the named page at one end."""
    return sorted(
        edge["relation_type"] for edge in context["graph"]["edges"]
        if name.lower() in f"{edge['src_key']} {edge['dst_key']}".lower()
    )


def _case_table(rows: list[dict]) -> str:
    columns = list(rows[0])
    return "\n".join([" | ".join(columns)] + [" | ".join(str(row[c]) for c in columns) for row in rows])


# One flow drives the whole publication pipeline: about 25 s on an idle machine, about 40 s under
# CI load, so it carries more than the default 60 s thread timeout's margin.
@pytest.mark.timeout(120)
def test_a_rich_turn_promotes_the_boat_and_leaves_the_incidental_and_one_off_names_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served_with_graph(tmp_path, monkeypatch)
    # The product default backend: the packet's lexical evidence is unsupported under the kill switch.
    monkeypatch.delenv("EXOMEM_LEXICAL_BACKEND", raising=False)

    # The corpus before the turn: a person, the harbour as an entity, and earlier notes about both.
    _as(monkeypatch, "seed")
    person = call("change_connections", {"operation": {
        "kind": "create-entity", "entity_type": "person", "name": PERSON, "summary": PERSON_SUMMARY,
    }})
    harbour = call("change_connections", {"operation": {
        "kind": "create-entity", "entity_type": "concept", "name": HARBOUR,
        "summary": "A synthetic harbour on the east coast.",
    }})
    _remember(call, "Winter berth", (
        f"## Observations\n\n- [finding] {PERSON} asked the harbour office at [[{HARBOUR}]] "
        "about a winter berth.\n"
    ))
    _remember(call, "Logbook habit", (
        f"## Observations\n\n- [finding] [[{PERSON}]] writes up every outing within a day.\n"
    ))

    # The rich turn, with a wikilink for every name it mentions.
    _as(monkeypatch, "agent")
    turn = _remember(call, "Boat season", (
        "## Observations\n\n"
        f"- [finding] [[{PERSON}]] spent the weekend readying her sailing boat [[{BOAT}]] for the "
        "autumn season, replacing the forestay and re-bedding two deck fittings.\n"
        f"- [finding] [[{BOAT}]] is kept at [[{HARBOUR}]], where the winter berth is booked "
        "through March.\n"
        f"- [finding] A passer-by, [[{INCIDENTAL_TWIN}]], asked the way to the chandlery and was "
        "not seen again.\n"
        f"- [finding] The harbour held an [[{ONE_OFF_TWIN}]] on the day of the refit and has not "
        "repeated it.\n"
    ))
    listed = {item["name"]: item for item in turn["undeclared_referents"]["names"]}
    assert set(listed) == {BOAT, INCIDENTAL_TWIN, ONE_OFF_TWIN}, (
        "the block lists the three unpaged names and neither existing entity", turn
    )

    # The script follows the block: one saved type, then each promoted name through its own route.
    inspected = call("read_schema", {"operation": {"kind": "inspect", "subject": "entity-types"}})
    saved = call("change_schema", {"operation": {
        "kind": "save", "subject": "entity-types", "proposal": {"upsert": VESSEL_TYPE},
        "expected_hash": inspected["content_hash"],
        "why": "Boats recur in this vault and need their own identity.",
    }})
    assert saved.get("status") == "committed", saved
    created = {}
    for name, plan in PROMOTIONS.items():
        route = listed[name]["route"]
        created[name] = call(route["tool"], {"operation": {
            **route["args"]["operation"], **plan,
            "relations": [{"kind": "located_at", "target": harbour["path"].removesuffix(".md")}],
        }})
        assert created[name].get("status") == "committed", created[name]
    boat_path = created[BOAT]["path"]
    _author_edge(call, person["path"], PERSON_SUMMARY, f"- owns [[{boat_path.removesuffix('.md')}]]")

    _publish(vault)

    # A fresh session picks the work up.
    _as(monkeypatch, "fresh")
    due_state.reset_emission_state()
    packet = call("activate_context", {
        "turn": f"I want to take {BOAT} out this weekend. What should I remember?",
    })
    resolved = [
        item for item in packet["turn_slice"]["items"]
        if item.get("match") == "resolved" and item.get("path") == boat_path
    ]
    boat_context = call("read_connections", {"operation": {
        "kind": "context", "path": boat_path.removesuffix(".md"),
    }})
    turn_context = call("read_connections", {"operation": {
        "kind": "context", "path": turn["path"].removesuffix(".md"),
    }})
    notice = call("bootstrap", {"section": "epistemics"})["due_state"]
    promotions = review_state.ReviewStateStore(vault).load()["promotions"].values()
    agent_rows = [row for row in promotions if row["creator"] == "session:agent"]

    entity_types = {
        node["title"]: node["metadata"].get("entity_type") for node in boat_context["graph"]["nodes"]
    }
    entity_pages = {path.stem for path in (vault / "Knowledge Base" / "Entities").rglob("*.md")}
    table = _case_table([
        {
            "case": case, "listed": name in listed,
            "decision": "promote" if name in PROMOTIONS else "decline",
            "entity page": name in entity_pages,
            "typed edges": [r for r in _relations_naming(context, name) if r != "links_to"],
            "promotion rows": sum(name in json.dumps(row["subjects"]) for row in agent_rows),
        }
        for case, name, context in (
            ("boat", BOAT, boat_context),
            ("incidental twin", INCIDENTAL_TWIN, turn_context),
            ("one-off twin", ONE_OFF_TWIN, turn_context),
        )
    ])

    assert entity_types[BOAT] == "vessel", f"the boat is an entity of the saved type\n{table}"
    assert any(
        edge["relation_type"] == "owns" and PERSON in edge["src_key"] and BOAT in edge["dst_key"]
        for edge in boat_context["graph"]["edges"]
    ), f"the person-to-boat ownership edge is in the graph\n{table}"
    assert any(
        edge["relation_type"] == "located_at" and BOAT in edge["src_key"] and HARBOUR in edge["dst_key"]
        for edge in boat_context["graph"]["edges"]
    ), f"the location edge is written even though located_at has no core endpoints\n{table}"
    assert len(resolved) == 1 and resolved[0]["anchor_kind"] == "entity", (
        f"the fresh packet resolves the boat\n{table}", packet["turn_slice"]
    )
    assert entity_types[PERSON] == "person", (
        f"the boat's context serves its owner through the ownership edge\n{table}"
    )

    for twin in (INCIDENTAL_TWIN, ONE_OFF_TWIN):
        assert twin not in entity_pages, f"{twin} got an entity page\n{table}"
        assert _relations_naming(turn_context, twin) == [], f"{twin} got an edge\n{table}"
        assert twin not in json.dumps(agent_rows), f"{twin} reached the promotion notice\n{table}"
    assert Counter(row["kind"] for row in agent_rows) == {"registry": 1, "entity": 1, "edge": 2}, (
        f"the ledger holds the saved type, the boat and its two edges, nothing else\n{table}"
    )
    assert notice["categories"] == {"recent_promotions": 6}, (
        f"the fresh session is told of every promotion in the corpus once\n{table}", notice
    )
