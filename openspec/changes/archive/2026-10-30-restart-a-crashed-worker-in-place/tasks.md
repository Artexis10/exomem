## 1. Supervisor

- [x] 1.1 Replace a worker that exits outside a transition while the listeners stay open, after the descendant-exit proof (`test_a_crashed_worker_is_replaced_while_the_listener_stays_up`).
- [x] 1.2 Stop without a replacement when a descendant of the exited worker outlives the stop allowance (`test_a_descendant_that_outlives_the_stop_allowance_blocks_the_restart`).
- [x] 1.3 Restart in place at most 3 times in any 10 minutes, then stop the supervisor with a failed exit status (`test_crashes_minutes_apart_are_each_restarted_in_place`, `test_a_worker_that_keeps_exiting_spends_the_restart_budget`, `test_a_worker_that_cannot_restart_stops_the_supervisor`).
- [x] 1.4 Report the `restarting` phase and the `worker_restarts` block in supervisor status.

## 2. Documentation

- [x] 2.1 Describe a worker crash in `docs/managed-service-upgrades.md`.
