# Proposal

## Why

A person keeps a ledger or log as a table on a Markdown page. Moving it into a Records collection today means retyping cells into `bulk_upsert` payloads, because `records.import` reads only ndjson, JSON, csv and json-document files. A retyped cell can drift from the page, and a lost cell goes unseen.

## What Changes

- Add `markdown-table` as a `records.import` format. It reads one table of a preserved Markdown page. `mapping.table` names the table by its 0-based place in the page, default 0. Header cells name the columns, and `mapping` maps them as it maps a csv header.
- Keep the source contract. `source_ref` still names a preserved Sources or Evidence file. The refusal for any other ref, the same text for an absent, an unpreserved and a withheld ref, now tells the agent to preserve the file's bytes with `save_evidence` and pass the Evidence path it returns. The Evidence is the agent's own copy of the page's text, not the page. No existing server operation preserves a vault page's exact bytes as Evidence: `adopt_vault` `copy-as-sources` covers files outside the Knowledge Base only and fences their text in a code block. This change builds none. The drift check is to compare `check_sum` with the totals the page states, and the contract and the skill say so.
- Type cells without silent loss. A number cell that does not parse rejects its row, as it does for csv. A leading `+` or `-` reads as a sign; a currency symbol, a thousands separator or parentheses reject the row. A `date` cell that is not `YYYY-MM-DD` leaves the field missing, and a second field mapped to the same column keeps the original text. `date` is an opt-in mapping type for every header format, csv included; a csv mapping that does not ask for it behaves as before. A `scale` on a date, string or boolean cell is refused.
- Drop no cell silently. A row with text in a cell beyond the header's width is rejected as `IMPORT_ROW_MALFORMED`, at `row has N cells, header has M`, in preview and in start. A header fault names its first bad cell.
- Extend the preview summary. For a markdown-table import, or a csv import that maps a `date`, it reports parsed, missing, unparsed and rejected cells and malformed rows per mapped column, adding up to the rows read, and names the column and original text of each rejected cell. `unparsed_cells` lists the first 20 cells left missing as dates, with row, column and original text. A job's status reports the same counts over the rows read so far. `source.tables` lists the first 20 tables and `source.tables_total` the page's table count. A `check_sum` argument adds the sum of one number field, per group when asked, over the rows the preview counts as importing; a group or total with no number is `null`, never 0, and each group reports its own missing count.
- Serve the whole `records.import` contract through `read_instructions(topic="records.import")`: modes, `source_ref`, formats, `table`, `mapping`, `check_sum`, `reimport`, status and cancel, limits, and one worked preview and start call.
- Add one generic example to the shipped skill: migrating a Markdown table into a Records collection.

The csv, ndjson, json-array and json-document formats keep their behaviour. No store schema, write queue or mutation boundary changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: `records.import` reads a Markdown table, reports per-column cell counts and a field sum in preview, and teaches its contract through `read_instructions`.

## Impact

The change adds `src/exomem/collection_store/import_markdown.py`, reads its rows through the existing importer reader, and extends the importer's mapping, preview and contract. It adds a topic to `read_instructions` and one skill example. The Markdown page is parsed whole, once per distinct bytes and cached in process, so the batch clock starts after the parse. A page over 256 KiB or 150,000 parser tokens is refused as `IMPORT_SOURCE_TOO_LARGE` with advice to import csv, which streams. The token cap also keeps the parser's autofill limit from cutting a table short unseen. Measured on generated 256 KiB pages (2026-11-27), a ledger parses in about 0.15 s and a page full of `[` in about 0.2 s; a page built to be refused costs about 1.2 s once, and its refusal is cached too. The shipped skill stamps are refreshed through `scripts/refresh-skill-contract.py`.
