## Why

Search cannot find a fact that exists only as a value inside a preserved JSON, CSV or TSV dataset. Today only the dataset's card is indexed, and the card holds no values. On 7 November 2026 an account number was preserved as one field in a 2.4 KB JSON profile, and two agents later searched for it and concluded that it was not on file. A `.jsonl`, `.yaml` or `.xml` export of the same bytes would have been found, because those formats get a searchable text preview.

The exclusion was deliberate: raw dataset reads stay behind owner-reviewed classification, a headerless first CSV record can be sensitive, and exports can hold millions of rows. Those reasons hold for large datasets and for readers who may not read the rows. They do not justify hiding a small dataset from a reader who may already read every row with `query_dataset`.

## What Changes

- A derived search index holds a bounded text rendering of a small dataset's values. The vault original and its card stay unchanged.
- That rendering is indexed under the dataset original's own path. Search therefore releases a value hit only to a reader whom the existing release plane allows to read the original at full level, the same rule `query_dataset` applies. Every other reader gets no hit.
- A value hit names the dataset original, carries a bounded passage around the match, and points to `query_dataset` for exact rows.
- Large datasets, malformed datasets, and Records and Planning collections stay excluded.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-source-preservation`: a small dataset's values become searchable for readers allowed to read its rows. The rule that raw rows never become search documents narrows to large datasets and to readers without raw-read access.

## Impact

- One new module, `src/exomem/dataset_values.py`, for the derived sidecar and its lane; one rendering function beside `src/exomem/source_inspection.py`; one marker on `Hit` and one drop rule in `governance/egress.py`; one call in `commands.op_find`.
- No vault writes, no new MCP parameter, no migration: the first search after release indexes existing small datasets.
- Search results may now include dataset originals for full-access readers.
