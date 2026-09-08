<!-- authority:non-specification -->

# Structured files and text exports

New evidence uploads retain the original bytes and create a searchable source
card. This applies to direct preservation, streaming uploads and client evidence
attachments that use the shared preservation operation.

| Format | Automatic discovery | Exact content |
| --- | --- | --- |
| CSV, TSV, JSON | Dataset source card with structural counts; bounded field names for JSON | `query_dataset` reads rows, filters, nested JSON fields and aggregates from the original |
| YAML/YML, JSONL/NDJSON, XML, TOML | Literal UTF-8 text preview | Original download; these formats do not gain structured query support |
| Other permitted files | Original and source companion | Extraction depends on the installed media/document capability |

For example, uploading `readings.csv` creates `readings.csv.md` with the
`dataset-export` source kind and a `data_file` pointer. Search discovers that
card, including with `file_types: [csv]`. Use its `data_file` with
`query_dataset(aggregate="sum:reading")` for a computed total. The card can be
cited as a source. Individual row values stay out of its automatic preview.

Dataset inspection uses at most 1 MiB and 10,000 records. JSON lists at most 64
fields, each shortened to 256 characters. CSV/TSV cards omit header labels, since a headerless first data row can look like a header. These cards state the first-record-as-header assumption and show column and parsed-record counts; authorized queries expose the actual fields. A 100,000-cell CSV/TSV shape budget prevents wide headers with short rows from expanding inspection memory. JSON field discovery samples the first
500 records; a larger record set or shortened field listing is marked `limited`.
JSON nesting beyond 64 levels is unavailable for automatic inspection. Exact
queries retain their separate 25 MiB input, 10,000 parsed-record and bounded
response limits.

Literal previews inspect the first 64 KiB. They show truncation explicitly and
reject malformed UTF-8 within the inspected prefix instead of substituting
corrupted text. YAML tags and XML entities are displayed as data; inspection
does not evaluate them or fetch references. Caller-provided text extraction
retains its existing behavior for these formats.

Oversize or malformed inspection does not discard an otherwise permitted
upload. The card reports `byte_limit`, `row_limit`, `invalid_encoding` or
`invalid_data` or `shape_limit` without inventing a schema. Download still returns the original
bytes. A successful preview does not promise that every later query is valid.

The card retains normal source governance. Uploading a dataset does not assert
reviewed semantic classification for its raw bytes: policies that need that
classification continue to withhold raw reads until the existing owner-reviewed
`backfill_companion` flow supplies it. Path-based policies remain effective.

These previews require no GPU, model, additional package or network service.
Historical companions are not rewritten, and this core capability does not
activate a hosted worker or change a deployment. Mutable ongoing datasets belong
to the separate [Records](records.md) workflow.
