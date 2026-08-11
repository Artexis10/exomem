## 1. Red-first protocol coverage

- [ ] 1.1 Add failing checkpoint/floor tests for exact closed shapes, strict 24-hex mutation identity, monotonic non-reuse, safe sorted path/deletion projection, full-scope overflow, domain-separated canonical digest vectors, semantic exclusion, and caught batch rollback.
- [ ] 1.2 Add failing writer tests proving narrow and wide operation guards release before graph join and a second canonical batch commits while the first caller waits.
- [ ] 1.3 Add failing single-flight tests proving one builder, checkpoint N+1 invalidation/retry, bounded waiter coverage, and no stale waiter success.
- [ ] 1.4 Add failing publication tests proving readers see old/available or unavailable, never temp/partial state; final boundary work is only checkpoint/freshness verification plus swap.
- [ ] 1.5 Add failing crash/restart tests for committed-unacknowledged checkpoint recovery, the commit-before-`graph_pending` uncertain cut, valid-floor malformed-checkpoint recovery, missing/corrupt-floor refusal, accepted pre-floor seeding, descriptor-locked abandoned-temp sweep, and exact current acknowledgement.
- [ ] 1.6 Add failing terminal/idempotency tests for durable nonterminal `graph_pending`, concurrent retry waiting, dead-owner graph-only resume, successful off-boundary join, projected committed graph failure, exact replay without duplicate canonical write, and later recovery.

## 2. Durable canonical-to-graph handoff

- [ ] 2.1 Implement the version 1 graph-sync checkpoint and internal monotonic floor parsers/renderers plus domain-separated digest helpers with strict validation and bounded path/full projection.
- [ ] 2.2 Include ordered floor/canonical/checkpoint replacement in graph-relevant guarded batches; include floor/checkpoint in recoverable file/directory trash transitions; exclude both from semantic/index candidates, public paths, and recursive input.
- [ ] 2.3 Expose coherent epoch status to graph availability, reconcile, readiness diagnostics, and bounded coordination status without path/content leakage.

## 3. Off-boundary single-flight rebuild

- [ ] 3.1 Thread the exact committed checkpoint through incremental fanout; acknowledge an eligible immediate successor in one SQLite transaction and register off-boundary full work without building under writer authority when proof fails.
- [ ] 3.2 Persist durable `graph_pending` after canonical commit while each narrow/wide operation guard is still held, then release every guard before graph work; implement graph-only dead-owner resume, committed-uncertain handling for the cross-store crash cut, and completed success/failure persistence before public replay.
- [ ] 3.3 Run one cross-process per-vault builder under a descriptor-validated persistent OS lock in shared runtime state keyed by canonical vault identity, with unique temporary databases, bounded checkpoint-aware waiters, and safe abandoned-temp sweep under the same lock.
- [ ] 3.4 Build each full pass privately, close/checkpoint/validate it, and under one bounded final hold perform the two epoch/freshness checks around acknowledgement plus atomic replacement.
- [ ] 3.5 Resolve waiters only from proven checkpoint lineage; terminalize same-generation conflicts, stopped uncovered flights, capacity/start failures, stabilization exhaustion, and platform sharing refusal as explicit committed derived failures.

## 4. Recovery and compatibility

- [ ] 4.1 Detect incoherent floor/checkpoint/ack state at startup/readiness/reconcile; recover above a valid floor, seed the floor only for accepted pre-floor checkpoint states, and fail closed on missing/corrupt-floor ambiguity without changing user-authored canonical bytes or reusing a generation.
- [ ] 4.2 Name temporary rebuild databases with a reserved per-vault prefix/nonce and sweep only while holding the cross-process rebuild lock.
- [ ] 4.3 Verify sidecar replacement with open readers on POSIX and the Windows service path.
- [ ] 4.4 Preserve exact legacy no-epoch graphs until first publication; provide a rollback compatibility build retaining floor/checkpoint parsing, graph-pending replay safety, and recovery.

## 5. Verification

- [ ] 5.1 Run epistemic graph freshness, mutation-boundary, writer lease/idempotency, index-sync, deletion/reconcile, and Records concurrency suites with embeddings disabled.
- [ ] 5.2 Run Ruff, targeted Mypy, strict OpenSpec validation, and `git diff --check`.
- [ ] 5.3 Re-run the reproduction at 500 and 2,000 pages and record boundary-hold p95 separately from request latency; the hold must not scale with vault size.
- [ ] 5.4 Run the installed-wheel product loop and lean suite.

### Baseline to beat

Measured on main with an unusable sidecar: 7,727.8 ms at 500 pages and 32,381.5 ms at 2,000 pages for the first write, with the vault mutation boundary held for the rebuild. CI observed 39,092 ms at 2,000 pages and 172,205 ms at 8,000. The required improvement is boundary hold, not removal of the first caller's graph wait.

### Prior art

#346 removed the full-rebuild escalation and failed the contract that a write against an unusable sidecar leaves the graph available. This change retains that terminal contract while moving the wait outside canonical writer authority.
