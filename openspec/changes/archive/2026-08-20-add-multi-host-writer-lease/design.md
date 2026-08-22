## Context

Exomem stores its canonical state as Markdown plus derived indexes and logs. Syncthing is excellent replication but does not provide a strongly consistent distributed lock. With two writable replicas, simultaneous command execution can create conflict copies or diverging index/log state even when most human edits are rare. The command registry already marks every exposed operation as read-only or write-capable and generates MCP, REST, and CLI adapters, giving us one reliable policy boundary.

The personal topology is a desktop and laptop that both expose Exomem connectors and replicate the vault. The desktop should normally write; the laptop should take over after the desktop disappears. The mechanism must also work for ordinary self-hosted replicas and must not depend on a language model making coordination decisions.

## Goals / Non-Goals

**Goals:**

- Keep all replicas readable while permitting exactly one active writer per vault.
- Automatically renew, expire, release, and transfer writer authority.
- Gate every registry-declared mutation consistently across MCP, REST, and CLI.
- Fail closed for writes when authority cannot be proven, while preserving reads.
- Make retries safe and expose clear role/health diagnostics.
- Use a small provider-neutral HTTP contract suitable for a strongly consistent cloud primitive or self-hosted service.
- Remain independent of the operator's vault replication or shared-storage product.
- Leave existing single-host installs unchanged unless explicitly enabled.

**Non-Goals:**

- Building a CRDT, offline multi-writer merge engine, or conflict resolver.
- Treating Syncthing folder modes or filesystem lock files as distributed consensus.
- Implementing or prescribing a vault replication engine.
- Replicating coordinator credentials or runtime state inside the vault.
- Moving vault content through the coordinator.

## Decisions

### A registry-level invocation boundary owns coordination

Add a shared `CommandInvoker` used by generated MCP, REST, and CLI adapters. It receives a `Command`, injected leaf arguments, keyword arguments, and optional idempotency key. For read-only commands it directly invokes the leaf. For write-capable commands it validates/acquires the lease, then invokes through the idempotency store. This avoids duplicating policy in dozens of leaves and makes command metadata the auditable source of truth.

### The coordinator contract is HTTP JSON and strongly consistent by contract

The client uses a base URL plus bearer token and the following conceptual operations:

- `POST /v1/vaults/{vault}/lease/acquire` with replica ID and TTL.
- `POST /v1/vaults/{vault}/lease/renew` with replica ID and fencing token.
- `POST /v1/vaults/{vault}/lease/release` with replica ID and fencing token.
- `GET /v1/vaults/{vault}/lease` for authoritative status.

The coordinator atomically grants one unexpired holder and increments a persistent fencing token on each new acquisition. Exomem validates ownership at each mutation boundary; it never relies only on cached role state. The contract, not a particular vendor, is normative. A reference coordinator service backed by SQLite transactions will be included for self-hosting and tests; managed implementations can use Durable Objects, transactional SQL, Consul, etcd, or an equivalent linearizable primitive.

### Replication remains an external substrate

The lease service elects a writer; it does not copy the vault. Syncthing is one
deployment example, not a dependency. Self-hosters may use shared storage, Unison,
rsync automation, Git-based replication, or another mechanism whose consistency
and recovery trade-offs they understand. Followers can only read data their chosen
replication layer has delivered.

Coordinator reachability is independent again: automatic takeover across different
networks requires a coordinator endpoint both replicas can reach. A LAN-only
replication link may converge later; until it does, the active replica operates on
its latest local copy.

### Acquisition is demand-driven with optional background renewal

On a write request, a replica asks to acquire or validate authority. The server process renews a held lease in the background and releases it during graceful shutdown where possible. Demand-driven acquisition keeps CLI invocations correct even though they are short-lived. Preferred-writer configuration affects who proactively acquires; it never overrides an existing valid holder.

### Coordination failure is explicit and safe

An authoritative response naming another holder produces `WRITER_LEASE_REQUIRED`; transport failure, malformed responses, or coordinator server errors produce `WRITER_COORDINATOR_UNAVAILABLE`. Both occur before the leaf runs. REST maps these errors to 409 and 503 respectively; CLI uses its existing structured envelope; MCP receives the stable code in the raised operation error. Reads never call the coordinator.

### Idempotency is local, durable, and scoped to the vault

The invoker hashes the command name and normalized arguments. When an idempotency key is provided, it stores pending/completed records in an Exomem runtime sidecar outside the replicated vault content. Identical completed calls return the saved result; mismatched reuse fails. The key is optional because existing callers do not supply it. Cross-replica failover safety requires clients to retry against the new writer with the same key and the coordinator-backed implementation may later centralize records; this slice prevents the common same-writer/network retry duplication and defines the boundary without pretending local files are distributed consensus.

### Configuration is explicit and default-off

Coordination enables only when `EXOMEM_WRITER_LEASE_URL` is set. `EXOMEM_WRITER_LEASE_VAULT_ID`, `EXOMEM_WRITER_LEASE_REPLICA_ID`, bearer token, TTL, request timeout, preferred role, and state directory are independently configurable. Missing required identity values while a URL is set is a startup/configuration error. Secrets are read from the environment and excluded from status output.

### Status is a read-only product command

Add a `coordination_status` read command to MCP, REST, and CLI. It reports standalone/writer/follower/unknown, identifiers, holder, expiry, fencing token, and coordinator health. It performs a bounded status lookup when enabled but never blocks unrelated reads.

## Risks / Trade-offs

- **Coordinator availability becomes a write dependency.** This is intentional: uncertain authority must not produce split brain. Mitigation: reads remain local and available; operators can deploy a highly available strongly consistent coordinator.
- **Lease expiry during a long mutation.** The invoker validates immediately before execution, and the server renewer maintains the lease. Some batch operations may outlive a TTL. Mitigation: configure TTL comfortably above worst-case leaf duration and renew while the process owns the lease; future storage-level fencing can strengthen this further.
- **A fencing token cannot stop arbitrary direct filesystem edits.** The guarantee covers Exomem command surfaces. Mitigation: operational docs keep Syncthing followers receive-only and discourage direct edits outside the active writer.
- **Local idempotency does not deduplicate across replicas.** It still solves ordinary transport retries and establishes a stable API. Full cross-replica idempotency would need coordinator storage and is deferred rather than falsely claimed.
- **SQLite reference service is a single coordinator node.** It is strongly consistent on one node but not highly available. It is appropriate for self-hosting; managed linearizable backends provide HA.

## Migration Plan

1. Ship the capability disabled by default; existing deployments require no changes.
2. Deploy a compatible coordinator and create a bearer token.
3. Assign the same vault ID and distinct replica IDs on desktop and laptop.
4. Configure both replicas with the coordinator URL; set the desktop as preferred writer and laptop as follower candidate.
5. Verify `coordination_status`, perform a desktop write, stop the desktop, wait one TTL, and verify laptop takeover.
6. For automatic two-way failover, set the replicated vault folder to Send & Receive on both hosts. Avoid direct Obsidian/filesystem edits on the follower because those bypass Exomem's lease gate.

Rollback is setting `EXOMEM_WRITER_LEASE_URL` empty and restarting. That restores legacy standalone behavior; operators must first ensure only one replica is writable to avoid reintroducing split brain.

## Open Questions

- Should a later version store idempotency records in the coordinator to guarantee deduplication across failover?
- Should preferred-writer priority include a voluntary hand-back policy, or remain sticky until expiry/release to avoid unnecessary churn?
- Which managed coordinator adapter should be documented first: Cloudflare Durable Objects or transactional Postgres?
