# Tasks

TDD order throughout: pure-logic unit tests before wiring. Torch/model paths stay behind the
existing soft-fail seams; the lean suite runs with `EXOMEM_DISABLE_EMBEDDINGS=1`.

## 1. Establish the failing baseline

- [x] 1.1 Test: `clear_deferred_work(include_full=True)` is reachable from at least one
      production call path. Assert by call-path, not by calling the function directly —
      the current defect is that no caller passes it.
- [x] 1.2 Test: a queue holding entries for already-current files reports zero outstanding
      work after a drain, without re-embedding.
- [x] 1.3 Test: with a seeded backlog and no operator command, repeated reconciliation
      passes strictly decrease the queue count.
- [x] 1.4 Test: in `quiet` policy, per-pass admission for deferred work is non-zero.
- [x] 1.5 Confirm whether `file_watcher.py`'s quiet branch actually defers downstream
      despite leaving `defer_semantic = False`. Write the test that pins whichever
      behaviour is correct; if the log disagrees with reality, that is in scope here.

## 2. Make both queues reachable

- [x] 2.1 Give the full-upsert queue a drain path mirroring `drain_deferred_work`.
- [x] 2.2 Pass `include_full=True` from `_index_main` so `exomem index` clears both queues.
- [x] 2.3 Audit `deferred_index.add` / `add_full` call sites against their clear paths and
      note any remaining asymmetry in the PR body.

## 3. Reconcile entries against index state

- [x] 3.1 Resolve each queued entry against the freshness check the indexer trusts.
- [x] 3.2 Retire satisfied entries without embedding; perform genuine work before retiring.
- [x] 3.3 Test both directions explicitly (satisfied → retired, genuine → performed first).

## 4. Own the drain on the reconcile pass

- [x] 4.1 Add the drain as a step of the reconcile pass, spending the pass's remaining
      budget after drift admission.
- [x] 4.2 Bound admission by the active mode policy; no independent write path, no new
      thread.
- [x] 4.3 Test: a mutation committed while entries are queued does not block on the drain.

## 5. Quiet becomes a throttle

- [x] 5.1 Replace `_QUIET_EXPENSIVE_INDEX_CAP = 0` with a non-zero bounded admission.
- [x] 5.2 Test convergence: a corpus-scale backlog reaches zero in a bounded number of
      passes under quiet policy.
- [x] 5.3 Confirm no steady-state GPU residency is introduced in `quiet` or `normal`.

## 6. Honest reporting

- [x] 6.1 `status`: `next_action` names only a real automatic action or a runnable command.
- [x] 6.2 `doctor`: warn when a queue exceeds a meaningful fraction of indexed pages.
- [x] 6.3 Test: a seeded corpus-scale backlog produces a doctor warning and the vault is not
      reported as unqualifiedly healthy.

## 7. Mode persistence fails loudly

- [x] 7.1 Catch `PermissionError` / `OSError` around the persist in `_mode_main`.
- [x] 7.2 Remove the temporary file on failure; exit non-zero.
- [x] 7.3 Emit one line naming the config path and the remediation — no bare traceback.
- [x] 7.4 Read the mode back from the persisted file rather than echoing the request.
- [x] 7.5 Test all four: exit code, no residue, message content, read-back.

## 8. Windows runtime DACL migration

- [x] 8.1 Add the `windows-runtime-security` delta spec before implementation.
- [x] 8.2 Keep pre-existing runtime state fail-closed while making validation errors name
      the exact offending path and an exact `icacls` remediation command.
- [x] 8.3 Make `doctor` surface the same actionable DACL failure.
- [x] 8.4 Document the upgrade remediation and the principal-private boundary: LocalSystem
      and a user-run CLI require separate runtime state directories.
- [x] 8.5 Test a pre-existing directory with a non-conforming DACL through a production
      validation path; it must never raise a bare, pathless `RuntimeError`.

## 9. Verification

- [ ] 9.1 `ruff check`
- [ ] 9.2 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q` green
- [x] 9.3 `openspec validate fix-deferred-work-drain --strict`
- [x] 9.4 Record before/after queue counts from a seeded backlog in the PR body.

## 10. Bound production backlog repair

- [x] 10.1 Add a failing watcher regression proving performance-mode deferred repair uses
      the smaller live-batch cap rather than the 500-file reconcile cap.
- [x] 10.2 Add failing full and semantic drain regressions proving a bounded incomplete
      batch isolates only a small fixed prefix while retaining every other receipt.
- [x] 10.3 Implement the background drain cap without changing real-drift admission or
      explicit unbounded operator-drain semantics.
- [x] 10.4 Bound per-receipt failure isolation for bounded drains and preserve fair rotation.
- [ ] 10.5 Run focused watcher/deferred tests, Ruff, strict OpenSpec validation, privacy
      validation, and the lean suite before delivery.
- [x] 10.6 Add red regressions for zero-cap progress and one-slot cross-queue fairness, then
      reserve one background convergence slot and alternate it durably.
- [x] 10.7 Prove an explicit unbounded operator drain still isolates more than the bounded
      four-receipt prefix for both full and semantic work.
