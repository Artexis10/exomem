## 1. Contracts and red-first acceptance

- [x] 1.0 Reconcile the `attention-queue` delta against PR #555: ADD only `question_aging` and
      `supersession_integrity`; state that `prediction_window` and `unfinished_experiments` are owned by
      `add-prediction-window-review` and `close-experiment-lifecycle` and consumed unchanged; align
      proposal, design, and tasks; `openspec validate --strict` passes.
- [x] 1.1 Add the `attention-queue`, `command-surface`, and `agent-bootstrap-contract` deltas.
- [x] 1.2 Red-first: prove today's gap — an overdue `check_by` surfaces in no queue, no response, and no bootstrap payload — then flip by implementation. Each consumer and each carrier gets a mechanism-removal test.
- [x] 1.3 Egress red-first: a withheld due item must contribute zero to every count, reference list, and ordering on every carrier, with the absence indistinguishable from nonexistence.

## 2. Consumers

- [x] 2.1 Implement the two remaining audit categories with standard review-item, fingerprint, and triage semantics: candidate-not-defect `question_aging` on a unit-local answering predicate, and `supersession_integrity` defects (dangling pointer, multi-headed chain). `prediction_window` and `unfinished_experiments` ship in the two #555 changes and are consumed unchanged.
- [x] 2.2 Wire the two new categories into attention with the existing fusion and state-summary paths: `supersession_integrity` into the default union, `question_aging` registered but opt-in.

## 3. Projection

- [x] 3.1 Implement the maintained projection: incremental per-family deltas on write, day-boundary re-bucketing, reconcile healing, full-recompute recovery, per-audience post-egress computation, persistence beside the review state.
- [ ] 3.2 Bound per-write projection work to the families a write can affect; verify under the write-latency gates including the page-size scaling bound.
      NOT DONE: the bounding half shipped (`due_state.DELTA_CATEGORIES` restricts the per-write
      delta to the three page-local families, and `test_a_delta_does_not_rerun_the_full_audit`
      pins that the write path never calls `audit.audit`), but the verification half is not
      evidence yet. `tests/test_semantic_write_latency_gate.py` exercises the *checker* in
      `scripts/semantic_write_latency.py` against synthetic samples; it does not measure this
      implementation. Real measurement means running that benchmark, which this lane's brief
      forbids. Operator: run `scripts/semantic_write_latency.py` on a quiesced machine and tick.
- [x] 3.3 Keep missing or unreadable projection recovery off interactive reads: return the
      advisory silent, start exactly one process-local background rebuild, and serve it only
      after the persisted or in-process projection is ready. Pin non-blocking and single-flight
      behavior with a blocked-reconcile regression.

## 4. Carriers

- [x] 4.1 Project the validated advisory `due_state` block into default compact mutating responses following the structural-suggestion posture; legacy detail omits it; envelope branch keys unchanged.
- [x] 4.2 Serve the block on recall responses delta-only, and in the bootstrap payload with the engagement-guidance teaching lines; correct any post-write guidance that names fields the default response does not carry.
- [x] 4.3 Implement emission governance: change-of-count or first-of-session, no identical-total repeats, batch-once for bulk operations; deterministic tests without an agent.
- [ ] 4.4 Live thin-client probes: demonstrate the block surviving tool-result handling on at least one hookless web client for a mutation and a recall, and record the evidence in the change verification.
      NOT THIS LANE: requires a deployed build and a real hookless web client. The operator
      attaches the evidence after deploy.

## 5. Verification

- [x] 5.1 Focused suites green; mechanism-removal checks for every consumer, the projection's time-bucket path, and each carrier; emission-governance and egress adversarial tests green.
- [x] 5.2 Lean suite, write-latency gates, and affected golden fixtures green; tool-surface fingerprint unchanged; scaffold/bootstrap regeneration through the existing packaging path.
- [ ] 5.3 Bench families f23 (counter governance) and f26 (carrier journey) from the no-nudge amendment execute against this implementation once both changes exist; record their status honestly in verification.
      PARTIAL, recorded honestly. f26 (carrier journey) was run against this lane's CLI on a
      throwaway copy of the sample vault and is RED there — not because the carrier is missing,
      but because `audit.audit` over the four due-state categories returns zero findings on
      `src/exomem/_sample_vault`: that corpus owes nothing, so the block is correctly absent and
      f26 would be red against any correct implementation of this change. A seeded variant of the
      same vault driven through the same CLI envelope was GREEN on the reconstruction probe.
      f23 (counter governance) belongs to the S3 lane; both changes do not exist together in this
      worktree, so it was not run. See `.task/RESULT.md` gate 7 for the verbatim output.
