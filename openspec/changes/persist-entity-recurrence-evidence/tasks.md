# Tasks

## 1. Evidence split
- [ ] 1.1 Split `entity_recurrence.collect` into per-page evidence, per-call anchor frames, and cross-page aggregation, with `collect` as their composition.
- [ ] 1.2 Extract one canonical status-class rule from `lifecycle_statuses.classify` for stored rows.

## 2. Store
- [ ] 2.1 Add the persisted evidence store with file-identity validation, metadata invalidation, fail-soft recovery, and privacy exclusions.
- [ ] 2.2 Route an audit that selects only `entity_recurrence` through the store, applying egress before aggregation.
- [ ] 2.3 Report walk, refresh, aggregate, and attention review-state spans with page counters.

## 3. Proof
- [ ] 3.1 Prove that store-path findings equal full-sweep findings for the owner and a restricted audience across edits, deletes, renames, Entity and alias changes, Source sharing, out-of-band rewrites, and cue edits.
- [ ] 3.2 Guard that a warm repeat sweep extracts no page again.
- [ ] 3.3 Compare the store path with the full sweep on a real vault, read-only, and record counts and timings.
