## 1. Establish the failing baseline

- [ ] 1.1 Add a regression test that registers a slow corpus-context flight and asserts a
  second joining caller returns within the fixed 2.0-second bound. It MUST fail against
  current `main`, where `flight.done.wait()` blocks for the owner's full duration.
- [ ] 1.2 Add a test asserting an owner build error still raises for the waiter, so the
  bound cannot be implemented by swallowing failures.
- [ ] 1.3 Record the measured baseline (98% post-commit share, 103.6s median) in the test
  module docstring so a future reader can tell whether the numbers still hold.
- [ ] 1.4 Add a regression test proving an already-settled flight returns immediately
  without invoking the timed wait. Record a mutation proof showing the named test fails
  when the settled fast path is removed or forced through the timed-wait branch.

## 2. Bound the join

- [ ] 2.1 Introduce the exact 2.0-second interactive bound as a fixed module-level
  constant with no configuration surface, justified in a comment against the measured
  103.6-second median and the 15-second connector timeout.
- [ ] 2.2 Add the already-settled fast path, then replace `flight.done.wait()` at
  `semantic_contract.py:2475` with `flight.done.wait(timeout=2.0)`. Distinguish
  "completed" from "expired" by the wait's own return value, not by inspecting flight
  state afterwards.
- [ ] 2.3 On expiry, return the existing retryable `MUTATION_WARMING` outcome with
  `committed: false` and `retry_after_ms: 2000`, and release admitted capacity in
  `finally` on every completion, cancellation, and failure path. Do not continue to
  semantic validation or canonical mutation without the context.
- [ ] 2.4 Leave the owner's build, registry cleanup, and `flight.done.set()` paths
  untouched; assert by test that an expired waiter does not disturb the owner.
- [ ] 2.5 Preserve the existing `same_inputs` recomputation branch exactly.

## 3. Make the deferred outcome visible

- [ ] 3.1 Reuse the existing pre-commit `MUTATION_WARMING` carrier rather than adding an
  optional corpus value or a committed terminal-sync field.
- [ ] 3.2 Surface code `MUTATION_WARMING`, status `retryable`, `committed: false`, and
  `retry_after_ms: 2000` in the default response projection, not only under
  `response_detail="full"` or `"legacy"`.
- [ ] 3.3 Add a test asserting timeout returns that exact default projection and that no
  canonical mutation occurs.
- [ ] 3.4 Audit consumers of `build_corpus_context_with_census` for paths that assume a
  context is always present, and handle the deferred case explicitly at each. The audit
  MUST cover `semantic_contract.py`, `relation_review.py`, and `semantic_writes.py` and
  record why any call site that cannot receive deferral is exempt.

## 4. Stop the next unbounded join

- [ ] 4.1 Add an enumeration test that walks request-reachable blocking joins and requires
  each to be bounded or carry an explicit background-only declaration with a reason.
- [ ] 4.2 Declare `file_watcher.py:426` and `media_worker.py:275` background-only with
  recorded reasons, or bound them if the audit shows they are request-reachable.
- [ ] 4.3 Assert the test fails when a new unbounded request-path join is introduced.

## 5. Verify against the real workload

- [ ] 5.1 Run the existing write-latency gates and confirm no regression in commit-path
  timings, which this change does not target.
- [ ] 5.2 Re-measure the post-commit share on a vault of comparable size and record the
  before/after split. The commit itself should be unchanged at ~150ms; the post-commit
  median is the number this change is accountable for.
- [ ] 5.3 Confirm on a real deployment that a bounded waiter reports deferred rather than
  silently returning a stale or empty context, using service logs rather than unit tests
  as the evidence.
- [ ] 5.4 Verify the interactive bound holds while a genuine rebuild is in flight, since
  that is the condition under which the stall was originally observed.
- [ ] 5.5 Run `openspec validate bound-corpus-context-flight-join --strict` and
  `openspec validate --all --strict`, recording the passing output with the lane result.
