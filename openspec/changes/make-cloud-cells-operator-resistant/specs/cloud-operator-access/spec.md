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

Every credential that can yield any of the following SHALL be an admin-tier credential:
- node root;
- cluster administration;
- the cluster datastore or its snapshots;
- provider control of a node, through its API, rescue system or console;
- escrow decryption;
- registry write;
- repository administration: rulesets, branch protection, environments and Actions secrets.

That covers:
- node SSH keys;
- the break-glass identity;
- the K3s server token and CA material;
- snapshot restore credentials;
- the provider API token;
- Terraform state and its backend credentials;
- the escrow identity;
- registry and package write tokens;
- GitHub credentials with repository administration permission;
- every secret-store entry that holds one of these.

Admin-tier custody SHALL require the operator's physical presence for the operator and recovery credentials that sit outside the running services:
- Node SSH SHALL accept only FIDO2 security-key logins that need a touch for each login. A certificate SHALL certify only such a key.
- Every other operator or recovery credential SHALL be encrypted at rest to an identity that needs a touch for each decryption, or SHALL sit behind the operator's own provider-account login with a hardware second factor.
- No routine-agent environment, and no secret store that the routine or deploy identities can read, SHALL hold an admin-tier credential in plaintext.

Two kinds of admin-tier credential are exempt from the presence check, each within its stated boundary:
- the release publisher: the repository's release workflow MAY publish images with its run-scoped token, only on release refs; the token expires with the run, and the release attestation records each image it publishes;
- service credentials inside the runtime: a running service MAY hold the credential it needs, such as the CSI controller's provider token or the K3s server token on the server node, where only node root or cluster administration can reach it.

The inventory of admin-tier credentials SHALL record each exemption with its permissions and access boundary.

The provider account owner can still regain control of a server through the provider console. Descriptions SHALL state that limit, not claim to gate it.

The break-glass credential SHALL be issued on demand, through an audited certificate request, only inside a presence-checked node session. It SHALL expire within about one hour, and SHALL NOT be the default in any operator procedure.

The issuing request, the approval, every API request made with the credential, and every node session SHALL be submitted to the access record. Each entry carries user or key identity, verb or command class, resource, namespace and time. No entry SHALL record Secret values or vault content. Request URIs SHALL lose their query parameters before they leave the node. Request and response bodies are recorded only for namespaces, PersistentVolumeClaims and StatefulSets, and the shipped fields SHALL be checked to carry no Secret values or content.

Where the cluster can enforce it at admission, exec, attach, port-forward and ephemeral containers in cell namespaces SHALL be denied to every identity outside the break-glass group.

#### Scenario: Break-glass credential expires

- **WHEN** an hour has passed since a break-glass credential was issued
- **THEN** the API server rejects it

#### Scenario: Break-glass exec into a cell

- **WHEN** the break-glass identity opens an exec session in a cell namespace
- **THEN** the access record receives who, which pod and when, without the command line or the session's content

#### Scenario: Admission enforcement for other identities

- **WHEN** cluster admission can match these CONNECT subresources, and any identity outside the break-glass group requests exec, attach, port-forward or an ephemeral container in a cell namespace
- **THEN** admission denies the request

#### Scenario: Node login without the operator's presence

- **WHEN** a process holding the operator's SSH configuration and routine credentials, but not the operator's authenticator, tries to open a node shell
- **THEN** the node refuses the login

#### Scenario: Automation reaches for an admin credential

- **WHEN** automation with the operator's routine credentials tries to obtain the provider API token, the K3s server token or a snapshot restore credential
- **THEN** it finds no plaintext copy in any environment or store it can read, and decrypting the operator's copy needs the operator's touch

#### Scenario: Release publishing stays unattended

- **WHEN** the release workflow publishes an image on a release ref
- **THEN** it authenticates with its run-scoped token and needs no touch, and the image carries the release attestation

### Requirement: The product states its privacy boundary truthfully

User-facing descriptions of Exomem Cloud privacy SHALL claim, for a given cell, only the properties whose requirements are in force in production for that cell and its mode.

For every cell, the claimable properties are:
- tenants cannot reach one another's data;
- the internet-facing edge cannot read durable tenant keys;
- the operator cannot see content by accident, and deliberate access leaves an audit trail;
- data on the cell's volume and in backups is encrypted at rest.

Once the per-cell key hierarchy, its content-scratch rule, the access record, the tenant access log and the admin tier are in force for the cell, these become claimable:
- routine and deploy credentials cannot reach content;
- reaching content needs the operator's physical presence;
- access through the cluster API and node logins is recorded off the server, where received events cannot be erased, and is shown to the tenant;
- the cell's data is encrypted under keys for that cell alone;
- deleting the cell destroys those keys, and copies in platform backups expire by the date shown.

For a cell in tenant-locked mode, this becomes claimable from `lock_complete_at`: stored data and backups cannot be decrypted without the tenant's key.

The descriptions SHALL also disclose that:
- the edge and the gateway see requests and responses in transit;
- in standard mode, the operator holds the cell's keys on the serving infrastructure and could deliberately read content;
- root on a node can suppress or forge events before they leave it, so the record shows what it received, not everything that happened;
- the provider account owner can regain control of a server through the provider console;
- in tenant-locked mode, while the cell is unlocked, a deliberate attack on the node that runs it can still reach its content;
- in tenant-locked mode, losing both the recovery code and every passkey loses the vault;
- code merged to the repository can reach a release, so anyone with merge rights could ship code that reads content, through a path that is public and permanent;
- an owner export's archive sits in platform storage, readable by the operator, until the export expires (its configured retention, 24 hours by default) or a newer export replaces it.

They SHALL NOT claim:
- that the operator is technically unable to access tenant data while cells run without hardware memory encryption and attestation;
- that the access record is complete.

#### Scenario: Privacy copy review

- **WHEN** Cloud privacy copy is published or changed
- **THEN** every claim maps to a property in force for the cells it describes, and every disclosure for those cells' modes is present
- **AND** no claim says that the operator cannot access tenant data, or that the record holds every access

#### Scenario: Claim made before its mechanism ships

- **WHEN** copy claims that access is shown to the tenant, while the tenant access log does not yet run in production
- **THEN** the copy review rejects the claim

## ADDED Requirements

### Requirement: The access record retains what its sources submit

The platform SHALL ship these events off the serving node to an access record within five minutes:
- the cluster audit events for exec, attach, port-forward, proxy and ephemeral containers;
- Secret reads;
- every create, update or delete of RBAC objects, admission policies and their bindings;
- certificate signing requests;
- node session events: logins with their key or certificate identity, privilege escalation and session end.

The access record SHALL be object storage whose object versions no user can delete or shorten before a configured retention of at least one year. The credential on the nodes SHALL be able to write to it, but not to delete. The shipper SHALL write a heartbeat at a configured interval, and a missing heartbeat SHALL count as a gap. The record SHALL NOT contain Secret values or vault content.

The record proves that a received event exists and when the shipper went silent. It SHALL NOT be described as complete. Root on a node can omit, alter or forge events before they leave it.

#### Scenario: Break-glass use is recorded off the node

- **WHEN** the break-glass identity reads a Secret
- **THEN** the event is in the access record within five minutes

#### Scenario: Erasing the record

- **WHEN** any user credential, including the account owner's, tries to delete or shorten a record object within its retention
- **THEN** the storage provider refuses

#### Scenario: Shipper stopped

- **WHEN** the shipper stops for longer than its heartbeat interval
- **THEN** the record shows a gap for that period

### Requirement: Tenants see the operator access the record received

The platform SHALL derive each cell's access log from the access record into the control database. The control plane SHALL serve it, including while the cell is locked or stopped. Entries SHALL appear within ten minutes of receipt. The log SHALL list:
- every received connect-subresource or Secret event in the cell's namespace;
- every received node session on the node that hosted the cell at that time;
- every break-glass credential issued while the cell ran;
- every image or configuration change to the component that derives the log;
- every gap in the record.

Each entry SHALL give time, identity tier (routine, deploy, admin, or service for a credential that a running service holds), action, target and any stated reason. It SHALL NOT contain vault content, and it SHALL render every field as text. The log SHALL state that it lists what the record received.

#### Scenario: Break-glass exec into a tenant's cell

- **WHEN** the operator opens a break-glass exec session in a tenant's cell namespace
- **THEN** that tenant's access log shows the session within ten minutes, with time, tier, target and reason, and another tenant's log does not show it

#### Scenario: Locked cell's access log

- **WHEN** a tenant opens the access log of their locked or stopped cell
- **THEN** the control plane serves it

#### Scenario: Record gap

- **WHEN** the access record has a gap while a cell runs
- **THEN** that cell's access log shows the gap as a gap, not as an access

### Requirement: Deploys cannot reach tenant content

A deploy identity SHALL be able to change only container image fields:
- of cells, through cellctl release attempts;
- of named platform workloads.

Admission SHALL accept an image only when it carries a GitHub artifact attestation signed by this repository's release workflow on a release ref, for that workload's image family. A release ref SHALL be a `v*` tag that only the release App can create, move or delete, on a commit that `main` contains. The App's signing key SHALL be readable only by workflows running on `main`, and GitHub Actions' own token SHALL NOT be able to create `v*` tags. The release workflow SHALL also refuse to build from a tag whose commit `main` does not contain, as a guard against mistakes. Admission SHALL deny any other change by this identity. The identity SHALL NOT be able to read Secrets, or to exec, attach, port-forward, proxy or add ephemeral containers. It SHALL NOT be able to change RBAC, admission policies, volumes or Secrets. A platform change outside an attested image-only update SHALL require the admin tier.

#### Scenario: Attested image rollout

- **WHEN** the deploy identity sets a named platform workload's image to a digest that the release workflow attested for that workload's image family
- **THEN** admission permits the change

#### Scenario: Image pushed outside the release workflow

- **WHEN** the deploy identity sets a workload's image to a registry digest with no release-workflow attestation, or to an attestation for another image family
- **THEN** admission denies the change

#### Scenario: Tag outside the release automation

- **WHEN** a workflow on a branch other than `main` tries to read the release App's key, or any credential other than the release App creates, moves or deletes a `v*` tag
- **THEN** the environment withholds the key or the repository refuses the tag, so no release attestation exists for an unmerged commit

#### Scenario: Deploy identity widens its access

- **WHEN** the deploy identity changes a workload's volumes, command or service account, or any RBAC or admission object
- **THEN** admission denies the change

#### Scenario: Deploy identity opens a shell

- **WHEN** the deploy identity requests exec in a cell namespace
- **THEN** the API server denies the request
