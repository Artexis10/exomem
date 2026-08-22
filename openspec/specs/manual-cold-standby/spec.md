# manual-cold-standby Specification

## Purpose
TBD - created by archiving change add-manual-cold-standby. Update Purpose after archive.
## Requirements
### Requirement: Cold standby is explicit and default-off

The system SHALL provide a manual cold-standby deployment profile that is inactive unless an operator explicitly configures it. Installing or upgrading Exomem without configuring this profile MUST NOT change Windows service startup, Cloudflare tunnel configuration, DNS, OAuth configuration, writer coordination, or vault files.

#### Scenario: Ordinary install ignores cold standby

- **WHEN** Exomem is installed or upgraded without running the cold-standby configuration action
- **THEN** existing service, tunnel, DNS, OAuth, coordination, and vault state remain unchanged

### Requirement: One generic local operator command

The project SHALL provide one Windows PowerShell entrypoint with `Configure`, `Status`, `Activate`, and `Handoff` actions. It SHALL load validated host-specific configuration from outside the repository, MUST NOT commit or print credentials, and SHALL support `-WhatIf` for every mutating action.

#### Scenario: Dry run plans without mutation

- **WHEN** an operator invokes a mutating action with `-WhatIf`
- **THEN** the command evaluates the same preconditions and reports the ordered plan
- **AND** it does not change services, scheduled tasks, files, tunnel configuration, DNS, or public routing

#### Scenario: Invalid local configuration

- **WHEN** required host, tunnel, health, service, or Syncthing configuration is missing or inconsistent
- **THEN** the command exits nonzero with actionable remediation
- **AND** no operational state is changed

### Requirement: Status is bounded and read-only

`Status` SHALL report local and peer service health, stable-endpoint health, Syncthing convergence, desired-host intent, named-tunnel availability, and the next safe operator action in both human-readable and JSON forms. It MUST remain read-only even when checks fail or external dependencies are unavailable.

#### Scenario: Status observes a healthy desktop primary

- **WHEN** the desktop service is ready, the laptop service is stopped, replication is converged, and the stable endpoint is healthy
- **THEN** status identifies the desktop as the active host
- **AND** it reports laptop activation as unnecessary
- **AND** it changes no local or remote state

#### Scenario: Status dependency is unavailable

- **WHEN** Syncthing, Cloudflare, or a health endpoint cannot be queried
- **THEN** status reports that dependency as unknown or unavailable with remediation
- **AND** it does not infer that the peer is safely offline

#### Scenario: Both Exomem origins appear ready

- **WHEN** local and peer health probes both report ready
- **THEN** status reports an unsafe ambiguous state rather than selecting an active host
- **AND** it offers no mutating next action

### Requirement: Stable routing uses distinct named tunnels

The profile SHALL preserve one stable MCP hostname and SHALL route it to exactly one of two distinct per-host Cloudflare named tunnels. Both named-tunnel connectors MAY remain running concurrently. When the profile is active, each direct operational hostname MUST forward only bounded health/readiness paths and MUST NOT expose MCP, OAuth, REST mutation, or artifact-transfer paths.

#### Scenario: Both tunnel connectors are running

- **WHEN** desktop and laptop `cloudflared` services are both connected to their distinct named tunnels
- **THEN** the stable hostname resolves through only the selected host tunnel
- **AND** Cloudflare does not treat the two hosts as connectors in one load-balanced tunnel replica set

#### Scenario: Direct hostname receives an MCP request

- **WHEN** a client sends an MCP request to a host's direct operational hostname
- **THEN** tunnel ingress refuses or does not forward that request to Exomem
- **AND** the same hostname can still serve the configured health/readiness probe

### Requirement: Activation fails closed on unsafe evidence

Normal `Activate` SHALL validate command access, local role, local service state, local runtime readiness, Syncthing API access, local folder convergence, configured peer completion, repeated peer health, stable-endpoint health, desired-host intent, and the redacted shared-configuration fingerprint published by each host. A healthy peer, stale or unknown replication state, conflicting intent, shared-configuration mismatch, unavailable required evidence, or ambiguous network state MUST refuse activation before public routing changes.

#### Scenario: Peer is still serving

- **WHEN** repeated probes show that the peer Exomem origin is ready
- **THEN** activation exits nonzero before starting the local Exomem service or changing DNS

#### Scenario: Replication is not converged

- **WHEN** the local folder has pending items or bytes, or configured peer completion is below the required terminal state
- **THEN** activation exits nonzero with Syncthing remediation
- **AND** service and routing state remain unchanged

#### Scenario: Unreachable peer conflicts with desired-host intent

- **WHEN** the peer is unreachable but the latest converged desired-host marker names that peer
- **THEN** normal activation treats the state as ambiguous and refuses
- **AND** it does not describe peer unreachability as proof of shutdown

#### Scenario: Stable endpoint is already healthy

- **WHEN** the stable endpoint is healthy before local activation begins
- **THEN** normal activation refuses because another serving origin may still be active
- **AND** it does not start the local service or change DNS

#### Scenario: Shared authentication configuration has drifted

- **WHEN** the local and peer manifests disagree on the stable base URL, OAuth application/callback, allowed identity, or JWT signing-key fingerprint
- **THEN** activation exits nonzero before starting the local service or changing DNS
- **AND** diagnostics identify the mismatched field without printing the raw identity or signing key

### Requirement: Successful activation follows safe ordering

After all normal guards pass, `Activate` SHALL record the next desired-host generation, start local Exomem, require local readiness, route the stable hostname to the local named tunnel, verify that Cloudflare records the expected tunnel target, and verify the public readiness and OAuth discovery paths in that order. The bounded readiness response SHALL expose a configured non-secret host ID. `Activate` SHALL not report success until the public response reports the expected local host ID and the expected stable base URL.

#### Scenario: Laptop activation succeeds

- **WHEN** the desktop is inactive, replication is converged, intent permits laptop activation, local Exomem becomes ready, and the stable route verifies
- **THEN** the laptop becomes the only running Exomem service
- **AND** the unchanged stable MCP hostname serves the laptop
- **AND** the operation journal records a successful terminal

#### Scenario: Public verification fails after route change

- **WHEN** local readiness succeeds but the stable public readiness or OAuth discovery check fails after DNS mutation
- **THEN** activation restores the previous route when it is known
- **AND** it stops the newly started local service
- **AND** it reports whether rollback completed or operator recovery is required

#### Scenario: Old tunnel remains healthy after route mutation

- **WHEN** Cloudflare route mutation was requested but the public readiness response still reports the previous host ID
- **THEN** activation does not commit success
- **AND** it compensates or reports operator recovery even if readiness and OAuth metadata are otherwise healthy

#### Scenario: Interrupted activation is replayed

- **WHEN** a local journal shows interruption after route mutation and before terminal commit
- **THEN** recovery may complete only if the journaled phase and previous route are consistent, Cloudflare reports the stable record on the local tunnel, and public readiness reports the local host ID
- **AND** any mismatch enters compensation or operator recovery rather than a fresh activation

### Requirement: Desktop boot recovery is guarded

`Configure` SHALL set Exomem to demand-start on both hosts. It SHALL keep laptop activation manual and SHALL support a desktop current-user logon task, with a configurable delay, that invokes a guarded activate-if-unserved action only after network and Syncthing checks. Any evidence of active laptop service or ambiguous intent, replication, peer, or stable-endpoint state MUST leave desktop Exomem stopped.

#### Scenario: Desktop boots while laptop serves the stable URL

- **WHEN** the desktop startup task runs and either the laptop health endpoint or stable endpoint is healthy
- **THEN** the desktop Exomem service remains stopped
- **AND** no DNS route is changed

#### Scenario: Desktop boots as intended primary

- **WHEN** converged intent names the desktop, the laptop is inactive, required health checks are conclusive, and Syncthing is converged
- **THEN** the startup task may activate desktop through the same guarded transition used by the interactive command

### Requirement: Handoff preserves a single active service

`Handoff` SHALL require source and configured peer replication completion and shared-configuration parity, record intent for the target, and then require evidence that the peer received that exact new intent generation before gracefully stopping the local Exomem service and moving the stable route to the target tunnel. It MUST NOT remotely start the target service or keep the source service running while waiting for target activation. If routing or verification fails after source shutdown, compensation SHALL restore the prior route and restart the source only after proving the target remains inactive; otherwise it SHALL keep the source stopped and report operator recovery.

#### Scenario: Laptop hands back to desktop

- **WHEN** laptop is active and both Syncthing replicas are complete
- **THEN** handoff records desktop intent and proves that generation reached the desktop before stopping laptop Exomem and selecting the desktop tunnel
- **AND** it reports a bounded outage until guarded desktop activation succeeds

#### Scenario: Peer replication completion is unknown

- **WHEN** handoff cannot prove the configured peer has received the latest replicated state
- **THEN** normal handoff refuses before stopping the source service or moving the route

#### Scenario: Route movement fails after source shutdown

- **WHEN** handoff has stopped the source but cannot move or verify the stable route on the target tunnel
- **THEN** it restores the prior route and restarts the source only if target inactivity is conclusively proven
- **AND** otherwise it leaves the source stopped and reports the exact recovery steps without risking two active services

### Requirement: Disaster override is explicit and narrow

A forced activation SHALL require a force switch plus a literal acknowledgement of split-brain and data-loss risk. It MAY bypass only peer-unreachable, conflicting-intent, or Syncthing-completion guards. It MUST NOT bypass configuration validation, local runtime readiness, DNS mutation success, or public verification, and its terminal output SHALL enumerate every bypassed guard.

#### Scenario: Force switch without acknowledgement

- **WHEN** an operator supplies the force switch without the required literal acknowledgement
- **THEN** activation refuses before changing state

#### Scenario: Acknowledged disaster activation

- **WHEN** an operator supplies both required override inputs and local runtime and routing checks succeed
- **THEN** activation may proceed despite explicitly named peer or replication uncertainty
- **AND** the journal and terminal output record each bypassed guard

### Requirement: Connector identity survives host switches

Both hosts SHALL use the same stable `EXOMEM_BASE_URL`, GitHub OAuth application and callback, allowed GitHub identity, and JWT signing key. The operator SHALL compare redacted fingerprints of this effective configuration before activation or handoff and MUST NOT print or replicate raw credentials or identity values. Switching hosts MUST NOT require changing or recreating the MCP connector definition. Documentation SHALL explain that host-local OAuth session and dynamic-client state may require reconnect/registration and GitHub reauthorization after a switch once shared OAuth storage is removed.

#### Scenario: Existing connector reaches newly active host

- **WHEN** DNS now selects the other host and the client's prior session is absent there
- **THEN** the existing connector URL remains valid
- **AND** the operator is instructed to reconnect or reauthorize the existing connector rather than create a second definition

#### Scenario: Both hosts share the connector identity contract

- **WHEN** the operator compares the two host manifests before a transition
- **THEN** normalized base URL, OAuth application/callback, allowed-identity fingerprint, and signing-key fingerprint match
- **AND** no raw identity, client secret, or signing key appears in output or replicated state

### Requirement: Migration and rollback remain staged

The runbook SHALL separate tooling installation from live cutover, require backups and convergence before removing HA environment, detach the HA Worker's stable-hostname route binding before direct tunnel routing, verify the direct desktop path before retiring the personal Worker/Durable Object deployment, and retain a bounded rollback window. It SHALL explicitly state that the existing product HA implementation remains supported and that unreplicated bytes cannot be recovered by standby activation.

#### Scenario: Direct cutover fails verification

- **WHEN** the standalone desktop path fails OAuth, readiness, authenticated read, or governed-write verification
- **THEN** the runbook directs the operator to restore the backed-up HA environment and stable Worker route
- **AND** it does not authorize deleting the Worker, Durable Object, or rollback secrets
