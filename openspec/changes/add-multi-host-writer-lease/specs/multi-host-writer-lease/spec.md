## ADDED Requirements

### Requirement: Opt-in multi-host coordination
Exomem SHALL preserve its current single-host behavior unless writer-lease coordination is explicitly configured. When coordination is enabled, every replica SHALL use a stable vault identifier and a unique replica identifier when requesting authority to mutate the vault.

#### Scenario: Existing single-host installation
- **WHEN** no writer-lease coordinator is configured
- **THEN** reads and writes behave as they did before this capability was added

#### Scenario: Coordinated replica starts
- **WHEN** a coordinator URL, vault identifier, and replica identifier are configured
- **THEN** the replica participates in writer-lease coordination for that vault

### Requirement: Exactly one active writer
The coordination service SHALL grant at most one unexpired writer lease for a vault at a time. All replicas MAY serve reads, but only the replica holding the current lease SHALL execute mutations.

#### Scenario: Two replicas request authority
- **WHEN** two replicas concurrently request the writer lease for the same vault
- **THEN** at most one receives writer authority and the other remains a readable follower

#### Scenario: Independent vaults
- **WHEN** replicas request leases for different vault identifiers
- **THEN** the leases do not block one another

### Requirement: Lease lifecycle and automatic takeover
The active writer SHALL renew its bounded lease before expiry. A follower SHALL be able to acquire the lease after the previous lease expires or is explicitly released, without manual Syncthing mode changes.

#### Scenario: Healthy writer renews
- **WHEN** the active writer renews before the lease deadline
- **THEN** it remains the writer and the lease deadline advances

#### Scenario: Writer disappears
- **WHEN** the active writer stops renewing and its lease expires
- **THEN** another replica can acquire a new lease and become writer

#### Scenario: Graceful shutdown
- **WHEN** the active writer explicitly releases its lease
- **THEN** another replica can acquire immediately

### Requirement: Stale-writer fencing
Every successful acquisition SHALL return a monotonically increasing fencing token for that vault. A replica SHALL validate current lease ownership at the mutation boundary and SHALL refuse a mutation when its token or ownership is stale.

#### Scenario: Former writer returns after failover
- **WHEN** a former writer attempts a mutation after another replica acquired a newer lease
- **THEN** the former writer is rejected before the mutation leaf executes

### Requirement: Uniform mutation gate
Every command declared as write-capable in the shared command registry SHALL pass through the same lease gate on MCP, REST, and CLI surfaces before its leaf executes. Read-only commands SHALL not require a lease.

#### Scenario: Follower invokes a write through any surface
- **WHEN** a follower invokes a registry command whose write metadata is true through MCP, REST, or CLI
- **THEN** Exomem rejects it before calling the command leaf with a stable `WRITER_LEASE_REQUIRED` error

#### Scenario: Follower invokes a read
- **WHEN** a follower invokes a read-only command
- **THEN** Exomem executes it without acquiring writer authority

### Requirement: Fail-closed mutations and available reads
When coordination is enabled and the coordinator cannot authoritatively confirm ownership, Exomem SHALL fail closed for mutations while continuing to serve local reads.

#### Scenario: Coordinator is unreachable
- **WHEN** a mutation is requested while the coordinator is unreachable
- **THEN** Exomem rejects it with a stable `WRITER_COORDINATOR_UNAVAILABLE` error and does not execute the leaf

#### Scenario: Read during coordinator outage
- **WHEN** a read-only command is requested while the coordinator is unreachable
- **THEN** Exomem serves the read from the local replica

### Requirement: Retry-safe mutation identity
Coordinated mutations SHALL accept an optional idempotency key at the common mutation boundary. A replica SHALL reject reuse of a key for a different command or payload and SHALL return the recorded result for an identical completed mutation without executing the leaf again.

#### Scenario: Client retries the same mutation
- **WHEN** an identical mutation with the same idempotency key is retried on the current writer
- **THEN** Exomem returns the recorded result without applying the mutation twice

#### Scenario: Key is reused for different input
- **WHEN** an idempotency key is reused with a different command or payload
- **THEN** Exomem rejects it with `IDEMPOTENCY_KEY_REUSED`

### Requirement: Role and lease health visibility
Exomem SHALL expose machine-readable coordination status containing whether coordination is enabled, the local role, vault and replica identifiers, current holder, lease expiry, fencing token, and coordinator health without exposing vault content or coordinator credentials.

#### Scenario: Operator checks a follower
- **WHEN** coordination status is requested on a healthy follower
- **THEN** the response identifies the current writer and reports the local role as `follower`

#### Scenario: Operator checks an uncoordinated instance
- **WHEN** status is requested without coordination configured
- **THEN** the response reports coordination disabled and the local role as `standalone`

### Requirement: Provider-neutral coordinator contract
The lease client SHALL communicate through a documented HTTP JSON contract that can be implemented by a strongly consistent managed service or a self-hosted service. Requests SHALL authenticate when a token is configured, and the contract SHALL exchange coordination metadata only, never vault content.

#### Scenario: Self-hosted coordinator
- **WHEN** an operator supplies a compatible self-hosted coordinator URL and credentials
- **THEN** Exomem coordinates writers without depending on a specific cloud provider

### Requirement: Replication-mechanism independence
Writer coordination SHALL NOT depend on Syncthing or any other specific vault replication product. The operator SHALL remain responsible for providing shared storage or an external replication mechanism appropriate to the deployment.

#### Scenario: Self-hoster uses a different replication layer
- **WHEN** replicas receive the same canonical vault through shared storage, Unison, rsync, Git-based automation, or another external mechanism
- **THEN** Exomem applies the same writer-lease behavior without requiring Syncthing APIs or configuration
