# Design

## Context

The existing projection stores due findings by category and page, with shared review fingerprints.
Its full served reader recomposes audience-dependent findings and caches the result.
Activation already returns guarded current material and applies final filtering in the shared dispatcher.
See proposal.md for the user-facing gap.

## Goals / Non-Goals

The change reuses those projection, release, and review rules with a small material association.
It adds no public argument, persisted target reference, index, classifier, service, or server mention ledger.

## Decisions

Select projection page buckets before audience recomposition and ordering.
Keep existing caller defaults and bypass the full served cache for bounded subset reads.
Artifact-role advice selects stored origins for those same paths before inspecting their evidence.
Missing projections return no advice, without activation scheduling recovery.
JSON decoding still reads the whole due projection; cold cost is measured as such.

Attach advice after the activation guard and outside the packet cache.
Reuse exact released identities from resolved anchors, served unit parents, and current-state sources.
The final dispatcher intersects that association with material that remains after filtering and scrubbing.
The hook emits a complete identity and handling instruction as one bounded line.

The active agent judges semantic relevance and remembers whether it raised any due item in the maintained client conversation.
Transport sessions, credential renewal, hook processes, and activation continuity tokens do not reset that memory.
Receiving a candidate records no mention.

Use existing operations for review, authored resolution, and presentation dismissal.
A due fingerprint binds a review finding; an authored unit fingerprint binds different state.
An applicable writer clears the finding through its existing delta; a dismissal needs one triage write.

## Risks / Trade-offs

Material intersection prevents unrelated advice; a wrong omission delays advice for the user.
The conversation cap limits interruptions; other due items wait for a later conversation.
Omitting unavailable projections prevents an activation scan; advice waits until existing maintenance restores the projection.
Existing fingerprint checks prevent stale decisions; a changed item costs the caller a context refresh.
These controls add no human release gate or mutation authority.

## Migration Plan

1. Run public activation and existing review workflows on isolated invented data, with projection and filtering checks passing.
2. Refresh generated artifacts through maintained producers, with producer checks passing.
3. Complete independent review and delivery checks, with the accepted commit ready for release.
4. Release through the existing process, with native client mention behavior checked after release.

The ordinary release rollback removes this optional carrier without changing stored authored data.
Native relevance judgment and conversation memory across compaction and OAuth renewal remain client acceptance checks.
