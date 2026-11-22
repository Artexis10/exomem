# Proposal

## Why

A summary can outrank the transcript that answers a question. Even when search finds the transcript, its excerpt can miss the words that matched. Importers also need to recover a committed file capture by external ID.

## What Changes

- Carry a verified transcript passage through the existing search and citation path.
- Rank a competitive Source passage ahead of summaries that cite it on default hybrid search.
- Bind an optional importer-owned external ID to one committed, exact-byte Source capture.

## Capabilities

### Modified Capabilities

- `passage-evidence` adds transcript matches to ordinary search.
- `durable-source-capture` adds importer-scoped receipts to single-file capture.

## Impact

Search still uses the existing retrieval encoders. Ingestion and receipt validation use exact bytes and deterministic checks. Exomem publishes transcript chunks as derived data.
