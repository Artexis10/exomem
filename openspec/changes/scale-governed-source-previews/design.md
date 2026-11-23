## Context

`make-source-reclassification-reversible` ships paged background previews. It verifies receipt evidence once per closed part, and releases a simple governed referrer from private held evidence. Its measurements show the ungoverned load target met: 10,001 referrers over 79 pages at a 48 ms median. On the same fixture with an owner-only governed scope, the build never completes.

A profile of the governed build (frozen tree, 10,000 referrers) shows about 20.7 ms per `visible()` call. The cumulative costs:

- `reserved_paths._physical_leaf_candidates`: about 161 s. `held_fs` `children()` enumerates the 10,000-entry folder for every candidate.
- `collection_store` ownership and preview reads: about 371 s, overlapping the time above.
- The build holds no lock across candidates.

## Goals / Non-Goals

**Goals:**
- A governed preview over 10,000 referrers in one flat folder completes, with a concurrent Records read staying responsive. Measure it before and after on the same instrument.
- No referrer, simple or dependent, is reread in full at the terminal gate when its proof is current.

**Non-Goals:**
- Changing what a governed caller may see, or any release rule.
- Native Windows reuse of change-time evidence, which keeps full verification.

## Decisions

- **Name resolution.** Memoize one folder listing for one build or request scope, keyed by the folder's held identity and change stamp. Never cache across scopes. A changed folder lists again.
- **Build budget and progress.** Decide between a separate bounded job budget and checkpointed progress from a measurement on the fixture. The job must stay cancellable and must not starve writers.
- **Snapshot check.** Bind the final check to the generations that can change the report. Prove that unrelated index or governance movement cannot discard a correct report, and that a relevant change still refuses it.
- **Dependent release.** Hold each dependency's own witness and identity, and require all of them to be current.

## Risks / Trade-offs

- A cached listing could miss a new alias name. Mitigation: key the cache on the folder's change stamp, scope it to one build, and keep the exact-one-match rule.
- A longer build holds its job slot longer. Mitigation: keep one running preview job and deadline-bounded store access.
