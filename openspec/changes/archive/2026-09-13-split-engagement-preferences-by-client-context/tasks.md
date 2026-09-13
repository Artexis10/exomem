## 1. Context-aware preference storage and resolution

- [x] 1.1 Add failing tests for the schema-2 record shape, schema-1 read compatibility and in-place upgrade on first write, per-context set and clear under one revision, no-op clear, stale and ABA revisions, invalid context keys and levels, and the previous reader degrading without erasing a schema-2 record; record the expected failures before implementation.
- [x] 1.2 Implement the schema-2 record, `set_preference` with an optional context, `clear_preference`, and the context-aware inspect result; pass the new and existing preference tests in temporary state.
- [x] 1.3 Add failing tests for the context tier in precedence, the `preference:context` source, context derivation for every known client name and for unknown clients, and the operator environment override still winning; implement `context_for_surface` and the resolver change and pass the prominence and prominence-control suites.

## 2. Canonical client control

- [x] 2.1 Extend `configure_memory` with the `context` argument and `clear` action while preserving the three-argument 0.81.0 set; test inspect, set, clear, invalid argument combinations and the operator-override refusal through the registry and the real MCP, REST and CLI adapters.
- [x] 2.2 Test two clients sharing one identity and vault through real MCP `configure_memory`: a coding client saving Balanced for `coding` while a conversational client of the same identity keeps resolving Maximal, and the change visible to a fresh connection without a restart.
- [x] 2.3 Update `docs/prominence.md` and bootstrap guidance for the context split, then regenerate the plugin tree, hosted renders, the v5 hosted candidate, the tool-surface contract, the pinned schema fixture and `docs/capabilities.md`; pass the sync, fidelity, candidate, capabilities and privacy checks.

## 3. Delivery

- [x] 3.1 Obtain an author-independent review of the actual diff covering the migration, the clear/set race under one revision, context derivation for unknown clients, the override precedence and every path the context string travels; resolve blocking findings and recheck.
- [x] 3.2 Run the scoped suites during rounds and the full CI shard suite on the pull request head, strict OpenSpec validation and the public-artifact privacy gate; open a ready pull request with the evidence.
- [x] 3.3 Deliver under the standing release authority, save Balanced for `coding` on the personal service, verify a fresh coding client resolves Balanced from `preference:context` and a fresh conversational client resolves Maximal, confirm the unrelated service instance is unchanged, then synchronize and archive this change.

## Delivery evidence

- Implementation merged in PR #1241 (`f083ad6f`) and published in v0.82.0.
- Independent review: APPROVE after one correction round (clear under an operator override); recheck APPROVE. CI green on the PR head (35 checks); release-evidence full run green at `e5400402`.
- Managed deployment 2026-09-13: worker 1697388 replaced by 727132 under supervisor 445696 at idle; one TCP connection survived; 164 dependency pins and the service configuration unchanged; the unrelated instance untouched.
- Live verification on the personal service: the existing 0.81.0 record (Maximal, revision `e63e5e8a…`) read as identity-wide Maximal with no contexts; a coding client saved Balanced for `coding` (receipt `f75c72dc-2be5-4e6d-9f36-7a355037e4e1`, revision `8d8b1504…`), and the response resolved Balanced from `preference:context` with the identity-wide Maximal retained for conversational clients.
