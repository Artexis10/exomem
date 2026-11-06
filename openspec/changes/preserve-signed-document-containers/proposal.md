# Proposal

## Why

Exomem preserves signed containers as opaque files, so their document payloads cannot enter the existing extraction and search workflow. Container custody, payload identity and signature verification need separate, durable states.

## What Changes

- Preserve supported ASiC-E containers unchanged and publish their unchanged DOCX or PDF payloads through the existing custody writers.
- Record exact parent, member and payload identities, inspection outcomes and unverified signature status.
- Inherit parent access restrictions for payloads and extracted text, including later policy changes.
- Reuse existing media jobs, extraction parts, search and original downloads without another worker or store.
- Keep malformed or unsupported containers as exact opaque originals with actionable inspection results.
- Keep duplicate preservation free of writes; process existing opaque containers only through an explicit supported processing route.

## Capabilities

### New Capabilities

- `signed-document-custody`: Byte-exact container and payload custody, bounded inspection, inherited access and searchable document processing.

### Modified Capabilities

None. Existing preservation, terminal replay, original download and media extraction contracts remain applicable.

## Impact

The change extends Source and Evidence custody planning, shared ZIP validation, companion provenance and access inheritance. It reuses MarkItDown and PyMuPDF; missing optional engines retain their existing actionable states. No model, new service, bulk migration or new credential is required.
