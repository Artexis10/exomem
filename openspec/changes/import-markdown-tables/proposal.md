# Proposal

## Why

A person keeps a ledger or log as a table on a Markdown page. Moving it into a Records collection today means retyping cells into `bulk_upsert` payloads, because `records.import` reads only ndjson, JSON, csv and json-document files. A retyped cell can drift from the page, and a lost cell goes unseen.

## What Changes

- Add `markdown-table` as a `records.import` format. It reads one table of a preserved Markdown page. `mapping.table` names the table by its 0-based place in the page, default 0. Header cells name the columns, and `mapping` maps them as it maps a csv header.
- Keep the source contract. `source_ref` still names a preserved Sources or Evidence file. The refusal for any other ref, the same text for an absent, an unpreserved and a withheld ref, now tells the agent to preserve the file's bytes with `save_evidence` and pass the Evidence path it returns.
- Type cells without silent loss. A number cell that does not parse rejects its row, as it does for csv. A `date` cell that is not `YYYY-MM-DD` leaves the field missing, and a second field mapped to the same column keeps the original text.
- Drop no cell silently. A row with text in a cell beyond the header's width is rejected as `IMPORT_ROW_MALFORMED`, at `row has N cells, header has M`, in preview and in start. A header fault names its first bad cell.
- Extend the preview summary. For a markdown-table import it reports parsed, missing, unparsed and rejected cell counts per mapped column, and names the column and original text of each rejected cell. A `check_sum` argument adds the sum of one number field, per group when asked, over the rows the preview counts as importing.
- Serve the whole `records.import` contract through `read_instructions(topic="records.import")`: modes, `source_ref`, formats, `table`, `mapping`, `check_sum`, `reimport`, status and cancel, limits, and one worked preview and start call.
- Add one generic example to the shipped skill: migrating a Markdown table into a Records collection.

The csv, ndjson, json-array and json-document formats keep their behaviour. No store schema, write queue or mutation boundary changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: `records.import` reads a Markdown table, reports per-column cell counts and a field sum in preview, and teaches its contract through `read_instructions`.

## Impact

The change adds `src/exomem/collection_store/import_markdown.py`, reads its rows through the existing importer reader, and extends the importer's mapping, preview and contract. It adds a topic to `read_instructions` and one skill example. The Markdown page is parsed whole, so the source is capped at 256 KiB; a larger table belongs in csv, which streams. The shipped skill stamps are refreshed through `scripts/refresh-skill-contract.py`.
