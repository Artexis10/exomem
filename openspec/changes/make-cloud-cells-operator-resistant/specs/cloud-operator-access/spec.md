## MODIFIED Requirements

### Requirement: Routine operator access cannot reach tenant content

The operator's everyday cluster identity SHALL be able to read:
- workload status and events;
- endpoint and isolation-policy metadata;
- resource use;
- the content-free logs that cells and controllers emit.

It SHALL NOT be able to read Secrets, or to exec, attach, port-forward, proxy or add ephemeral containers in any cell namespace. The documented operator procedures SHALL use this identity by default. The identity SHALL work from the operator's own machine over the private network, and its credential SHALL NOT be stored on any node. No routine procedure SHALL require a login to a node.

#### Scenario: Operator inspects a failing cell

- **WHEN** the operator uses the everyday identity to view a cell's pod status, events or logs
- **THEN** the request succeeds and returns no vault content

#### Scenario: Operator verifies artifact isolation

- **WHEN** the operator reads EndpointSlices, NetworkPolicies, ValidatingAdmissionPolicies and their bindings to verify a selected cell’s broker route
- **THEN** metadata inspection succeeds through the everyday identity, which retains no Secret, connect-subresource or mutation permission

#### Scenario: Logs stay content-free under real use

- **WHEN** a tenant writes a unique canary string, then searches and reviews it, and the operator reads that cell's and the controllers' logs
- **THEN** the canary string appears in none of them

#### Scenario: Operator mistakenly opens a shell in a cell

- **WHEN** the everyday identity requests exec, attach, port-forward, proxy or an ephemeral container in a cell namespace
- **THEN** the API server denies the request

#### Scenario: Operator requests a tenant Secret

- **WHEN** the everyday identity requests any Secret
- **THEN** the API server denies the request

#### Scenario: Routine check without a node login

- **WHEN** an operator or an agent checks cell health, resource use or controller errors
- **THEN** the documented procedure uses the everyday identity from the operator's machine, and no node login occurs

### Requirement: Break-glass access is deliberate and audited

Access beyond the everyday and deploy identities SHALL require the admin tier. The admin tier covers:
- a node shell;
- the break-glass identity;
- applying Secrets;
- a platform change outside an image-only update.

Every admin-tier session SHALL require a presence check, a touch on a FIDO2 authenticator that the operator holds. Node SSH SHALL accept only such keys, or short-lived certificates issued from such a session.

The break-glass credential SHALL be issued on demand through an audited certificate request, only from an admin-tier session. It SHALL expire within about one hour, and SHALL NOT be the default in any operator procedure.

The issuing request, the approval, every API request made with the credential, and every node session SHALL appear in the locked access record. Each entry carries user or certificate, verb or command class, resource, namespace and time. No entry SHALL record Secret values or vault content. Request and response bodies are recorded only for namespaces, PersistentVolumeClaims and StatefulSets, which carry neither.

Where the cluster can enforce it at admission, exec, attach, port-forward and ephemeral containers in cell namespaces SHALL be denied to every identity outside the break-glass group.

#### Scenario: Break-glass credential expires

- **WHEN** an hour has passed since a break-glass credential was issued
- **THEN** the API server rejects it

#### Scenario: Break-glass exec into a cell

- **WHEN** the break-glass identity opens an exec session in a cell namespace
- **THEN** the locked access record holds who, which pod and when, without the session's content

#### Scenario: Admission enforcement for other identities

- **WHEN** cluster admission can match these CONNECT subresources, and any identity outside the break-glass group requests exec, attach, port-forward or an ephemeral container in a cell namespace
- **THEN** admission denies the request

#### Scenario: Node login without the operator's presence

- **WHEN** a process holding the operator's SSH configuration and routine credentials, but not the operator's authenticator, tries to open a node shell
- **THEN** the node refuses the login

### Requirement: The product states its privacy boundary truthfully

User-facing descriptions of Exomem Cloud privacy SHALL claim, for a given cell, only the properties whose requirements are in force in production for that cell and its mode.

For every cell, the claimable properties are:
- tenants cannot reach one another's data;
- the internet-facing edge cannot read durable tenant keys;
- the operator cannot see content by accident, and deliberate access leaves an audit trail;
- data at rest and in backups is encrypted.

Once the per-cell key hierarchy, the locked access record, the tenant access log and the admin-tier presence check are in force for the cell, these become claimable:
- every deliberate access path is recorded where the operator cannot erase it, and is shown to the tenant;
- the cell's data is encrypted under keys for that cell alone, and deleting the cell destroys them.

For a cell in tenant-locked mode, this also becomes claimable: stored data and backups cannot be decrypted without the tenant's key.

The descriptions SHALL also disclose that:
- the edge and the gateway see requests and responses in transit;
- in standard mode, the operator holds the cell's keys on the serving infrastructure and could deliberately read content;
- in tenant-locked mode, while the cell is unlocked, a deliberate attack on the node that runs it can still reach its content;
- in tenant-locked mode, losing both the recovery code and every passkey loses the vault.

They SHALL NOT claim that the operator is technically unable to access tenant data while cells run without hardware memory encryption and attestation.

#### Scenario: Privacy copy review

- **WHEN** Cloud privacy copy is published or changed
- **THEN** every claim maps to a property in force for the cells it describes, every disclosure for those cells' modes is present, and none says the operator cannot access tenant data

#### Scenario: Claim made before its mechanism ships

- **WHEN** copy claims that every access is shown to the tenant, while the tenant access log does not yet run in production
- **THEN** the copy review rejects the claim

## ADDED Requirements

### Requirement: Operator access is recorded where the operator cannot erase it

The platform SHALL ship these events off the serving node to a locked access record within five minutes:
- the cluster audit events for exec, attach, port-forward, proxy and ephemeral containers;
- Secret reads;
- every create, update or delete of RBAC objects, admission policies and their bindings;
- certificate signing requests;
- node session events: logins with their key or certificate identity, privilege escalation and session end.

The locked access record SHALL be object storage whose object versions nobody can delete or shorten before a configured retention of at least one year. The credential on the nodes SHALL be able to write to it, but not to delete. The shipper SHALL write a heartbeat at a configured interval, and a missing heartbeat SHALL count as a gap. The record SHALL NOT contain Secret values or vault content.

#### Scenario: Break-glass use is recorded off the node

- **WHEN** the break-glass identity reads a Secret
- **THEN** the event is in the locked access record within five minutes

#### Scenario: Erasing the record

- **WHEN** any credential, including the account owner's, tries to delete or shorten a record object within its retention
- **THEN** the storage provider refuses

#### Scenario: Shipper stopped

- **WHEN** the shipper stops for longer than its heartbeat interval
- **THEN** the record shows a gap for that period

### Requirement: Tenants see every operator access to their cell

Each cell SHALL show its tenant an access log derived from the locked access record, within ten minutes of each event. The log SHALL list:
- every connect-subresource or Secret event in the cell's namespace;
- every node session on the node that hosts the cell;
- every break-glass credential issued while the cell ran;
- every gap in the record.

Each entry SHALL give time, identity tier, action, target and any stated reason, and SHALL NOT contain vault content.

#### Scenario: Break-glass exec into a tenant's cell

- **WHEN** the operator opens a break-glass exec session in a tenant's cell namespace
- **THEN** that tenant's access log shows the session within ten minutes, with time, tier, target and reason

#### Scenario: Record gap

- **WHEN** the locked access record has a gap while a cell runs
- **THEN** that cell's access log shows the gap as a gap, not as an access

### Requirement: Deploys cannot reach tenant content

A deploy identity SHALL be able to change only container image fields:
- of cells, through cellctl release attempts;
- of named platform workloads.

Each image SHALL be a digest in the release registry. Admission SHALL deny any other change by this identity. The identity SHALL NOT be able to read Secrets, or to exec, attach, port-forward, proxy or add ephemeral containers. It SHALL NOT be able to change RBAC, admission policies, volumes or Secrets. A platform change outside an image-only update SHALL require the admin tier.

#### Scenario: Image rollout

- **WHEN** the deploy identity sets a named platform workload's image to a release-registry digest
- **THEN** admission permits the change

#### Scenario: Deploy identity widens its access

- **WHEN** the deploy identity changes a workload's volumes, command or service account, or any RBAC or admission object
- **THEN** admission denies the change

#### Scenario: Deploy identity opens a shell

- **WHEN** the deploy identity requests exec in a cell namespace
- **THEN** the API server denies the request
