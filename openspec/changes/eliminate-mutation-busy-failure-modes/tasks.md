## 1. R1 — Telemetry + dynamic retry_after_ms

- [x] 1.1 `mutation_lock.py::hold()` emits `acquired`/`released` events with
      `wait_ms`/`hold_ms` (the long-hold WARNING fires unconditionally, not
      only when probed).
- [x] 1.2 `_mutation_busy()` computes
      `retry_after_ms = min(15000, max(750, age_seconds*500))`, `>=5000` when
      overdue; mirrored in `epistemic_graph.py` for free — the graph lane's
      `_mutation_coordinator` is the same `VaultMutationCoordinator`/
      `_mutation_busy()` code path, no separate implementation needed.
      `writer_lease.py`'s hint assembly changes to `setdefault` so a computed
      hint survives.
- [x] 1.3 Existing `retry_after_ms == 750` assertions in
      `tests/test_writer_lease.py` audited: both are legitimately unchanged
      (an age-~0 contention fixture, and the separately-static
      `MUTATION_WARMING` code path) — no update needed.
- [x] 1.4 Red-first tests for the retry-hint formula:
      `tests/test_mutation_lock.py` (dynamic_retry_after_ms, wait_ms in
      details, acquired/released events, guaranteed long-hold warning).

## 2. R2 — GAP C: age-aware orphan snapshot

- [x] 2.1 When the metadata mutex can't be taken within 250ms, read the
      sidecar lock-free (atomic `os.replace` publish makes it tear-free);
      return real `age_seconds`/`overdue` with `verified: false`.
- [x] 2.2 Fabricate an unknown holder only when no sidecar exists at all.
- [x] 2.3 Preserve the content-free/bounded holder pins
      (`tests/test_mutation_lock.py:274-571`) — audited: none of the pinned
      tests actually contend the metadata mutex past the 250ms status
      timeout, so all resolve via the pre-existing paths unchanged.
- [x] 2.4 Red-first tests for the age-aware snapshot path:
      `test_orphan_snapshot_reports_real_age_when_metadata_mutex_is_contended`,
      `test_orphan_snapshot_reports_real_overdue_state`,
      `test_orphan_snapshot_falls_back_to_unknown_holder_without_a_sidecar`.

## 3. R3 — GAP D: reads never take the boundary

- [x] 3.1 Delete the hosted-read guard branch (`writer_lease.py`) and
      `_read_bypasses_consistency_guard`.
- [x] 3.2 No escape-hatch env var.
- [x] 3.3 Red-first tests: a hosted read-only operation does not wait behind
      a long write (`test_hosted_reads_never_contact_the_coordinator_or_wait_for_the_boundary`,
      `test_hosted_plain_read_bypasses_boundary_held_by_other_manager` — the
      latter mirrors `test_hosted_public_audit_routes_bypass_boundary_held_by_other_manager`
      for an ordinary, non-audit read); the two prior tests asserting the
      superseded wait-for-boundary behavior were rewritten in place.

## 4. R4 — GAP B: abandoned idempotency receipts

- [x] 4.1 Add an `owner` column (`pid:process-nonce`) via a guarded
      `ALTER TABLE`.
- [x] 4.2 Liveness = exclusive OS lock file per process under
      `<state_dir>/idempotency-owners/`; a probe error is treated as alive
      (fail closed).
- [x] 4.3 A dead-owner `pending` row becomes `abandoned`; the client gets
      `OpError("MUTATION_OUTCOME_UNKNOWN", ..., details={status: "uncertain",
      committed: None})` (HTTP 409 via `cli_ops._CONFLICT_CODES`); an
      identical retry executes fresh after 60s.
- [x] 4.4 Legacy `NULL`-owner rows honored under the old rule for up to 600s.
- [x] 4.5 `coordination_status` gains a content-free `idempotency`
      {pending, abandoned, oldest_pending_age_seconds}; added the doctor
      `_check_idempotency_store`.
- [x] 4.6 Confirmed same-process pins stay green unchanged:
      `test_explicit_idempotency_blocks_orphaned_pending_after_process_abort`
      (owner alive — same process), and
      `test_identical_pending_retry_waits_outside_mutation_boundary_and_replays`
      (`test_command_surface_retry.py`).
- [x] 4.7 Red-first tests: injected liveness/clock for abandonment
      (`test_pending_row_with_dead_owner_becomes_abandoned`), legacy-row
      grace period
      (`test_identical_orphaned_legacy_pending_becomes_abandoned_after_grace_period`,
      `test_identical_recently_orphaned_legacy_pending_still_reports_acknowledgement_pending`),
      fresh retry after the pause
      (`test_identical_retry_executes_fresh_after_abandonment_grace_period`).
      The prior test asserting the superseded "orphaned pending never
      resolves" behavior was rewritten to assert the new contract.

## 5. R5 — GAP A: writer-lease idle release (lands last, on the instrumented base)

- [x] 5.1 `LeaseConfig.idle_release_seconds` = 60,
      `EXOMEM_WRITER_LEASE_IDLE_SECONDS` (`0` = off; reject `0 < idle < ttl`).
      Default on; preferred replicas exempt.
- [x] 5.2 Track `_active_mutations` and `_last_activity_monotonic` at the
      single choke point `writer_authority_guard()`.
- [x] 5.3 In `_renew_loop`, under `self._lock`: idle + `count == 0` clears the
      token and calls `client.release(token)` while holding the lock; release
      RPC failure is swallowed (token already cleared locally).
- [x] 5.4 Red-first tests (FakeClient + injected clock, `SQLiteLeaseStore` for
      multi-replica scenarios): idle fires at exactly the threshold, activity
      resets the idle clock (T+59 defers to T+119), an in-flight mutation
      blocks release until it completes, a mid-release race gets a fresh
      bumped token with no `WRITER_FENCED` for that fresh grant, a straggler
      commit is fenced at `validate_active_write_fence`, a preferred replica
      never releases, `idle=0` never releases, a release RPC failure is
      swallowed (token still cleared locally), config validation, and a
      two-manager handover completes within `idle + ttl/3`.

## 6. R6 — Lease CLI

- [x] 6.1 `exomem lease status|release [--yes] [--json]` in `__main__.py`
      (ops-only; not an MCP/REST product command).
- [x] 6.2 `release` works cross-device via a small
      `release_holder(holder_replica_id, fencing_token)` generalization of
      `LeaseCoordinatorClient.release()`.
- [x] 6.3 Deliberately excluded: `steal`/`force-acquire` (release + preferred
      reclaim already hands over within roughly one TTL using existing
      fencing proof) — asserted via argparse's own invalid-choice rejection.
- [x] 6.4 Red-first tests: `tests/test_lease_coordinator.py` (coordinator's
      `/release` endpoint, via `httpx.ASGITransport` against the real
      `create_app()`, accepts release-on-behalf-of and no-ops when
      unheld/mismatched — proving no coordinator change was needed);
      `tests/test_lease_cli.py` (CLI status/--yes gate/--json/exit codes via
      a FakeClient injected in place of `LeaseCoordinatorClient`, including
      the unauthorized-coordinator -> clean `WRITER_COORDINATOR_UNAVAILABLE`
      path).

## 7. Verification

- [x] 7.1 Pinned suites stay green: `test_writer_lease.py` (150 passed —
      fencing, reclaim, receipt fail-closed), `test_mutation_lock.py` (28
      passed — content-free/bounded holder).
- [ ] 7.2 `uv run ruff check src tests` clean on changed files — ruff is not
      installed in this environment; the orchestrator runs it out-of-band.
- [ ] 7.3 Linux CI: full suite, fcntl/multi-process kill tests, latency +
      golden gates (authoritative; this box cannot run the full suite).
- [ ] 7.4 Live smoke after merge+deploy: `exomem lease status` shows both
      replicas; induce a busy (long write + concurrent write) and see it in
      the mutation journal and counters (depends on `add-observability-layer`
      landing first).
