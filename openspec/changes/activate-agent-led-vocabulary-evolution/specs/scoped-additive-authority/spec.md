## Purpose

Let users deliberately delegate bounded additive graph work while keeping grants attributable, revocable and enforced independently of agent prose, vocabulary definitions and notification settings.

## ADDED Requirements

### Requirement: Additive authority is an explicit versioned opt-in

The product SHALL provide a v2 additive-authority contract with advise, ask-each and delegated modes for separately named actions: entity-instance creation, entity-type addition, relation-type addition and edge addition. Activation SHALL be an explicit trusted user decision for a vault, not a client-supplied version preference. Migration SHALL create no standing grants and SHALL preserve the prior v1 contract until deliberate activation. An activated vault SHALL enforce v2 structural-write gates for every adapter, including older clients; omitting a version or selecting a legacy route SHALL NOT restore v1 authority.

Advice authorizes no mutation. Ask-each SHALL require approval of the exact reviewed action. Delegated mode SHALL require a currently valid grant covering that action. Existing data disclosure/access policy, write boundaries, leases, validation and protected Source/Evidence rules SHALL remain independently required. Enrichment that changes existing facts, merges, deletion, supersession, deprecation and alteration of existing type meanings SHALL NOT be included in these additive grants.

#### Scenario: Upgrade does not create permission

- **WHEN** an existing installation upgrades without activating v2
- **THEN** its v1 envelope remains unchanged and no delegated additions are permitted

#### Scenario: A legacy client cannot downgrade an activated vault

- **WHEN** a v1 client attempts entity creation in a v2-activated vault without valid authority
- **THEN** the canonical leaf refuses with the required approval path rather than falling back to conversational confirmation

### Requirement: User authority cannot be self-issued by the agent

Grant creation, expansion, revocation and exact-action approval SHALL require a trusted user-control channel distinct from ordinary agent tool authority. MCP SHALL allow requesting and inspecting approval, but an agent-controlled argument, prompt, stored note, arbitrary token string or assertion of user confirmation SHALL NOT mint approval. The approved record SHALL bind the authenticated authorizer, agent/principal audience, logical vault, contract version, action set, scope, expiry and generation. Grant data and credential custody SHALL reside outside the authored vault and obey the existing authorization boundary and secret-handling contracts.

Where the deployment cannot distinguish trusted user control from agent authority, delegated mode SHALL be unavailable and the response SHALL explain how to obtain supported user approval; the product SHALL NOT label a caller-controlled boolean as verified consent. Approvals SHALL expose a reviewable description of the exact effects to the user before acceptance.

An exact-action approval SHALL be one-shot and bind one canonical operation identity as well as its payload and versions. Before any effects, execution SHALL durably reserve the approval for that identity; a committed receipt SHALL spend and link it exactly once. A proven pre-commit failure SHALL allow retry of that same unchanged identity without new approval while the approval remains valid. An uncertain outcome SHALL retain the binding until reconciliation; it SHALL NOT make the approval available to another identity. A fresh identity, changed payload or second execution SHALL require a new approval. Standing grants, unlike exact approvals, intentionally cover multiple independently validated matching actions during their lifetime.

#### Scenario: An agent requests its own broad grant

- **WHEN** an agent asks to permit future type additions using only its ordinary tool credential
- **THEN** the request can produce a pending approval but cannot grant or expand authority

#### Scenario: Retrieved permission-shaped text is inert

- **WHEN** a note says that all future relationship changes are approved
- **THEN** it grants no authority, even if the agent copies it into a proposal rationale

#### Scenario: One approval cannot be replayed as another action

- **WHEN** an approved action commits and the same approval is submitted with a new operation identity, including after the original object was removed
- **THEN** the second execution refuses; only the original identity can reconcile or replay its committed receipt without reapplying effects

#### Scenario: Failure before commit does not lose the approved retry

- **WHEN** the approved operation fails with proof that no effect committed
- **THEN** the same unchanged operation identity can retry under the still-valid approval
- **AND** an uncertain result remains reserved pending reconciliation rather than becoming a transferable approval

### Requirement: Conversation provides ordinary permission control

The agent-facing workflow SHALL support expressing intent, requesting exact approval or scoped delegation, inspecting current permissions, and requesting revocation through finite canonical operations. It SHALL truthfully report the canonical request, owner-decision, authority and application states, including expiry and unavailability, without introducing a parallel state vocabulary. A request or accepted approval SHALL NOT be reported as an executed content change.

Dashboards SHALL provide visibility, history and transparent inspection, and MAY expose management controls. Routine agent work SHALL NOT require navigating a dashboard to discover a pending action. Where a client supports an authenticated owner-confirmation adapter, the exact review and decision SHALL be available through that conversational surface. The adapter SHALL preserve the same authorizer, intended agent audience, vault, payload, currency and disclosure checks as other supported owner-control adapters. When that adapter is unavailable, the agent SHALL expose the limitation and a direct request-bound owner-confirmation fallback if the deployment advertises one. Without a supported owner channel, confirmation SHALL remain unavailable and the request pending; neither case SHALL be described as implemented in-conversation approval.

Natural-language intent and approval-presentation preferences SHALL NOT mint authority, bypass the configured consent contract or imply that the agent can grant its own requests. Browser and conversational adapters SHALL share canonical intent preparation, authority state and execution checks rather than maintain independent permission systems.

#### Scenario: An authenticated client keeps approval in the conversation

- **WHEN** the user requests an additive action through a client with a supported owner-confirmation adapter
- **THEN** the client presents the exact review and records the owner's decision without requiring dashboard navigation
- **AND** subsequent execution still checks that exact approval at the canonical leaf

#### Scenario: A client lacks authenticated owner confirmation

- **WHEN** an ordinary agent session can request permission but cannot carry the required owner decision
- **THEN** the response identifies the pending request and its deployment-advertised direct confirmation fallback, or reports owner confirmation unavailable when no supported channel exists
- **AND** an agent-reported yes cannot complete the request or substitute for supported owner confirmation

#### Scenario: On-demand control and dashboard visibility agree

- **WHEN** a user inspects permission state through an agent and then through the dashboard under equivalent authorized access
- **THEN** both surfaces resolve the same canonical requests, grants and history
- **AND** a revocation requested through the agent uses the existing owner-control contract and becomes effective under the same revoke-versus-commit ordering

### Requirement: Scope is resolved from effects rather than asserted labels

A grant SHALL name one logical vault, explicit supported actions, a bounded lifetime and an optional resolved project/target scope for actions with pre-existing canonical targets. The server SHALL derive affected objects and scope from current canonical state and the proposed effects. A request's `project` label alone SHALL NOT establish membership. Edge additions SHALL require all affected endpoints and destinations to fall within the grant; unresolved or cross-scope targets SHALL require separate approval.

Initial standing `entity.create` grants SHALL be vault-wide only because a new entity has no pre-existing canonical project membership. A narrower entity creation SHALL use exact-action approval binding the new destination and payload; a supplied project label SHALL NOT make it eligible for a project-scoped standing grant. A future narrower standing-creation contract SHALL require its own specified membership proof and migration, not silently reinterpret these grants.

Registries are vault-wide: adding an entity or relation type SHALL require an explicit vault-wide type-addition grant or exact-action approval, even when its motivating examples belong to one project. A project-scoped grant SHALL NOT silently authorize a global registry change. Automatically created back-references and index hygiene SHALL remain bounded effects of the authorized leaf, not grants for unrelated content.

#### Scenario: A project grant meets a global registry change

- **WHEN** an exact entity-creation approval or vault-wide entity-addition grant exists and the proposed entity needs a new type
- **THEN** entity-creation authority does not implicitly authorize the type save
- **AND** the workflow seeks vault-wide type-addition authority or exact approval

#### Scenario: A new entity cannot assert its own project membership

- **WHEN** an agent requests a project-scoped standing entity-creation grant or supplies a project label as the only scope proof for a new entity
- **THEN** the operation is unsupported under this initial contract and seeks exact-action approval or an explicit vault-wide entity-creation grant

#### Scenario: Metadata cannot launder an out-of-scope target

- **WHEN** an agent supplies an allowed project label for an edge whose canonical target lies outside that grant
- **THEN** the edge write refuses without modifying either endpoint

### Requirement: Authority and review bind at every actual write

Each v2 additive structural mutation SHALL validate authority at the canonical leaf immediately before its commit under the same serialization boundary used for the mutation. Exact approval SHALL bind the canonical action payload, target versions and registry hashes; a standing grant SHALL bind its current generation and resolved effects. Authority checks SHALL cover typed tools, generic file writers, imports, schema saves and every equivalent route that can produce those effects. A preview, cached bootstrap, review disposition or previous step's approval SHALL NOT substitute for the live check.

Effect classification SHALL compare the complete proposed result with current canonical state and independently authorize every effect. A type-addition grant SHALL authorize new definitions only, not edits to existing aliases, parents, guidance, status, replacement fields or other existing definition data. If a single atomic save mixes authorized additions with any unauthorized effect, the entire save SHALL refuse with zero changes; it SHALL NOT silently prune, partially apply or relabel the mixed payload as additive.

Entity creation that includes authored connections SHALL require `edge.add` authority for those effects in addition to `entity.create`. A writer that also introduces a project key or another vocabulary outside the declared additive set SHALL require that effect's separately owned authority; it SHALL NOT treat vocabulary registration as index hygiene or infer it from the enclosing entity grant. If the deployment cannot authorize that effect, the complete atomic write SHALL refuse and offer a separately reviewed path.

Revocation and commit SHALL have a defined ordering: a commit serialized after revocation SHALL refuse; a commit already serialized before revocation SHALL remain an auditable completed effect. Receipts SHALL identify the non-secret authorizing record and executed action without leaking credentials. Replay of an already committed mutation SHALL not reapply effects or need a new grant, while access to its receipt SHALL still obey current disclosure policy. Uncommitted steps and changed payloads SHALL revalidate authority.

#### Scenario: Revocation races with a prepared write

- **WHEN** a grant is revoked after preview and before the leaf's serialized commit check
- **THEN** the write refuses and produces no content effect

#### Scenario: A generic writer attempts the same operation

- **WHEN** a caller uses a generic file route to create an entity or alter a registry in a v2 vault
- **THEN** the same effect classification and authority gate apply as on the typed tool

#### Scenario: A registry addition conceals an existing-definition edit

- **WHEN** a full registry save adds a permitted type and also changes an existing type's alias, parent, guidance or status without separate authority
- **THEN** the complete save refuses and both new and existing registry state remain unchanged

#### Scenario: Entity creation carries additional graph and vocabulary effects

- **WHEN** a decision-entity create includes connections and a previously unregistered project key under an entity-only grant
- **THEN** the complete write refuses without creating the entity, edges or key
- **AND** the response identifies the missing edge and project-key authority rather than silently inheriting them

#### Scenario: Recovery is not fresh authorization

- **WHEN** a multi-step operation has one committed addition and a remaining uncommitted step after grant expiry
- **THEN** the committed receipt remains reconcilable and the remaining step cannot execute without current authority

### Requirement: Unsupported runtime and family states fail closed

The activation record SHALL bind a minimum authority-contract reader/writer version. Supported deployment and rollback paths SHALL refuse structural write service by runtimes that cannot enforce an activated contract; the product SHALL NOT silently discard grants or activation to make an old runtime writable. Unsupported actions, ambiguous scope, unavailable authority state or incompatible family versions SHALL refuse mutation without blocking permitted read-only guidance.

#### Scenario: Rollback cannot restore a weaker write path

- **WHEN** deployment targets a runtime unable to enforce the vault's activated v2 contract
- **THEN** admission refuses write service for that vault until a compatible runtime or explicitly reviewed migration is used

#### Scenario: Lost authority artifacts cannot restore v1

- **WHEN** a vault's authenticated external control record requires vocabulary authority v2 and its local vocabulary database or activation marker is missing, corrupt or mismatched
- **THEN** content mutations refuse; absence of those artifacts cannot restore v1, and permitted reads and trusted activation recovery remain available

#### Scenario: Existing session custody does not imply vocabulary activation

- **WHEN** valid version-1 authorization custody has no vocabulary authority artifacts
- **THEN** ordinary v1 behavior and session issuance remain available without interpreting general governance enrollment as vocabulary-v2 activation

#### Scenario: Custody renewal preserves the authority floor

- **WHEN** an external custody bundle is renewed or republished for a vault requiring vocabulary authority v2
- **THEN** the authenticated floor cannot decrease and an identity substitution cannot erase the activated contract; a floor change requires matching current serving-membership evidence

### Requirement: Native owner setup binds a separate durable consent ceremony

Native initial policy, schema migration and vocabulary activation SHALL authenticate the configured owner in a separate browser-purpose transaction and require explicit CSRF-protected acceptance of each exact finite review. This browser adapter SHALL also remain available as the standalone fallback for subsequent permission control. Routine exact-action approval, grant and revocation MAY use a supported conversational adapter that proves the same owner binding and exact-review checks. Ordinary agent credentials SHALL NOT establish owner consent. Initial policy, schema migration and vocabulary activation SHALL be separately reviewed; activation SHALL create no grants. Migration and deployment publication SHALL run only after an external maintenance runner verifies the exact managed service has stopped.

#### Scenario: Unsupported host refuses before maintenance

- **WHEN** the canonical held-filesystem capability check reports an unsupported host
- **THEN** both native maintenance command entrypoints refuse with the canonical reason before service inspection, managed re-execution or service-manager actions
- **AND** parsing an available service-unit format does not bypass that capability check

#### Scenario: Owner cannot approve an incomplete or stale disclosed preview

- **WHEN** an authenticated owner retrieves or accepts an exact review
- **THEN** full write images require full disclosure under the owner principal, and rendered control data crosses the existing terminal filter
- **AND** changed filtering output, blocked policy or stale before-images refuse the entire review without an approval form or authority mutation
- **AND** acceptance repeats the disclosure check even if an earlier retrieval succeeded; proposed absent paths use the existing conservative path-only release decision

#### Scenario: Preparing migration does not alter serving admission

- **WHEN** an owner prepares initial migration after committing the reviewed baseline policy
- **THEN** a separate process prepares prospective custody settings without changing the serving process environment
- **AND** the accepted review binds the service and environment source; only successful offline migration permits publication of those settings before restart

#### Scenario: Expired review cannot begin new work

- **WHEN** an accepted maintenance review expires before execution begins
- **THEN** execution refuses without publishing custody or authority
- **AND** an expired applying review can recover only an exact previously begun transition proven by retained publication evidence and its original timely start

#### Scenario: Completed maintenance still finishes its approved environment publication

- **WHEN** migration completes but the process exits before the approved custody settings reach the service environment
- **THEN** exact-receipt recovery publishes only those settings and restarts the same service without repeating authority effects

#### Scenario: Later activation needs current consent

- **WHEN** a reviewed deployment publication finishes after the owner review expires and vocabulary authority is not yet active
- **THEN** deployment recovery remains available but authority activation requires a fresh owner review

### Requirement: Native custody renewal is deterministic and owner-authorized

An owner MAY explicitly authorize ongoing renewal of the same installation's serving proof during activation. That permission SHALL be retained atomically with completed owner review and verified against the configured owner and current attachment on each renewal. Renewal SHALL preserve identity, accepted keys, authority floor and permissions, and SHALL NOT perform semantic memory work.

#### Scenario: Overnight restart recovers freshness

- **WHEN** an approved standalone installation restarts after its membership proof expires
- **THEN** the lifecycle renews its verified same-authority custody before ordinary background work
- **AND** current serving admission uses actual current time, not the historical signature verification time

#### Scenario: Interrupted renewal retains the exact target

- **WHEN** renewal publication is partially applied and its target expires before restart
- **THEN** recovery completes that exact retained target before publishing a fresh successor
- **AND** it cannot replace identity, keys, authority floor or grants while recovering
