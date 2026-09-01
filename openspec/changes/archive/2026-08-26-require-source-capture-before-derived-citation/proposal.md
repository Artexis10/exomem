## Why

Compiled notes can currently declare a source that does not exist in the governed vault. The write succeeds with a warning because the system permits a compile-then-capture sequence. In practice that creates convincing derived notes whose original material cannot be opened, audited, or recovered, and connector identifiers can masquerade as vault citations.

Dogfooding found exactly this failure in a real production flow: working derivatives survived, but the original external script package was never captured. The product should make the safe sequence the easy sequence—capture raw material first, then cite it—while auditing legacy gaps without inventing evidence from partial notes.

## What Changes

- **BREAKING**: A new compiled-note write with non-empty explicit `sources` refuses to commit unless every entry resolves to governed captured material visible to the caller.
- Keep `sources: []` valid when no external source exists; the contract distinguishes honest absence from a broken citation.
- Apply the same precommit source-closure rule to every semantic writer and to MCP, CLI, and REST facades.
- Store external connector identifiers, message IDs, file IDs, and URLs as provenance on the captured Source or Evidence page rather than accepting them as substitute vault links.
- Return a bounded, non-disclosing refusal with the unresolved entries and the capture-then-retry remediation.
- Allow unrelated edits to legacy notes with unresolved sources, but refuse any operation that creates, replaces, or changes an unresolved source claim.
- Add a dedicated read-only audit category for legacy unresolved source citations and avoid duplicating them as generic broken-wikilink findings.
- Require remediation to capture the original material or explicitly remove the unsupported citation; never reconstruct a missing source from a derivative note.

## Capabilities

### New Capabilities

- `compiled-source-lineage`: Defines source closure, capture-first ordering, external provenance handling, atomic back-reference updates, and safe remediation of missing source material.

### Modified Capabilities

- `command-surface`: Makes source-closure validation and refusal envelopes consistent across semantic writers, MCP, CLI, and REST.
- `attention-queue`: Adds deterministic audit coverage for legacy unresolved source citations without turning the migration into an unsolicited default queue.

## Impact

This changes note normalisation, semantic write precommit checks, source back-reference maintenance, error envelopes, audit classification, and tests for all write surfaces. Existing legacy notes remain readable and editable when an operation does not create or change the unresolved source claim. No raw source is synthesised or backfilled automatically; external recovery remains an explicit user-authorised ingestion step.
