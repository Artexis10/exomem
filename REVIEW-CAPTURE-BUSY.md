# Review: capture survives ordinary contention (PR #1450)

Scope: `origin/perf/activation-latency-warmup...origin/fix/capture-survives-contention` at `e576e82`.

## Verdict: REQUEST_CHANGES

Exactly-once holds. Blocking: waits park scarce sync workers and overshoot the caller's deadline.

## Findings

**High: waiting captures starve all sync tools, activation included.**
- Location: `writer_lease.py:5434-5456` (retry loop with `time.sleep`); `runtime_resources.py:63,251` (`sync_workers`=8 is anyio's default limiter, which FastMCP sync tools share).
- A busy capture now holds its thread up to 40 s (was ≤5 s).
- Scenario: a 20 s edit holds the boundary while 8 agents call `remember`. Every token is taken, so `search` and activation queue for about 20 s. The foreground-priority gate cannot help (it orders bulk work, not tokens).
- Repro: call `LeaseManager._run_absorbing_capture_contention` with a `run` that raises `_mutation_busy` for a non-overdue holder, with `EXOMEM_CAPTURE_WAIT_SECONDS=3`. The calling thread is held for 3.01 s.
- Fix: cap absorbed waiters below `sync_workers` (for example `sync_workers // 4`) and refuse past the cap with `cause: capture_waiters_full`. Test that a read completes while captures wait.

**Medium: the wait overshoots the caller's deadline.**
- Location: `writer_lease.py:3807-3809` (`remaining - 2.0`, but `DELIVERY_RESERVE_SECONDS` is 5); `:5434` (each retry still blocks the full 5 s guard timeout).
- Repro: the same harness with `request_budget.RequestBudget(seconds=8)` and a `run` that sleeps 5 s before raising busy returns after **10.05 s**. The client has already timed out, which violates the spec line "never past the caller's own request budget".
- Fix: subtract `DELIVERY_RESERVE_SECONDS` plus the guard timeout, or pass `min(guard_timeout, deadline - now)` to the guard. Pin it with a budget test.

**Medium: `exomem_mutation_busy_total` counts absorbed attempts.**
- Location: `mutation_lock.py:2795` (`_mutation_busy` bumps the metric); `:2287-2320` (`_refused` also logs a WARNING row).
- Both fire on every absorbed retry. Repro: a capture that commits on its 20th attempt bumps the metric **19×**, yet no client saw `MUTATION_BUSY`.
- Task 9.3 ("metric stays flat") therefore fails by construction, and each wait also floods WARNING logs.
- Fix: count only client-visible refusals (in `invoke`'s error path), or add an `absorbed` label. Demote the absorbed refusal log.

**Low: the pre-boundary warm spends the budget and runs before replay.**
- Location: `writer_lease.py:4318-4324`; `reserved_paths.py:2211` (follower waits the full `budget.remaining()`).
- The warm runs for every mutating command. A follower of a running cold build waits out its whole budget, then enters the write with about 0 s left. Previously it got a prompt `MUTATION_BUSY`.
- The warm also precedes `completed_terminal`, so a post-disconnect replay of a durable terminal pays the cold walk first.
- Fix: reserve delivery plus guard time in the follower wait, and warm after the replay check.

**Low: fairness is in-process and covers captures only.**
- Location: `_FairCaptureQueue` (`writer_lease.py:3813`).
- Non-capture writers still race on a non-FIFO lock and see more `MUTATION_BUSY` under capture load.
- `waiting()` followed by `join()` is not atomic, so a capture arriving at an empty queue can bypass the line.
- This is bounded and not starvation. The spec's "arrival order" should say "within one process".

## CI

- #1450 is a **draft targeting `main`** (not the perf base) with `mergeable_state: dirty`, which means a merge conflict.
- On `e576e82` the only checks are two queued **Conventional Commit title** runs. Commit status is `pending` with 0 statuses. No test CI has run.

## Local tests

Scoped pytest over mutation_lock, writer_lease, capture_*, shorten_critical_section, observe_memory, episode_*, records_*, record_memory_command, semantic_write_latency_gate: **921 passed, 16 skipped** (Windows-only).

## Hypotheses cleared

- **Concurrent identical retries.** Busy is raised before `leaf_started` or with `commit_observed()` false, so `idempotency.run` deletes the claim. A same-key caller either waits on `pending` or becomes the sole owner. The loop never retries once a commit is observed or `committed` is not `False`.
- **Same key, different payload.** The digest conflict is a non-busy error and is re-raised. The first commit wins, and the other caller is told so.
- **Disconnect or restart mid-wait.** Nothing is durable between attempts. A write that commits after the client leaves replays on the same-key retry. A restart loses only an uncommitted attempt, which was never reported as committed.
- **Honest status.** There is no accepted-and-queued mode. Every response is either committed or `MUTATION_BUSY` with `committed: false` and an accurate `cause`.
- **State leaking across retries.** Fast-ack batches, fanout and housekeeping are registered only after the canonical commit.
- **Stuck holder.** An overdue holder (30 s) is refused at once. A holder that is not yet overdue is waited out for a bounded time, subject to the Medium above.
- **Identity walk vs resolve-before-create.** The warm is the same generation-proved, single-flighted build that the existing background warm uses. A crossing publication forces a retry or the locked scan. Duplicate-entity refusal still runs inside the boundary; only a cache is prefilled, and no check left the lock.
- **scandir counting.** Matches the `rglob` rules and is pinned by test.
