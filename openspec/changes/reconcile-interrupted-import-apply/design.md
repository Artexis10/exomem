# Design

## Context

See `proposal.md` for the failure and outcome. Import chunks commit before the outer writer records a complete terminal.
Current item reconciliation sits below the unknown-mutation refusal. Generic canonical resume completes derived work and cannot resume an incomplete import.
Canonical batches roll back ordinary exceptions, but abrupt interruption can expose only some destinations.

## Goals / Non-Goals

Recovery retains the original mutation identity, plan, invocation scope, Sources and original bytes. Status remains usable for a large run.
This change grants no customer access, changes no live runtime and performs no semantic indexing acceptance.

## Decisions

### Reconcile under the existing identity

Only the canonical import apply command can reclaim a proven abandoned unknown row into a fresh owned attempt.
The store checks the exact command digest and retains vault, principal and verified-client namespaces.
An atomic claim and normal writer guard serialize reconciliation. Completed terminals still replay, and unrelated unknown mutations remain blocked.
Partial Source custody never becomes a complete signed mutation receipt. Canonical resume retains its current meaning.

### Freeze each invocation before publication

The canonical run records each invocation's namespaced identity digest, command digest, plan, resolved scope and incoming continuation before the first rename or publication.
Recovery reuses that checkpoint before recalculating failed subsets or changing cursor state.
Unresolved checkpoints remain keyed by invocation identity across later calls until a durable terminal settles them.
Issued continuations retain their scope. A later invocation cannot widen an earlier invocation's recovery.
Legacy full-plan and explicit-path calls can reconstruct their scope from exact arguments.
A current open legacy continuation retains canonical scope: every fresh apply supersedes it before changing that scope.
A completed cursor's saved apply reply can replay without using current scope. A superseded cursor's empty reply proves neither completion nor scope.
A legacy failed-subset or continuation call without this evidence remains unresolved with a specific diagnostic.
The service never invents its earlier subset or suggests a replacement mutation key.
Legacy complete chunks can reconcile through persisted outcomes and exact provenance. Legacy partial publication lacks frozen post-images and remains unresolved where existing evidence cannot establish ownership.
An upgrade test starts from checkpoint-free state rather than rerunning the entire workflow under the new implementation.

### Reconcile interrupted chunk publication

Before publication, the canonical run records the active chunk and exact expected Source and artifact identities.
Recovery validates existing components against those identities and validates original bytes before creating a missing component.
It never overwrites a conflicting Source or artifact. It reconstructs affected index entries from canonical Sources and deduplicates the chunk log entry by structured operation identity.
Another invocation can process disjoint items while an interrupted chunk retains custody. An overlapping invocation must reconcile that chunk's original identity first.
Only after custody and required canonical bookkeeping exist does recovery settle outcomes and staged copies.
Existing note identity checks reconcile committed draft notes; frozen invocation scope keeps owed notes eligible after an interruption.

### Bound the public status view

The internal full status view remains available to the browser. Public status returns summary metadata, aggregate counts, plan identity and continuation by default.
Explicit inventory, selection, plan and outcome views return bounded pages with offset, limit, total and next offset.
A run revision binds pages to one snapshot. A changed revision returns a read-only stale-page diagnostic instead of silently skipping rows.
Default status and detail pages omit content and unbounded lists. Page byte limits and explicit omitted-row diagnostics handle unusually large metadata rows.
The existing browser explicitly requests `view="full"`; that compatibility view retains the previous complete document and is exempt from bounded-response guarantees.
The shared command registry generates the same contract for MCP, REST, CLI and packaged plugins.

## Risks / Trade-offs

- Reclaiming an active mutation could duplicate writes. Only a terminal unknown or proven-dead attempt can enter reconciliation.
- Recomputing a failed subset could lose an owed note. A durable invocation checkpoint preserves its resolved scope.
- A partial batch can resemble an unrelated occupied target. Exact frozen post-images permit only owned completion; conflicts remain visible.
- Older subset calls may lack scope proof. Recovery reports that limitation and preserves all files rather than widening the request.
- Summary status changes public defaults. Versioned schemas and guidance ship together; the full browser path remains unchanged.
- Checkpoints add canonical writes. They replace unsafe replay inference; unresolved identities remain available until their durable terminals settle.

## Migration Plan

1. Ship one independently reviewed source batch through the existing release owner; published artifacts contain the repair.
2. Verify the exact released bytes in an isolated synthetic crash and large-status workflow; custody and counts must agree.
3. Publish version-specific same-run guidance with any legacy scope limitation; customer rollout remains a separate authority boundary.
4. Roll back through the ordinary release procedure if needed; preserve additive checkpoints, run state and staged files.
