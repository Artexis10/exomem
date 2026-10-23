# Design

## Context

See `proposal.md` for the failure and scope.
The catalogue release adds a tenth derived component; the earlier application implements nine.
Earlier receipt activation converts every stored component through its enum, including rows in completed history.
The store retains that history, so draining current work does not remove the replay failure.

The catalogue release already supplies useful compatibility seams.
Its reader synthesizes absent added rows, exact preparation compares the original components, and acknowledgement excludes the catalogue.
The existing managed supervisor can replace a worker without replacing its launcher.

## Goals / Non-Goals

The goal is a separately versioned earlier application that safely shares receipt state with the catalogue release.
It must preserve canonical writes, exact replay and durable optional custody through rollback and return.
It adds no catalogue views, map activation, model work, live setting or launcher control protocol.
It does not declare the unchanged earlier binary compatible.

## Decisions

### Reuse the earlier application and explicit protocol compatibility

Build the compatibility artifact from the earlier application with the receipt changes needed by this contract.
Reuse the capable reader's absent-row and exact-preparation rules.
Recognize only the documented catalogue extension; do not accept arbitrary unknown components.
This is a closed storage protocol value, not a content vocabulary or a meaning classifier.

Retain one definition of readable and executable components within the artifact.
The reader can understand catalogue custody while the writer and scheduler require and execute only the original components.
Earlier-application writes create no catalogue obligation.
Keep old nine-row batches readable; do not require a synthetic stored row for an obligation they never incurred.
Insert only the original nine component rows when the earlier application prepares a new batch.
Synthesize the absent catalogue row only in the reader; retain the earlier schema's nine-name constraint.

### Preserve optional work rather than pretending to execute it

Exclude catalogue work from the earlier scheduler's claims, due counts and dispatch.
Preserve its rows, revisions, attempts and unexpired claims.
Exclude it from recovery-failure attempt increments and backoff updates as well as normal work selection.
Normal content proof and supersession transitions remain authoritative.
A parent batch must not become irrecoverably terminal merely because its supported components finished while catalogue work remains owed.

Apply the explicit catalogue exclusion at acknowledgement and pending-visibility boundaries.
Supported projection proof remains necessary; optional custody must not shadow otherwise admitted recall.
Diagnostics report deferred optional custody independently from executable backlog.

Reuse the existing bounded retirement retry for a ready parent whose supported components already prove visibility.
Keep the parent ready while optional work remains owed; do not mark it completed to trigger retirement.
This closes the crash window after the final supported component commits but before its visibility callback runs.
The same proof and retirement rules must work after restart without a callback from the lost process.

### Keep the existing managed handoff

A reader that preserves pending optional work removes the need to prove a globally empty queue during cutover.
Reuse the maintained standby, ingress drain, worker stop and promotion sequence.
Do not add a launcher hook, stop window, manual SQL conversion or out-of-process drain.

An older receipt snapshot is not a safe substitute.
Restoring it can lose post-snapshot custody or disagree with canonical state.
The compatibility artifact keeps current state and does not erase history.

### Qualify a distinct official artifact

Keep the feature release and maintenance artifact as separate delivery batches because they serve different rollback roles.
The maintenance artifact preserves the earlier application's surface and does not include catalogue views or map activation.
Select maintenance version `0.134.6`, subject to a final collision check before publication.
The tag and PyPI version were absent during preparation; this does not reserve either name.
Create `maintenance/0.134` at `v0.134.5` (`cac93b792a1ee68ce510e74f9e782e2ba3de70dc`).
Open the compatibility implementation PR against that maintenance branch, with its own linked task worktree.
The current post-feature planning worktree is not the maintenance application's source.
Carry this contract into the maintenance branch without importing the catalogue feature.

Run the existing full CI workflow at the exact reviewed maintenance head before publishing.
Use the existing tag-dispatch publisher after creating the reviewed maintenance tag and a GitHub release with `make_latest=false`.
Do not run the main-branch release-please planner for the maintenance release.
Keep the feature release as GitHub's latest release so the producer leaves its floating image tags unchanged.
The [official registry comparator](https://github.com/modelcontextprotocol/registry/blob/main/internal/service/versioning.go) orders semantic versions by their version values.
The maintenance version therefore sorts below the feature version.
Verify the registry's actual latest entry before and after publication; preserve the feature release's discovery position.
Preserve source identity and original producer receipts.
Do not retarget an already qualified artifact or replace its bytes.

The source implementation touches the reader, scheduler, writer-required components, projection dispatch and pending visibility.
Inspect the schema-open and insertion paths before changing them; retain narrow old schemas when optional rows need not be written.
Do not import the catalogue implementation into the maintenance artifact.

## Risks / Trade-offs

- Misclassifying required work as optional could hide missing projections. Support only the explicit protocol extension and preserve other errors.
- Marking a parent completed too early could strand catalogue work. Prove forward recovery after restart with pending optional rows.
- A fake completion could discard custody. Assert preserved rows and attempt counters while the earlier application runs.
- A source-only exercise could miss packaging or handoff failures. Qualify installed wheels through the existing supervisor with invented state.
- A maintenance version can disturb discovery tags. Review the existing publisher's version and floating-tag rules before publication.

## Migration Plan

1. Qualify the compatibility source and installed maintenance artifact; retain its exact identity and producer receipt.
2. Qualify the capable release against that artifact; preserve canonical state and all receipt history through the journey.
3. Select a live operation only after ordinary review, artifact and workload admission gates pass; keep service authority unchanged.

The proof plan uses one invented store and the two installed artifacts.
It covers completed historical replay, delayed and claimed optional work, ordinary earlier-application writes and forward recovery.
Include recovery failure without optional attempt changes, and restart after a lost visibility-retirement callback.
Include a new earlier-application write against the narrow original schema without adding a tenth stored row.
Unit checks first isolate the enum failure and scheduler exclusions; the installed journey proves their integration.
Use existing receipt and handoff fixtures, with no private vault copy or model work.
