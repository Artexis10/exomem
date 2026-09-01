## Why

Exomem already preserves exact client-supplied bytes, but active agents have no contract for recognising when a generated draft becomes durable work. A selected, approved, sent, or published output can therefore be reduced to a textual note or lost entirely, while naively preserving every generated draft would create uncontrolled Evidence clutter.

## What Changes

- Define a generic artifact-adoption transition for images, PDFs, slide decks, spreadsheets, audio, video, text outputs, and other generated deliverables.
- Keep unselected, revised-away, abandoned, and merely generated drafts ephemeral by default.
- Treat explicit selection or approval as adoption of one exact offered artifact and preserve its original bytes through the existing client-artifact surface with path, hash, size, content type, and media identity receipts.
- Make repeat adoption idempotent and prevent rejected sibling variants from landing.
- Treat sent/published/delivered as a separate observed event linked to the preserved artifact; do not claim third-party byte identity without a platform receipt or export.
- Route reasoning material to Source and proof-bearing final/correspondence output to Evidence without using MIME type as the semantic decision.
- Teach capability-sensitive delivery: direct file handles use `capture_source` for adopted reasoning inputs and `preserve_artifacts` for adopted Evidence outputs; clients without them expose an honest upload/gateway handoff and never claim success from capability minting alone.
- Add real-client behavior fixtures and mutants for missing adoption gates, wrong-variant preservation, textual reconstruction, false save claims, duplicates, and draft clutter.

## Capabilities

### New Capabilities

- `generated-artifact-adoption`: Draft-versus-adopted lifecycle, exact artifact identity, idempotency, semantic destination, external-delivery observation, and active-agent behavior.

### Modified Capabilities

- `client-artifact-preservation`: Existing exact-byte receipts become the required transport for adopted generated outputs; textual reconstruction is explicitly insufficient.
- `attachment-source-ingestion`: The Source handle lane accepts the same adoption identity and writes its receipt atomically into the canonical Source page.
- `agent-bootstrap-contract`: The active agent receives the adoption trigger and draft-no-write boundary.
- `hosted-agent-surface`: Hosted adoption doctrine and fixtures participate in the single shared v5 candidate owned by `capture-durable-personal-baselines`; this change does not mint a competing candidate.
- `hosted-gateway-contract`: Clients without direct file handles expose an explicit handoff outcome and cannot report preservation before upload commits.
- `delegation-envelope`: Agent-initiated artifact adoption and delivery observations obey `proactive_capture`; selecting a variant is not itself write confirmation or standing authority.

## Impact

Affected areas include scaffold and Hosted skills, bootstrap and delegation behavior, Source and Evidence client-handle staging, upload handoff messaging, exact-byte/idempotency tests, Record linkage for external delivery, the shared v5 candidate package, and synthetic cross-media behavior fixtures. Existing append-only Source/Evidence storage and byte transport remain compatible; no background artifact collector is added.
