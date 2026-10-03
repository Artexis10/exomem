## Purpose

Make server administration independent of changing home IP addresses through a shared, governed tailnet transport while preserving existing SSH custody and recovery.

## ADDED Requirements

### Requirement: Administration policy has one shared source
Organisation-owned servers SHALL use a versioned shared administration policy and enrollment role. Product repositories SHALL consume that source without independently maintained copies. Adoption SHALL identify the exact intended tailnet, server identity, tag ownership and authorised administrator devices before enrollment. An executing client being connected to a tailnet MUST NOT establish the intended server enrollment identity.

#### Scenario: Tailnet identity is unresolved
- **WHEN** an operator cannot establish the intended tailnet and enrollment authority
- **THEN** enrollment remains held and the existing managed administration route remains available

### Requirement: Tailnet transport preserves managed SSH authentication
Routine administration SHALL use ordinary OpenSSH over the tailnet with the canonical Bitwarden-managed SSH identity and verified host pin. Password-based network authentication MUST remain disabled. Tailscale SSH interception MUST NOT be enabled by this adoption. Server tags and least-privilege network policy SHALL admit authorised administration sources to the intended port and deny unauthorised sources. Enrollment credentials and daemon state MUST remain private; secrets MUST NOT appear in command arguments, task output or repository content.

#### Scenario: An administrator connects over the VPN
- **WHEN** an authorised administrator uses the managed server profile after adoption
- **THEN** the connection traverses the selected tailnet and verifies the existing SSH key identity and host pin

#### Scenario: A device lacks administration permission
- **WHEN** a device outside the authorised administration sources tries the server's management port
- **THEN** the connection is denied without widening the tailnet policy or substituting password authentication

### Requirement: Enrollment does not change application routing
The administration role MUST NOT advertise subnet or exit-node routes, accept unrelated subnet routes, change host DNS management or migrate database/application traffic as an enrollment side effect. Application endpoints and existing private connectivity SHALL remain unchanged.

#### Scenario: A control host joins the tailnet
- **WHEN** the control host is enrolled for administration
- **THEN** its existing database clients keep their selected endpoints and private routes and no new subnet route is advertised

### Requirement: Ingress restriction follows verified recovery
Adoption SHALL preserve existing narrow SSH access until a second managed connection over the intended tailnet succeeds. Provider and guest firewall changes SHALL be made through their respective owning configuration only after allowed/denied connectivity checks and provider-console recovery are verified. Disposable-host acceptance SHALL demonstrate service recovery after reboot. A failed prerequisite MUST leave the existing route available and rollout incomplete. Recovery policy MUST NOT depend on a remembered home IP.

#### Scenario: Tailnet connectivity fails before cutover
- **WHEN** enrollment or managed VPN connectivity fails
- **THEN** public SSH restrictions are not applied and the operator can continue through the existing narrow route

#### Scenario: An enrolled host restarts
- **WHEN** a disposable acceptance host reboots with the selected configuration
- **THEN** authorised managed tailnet SSH resumes without a new enrollment or a public SSH opening
