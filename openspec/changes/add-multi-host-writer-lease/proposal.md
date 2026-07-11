## Why

Exomem can run against Syncthing-replicated vaults on multiple hosts, but each server currently assumes it may write locally; concurrent mutations can therefore fork canonical Markdown, `index.md`, and `log.md` before Syncthing detects the conflict. Multi-host and self-hosted deployments need automatic failover without allowing two replicas to become writers at the same time.

## What Changes

- Add an opt-in, strongly consistent writer lease that elects exactly one writable Exomem replica per vault while allowing every healthy replica to continue serving reads.
- Gate every mutation through the shared command leaf so MCP, REST, and CLI enforce the same leader/follower contract without duplicated surface logic.
- Renew leases with a bounded TTL, release them on graceful shutdown, and allow another replica to take over automatically after expiry.
- Make follower and coordinator-unavailable write failures stable and machine-readable, including retry/leader metadata where available; reads remain available when coordination fails.
- Add durable per-replica idempotency protection for transport/process retries and a stable idempotency boundary that can later move to shared coordinator storage for cross-replica failover.
- Expose replica role and lease health for reverse proxies, tunnels, health checks, and operators so one stable MCP endpoint can route to the active writer.
- Keep coordination default-off: ordinary single-host installations retain current behavior and gain no required external service. When explicitly enabled but unavailable, coordination soft-fails operationally—reads stay healthy and writes fail closed rather than risking split brain.
- Document provider-neutral self-hosting requirements and a reference coordination deployment; the lease carries only vault/replica identity and expiry metadata, never vault content.
- Explicitly exclude offline multi-writer merging, CRDT conversion, and Syncthing lockfiles from this change.

## Capabilities

### New Capabilities

- `multi-host-writer-lease`: Opt-in single-writer election, lease lifecycle, mutation gating, idempotent failover, role health, and provider-neutral self-hosted coordination for replicated Exomem vaults.

### Modified Capabilities

None.

## Impact

- Affects command dispatch around write-capable registry leaves, MCP/REST/CLI error envelopes, service startup/shutdown lifecycle, and health/readiness reporting.
- Adds a coordination abstraction plus at least one strongly consistent shared backend suitable for Desktop/laptop and general self-hosted deployments.
- Adds configuration for vault identity, replica identity, lease TTL/renewal, coordinator selection, and optional leader routing metadata.
- Requires concurrency, expiry, takeover, local idempotency, coordinator-outage, and single-host compatibility tests.
- Requires deployment and operator documentation for stable-endpoint routing and safe multi-replica setup.
