## Why

Exomem 0.24.2 can leave callers facing minutes of opaque `MUTATION_BUSY` contention after an edit/preflight sequence, while acknowledgement-loss retries are not replay-safe in the released tree. Its proactive capture guidance also hard-codes note-shaped stepping stones and never routes durable recurring people or organizations into Entities, so the knowledge graph silently loses its nodes while Notes continue growing.

These are launch blockers: one undermines governed-write reliability, and the other undermines Exomem's promise of an ever-improving governed graph.

## What Changes

- Keep `edit_memory` as the canonical surgical-edit tool, but make validate-only invocations genuinely read-only and non-blocking on the vault mutation boundary.
- Integrate replay-safe mutation receipts so identical MCP retries wait/replay outside the exclusive mutation boundary instead of surfacing ambiguous `MUTATION_BUSY` results after acknowledgement loss.
- Add bounded owner/request/age telemetry for the vault mutation boundary and expose safe readiness diagnostics for long holders without leaking vault content.
- Bound background reconciliation work so optional media maintenance cannot monopolize the global vault mutation boundary for an unbounded batch.
- Replace duplicated entity-kind switches and prose with one internal registry of stable supported kinds; selected knowledge packs use that registry to prioritize relevant entity capture.
- Add Organizations as a built-in entity type and make index creation/refresh registry-driven.
- Update proactive capture guidance to recognize durable recurring entities, check for an existing canonical entity first, then update/link or create conservatively instead of producing entity spam.
- Generate tool guidance, scaffold documentation, pack validation, and index choices from the same registry, with drift tests that fail when any surface falls out of sync.
- Add real edit/preflight/cancellation/retry regressions plus entity creation, update, pack-extension, compatibility, and no-spam tests.

## Capabilities

### New Capabilities
- `entity-schema-registry`: Defines the stable supported entity kinds, their folders/fields, pack priorities, compatibility behavior, and single-source surface generation.
- `proactive-entity-capture`: Defines when agents should create, update, or link durable recurring entities and when they must avoid speculative entity creation.

### Modified Capabilities
- `hosted-mutation-safety`: Strengthens cancellation/retry behavior, long-holder observability, and bounded background-writer participation in the shared mutation boundary.
- `command-surface`: Classifies `edit_memory(validate_only=true)` as read-only and requires registry-derived entity options across MCP, CLI, REST, and bootstrap guidance.

## Impact

Affected areas include writer-lease/idempotency coordination, mutation-lock telemetry, file-watcher/media reconciliation, command read/write classification, entity creation/indexing, pack/schema loading, bootstrap/tool descriptions, the shipped scaffold and hooks, readiness output, focused transport/write tests, entity/link tests, and connector contract verification. Existing entity paths and the `edit_memory` tool name remain compatible; no server-side reasoning model is introduced.
