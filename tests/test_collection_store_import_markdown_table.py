"""A preserved Markdown page's table imports into a store collection with honest per-cell reporting.

The agent route is the one the importer serves: preserve the page as Evidence, preview, start, read the status.
The fixture is an invented ledger.
"""

from __future__ import annotations

from pathlib import Path

from test_collection_store_importer import CID, refused, run, setup, small
from test_collection_store_writer import store as store  # noqa: F401 - the pytest fixture

from exomem import commands
from exomem.collection_store import typed_storage
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope

LEDGER_FIELDS = (
    "  natural_key: [ref]\n  fields:\n"
    "    ref: {type: string, required: true}\n    entry_date: {type: date}\n    entry_date_text: {type: string}\n"
    "    counterparty: {type: string}\n    amount: {type: number}\n    note: {type: string}\n"
)
# The page opens with an index table, so the ledger is table 1.
PAGE = """# Ledger, spring

| Section | Rows |
|---------|-----:|
| Deposits and refunds | 5 |

| Ref | Date | Counterparty | Amount | Note |
|-----|------|--------------|-------:|------|
| L-01 | 2025-01-15 | Ada Quill | 120.50 | Deposit |
| L-02 | 2025-??-?? | Ada Quill | 80 | Date lost |
| L-03 | 2025-02-03 | Boris Fenn | 45.25 | |
| L-04 | 2025-02-10 | Boris Fenn | about 60 | Estimate |
| L-05 | 2025-03-01 | Ada Quill | 19.75 | Refund |

Closing remarks.
"""
MAPPING = {
    "table": 1,
    "fields": {
        "ref": "Ref",
        "entry_date": {"from": "Date", "type": "date"},
        "entry_date_text": "Date",
        "counterparty": "Counterparty",
        "amount": {"from": "Amount", "type": "number"},
        "note": "Note",
    },
    "on_invalid": "skip",
    "coverage": {name: {"classification": None} for name in ("Ref", "Date", "Counterparty", "Amount", "Note")},
}


def _agent(store, owner, **request):
    with request_scope(owner), preview_store(store.root, store.handle):
        return commands.op_record_memory(store.root, action="import", collection=CID, import_request=request)


def test_an_agent_previews_and_imports_a_ledger_table_from_preserved_evidence(store):
    """Defect: a Markdown ledger page had no import path, and a bad cell was lost or stored as null unseen."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "spring", "ledger.md", PAGE)

    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table",
                     mapping=MAPPING, check_sum={"field": "amount", "group_by": "counterparty"})

    assert preview["mapping"]["findings"] == []
    assert [(found["table"], found["columns"][0], found["rows"]) for found in preview["source"]["tables"]] == [
        (0, "Section", 1), (1, "Ref", 5)]
    assert (preview["coverage"], preview["rows_total"], preview["would_insert"]) == ("full", 5, 4)
    assert preview["columns"] == {
        "ref": {"column": "Ref", "parsed": 5, "missing": 0, "unparsed": 0, "rejected": 0},
        "entry_date": {"column": "Date", "parsed": 4, "missing": 0, "unparsed": 1, "rejected": 0},
        "entry_date_text": {"column": "Date", "parsed": 5, "missing": 0, "unparsed": 0, "rejected": 0},
        "counterparty": {"column": "Counterparty", "parsed": 5, "missing": 0, "unparsed": 0, "rejected": 0},
        "amount": {"column": "Amount", "parsed": 4, "missing": 0, "unparsed": 0, "rejected": 1},
        "note": {"column": "Note", "parsed": 4, "missing": 1, "unparsed": 0, "rejected": 0},
    }
    rejected = preview["would_reject"]["IMPORT_VALUE_INVALID"]
    assert rejected["rows"] == 1
    assert [(cell["row"], cell["column"], cell["text"]) for cell in rejected["first"]] == [(3, "Amount", "about 60")]
    assert preview["check_sum"] == {
        "field": "amount", "group_by": "counterparty", "rows": 4, "missing": 0, "total": 265.5,
        "groups": [{"group": "Ada Quill", "rows": 3, "sum": 220.25}, {"group": "Boris Fenn", "rows": 1, "sum": 45.25}],
        "groups_total": 2,
    }

    job = _agent(store, owner, mode="start", source_ref=kept["path"], format="markdown-table", mapping=MAPPING)
    for _ in range(8):
        run(store)
        if _agent(store, owner, mode="status", continuation=job["continuation"])["state"] != "running":
            break
    done = _agent(store, owner, mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["imported"]) == ("complete", 4)

    committed = {values["ref"]: values for _, values in typed_storage.collection_values(store.connection, CID)}
    cited = {
        source for (source,) in store.connection.execute(
            "SELECT s.source_ref FROM items i JOIN item_sources s "
            "ON s.row_id = i.row_id AND s.row_version = i.row_version WHERE i.collection_id = ?", (CID,))
    }
    assert sorted(committed) == ["L-01", "L-02", "L-03", "L-05"]
    assert cited == {kept["path"]}
    lost, kept_date = committed["L-02"], committed["L-01"]
    assert "entry_date" not in lost and lost["entry_date_text"] == "2025-??-??" and lost["amount"] == 80
    assert kept_date["entry_date"] == "2025-01-15" and kept_date["amount"] == 120.5
    assert "note" not in committed["L-03"]


def test_a_table_import_resumes_across_batches_without_losing_or_repeating_a_row(store, monkeypatch):
    """Defect: a job's checkpoint is a row ordinal and a byte offset, and a Markdown page has no line framing to resume by."""
    small(monkeypatch, 2)
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "spring", "ledger.md", PAGE)
    job = _agent(store, owner, mode="start", source_ref=kept["path"], format="markdown-table", mapping=MAPPING)
    for _ in range(10):
        run(store, max_batches=1)
        done = _agent(store, owner, mode="status", continuation=job["continuation"])
        if done["state"] != "running":
            break
    assert (done["state"], done["rows"]["inserted"], done["rows"]["rejected"]) == ("complete", 4, 1)
    assert sorted(values["ref"] for _, values in typed_storage.collection_values(store.connection, CID)) == [
        "L-01", "L-02", "L-03", "L-05"]


def test_a_table_index_past_the_page_is_reported_with_the_tables_it_has(store):
    """Defect: a wrong `table` must name what the page holds, or the agent retries blind."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "spring", "ledger.md", PAGE)
    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table",
                     mapping={**MAPPING, "table": 7})
    assert (preview["coverage"], preview["rows_total"]) == ("partial", None)
    assert [(error["code"], error["at"]) for error in preview["errors"]] == [("IMPORT_TABLE_NOT_FOUND", "mapping.table")]
    assert [found["table"] for found in preview["source"]["tables"]] == [0, 1]


def test_a_page_that_is_not_preserved_evidence_is_refused_with_the_save_evidence_route(store):
    """Defect: the refusal named no tool the agent can call, so it hand-built bulk_upsert payloads."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    error = refused(_agent, store, owner_principal(surface="mcp"), mode="preview",
                    source_ref="Knowledge Base/Notes/ledger.md", format="markdown-table", mapping=MAPPING)
    assert error.code == "IMPORT_SOURCE_NOT_FOUND"
    assert "save_evidence" in error.details["repair"] and "source_ref" in error.details["repair"]


def test_read_instructions_serves_the_records_import_contract_and_a_call_that_works(store):
    """Defect: the import contract lived only in a tool description, so an agent never learned a format or mode."""
    reply = commands.op_bootstrap(Path("."), topic="records.import")

    assert {tool["tool"] for tool in reply["tools"]} == {"inspect_memory", "manage_memory"}
    contract = reply["contract"]
    assert "markdown-table" in contract["import_request"]["format"]
    assert {"table", "fields"} <= set(contract["import_request"]["mapping"])
    assert {"check_sum", "reimport", "continuation", "members", "source_ref"} <= set(contract["import_request"])
    assert "save_evidence" in contract["import_request"]["source_ref"]

    # The taught preview call must itself preview cleanly, or an agent that copies it starts on a refusal.
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "taught", "ledger.md", PAGE.split("\n\n", 2)[2])
    taught = reply["examples"][0]["call"]["arguments"]["import_request"]
    preview = _agent(store, owner, **{**taught, "source_ref": kept["path"]})
    assert (preview["mapping"]["findings"], preview["coverage"], preview["rows_total"]) == ([], "full", 5)
    assert preview["check_sum"]["total"] == 265.5
