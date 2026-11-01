## 1. Supervisor

- [x] 1.1 Open both listeners and the control socket before the first worker starts, refuse calls at once while it warms, and serve once it reports ready (`test_the_listener_answers_while_the_first_worker_warms`).
- [x] 1.2 Keep a failed first start stopping the supervisor with its error, and close both listeners (`test_a_failed_first_start_stops_the_supervisor_and_closes_both_listeners`).

## 2. Documentation

- [x] 2.1 Describe the start sequence in `docs/managed-service-upgrades.md`.
