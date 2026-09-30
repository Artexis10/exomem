## Context

`Supervisor.upgrade()` ran the whole post-warm sequence inside one `asyncio.timeout(deadline.remaining(40))`, where `deadline` is the cutover budget. That block covered stream detachment, the stop of the old worker, the offline migrator, *and* the wait for the replacement to report ready. `promote_standby` was additionally handed `deadline.remaining(30)`, and its non-migrated promote POST capped at ten seconds.

One budget bounding both halves conflates two different questions. Before the stop, the supervisor is asking "is this upgrade worth continuing?", and the answer can still be no, because a worker is serving and admission can be given back to it. After the stop, the only question left is "is the replacement coming up?", and there is no longer an alternative to wait for. The 2026-09-20 outage is what the conflation costs: a promoted worker that held the writer lease and was healthy was stopped for taking longer than 40 s to finish a background retrieval-catalog repair, and every `--resume` re-killed that repair.

## Goals / Non-Goals

**Goals:**

- Bound the replacement's readiness wait by the cold-start budget on every path that reaches it: fresh upgrade with a promoted standby, fresh upgrade with a cold one-worker start, and `--resume`.
- Keep one source of truth for that window, shared with `Supervisor.start()` and with the operator client's polling deadline.
- Keep every fast failure fast: a replacement that exits, or that serves the wrong release, still fails immediately.
- Keep admission bounded and explicit through the longer wait.
- Keep the terminal outcome of a replacement that never arrives exactly as it is, only later.

**Non-Goals:**

- Change what a standby does, what promotion proves, or when the migrator runs.
- Change the ingress admission contract.
- Make `Supervisor.start()` roll a pending transition record forward automatically (see below).

## Decisions

### The cutover budget ends at the migrator

The `asyncio.timeout(deadline.remaining(40))` block now closes after the migrate step. Pause, drain, record, detach, stop and migrate keep the 40-second cutover budget; a hang in any of them still fails while the decision is cheap, and the existing drain-expiry path still reopens admission to the worker that is still serving.

### The replacement's readiness wait gets the cold-start window

After the block, `Supervisor._replacement_budget()` supplies the timeout for `promote_standby` or `start`. It is `cold_start_window(self.cold_start_timeout, self.cold_start_floor)` — the same function `Supervisor.start()` now calls, which is `max(floor, EXOMEM_COLD_START_SECONDS or 300 s)` with a 120-second floor. Naming it once means an operator who lengthens the window for a large vault lengthens it everywhere it applies, and cannot lengthen one path while leaving another short.

No outer `asyncio.timeout` wraps that wait. `WorkerRuntime.start` and `WorkerRuntime.promote_standby` each build their own `Deadline(timeout)` and raise `TimeoutError` from it, so the wait is already bounded, and this matches what `Supervisor.start()` has done since #1332. An outer timeout would also race the inner one: `promote_standby` catches its own `TimeoutError` to record that ownership already moved to the candidate, and a cancellation arriving from outside would skip that bookkeeping and lose the single most useful fact for whoever resumes.

### `promote_standby` is unchanged

It already derives everything from the timeout it is handed. Its non-migrated ten-second cap applies to the promote POST — "does the candidate's control surface answer at all", the fast-failure case — while the readiness loop that follows is bounded by the passed budget. Handing it the cold-start window is therefore the whole fix for the promoted path; the incident's POST did answer, with `snapshot: advanced`, and it was the readiness wait that was cut short.

### Admission keeps pausing

`ServiceIngress.pause()` does not queue without a bound. `_queue` gives a paused request `queue_timeout` seconds from reservation — 45 s by default, including body intake — and then answers `_undispatched(send, 503, "queue wait timed out", body)`, which renders as a JSON-RPC error saying the request was not dispatched, with the caller's own id when it has one. That is already the bounded explicit refusal D3 requires, and it is deliberately shorter than the replacement window, so no client ends up waiting on a handoff without an answer. Switching to `unavailable()` during the wait would only replace one explicit refusal with another while discarding the worker client, so pausing stays.

### The operator's deadline covers both budgets

`service_upgrade._transition_budget()` becomes `standby_warm_budget() + cold_start_window() + 120.0`, reusing the same two functions rather than restating a number. A client that gives up while the supervisor is still legitimately waiting is what sends an operator to `--resume` in the middle of a handoff that was going to succeed — which is how the incident's second failure was produced. `scripts/upgrade.sh` imposes no deadline of its own on the managed branch, so this is the only bound to extend.

### `ready_after_ms`

The handoff record already carried `unavailable_ms` (pause to resume). `ready_after_ms` is measured from the old worker's stop to the replacement's readiness, so it says how much of the outage was the replacement's own warm rather than the cutover mechanics. When the two are close, the cutover was cheap and the warm is the thing to fix. `--status` needed no new surface: a transition in flight already reports `phase: upgrading`, its `transition` id and a `pending` record naming the step.

## Considered and deferred

### Rolling a pending record forward automatically at boot

`Supervisor.start()` refuses to launch a worker while a transition record is pending, which is why a unit restart could not recover the incident either. Making it roll the recorded target forward on its own would have recovered that service without an operator.

It is deferred. With the change above, `--resume` works — it waits out the replacement instead of killing it — so automatic roll-forward is no longer the difference between recoverable and stranded. And it is a contract change, not a bound: the current refusal is what guarantees that a supervisor which restarted mid-transition never selects a release without a person deciding, and the canonical spec states it as "it does not launch the prior release and offers explicit roll-forward recovery". Rolling forward automatically means starting a release whose migration may have run, may have half-run, or may not have started, with no operator having looked. That deserves its own proposal, its own proof that the recorded phase is sufficient to distinguish those cases, and its own scenarios — not a paragraph inside a budget repair.

## Risks / Trade-offs

- A genuinely broken replacement that stays alive and on the right release now holds the service unavailable for up to the cold-start window (300 s by default) instead of 40 s before reporting failure. This is deliberate: the shorter failure did not restore service, it removed the only path back to it. The exit and wrong-release checks keep the common broken-replacement cases fast.
- The window is shared with `Supervisor.start()`. An operator lengthening it for a slow vault also lengthens how long a hung replacement stays unreported during a handoff. That is the intended coupling — both answer "how long may a worker take to warm with nothing serving" — but it means the knob is not per-path.
- `ready_after_ms` is computed from the stop, so it includes the migrator when one runs. That is the honest number for "how long after the last owner died was anything serving", and `migration.state` says whether the migrator was part of it.
