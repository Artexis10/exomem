## Context

`build_corpus_context_with_census` uses a single-flight cache. The first caller for a
cache key constructs a `_CorpusContextFlight`, registers it in `_CORPUS_CONTEXT_FLIGHTS`,
and owns the build. Every subsequent caller for that key takes the `not owns_flight`
branch and blocks on `flight.done.wait()` (`semantic_contract.py:2475`). There is no
timeout on that wait, and `_CorpusContextFlight` exposes no bounded variant.

The owner path is otherwise well behaved: it clears its registry entry and sets
`flight.done` in both the success and failure paths, so waiters are
always released eventually. "Eventually" is the defect. On a 3,006-page vault a build is
tens of seconds; the waiter has no budget of its own.

### Measured evidence

From the service log directory, 61 complete mutation traces:

| metric | value |
|---|---|
| median share of mutation spent post-commit | 98% |
| median post-commit duration | 103.6s |
| worst post-commit duration | 409.4s (of a 410.3s total) |
| durable canonical write | ~150ms |

A representative trace: `canonical_files_committed` at `20:41:50.523`, `returned` at
`20:43:01.490` — 70.97s of a 77.6s call, after the files were already durable.

### Prior art in this repository

- `rebuild-graph-without-blocking-writes` specifies a bounded waiter for the graph
  rebuild flight; `graph_sync.py:2257` implements "Admit one bounded response waiter for
  work already registered." The corpus-context flight is the unbounded sibling.
- `Notes/Patterns/bound-interactive-waits-below-the-work` records the rule, the
  non-configurable constraint, the do-not-launder-failures constraint, and the
  enumeration test.
- `Notes/Insights/the-exomem-graph-rebuild-never-touches-the-vault-creation-lock` is a
  measured negative result. `VAULT_LOCK_NESTED` and `VAULT_LOCK_TIMEOUT` appear in the
  same log windows and are correlated symptoms, not the cause. Do not re-derive that.

## Goals / Non-Goals

**Goals.** Bound what an interactive caller waits for. Keep the deferred outcome
visible. Keep a real error an error. Prevent the next unbounded join from surviving.

**Non-Goals.** Speeding up the build. Changing corpus-context semantics. Fixing the
restart-triggered fallback that raises build frequency — that is the graph change's
scope, and it is why this change is framed as blast-radius limitation rather than a cure.

## Decisions

### One fixed 2.0-second caller-side bound

The join uses one fixed 2.0-second bound. That leaves substantial room above ordinary
in-process scheduling noise while remaining roughly 52 times shorter than the measured
103.6-second median post-commit interval and far below the 15-second connector timeout
that exposed the failure. Sizing the bound from observed build duration would reproduce
the defect: a bound that usually succeeds would still occasionally stall a caller for
minutes.

### Already-settled flights take the zero-work path

The waiter checks `flight.done.is_set()` before invoking the timed wait. When the owner
has already published completion, the waiter consumes the existing result or error
immediately. If completion races with the check, `Event.wait(timeout=2.0)` returns
promptly, preserving the same result without burning the budget. This fast path is
covered independently so an implementation that mechanically sleeps or invokes a
patched timed wait for already-complete work fails the suite.

### Non-configurable

No environment variable, config key, or parameter raises it. A configurable bound is a
supported way to restore the stall. Callers that genuinely need convergence get an
explicit opt-in path instead, which is out of scope here.

### Deferred is a distinct outcome, not a degraded success

The deferred result must survive the default response projection. If it is only visible
under `response_detail="full"`, the default caller cannot tell a bounded result from a
completed one, and the bound becomes invisible in exactly the situation it fired.

### The waiter never disturbs the owner

Expiry releases the waiter only. Cancelling the owner would convert one slow caller into
a build that never completes, which is the livelock this is meant to reduce.

## Risks / Trade-offs

- **A waiter can now return without a corpus context it previously blocked for.** That is
  the intended trade. Every consumer of the deferred outcome must be checked for a path
  that assumed a context was always present.
- **The bound could fire routinely if builds stay slow.** That would be a true signal
  rather than a regression, but it makes the deferred path hot, so its correctness
  matters as much as the bound itself.
- **The enumeration test will flag legitimate background joins** (`file_watcher.py:426`,
  `media_worker.py:275`). Those need a declaration, not a bound; the declaration must
  record why the site is background-only.

## Open Questions

None. The consumer migration surface is a mandatory implementation audit rather than an
open design choice: every current call site must handle the typed deferred outcome
explicitly or prove that it cannot receive one.
