# Design — converge the epistemic graph incrementally

## The failure this change is aimed at

The graph does not converge under concurrent writes, and the reason is structural
rather than incidental. It is worth stating precisely, because seven previous fixes
were each correct and none of them helped.

Let a rebuild pass over a vault of `N` pages take time `T(N)`, and let writes arrive
at rate `λ`. The rebuild samples a vault-global projection identity before the pass
and again after, and refuses if it moved. Any write to any recall-admitted page moves
it. So a pass succeeds only if no write lands during it, with probability roughly
`e^(−λ·T(N))`.

The retry budget does not rescue this, because each retry is another full pass with
the same exposure. The dispatch succeeds with probability `1 − (1 − e^(−λ·T(N)))^k`
for a budget of `k` passes — but `k` is a small constant (a stabilization count times
a publication count) while `T(N)` grows with the vault. **The failure probability
rises with both vault size and write rate, and the retry budget is the only term that
does not scale.** That is a livelock by construction.

It also explains each observed symptom without any additional hypothesis: writes fail
in consecutive runs (a busy period defeats every pass in the budget); generations die
in bursts (each dispatch burns its whole budget quickly); and the benchmark fails
specifically at "1200 pages with a concurrent writer" and not at 1200 pages alone
(large `T(N)` *and* nonzero `λ` are both required).

The three decisions reinforce each other. Because repair is all-or-nothing (1), `T(N)`
is as large as it can possibly be, which minimises the success probability in (2).
Because availability is a single vault-global boolean (3), the fast path must prove a
vault-global property to stay on it — which is why it has sixteen bail-out sites, which
is what makes (1) fire constantly. Fixing any one of the three in isolation leaves the
other two generating the same failures, which is the pattern the previous seven fixes
show.

## What a working design looks like

`basic-memory`, a sibling project over the same substrate (markdown files, SQLite
index, wikilink graph), was examined deliberately as a cross-check rather than a
source. Its graph converges, and it makes three choices that map exactly onto the
three problems above:

| Problem here | basic-memory's choice |
|---|---|
| All-or-nothing O(vault) repair | Per-file checksums; only changed files are re-indexed |
| Global optimistic-concurrency guard | None — indexing is monotone per file, so there is nothing to invalidate |
| Global availability boolean | None — the index is always readable; unresolved links are stored as forward references and resolved later |

The important observation is not that basic-memory is right and exomem is wrong. It
is that **exomem already has all three primitives** and does not use them for the
graph:

- Per-file content hashing: `graph_nodes.source_hash` already exists and is already
  written per node.
- Forward references: `_placeholder_node` already exists — an edge to a page that has
  not been indexed yet is already representable.
- A durable per-path work queue with receipts, generation triggers, and poison
  isolation: `deferred_index`, already in production for the embedding and semantic
  indexes, drained by `index_sync.drain_deferred_work` from call sites that already
  exist in the watcher, the CLI, and the server entry point.

And the dirty set itself is already computed and already durable: the graph sync
checkpoint carries `paths` and `created_paths` and is written into the canonical batch
before the caller's files land. The information required to repair proportionally is
written to disk, crash-safely, on every single write — and then thrown away.

So this is not a rewrite. It is connecting three existing mechanisms to a fourth that
was built without them.

The in-repo precedent is even closer than the cross-project one. `live-index-freshness`
already specifies the inbound-link index as event-maintained and incrementally patched,
with two obligations that this change adopts verbatim in shape: the patched index's
*content* must be identical to what a full rebuild would produce for the same vault
state, and when the incremental registry is not live the system falls back to a full
rebuild. The graph is the outlier among exomem's derived indexes, not the pattern.

## Why the phases are ordered this way, and why Phase 3 is conditional

**Phase 1 — off the write path.** Independently correct and independently valuable: it
removes the graph term from write latency entirely, and it is a prerequisite for Phase 2
being observable (while the write blocks on the rebuild, "deferred" has no meaning).

The design decision inside Phase 1 is that the interactive join is **removed, not
bounded**. A bound must be simultaneously shorter than the commit latency budget and
longer than a small-vault rebuild, or a dozen tests that legitimately expect `completed`
begin reporting `pending`. That constant does not exist. The earlier bounded-join attempt
was not badly tuned; it was over-constrained, and the constraint is the design saying the
wait does not belong there. A check-only join has a provably zero latency contribution
rather than one bounded by a constant nobody can size.

`reconcile` and explicit maintenance keep the unbounded join. Waiting is the entire point
of those operations, and their latency budget is a user's patience, not a write gate.

**Phase 2 — the dirty queue.** This is the phase that fixes convergence. Two properties
matter, and they are different properties:

- *Proportionality*: work per drain is `O(changed)`, not `O(N)`. This shrinks `T` by
  orders of magnitude for ordinary writes, which raises `e^(−λ·T)` toward 1.
- *Monotonicity*: this is the more important one. A concurrent write **appends an entry**
  to the queue instead of **invalidating a global proof**. Proportionality improves the
  odds; monotonicity removes the failure mode. Even if a drain raced with a write, the
  raced path is still queued and the next drain picks it up. Nothing is lost, so nothing
  needs to be retried from scratch.

Phase 1 alone makes writes fast and leaves the graph drifting, which is why Phase 2 is
not optional. Shipping Phase 1 without Phase 2 would trade a visible failure for an
invisible one.

**Phase 3 — the availability fence, only if measurement demands it.** After Phase 2, the
global availability equality may simply stop being the binding constraint: if drains are
cheap and the incremental publication proof usually succeeds, availability recovers on
its own. Relaxing a fail-closed fence speculatively, to fix a problem that measurement
has not confirmed still exists, is how a safety property gets traded away for nothing.

If it *is* still binding, the change is narrow and well-founded rather than a
loosening. The read snapshot already evaluates the recall policy version and the access
fingerprint as **independent terms**; those are a genuine access-safety fence and stay
all-or-nothing, because serving a page a reader may not see is a correctness violation.
The content-freshness equality is a *separate* term that has been given the same
fail-closed grade. Applying an access-safety-grade fence to content staleness is the
category error under the whole design: stale content is a quality dimension to report,
not a permission to deny. Only that term is in scope, and only after measurement.

Phase 3 is also where the machinery that exists solely to make repeated doomed
whole-vault rebuilds survivable — the stabilization and publication retry budgets, the
Class B/C classification, the refusal memos — can be reduced, because by then the
rebuilds it protects will be rare.

## The concurrency hazard Phase 1 exposes

This was not predicted and is the most important operational finding in the change.

Removing the write's join lets a canonical write and a graph rebuild run concurrently
**for the first time**. They conflict — on POSIX as well as Windows, so it was never a
platform quirk. The write blocking on its own rebuild is what has been hiding it, and
every production path that *already* rebuilds off the write path (the watcher's
background rebuild, deferred drains) can hit it today, before this change.

Four distinct collisions, each fixed at its own layer rather than papered over with a
retry at the top:

1. **The canonical directory census counts the rebuild's own scratch sidecars.** A
   rebuild creates, replaces, and removes `.graph-rebuild-<digest>.sqlite` and its
   companions inside the same vault directory a canonical write takes a guarded census
   of, so an in-flight rebuild turns an unrelated write into a path-guard change and then
   a stale-record refusal. Derived-index residue is excluded from the census by the same
   mechanism already applied to the batch writer's own workspace residue. The graph sync
   artifacts stay censused — the canonical batch writes those itself, so they are exactly
   the files the guard is for.

2. **Windows page reads pin the pages they read.** Python's `open()` omits
   `FILE_SHARE_DELETE`, so a concurrent replace of a page the rebuild happens to be
   reading is refused. Rebuild reads go through a helper that opens with all three share
   flags on Windows and is an ordinary read everywhere else. This is a read that must not
   take custody of what it reads — the graph is a *derived* projection, and a derived
   reader has no business blocking the canonical writer.

3. **A refused replace is retried against a re-checked precondition.** Sharing violations
   are transient by nature. The retry re-runs its own precondition check each attempt
   rather than replacing against a stale one, so the bound is on how long the transient
   is tolerated, not on how stale a proof may be.

4. **Epoch sampling could observe a batch's interior.** The canonical batch installs the
   generation floor before the checkpoint; a rebuild sampling between the two sees a floor
   without its checkpoint and refuses with a lineage conflict. Sampling now happens under
   the canonical mutation hold, so it observes one side of a batch or the other and never
   the middle.

## Test-suite consequence, stated as a design fact

A rebuild is now a daemon thread that outlives the request that started it — which in the
suite means it outlives the *test* that started it, and then touches process-global
projections while the next test is already running against a different vault.

That is an artefact of the suite sharing one process across many vaults; production is one
long-lived process against one vault and has no such shape. The write's join was providing
per-test isolation by accident, and that accident is what is being removed.

The fixture that drains active rebuilds at teardown is therefore deliberately **not** a
convergence helper. It guarantees only that nothing is still running when a test ends. A
test that needs the graph to be *current* asserts that for itself by joining the flight
explicitly. Weakening the affected assertions to accept `completed` or `pending` was
rejected: that would hide exactly the regression this change is meant to make visible.

## Alternatives considered

**Tune the join bound.** This was the previous attempt. Rejected: the constant is
over-constrained by two hard requirements in opposite directions, so no value satisfies
both. Attempting it again would be the eighth fix inside the triangle.

**Raise the retry budget.** Rejected on the arithmetic above: `k` is the only term that
does not scale with the problem, so increasing it buys a constant factor against a
failure probability that grows with vault size and write rate. It also makes the bad
case worse — more doomed whole-vault passes, more time spent invalidating other writers.

**Make the rebuild incremental without the durable queue** — recompute the dirty set from
the current checkpoint at drain time. Rejected: it loses the dirty set across a crash cut
between the markdown write and the drain, which is exactly the window the existing
receipt/epoch protocol was built to make honest. Enqueue must be part of the same durable
step as the checkpoint write, or the queue is a cache with a correctness claim.

**Write a new per-path store for the graph.** Rejected: `deferred_index` already has the
schema shape, receipts, generation triggers, and `rotate_receipts` poison isolation, and
it is already drained from the call sites the graph needs. A parallel store would be a
second thing to keep crash-safe and a second thing to drift.

**Remove the global availability fence now, as part of Phase 2.** Rejected as
speculative — see Phase 3 above. The fence may be irrelevant once drains are cheap, and
a fail-closed safety property should not be relaxed to fix a problem that may no longer
exist.

## Out of scope

Rewriting the graph sync receipt, epoch, and lineage-reset protocol. It is crash-safety
machinery, it is correct, and it stays. This change reduces how often it is exercised,
not what it does.
