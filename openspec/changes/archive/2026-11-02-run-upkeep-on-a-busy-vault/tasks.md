## 1. Policy and loop

- [x] 1.1 `dreamer_policy`: remove the `quiet_mode` wait, add `MAX_DEFER_SECONDS`, `TickSignals.seconds_since_tick` and the new rule order, and add the pure `sensor_gate`. Rewrite the module docstring.
- [x] 1.2 `dreamer`: track the last completed tick and the worker start, feed `seconds_since_tick`, and pass `sensor_gate` to `sensor_worker.supervise` from `_loop_once` and `_regate_sensor`. Update the docstring.
- [x] 1.3 Tests: the quiet row runs; a busy vault holds a recent tick and runs one past the bound; a live request and the hard gates hold past the bound; the sensor gate rows; the bound counts from the last completed tick; a busy vault in quiet mode ticks.

## 2. Sensor memory admission

- [x] 2.1 Move `available_memory` to `runtime_resources` and add `peak_rss_bytes`. Keep one reader, imported by `embedding_backend` and `sensor_worker`.
- [x] 2.2 The child reports its peak in its spend report. The supervisor persists the last peak, refuses to launch with `memory`, and never kills a running child for memory.
- [x] 2.3 Tests: a recorded peak at or above available memory refuses; unknown availability launches; no peak launches; a running child survives; the peak persists across restarts and a cut-off or refused child cannot lower it; the child reports its peak.

## 3. Cloud cell

- [x] 3.1 Remove the cloud-mode early return in `server_runtime._start_dreamer` and correct its docstring.
- [x] 3.2 Test: with `EXOMEM_CLOUD_CELL=1` and the setting on, the runtime starts the Dreamer, and its sidecar lies under the state root.

## 4. Specification and documentation

- [x] 4.1 Add the `resource-governance` delta.
- [x] 4.2 Amend `close-memory-loop` and `add-sensed-epistemic-model` in place:
  - `close-memory-loop` `adaptive-memory-maintenance`: when a tick starts and what bounds the wait.
  - `add-sensed-epistemic-model` `sensed-epistemic-model`: the child's gate, termination and launch, and the scenario "Quiet mode returns the memory".
  - `add-sensed-epistemic-model` `design.md` Launch and Termination, and tasks 4.1 and 4.3.
- [x] 4.3 Correct text that says quiet stops the Dreamer or that it runs only when idle.
- [x] 4.4 Validate with `openspec validate --all --strict`, archive with `openspec archive run-upkeep-on-a-busy-vault`, and validate again.

## 5. Review corrections

- [x] 5.1 A tick cut off at page 0 does not restart the bound. The sensor gate drops pressure. The supervisor keeps the peak by instrument key. A Cloud cell never launches the child. The child raises its own `oom_score_adj`. The posture checks live in one helper.
- [x] 5.2 Correct text in `close-memory-loop` and `add-sensed-epistemic-model` that said quiet kills the child, that the Dreamer waits for an idle service with no maximum, or that cells do not run it.
