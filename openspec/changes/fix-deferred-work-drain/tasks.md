# Tasks

TDD order throughout: pure-logic unit tests before wiring. Torch/model paths stay behind the
existing soft-fail seams; the lean suite runs with `EXOMEM_DISABLE_EMBEDDINGS=1`.

## 1. Establish the failing baseline

- [ ] 1.1 Test: `clear_deferred_work(include_full=True)` is reachable from at least one
      production call path. Assert by call-path, not by calling the function directly —
      the current defect is that no caller passes it.
- [ ] 1.2 Test: a queue holding entries for already-current files reports zero outstanding
      work after a drain, without re-embedding.
- [ ] 1.3 Test: with a seeded backlog and no operator command, repeated reconciliation
      passes strictly decrease the queue count.
- [ ] 1.4 Test: in `quiet` policy, per-pass admission for deferred work is non-zero.
- [ ] 1.5 Confirm whether `file_watcher.py`'s quiet branch actually defers downstream
      despite leaving `defer_semantic = False`. Write the test that pins whichever
      behaviour is correct; if the log disagrees with reality, that is in scope here.

## 2. Make both queues reachable

- [ ] 2.1 Give the full-upsert queue a drain path mirroring `drain_deferred_work`.
- [ ] 2.2 Pass `include_full=True` from `_index_main` so `exomem index` clears both queues.
- [ ] 2.3 Audit `deferred_index.add` / `add_full` call sites against their clear paths and
      note any remaining asymmetry in the PR body.

## 3. Reconcile entries against index state

- [ ] 3.1 Resolve each queued entry against the freshness check the indexer trusts.
- [ ] 3.2 Retire satisfied entries without embedding; perform genuine work before retiring.
- [ ] 3.3 Test both directions explicitly (satisfied → retired, genuine → performed first).

## 4. Own the drain on the reconcile pass

- [ ] 4.1 Add the drain as a step of the reconcile pass, spending the pass's remaining
      budget after drift admission.
- [ ] 4.2 Bound admission by the active mode policy; no independent write path, no new
      thread.
- [ ] 4.3 Test: a mutation committed while entries are queued does not block on the drain.

## 5. Quiet becomes a throttle

- [ ] 5.1 Replace `_QUIET_EXPENSIVE_INDEX_CAP = 0` with a non-zero bounded admission.
- [ ] 5.2 Test convergence: a corpus-scale backlog reaches zero in a bounded number of
      passes under quiet policy.
- [ ] 5.3 Confirm no steady-state GPU residency is introduced in `quiet` or `normal`.

## 6. Honest reporting

- [ ] 6.1 `status`: `next_action` names only a real automatic action or a runnable command.
- [ ] 6.2 `doctor`: warn when a queue exceeds a meaningful fraction of indexed pages.
- [ ] 6.3 Test: a seeded corpus-scale backlog produces a doctor warning and the vault is not
      reported as unqualifiedly healthy.

## 7. Mode persistence fails loudly

- [ ] 7.1 Catch `PermissionError` / `OSError` around the persist in `_mode_main`.
- [ ] 7.2 Remove the temporary file on failure; exit non-zero.
- [ ] 7.3 Emit one line naming the config path and the remediation — no bare traceback.
- [ ] 7.4 Read the mode back from the persisted file rather than echoing the request.
- [ ] 7.5 Test all four: exit code, no residue, message content, read-back.

## 8. Verification

- [ ] 8.1 `ruff check`
- [ ] 8.2 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q` green
- [ ] 8.3 `openspec validate fix-deferred-work-drain --strict`
- [ ] 8.4 Record before/after queue counts from a seeded backlog in the PR body.
