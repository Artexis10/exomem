## Why

Exomem can adopt legacy material and govern an existing vault, but it cannot reconcile two already-managed Exomems into one policy-bound destination through a single previewable, resumable workflow. The missing product path blocks a safe move from temporary physical isolation to one owner-controlled vault with deterministic delegated disclosure.

## What Changes

- Add a first-class natural-language-facing `consolidate_memory` product command across MCP, REST, Hosted, CLI, OpenAPI, and capability documentation.
- Intake a quiesced, content-addressed source export only when its source identity and authenticity are bound by an authenticated transport receipt or signed attestation.
- Inventory source and destination without mutation, reconcile identities and paths deterministically, and surface every non-byte-identical conflict for owner review.
- Produce one exact joint content-plus-policy plan with representative principal-by-purpose disclosure results and a single-use approval bound to both vault snapshots, principal attestations, conflict decisions, preimage, expiry, and planned writes.
- Execute an in-place destination-sealed saga: stage outside recall, seal ordinary reads, activate restrictive policy first, publish journaled content batches, rebuild derived indexes, verify positive and negative access, then unseal.
- Preserve a content-addressed destination preimage and distinguish pre-publication abort, post-publication rollback, and separately approved source retirement.
- Store durable owner-only run control state and plaintext-free receipts while keeping inventories, paths, conflicts, and previews invisible to ordinary knowledge surfaces.
- Preserve Sources, Evidence, Records, media, semantic units, identities, history, relations, citations, review state, and provenance without transplanting source audience hashes or live policy as destination authority.

## Capabilities

### New Capabilities

- `vault-consolidation`: Authenticated two-vault inventory, deterministic reconciliation, exact joint review, destination-sealed application, verification, recovery, rollback, and source-retirement gating.

### Modified Capabilities

- `command-surface`: Expose one multiplexed `consolidate_memory` command with action-aware read/write classification and surface parity.
- `hosted-vault-portability`: Bind exports to source identity/authenticity and make verified archives reusable as bounded consolidation intake without permitting active-root overlay restore.
- `hosted-mutation-safety`: Add destination sealing, exclusive consolidation authority, journaled publication, preimage restoration, and crash/retry admission rules.
- `release-gate`: Enforce a destination-wide content-free seal for ordinary principals throughout policy/content publication and recovery.
- `disclosure-evidence`: Record plaintext-free consolidation intents, phase transitions, verification outcomes, aborts, rollbacks, and retirement approvals without making receipts policy input.
- `product-e2e`: Prove the installed multi-surface consolidation lifecycle, restart recovery, and negative-disclosure behavior.

## Impact

- Adds a durable consolidation run engine, source archive/attestation validation, reconciliation planner, exact review token, sealed saga coordinator, verification matrix, rollback handling, and one command-registry entry.
- Reuses Hosted portability manifests, governance journals/markers/receipts, batch writes, canonical identity/reference resolution, review primitives, rebuild machinery, and writer-lease/idempotency boundaries; Adoption Studio remains unchanged and is not used as a vault merge.
- **BREAKING**: an unsigned or unauthenticated archive, stale plan, unresolved conflict, unbound destination principal, unverified rollback preimage, or failed disclosure probe refuses application.
- The capability is deterministic pure substrate: the agent interprets intent and proposes decisions, while Exomem validates exact state and effects; no server-side reasoning model or optional heavy dependency is added.
- This change implements reusable product capability only. A real source/destination rehearsal, connector switch, and source retirement remain separate exact-plan operational approvals.
