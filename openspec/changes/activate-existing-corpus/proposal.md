## Why

Exomem can already represent deep epistemic relationships, but an existing vault can remain mostly connected by generic wikilinks with little typed relation or provenance structure. The system needs a safe, low-friction way to measure that dormant potential and turn it into a reviewable improvement loop without bulk inference or automatic rewriting.

## What Changes

- Add a read-only corpus-activation report that measures graph and epistemic coverage across eligible compiled knowledge.
- Add a dedicated ranked activation queue for disconnected notes, generic-link-only notes, provenance gaps, and unregistered relation observations.
- Give every activation item stable review identity, measured reasons, and concrete routes into existing proposal and governed edit operations.
- Preserve human judgment: activation never asserts a semantic relationship, changes a note, or mutates Sources, Evidence, or read-only content.
- Keep model-assisted relation suggestions explicit and optional; the activation measurement and ranking path is deterministic, dependency-light, and soft-fails individual unreadable notes.
- Verify the full lifecycle across the shared MCP, REST, and CLI command surface, including triage persistence and non-mutation.

## Capabilities

### New Capabilities

- `corpus-activation`: Measures existing-corpus coverage and provides a deterministic, governed queue for progressively activating deeper epistemic structure.

### Modified Capabilities

- `attention-queue`: Adds a dedicated activation review composition that reuses stable item identity, deterministic ranking, triage, and read-only guarantees without changing the default daily attention categories.

## Impact

This affects audit/coverage measurement, attention queue composition, `review_memory` routing, command documentation, and focused product E2E tests. It adds no required service, model, storage migration, or automatic write path; existing generated MCP, REST, and CLI surfaces inherit the behavior from the shared command implementation.
