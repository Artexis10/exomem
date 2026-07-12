## Why

Exomem's HA edge can now avoid ambiguous tool-call replay, but a replica whose checkout and installed runtime have drifted can still be selected during failover. Hosted rollout needs the same protection without requiring exact release equality during normal rolling deployments.

## What Changes

- Add a small, unauthenticated runtime readiness endpoint distinct from the existing liveness endpoint.
- Report non-secret release identity, a compatibility contract version, HTTP transport mode, coordination state, and whether the replica is eligible for takeover.
- Gate HA tool-call origin selection on readiness and compatibility instead of OAuth discovery alone.
- Fail closed when an active holder is unavailable, incompatible, or ineligible; never replay the tool call to another replica.
- Add an HA doctor profile that compares configured replica readiness and explains release drift versus true incompatibility.
- Document that deployment infrastructure owns image pinning, rollout, and rollback; Exomem does not auto-update replicas or require exact release equality.

## Capabilities

### New Capabilities
- `runtime-readiness-contract`: Machine-readable runtime readiness, compatibility-aware HA admission, and fail-closed routing behavior for self-hosted and hosted deployments.

### Modified Capabilities
- `install-readiness`: Extend the read-only doctor command with an HA profile that validates replica readiness and compatibility without mutating services or vaults.

## Impact

- Exomem HTTP server routes and runtime metadata.
- Writer-lease/coordination status used to determine takeover eligibility.
- Cloudflare HA Worker origin probes and routing tests.
- Doctor CLI, tests, deployment documentation, and Worker example configuration.
- No new model, storage backend, updater, or Syncthing dependency.
