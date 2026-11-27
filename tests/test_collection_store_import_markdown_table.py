"""A preserved Markdown page's table imports into a store collection with honest per-cell reporting.

The agent route is the one the importer serves: preserve the page as Evidence, preview, start, read the status.
The fixture is an invented ledger.
"""

from __future__ import annotations

import time
from pathlib import Path

from test_collection_store_import_exports import agent, finish, preserve_export
from test_collection_store_importer import CID, importer, refused, run, setup, small
from test_collection_store_writer import store as store  # noqa: F401 - the pytest fixture

from exomem import commands
from exomem.collection_store import import_markdown, typed_storage
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
| Deposits and refunds | 9 |

| Ref | Date | Counterparty | Amount | Note |
|-----|------|--------------|-------:|------|
| L-01 | 2025-01-15 | Ada Quill | 120.50 | Deposit |
| L-02 | 2025-??-?? | Ada Quill | 80 | Date lost |
| L-03 | 2025-02-03 | Boris Fenn | 45.25 | |
| L-04 | 2025-02-10 | Boris Fenn | about 60 | Estimate |
| L-05 | 2025-03-01 | Ada Quill | 19.75 | Refund |
| L-06 | ? (reported 2026-03-12) | Boris Fenn | +85.30 | Transfer in |
| L-07 | 2026-03-08? (reported 2026-03-10) | Ada Quill | -40.00 | Transfer out |
| L-08 | 2026-11-02 | Boris Fenn | $1,200.00 | Cash |
| L-09 | 2026-11-03 | Ada Quill | (12.00) | Fee |

Closing remarks.
"""
# The nine ledger rows through each mapped column: L-02, L-06 and L-07 hold a date that is not YYYY-MM-DD, and
# L-04, L-08 and L-09 hold an amount that is not a number.
EXPECTED_COLUMNS = {
    "ref": {"column": "Ref", "parsed": 9, "missing": 0, "unparsed": 0, "rejected": 0, "malformed": 0},
    "entry_date": {"column": "Date", "parsed": 6, "missing": 0, "unparsed": 3, "rejected": 0, "malformed": 0},
    "entry_date_text": {"column": "Date", "parsed": 9, "missing": 0, "unparsed": 0, "rejected": 0, "malformed": 0},
    "counterparty": {"column": "Counterparty", "parsed": 9, "missing": 0, "unparsed": 0, "rejected": 0, "malformed": 0},
    "amount": {"column": "Amount", "parsed": 6, "missing": 0, "unparsed": 0, "rejected": 3, "malformed": 0},
    "note": {"column": "Note", "parsed": 8, "missing": 1, "unparsed": 0, "rejected": 0, "malformed": 0},
}
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
        (0, "Section", 1), (1, "Ref", 9)]
    assert preview["source"]["tables_total"] == 2
    assert (preview["coverage"], preview["rows_total"], preview["would_insert"]) == ("full", 9, 6)
    assert preview["columns"] == EXPECTED_COLUMNS
    # Each column's counts add up to the rows read, so no row goes unaccounted for.
    assert all(sum(count for name, count in tally.items() if name != "column") == 9
               for tally in preview["columns"].values())
    assert preview["unparsed_cells"] == [
        {"row": 1, "column": "Date", "text": "2025-??-??"},
        {"row": 5, "column": "Date", "text": "? (reported 2026-03-12)"},
        {"row": 6, "column": "Date", "text": "2026-03-08? (reported 2026-03-10)"},
    ]
    rejected = preview["would_reject"]["IMPORT_VALUE_INVALID"]
    assert rejected["rows"] == 3
    assert [(cell["row"], cell["column"], cell["text"]) for cell in rejected["first"]] == [
        (3, "Amount", "about 60"), (7, "Amount", "$1,200.00"), (8, "Amount", "(12.00)")]
    assert preview["check_sum"] == {
        "field": "amount", "group_by": "counterparty", "rows": 6, "missing": 0, "total": 310.8,
        "groups": [{"group": "Ada Quill", "rows": 4, "missing": 0, "sum": 180.25},
                   {"group": "Boris Fenn", "rows": 2, "missing": 0, "sum": 130.55}],
        "groups_total": 2,
    }

    job = _agent(store, owner, mode="start", source_ref=kept["path"], format="markdown-table", mapping=MAPPING)
    for _ in range(8):
        run(store)
        if _agent(store, owner, mode="status", continuation=job["continuation"])["state"] != "running":
            break
    done = _agent(store, owner, mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["imported"], done["rows"]["rejected"]) == ("complete", 6, 3)
    assert done["columns"] == EXPECTED_COLUMNS  # the job reports the cells it lost, not only the preview

    committed = {values["ref"]: values for _, values in typed_storage.collection_values(store.connection, CID)}
    cited = {
        source for (source,) in store.connection.execute(
            "SELECT s.source_ref FROM items i JOIN item_sources s "
            "ON s.row_id = i.row_id AND s.row_version = i.row_version WHERE i.collection_id = ?", (CID,))
    }
    assert sorted(committed) == ["L-01", "L-02", "L-03", "L-05", "L-06", "L-07"]
    assert cited == {kept["path"]}
    lost, kept_date = committed["L-02"], committed["L-01"]
    assert "entry_date" not in lost and lost["entry_date_text"] == "2025-??-??" and lost["amount"] == 80
    assert kept_date["entry_date"] == "2025-01-15" and kept_date["amount"] == 120.5
    assert "note" not in committed["L-03"]
    # A ledger's signed amounts and its free-text dates: the sign parses, and the date text survives.
    assert (committed["L-06"]["amount"], committed["L-07"]["amount"]) == (85.3, -40.0)
    assert "entry_date" not in committed["L-06"] and committed["L-06"]["entry_date_text"] == "? (reported 2026-03-12)"
    assert committed["L-07"]["entry_date_text"] == "2026-03-08? (reported 2026-03-10)"


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
    assert (done["state"], done["rows"]["inserted"], done["rows"]["rejected"]) == ("complete", 6, 3)
    assert sorted(values["ref"] for _, values in typed_storage.collection_values(store.connection, CID)) == [
        "L-01", "L-02", "L-03", "L-05", "L-06", "L-07"]
    assert done["columns"] == EXPECTED_COLUMNS  # five batches' counts add up to the whole page's


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


WIDE = """| Ref | Counterparty | Amount |
|-----|--------------|-------:|
| W-01 | Ada Quill | 10 |
| W-02 | Boris Fenn | 20 | spare 5 |
| W-03 | Ada Quill | 30 | |
| W-04 | Boris Fenn | 40 |
"""
WIDE_MAPPING = {
    "fields": {"ref": "Ref", "counterparty": "Counterparty", "amount": {"from": "Amount", "type": "number"}},
    "on_invalid": "skip",
    "coverage": {name: {"classification": None} for name in ("Ref", "Counterparty", "Amount")},
}


def test_a_row_with_text_beyond_the_header_is_rejected_not_silently_cut(store):
    """Defect: the parser drops a cell past the header, so a ledger row lost its fourth cell with no trace."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "wide", "wide.md", WIDE)

    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table",
                     mapping=WIDE_MAPPING, check_sum={"field": "amount"})

    rejected = preview["would_reject"]["IMPORT_ROW_MALFORMED"]
    assert rejected["rows"] == 1
    assert [(found["row"], found["at"]) for found in rejected["first"]] == [(1, "row has 4 cells, header has 3")]
    assert (preview["would_insert"], preview["check_sum"]["total"]) == (3, 80)  # the empty extra cell of W-03 holds nothing
    # The refused row is counted in every column, so each column's counts still add up to the rows read.
    assert preview["rows_total"] == 4
    assert all(tally["malformed"] == 1 and tally["parsed"] == 3 for tally in preview["columns"].values())

    job = _agent(store, owner, mode="start", source_ref=kept["path"], format="markdown-table", mapping=WIDE_MAPPING)
    for _ in range(8):
        run(store)
        if _agent(store, owner, mode="status", continuation=job["continuation"])["state"] != "running":
            break
    done = _agent(store, owner, mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["imported"]) == ("complete", 3)
    assert [(found["row"], found["code"], found["at"]) for found in done["rejections"]] == [
        (1, "IMPORT_ROW_MALFORMED", "row has 4 cells, header has 3")]
    assert done["columns"] == preview["columns"]
    assert sorted(values["ref"] for _, values in typed_storage.collection_values(store.connection, CID)) == [
        "W-01", "W-03", "W-04"]


def test_a_header_that_cannot_name_the_columns_is_reported_at_its_cell(store):
    """Defect: a repeated or empty header cell stopped the read with no position to fix."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(
            store.root, "ledger", "header", "header.md", "| Ref | Amount | Ref |\n|---|---|---|\n| a | 1 | b |\n")
    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table",
                     mapping=WIDE_MAPPING)
    assert [(error["code"], error["at"]) for error in preview["errors"]] == [
        ("IMPORT_SOURCE_MALFORMED", "header cell 2 repeats cell 0")]


def test_a_slow_page_parse_does_not_shrink_the_first_batch(store, monkeypatch):
    """Defect: the batch clock started before the page's parse, so a slow parse made a one-row batch."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "spring", "slow.md", PAGE + "\nSlow.\n")
    job = _agent(store, owner, mode="start", source_ref=kept["path"], format="markdown-table", mapping=MAPPING)

    import_markdown._read.cache_clear()
    parse = import_markdown.read_tables

    def slow(data):
        time.sleep(0.4)  # longer than the batch's clock
        return parse(data)

    monkeypatch.setattr(import_markdown, "read_tables", slow)
    monkeypatch.setattr(importer(), "BATCH_SECONDS", 0.2)
    run(store, max_batches=1)
    status = _agent(store, owner, mode="status", continuation=job["continuation"])
    assert (status["rows"]["imported"], status["rows"]["rejected"]) == (6, 3)  # all nine rows, in one batch


def test_a_page_too_big_or_too_wide_to_read_is_refused_and_points_to_csv(store):
    """Defect: a wide sparse table is cut short by the parser's autofill limit, and a huge page parsed for seconds."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    # 230 one-cell rows under 300 columns autofill 68,000 empty cells, past the parser's limit of 65,536.
    wide = "|" + "|".join(f"c{n}" for n in range(300)) + "|\n|" + "|".join("-" for _ in range(300)) + "|\n" + "|x|\n" * 230
    huge = "| c0 |\n|----|\n" + "| x |\n" * 45_000
    mapping = {"fields": {"ref": "c0"}, "on_invalid": "skip", "coverage": {"c0": {"classification": None}}}
    with request_scope(owner):
        pages = {name: commands.op_preserve_evidence(store.root, "ledger", "limits", f"{name}.md", text)["path"]
                 for name, text in (("wide", wide), ("huge", huge))}
    assert len(huge) > importer().MAX_TABLE_SOURCE_BYTES
    for path in pages.values():
        preview = _agent(store, owner, mode="preview", source_ref=path, format="markdown-table", mapping=mapping)
        assert preview["coverage"] != "full"
        assert [error["code"] for error in preview["errors"]] == ["IMPORT_SOURCE_TOO_LARGE"]
        assert "csv" in preview["errors"][0]["at"]


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
    assert (preview["mapping"]["findings"], preview["coverage"], preview["rows_total"]) == ([], "full", 9)
    assert preview["check_sum"]["total"] == 310.8


def test_a_csv_mapping_can_ask_for_a_date_and_then_reports_the_dates_it_could_not_read(store):
    """Defect: a date type was markdown-only, so a csv date either could not be asked for or was lost unseen."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(
            store.root, "ledger", "csv", "ledger.csv", "Ref,Date,Amount\nC-01,2025-01-15,5\nC-02,2025-??-??,6\n")
    mapping = {
        "fields": {"ref": "Ref", "entry_date": {"from": "Date", "type": "date"}, "amount": {"from": "Amount", "type": "number"}},
        "on_invalid": "skip",
        "coverage": {name: {"classification": None} for name in ("Ref", "Date", "Amount")},
    }
    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="csv", mapping=mapping)
    assert preview["rows_total"] == 2
    assert preview["columns"]["entry_date"] == {
        "column": "Date", "parsed": 1, "missing": 0, "unparsed": 1, "rejected": 0, "malformed": 0}


def test_a_streamed_csv_export_reports_its_cells_in_status_as_preview_does(store):
    """Defect: a job that streams an export's csv members counted no cells, so status lacked the columns preview gave."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    manifest_ref, _ = preserve_export(store, "ledger", {
        "days/a.csv": b"Ref,Date,Amount\nC-01,2025-01-15,5\nC-02,2025-??-??,6\n",
        "days/b.csv": b"Ref,Date,Amount\nC-03,2025-02-01,7\nC-04,,8\n",
    })
    mapping = {
        "fields": {"ref": "Ref", "entry_date": {"from": "Date", "type": "date"}, "amount": {"from": "Amount", "type": "number"}},
        "on_invalid": "skip",
        "coverage": {name: {"classification": None} for name in ("Ref", "Date", "Amount")},
    }
    request = {"source_ref": manifest_ref, "format": "csv", "members": "days/*", "mapping": mapping}
    preview = agent(store, mode="preview", **request)
    done = finish(store, agent(store, mode="start", **request))
    assert (done["state"], done["rows"]["imported"]) == ("complete", 4)
    assert done["columns"]["entry_date"] == preview["columns"]["entry_date"] == {
        "column": "Date", "parsed": 2, "missing": 1, "unparsed": 1, "rejected": 0, "malformed": 0}


def test_a_check_sum_group_with_no_number_reports_null_and_its_own_missing_count(store):
    """Defect: a group whose amount cells were all empty reported a sum of 0, as if the page said so."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    page = ("| Ref | Counterparty | Amount |\n|---|---|--:|\n"
            "| M-1 | Cleo Marsh | |\n| M-2 | Cleo Marsh | |\n| M-3 | Dan Voss | 10 |\n| M-4 | Dan Voss | |\n")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "gaps", "gaps.md", page)
    mapping = {
        "fields": {"ref": "Ref", "counterparty": "Counterparty", "amount": {"from": "Amount", "type": "number"}},
        "on_invalid": "skip",
        "coverage": {name: {"classification": None} for name in ("Ref", "Counterparty", "Amount")},
    }
    preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table", mapping=mapping,
                     check_sum={"field": "amount", "group_by": "counterparty"})
    assert preview["check_sum"]["groups"] == [
        {"group": "Cleo Marsh", "rows": 2, "missing": 2, "sum": None},
        {"group": "Dan Voss", "rows": 2, "missing": 1, "sum": 10},
    ]
    assert (preview["check_sum"]["rows"], preview["check_sum"]["missing"], preview["check_sum"]["total"]) == (4, 3, 10)


def test_a_scale_on_a_cell_that_is_not_a_number_is_refused_when_the_mapping_is_read(store):
    """Defect: a scale on a date cell made every date read as missing, and the page showed nothing wrong."""
    setup(store, b"", title="Ledger", fields=LEDGER_FIELDS)
    owner = owner_principal(surface="mcp")
    with request_scope(owner):
        kept = commands.op_preserve_evidence(store.root, "ledger", "scale", "scale.md", PAGE)
    for kind in ("date", "string", "boolean"):
        mapping = {**MAPPING, "fields": {"ref": "Ref", "entry_date": {"from": "Date", "type": kind, "scale": 2}}}
        preview = _agent(store, owner, mode="preview", source_ref=kept["path"], format="markdown-table", mapping=mapping)
        assert [(found["code"], found["at"]) for found in preview["mapping"]["findings"]] == [
            ("IMPORT_MAPPING_INVALID", "mapping.fields.entry_date.scale")], kind
