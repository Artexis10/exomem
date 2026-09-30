## Context

See proposal.md for the ingestion gap. The query engine accepts CSV, TSV and JSON with byte, row and response limits. Governance resolves these files through a unique `type: dataset` card with `data_file`, format and a closed descriptor bound to the exact original. Raw dataset rows are excluded from ordinary recall.

## Goals / Non-Goals

Create useful, citable source discovery within the existing atomic evidence writer. Inspection must consume the writer's private staged bytes, never reopen a published pathname or invoke a model. Keep upload-time memory and work modest.

This change does not migrate existing companions, add dataset mutations, activate a hosted worker, or change Records and Planning discovery. Existing standalone source import and file-watcher reconciliation are not additional entrypoints in this change.

## Decisions

### Dataset cards belong to the canonical evidence writer

Extend the shared sidecar renderer with an explicit dataset projection. Cards retain `type: source`, `source_type: dataset-export`, existing source tags, citation eligibility, capture and adoption metadata. `source_type: dataset-export`, `data_file` and format identify the dataset source card. The dataset companion locator accepts this explicit shape alongside legacy `type: dataset` cards. New cards carry exact provenance but omit a governance descriptor until owner-reviewed classification. Originals and cards remain one create-only publication batch. Reusing this path covers streaming upload, direct preservation and client evidence adoption without a new API.

Automatic inspection reuses the existing row parser under a smaller 1 MiB input budget. A streaming CSV/TSV preflight caps the padded table shape at 100,000 cells before row dictionaries are allocated, preventing a wide header and many short rows from amplifying small input into large memory use. A successful card records the exact parsed row count. JSON cards show at most 64 observed field names, each bounded to 256 characters. CSV/TSV cards show column count but omit header labels: without an explicit header declaration, the first record can be sensitive data. The existing exact parser assumes a header, which the card states; header content remains behind authorized queries. Schema sampling/truncation is explicit. No row values or numeric/categorical summaries are copied into the card. Exact answers continue through `query_dataset`, whose existing limits and authorization remain unchanged. Caller-supplied extracted text is omitted from dataset cards; the description remains a caller-authored caption.

Unavailable inspection produces a card with a stable status code and no fabricated row count. Oversize, malformed, excessive-row, invalid encoding and excessive nesting outcomes preserve the original. Error messages from parsers are not persisted. An upload-time preview budget is distinct from the larger exact-query budget.

### Literal text previews keep binary companion compatibility

YAML, YML, JSONL, NDJSON, XML and TOML are a separate explicit literal-text allowlist in a small dependency-free inspection module. Read at most 64 KiB plus a UTF-8 boundary/lookahead allowance, use strict decoding, and visibly mark truncation. Use a code fence longer than every backtick run in the original prefix so Markdown and graph scanners both treat the preview literally. Treat syntax, tags, entities and formulas as data; perform no format evaluation, file resolution or network fetch.

Keep `media_types` unchanged: classifying these extensions as media would invalidate historical binary companion descriptors and require migration. The preview is written by preserve from its private stage and needs no media job. Existing caller-provided extraction remains supported for these source formats.

### Preserve the governance boundary

A dataset upload does not supply reviewed semantic classification. Automatically asserting empty semantics would change previously unresolved raw datasets into unmatched, fully released content under semantic-only policies. New dataset source cards therefore omit the descriptor: existing owner-only receipt-first companion backfill supplies it when classification is wanted. Policy-free and path/ref-only queries continue to work; still-undecided semantic scopes continue to withhold raw reads. The source page retains its existing type and tags, so its new preview obeys existing source policies and remains a valid citation. Dataset format/path values are safely serialized. Missing, duplicate or stale cards continue to fail closed under existing companion classification. No existing card is adopted or overwritten. Raw datasets and Records/Planning exclusion behavior are unchanged.

## Risks / Trade-offs

- A dataset larger than the automatic budget has a discoverable card without a schema; an explicit bounded query may still work within the query engine's larger budget.
- Field names can be sensitive and are content. They appear only in the governed companion and are bounded and escaped as literal text.
- Historical generic companions remain historical. Converting them requires a separately authorized migration that respects immutable evidence; this release does not silently rewrite them.
- JSONL is searchable literal text in this change, not a new structured query format. YAML and XML are not evaluated.

## Migration Plan

Deliver the core behavior through a reviewed PR and the normal release process. Test new captures, original-byte identity, governed discovery and exact reads, failure cases, and legacy compatibility. Deploying or activating the hosted service is separate. Rolling back stops new inspection. Source cards remain discoverable and citable; a rollback also removes recognition of the new dataset source-card variant for raw semantic classification, so those raw reads fail closed until the matching code returns.
