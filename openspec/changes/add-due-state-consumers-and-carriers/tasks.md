## 1. Contracts and red-first acceptance

- [ ] 1.1 Add the `attention-queue`, `command-surface`, and `agent-bootstrap-contract` deltas.
- [ ] 1.2 Red-first: prove today's gap — an overdue `check_by` surfaces in no queue, no response, and no bootstrap payload — then flip by implementation. Each consumer and each carrier gets a mechanism-removal test.
- [ ] 1.3 Egress red-first: a withheld due item must contribute zero to every count, reference list, and ordering on every carrier, with the absence indistinguishable from nonexistence.

## 2. Consumers

- [ ] 2.1 Implement the four audit categories with standard review-item, fingerprint, and triage semantics; unit-local prediction predicate; age-ordered experiments; candidate-not-defect question aging; supersession-integrity defects.
- [ ] 2.2 Wire the categories into attention with the existing fusion and state-summary paths.

## 3. Projection

- [ ] 3.1 Implement the maintained projection: incremental per-family deltas on write, day-boundary re-bucketing, reconcile healing, full-recompute recovery, per-audience post-egress computation, persistence beside the review state.
- [ ] 3.2 Bound per-write projection work to the families a write can affect; verify under the write-latency gates including the page-size scaling bound.

## 4. Carriers

- [ ] 4.1 Project the validated advisory `due_state` block into default compact mutating responses following the structural-suggestion posture; legacy detail omits it; envelope branch keys unchanged.
- [ ] 4.2 Serve the block on recall responses delta-only, and in the bootstrap payload with the engagement-guidance teaching lines; correct any post-write guidance that names fields the default response does not carry.
- [ ] 4.3 Implement emission governance: change-of-count or first-of-session, no identical-total repeats, batch-once for bulk operations; deterministic tests without an agent.
- [ ] 4.4 Live thin-client probes: demonstrate the block surviving tool-result handling on at least one hookless web client for a mutation and a recall, and record the evidence in the change verification.

## 5. Verification

- [ ] 5.1 Focused suites green; mechanism-removal checks for every consumer, the projection's time-bucket path, and each carrier; emission-governance and egress adversarial tests green.
- [ ] 5.2 Lean suite, write-latency gates, and affected golden fixtures green; tool-surface fingerprint unchanged; scaffold/bootstrap regeneration through the existing packaging path.
- [ ] 5.3 Bench families f23 (counter governance) and f26 (carrier journey) from the no-nudge amendment execute against this implementation once both changes exist; record their status honestly in verification.
