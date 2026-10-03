## Purpose

Give shared Substrate control infrastructure one organisation-owned management boundary while preserving the existing database, consumers and recovery lineage.

## ADDED Requirements

### Requirement: Shared control infrastructure has one management owner
Shared control infrastructure SHALL have one canonical organisation-owned repository and one authorised writer per managed resource. Product deployment roots SHALL consume explicit dependencies and MUST NOT retain ownership of a transferred host, primary IP, database firewall, database DNS record or dedicated backup resource. Host configuration and shared recovery procedures SHALL have one source. Application repositories SHALL retain ownership of their schemas and application migrations.

#### Scenario: Exomem plans after ownership handover
- **WHEN** Exomem's infrastructure is planned after a completed handover
- **THEN** its plan cannot create, update or delete transferred shared control resources and its configured database dependency remains usable

### Requirement: State handover preserves deployed identities
Management handover MUST preserve provider resource IDs, disks, addresses, private connectivity, database endpoints, TLS lineage, roles, credential values and backup history. It SHALL use reviewed state-only removal and adoption under a coordinated writer freeze. Any unexpected create, delete, replacement, credential rotation or guest mutation SHALL block handover. The first handover SHALL preserve the existing provider project and private network.

#### Scenario: A target import would replace the database host
- **WHEN** the target management plan proposes a host replacement or any other unapproved resource mutation
- **THEN** handover stops before applying that plan and the serving database remains unchanged

#### Scenario: Only management ownership changes
- **WHEN** source removal and target adoption complete under the coordinated freeze
- **THEN** the same deployed resource IDs are owned by exactly one target state and all source and target plans satisfy the approved state-only contract

### Requirement: Shared infrastructure state is isolated and recoverable
Shared compute and database durability SHALL use distinct locked, versioned remote states with explicit execution and access contracts. Source and target workspace identities and execution settings MUST be verified before initialisation or state mutation. Recoverable pre-handover state versions and resource manifests SHALL be retained privately. Incomplete handover MUST leave writers frozen until one owner has been re-established and validated; recovery MUST NOT recreate resources or accept simultaneous source and target ownership.

#### Scenario: Target adoption fails after source removal
- **WHEN** target adoption fails after an approved source forget operation
- **THEN** writers remain frozen and the operator completes target adoption or restores source management of the same resources without destroying them or introducing a second owner

### Requirement: Dependency publication preserves consumer authority
Shared infrastructure SHALL publish a versioned non-secret dependency contract identifying endpoints, transport requirements and management ownership. Products MUST validate that contract without acquiring shared infrastructure apply authority or sharing raw Terraform state. Private network dependencies SHALL identify their existing owner. Missing or incompatible dependency versions MUST block the affected deployment before mutation.

#### Scenario: A product selects an incompatible dependency version
- **WHEN** a product deployment cannot validate its selected shared database dependency contract
- **THEN** it refuses deployment without modifying the shared host or substituting another database

### Requirement: Credential and recovery custody survive extraction
The handover SHALL preserve exact canonical Bitwarden bindings, encrypted credential delivery versions, SSH identity and host pins, database TLS verification and backup encryption/retention lineage. Credential plaintext and raw state MUST NOT enter repository content, chat, logs or plan summaries. A recovery check SHALL prove a recent database backup can be restored in an isolated environment with production sends, payments and publishing blocked. Extraction MUST NOT silently rotate credentials or widen access.

#### Scenario: Managed SSH after handover
- **WHEN** an authorised administrator connects after the management split
- **THEN** the canonical managed profile authenticates with its existing Bitwarden identity and trusted host pin while password-based network SSH remains disabled

#### Scenario: Backup restoration is isolated
- **WHEN** the pre-handover recovery gate restores the selected backup
- **THEN** the restored database is isolated from production consumers and external effects, and its recorded lineage and selected integrity checks match the recovery receipt

### Requirement: Completion is established by live consumer and ownership evidence
Handover completion SHALL require author-independent review, exact approved plan evidence, single-owner state reconciliation, preserved host identity, database TLS/health and role-boundary checks, backup continuity and representative application consumer checks. A rename, repository creation or Terraform import alone MUST NOT establish completion. Any held migration or failed check SHALL remain visible as incomplete.

#### Scenario: Import succeeds but a consumer is disconnected
- **WHEN** target state adoption succeeds but a previously verified consumer cannot reach its intended database
- **THEN** handover remains incomplete and follow-through restores the existing dependency before the writer freeze is lifted
