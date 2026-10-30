## Why

When the managed worker crashes, the supervisor stops on purpose so that systemd cleans the unit's cgroup and restarts it. The public listener is closed from the crash until the next worker reports ready, which took 46 to 50 seconds per crash in the 30 October 2026 incident. Clients behind the tunnel saw 502 errors and broken calls instead of a bounded answer.

## What Changes

- A worker that exits outside a transition is replaced in place. The public listener, the local listener and the control socket stay open. Ingress holds calls under the existing admission budgets until the replacement reports ready.
- Before the replacement starts, the supervisor proves that the exited worker and all its descendants have exited, with the same proof that a handoff runs. If that proof fails, the supervisor stops and leaves the cleanup to systemd.
- The supervisor restarts a worker in place at most 3 times in any 10 minutes. One more exit inside the window stops the supervisor with a failed exit status, so the unit's cgroup cleanup and `Restart=` apply as before.
- Status reports a `restarting` phase and a `worker_restarts` block: the restarts inside the window, the limit, the window and the last exit status.

## Impact

- Affected spec: `managed-service-upgrades`.
- Affected code: `src/exomem/service_manager.py`.
- Affected docs: `docs/managed-service-upgrades.md`.
