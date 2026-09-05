## Why

Dogfooding the UUID-to-readable structured-file migration against a real Obsidian vault exposed integrity failures that the synthetic acceptance fixture missed: governed renames can leave the collection audit chain unhealthy, Planning rebaseline is not recognised as a discontinuity, and a compatibility-Unicode filename can be previewed even though the held rename publishes a different physical spelling. A live migration must not proceed until preview, publication, rollback, and post-migration inspection agree on the same paths and audit history.

## What Changes

- Make structured-file apply publish a continuous, content-free audit trail for every moved or re-rendered item and advance the manifest head in the same atomic batch.
- Preserve existing acknowledged discontinuities across Planning and Records revision and representation maintenance.
- Recognise Planning rebaseline events everywhere Records rebaseline events are recognised.
- Normalize structured filename projections to the portable held-path spelling before collision detection and preview, so compatibility-equivalent Unicode paths cannot diverge between plan and publication.
- Add copied-vault regressions proving post-apply audit health, exact rollback, portable Unicode handling, and a second empty preview.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-collections`: Representation migration must extend the existing collection audit chain, preserve acknowledged gaps, use one portable filename spelling from preview through publication, and remain exactly reversible on failure.

## Impact

The change affects structured filename rendering, structured-files preview/apply, shared Planning/Records audit reconstruction, collection manifest/item audit markers, migration receipts, and focused lifecycle/migration tests. The public command shape remains compatible; previewed paths and receipt hashes become truthful for compatibility-Unicode inputs.
