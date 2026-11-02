## Context

`dreamer_policy.decide` maps one snapshot of cheap probes to a run or wait decision. It waits for `quiet_mode` before every activity gate. The live service is pinned to quiet by a service drop-in, so the Dreamer has never ticked there. `openspec/specs/resource-governance/spec.md` defines quiet as CPU-only, no preload and unload on entry. It never says quiet stops upkeep, and a tick loads no model (`tests/test_dreamer_no_side_effects.py`).

A tick already stops before its next page on the first foreground request (`foreground_activity.foreground_since`) or a vault generation change. So the 60-second idle gate (`IDLE_SECONDS`) and the settle window decide only when a tick may start, not whether it may disturb a request.

The sensor child inherits the tick gate: `dreamer._loop_once` and `_regate_sensor` pass the tick decision to `sensor_worker.supervise`. The child runs at the lowest OS priority, on CPU, with one thread, in its own process. Its measured peak resident memory is 1,362 MiB.

## Decisions

### D1. Quiet mode no longer gates a tick

`decide` drops the `quiet_mode` wait. Quiet keeps GPU use and preload off elsewhere (`mode`, `accel`). The tick's budgets stay as they are: a separate quiet budget would be a second number to review, for work that already runs at about 1.7% of one core at most.

### D2. The idle and settle preferences end at `MAX_DEFER_SECONDS`

`TickSignals.seconds_since_tick` carries the time since the last completed tick, or since the worker started before the first. A tick counts as completed when `run_once` returns, whatever its stop reason.

The rule order in `decide` is: off, paused, standby, pressure, then:

1. A live request (`idle_seconds <= 0`) always waits as `foreground`. No bound waives it.
2. `freshness_unavailable` stays hard.
3. The `foreground` (idle under `IDLE_SECONDS`) and `settling` holds apply only while `seconds_since_tick < MAX_DEFER_SECONDS`.
4. `graph_debt`, `index_backlog`, `budget_exhausted` and `backoff` stay hard, in their current order.

A hold sleeps until the preference ends or the bound passes, whichever comes first. A `None` value reads as "bound not reached", the earlier and stricter rule. The worker always supplies a number.

The bound is 60 seconds, the same as `IDLE_SECONDS`. At the worker's 30-second poll a tick starts within 90 seconds of the last one on a vault that is never idle. The per-tick budgets are unchanged, so a tick does at most 32 pages, 200 ms of thread CPU and one second of wall time, and the hourly budget stays 60 CPU-seconds.

### D3. The tick yields per page, so a request does not wait for a tick

The latency risk of starting a tick on a busy vault is the page in flight when a request arrives. A page is one transaction in the Dreamer's own sidecar. The tick checks the foreground request, the vault generation and its budgets before each page, so it stops before the next page. The reviewer checks that this per-page bound overlaps with foreground requests; no latency probe is added.

### D4. The sensor child has its own gate

`dreamer_policy.sensor_gate` is a pure function over the same signals. It waits for the setting off, paused, standby and `pressure is True`. It does not wait for quiet mode, foreground activity, a settling vault, graph debt, an index backlog or the tick's CPU budget. The child has its own budgets: 300 CPU-seconds and 600 judgements per rolling hour, one thread, the lowest priority, and an exit after 60 seconds without work.

The pressure probe is gathered only when the tick's cheap gate passes, as before. When it was not gathered, `pressure` is `None` and the sensor gate reads that as no pressure. The probe measures GPU headroom, and the child never uses the GPU.

### D5. Memory admission before launch

`Supervisor.step` refuses to launch with the action `memory` when a peak is recorded and `runtime_resources.available_memory()` is not above it. With no recorded peak it launches, and the child records its peak. A host that cannot report available memory also launches, as an unmeasurable host builds in `embedding_backend`. A running child is never killed for memory.

The child records `VmHWM` where the host reports it, else `ru_maxrss` in the platform's unit, else nothing, in its spend report. The supervisor keeps the last peak in its own file (`peak-rss.json`) beside the ledger. Each child overwrites `spend.json` from its first report, which would erase a peak kept there. A final report replaces the recorded peak. A report from a child cut off mid-load can only raise it. A refused child records nothing, because it never loaded the instrument.

`available_memory()` moves from `embedding_backend` to `runtime_resources`, and both import the one reader.

### D6. A Cloud cell starts the Dreamer

`server_runtime._start_dreamer` drops its cloud-mode early return. A cell starts the Dreamer when `EXOMEM_DREAMER` is on. The sidecar and checkpoint resolve through `vault_state_dir`, under the cell's state root, and `reconcile_setting` has no cell branch.

## Risks

- A host that reports no available memory launches without the check. Refusing there would lock sensing out for good on a host that records a peak but cannot read free memory (macOS `ru_maxrss` without `SC_AVPHYS_PAGES`). Linux, the live service and Cloud cells report it.
- In a container, `MemAvailable` shows the host, not the cgroup limit. The admission check does not replace the cell's own memory bound.
- A tick that starts on a busy vault may do one page before it sees a request. That page is the cost of the bound, and it is small and transactional.
