# Proposal

## Why

A new conversation can reach the right topic yet miss a related unfinished intention or the user's applicable method.
Existing continuity and method contracts promise this behavior, but discovery and revision delivery do not consistently connect their carriers.

## What Changes

- Discover relevant notes, episodes, intentions, methods and collection descriptions through existing retrieval and graph primitives, independently of successful anchor resolution.
- Keep maps for orientation and profiles for depth; combine useful candidates under the existing packet budget and selective refresh workflow.
- Reconcile intentions with corrections and closure evidence before presenting them as ongoing work.
- Project user-authored methods from governed knowledge with identity, applicability, exclusions, source versions and attribution.
- Deliver revised or withdrawn methods at the next relevant activation across supported clients, without distributing another static skill package.
- Add a provider-neutral boundary for one optional, ephemeral context-fit judgment over admitted candidates. Prefer local execution; require explicit configuration for API placement.
- Keep that model path default-off and soft-failing. Compare it with agent interpretation before enabling any deployment.
- Preserve the distinction between retrieving a method and granting authority to act on it.

## Capabilities

### New Capabilities

- `context-interpretation`: bounded, attributable, ephemeral judgments of candidate relevance with replaceable local or API execution.

### Modified Capabilities

- `context-activation`: discovery across existing carriers, reconciliation of ongoing situations, and source-bound method projection within the shared budget.
- `conversation-aware-activation`: a narrow optional context-fit exception to the prohibition on model interpretation of bounded conversation input.
- `skill-loading`: portable method delivery and invalidation without reloading every product reference or relying on client-native memory.

## Impact

The implementation reuses `working_set`, `context_views`, the context catalogue, semantic units, retrieval lanes, Planning/Records and existing governed writers.
It does not create a second obligation store, workflow store, retrieval engine or execution scheduler.
Users and their authorized agents author methods; the product supplies their representation and delivery, not a fixed catalog of domain workflows.

This change implements the relevant continuity and method obligations in `close-memory-loop`, especially tasks 6.32 and 7.6.
It builds on `compile-context-maps-and-profiles` and `add-stance-block-and-served-context`; their unfinished work and evidence remain separately accounted for.
Earlier unsuccessful method-selection experiments establish no gain and do not justify restoring their implementation.

The optional interpreter has no canonical write or policy effect. Its output can propose relevance only; the active agent interprets evidence and authors permitted effects.
Existing sensed epistemic readings keep their separate ledger and non-ranking contract.
The new boundary requires explicit disclosure and resource configuration, independent review, real-model proof and an ordinary release.
