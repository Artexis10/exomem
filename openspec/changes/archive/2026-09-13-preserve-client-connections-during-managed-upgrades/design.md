## Context

See proposal.md for the user-visible failure. FastMCP 4.0.3 already runs stateless HTTP. Tests with one entered FastMCP Client context reproduce a permanent transport failure after a connection refusal during downtime. A retained listening socket covers new connections but does not retain accepted keepalive connections or prevent active SSE calls from being cancelled at shutdown. Durable OAuth sessions are an existing, separate authority.

## Goals / Non-Goals

**Goals:** Keep the public listener and accepted connections owned by a stable process; replace a single state-owning worker within bounded downtime; retain existing mutation and authentication authorities.

**Non-Goals:** Concurrent workers over unfenced local state, transport-level mutation replay, general high availability, automatic schema rollback, Hosted rollout coordination, or preserving connections when the supervisor itself or host restarts. Initial enablement changes the listener owner once.

## Decisions

### Persistent ingress with a private worker

Use a small ASGI ingress served by the existing HTTP runtime and a private Unix-domain worker socket. The supervisor never imports the server, opens vault stores or creates an OAuth authority. The worker keeps the existing environment, working directory, external state root, issuer and FastMCP home. The public URL and port remain stable. Forward the original request target, body and end-to-end headers; remove hop-by-hop headers, disable environment HTTP proxies and disable redirects/retries. Only the worker authenticates and executes requests.

Linux/WSL managed mode is opt-in at install with `--seamless`. Its unit runs the persistent launcher via `python -m exomem.service_manager serve`. Other platforms and direct invocations are unaffected. A private owner-only directory holds control/worker sockets, the active-release record, transition receipt and a lifetime deployment lock. No control route is registered on the public application. A local JSON control command selects an already staged interpreter; remote callers cannot choose executables.

### Admission and ownership of forwarded requests

`service_ingress.py` owns HTTP forwarding and an admission controller. The controller can pause admission, await finite active requests, disconnect standalone upstream GET streams, install a new upstream generation and resume. A forwarded finite request remains counted until its entire upstream response finishes, including when the client disconnects. A client disconnect drops delivery, not operation ownership; never cancel or replay a forwarded mutation to make an upgrade finish.

During handoff, queued requests are bounded by count, aggregate bytes, per-request bytes, body-read time and queue time. Defaults are 64 queued requests, 64 MiB aggregate, 32 MiB per body and 45 seconds from admission reservation, including body intake; each limit is validated and documented. Normal forwarding streams bodies without imposing this buffering limit. Admission reservation occurs before body intake so an arbitrary number of slow uploads cannot bypass the bounds. Oversize/overflow/expiry requests are never forwarded. Valid MCP request IDs receive a JSON-RPC error stating `not dispatched`; notifications receive a non-success HTTP response. No request body or credential enters logs or durable receipts.

Only a successful authenticated standalone GET `/mcp` SSE response is exempt from finite drain. Preserve its outer connection and send comment heartbeats while the old upstream closes; reconnect it to the new worker with the original credentials before forwarding further content. Never reconnect a POST response stream. Authentication refusal on reattachment closes the stream. Other routes and streams drain normally. Modern POST-only protocol behavior remains unchanged.

### Sequential replacement and durable recovery

The operator stages a new immutable venv and verifies its version before asking the supervisor to change workers. Package download and install occur while the old worker serves. The stable supervisor interpreter is never modified by an ordinary worker upgrade.

One serialized transition has a single 40-second deadline from admission pause and waits up to 30 seconds for finite requests. Shutdown, migration and startup each use the remaining global budget rather than restarting the clock. The 45-second queue budget leaves a response margin; normal tool execution after dispatch remains subject to the client's own timeout. If draining exceeds its budget, resume the old worker without signalling it. Otherwise durably record the target and old worker identity, detach standalone GET upstreams and signal the owned worker process group. Prove the worker and all owned descendants are gone before running target-interpreter offline migration. Use a Linux subreaper and inspect/reap owned descendants so a child that changes process groups cannot escape the stop proof. Bound shutdown, migration and startup separately; failure leaves admission closed with a retained receipt, never a second worker or an implicit downgrade.

After migration, start the target on the private socket and verify its reported `/health` version and `/health/ready` admission status before publishing the active-release record and releasing queued requests exactly once. The public ingress remains available throughout. A control-client disconnect does not cancel the transition. A supervisor restart with an unfinished receipt does not launch the old release; explicit resume rolls forward the recorded target. Worker crashes are surfaced as unavailable and do not cause blind request replay.

The supported supervisor launcher is the systemd unit MainPID, verified against the manager-reported MainPID, ControlGroup and InvocationID and its own process cgroup. Pin KillMode=control-group, SendSIGKILL=yes and a finite TimeoutStopSec in the unit. Before launching children, verify that the service cgroup and its descendants contain no process besides the new supervisor. Persist unit, invocation and boot identity with transition records. This systemd boundary proves cleanup after supervisor death; the subreaper only proves ownership while that supervisor lives. Refuse a standalone launcher or an unproven residual process rather than infer ownership from stale PID snapshots.

The shared service environment is the configuration authority. Changes to that environment or the stable launcher require a maintenance restart and are outside a worker-only upgrade. Preserve the existing offline state migration gate and never infer offline authority from a zero request count alone.

### Operator integration and file boundaries

- `service_ingress.py` and ingress tests: admission, transparent forwarding, streams, disconnect and no-replay behavior.
- `service_manager.py` and lifecycle tests: process ownership, private control socket, durable records, transition budgets and recovery.
- `service_upgrade.py` and CLI tests: immutable staging, target inspection, status/upgrade/resume commands and deployment serialization.
- `server.py`: private Unix socket runner configuration only; direct HTTP and stdio paths retain behavior.
- `scripts/exomem-managed.service`, `install-service.sh`, `upgrade.sh` and service tests: opt-in bootstrap, managed-unit detection and routing to the managed upgrade command. Refuse a legacy in-place install over a managed service.
- `docs/managed-service-upgrades.md`: enablement, normal upgrade, status and recovery procedures.

## Risks / Trade-offs

- A public intermediary or client can time out before the configured handoff budget → document the supported budget, keep downloads outside it and test the same entered client in both protocol modes. An expired queued call gets a definite non-dispatch response; arbitrary client timeouts are not preventable server-side.
- Lifecycle cleanup or offline migration can exceed the budget → retain a failure receipt and prove process ownership before resuming. Never auto-roll back changed state.
- An ingress bug could alter auth or response semantics → preserve end-to-end headers and raw bytes, use a private upstream socket, and test authentication failures, GET/SSE, POST/SSE, keepalive, cancellation and duplicate-execution counters with real processes.
- Staged releases use disk → retain active and failed-candidate environments for recovery; never delete an environment used by the launcher or a live process. Automated retention is deferred.

## Migration Plan

Install a release carrying the managed runtime, then enable the managed Linux unit through the existing stopped-state proof at one explicit bootstrap cutover. Keep the existing service environment and external roots. Verify public auth and live version. Subsequent `scripts/upgrade.sh` calls stage a release and use the private control channel without stopping the unit. Reverting to a direct unit is a separate maintenance operation and cannot downgrade incompatible state.
