## 1. Yield and handoff

- [x] 1.1 Count interactive waiters only (`waiter_count`), including the queue behind the capture head; raise the cross-process signal for interactive waiters only.
- [x] 1.2 End an import apply call at a chunk boundary for a waiting interactive writer, with `stop_reason` and the existing continuation.
- [x] 1.3 Hand the boundary over after a waiter-prompted release in `chunk_hold` and the derived-drain turns, within the foreground-priority floors.
- [x] 1.4 Run the reconcile unit-sidecar and embedding phases in short holds that end for a waiting writer; name each hold by its phase.
- [x] 1.5 Leave the Windows waiter signal process-local and say so in the design.
- [x] 1.6 End an import `plan`, a `finish` and the steps of a `file` call at an item or step boundary for a waiting interactive writer, and narrow each call's budget to the room its hold has left under the overdue limit.

## 2. Replay marker

- [x] 2.1 Return `replayed: true` on a replayed terminal; keep the original `mutated`, `request_id` and `receipt_id`.
- [x] 2.2 Hash the implicit-retry digest over NFC strings.
- [x] 2.3 Correct the retry window in the retry-safe-mutations spec from 60 s to 600 s.

## 3. Proof

- [x] 3.1 End-to-end: an import apply is running; an agent write and a CLI write each commit once with no `MUTATION_BUSY` and no counter movement; the import returns a continuation and finishes every item once.
- [x] 3.2 Unit: interactive waiter count; the handoff wait bound; a maintenance phase lets a waiting writer in within one batch and applies every item once; the replay marker; the NFC digest.
- [x] 3.3 End-to-end: a slowed `plan` meets a waiting `create_note` and CLI write, and a waiting `file`; a `file` call ends between steps for a waiting writer; a call with no waiter ends before the scaled overdue limit.

## 4. Closure

- [ ] 4.1 After the release is live, the caller-visible refusal journal of the personal service shows no `MUTATION_BUSY` caused by an import apply, a maintenance job or a reconcile phase. (Moved from `absorb-transient-write-busy` task 4.2.)
- [ ] 4.2 On a disposable vault with its own state root, a second process holds the boundary for 4 s while clients write over MCP (no explicit key), REST with an `Idempotency-Key`, a Studio action, the CLI, `/upload` and tus: no busy refusal, and every write committed once. A REST write without a key is refused with a retryable `MUTATION_BUSY` after one bounded acquire wait, with one refusal-journal row. (Moved from `absorb-transient-write-busy` task 4.1. `tests/test_write_reaches_a_maintenance_job_in_another_process.py` covers the MCP write only.)
- [ ] 4.3 Archive this change with `openspec archive yield-boundary-to-interactive-writes` after tasks 4.1 and 4.2, and run `openspec validate --all --strict` before and after.
