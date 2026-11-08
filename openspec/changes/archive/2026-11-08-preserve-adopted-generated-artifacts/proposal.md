Retirement scope (2026-10-11): Retirement supersedes Hosted v5 packaging and gateway forwarding. Exact adoption, no-handle truth, shared fixtures, and pending client traces remain in scope. Historical Hosted evidence does not close current client acceptance.

## Why

Exomem already preserves exact client-supplied bytes, but active agents have no contract for recognising when a generated file becomes durable work. A file handed to the user can therefore be reduced to a textual note or lost entirely, while naively preserving every generated draft would create uncontrolled Evidence clutter.

## What Changes

- Define a generic artifact-adoption transition for images, PDFs, slide decks, spreadsheets, audio, video, text outputs, and other generated deliverables.
- Keep drafts the agent never hands to the user (revised-away, abandoned, and merely generated files) ephemeral by default.
- Treat handing a generated file to the user as adoption of that exact file and preserve its original bytes through the existing client-artifact surface with path, hash, size, content type, and media identity receipts. The rule covers files the agent generates and gives the user, not files it writes or edits in the user's own workspace or repository.
- Make repeat adoption idempotent: identical bytes are stored once, a revision is kept beside the first, and siblings the agent did not hand over do not land.
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
- `portable-agent-contract`: Retained Cloud packages carry adoption doctrine with baseline, recurring-entity, and governed-curation behavior after their shared fixtures are integrated.
- `hosted-gateway-contract`: Clients without direct file handles expose an explicit handoff outcome and cannot report preservation before upload commits.
- `delegation-envelope`: Agent-initiated artifact adoption and delivery observations obey `proactive_capture`; handing over a file is not itself write confirmation or standing authority.

## Impact

Affected areas include the generic scaffold, retained Cloud packages, bootstrap and delegation behavior, Source and Evidence client-handle staging, and upload handoff messaging.
Exact-byte/idempotency tests, Record linkage for external delivery, and synthetic cross-media behavior fixtures remain in scope.
Historical Hosted packages and their original receipts remain immutable.
Existing append-only Source/Evidence storage and byte transport remain compatible; no background artifact collector is added.
