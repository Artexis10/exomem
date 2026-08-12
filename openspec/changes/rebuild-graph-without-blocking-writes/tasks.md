## 1. Red-first canonical lifecycle and epoch coverage

- [x] 1.1 Add failing tests for the closed non-owned lifecycle
  `reserved → executing → canonically_committed → completed`, attempt-scoped
  compare-and-swap claims, dead-reserved reclaim, dead-executing
  outcome-unknown, fresh lowercase-24-hex commit tokens, and no new
  `graph_pending` writes.
- [x] 1.2 Add failing tests for an exact per-claim receipt binding namespaced
  key, command/payload digests, attempt/token, terminal projection, and exact
  checkpoint pair; require one atomic HMAC-SHA256 receipt v2 with a private
  32-byte local secret, matching-row/secret-only CAS healing, bounded reads,
  no scans, byte-exact replay, a versioned domain plus canonical unsigned JSON
  HMAC input, closed outer fields, duplicate/surplus rejection, and constant-time
  verification; include canonical-disposition enum tampering and both authenticated
  orphan cuts (`success` heal and `committed_failure` replay suppression).
- [x] 1.3 Add failing subprocess tests for every multi-store cut: crash before
  leaf, after caller files, after checkpoint, after receipt v2 installation,
  and during committed cleanup. Prove only a receipt with its matching trusted
  executing row and secret may heal local CAS; all other ambiguous cuts,
  including cleanup with lost trusted state, are fail-closed
  `outcome_unknown` and invoke the leaf at most once.
- [x] 1.4 Add failing strict-parser tests for <=1,000 path entries before normalization
  limit, closed v1 fields, already-NFC safe canonical POSIX paths,
  duplicate/conflict rules, hash/null rules, created subset rules, full-scope
  emptiness, and both unchanged public digest vectors.
- [x] 1.5 Add failing tests for exact legacy/pre-floor migration, valid-floor
  higher-full recovery, malformed/ambiguous fail-closed states, all exact ack
  metadata keys including `graph_sync_checkpoint`, and malformed-state fresh
  full recovery.

## 2. Receipted canonical publication and lifecycle safety

- [x] 2.1 Implement the non-owned lifecycle, attempt-scoped claim ownership,
  one atomic authenticated receipt v2, owner-only runtime secret/DB permissions,
  bounded binary DPAPI CurrentUser secret BLOB/DACL enforcement on Windows,
  matching-row/secret-only CAS healing, advisory-only v1/copied receipts,
  local-row-only legacy `graph_pending` migration, and stable
  outcome-unknown/readback without any leaf replay; implement the closed v2
  field schema, HMAC-covered `canonical_disposition`, versioned canonical HMAC
  verification, and both orphan outcomes exactly.
- [x] 2.2 Implement strict v1 checkpoint/floor parsing/rendering without
  changing valid public bytes/digest vectors; make floor/caller/checkpoint/ack
  admission and migration closed.
- [x] 2.3 Implement `floor → caller files → checkpoint → one authenticated
  receipt v2 → local CAS` for ordinary graph-relevant batches; keep
  graph-irrelevant batches epoch-free and make setup/recovery/repair/rollback
  explicitly `commit_point=False` without storing a receipt marker.
- [x] 2.4 Implement file, recursive-directory, and restore lifecycle ordering,
  caught rollback/fsync restoration, crash-after-rename higher-full recovery,
  and receipt exclusion for non-caller recovery/rollback.
- [x] 2.5 Expose only exact epoch/receipt outcome status to terminal, readiness,
  reconcile, and bounded coordination diagnostics without path/content leakage.

## 3. Exact graph handles and derived availability

- [x] 3.1 Add failing tests that every checkpoint leaving canonical authority
  has either one-transaction incremental acknowledgement, exact registered
  rebuild handle, or explicit durable failure handle; reject
  accepted-unverified handoff.
- [x] 3.2 Implement separate coordinator `ensure_started(checkpoint)` and
  bounded `join(handle)` APIs: start consumes no waiter capacity, joins always
  release capacity in `finally`, and start/capacity/stopped/lineage/platform
  failures produce explicit recoverable handles.
- [x] 3.3 Implement dynamic graph join/recompute from a
  `canonically_committed` receipt after process death, retry, readiness, or
  reconcile; never couple graph health to an attempt or replay the leaf.
- [x] 3.4 Implement exact incremental eligibility and one-transaction rows,
  availability markers, and complete acknowledgement metadata; proof failure
  registers full work without builder/join work under canonical authority.
- [x] 3.5 Add failing real-seam tests for original-index freshness changes
  before acknowledgement and immediately before replacement, then implement the
  private build/close/checkpoint/WAL-truncate/full-integrity/direct
  source-membership-topology proof and immutable ticket sequence. Under
  canonical authority test only two O(1)/bounded ticket checks around the
  publication hook and atomic replacement; cold/dirty caches, observed external
  Markdown/access edits, pending epochs, hook failure, or ticket mismatch
  release authority and reconcile/retry.
- [x] 3.6 Implement exact completed graph-available and committed-derived-failure
  terminals, durable recovery handles, and byte-exact completed replay while
  allowing later recovery to improve only live graph health.

## 4. Cooperative rebuild and recovery correctness

- [x] 4.1 Add failing POSIX tests for owner-controlled trusted runtime root,
  owner-only idempotency runtime directory/DB permissions, direct persistent
  no-follow lock, cross-process single builder, attempt death, scoped active-temp
  no-sweep, and hostile same-user root replacement being explicitly outside the
  contract.
- [x] 4.2 Implement cooperative POSIX single-flight and scoped cleanup under the
  rebuild lock; serialize all exact-revalidated reader-visible publication under
  canonical authority.
- [ ] 4.3 Add and pass native Windows tests for cross-process claim/release,
  no-delete-share live-reader replacement refusal, retained old/temp files,
  successful later retry after reader close, DPAPI attempt/token entropy,
  exact BLOB header/length/truncation/trailing/provider-version rejection,
  ciphertext-only SQLite/WAL/SHM, protected DACL/reparse rejection, profile/copy/
  raw-legacy outcome-unknown, and the real service loop.
  - Native installed-wheel DPAPI/DACL/reparse, cross-process, alias, and
    open-reader cases pass. The remaining real LocalSystem/NSSM loop is blocked:
    the available Windows identity is non-administrative and `OpenSCManager`
    refused disposable service creation with error 5. Exact evidence is retained
    in `.task/WINDOWS_SERVICE_RESULT.md`; no existing service was modified.
- [x] 4.4 Implement Windows-safe lock acquisition and no-delete-share
  replacement handling without weakening the POSIX trusted-root path or falling
  back to delete-and-replace.
- [x] 4.5 Implement startup/readiness/reconcile recovery from exact
  floor/checkpoint/ack/receipt classification, with an exact rebuild/failure
  handle before authority release and no user-authored-byte mutation; read only
  bounded exact artifacts named by trusted local rows and never scan or promote
  synced receipts.
- [x] 4.6 Implement the rollback compatibility gate: strict v1 parsing,
  NFC path admission, advisory-only legacy receipt reading, local legacy
  graph-pending migration, outcome-unknown protection, and recovery remain
  enabled while new scheduling may be disabled explicitly.

## 5. Product, performance, and independent verification

- [x] 5.1 Run the focused graph, epoch, lifecycle, mutation-terminal, writer
  lease, index-sync, deletion, reconcile, readiness, and Records-concurrency
  suites with embeddings/media extraction disabled.
- [x] 5.2 Run Ruff, targeted Mypy, strict OpenSpec validation, and
  `git diff --check` on the final corrected diff.
- [x] 5.3 Build the wheel and run the installed-wheel Linux product loop plus a
  disposable live MCP Records mutation that crosses canonical receipt, graph
  recovery, exact retry, and inspection end to end.
- [ ] 5.4 Run the native Windows installed-wheel service path with the exact
  product loop, cross-process rebuild, open-reader replacement, DPAPI/WAL/DACL
  receipt cases, and real service loop; retain command/output evidence rather
  than simulated-only proof.
  - The fresh installed-wheel interactive product loop and native component
    cases pass. Only the elevated real-service phase remains blocked by the
    `OpenSCManager` authorization boundary described in task 4.3.
- [x] 5.5 On a quiesced machine, rerun the 500- and 2,000-page reproduction;
  record canonical-boundary-hold p95 separately from request latency and prove
  the hold does not scale with vault size. Also test exact ticket fields and
  closed temp identity, no O(vault)/O(sidecar) work under canonical authority,
  bounded checks around the hook, warmed `RecallFreshnessCheckpoint`, bounded
  no-follow `_access.yaml` snapshot/fingerprint, cold/dirty retry,
  external-edit fail-closed behavior, and watcher-periodic convergence for
  missed edits; arbitrary-editor linearizability requires an out-of-scope
  broker/journal.
- [x] 5.6 Obtain a fresh independent review against this corrected architecture
  and rerun all invalidated gates before marking any implementation task done.

### Baseline to beat

Measured on main with an unusable sidecar: 7,727.8 ms at 500 pages and
32,381.5 ms at 2,000 pages for the first write, with the mutation boundary held
for the rebuild. CI observed 39,092 ms at 2,000 pages and 172,205 ms at 8,000.
The required improvement is boundary hold, not removal of the first caller's
graph-availability wait.

### Prior art

#346 removed full-rebuild escalation and violated the graph-available terminal
contract. This change retains that contract while moving derived work outside
canonical writer authority and making the cross-store outcome boundary explicit.
