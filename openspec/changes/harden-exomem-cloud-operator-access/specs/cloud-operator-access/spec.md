## Purpose

Keep Exomem Cloud tenant content and tenant keys out of reach of the internet-facing edge and of routine operator access, while every server-side feature still runs inside the tenant's own cell.

## ADDED Requirements

### Requirement: The public edge cannot read tenant keys

The component that terminates public TLS for the Cloud MCP hostname SHALL run in a namespace that holds no Secret except the certificate it serves. Its identity SHALL NOT be able to read Secrets in any other namespace, including cell namespaces, the Cloud controller's namespace and the platform namespace. The DNS-01 credential used to obtain that certificate SHALL NOT be readable by the edge.

#### Scenario: Edge identity attempts to read a tenant Secret

- **WHEN** the edge component's identity requests any Secret outside its own namespace
- **THEN** the API server denies the request

#### Scenario: Edge namespace contents

- **WHEN** the Secrets in the edge namespace are listed
- **THEN** the only one is the Cloud MCP certificate

#### Scenario: Gateway ingress

- **WHEN** a pod connects to the Cloud gateway
- **THEN** the connection is admitted only if it comes from the edge component's pods

### Requirement: Routine operator access cannot reach tenant content

The operator's everyday cluster identity SHALL be able to read workload status, events and the content-free logs that cells and controllers emit. It SHALL NOT be able to read Secrets, or to exec, attach, port-forward, proxy or add ephemeral containers in any cell namespace. The documented operator procedures SHALL use this identity by default.

#### Scenario: Operator inspects a failing cell

- **WHEN** the operator uses the everyday identity to view a cell's pod status, events or logs
- **THEN** the request succeeds and returns no vault content

#### Scenario: Operator mistakenly opens a shell in a cell

- **WHEN** the everyday identity requests exec, attach, port-forward, proxy or an ephemeral container in a cell namespace
- **THEN** the API server denies the request

#### Scenario: Operator requests a tenant Secret

- **WHEN** the everyday identity requests any Secret
- **THEN** the API server denies the request

### Requirement: Break-glass access is deliberate and audited

Access beyond the everyday identity SHALL require a separate break-glass identity. It SHALL be held only by the node's root account and SHALL NOT be the default in any operator procedure. Every API request made with it SHALL be recorded in the cluster audit log with user, verb, resource, namespace and time, and SHALL NOT record Secret values or request bodies. Where the cluster can enforce it at admission, exec, attach, port-forward and ephemeral containers in cell namespaces SHALL be denied to every identity outside the break-glass group.

#### Scenario: Break-glass exec into a cell

- **WHEN** the break-glass identity opens an exec session in a cell namespace
- **THEN** the audit log records who, which pod and when, without the session's content

#### Scenario: Admission enforcement for other identities

- **WHEN** cluster admission can match these CONNECT subresources, and any identity outside the break-glass group requests exec, attach, port-forward or an ephemeral container in a cell namespace
- **THEN** admission denies the request

### Requirement: Operator exports are readable only by the tenant

An operator-assisted export of a tenant's vault SHALL be encrypted to recipients supplied by that tenant, and only to those. No plaintext archive SHALL be written outside the scratch restore volume, and the operator SHALL NOT hold a key that decrypts the archive. The operator SHALL record only the recipient fingerprint, the archive digest and the tenant's verification result.

#### Scenario: Operator exports a friend's vault

- **WHEN** the operator runs the export for a tenant
- **THEN** the archive decrypts only with the tenant's private key, and the operator's records contain no vault content

#### Scenario: Operator supplies their own recipient

- **WHEN** the recipients file names a key that the tenant did not supply
- **THEN** the procedure stops before any restore runs

### Requirement: The product states its privacy boundary truthfully

User-facing descriptions of Exomem Cloud privacy SHALL claim no more than these properties:
- tenants cannot reach one another's data;
- the internet-facing edge cannot read tenant keys;
- the operator cannot see content by accident, and deliberate access leaves an audit trail;
- data at rest and in backups is encrypted.

They SHALL NOT claim that the operator is technically unable to access tenant data while the volume and backup keys are held on the serving node.

#### Scenario: Privacy copy review

- **WHEN** Cloud privacy copy is published or changed
- **THEN** every claim maps to one of the stated properties, and none says the operator cannot access tenant data
