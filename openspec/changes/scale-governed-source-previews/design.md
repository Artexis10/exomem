## Context

`make-source-reclassification-reversible` ships paged background previews. It verifies receipt evidence once per closed part, and releases a simple governed referrer from private held evidence. Its measurements show the ungoverned load target met: 10,001 referrers over 79 pages at a 48 ms median. On the same fixture with an owner-only governed scope, the build never completes.

A profile of the governed build (frozen tree, 10,000 referrers) took 208.9 s of wall time, about 20.7 ms per `visible()` call. The profiler summed every thread, including a concurrent Records probe, so its cumulative times are larger than the wall time and show only where the cost sits:

- `reserved_paths._physical_leaf_candidates`: 161 s cumulative. `held_fs` `children()` enumerates the 10,000-entry folder for every candidate.
- `collection_store` ownership and preview reads: 371 s cumulative, enclosing most of the time above.
- The build holds no lock across candidates.

Task 1.1 measures the wall-time split per phase on one thread before 2.2 chooses a budget or progress model.

## Goals / Non-Goals

**Goals:**
- On Linux, a governed preview completes for 10,000 referrers in the two-folder fixture. Each folder has 10,001 entries. A concurrent Records read stays responsive. Measure the build and paging before and after on the same instrument.
- On Linux, release container members, extraction parts, and scene frames without rereading the referrer when their held evidence and dependencies are current.

**Non-Goals:**
- Changing what a governed caller may see, or any release rule.
- Native Windows reuse of change-time evidence, which keeps full verification.

## Decisions

- **Name resolution.** Reuse current listings within one governed build, keyed by each folder's held identity and change stamp. Bound the cache by its total name count. When a listing cannot fit, use the full lookup.
- **Build budget.** Give the build a ten-minute budget. The job stays cancellable and allows writers to proceed while it runs.
- **Snapshot check.** Keep the exact generation and authority checks during the build and when releasing a retained report.
- **Dependent release.** A held row keeps its own scopes and names what it inherits. The build checks this split against full membership. Parts and frames use the same physical parent path as ordinary membership. A report request reuses selector inputs only while the settled parent file version still matches. Parts and frames with a store-owned parent or a parent with its own inheritance use full verification. For a container member, release revalidates the parent binary and checks the witness on the member's payload. Bridge rows also keep full verification because release depends on resolving the bridge reference across the vault.

In the initial laptop runs, the frozen tree stopped at 50 s on each capped attempt and completed in 118 s without that cap. The scoped listing brought the build down to 28–29 s. Index and governance generations stayed stable during those runs.

Defer narrower invalidation until a measurement shows rebuilds caused by unrelated writes and a proof covers every report dependency. The candidate read set missed changes to container companion provenance and path-prefix siblings that can alter a rewrite.

## Risks / Trade-offs

- A cached listing could miss a new alias name. Mitigation: key the cache on the folder's change stamp, scope it to one build, and keep the exact-one-match rule.
- A longer build holds its job slot longer. Mitigation: keep one running preview job and deadline-bounded store access.
