## 1. Contract and regression tests

- [x] 1.1 Add command-surface and attention-queue delta specifications.
- [x] 1.2 Add a semantic-write-contract delta specification for advisory but non-qualifying unregistered observations.
- [x] 1.3 Replace the private feedback-builder regression with a red-first public compiled-write test.
- [x] 1.4 Add red-first contract regressions proving an unknown-only relation remains non-qualifying.
- [x] 1.5 Add red-first tests for at-threshold and below-threshold proposal inference.
- [x] 1.6 Retain explicit-save, stale-hash, and observed-deletion regression coverage.

## 2. Semantic disposition and write feedback

- [x] 2.1 Resolve distinct authored relation labels through the active registry.
- [x] 2.2 Make `unregistered_relation` advisory without allowing the fact to qualify.
- [x] 2.3 Return the non-blocking signal and promotion next action from the public compiled-write result.
- [x] 2.4 Keep registered relation feedback and the top-level note response shape unchanged.

## 3. Recurrence proposals

- [x] 3.1 Add a deterministic recurrence threshold defaulting to three.
- [x] 3.2 Populate the copied proposal for recurring unregistered labels with unset parent and description.
- [x] 3.3 Keep inference response-only and preserve reviewed extensions.

## 4. Verification

- [x] 4.1 Run focused note, memory-schema, and relation-registry tests.
- [ ] 4.2 Run the full pytest suite and `ruff check . --select F` on Linux.
- [x] 4.3 Validate the OpenSpec change in strict mode.
