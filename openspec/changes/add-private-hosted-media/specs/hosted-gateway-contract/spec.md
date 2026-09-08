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

### Requirement: Direct content ingress exposes only the public contract

An activated direct profile SHALL terminate content TLS at the controlled public edge and bypass third-party content proxies on its MCP, transfer and export paths. The edge SHALL expose only its configured host, public protocol routes and required discovery metadata. Administrative routes, Kubernetes discovery credentials and unrestricted internal forwarding MUST NOT be accessible through that edge. Transport protection and existing tenant/grant authorization SHALL both remain enforced.

#### Scenario: A caller spoofs routing or ingress identity

- **WHEN** a request supplies an unexpected Host/SNI, encoded private path, forged forwarding header or private control method
- **THEN** public admission rejects it or replaces untrusted provenance before authorized forwarding
- **AND** the public edge cannot reach the private administrative listener by selecting another Host header

#### Scenario: A direct profile still traverses a content proxy

- **WHEN** a DNS chain, application rewrite, transfer URL or export delivery sends that profile's content through a third-party TLS terminator
- **THEN** direct-profile readiness fails
- **AND** that processor remains disclosed until the path is corrected and verified

### Requirement: Resource migration preserves exact OAuth and transfer authority

The direct MCP resource SHALL have an explicit identity distinct from the separately configured authorization issuer. Discovery and challenges SHALL advertise the actual resource and issuer. Codes, access tokens, refresh tokens and transfer grants MUST NOT gain authority for another resource or host through aliases, redirects or fallback. Legacy compatibility SHALL be bounded to legacy connections pending explicit migration and revocation.

#### Scenario: A legacy token reaches the direct resource

- **WHEN** a client presents a token bound only to the legacy resource at the direct endpoint
- **THEN** authentication fails and requires authorization for the direct resource
- **AND** the server does not reinterpret the old audience or forward the bearer to another origin

#### Scenario: An existing user migrates uploads and downloads

- **WHEN** the user's content profile changes to the direct hostname
- **THEN** old transfers drain or expire and newly issued grants bind the direct host under the existing lifecycle and tenant checks
- **AND** the migration does not leave permanent dual-host grant acceptance or redirect grant-bearing URLs

### Requirement: Direct exposure requires bounded operation and certificate evidence

The direct profile SHALL remain disabled until actual connections prove its TLS identity, encrypted certificate-state placement, renewal/recovery, resource admission and public-route isolation. Public connection/request controls SHALL preserve interactive and recovery capacity under the specified synthetic workload. Evidence MUST distinguish this application protection from volumetric network protection. Rollback MUST NOT silently restore a content-proxy or cleartext path for an activated direct connection.

#### Scenario: Certificate issuance precedes content activation

- **WHEN** a new direct hostname is exposed for certificate validation before privacy and client acceptance completes
- **THEN** only certificate validation and content-free unavailable responses are served
- **AND** no tenant credentials or content are forwarded to a backend

#### Scenario: A certificate expires or direct protection fails

- **WHEN** renewal, identity verification, encrypted state or the activated private transport profile cannot be established
- **THEN** the affected content route becomes unavailable without weakening verification
- **AND** recovery uses a compatible direct profile rather than an undisclosed Tunnel, rewrite or HTTP fallback
