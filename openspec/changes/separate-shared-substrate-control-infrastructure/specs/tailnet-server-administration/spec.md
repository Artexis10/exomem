## Purpose

Make server administration independent of changing home IP addresses through a shared, governed tailnet transport while preserving existing SSH custody and recovery.

## ADDED Requirements

### Requirement: Administration policy has one shared source
Organisation-owned servers SHALL use a versioned shared administration policy and enrollment role. NetBird Cloud SHALL be the initial organisation administration provider; its selected account and plan SHALL be verified before enrollment. Product repositories SHALL consume that source without independently maintained copies. Adoption SHALL identify the exact account, server identity, peer-group ownership and authorised administrator devices before enrollment. An executing client being connected to a VPN MUST NOT establish the intended server enrollment identity. NetBird SHALL also become the owner-device VPN standard through staged migration of phone, workstation and Moshi connections. Existing Tailscale dependencies SHALL remain available until their replacement workflows pass. Administrator, server and personal-service groups SHALL retain separate explicit access rules; common VPN membership MUST NOT imply unrestricted access.

#### Scenario: Tailnet identity is unresolved
- **WHEN** an operator cannot establish the intended tailnet and enrollment authority
- **THEN** enrollment remains held and the existing managed administration route remains available

### Requirement: Tailnet transport preserves managed SSH authentication
Routine administration SHALL use ordinary OpenSSH over the selected private mesh with the canonical Bitwarden-managed SSH identity and verified host pin. Password-based network authentication MUST remain disabled. Provider SSH interception or a separate provider SSH server MUST NOT be enabled by this adoption. Explicit server peer groups and least-privilege network policy SHALL admit authorised administration sources to the intended port and deny unauthorised sources; a default allow-all policy MUST NOT defeat those restrictions. Enrollment credentials and daemon state MUST remain private; secrets MUST NOT appear in command arguments, task output or repository content. Successful enrollment/configuration commands MUST NOT substitute for live account/peer identity and effective configuration evidence.

#### Scenario: An administrator connects over the VPN
- **WHEN** an authorised administrator uses the managed server profile after adoption
- **THEN** the connection traverses the selected tailnet and verifies the existing SSH key identity and host pin

#### Scenario: A device lacks administration permission
- **WHEN** a device outside the authorised administration sources tries the server's management port
- **THEN** the connection is denied without widening the tailnet policy or substituting password authentication

### Requirement: Enrollment does not change application routing
The administration role MUST NOT advertise subnet or exit-node routes, accept unrelated subnet routes, change host DNS management or migrate database/application traffic as an enrollment side effect. Application endpoints and existing private connectivity SHALL remain unchanged.

#### Scenario: Existing device connectivity survives staged migration
- **WHEN** company administration and owner-device connections migrate from Tailscale to NetBird
- **THEN** actual address, route and DNS checks plus fresh personal-service and managed-server connections prove each replacement workflow
- **AND** desktop coexistence and mobile tunnel switching are checked on their adopted platforms, without requiring two active phone VPN tunnels
- **AND** each Tailscale dependency is retired only after its NetBird replacement passes
- **AND** the existing managed SSH command and its key/host pin remain usable without manually choosing a VPN for every connection

#### Scenario: A control host joins the tailnet
- **WHEN** the control host is enrolled for administration
- **THEN** its existing database clients keep their selected endpoints and private routes and no new subnet route is advertised

#### Scenario: Owner mobile clients resolve private peer names
- **WHEN** an explicitly selected owner mobile device adopts NetBird
- **THEN** the shared infrastructure policy supplies its primary DNS resolver so private peer names resolve through NetBird
- **AND** DNS distribution grants no additional service access and does not change concurrent workstation or client-owned DNS
- **AND** an actual named phone connection proves acceptance before the old Tailscale entry is retired

### Requirement: Ingress restriction follows verified recovery
Adoption SHALL preserve existing narrow SSH access until a second managed connection over the intended tailnet succeeds. Provider and guest firewall changes SHALL be made through their respective owning configuration only after allowed/denied connectivity checks and provider-console recovery are verified. Disposable-host acceptance SHALL demonstrate service recovery after reboot. A failed prerequisite MUST leave the existing route available and rollout incomplete. Recovery policy MUST NOT depend on a remembered home IP.

#### Scenario: Tailnet connectivity fails before cutover
- **WHEN** enrollment or managed VPN connectivity fails
- **THEN** public SSH restrictions are not applied and the operator can continue through the existing narrow route

#### Scenario: An enrolled host restarts
- **WHEN** a disposable acceptance host reboots with the selected configuration
- **THEN** authorised managed tailnet SSH resumes without a new enrollment or a public SSH opening

### Requirement: Concurrent user transport retains existing authentication
A client-owned native daemon SHALL remain unchanged while owner services use
a separate user-owned company instance. Incoming company traffic SHALL use
explicit owner-device to owner-service port grants and an opted-in loopback
listener for the existing authenticated service. Network membership alone
MUST NOT permit all service ports. A workstation installer SHALL retain a live
legacy listener during staged migration and SHALL work after its retirement.
The company instance SHALL advertise only address families supported by that
listener. Reconnection SHALL leave outbound proxy traffic using the active
company network stack without restarting a client-owned native daemon.

#### Scenario: Userspace VPN reaches workstation SSH
- **WHEN** an enrolled owner device reaches the owner-service TCP2222 grant
- **THEN** the separate VPN forwards to the existing key-only loopback SSH listener and other service ports remain denied
- **AND** fresh inbound and outbound connections pass after transport reconnection; management connectivity alone does not prove the service path

#### Scenario: Existing peer identity survives userspace forwarding
- **WHEN** the workstation topology opts into the separate userspace VPN
- **THEN** the source-managed peer wiring retains its existing key, admits loopback ingress for that key, and pins the host identity observed through the prior trusted route
- **AND** a selected loopback SOCKS path cannot reuse an ambient SSH master or another host-key provider to bypass the enrolled transport or pin
- **AND** the shared Bitwarden topology drives both workstations while legacy entries retain their current behavior

#### Scenario: Migration retains legacy access
- **WHEN** the operator opts into the loopback listener while the legacy VPN address is live
- **THEN** the source-managed installer retains that address and adds only loopback, without a wildcard listener or authentication replacement
