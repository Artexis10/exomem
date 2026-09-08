## ADDED Requirements

### Requirement: Hosted network boundaries require verified encrypted transport

Hosted command, transfer, lifecycle, recovery and processing traffic carrying tenant data or credentials across pod or host boundaries SHALL use authenticated, certificate-verified encryption. Peer identity and existing principal/cell authorization SHALL both be enforced. Private addressing or network policy MUST NOT substitute for transport encryption. Database and object-store connections SHALL enforce equivalent server identity verification.

#### Scenario: Public TLS forwards to an insecure internal origin

- **WHEN** the resolved path contains HTTP across a pod or host boundary
- **THEN** the strengthened privacy profile rejects that route before forwarding content or credentials
- **AND** public HTTPS or tunnel encryption does not make the insecure hop acceptable

#### Scenario: Certificate verification fails

- **WHEN** a peer certificate is expired, untrusted or bound to another identity
- **THEN** the request fails without an insecure fallback, credential-bearing redirect or verification bypass

#### Scenario: Existing operator probe uses literal loopback

- **WHEN** the bounded operator probe addresses its existing literal loopback endpoint entirely inside the cell network namespace
- **THEN** its existing no-proxy, no-DNS, no-redirect contract remains valid
- **AND** this exception cannot authorize a cleartext connection crossing a pod or host boundary

#### Scenario: Certificate rotates under normal service

- **WHEN** a service rotates to a valid certificate under the configured trust and identity policy
- **THEN** authorized traffic can continue through a proved overlap or coordinated restart
- **AND** principal routing, transfer grant scope and credential rotation semantics remain unchanged

#### Scenario: Mixed release or rollback would downgrade transport

- **WHEN** a gateway, routing layer or cell release cannot prove the activated transport profile and peer identities bound to signed deployment evidence
- **THEN** the affected content route fails closed even during rollout or rollback
- **AND** it cannot retry over HTTP, drop the activated transport requirement, or bypass existing custody and governance migration fences
