## Context

A full lexical repair builds a detached catalogue and then replays the registry's delta onto it. Today the off-barrier replay proves every replayed page on disk. When a page changed after the replay's target, the replay waits for the newer event, applies it, and proves the whole delta again, up to 3 times. Under a sustained import, the import rewrites index pages on every call, so the proof never holds for a whole round.

## Decisions

### D1. Defer an observed rewrite; do not chase it

The published catalogue keeps this invariant for its checkpoint C: every row equals its registry state at C, except pages that the registry names after C. Those rows hold the state at C or a newer state.

- Readers that need a current catalogue serve only when the stored checkpoint equals the registry's checkpoint. A deferred page has an event after C, so those readers see a lagging catalogue and wait or disclose the lag.
- Readers of a lagging catalogue check each hit against the page on disk, so a newer row can only remove a stale hit.
- No update is lost. Every deferred page is in the delta from C, and every catch-up replays from C. When the registry loses that history, the delta is incomplete, and the result is a full pass, never a skipped page.
- The live write path already stamps a checkpoint from rows read off disk, so it already keeps this invariant.

The replay therefore waits once, under the existing bounds, for the registry to name each unproven page after the target. Then it records the original target. The next replay applies the newer events.

Two limits keep the invariant exact:
- A delta that needs a source proof defers nothing, because its target comes from a walk, not from observed events.
- Both scopes share a page's rows, so a page defers only when every scope that covers it names it after its target.

### D2. A catch-up records the checkpoint it fully applied

A catch-up round today records a checkpoint only when the registry stays still through the round. The round instead records its starting checkpoint R once every page of the delta from the stored checkpoint to R was applied after R. Newer events become a lag-only request, not a full repair. This changes how the live catalogue is stamped, so it ships as its own commit with its own review focus.

## Risks / Trade-offs

- **The barrier replay cap.** The census walk yields to imports, so the delta between the census and the barrier may exceed 32 pages. If the repro shows `delta_unavailable` at the barrier, the next fix is to drop the second source walk of a catch-up retry. Raising the cap is not the fix.
- **A very large deferred set** would mean that most of the catalogue is behind its checkpoint. The repro reports the deferred count. A count in the thousands reopens this design.
