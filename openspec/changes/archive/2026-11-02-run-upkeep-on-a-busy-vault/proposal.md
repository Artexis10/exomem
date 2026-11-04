## Why

The Dreamer, the default-off upkeep worker, has never ticked on the operator's live service. That service runs in quiet mode all day, and `dreamer_policy.decide` waits on `quiet_mode` before every other gate. Quiet mode stops GPU use and model preload. A tick loads no model, so the wait protects nothing.

Two more holds starve the worker on a vault with sessions that run all day:

- A tick may start only after 60 seconds without a foreground request and after the vault settles. A vault that is never idle never gets a tick.
- The sensor child inherits the tick's gate. Quiet mode, foreground activity and a settling vault stop it, so on a busy vault it never senses. It runs on CPU, in its own process, at the lowest OS priority.

A Cloud cell never starts the Dreamer, even with `EXOMEM_DREAMER=on`, because `_start_dreamer` returns early in cloud mode. The operator wants one behaviour on the local service and in a cell.

## What Changes

- Quiet mode no longer stops a tick. Quiet keeps GPU use and model preload off.
- A live foreground request still stops a tick from starting, and a tick still yields to any request between pages. The idle and settle preferences hold a tick at most `MAX_DEFER_SECONDS` (60 seconds) since the last completed tick. Graph debt, an index backlog, the freshness registry, the CPU budgets and the failure backoff stay hard gates.
- The sensor child gets its own gate: the setting and standby. A Cloud cell never launches it. Quiet mode, foreground activity, a settling vault, graph debt, an index backlog and the tick's CPU budget no longer stop it.
- The supervisor launches the sensor child only while the host's available memory is above the peak resident memory the last child recorded. It never kills a running child for memory.
- A Cloud cell starts the Dreamer when its setting is on.

## Capabilities

### Modified Capabilities

- `resource-governance`: quiet mode keeps deterministic background upkeep running within its budgets.

Two active changes carry the requirement text this change replaces. It amends them in place, as `add-sensed-epistemic-model` task 1.2 did for `close-memory-loop`:

- `close-memory-loop`, `adaptive-memory-maintenance`: when a tick starts.
- `add-sensed-epistemic-model`, `sensed-epistemic-model`: the sensor child's gate, termination and launch.

## Impact

- Code: `src/exomem/dreamer_policy.py`, `src/exomem/dreamer.py`, `src/exomem/sensor_worker.py`, `src/exomem/runtime_resources.py`, `src/exomem/embedding_backend.py`, `src/exomem/server_runtime.py`.
- Behaviour: a tick runs on a vault that is never idle, within the existing per-tick and per-hour budgets. A tick on a busy vault stops before its next page on the first request, so it adds no wait to a request.
- Undo: set `EXOMEM_DREAMER=off` or `paused`. Each part reverts on its own.
