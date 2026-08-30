# Extend due-state carriage to the operation leaves

## Why

The due-state block is the accumulation signal that reaches an agent
mid-conversation — the channel the no-nudge architecture report made
load-bearing because structural drift accumulates *during* work, not at
session boundaries. Today the carriers are the compact page-write and recall
responses plus the structured-collection mutations. The operation leaves —
vault adoption, Adoption Studio applies, maintenance repair, artifact
preservation, media processing — are not carriers. For adoption and applies
that gap sits exactly on the product's largest write bursts, where the
projection moves most; for artifact preservation and media processing the
leaves' own writes do not move the projected categories, but a carrying
response is still the session's channel for counts that changed since the
last delivery. One honest bound up front: mutating `maintain_memory` (other
than `structured-files`) is refused on every remote surface, so maintenance
carriage reaches the local CLI operator only — the mid-conversation claim for
remote clients rests on the other leaves and on ordinary writes.

The batch-once emission discipline already exists canonically (the emission
ledger's "at most once, at the end of the invocation, under the unchanged
change-only rule", and the cannot-nag bulk rule). The f23 bench family was
deliberately built to measure the absence this change removes: its journey
records that "no product leaf reaches the write carrier" and reports the
emission assertion `unsupported`, and two tripwire tests in
`tests/test_due_state_emission_capture.py` pin the measured zero so that the
day a leaf commits through the carrier, they say so. This change flips those
artifacts deliberately and says so (design D5), rather than leaving them to
go red as a surprise.

The founder decision (2026-08-30, the S6 bulk-carrier question from the
architecture report's open decisions, recorded in the wave's KB milestone
note): the operation leaves carry the block, batch-once. This change adds
carriage, not machinery.

## What Changes

- **`command-surface` delta — one ADDED requirement.** The enumerated
  mutating invocations of the five operation leaves (see the requirement for
  the exact `dry_run`/action/mode qualifications) carry the bounded advisory
  due-state block when — and only when — the invocation commits at least one
  governed write, under the same carrier contract as page writes, reusing the
  shared due-state helpers behind the terminal projection. Emission follows
  the canonical governance and ledger requirements unchanged. The no-write
  case (clean-vault repair, already-valid media, a re-enqueue) carries no
  block: it has no committed terminal to carry one, and closing that gap is a
  response-contract change explicitly out of scope.
- **`command-surface` delta — one MODIFIED requirement.** "The f23 family
  runs against the real runtime": the emission assertion's recorded outcome
  moves from `unsupported` (measured zero carrier) to decided — one block
  against twelve writes in the bulk batch. No family, assertion, predicate,
  gate, or schema changes; design D5 adjudicates why this needs no §7
  amendment.
- **No new machinery.** No tool input schema changes, no projection or ledger
  changes, no new emission rules.

## Impact

- Affected specs: `command-surface` (one added requirement, one modified
  requirement).
- Affected code (implementation slice, after approval): the five leaf
  responders reach the shared due-state terminal projection; the
  projection-delta path for the newly carrying leaves is settled per design
  D3; the f23 driver docstring updated.
- Affected tests, named: `tests/test_due_state_emission_capture.py` — the two
  tripwire pins (`test_a_multi_write_command_carries_one_block`-adjacent
  zero-carrier assertions) are **inverted, not deleted** (tasks 2.1).
- Affected benchmarks: f23's recorded emission-assertion outcome flips
  `unsupported` → decided and is recorded in tasks; f26
  (`hookless_episode_carrier`, withheld with amendment sequence 2) has its
  measured world change recorded when sequence 2 activates.
- Not affected: due-state projection and ledger semantics, emission
  governance, family dispositions, tool input schemas, the legacy response
  detail. If any leaf's recorded response contract or packaged digest moves,
  the documented two-phase response-contract rollout applies.
