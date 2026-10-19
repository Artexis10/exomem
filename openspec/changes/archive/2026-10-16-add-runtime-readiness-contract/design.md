## Context

The HA edge already asks a Durable Object for the active writer lease before routing MCP tool calls. It currently treats OAuth metadata as health when no holder exists and trusts any active holder without knowing which Exomem runtime is installed. Repository state and the installed service package can drift, while hosted rolling deployments legitimately run different releases for a bounded period.

The existing `/health` route is liveness only and must remain cheap and independent of vault, coordinator, or model state. The existing writer lease provides a fencing token that changes whenever ownership transfers, which is a natural generation key for cached runtime admission.

## Goals / Non-Goals

**Goals:**

- Expose a small, non-secret readiness contract usable by self-hosted and hosted orchestration.
- Distinguish release identity from behavioral compatibility.
- Prevent an incompatible, stateful, or coordination-broken replica from receiving an HA tool call.
- Preserve the steady-state MCP latency path after one admission probe per lease generation.
- Give operators a read-only doctor workflow that detects drift before failover.

**Non-Goals:**

- Automatically update Exomem, Windows services, containers, or repositories.
- Require exact release equality across replicas.
- Couple Exomem to Syncthing or a particular deployment platform.
- Turn readiness into a model, retrieval, or vault-content check.
- Replay a failed or ambiguous MCP tool call to another replica.

## Decisions

### Separate liveness from readiness

Keep `/health` unchanged. Add `GET /health/ready`, returning HTTP 200 when the runtime can be admitted and 503 when it cannot. The JSON contains only service/release metadata, integer `runtime_contract`, stateless HTTP transport identity, replica identity, coordination health/role, `takeover_eligible`, and stable reason codes. It never returns vault paths, vault IDs, credentials, or content.

Alternative considered: extend `/health`. Rejected because liveness must continue answering during coordinator outages so supervisors do not restart a healthy process merely because it cannot safely take writes.

### Gate on a compatibility contract, not semantic version

`release` is diagnostic. `runtime_contract` is the routing contract and changes only for incompatible runtime/edge behavior. The Worker accepts a configurable comma-separated set of contracts so a rolling deployment can temporarily admit old and new contracts together.

Alternative considered: require matching Exomem versions. Rejected because patch releases and compatible rolling deployments should not cause downtime.

### Require HA-specific eligibility at the edge

The Worker validates readiness status, supported contract, stateless transport, expected replica ID, takeover eligibility, and (by default) enabled/healthy coordination. Standalone Exomem remains ready for single-host deployments; the HA Worker applies the stricter coordination requirement through configuration.

### Cache admission by lease fencing token

The Durable Object stores the admitted readiness summary only when its holder and fencing token still match the active lease. Subsequent tool calls reuse that admission after re-evaluating it against the Worker's current supported-contract configuration. Lease expiry, release, or ownership change clears the admission.

If an active lease has no admission, the Worker probes that holder once and records the result. With no holder, it probes replicas concurrently, chooses one eligible origin, and forwards the tool call exactly once. The next request admits the newly acquired lease generation. This avoids a readiness network round trip on the steady-state path without using unsafe module-global request state.

Alternative considered: probe readiness on every tool call. Rejected because it adds avoidable origin latency to cached retrievals. Module-global caching was also rejected because Workers reuse isolates across requests and it would not be authoritative across isolates.

### Keep doctor network access explicit

Add `ha` as a doctor profile. Local HA configuration checks run offline. Cross-replica readiness checks run only with `--probe` and explicit repeatable `--replica-url` values (or `EXOMEM_HA_REPLICA_URLS`). Release differences produce warnings; unsupported contracts, non-stateless transport, disabled/unhealthy coordination, duplicate replica IDs, or ineligible replicas fail.

## Risks / Trade-offs

- **A cached admission can outlive a process restart briefly** → admission is bound to the lease fencing token; normal release/expiry invalidates it, and ambiguous tool calls still never replay.
- **A direct-origin caller could acquire a lease before edge admission** → the first stable-edge tool call to that holder must admit its readiness or fail closed.
- **Public readiness metadata can fingerprint a release** → the payload is intentionally content-free and small; deployments that require secrecy can keep origin routes private.
- **Contract bumps require rollout choreography** → configure the Worker to accept both contracts, roll replicas, then remove the old contract.
- **Coordinator outage makes a coordinated replica unready for takeover** → this is intentional fail-closed behavior; `/health` remains live and reads through already-routed safe traffic remain available.

## Migration Plan

1. Ship the readiness endpoint and HA doctor support with runtime contract `1`.
2. Upgrade every replica and verify `/health/ready` directly while the Worker still runs the previous routing release.
3. Deploy the Worker with `SUPPORTED_RUNTIME_CONTRACTS = "1"` and coordination enforcement enabled.
4. Run the HA doctor probe and a live MCP read/write test.
5. Roll back the Worker if admission unexpectedly rejects a replica; `/health` and the existing services remain untouched.

## Open Questions

None for contract version 1. Future hosted infrastructure may move the same readiness checks behind private service bindings or platform-native readiness probes without changing the payload semantics.
