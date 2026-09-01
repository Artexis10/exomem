## Why

An interactive request that finds a corpus-context build already in flight joins it
with `flight.done.wait()` and no timeout (`semantic_contract.py:2353`). The waiter
therefore inherits the owner's entire build duration, and a vault-sized corpus build
is not bounded by any caller's latency budget.

Measured on a 3,006-page vault over 61 complete mutation traces: the median mutation
spent **98% of its wall time after `canonical_files_committed`**, a median of 103.6s,
with a worst case of 409.4s post-commit against a 410.3s total. The durable write
itself completed in ~150ms. The cost is not the commit; it is the join.

The graph-rebuild path already treats this as a correctness requirement.
`rebuild-graph-without-blocking-writes` specifies that `join(handle)` SHALL admit a
**bounded** waiter and release capacity in `finally` on every completion, cancellation,
and failure path, and `graph_sync.py:2257` implements it. The corpus-context flight is
the same shape with none of the bounding. This is the second join site the existing
pattern note predicted would survive a site-by-site fix.

## What Changes

- Bound the corpus-context flight join by one fixed 2.0-second caller-side interactive
  budget rather than by how long the owner's build takes. A waiter that exceeds the
  bound returns the existing retryable `MUTATION_WARMING` outcome with
  `committed: false` and `retry_after_ms: 2000` instead of continuing to block.
- Return immediately when the joined flight is already settled. The zero-work path
  MUST NOT invoke the timed wait or spend the 2.0-second budget merely to discover
  completion that is already observable.
- Keep the bound non-configurable. A knob for raising it is a knob for reintroducing
  the stall; genuine convergence belongs in an explicit opt-in path, not in the default
  interactive budget.
- Return the deferred outcome only on timeout. A real build error MUST continue to
  propagate as an error, so the bound cannot convert a loud failure into a silent one.
- Make the deferred outcome visible in the default response projection, so a bounded
  result is never byte-indistinguishable from a completed one.
- Audit every remaining unbounded join and require each to be bounded or explicitly
  declared background-only with a reason, enforced by a test that enumerates call sites
  rather than asserting on one of them.

## Capabilities

### New Capabilities

- `corpus-context-availability`: an interactive caller joining an in-flight corpus-context
  build must be bounded by one fixed 2.0-second caller budget, must return immediately
  when the flight is already settled, must observe the existing pre-commit
  `MUTATION_WARMING` outcome only on timeout, and must never have a build error
  laundered into a deferral.

### Modified Capabilities

None.

## Non-Goals

- Making the corpus-context build itself faster. This change bounds what a caller waits
  for, not what the owner does.
- Changing corpus-context correctness, cache keying, census reconciliation, or the
  `same_inputs` recomputation path.
- The restart-triggered rebuild fallback that drives build frequency. That trigger is
  covered by `rebuild-graph-without-blocking-writes`; this change limits the blast
  radius when a build is in flight for any reason.
