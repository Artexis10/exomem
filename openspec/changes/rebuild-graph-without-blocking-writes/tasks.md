## 1. Red-first protocol coverage

- [ ] 1.1 Add failing checkpoint tests for exact closed shape, monotonic generation, safe sorted path/deletion projection, full-scope overflow, domain-separated canonical digest vector, semantic exclusion, and caught batch rollback.
- [ ] 1.2 Add failing writer tests proving narrow and wide operation guards release before graph join and a second canonical batch commits while the first caller waits.
- [ ] 1.3 Add failing single-flight tests proving one builder, checkpoint N+1 invalidation/retry, bounded waiter coverage, and no stale waiter success.
- [ ] 1.4 Add failing publication tests proving readers see old/available or unavailable, never temp/partial state; final boundary work is only checkpoint/freshness verification plus swap.
- [ ] 1.5 Add failing crash/restart tests for committed-unacknowledged checkpoint recovery, malformed checkpoint refusal, abandoned-temp sweep, and exact current acknowledgement.
- [ ] 1.6 Add failing terminal/idempotency tests for successful off-boundary join, committed graph failure, exact replay without duplicate canonical write, and later recovery.

## 2. Durable canonical-to-graph handoff

- [ ] 2.1 Implement the version 1 graph-sync checkpoint parser/renderer and domain-separated digest helper with bounded path/full projection.
- [ ] 2.2 Include the checkpoint replacement in graph-relevant guarded canonical batches and exclude it from semantic/index candidates and recursive input.
- [ ] 2.3 Expose current checkpoint status to graph availability, reconcile, readiness diagnostics, and bounded coordination status without path/content leakage.

## 3. Off-boundary single-flight rebuild

- [ ] 3.1 Change unusable-graph dispatch to register a per-invocation required checkpoint and start/join one per-vault flight without blocking under writer authority.
- [ ] 3.2 Move graph joining to writer coordination after every narrow or wide operation guard releases and before terminal/idempotency finalization.
- [ ] 3.3 Build each pass into a reserved temporary database, snapshot checkpoint plus full freshness, and retry when either changes.
- [ ] 3.4 Under one bounded final hold, recheck inputs, write consumed checkpoint metadata, and atomically replace the live sidecar.
- [ ] 3.5 Resolve waiters only from covering checkpoint lineage; cap registrations and return explicit committed derived failure on exhausted stabilization.

## 4. Recovery and compatibility

- [ ] 4.1 Detect a current unacknowledged/malformed checkpoint at startup/readiness/reconcile and force whole-vault recovery without changing canonical bytes.
- [ ] 4.2 Name temporary rebuild databases with a reserved per-vault prefix and sweep only proven abandoned paths.
- [ ] 4.3 Verify sidecar replacement with open readers on POSIX and the Windows service path.
- [ ] 4.4 Preserve legacy no-checkpoint graphs until first checkpoint publication; provide a rollback compatibility build that retains checkpoint parsing/recovery.

## 5. Verification

- [ ] 5.1 Run epistemic graph freshness, mutation-boundary, writer lease/idempotency, index-sync, reconcile, and Records concurrency suites with embeddings disabled.
- [ ] 5.2 Run Ruff, targeted Mypy, strict OpenSpec validation, and `git diff --check`.
- [ ] 5.3 Re-run the reproduction at 500 and 2,000 pages and record boundary-hold p95 separately from request latency; the hold must not scale with vault size.
- [ ] 5.4 Run the installed-wheel product loop and lean suite.

### Baseline to beat

Measured on main with an unusable sidecar: 7,727.8 ms at 500 pages and 32,381.5 ms at 2,000 pages for the first write, with the vault mutation boundary held for the rebuild. CI observed 39,092 ms at 2,000 pages and 172,205 ms at 8,000. The required improvement is boundary hold, not removal of the first caller's graph wait.

### Prior art

#346 removed the full-rebuild escalation and failed the contract that a write against an unusable sidecar leaves the graph available. This change retains that terminal contract while moving the wait outside canonical writer authority.
