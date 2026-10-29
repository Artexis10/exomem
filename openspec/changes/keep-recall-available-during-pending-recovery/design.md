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
Identity coverage for a pending page whose body is unproven comes from its canonical bytes, read once per lookup with the canonical scan's rule. Only unreadable pages, unknown custody, and more such pages than one hydration holds keep the unavailable state.
Current access, policy, catalogue identity, and terminal byte checks remain mandatory.

Public diagnostics stay content-free and preserve restricted callers' existing suppression.
Raw pending paths remain internal; revealing those names would require their own release proof.

### Retain one catalogue during bounded selection

The existing request binding retains checkpoints while each catalogue operation opens another connection.
An ordinary publication can therefore invalidate an admitted request before its next query.
Removing that comparison would mix eligibility, rows, and statistics from different catalogues.
Reuse one SQLite read transaction for catalogue selection, then materialize its results and close it before provider work.
This adds no persistent state, shared cache, registry, scheduler, or schema migration.

Prepare required query encodings and cold independent indexes before opening the transaction.
Preserve hot cache hits through a short lookup view before encoding.
On a miss, discard its request evidence, prepare encodings after closing, then select under a newly admitted view using the original arguments.
Give the discarded lookup private warnings and catalogue output; retain its actual cost in request timings.
Recheck the serving sidecar before using prepared vectors; a cutover keeps the existing degraded outcome and prevents caching.
Admit both scope checkpoints, schema, semantic identity, and policy identity from the same connection.
When the existing proof shows identical stored inputs, bind the current runtime checkpoint; local lineage alone does not make the catalogue stale.
Reuse existing query bodies for scope membership, filters, ordering, parent hints, lexical candidates, unit candidates, statistics, and selected page hashes.
Exact and lagging scopes both read their membership from that admitted view.
Do not apply a foreground delta through the retained connection.
Strict readiness uses independent connections and explicit current checkpoints.
After a lagging selection closes, request the existing background repair so stale admission still converges.
The explicit Python backend keeps its fallback; FTS5 absence alone does not disable normal catalogue tables.

Materialize page and unit selections before closing, including requested widening candidates and their ordering.
Apply widening exclusions after primary reranking without increasing its existing candidate depth.
Run reranking and later assembly after closing the transaction, using retained selections and passages.
Cache hits, exceptions, and cancellation close the view; nested recalls restore their caller's binding.
A copied execution context must not share a SQLite connection across threads.
No model load, query encoding, or reranking may retain that connection.

The catalogue view does not freeze canonical files, vector indexes, media, or graph state.
Retain pending suppression and request-owned proven pages after global custody retirement.
Compare canonical hydration with selected `pages.content_hash`, using the existing page snapshot hash.
Omit changed or unproved evidence and disclose incomplete coverage; never recover historical bytes from an unrelated current page.
Canonical omissions can reduce the pool for later ranking; they never justify reading a newer catalogue's rows or statistics.
Keep existing unit generations and fresh terminal authority and byte checks.
Independent chunk or media evidence gains no catalogue-generation proof from path membership alone.

Data publication alone may advance without invalidating the admitted view.
Current policy or semantic identity changes still invalidate its interpretation.
Disclose known projection lag and incomplete evidence through existing response fields, and keep partial results out of shared caches.
Preserve the existing decline for widening that was already lagging at admission.
Atomic replacement retains its WAL quiescence proof and may decline while a reader holds the old view.
Keep selection short because a retained WAL reader can delay replacement and a DELETE-mode reader can block incremental commits.

### Separate redaction evidence from current reference resolution

An optional citation lookup must not cancel material that passed recall admission.
Reuse the maintained identity source before pending withdrawal, with its current-snapshot recheck and whole-batch fallback.
Acquire that raw ownership once, then retain the existing pending adjustments for strict identifier resolution.
Keep raw ownership, original unprovable paths, and current mappings distinct in an ephemeral owner view.
This view adds no cache, index, sidecar query, or canonical write.

The redactor applies `pending_identity_candidates` before interpreting uncertain coverage.
Its existing minimum existence level preserves unresolved membership and possible widening grants.
The callback excludes only independently hidden uncertainty; every custody withdrawal remains intact.
For redaction only, combine proven current mappings with raw owners on unprovable custody paths.
Those owner hints establish no current mapping, uniqueness, content admission, or disclosure grant.
Keep proven moves, tombstones, and distinct-owner ambiguity under their existing rules.

When potentially visible identity remains unprovable under servable coverage, omit the optional stable-reference token uniformly.
Known, absent, and hidden-only identifiers share that uncertainty condition; do not choose omission from identifier membership.
Keep existing canonical-owner redaction when only pending custody changes, including a previously hidden owner withdrawn from current resolution.
Unknown authoritative custody remains unavailable; do not convert that failure into optional citation omission.
Preserve caller-supplied error-reference exemptions and every current content-release check.
Expose no protected identifiers, paths, counts, or per-reference diagnostic state.

Structured pointer origins use the same owner view through one internal optional lookup mode.
Keep strict lookup as the default for exact operations and every other caller.
Complete identity coverage still requires one currently visible owner; raw redaction hints cannot admit an origin.
When caller-filtered identity coverage is incomplete, omit whole stable-origin pointer entries and refund their existing prose charges.
Preserve admitted anchors, units, and independently governed targets.

Use the existing `missing` envelope with `role: pointers` and the technical reason `identity_incomplete`.
That reason reports incomplete optional origin coverage, never pointer existence or known withholding.
Check caller-filtered coverage even when a non-abstained packet has no stable-origin pointers, without an identity census for an empty batch.
The marker must not depend on identifier membership or removal counts.
Independently hidden uncertainty changes neither the marker nor the packet.
Preserve strict empty-batch behavior, current lexical evidence, and fresh terminal release checks.
Preserve the existing ungoverned packet fast path.

Conservative omission can remove a citation that would later prove valid.
That cost replaces loss of the admitted recall answer, while current identity resolution remains strict.
This repair preserves observed-checkpoint freshness and does not retain identity history absent from the maintained source.
It neither reconstructs an unreadable earlier identity nor treats receipt hashes as that identity.

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
- A retained reader can delay publication. Close it before provider work and preserve the publisher's existing safe decline and recovery.

## Migration Plan

1. Install the additive active-row index through the existing deferred-state migration, with existing receipt identities preserved.
2. Deliver the reviewed source through ordinary CI and the managed release, with the previous compatible release available for rollback.
3. Verify real recall and coverage through the programme canary under natural concurrent traffic, with no synthetic production writes.
4. Preserve every failed or partial result until installed evidence supports closure.
