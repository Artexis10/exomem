# Design

## Context

See proposal.md for the availability defect.
The receipt store already holds exact pending paths, revisions, generations, and component demand.
The overlay currently discards all coverage when any page cannot be proven or more than 512 rows remain.
Its publication callbacks also depend on hydrated batches, so overflow can prevent retirement.

## Goals / Non-Goals

**Goals:** Preserve useful admitted recall and recover derived custody through its existing owner.
Keep body hydration bounded and keep repaired state separate from canonical mutation authority.

**Non-Goals:** Add a scheduler, content index, model, disclosure grant, or canonical rewrite.
The programme owns the shared canary and managed release.

## Decisions

### Separate pending identity coverage from content hydration

Extend `PendingVisibilitySnapshot` with the complete validated custody-path footprint and its existing generation.
Read active metadata in one transaction, with receipt lineage checks.
Validate overflow metadata through indexed lineage lookups without loading every full receipt.
Keep the existing content hydration bound; overflow carries the footprint and no hydrated pages.
Use one partial covering index over active rows so retired history does not determine request cost.

`PendingOverlay` distinguishes exact, degraded, and unavailable coverage.
Its suppression methods use every custody path; its page methods expose only proven current content.
Keep uncertain paths distinct from proven tombstones.
After body hydration loses its fence, obtain one fresh footprint-only snapshot and serve degraded coverage.
Do not cache degraded results or claim current complete coverage.
Move opportunistic retirement off the request path; publication callbacks and the existing worker own it.

This reuses durable custody instead of adding a second pending index or querying membership separately in every retrieval lane.
The metadata cost grows with active pending identities; content reads remain bounded.

### Apply one suppression contract to every consumer

`find` accepts servable degraded coverage and reports it through the existing response envelope.
`FreshnessSnapshot` removes every custody path before adding proven pending pages.
Lexical, keyword, vector, graph, CLIP, browse, filter, and unit paths apply the same suppression before ranking and limits.
Reference projection removes covered persistent mappings before adding proven mappings.
Exact identifier resolution retains an explicit unavailable state when incomplete identity coverage prevents uniqueness proof.
Current access, policy, catalogue identity, and terminal byte checks remain mandatory.

Public diagnostics stay content-free and preserve restricted callers' existing suppression.
Raw pending paths remain internal; revealing those names would require their own release proof.

### Reuse bounded recovery without resetting healthy work

Extend the existing derived worker's due recovery selection to ready live custody that needs supersession proof.
Reuse exact per-path delegation when both recall lanes already hold current bytes.
Do not reactivate ordinary ready batches or reset their component backoff.
Ordinary after-state retirement still requires its existing component proof.
Advance retry eligibility after unsuccessful proofs as well as callback exceptions, so an unrecoverable prefix cannot starve later work.

Recover previously proven committed stranded batches through receipt-scoped current-byte convergence with bounded retry backoff.
Leave unproven mixed crash state under transactional reconciliation.
Replay exact deletions through the existing removal owner, including ordinary tombstone-only component work.
Cover mixed present-file and tombstone batches through that same component owner.
Preserve the worker's current component allowance and foreground priority.

Retire completed live custody through bounded targeted snapshots from publication callbacks and the existing worker.
This handles restart between component completion and its retirement callback, including overflow without hydrated batches.
Completed batches with live custody remain eligible even when no component has another scheduled callback.

### Preserve failures that represent unknown authority

Malformed custody, unreadable storage, unsafe paths, inconsistent lineage, and policy failures remain explicit unavailable outcomes.
Those states cannot provide a trustworthy suppression footprint.
Ordinary lag, an unreadable pending page, or a legitimate burst instead reduces known coverage.
The former protects data and authorization; the latter avoids making unrelated recall pay for delayed index work.

## Risks / Trade-offs

- Large active footprints use memory and metadata time. Check their query plan against substantial retired history and measure active backlog cost.
- An omitted pending page reduces answer completeness. Disclose the omission and keep partial answers out of shared caches.
- Missing reference identity can hide a duplicate. Preserve typed unavailability for exact identifier operations that need a uniqueness proof.
- Automatic replay can disturb retry order. Preserve backoff, fair bounded selection, exact claims, and existing fan-out ownership.
- A completed batch can retain custody after a crash. Retire it independently of new foreground requests or hydrated overlay state.

## Migration Plan

1. Install the additive active-row index through the existing deferred-state migration, with existing receipt identities preserved.
2. Deliver the reviewed source through ordinary CI and the managed release, with the previous compatible release available for rollback.
3. Verify real recall and coverage through the programme canary under natural concurrent traffic, with no synthetic production writes.
4. Preserve every failed or partial result until installed evidence supports closure.
