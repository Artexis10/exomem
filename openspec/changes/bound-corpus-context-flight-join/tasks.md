## 1. Establish the failing baseline

- [ ] 1.1 Add a regression test that registers a slow corpus-context flight and asserts a
  second joining caller returns within the interactive bound. It MUST fail against
  current `main`, where `flight.done.wait()` blocks for the owner's full duration.
- [ ] 1.2 Add a test asserting an owner build error still raises for the waiter, so the
  bound cannot be implemented by swallowing failures.
- [ ] 1.3 Record the measured baseline (98% post-commit share, 103.6s median) in the test
  module docstring so a future reader can tell whether the numbers still hold.

## 2. Bound the join

- [ ] 2.1 Introduce the interactive bound as a fixed module-level constant with no
  configuration surface, justified in a comment against measured percentiles.
- [ ] 2.2 Replace `flight.done.wait()` at `semantic_contract.py:2353` with a bounded wait
  that distinguishes "completed" from "expired" by the wait's own return value, not by
  inspecting flight state afterwards.
- [ ] 2.3 On expiry, return the typed deferred outcome and release admitted capacity in
  `finally`, on every completion, cancellation, and failure path.
- [ ] 2.4 Leave the owner's build, registry cleanup, and `flight.done.set()` paths
  untouched; assert by test that an expired waiter does not disturb the owner.
- [ ] 2.5 Preserve the existing `same_inputs` recomputation branch exactly.

## 3. Make the deferred outcome visible

- [ ] 3.1 Represent the deferred corpus context as a typed outcome distinct from both a
  completed context and an error.
- [ ] 3.2 Surface it in the default response projection, not only under
  `response_detail="full"` or `"legacy"`.
- [ ] 3.3 Add a test asserting a deferred result is not byte-identical to a completed one
  in the default projection.
- [ ] 3.4 Audit consumers of `build_corpus_context_with_census` for paths that assume a
  context is always present, and handle the deferred case explicitly at each.

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
