## Why

Operators who value a simple, normally single-host Exomem deployment currently have to choose between accepting no standby or operating the full automatic multi-replica lease, shared-session, and edge-routing stack. A manual cold-standby profile should preserve one stable MCP URL and a recoverable laptop copy while moving failover discipline into a guarded local command instead of an always-on HA control plane.

## What Changes

- Add an opt-in, default-off manual cold-standby deployment profile for two Windows hosts with externally replicated vaults and distinct Cloudflare named tunnels.
- Add one repository-owned PowerShell entrypoint for configuration, status, local activation, handoff, and dry-run planning.
- Require fail-closed promotion checks for peer inactivity, replication freshness, local runtime readiness, and stable-hostname routing; ambiguous state leaves services and DNS unchanged.
- Keep one stable public MCP URL by moving its Cloudflare DNS route between the existing per-host tunnels rather than sharing one tunnel connector or retaining the HA Worker as a routing dependency.
- Keep only the active host's Exomem service running; the laptop remains demand-start, and desktop startup is guarded against an already-healthy standby.
- Document staged cutover, connector reauthorization expectations, disaster override semantics, rollback, and the unavoidable stale-copy/network-partition limitations.
- Preserve the existing automatic multi-host writer-lease and Cloudflare HA deployment as a separate supported option; this change does not remove product HA code.

## Capabilities

### New Capabilities

- `manual-cold-standby`: Guarded single-active-host configuration, promotion, handoff, stable-hostname routing, status, rollback, and operator documentation for an externally replicated two-host deployment.

### Modified Capabilities

None.

## Impact

- Adds a generic Windows operator script and focused contract tests under `scripts/` and `tests/`.
- Adds a self-hosted cold-standby runbook and updates `docs/deployment.md` to distinguish manual standby from automatic HA.
- Uses existing Windows service, local readiness, Syncthing, Cloudflare named-tunnel, and DNS-routing surfaces; no model or server-side reasoning capability is introduced.
- Personal deployment migration can later retire its Worker, Durable Object, shared OAuth storage, and writer-lease environment only after direct routing is verified. The repository's optional HA implementation remains intact.
