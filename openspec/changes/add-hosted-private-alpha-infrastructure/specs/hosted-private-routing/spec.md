## ADDED Requirements

### Requirement: Control routes require two independent credentials
Cloudflare Tunnel SHALL be the only public ingress to the cluster. The control hostname SHALL require a Cloudflare Access service token held by Vercel/operators and Traefik SHALL expose only provisioner `/cells/*` and versioned private cell-control paths. Provisioner calls SHALL additionally require the provisioner bearer; cell calls SHALL additionally require the per-cell service bearer and exact hosted identity headers. The personal OAuth/public Exomem surface MUST NOT be routed.

#### Scenario: Access credential is missing
- **WHEN** a caller presents a valid cell bearer and headers but no valid Cloudflare Access service token on the control hostname
- **THEN** the request is rejected at the edge and never reaches the cell

#### Scenario: Personal route is requested through control ingress
- **WHEN** an Access-authenticated caller requests a non-private Exomem route
- **THEN** Traefik rejects it without forwarding to the cell

### Requirement: External endpoints map only to registered opaque cells
The provisioner SHALL return an HTTPS base path containing only the immutable opaque cell ID. Traefik targets SHALL be generated from the provisioner's resource registry and fixed chart labels, not a caller-supplied host, namespace, Service, URL, or header. Prefix stripping SHALL preserve the exact private cell path and streaming body.

#### Scenario: Caller tries to select another upstream
- **WHEN** a request supplies a forged host, tenant/cell selector header, namespace, or upstream URL
- **THEN** routing continues only to the registered endpoint for the authenticated target or fails closed

### Requirement: Browser file bodies bypass Vercel Functions
Substrate SHALL authenticate and authorize a transfer using a small ticket request, then return a short-lived direct-transfer URL and bounded signed headers. The browser SHALL send upload/download bodies directly through a separate transfer hostname to the cell. Vercel Functions MUST NOT receive the file body. The alpha upload payload limit SHALL be 90 MiB plus explicitly bounded multipart overhead below Cloudflare's 100 MB request cap.

#### Scenario: Maximum alpha upload follows the direct path
- **WHEN** an authorized browser uploads a 90 MiB payload
- **THEN** the request streams browser -> Cloudflare -> Traefik -> cell without a Vercel Function invocation and succeeds within the byte bound

#### Scenario: Oversized upload fails before storage mutation
- **WHEN** the declared content length or streamed body exceeds the signed limit or edge cap
- **THEN** the transfer fails and no partial canonical artifact is published

### Requirement: Direct transfers use one-time cell-bound grants
The public transfer hostname SHALL expose only versioned upload/download routes. Exomem SHALL authorize those routes with a signed grant instead of the long-lived cell bearer. The grant SHALL bind audience, operation, tenant, cell, principal scope, JTI, issue/expiry time, and byte limit. The cell SHALL durably consume the JTI before body bytes or download access, and reuse MUST fail after pod restart. A grant MUST NOT change cell, path, operation, or limit.

#### Scenario: Transfer grant is replayed
- **WHEN** a successful or aborted transfer's JTI is presented again after the same or a restarted pod
- **THEN** the cell rejects it and the browser must request a fresh ticket

#### Scenario: Grant operation or path is altered
- **WHEN** an upload grant is used for download or against a different requested artifact/path
- **THEN** signature/claim validation rejects before file access

### Requirement: Transfer CORS is exact and non-authoritative
The transfer hostname SHALL answer unauthenticated `OPTIONS` only for the canonical Substrate origin, exact upload/download methods, and exact required request headers, and SHALL expose only required response headers. The Origin check SHALL be defense in depth; signed grant validation and durable JTI consumption SHALL remain the authorization boundary.

#### Scenario: Canonical preflight succeeds
- **WHEN** the canonical browser origin preflights an allowed transfer with the exact declared headers
- **THEN** it receives the minimal matching CORS policy without consuming a grant

#### Scenario: Hostile origin is denied
- **WHEN** an unconfigured origin preflights or attempts a transfer even with syntactically valid headers
- **THEN** the browser-readable request is denied, and a non-browser caller still requires a valid unused signed grant

### Requirement: Maintenance gates cover control and transfer routes
Before asserting routing stopped for backup/export/restore/suspend/delete, the provisioner SHALL acquire the durable per-cell operation lock, disable both control and transfer routes, externally verify rejection on both, and then call internal quiesce/drain. The gate SHALL serialize with every other lifecycle/durability action. Routes SHALL reopen only after safe checkpoint release and runtime resume.

#### Scenario: Previously issued ticket is blocked during backup
- **WHEN** an unused valid transfer ticket is presented after the maintenance gate closes but before the snapshot releases
- **THEN** the route rejects it and no bytes reach the cell until routing reopens

#### Scenario: In-flight mutation drains
- **WHEN** maintenance begins while a mutation or transfer is already inside the cell
- **THEN** route closure blocks new work and runtime quiesce waits for the in-flight operation or fails the backup without claiming routing-stopped success

### Requirement: Tenant network policy denies lateral and external access
Each tenant namespace SHALL default-deny ingress and egress. Only correctly labelled Traefik pods from the platform namespace SHALL reach the cell port; monitoring ingress MAY be separately labelled. Alpha cells SHALL have no external egress. Network policy and executable probes SHALL deny other cells, Kubernetes API, Neon, B2, node/cloud metadata, and unlabelled platform pods.

#### Scenario: Cell attempts lateral access
- **WHEN** a compromised cell tries another cell's Service/PVC endpoint or the Kubernetes API
- **THEN** the network connection fails and no target request is observed

#### Scenario: Unlabelled platform pod attempts ingress
- **WHEN** a pod in the platform namespace lacks the exact Traefik selector labels and calls a cell
- **THEN** ingress is denied

### Requirement: Hosted request identity fails closed
Every private cell control request SHALL validate the exact cell ID, hosted protocol, canonical UUIDv4 request ID, derived principal-scope digest, and idempotency key where mutating. Caller end-user tokens, emails, IDs, roles, and routing selectors MUST NOT be forwarded to the cell.

#### Scenario: Identity header is substituted
- **WHEN** a valid service bearer is combined with a different cell ID, protocol, malformed request ID, or principal scope
- **THEN** the cell rejects the request without revealing vault identity or content
