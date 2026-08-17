## Why

Eight pull requests and seven `fix(graph):` commits have not made the epistemic
graph converge under ordinary use. Live vaults show consecutive writes ending in
`graph_sync: failed`, several generations dying inside an hour, and the graph
benchmark failing specifically at "1200 pages with a concurrent writer". The
failures are not a sequence of unrelated defects. They are three design decisions
that each make the other two worse:

1. **Repair is all-or-nothing and O(entire vault).** The incremental refresh path
   has sixteen distinct bail-out sites, and every one of them routes to a rebuild
   that deletes every node and edge and re-walks every markdown file under the
   knowledge base. There is no partial repair. The fast path is a conjunction of
   roughly sixteen global proofs; its only alternative is the most expensive
   operation in the system.

2. **That whole-vault rebuild is guarded by a vault-global optimistic-concurrency
   check.** The rebuild samples a projection identity derived from a full walk of
   every recall-admitted file, runs the pass, then samples again and refuses if
   the key moved. Any unrelated concurrent write moves it. The retry budget is a
   stabilization count times a publication count, so a single dispatch can run up
   to eight whole-vault passes — each of which strictly *widens* the window in
   which a concurrent writer can invalidate it.

   This is the reason the subsystem has been hard to fix: **success probability
   falls as vault size and write rate rise.** It is a livelock by construction, and
   no amount of retry tuning inverts that gradient.

3. **Availability is one vault-global boolean, and the write synchronously waits
   for it.** A single equality decides whether the *entire* graph is readable, one
   write anywhere makes the whole graph unavailable, and the only route back is
   (1) — which the write then blocks on, because the terminal contract promises
   `graph_sync: "completed"`.

Each prior fix was individually correct and addressed a symptom *inside* this
triangle, which is why none of them helped.

**The codebase already contains the working alternative, twice.** `deferred_index`
is a durable per-path dirty queue with receipts, generation triggers, and poison
isolation, drained asynchronously; the embedding and semantic indexes use it, and
those are precisely the subsystems that keep working while the graph does not. The
inbound-link index (`live-index-freshness`) already specifies incremental patching
with a full-rebuild equivalence contract and a fall-back-to-rebuild clause. The
graph is the outlier, not the pattern.

And the changed-path set the graph needs is **already recorded durably**: the graph
sync checkpoint carries `paths` and `created_paths`, written into the canonical
batch before the caller's files. The system already knows exactly which pages
changed, per generation, crash-safely — and then discards that information and
rebuilds everything.

## What Changes

- **Take the graph off the interactive write path.** An interactive canonical write
  SHALL report an already-settled graph outcome and SHALL NOT wait for one that is
  not. The join becomes a check, not a bounded wait: there is no timeout constant,
  because no constant can be both shorter than the write-latency budget and longer
  than a vault-sized rebuild. `reconcile` and explicit maintenance keep the
  unbounded join, where waiting is the point.
- **Add a fourth terminal graph outcome, `pending`.** Absent / `completed` /
  `failed` cannot express "the graph is converging and you did not wait for it". A
  write that did not wait MUST be distinguishable, through the compact projection
  as well as the full terminal, from one whose graph was already current.
- **Enqueue the changed paths durably instead of discarding them.** The checkpoint's
  `paths` and `created_paths` are written into a `graph_upserts` dirty queue in the
  same durable step as the checkpoint itself, so no crash cut can lose the dirty set
  while keeping the markdown. A full-scope batch enqueues a rebuild marker instead.
- **Make the sixteen bail-out sites proportional.** They stop meaning "rebuild the
  whole vault" and start meaning "enqueue the affected paths and return deferred".
  The true whole-vault rebuild survives only where it is genuinely required: no
  sidecar, a schema or registry version change, a full-scope batch, a lineage reset,
  and explicit reconcile.
- **Drain the queue through the existing per-path primitives**, publishing through
  the existing incremental marker. No `DELETE FROM graph_nodes`. Existing drain call
  sites in the watcher, the CLI, and the server entry point need no new scheduler.
- **Let a rebuild and a canonical write coexist.** Removing the write's join lets
  them overlap for the first time, and they conflict: the canonical directory census
  counts the rebuild's own scratch sidecars, and on Windows a rebuild's page reads
  pin the files a concurrent replace needs. Derived-index residue is excluded from
  the census by the same mechanism already applied to the batch writer's own
  workspace; rebuild reads use share-all-modes opens; a refused replace retries
  against a re-checked precondition; and epoch sampling happens under the canonical
  mutation hold so it never observes a batch's interior.

## Capabilities

### New Capabilities

- `graph-incremental-convergence`: proportional graph repair through a durable
  per-path dirty queue, a non-blocking interactive write, a `pending` terminal
  outcome, and safe concurrency between a graph rebuild and a canonical write.

### Modified Capabilities

- `live-index-freshness`: the graph joins the inbound-link index in being
  event-maintained and incrementally patched, with the same full-rebuild
  equivalence obligation and the same fall-back-to-rebuild clause.

## Impact

Affected areas are graph refresh and rebuild dispatch, the deferred-index schema,
the graph sync checkpoint write, mutation terminal projection, the canonical
directory census, and vault artifact replacement. This changes a *response*
contract (a new `graph_sync` value), not a tool schema: no MCP selector, no vault
markdown schema, no graph query shape, and no pinned plugin digest moves. The
crash-safety protocol — receipts, epochs, lineage resets, publication tickets — is
unchanged; this change reduces how often it is *exercised*, not what it does.

Deployment is staged, because the phases have independent value and independent
risk. Phase 1 (non-blocking write) is correct on its own and unblocks the write
latency gate; Phase 2 (dirty queue) is what actually makes the graph converge;
Phase 3 (relaxing the availability fence) is explicitly conditional on measurement
and MUST NOT be done speculatively.

The whole-vault rebuild is not removed. It is demoted from the routine consequence
of an ordinary write to what it should always have been: a repair tool.
