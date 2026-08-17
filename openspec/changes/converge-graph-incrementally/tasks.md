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

- [ ] 4.1 Add failing tests for a `graph_upserts` queue: enqueue is durable in the same
  step as the checkpoint write; a crash cut after markdown and before any drain still
  leaves the paths queued; a full-scope batch enqueues a rebuild marker rather than an
  unbounded path list; a repeatedly-failing path is rotated into isolation without
  stalling the rest.
- [ ] 4.2 Add failing tests for full-rebuild equivalence: the same change sequence
  applied through queued drains and through a full rebuild yields identical nodes, edges,
  and parent references.
- [ ] 4.3 Add failing tests for monotonicity: a write landing during a drain is enqueued
  and repaired by the next drain, and the in-progress drain's completed work is not
  discarded.
- [ ] 4.4 Add a failing test that an ordinary incremental bail-out performs no
  whole-vault walk, and that each genuinely-required case (no sidecar, schema or registry
  version change, full scope, lineage reset, explicit reconcile) still does.

## 5. Phase 2 — implementation

- [ ] 5.1 Add the `graph_upserts` table to the deferred index, reusing the existing
  generic add/snapshot/receipt/rotate machinery and generation triggers verbatim. No
  parallel store.
- [ ] 5.2 Enqueue the checkpoint's changed and created paths at the durable checkpoint
  seam, inside the same canonical batch step as the checkpoint write.
- [ ] 5.3 Convert the incremental refresh path's bail-out sites from whole-vault rebuild
  to enqueue-and-defer, using the affected set the function already computes — its delta
  paths plus the resolver-affected sources it folds in.
- [ ] 5.4 Reserve the whole-vault rebuild for the cases that require it, and confirm by
  test that each of those cases still reaches it.
- [ ] 5.5 Add a graph branch to the deferred drain that re-indexes queued paths through
  the existing per-path primitive and publishes through the existing incremental
  availability marker. No table deletion. Follow the module's existing
  optimistic-batch-then-isolate shape.
- [ ] 5.6 Confirm the existing drain call sites — watcher, CLI, server entry point —
  cover the graph branch; add no new scheduler.
- [ ] 5.7 Extend the rebuild-hold repro script to measure per-path drain latency
  alongside whole-rebuild latency, so the improvement is measured rather than asserted.

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
