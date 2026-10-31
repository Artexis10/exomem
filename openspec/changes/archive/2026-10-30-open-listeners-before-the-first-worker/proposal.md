## Why

The managed supervisor opens its public listener only after the first worker reports ready. A cold start that rebuilds the lexical catalogue took 15 and 33 minutes on 30 October 2026, and the port was closed for all of it. Clients behind the tunnel get 502 errors instead of an answer, as they did after a crash before in-place restarts.

## What Changes

- The supervisor opens the public listener, the local listener and the control socket before it starts the first worker.
- Until the first worker reports ready, the phase is `starting`, and ingress refuses every call at once as not dispatched with HTTP 503. Calls do not wait in the admission queue, because a cold warm can outlast any queue budget.
- A first worker that fails to start still stops the supervisor with a failed exit status.

## Impact

- Affected spec: `managed-service-upgrades`.
- Affected code: `src/exomem/service_manager.py`.
- Affected docs: `docs/managed-service-upgrades.md`.
