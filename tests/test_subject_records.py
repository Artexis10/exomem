"""`recall_context` serves a named person's ledger from the Records collection that declares `subject`.

The vault is the recall tests' shared structure plus two invented people, a page on how the tab is kept, and
one ledger collection whose rows cite those people. A turn that names a person and touches the ledger's claims
gets the person's sums by state, the rows whose amount or date is unknown, and the newest rows.
"""

import contextlib
import time

import pytest
from test_activate_context_maps import _activate
from test_collection_link_values import _ready
from test_collection_row_references import path, revise
from test_collection_store_writer import store as store  # noqa: F401
from test_context_catalogue import _built
from test_governance_egress import _external, write_rule, write_scope
from test_page_citations import publish
from test_working_set_index import _seed_structure

from exomem import (
    lexstore,
    page_citations,
    record_governance,
    request_budget,
    subject_records,
    working_set,
)
from exomem import structured_collections as collections
from exomem.collection_store import connection, typed_storage
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

LEDGER = "a1a1a1a1-0000-4000-8000-00000000a1a1"
MARA = "b2b2b2b2-0000-4000-8000-00000000b2b2"
IVEL = "c3c3c3c3-0000-4000-8000-00000000c3c3"
PELL = "d4d4d4d4-0000-4000-8000-00000000d4d4"
METHOD = "e5e5e5e5-0000-4000-8000-00000000e5e5"
QUIN = "f6f6f6f6-0000-4000-8000-00000000f6f6"
# Two ids for a second collection: one sorts before the ledger's, one after.
VISITS = "09090909-0000-4000-8000-000000000909"
LATE_VISITS = "f9f9f9f9-0000-4000-8000-00000000f9f9"
MARA_REF, IVEL_REF, PELL_REF = f"exomem://memory/{MARA}", f"exomem://memory/{IVEL}", f"exomem://memory/{PELL}"
TURN = "what does Mara owe me"
AMOUNT_MEANS = "Positive: they owe me. Negative: I owe them."
FIELDS = ("    label: {type: string, required: true}\n"
          "    person: {type: link}\n"
          f"    amount: {{type: integer, description: \"{AMOUNT_MEANS}\"}}\n"
          "    status: {type: enum, enum: [settled, provisional]}\n"
          "    day: {type: date}\n"
          "    note: {type: string}\n")
SUBJECT = "subject: {field: person, measure: amount, state: status, order: day, show: [note]}\n"
# Mara's rows: one provisional, one with no amount, one with no date. Ivel's row is never Mara's.
ROWS = (("r1", MARA_REF, 40, "settled", "2026-11-01", "lunch"),
        ("r2", MARA_REF, 25, "provisional", "2026-11-20", "train tickets"),
        ("r3", MARA_REF, None, "settled", "2026-11-10", "amount to confirm"),
        ("r4", MARA_REF, 15, "settled", None, "undated"),
        ("r5", IVEL_REF, 100, "settled", "2026-11-15", "rent share"))


def key(label):
    return f"00000000-0000-4000-8000-0000000000{label[1:].zfill(2)}"


def person(root, name, identity):
    target = root / "Knowledge Base" / "Entities" / "People" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ntype: entity\nentity_type: person\nexomem_id: {identity}\nstatus: active\n"
                      f"aliases: [{name.split()[0]}]\n---\n\n# {name}\n\n## Summary\n\nA friend.\n")


def ledger_text(*, layout="sqlite", fields=FIELDS, subject=SUBJECT):
    """The ledger manifest: a sqlite store collection keeps typed rows, a markdown-items one keeps JSON rows."""
    storage = ("view_mode: summary\nstorage:\n  strategy: sqlite\n  source: Items\n  format_version: 1\n"
               if layout == "sqlite" else "storage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n")
    return (f"---\ntype: collection\nexomem_id: {LEDGER}\ntitle: Tab\nsemantic_profile: records\n"
            "collection_version: 1\nschema_version: 1\nlifecycle: active\n"
            "claims:\n  terms: [owe, owes, lent, borrowed]\n"
            f"capture: {{method: \"exomem://memory/{METHOD}\"}}\n{subject}{storage}"
            f"item_schema:\n  natural_key: [label]\n  fields:\n{fields}---\n")


def add(store, label, person_ref, amount, status, day, note, who=None):
    """One row; an amount of None is stored as null, and a day of None is left out."""
    item = {"label": label, "person": person_ref, "amount": amount, "status": status, "note": note}
    if day is not None:
        item["day"] = day
    with request_scope(who or owner_principal()):
        return store.append_record(LEDGER, item=item, item_key=key(label), why="record")


def build(store, monkeypatch, layout="sqlite", rows=ROWS, subject=SUBJECT, ready=True):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _seed_structure(store.root)
    person(store.root, "Mara Quillon", MARA)
    person(store.root, "Ivel Brask", IVEL)
    person(store.root, "Pell Arden", PELL)
    person(store.root, "Quin Aldous", QUIN)
    method = store.root / "Knowledge Base" / "Notes" / "Keeping the tab.md"
    method.parent.mkdir(parents=True, exist_ok=True)
    method.write_text(f"---\nexomem_id: {METHOD}\ntitle: Keeping the tab\n---\n# Keeping the tab\n\nOne row per IOU.\n")
    _built(store.root).close()
    lexstore.ensure_fresh(store.root)
    store.create_collection(path("Tab"), ledger_text(layout=layout, subject=subject), why="declare", scaffold=False)
    for row in rows:
        add(store, *row)
    if ready:
        _ready(store, LEDGER)
    publish(store.root)
    return store


@pytest.fixture
def world(store, monkeypatch, corpus_cache_on):
    return build(store, monkeypatch)


def blocks(packet):
    return packet.get("subject_records", {}).get("entries", [])


def lead(packet, ref):
    [found] = [item for item in packet["turn_slice"]["items"] if item.get("ref") == ref]
    return found


def hide_rows(root, *labels):
    """Withhold the named rows from the external audience by listing their refs in a ceiling-0 scope."""
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    refs = ", ".join(f'"{collections.record_ref(LEDGER, key(label))}"' for label in labels)
    scope.write_text(scope.read_text() + f"refs: [{refs}]\n")
    write_rule(root, ceiling=0)


@pytest.mark.parametrize("layout", ["sqlite", "markdown-items"], ids=["typed rows", "json rows"])
def test_a_turn_naming_a_person_and_the_ledger_gets_their_sums_unknowns_and_newest_rows(store, monkeypatch,
                                                                                         corpus_cache_on, layout):
    build(store, monkeypatch, layout)
    packet = _activate(store.root, turn=TURN)
    [entry] = blocks(packet)
    assert entry["anchor"]["ref"] == MARA_REF and entry["anchor"]["title"] == "Mara Quillon"
    assert entry["collection"] == {"ref": f"exomem://memory/{LEDGER}", "title": "Tab"}
    assert entry["method"] == {"title": "Keeping the tab", "read_ref": f"exomem://memory/{METHOD}"}
    assert entry["measure"] == {"field": "amount", "description": AMOUNT_MEANS}
    assert entry["state"] == "ready"
    assert list(entry)[0] == "summary"
    assert entry["summary"] == ("Mara Quillon in Tab: amount sums to 80 over 4 rows (provisional 25 over 1 row, "
                                "settled 55 over 3 rows); 1 row has no amount; 1 row has no day; newest day 2026-11-20.")
    # The row with no amount counts in its state's rows and under unknown, and adds nothing to any sum.
    assert sorted(entry["by_state"], key=lambda group: str(group["state"])) == [
        {"state": "provisional", "sum": 25, "rows": 1}, {"state": "settled", "sum": 55, "rows": 3}]
    assert entry["total"] == {"sum": 80, "rows": 4}
    assert entry["unknown"] == {"measure": 1, "order": 1}
    # Newest first by date; the undated row sorts after every dated one.
    assert entry["newest"] == [
        {"ref": collections.record_ref(LEDGER, key("r2")), "values": {"day": "2026-11-20", "amount": 25, "note": "train tickets"}},
        {"ref": collections.record_ref(LEDGER, key("r3")), "values": {"day": "2026-11-10", "amount": None, "note": "amount to confirm"}},
        {"ref": collections.record_ref(LEDGER, key("r1")), "values": {"day": "2026-11-01", "amount": 40, "note": "lunch"}},
    ]
    assert packet["subject_records"]["omitted"] == 0 and "state" not in packet["subject_records"]
    # The entry stands for the person's citations from the ledger, so the lead keeps its count and not a second list.
    assert lead(packet, MARA_REF)["cited_by_count"] == 4 and "cited_by" not in lead(packet, MARA_REF)
    assert "subject_records" not in packet.get("task_context", {})
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def add_visits(store, identity=VISITS, subject="", typed=False):
    """A second Records collection whose one row cites Mara through a link field; it declares no `subject` by default."""
    storage = ("view_mode: summary\nstorage:\n  strategy: sqlite\n" if typed
               else "storage:\n  strategy: markdown-items\n")
    visits = (f"---\ntype: collection\nexomem_id: {identity}\ntitle: Visits\nsemantic_profile: records\n"
              "collection_version: 1\nschema_version: 1\nlifecycle: active\n"
              f"claims:\n  terms: [owe, visit]\n{subject}"
              f"{storage}  source: Items\n  format_version: 1\n"
              "item_schema:\n  natural_key: [label]\n  fields:\n    label: {type: string, required: true}\n"
              "    person: {type: link}\n    cost: {type: integer}\n---\n")
    store.create_collection(path("Visits"), visits, why="declare", scaffold=False)
    with request_scope(owner_principal()):
        store.append_record(identity, item={"label": "v1", "person": MARA_REF, "cost": 5}, item_key=key("v1"),
                            why="record")
    _ready(store, identity)


# The citation read walks the index in collection id order, so the ledger's rows sorting first could crowd out the rest.
@pytest.mark.parametrize("other", [VISITS, LATE_VISITS], ids=["other collection sorts first", "ledger sorts first"])
def test_a_ledger_entry_keeps_the_leads_citations_from_other_collections(world, other):
    """The entry stands for the ledger's own citations of the person; another collection's stay on the lead."""
    add_visits(world, other)
    packet = _activate(world.root, turn=TURN)
    assert [entry["state"] for entry in blocks(packet)] == ["ready"]
    assert lead(packet, MARA_REF)["cited_by_count"] == 5
    assert [item["ref"] for item in lead(packet, MARA_REF)["cited_by"]] == [collections.record_ref(other, key("v1"))]


def test_a_ledger_entry_without_newest_rows_leaves_the_leads_citations_whole(store, monkeypatch, corpus_cache_on):
    build(store, monkeypatch, subject=SUBJECT.replace("}", ", recent: 0}"))
    packet = _activate(store.root, turn=TURN)
    [entry] = blocks(packet)
    assert entry["state"] == "ready" and "newest" not in entry
    assert {item["ref"] for item in lead(packet, MARA_REF)["cited_by"]} <= {
        collections.record_ref(LEDGER, key(label)) for label in ("r1", "r2", "r3", "r4")}
    assert len(lead(packet, MARA_REF)["cited_by"]) == 3


def test_naming_a_person_without_touching_the_ledger_serves_no_sums(world):
    packet = _activate(world.root, turn="how is Mara doing these days")
    assert "subject_records" not in packet
    assert "cited_by" in lead(packet, MARA_REF)


def test_touching_the_ledger_without_naming_a_person_its_rows_cite_serves_no_sums(world):
    assert "subject_records" not in _activate(world.root, turn="what does Pell owe me")


def test_a_lead_the_turn_does_not_name_gets_no_sums(world):
    """A ledger for a lead the turn only resembles would put the wrong person's numbers in the answer."""
    with request_scope(owner_principal()):
        packet = {"budget": {"limit_chars": 10_000, "used_chars": 0}}
        served = subject_records.for_packet(world.root, packet, TURN, [
            {"ref": MARA_REF, "title": "Mara Quillon", "evidence": ["lexical_overlap", "retrieval"]}])
    assert served == {} and "subject_records" not in packet


def test_a_request_whose_task_context_ran_out_of_time_still_serves_the_sums(world, monkeypatch):
    """The 27 Nov 2026 failure: discovery spent the activation's time, and the tally went with it."""
    original = working_set.task_candidates

    def expiring(*args, **kwargs):
        # The turn slice has resolved its anchors; task discovery then spends the activation's whole budget.
        request_budget.current().deadline = time.monotonic()
        return original(*args, **kwargs)

    monkeypatch.setattr(working_set, "task_candidates", expiring)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=60))
    try:
        packet = _activate(world.root, turn=TURN)
    finally:
        request_budget.reset_current(token)
    assert packet["task_context"]["state"] == "partial"
    [entry] = blocks(packet)
    assert entry["state"] == "ready" and entry["total"] == {"sum": 80, "rows": 4}


def test_the_sums_read_the_store_twice_however_many_rows_cite_the_person(world, monkeypatch):
    statements = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)

    def row_reads():
        statements.clear()
        [entry] = blocks(_activate(world.root, turn=TURN))
        return entry["total"]["rows"], sum("item_references r CROSS JOIN" in sql for sql in statements)

    assert row_reads() == (4, 2)
    for number in range(6, 46):
        add(world, f"r{number}", MARA_REF, number, "settled", "2026-10-01", "older")
    _ready(world, LEDGER)
    assert row_reads() == (44, 2)


def test_a_collection_that_declares_no_subject_costs_the_block_nothing(world, monkeypatch):
    """Every turn that names a page reads this block, so a collection that is no ledger must add no statement to it."""
    statements = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)

    def statements_per_call():
        statements.clear()
        with request_scope(owner_principal()):
            block = subject_records.summaries(world.root, [(MARA_REF, "Mara Quillon")], TURN)
        assert [entry["state"] for entry in block.entries] == ["ready"]
        return len(statements)

    # The second call of each pair is the steady cost: the first may read a manifest it has not seen.
    statements_per_call()
    before = statements_per_call()
    add_visits(world)
    statements_per_call()
    assert statements_per_call() == before


def test_a_turn_naming_four_people_the_ledger_cites_gets_the_first_three(world):
    for label, who in (("r6", PELL), ("r7", QUIN)):
        add(world, label, f"exomem://memory/{who}", 5, "settled", "2026-11-02", "coffee")
    _ready(world, LEDGER)
    anchors = [(MARA_REF, "Mara Quillon"), (IVEL_REF, "Ivel Brask"), (f"exomem://memory/{PELL}", "Pell Arden"),
               (f"exomem://memory/{QUIN}", "Quin Aldous")]
    with request_scope(owner_principal()):
        entries = subject_records.summaries(world.root, anchors, TURN).entries
    assert [entry["anchor"]["ref"] for entry in entries] == [ref for ref, _ in anchors]
    assert [entry["state"] for entry in entries] == ["ready", "ready", "ready", "omitted"]
    # The fourth person is named as left out, with what to ask, and carries no number.
    assert entries[3]["reason"] and set(entries[3]) == {"reason", "anchor", "collection", "state"}


def test_a_caller_who_may_not_read_the_amount_gets_no_sums_and_is_told_why(store, monkeypatch, corpus_cache_on):
    world = build(store, monkeypatch)
    hidden = ledger_text(fields=FIELDS.replace("type: integer,", "type: integer, classification: location,"))
    with request_scope(owner_principal()):
        revise(world, LEDGER, hidden)
    [entry] = blocks(_activate(world.root, _external(), turn=TURN))
    assert entry["state"] == "unavailable" and list(entry)[0] == "reason" and entry["reason"]
    assert not {"by_state", "total", "unknown", "newest", "measure"} & set(entry)


def test_a_caller_who_may_read_neither_the_amount_nor_any_of_the_rows_is_told_nothing(store, monkeypatch,
                                                                                       corpus_cache_on):
    """An `unavailable` entry would say that rows the caller may not read cite the person."""
    world = build(store, monkeypatch)
    hidden = ledger_text(fields=FIELDS.replace("type: integer,", "type: integer, classification: location,"))
    with request_scope(owner_principal()):
        revise(world, LEDGER, hidden)
    hide_rows(world.root, "r1", "r2", "r3", "r4")
    packet = _activate(world.root, _external(), turn=TURN)
    assert "subject_records" not in packet
    assert LEDGER not in str(packet)


def test_a_row_the_caller_may_not_read_leaves_no_trace_in_the_sums(world):
    hide_rows(world.root, "r2")
    [entry] = blocks(_activate(world.root, _external(), turn=TURN))
    assert entry["state"] == "ready"
    assert entry["by_state"] == [{"state": "settled", "sum": 55, "rows": 3}]
    assert entry["total"] == {"sum": 55, "rows": 3}
    assert collections.record_ref(LEDGER, key("r2")) not in str(entry)


def test_an_index_still_building_says_warming_instead_of_a_zero_total(store, monkeypatch, corpus_cache_on):
    world = build(store, monkeypatch)
    world.handle.connection.execute("UPDATE item_reference_state SET ready=0 WHERE collection_id=?", (LEDGER,))
    packet = _activate(world.root, turn=TURN)
    [entry] = blocks(packet)
    assert entry["state"] == "warming" and entry["reason"]
    assert entry["collection"]["title"] == "Tab" and not {"total", "measure"} & set(entry)
    # The block says so too: a building index may not yet hold every pair it will hold.
    assert packet["subject_records"]["state"] == "warming" and list(packet["subject_records"])[0] == "reason"


def test_a_freshly_declared_ledger_says_warming_without_naming_anyone(store, monkeypatch, corpus_cache_on):
    """An index that holds no pair yet proves none, so without the block's own state the tally would vanish."""
    build(store, monkeypatch, ready=False)
    block = _activate(store.root, turn=TURN)["subject_records"]
    assert block["state"] == "warming" and list(block)[0] == "reason" and block["reason"]
    assert block["entries"] == [] and block["omitted"] == 0
    assert MARA_REF not in str(block) and LEDGER not in str(block)


def test_a_page_index_that_cannot_be_read_makes_the_block_unavailable_for_any_page(world, monkeypatch):
    """The block's state is decided before any page is looked at: a page rows cite and one none cites read alike."""
    monkeypatch.setattr(record_governance._LinkProjector, "index_available", lambda self: False)
    found = []
    for ref, title in ((MARA_REF, "Mara Quillon"), (PELL_REF, "Pell Arden")):
        packet = {"budget": {"limit_chars": 10_000, "used_chars": 0}}
        with request_scope(owner_principal()):
            subject_records.for_packet(world.root, packet, TURN, [
                {"ref": ref, "title": title, "evidence": ["exact_alias"]}])
        found.append(packet["subject_records"])
    assert found[0] == found[1]
    assert found[0]["state"] == "unavailable" and list(found[0])[0] == "reason" and found[0]["reason"]
    assert found[0]["entries"] == [] and found[0]["omitted"] == 0
    # A turn that no ledger applies to hears nothing of the failure.
    packet = {"budget": {"limit_chars": 10_000, "used_chars": 0}}
    with request_scope(owner_principal()):
        subject_records.for_packet(world.root, packet, "how is Mara doing these days", [
            {"ref": MARA_REF, "title": "Mara Quillon", "evidence": ["exact_alias"]}])
    assert "subject_records" not in packet


def test_a_deadline_spent_proving_a_pair_makes_the_block_unavailable(world, monkeypatch):
    """A pair the deadline leaves unproven would otherwise read as one nothing cites."""
    original = page_citations.citing_rows

    def spent(session, *args, **kwargs):
        # The read session's wall-clock bound passes as the first pair is proven.
        session._deadline._wall -= 60
        return original(session, *args, **kwargs)

    monkeypatch.setattr(page_citations, "citing_rows", spent)
    block = _activate(world.root, turn=TURN)["subject_records"]
    assert block["state"] == "unavailable" and list(block)[0] == "reason" and block["reason"]
    assert block["entries"] == [] and block["omitted"] == 0 and MARA_REF not in str(block)


def test_a_deadline_spent_admitting_a_ledger_makes_the_block_unavailable(world, monkeypatch):
    """Once one ledger applies, a second whose admission the deadline cuts short is not left out in silence."""
    add_visits(world, subject="subject: {field: person, measure: cost}\n")
    original = page_citations._readable_fields

    def spent(session, collection_id, *args, **kwargs):
        # Visits sorts first and applies; the read session's wall-clock bound passes as the ledger is admitted.
        if collection_id == LEDGER:
            session._deadline._wall -= 60
        return original(session, collection_id, *args, **kwargs)

    monkeypatch.setattr(page_citations, "_readable_fields", spent)
    with request_scope(owner_principal()):
        block = subject_records.summaries(world.root, [(MARA_REF, "Mara Quillon")], TURN)
    assert block.state["state"] == "unavailable" and list(block.state)[0] == "reason"
    assert block.entries == []


@pytest.mark.parametrize("broken", [VISITS, LATE_VISITS],
                         ids=["broken collection sorts first", "healthy ledger sorts first"])
def test_a_collection_that_cannot_be_admitted_leaves_the_other_ledgers_served(world, monkeypatch, broken):
    """A collection refused for its own reason, on every read until it recovers, costs the other ledgers nothing."""
    add_visits(world, broken, subject="subject: {field: person, measure: cost}\n", typed=True)
    original = typed_storage.require_layout

    def converting(conn, collection_id):
        # A layout conversion is under way, so no layout is published and every read of the collection is refused.
        if collection_id == broken:
            raise typed_storage.TypedStorageError("typed collection has no published layout")
        return original(conn, collection_id)

    monkeypatch.setattr(typed_storage, "require_layout", converting)
    with request_scope(owner_principal()):
        block = subject_records.summaries(world.root, [(MARA_REF, "Mara Quillon")], TURN)
    [entry] = block.entries
    assert entry["collection"]["ref"] == f"exomem://memory/{LEDGER}" and entry["state"] == "ready"
    assert entry["total"] == {"sum": 80, "rows": 4}
    assert block.state == {}


def test_a_sum_that_cannot_be_read_makes_its_own_entry_unavailable(store, monkeypatch, corpus_cache_on):
    """The pair is proven readable before its sums fail, so its entry says so and the block keeps no state of its own."""
    build(store, monkeypatch, rows=(*ROWS, ("r6", MARA_REF, 2**63 - 1, "settled", "2026-11-02", "past any sum")))
    with request_scope(owner_principal()):
        block = subject_records.summaries(store.root, [(MARA_REF, "Mara Quillon")], TURN)
    [entry] = block.entries
    assert entry["state"] == "unavailable" and list(entry)[0] == "reason" and "total" not in entry
    assert block.state == {}


def test_a_ledger_whose_newest_rows_do_not_fit_keeps_its_sums(store, monkeypatch, corpus_cache_on):
    long_rows = tuple((label, MARA_REF, 10, "settled", f"2026-10-{number:02}", "x" * 400)
                      for number, label in enumerate((f"r{n}" for n in range(1, 11)), start=1))
    build(store, monkeypatch, rows=long_rows, subject=SUBJECT.replace("}", ", recent: 10}"))
    packet = _activate(store.root, turn=TURN)
    [entry] = blocks(packet)
    assert entry["total"] == {"sum": 100, "rows": 10} and "newest" not in entry
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_a_nearly_full_packet_names_the_ledger_it_had_no_room_for(world):
    packet = {"budget": {"limit_chars": 20_000, "used_chars": 19_600}}
    with request_scope(owner_principal()):
        served = subject_records.for_packet(world.root, packet, TURN, [
            {"ref": MARA_REF, "title": "Mara Quillon", "evidence": ["exact_alias"]}])
    [stub] = blocks(packet)
    assert stub["state"] == "omitted" and stub["reason"] and stub["anchor"]["ref"] == MARA_REF
    assert not {"by_state", "total", "unknown", "newest"} & set(stub)
    assert packet["subject_records"]["omitted"] == 1 and served == {}
    assert 19_600 < packet["budget"]["used_chars"] <= 20_000


def manifest(subject, profile="records"):
    return ledger_text(subject=subject, layout="markdown-items").replace(
        "semantic_profile: records", f"semantic_profile: {profile}").encode()


def test_a_subject_declaration_parses_with_its_defaults(tmp_path):
    parsed = collections.parse_manifest_bytes(tmp_path, tmp_path / path("Tab"), manifest(
        "subject: {field: person, measure: amount}\n"))
    assert parsed.subject == collections.SubjectDeclaration("person", "amount", None, None, (), 3)
    assert parsed.schema.fields["amount"].description == AMOUNT_MEANS
    assert collections.parse_manifest_bytes(tmp_path, tmp_path / path("Tab"), manifest("")).subject is None


@pytest.mark.parametrize(("subject", "fragment"), [
    ("subject: [person]\n", "subject must be a mapping"),
    ("subject: {field: person, measure: amount, total: amount}\n", "subject has unknown key total; remove it"),
    ("subject: {measure: amount}\n", "subject.field is required"),
    ("subject: {field: label, measure: amount}\n",
     "subject.field names the string field label; it must name a declared link field, one of: person"),
    ("subject: {field: person, measure: note}\n",
     "subject.measure names the string field note; it must name a declared integer or number field, one of: amount"),
    ("subject: {field: person, measure: amount, state: day}\n", "subject.state names the date field day"),
    ("subject: {field: person, measure: amount, order: status}\n",
     "subject.order names the enum field status; it must name a declared date or datetime field, one of: day"),
    ("subject: {field: person, measure: amount, show: [missing]}\n", "subject.show names no declared field"),
    ("subject: {field: person, measure: amount, show: [note, note]}\n", "subject.show names a field twice"),
    ("subject: {field: person, measure: amount, show: [person]}\n", "subject.show names the link field person"),
    ("subject: {field: person, measure: amount, show: [label, note, day, status, amount]}\n",
     "subject.show must be a list of at most 4"),
    ("subject: {field: person, measure: amount, recent: 11}\n", "subject.recent must be a whole number from 0 to 10"),
], ids=["not-a-mapping", "unknown-key", "no-field", "field-not-a-link", "measure-not-a-number", "state-a-date",
        "order-not-a-date", "show-undeclared", "show-twice", "show-a-link", "show-too-many", "recent-too-large"])
def test_a_malformed_subject_is_refused_naming_what_to_change(tmp_path, subject, fragment):
    with pytest.raises(collections.CollectionError) as refused:
        collections.parse_manifest_bytes(tmp_path, tmp_path / path("Tab"), manifest(subject))
    assert refused.value.code == "INVALID_COLLECTION_SUBJECT"
    assert fragment in str(refused.value)


def test_only_a_records_collection_declares_a_subject(tmp_path):
    with pytest.raises(collections.CollectionError) as refused:
        collections.parse_manifest_bytes(tmp_path, tmp_path / "Knowledge Base/Planning/Tab/_collection.md",
                                         manifest(SUBJECT, profile="planning"))
    assert refused.value.code == "INVALID_COLLECTION_SUBJECT"


def test_validating_or_revising_a_ledger_refuses_a_malformed_subject(world):
    bad = ledger_text(subject="subject: {field: label, measure: amount}\n")
    with request_scope(owner_principal()), pytest.raises(collections.CollectionError) as validated:
        world.validate_revision(LEDGER, manifest_text=bad)
    with request_scope(owner_principal()), pytest.raises(collections.CollectionError) as revised:
        revise(world, LEDGER, bad)
    assert validated.value.code == revised.value.code == "INVALID_COLLECTION_SUBJECT"
