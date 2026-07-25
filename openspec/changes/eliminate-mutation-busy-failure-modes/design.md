## Why removing the hosted read-bypass is safe (R3, GAP D)

The archived design (`make-mcp-acknowledgement-replay-safe`) reserved the
consistency-guard bypass to `mode="audit"` and `validate_only` reads because,
at the time, other hosted reads were assumed to need the guard to avoid
observing a half-committed mutation. That assumption does not hold: every
canonical write already lands via atomic staging (write-to-temp then
`os.replace`), which is exactly what makes a whole-file read torn-free without
any lock. Local reads have never taken the guard and have never needed to.
Removing the guard for hosted reads makes hosted and local reads behave
identically and removes the actual observed failure — a plain read returning
`MUTATION_BUSY` merely because an unrelated write is still validating its
12-45s corpus check.

The residual risk is a read observing "pre-mutation" state a few milliseconds
longer than it might have with the guard held. That is the same staleness
window every local read already accepts, and it is bounded by the same atomic
publish, so it introduces no new class of inconsistency — only removes a
serialization cost that reads never structurally needed. No escape-hatch env
var is added to keep the guard optional, because that would keep the failure
mode reachable by default drift and double the test matrix (guard on/off ×
everything else) for a behavior that should not exist at all.

## Orphan snapshot age-awareness (R2, GAP C)

`mutation_lock.py` today either takes the metadata mutex and reports a
verified holder, or times out and fabricates `_unknown_external_holder()` at
age 0 with `overdue: false` — indistinguishable from "just acquired,
everything's fine." When the mutex cannot be taken within a short bound
(250ms), the fix reads the holder sidecar directly instead of giving up: the
sidecar is published via atomic `os.replace`, so a lock-free read is
tear-free even without the mutex. That gives a real `age_seconds` and
`overdue` even when verification isn't possible, tagged `verified: false` so
callers can still distinguish a probed-but-unverifiable holder from a fully
confirmed one. Only the genuine absence of any sidecar still falls back to
the unknown-holder fabrication, because there is nothing to read.

## Idle-triggered lease release (R5, GAP A) — the exemption is load-bearing

A preferred replica must never be subject to idle release. Without the
exemption, the desktop (typically configured preferred) would acquire the
lease, sit idle, release it after 60s, and then reclaim it again on the next
renew tick (the existing preferred-reclaim behavior from
`fix-preferred-writer-reclaim`) — a self-inflicted churn loop that flaps edge
routing every renew interval for no operational benefit. Idle release exists
to solve a different problem: a non-preferred holder (the laptop) that is
powered on but not being used should hand authority back so the desktop can
serve without the laptop ever noticing it should let go.

Counting "am I idle" correctly requires one choke point.
`writer_authority_guard()` is the single place that sets the write fence
before a mutation and clears it after, so it is also the only place that can
increment/decrement `_active_mutations` and touch
`_last_activity_monotonic` without a race between "count the mutation" and
"the mutation actually fences." In `_renew_loop`, under the same lock the
renewer already uses: `idle-for-≥60s AND active_mutations == 0` clears the
local fencing token and calls `client.release(token)` while still holding the
lock, so an `ensure_writer()` racing in from another thread blocks briefly
(≤3s, the existing acquire timeout) and then acquires a fresh token — it
never observes a torn state where the process thinks it's still the writer
but the coordinator disagrees. A release RPC failure is swallowed because the
local token is already cleared; the coordinator's own TTL expiry closes the
gap within one TTL, which is a degraded handover, never split-brain (a
straggler write in flight when release happens is still rejected by
`validate_active_write_fence` at the atomic-write boundary — the existing
fencing guarantee from `harden-writer-lease-fencing` is unmodified and is what
makes idle release safe to add at all).

Handover latency is therefore bounded by roughly `idle_seconds + ttl/3` (the
desktop's own reclaim tick), and the edge falls back to
`selectEligibleOrigin` (`worker.js`) while no replica is currently assigned,
so an idle-released window does not produce a hard outage.

## Abandoned idempotency receipts (R4, GAP B) — why liveness, not TTL alone

A bare TTL on a `pending` row cannot distinguish "the process is still
working, just slow" from "the process is dead." Making the TTL long enough to
never misfire during genuine slow writes reintroduces the original hang;
making it short enough to recover quickly risks declaring a live, still-
committing write abandoned and letting a second attempt race it. The fix
instead tracks liveness directly: each row gets an `owner` string
(`pid:process-nonce`), and liveness is proven by attempting an exclusive OS
lock file per process under `<state_dir>/idempotency-owners/` — a PID alone
is reusable across a reboot and would falsely report the wrong process as
alive; the lock file is held only while that specific process is running, so
a failed probe (lock acquired, meaning nothing holds it) means the owner is
provably gone, and a probe error is treated as "alive" (fail closed — never
declare abandonment on an inconclusive probe).

A dead owner's `pending` row moves to `abandoned`, and the client receives
`OpError("MUTATION_OUTCOME_UNKNOWN", ..., details={status: "uncertain",
committed: None})` rather than an indefinite `MUTATION_ACKNOWLEDGEMENT_PENDING`
loop. The identical retry is deliberately paused for 60s before it is allowed
to execute fresh: a crashed-after-commit write becomes loud (a duplicate
attempt hits an existing leaf conflict guard) rather than silently
duplicating a committed effect, and 60s gives any last-moment straggler
persistence a chance to finish first. Legacy `NULL`-owner rows (written before
this change) are honored under the old any-pending-blocks-forever rule for up
to 600s so a rollout does not retroactively abandon rows from a version that
never recorded an owner, then age out the same way.

## Rejected: escape-hatch env var for the read-bypass removal

Considered and rejected. It would let the old failure mode persist by
default drift (a copied `.env` carries it forward), and it forces every other
test in this change to run twice — once with the guard, once without — for a
behavior nothing legitimately depends on once atomic staging is understood.
The deferred `shorten-mutation-critical-section` change is a better home for
any future latency work discovered by R1's telemetry; this change does not
speculatively design for it.
