## 1. Phase 1 — take the graph off the interactive write path

- [x] 1.1 Replace the interactive graph join at both writer-lease sites with a
  check-only join that reports an already-settled outcome and returns immediately
  otherwise. Delete the interactive join timeout constant rather than retuning it;
  record in the seam's docstring why no such constant can exist.
- [x] 1.2 Keep the unbounded join for `reconcile` and reconcile-mode maintenance.
- [x] 1.3 Add the fourth terminal outcome `graph_sync: "pending"` with a stable
  reason code, and widen the compact terminal projection so it survives — a response
  whose graph is converging must not be byte-identical to one whose graph is current.
- [x] 1.4 Give the permitted-outcome set exactly one definition and have the live
  acceptance script import it, so a consumer outside CI cannot drift from it.
- [x] 1.5 Add `await_active_rebuild` as the explicit convergence seam for callers and
  tests that genuinely need the graph current.
- [x] 1.6 Port the join-site enumeration test: every caller that joins a registered
  graph flight is check-only or a declared convergence opt-out, and the suite fails if a
  third unbounded site appears.
- [x] 1.7 Retarget the tests that relied on the write's implicit join to join explicitly.
  Do not weaken any assertion to accept `completed` or `pending` interchangeably.

## 2. Phase 1b — let a rebuild and a canonical write coexist

Uncovered by 1.1: removing the join let a write and a rebuild overlap for the first
time. Reachable today on every path that already rebuilds off the write path.

- [x] 2.1 Exclude derived-index residue from the canonical directory census, using the
  same mechanism already applied to the batch writer's own workspace residue. Keep the
  graph sync and floor artifacts censused — the canonical batch writes those itself.
- [x] 2.2 Give rebuild page reads a non-pinning open on platforms whose default open
  mode denies concurrent deletion or replacement; an ordinary read elsewhere.
- [x] 2.3 Retry a replacement refused by a transient sharing violation for a bounded
  interval, re-evaluating the precondition on each attempt rather than replacing against
  a stale one.
- [x] 2.4 Stop a rebuild classifying a batch's interior as an incoherent lineage.
  Coalesce through the canonical boundary only to *re-read* a sample that came back
  torn, never on every attempt: acquiring the boundary is what waits the batch out,
  and holding it unconditionally charges each attempt a lock acquisition whose
  holder-metadata write is observable to anything counting replacements.
- [x] 2.5 Drain active graph rebuilds at test teardown, failing rather than leaking one.
  Keep it a quiesce, not a convergence helper.
- [x] 2.6 Drain in-flight graph rebuilds before a command-line process exits, on
  every exit path including the early returns. A rebuild is a daemon thread: right
  for the long-lived server, wrong for a one-shot invocation that would otherwise
  take it down and leave `pending` permanently true. Bound the drain and report a
  rebuild it had to abandon.
- [x] 2.7 Make the product E2E wait for convergence rather than assume the write
  performed it, and keep that a real gate rather than a tolerance.
- [x] 2.8 Apply 2.2 and 2.3 to review state as well. A reader that pins the file and a
  replace with no tolerance for a transient sharing refusal is the same conflict as the
  graph sidecar's, and it escaped the triage command as an unhandled `PermissionError`,
  reaching the client as a bare `500` with no JSON body. The defect predates this change;
  taking the rebuild off the write path moved when reads and writes overlap, and it began
  failing about three product-E2E runs in four on Windows while remaining invisible on
  Linux, whose sharing semantics cannot produce it.
- [x] 2.9 Have the E2E retain its scratch root on request. Everything a failure can be
  diagnosed from — server log, vault, isolated home — lived only there and was deleted on
  the way out, including on failure, so an intermittent defect had to be chased by
  re-running until it reproduced rather than by reading the traceback it had already
  written down. Report a response body that is not JSON, too, rather than only the decode
  error.
- [x] 2.10 Join explicitly at the assertion sites 1.7 missed. A site that asserts *which
  boundary* an off-boundary rebuild used, rather than asserting graph state, still relies
  on the rebuild winning a race against the daemon thread it runs on — it passed every
  local run and lost on a loaded CI shard.

## 3. Phase 1 verification

- [x] 3.1 `semantic write latency (2k and 8k)` green, `commit_median_ms < 750` and
  `commit_p95_ms < 1500` at both sizes. This is the gate the bounded-join attempt could
  not pass.
- [x] 3.2 Full suite diffed against an `origin/main` baseline worktree on the same box:
  no new failures.
- [x] 3.3 Confirm the live-acceptance vocabulary accepts `pending` and still rejects
  an unknown value. The script itself needs a deployed endpoint and deployment digests,
  so it stays a deploy-time gate; its validator was exercised directly.
- [x] 3.4 Grep every consumer of the graph sync seam, the join helper, the off-boundary
  rebuild entry point, and the terminal `graph_sync` field. This found a `vault.py`
  comment claiming a test bound its copied artifact names back to `graph_sync` when no
  such test existed; writing it is what surfaced that the non-pinning read and the
  replacement retry are a pair.

## 4. Phase 2 — red-first durable dirty queue

- [x] 4.1 Add failing tests for a `graph_upserts` queue: enqueue is durable in the same
  step as the checkpoint write; a crash cut after markdown and before any drain still
  leaves the paths queued; a full-scope batch enqueues a rebuild marker rather than an
  unbounded path list; a repeatedly-failing path is rotated into isolation without
  stalling the rest.
- [x] 4.2 Add failing tests for full-rebuild equivalence: the same change sequence
  applied through queued drains and through a full rebuild yields identical nodes, edges,
  and parent references. This is the test that earned its keep: it found the forward
  reference (a link written before its target exists produces no edge at all, and
  indexing the target later cannot repair the source).
- [x] 4.3 Add failing tests for monotonicity: a write landing during a drain is enqueued
  and repaired by the next drain, and the in-progress drain's completed work is not
  discarded.
- [x] 4.4 Add a failing test that an ordinary incremental bail-out performs no
  whole-vault walk, and that each genuinely-required case (no sidecar, schema or registry
  version change, full scope, lineage reset, explicit reconcile) still does. The
  defer/rebuild split is a declared table parsed back out of the module by `ast`, so a
  renamed, added or removed bail-out reason fails the suite rather than picking a side
  silently.

## 5. Phase 2 — implementation

- [x] 5.1 Add the `graph_upserts` table to the deferred index, reusing the existing
  generic add/snapshot/receipt/rotate machinery and generation triggers verbatim. No
  parallel store. Rotation now sorts a poisoned receipt strictly behind the queue's
  maximum rather than re-stamping it with a coarse wall clock, which on Windows could
  leave it first and pin exactly the work rotation exists to unpin.
- [x] 5.2 Enqueue the checkpoint's changed and created paths at the durable checkpoint
  seam, inside the same canonical batch step as the checkpoint write — specifically
  *before* the markdown replace, because over-enqueueing costs a no-op re-index and
  under-enqueueing costs a whole-vault rebuild.
- [x] 5.3 Convert the incremental refresh path's bail-out sites from whole-vault rebuild
  to enqueue-and-defer, using the affected set the function already computes — its delta
  paths plus the resolver-affected sources it folds in.
- [x] 5.4 Reserve the whole-vault rebuild for the cases that require it, and confirm by
  test that each of those cases still reaches it.
- [x] 5.5 Add a graph branch to the deferred drain that re-indexes queued paths through
  the existing per-path primitive and publishes through the existing incremental
  availability marker. No table deletion. Follow the module's existing
  optimistic-batch-then-isolate shape. A drain that changes topology also repairs the
  pages whose edges change as a result; an ordinary edit pays nothing for that.
- [x] 5.6 Confirm the existing drain call sites — watcher, CLI, server entry point —
  cover the graph branch; add no new scheduler.
- [x] 5.7 Extend the rebuild-hold repro script to measure per-path drain latency
  alongside whole-rebuild latency, so the improvement is measured rather than asserted.
- [x] 5.8 Have a drain acknowledge the committed generation it converged. Repairing the
  pages is only half of convergence: until the graph sync acknowledgement moves, the
  epoch stays stale, the sidecar stays unavailable, and the next dispatch takes the
  whole-vault rebuild anyway — leaving the queue as overhead beside the expensive path
  rather than a replacement for it. Acknowledge only on coverage of the checkpoint's
  whole path set, since a truncated batch, an older generation's leftovers, and a
  full-scope marker each prove nothing. Coverage is membership in the processed batch
  rather than the indexed set, or every generation containing a deletion stalls forever.
- [x] 5.9 Exclude the queue database from the canonical directory census. Enqueueing
  before the commit is what makes a crash cut safe, and it also puts a derived SQLite
  file's creation inside the guarded window — so the first write to a fresh vault
  invalidated its own census and failed as `STALE_RECORD`, with nothing concurrent
  involved at all. Bind the excluded name to its definition, as 2.1's names are bound.
- [x] 5.11 Bound the enqueue on both sides, not just the early one. Opening the queue
  database creates the knowledge-base directory when it is absent, and a batch creating
  that directory has already captured it as a *missing parent* it will create itself, in
  order — so enqueueing before the guards take custody fails the batch's own recheck and
  surfaces as `STALE_SEMANTIC_WRITE`, a write invalidated by its own bookkeeping. Enqueue
  after the guards create those parents and still strictly before any canonical byte is
  replaced; both bounds are load-bearing.
- [x] 5.12 Tolerate a store that predates this queue. Every deployed vault already has a
  deferred-index database with no graph table, and the readers run before any writer
  through a read-only connection where the table cannot be created. Absent reads as
  empty; the next writable open migrates the store.
- [x] 5.13 Publish a drain's acknowledgement in the transaction that writes the rows it
  describes, through the same before-commit seam the incremental refresh path uses, after
  re-proving the projection did not move under the pass. Writing it afterwards through a
  second connection tears the two apart, and an acknowledgement landing against a moved
  projection is what the lineage check refuses — reported at the *next* write rather than
  at the drain that caused it. A refused proof rolls the pass back, clears no receipt, and
  leaves the work queued.
- [x] 5.14 Keep each reported queue count describing the queue its neighbour describes.
  The drain's return counts every queue it serves, so a surface reporting one queue's
  refresh must measure that queue rather than the aggregate, or the pair it is reported
  beside stops adding up for a reason no reader of that response can see.
- [ ] 5.10 Stop the dispatch layer re-scheduling the whole-vault rebuild that 5.3
  removed. A defer-classified bail-out returns `deferred`, but the layer above reads an
  unregistered, unacknowledged checkpoint as a missing rebuild and registers one — so
  the expensive path still runs on exactly the bail-outs this change exists to make
  proportional, now with a queue beside it rather than instead of it. The queue owns the
  repair, so the write's terminal must say `pending` (1.3) rather than report a
  registration. `register_deferred` is not the seam: its handle carries a
  scheduling-disabled error, not "queued for drain". Red-first, and do not let an
  unregistered checkpoint fall through to `completed` — a write whose graph has not
  converged must never be byte-identical to one whose graph is current.

## 6. Phase 2 verification

- [ ] 6.1 The 1200-page-with-concurrent-writer lane reaches `completed`, not `pending`,
  with no write blocking. Record before and after.
- [ ] 6.2 Graph drift stays at zero across a sustained concurrent-write run.
- [ ] 6.3 Full suite green; latency gate still green.
- [ ] 6.4 Live acceptance run explicitly; consumer grep repeated for the seams this
  phase touched.

## 7. Phase 3 — availability fence, conditional on measurement

Do not start this phase speculatively. It is gated on 7.1 answering yes.

- [ ] 7.1 After Phase 2, re-measure whether the vault-global content-freshness equality
  in the read snapshot is still the binding constraint on availability. If drains are
  cheap and the incremental publication proof usually succeeds, availability recovers on
  its own and the rest of this phase is unnecessary — record the measurement and stop.
- [ ] 7.2 If still binding: replace only the content-freshness term with a check against
  the durable dirty set, readable cross-process so a cold reader can evaluate it. Leave
  the recall-policy-version and access-fingerprint terms fail-closed and all-or-nothing —
  those are access safety, not content staleness.
- [ ] 7.3 Report residual lag as a reported dimension rather than a fail-closed one.
- [ ] 7.4 Reduce the stabilization, publication, and supersession retry budgets now that
  whole-vault rebuilds are rare, and retire the classification and refusal-memo machinery
  that exists only to make repeated doomed rebuilds survivable.
- [ ] 7.5 Confirm the pinned surface digests did not move: this change alters a response
  contract, not a tool schema. Confirm rather than assume.
