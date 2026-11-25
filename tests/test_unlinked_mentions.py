"""The write-time `unlinked_mentions` block, read from the MCP response a hookless client sees.

Every case drives the in-process MCP server over an initialized vault, so the block is read after the
terminal projection and egress, and the ready call is sent with the arguments the block names.
Fixtures are invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_recent_promotions import _as
from test_undeclared_referents import (
    NO_RELATION,
    _committed,
    _listed,
    _restricted_writer,
    _same_bytes_but_for_ids,
    _switch_structural_suggestions_off,
    _switch_to_light_prominence,
    _withhold_from_external,
)
from test_vocabulary_registries import _served

from exomem import capture_sweep, due_state, episode_workflow, name_mentions
from exomem.governance import principal as principal_module

FAMILY = "unlinked_mentions"
MARIN = "Knowledge Base/Entities/Concepts/Marin Holt"
HARBOUR = "Knowledge Base/Entities/Organizations/Harbour Supply Co"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    for name in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(name, raising=False)
    due_state.reset_emission_state()
    yield
    due_state.reset_emission_state()


def _entity(call, entity_type: str, name: str, **extra) -> dict:
    """Create one Entity; a same-alias twin is created with the `distinct` identity decision the write asks for."""
    arguments = {
        "operation": "create-entity", "entity_type": entity_type, "name": name,
        "summary": extra.pop("summary", f"A synthetic entity named {name}."), **extra,
    }
    made = call("connect_memory", arguments)
    if made.get("error", {}).get("code") == "ENTITY_EXISTS":
        fingerprint = made["error"]["message"].split("candidate_fingerprint: ")[1].split(")")[0]
        made = call("connect_memory", {
            **arguments, "identity_decision": {"outcome": "distinct", "candidate_fingerprint": fingerprint},
        })
    assert "error" not in made, made
    return made


def _prose(title: str, prose: str) -> dict:
    return {"title": title, "note_type": "insight", "content": f"## Observations\n\n- [finding] {prose}\n"}


def _write(call, arguments: dict) -> dict:
    """Commit one note through the validate-then-commit door."""
    draft = call("remember", {**arguments, "validate_only": True})
    return _committed(call, "remember", {
        **arguments, "draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"],
        "draft_token": draft["draft_token"],
    }, draft["draft_hash"])


def _note(call, title: str, prose: str) -> dict:
    return _write(call, _prose(title, prose))


def _replace_text(call, note: str, old: str, new: str) -> dict:
    """Edit one note with `replace_text`, repeating with the review the barrier asks for."""
    arguments = {"note": note, "why": "Change the note.", "action": "replace_text", "old_text": old, "new_text": new}
    edited = call("edit_note", arguments)
    if "retry" in edited.get("error", {}):
        edited = call("edit_note", {**arguments, "unlinked_reason": NO_RELATION})
    return edited


def _offers(response: dict) -> list[tuple[str, str]]:
    """`(name, link target)` of every Entity the response offers, in order."""
    pages = response.get("unlinked_mentions", {}).get("pages", [])
    return [(item["name"], item["link"]) for page in pages for item in page["mentions"]]


def _ready(response: dict, page: int = 0) -> dict:
    return response["unlinked_mentions"]["pages"][page]["call"]


def _send(call, ready: dict) -> dict:
    return call(ready["tool"], ready["args"])


def _read(vault: Path, note: str) -> str:
    return (vault / note).read_text(encoding="utf-8")


def test_a_note_that_names_entities_in_prose_is_offered_their_links_and_the_ready_call_links_exactly_those(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    _entity(call, "organization", "Harbour Supply Co")
    _entity(call, "concept", "Kestrel (sailing boat)", aliases=["the Kestrel"])
    _entity(call, "concept", "Dana Reyes", aliases=["Dana"])
    _entity(call, "organization", "Dana Freight", aliases=["Dana"])
    prose = "Marin Holt took the Kestrel to Harbour Supply Co, where Dana signed."

    written = _note(call, "Harbour review", prose)

    assert _offers(written) == [
        ("Harbour Supply Co", HARBOUR),
        ("the Kestrel", "Knowledge Base/Entities/Concepts/Kestrel (sailing boat)"),
        ("Marin Holt", MARIN),
    ], "Dana names two Entities and is not offered; the longest names come first"
    note = written["unlinked_mentions"]["pages"][0]["note"]
    assert prose in _read(vault, note), "the note is committed exactly as written"

    ready = _ready(written)
    assert ready["tool"] == "edit_note" and ready["args"]["action"] == "replace_text"
    sent = _send(call, ready)

    assert "error" not in sent, sent
    linked = prose
    for name, target in _offers(written):
        linked = linked.replace(name, f"[[{target}|{name}]]", 1)
    assert linked in _read(vault, note), "each listed name is a link and nothing else changed"
    assert "unlinked_mentions" not in sent, "every Entity the page names is linked now"


def test_a_repeated_name_gets_a_unique_anchor_and_a_page_edited_in_between_refuses_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    _entity(call, "organization", "Harbour Supply Co")
    written = _note(
        call, "Harbour review",
        "Marin Holt asked Harbour Supply Co to ship. Later Harbour Supply Co shipped, and Marin Holt paid.",
    )
    note = written["unlinked_mentions"]["pages"][0]["note"]
    before_call = _read(vault, note)
    edits = _ready(written)["args"]["edits"]
    assert len(edits) == 2
    for edit in edits:
        assert before_call.count(edit["old_text"]) == 1, "each anchor names one place"

    # Remove the text one anchor stands on: the call must refuse as a whole.
    _replace_text(call, note, "Marin Holt asked", "Someone asked")
    drifted = _read(vault, note)
    refused = _send(call, _ready(written))

    assert "error" in refused, refused
    assert _read(vault, note) == drifted, "a refused call changes nothing, not even its first edit"

    # The edit that adds a mention is offered the page again, and its anchor is unique on the drifted page.
    fresh = _replace_text(call, note, "ship. Later", "ship. Also Marin Holt came. Later")
    assert _offers(fresh) == [("Marin Holt", MARIN)], "the Entity whose mentions did not grow is not offered again"
    sent = _send(call, _ready(fresh))
    assert "error" not in sent, sent
    linked = _read(vault, note)
    assert linked.count(f"[[{MARIN}|Marin Holt]]") == 1
    assert linked.index(f"[[{MARIN}|") < linked.index("Later Harbour"), "the first mention is the one linked"


def _quiet_the_family(call) -> None:
    quieted = call("triage_memory", {
        "ref": f"exomem://review/family/{FAMILY}", "action": "quiet", "why": "too_frequent: links are chosen by hand.",
    })
    assert "error" not in quieted, quieted


@pytest.mark.parametrize(
    "silence",
    [_switch_structural_suggestions_off, _switch_to_light_prominence, _quiet_the_family],
    ids=["structural_suggestions-off", "light-prominence", "family-quiet"],
)
def test_the_block_is_absent_where_the_owner_has_asked_for_less(
    silence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    assert _offers(_note(call, "Harbour review", "Marin Holt arrived.")) == [("Marin Holt", MARIN)]

    silence(call)

    assert "unlinked_mentions" not in _note(call, "Pier review", "Marin Holt left.")


def test_quieting_this_family_leaves_the_sibling_family_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    _quiet_the_family(call)

    written = _note(call, "Harbour review", "Marin Holt met [[Kestrel]].")

    assert "unlinked_mentions" not in written
    assert _listed(written) == ["Kestrel"]


def test_a_dismissed_entity_is_not_offered_again_for_that_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    first = _note(call, "Harbour review", "Marin Holt arrived.")
    note = first["unlinked_mentions"]["pages"][0]["note"]
    handle = first["unlinked_mentions"]["pages"][0]["mentions"][0]
    dismissed = call("decide_review_item", {
        "review_item": handle["review"], "decision": "dismiss", "why": "intentional: the name stays plain here.",
    })
    assert dismissed.get("state") == "dismissed", dismissed

    edited = _replace_text(call, note, "arrived.", "arrived. Marin Holt left.")

    assert "unlinked_mentions" not in edited
    other = _note(call, "Pier review", "Marin Holt waited.")
    assert _offers(other) == [("Marin Holt", MARIN)], "a dismissal names one Entity on one page"


def test_only_a_new_unlinked_mention_of_an_unlinked_entity_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    _entity(
        call, "organization", "Harbour Supply Co", summary="Harbour Supply Co supplies the harbour.",
    )
    own_page = f"{HARBOUR}.md"
    own = _replace_text(call, own_page, "supplies the harbour.", "supplies the harbour. Harbour Supply Co is old.")
    assert "error" not in own, own
    assert "Harbour Supply Co is old." in _read(vault, own_page)
    assert "unlinked_mentions" not in own, "an Entity's own page is never offered its own name"

    linked = _note(call, "Linked review", f"[[{MARIN}|Marin]] spoke, and later Marin Holt agreed.")
    assert "unlinked_mentions" not in linked, "an Entity the page already links anywhere is not offered"

    old = _note(call, "Old review", "Marin Holt arrived.")
    note = old["unlinked_mentions"]["pages"][0]["note"]
    untouched = _replace_text(call, note, "arrived.", "arrived early.")
    assert "unlinked_mentions" not in untouched, "an edit that adds no mention repeats no offer"
    grown = _replace_text(call, note, "arrived early.", "arrived early. Marin Holt left.")
    assert _offers(grown) == [("Marin Holt", MARIN)], "an edit that adds a mention is offered again"


def test_a_name_in_code_a_link_a_url_or_a_heading_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    content = (
        "## Marin Holt\n\n- [finding] The `Marin Holt` token, [Marin Holt](notes/other.md), "
        "https://example.org/Marin_Holt and ![Marin Holt](figure.png) are not prose.\n"
    )

    written = _write(call, {"title": "Masked review", "note_type": "insight", "content": content})

    assert "unlinked_mentions" not in written


def test_a_fault_in_the_matcher_costs_the_offer_and_never_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")

    def fault(*_args, **_kwargs):
        raise RuntimeError("the matcher failed")

    monkeypatch.setattr(name_mentions, "find_mentions", fault)
    written = _note(call, "Faulted review", "Marin Holt arrived.")

    assert "unlinked_mentions" not in written
    assert any("Marin Holt arrived." in path.read_text(encoding="utf-8") for path in vault.rglob("*.md")), (
        "the note is committed"
    )


def test_a_batch_lists_an_entity_once_for_each_page_it_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `episode_memory` resume commits two notes that name one Entity; the batch carries one block."""
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _entity(call, "concept", "Marin Holt")
    key = "ep-" + "c0" * 16

    def episode(**arguments) -> dict:
        return call("episode_memory", {"episode": key, **arguments})

    def prepare(candidate: str, slug: str, prose: str) -> str:
        leaf = {"leaf_key": "write", "effect_revision": 1, "kind": "create-note", "args": {
            "title": slug.replace("-", " ").title(), "slug": slug,
            "content": f"## Observations\n\n- [finding] {prose}\n",
            "relation_disposition": "reviewed_none", "relation_review_reason": NO_RELATION,
        }}
        prepared = episode(action="prepare", candidate=candidate, proposal={
            "route": "focused_note", "alternatives": [], "evidence": "complete",
            "reason": "The agent chose this home.", "leaves": [leaf], "title": slug,
        })
        row = next(row for row in prepared["candidates"] if row["candidate_key"] == candidate)
        episode(action="disposition", candidate=candidate, disposition="routed", reason="Routed for the test.")
        return row["leaves"][0]["leaf_id"]

    episode(
        action="record", subject="Harbour review", summary="Reviewed two harbour notes.",
        worked_on=["Reviewed the harbour"], decided=["The numbers hold", "The pier numbers hold"], open=[],
    )
    first = prepare("first", "first-episode-note", "Marin Holt approved the berth. Marin Holt also paid.")
    second = prepare("second", "second-episode-note", "Marin Holt inspected the pier.")
    state = episode(action="candidates")

    resumed = episode(
        action="resume", input_revision=state["input_revision"], journal_digest=state["journal_digest"],
        order=[first, second],
    )

    pages = resumed["unlinked_mentions"]["pages"]
    assert [page["note"].rsplit("/", 1)[-1] for page in pages] == ["first-episode-note.md", "second-episode-note.md"]
    for page in pages:
        assert [item["name"] for item in page["mentions"]] == ["Marin Holt"], "once per page, however often it is named"
        assert page["call"]["args"]["note"] == page["note"]
    sent = _send(call, pages[0]["call"])
    assert "error" not in sent, sent
    assert _read(vault, pages[0]["note"]).count(f"[[{MARIN}|Marin Holt]]") == 1


def test_a_restricted_writer_gets_the_same_receipt_whether_a_same_name_entity_is_withheld_or_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The names an Entity the writer may not see answers to are never offered, and never make a name ambiguous."""
    seen: dict[str, dict] = {}
    for variant, withheld in (("withheld", True), ("absent", False)):
        vault, call = _served(tmp_path / variant, monkeypatch)
        _as(monkeypatch, "owner-session")
        _entity(call, "organization", "Marin", summary="A visible organization.")
        _entity(call, "organization", "Harbour Supply Co", summary="A visible organization.")
        if withheld:
            _entity(call, "concept", "Kestrel", aliases=["Marin"], summary="An entity the writer may not see.")
        _withhold_from_external(vault, "Entities/Concepts/**")
        with monkeypatch.context() as restricted:
            _as(restricted, "restricted-session")
            capture_sweep.reset_state()
            writer = _restricted_writer(vault, tmp_path / variant, restricted, inbox="Notes/Inbox/restricted")
            restricted.setattr(principal_module, "resolve_mcp_principal", lambda bound=writer: bound)
            seen[variant] = _note(call, "Harbour review", "Marin met Kestrel at Harbour Supply Co.")

    assert [name for name, _link in _offers(seen["absent"])] == ["Harbour Supply Co", "Marin"]
    assert _same_bytes_but_for_ids(seen["withheld"]) == _same_bytes_but_for_ids(seen["absent"])
