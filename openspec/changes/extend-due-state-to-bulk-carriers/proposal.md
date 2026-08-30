# Extend due-state carriage to the bulk-operation leaves

## Why

The due-state block is the one accumulation signal that reaches every client
mid-conversation — the channel the no-nudge architecture report made
load-bearing precisely because structural drift accumulates *during* work, not
at session boundaries. Today the carriers are the compact page-write and
recall responses plus the structured-collection mutations (`command-surface`,
"Structured-collection mutations are due-state carriers"). The bulk-operation
leaves — vault adoption, Adoption Studio applies, maintenance fix passes,
artifact preservation, media processing — are not carriers, yet they are the
product's largest write bursts: exactly where the projection moves most and
the agent currently hears nothing until the next ordinary write.

The batch-once emission discipline already exists canonically: the emission
ledger requires a multi-write invocation to emit "at most once, at the end of
the invocation, under the unchanged change-only rule", and the
cannot-nag requirement pins "a bulk import does not emit forty blocks". What
is missing is only carriage. The founder decision (2026-08-30, S6
bulk-carrier question from the report's open decisions): the bulk leaves carry
the block, batch-once. This change adds carriage, not machinery.

## What Changes

- **`command-surface` delta (one added requirement).** The bulk-operation
  leaves' mutating invocations — `adopt_vault` mutating modes,
  `adoption_studio` mutating actions, `maintain_memory` fix and reconcile
  modes, `preserve_artifacts`, and `process_media` mutating operations — carry
  the bounded advisory due-state block under the same carrier contract and
  emission governance as page writes, reusing the shared due-state helpers;
  one invocation is one batch scope (at most one block, at its end).
- **No new machinery.** No tool input schema changes; no projection, ledger,
  or governance changes — the existing batch-once and change-only rules apply
  as written. If the recorded response contract or packaged digest moves for
  any of the five leaves, the documented two-phase response-contract rollout
  applies.

## Impact

- Affected specs: `command-surface` (one added requirement).
- Affected code (implementation slice, after approval): the five leaf
  responders call the shared due-state release-plane helpers; tests per leaf.
- Not affected: due-state projection and ledger semantics, emission
  governance, family dispositions, tool input schemas, the legacy response
  detail (which continues to omit the block).
