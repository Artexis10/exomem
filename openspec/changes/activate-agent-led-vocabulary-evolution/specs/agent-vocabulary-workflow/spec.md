## Purpose

Make vocabulary evolution an ordinary, evidence-backed agent workflow that improves the useful graph while preserving honest abstention, user control and compatibility with future vocabulary families.

## ADDED Requirements

### Requirement: Ordinary work exposes a bounded vocabulary consideration

When permitted by the active structural-suggestion disposition, ordinary capture and review SHALL expose bounded, actionable vocabulary considerations drawn from the current material and available indexed evidence. Considerations SHALL cover entity promotion, existing-entity enrichment, entity-type fit and relationship-type fit, not only unknown authored labels. A generic registered relation SHALL be eligible through structural evidence: an indexed resolved entity pair or canonical generic edge supported by at least two independent origins, with bounded raw contexts. An explicit agent-submitted meaning question SHALL also be eligible without that recurrence threshold. Duplicate copies of one origin SHALL NOT satisfy recurrence; unresolved origin independence SHALL be reported unavailable. The runtime SHALL NOT label the opportunity as a supplier relationship, semantic equivalent or other inferred meaning. The mere use of a generic edge SHALL NOT itself be an error or a reason to demand greater specificity.

Each item SHALL carry a stable reference, family, target identities, evidence references, registry/projection currency, signal fingerprint, supported decisions and a continuation for omitted evidence. Unavailable evidence SHALL be reported as unavailable, not as absence. Item generation SHALL NOT scan or embed the full corpus on the synchronous write path.

#### Scenario: A registered generic edge can still receive useful review

- **WHEN** a capture uses `relates_to` for a resolved pair supported by two independent origins whose raw contexts describe supplying goods
- **THEN** ordinary guidance exposes the pair, origin evidence, raw contexts and registry review route without requiring an unregistered label first
- **AND** the server neither selects a predicate nor rejects the generic edge

#### Scenario: A keyword is not semantic detection

- **WHEN** one context contains the word supplier but has neither independent recurrence nor an explicit agent-submitted meaning question
- **THEN** that keyword alone does not generate a relation-meaning consideration

#### Scenario: Existing entities receive new facts

- **WHEN** the entity-lifecycle evidence provider identifies new durable facts about an existing entity
- **THEN** the work item offers enrichment of that entity and its evidence, not duplicate creation as the default

#### Scenario: Coverage is bounded and honest

- **WHEN** a consideration has more evidence than the response budget or its projection is warming
- **THEN** the response exposes continuation or typed unavailability and does not claim that no other candidates exist

### Requirement: Meaning is decided by the active agent

The workflow SHALL present existing definitions, aliases, direction, applicable parent families and available overlap evidence before a new type is committed. The active agent SHALL choose reuse, enrich, propose-new, generic, no-edge or defer with a concise rationale appropriate to the family. New meanings SHALL require a definition, justification and the family's existing registry validation. Exact identity or alias collisions SHALL be rejected; semantic proximity alone SHALL NOT veto a distinct meaning or select an equivalent meaning automatically.

No server-side reasoning model, numeric semantic-authority score, minimum edge count, type-count target or relation-diversity quota SHALL decide or satisfy the workflow.

#### Scenario: Similar labels describe different meanings

- **WHEN** two nearby relation definitions have different direction or scope and the agent explains the distinction
- **THEN** a structurally valid new proposal remains possible despite lexical overlap
- **AND** the agent must still pass the canonical registry and authority checks

#### Scenario: No useful distinction is supported

- **WHEN** the agent reviews the evidence and finds only a genuine generic connection or no supported connection
- **THEN** generic or no-edge closes consideration of that unchanged evidence with a rationale and without creating a type

#### Scenario: A school does not force a school type

- **WHEN** an organisation-type entity already represents the school and no useful type distinction is supported
- **THEN** reuse or enrichment is valid; a new school type is not demanded by the noun alone

### Requirement: Decisions are durable and completion is evidence-bound

Decision writes SHALL bind to the item fingerprint, reviewed registry hashes and relevant target versions. Stale decisions SHALL refuse without altering content or disposition. Recorded states SHALL distinguish pending consideration, deferred, resolved without mutation, proposed, awaiting approval, applying and applied. A proposal or approval SHALL NOT count as applied; applied SHALL reference successful canonical mutation receipts and the resulting state. A stale or failed application SHALL remain recoverable rather than silently resolved.

Unchanged items already considered SHALL NOT repeatedly interrupt the same conversation. A durable defer or family quiet SHALL suppress unsolicited repetition while preserving explicit review access. New evidence that changes the fingerprint SHALL permit reconsideration; a registry timestamp change alone SHALL NOT create an endless stream of equivalent advice. An ignored item SHALL remain pending without increasing authority.

Workflow notification and semantic consideration SHALL be distinct from canonical integrity findings. A defer, generic/no-edge outcome or family quiet SHALL NOT dismiss, hide or resolve a finding whose owning contract requires state repair, including `entity_type_unregistered`. Linked audit findings SHALL retain their original visibility and state-based resolution rules even when a workflow notification is suppressed. Claiming a consideration resolved SHALL NOT claim its linked integrity defect repaired.

#### Scenario: A proposal is not completion

- **WHEN** a new relation type has been proposed but its registration has not committed
- **THEN** the item remains proposed or awaiting approval and is not reported as an active typed edge

#### Scenario: Registry changes invalidate the decision

- **WHEN** an agent submits a decision against an older registry hash
- **THEN** the write refuses with refresh guidance and no semantic mutation or false resolved state

#### Scenario: Quiet is not clean

- **WHEN** a user quiets the family and requests review in a later session
- **THEN** unresolved work is still inspectable with its reasons and evidence
- **AND** quieting has not granted mutation authority

#### Scenario: Deferral cannot conceal an unregistered entity type

- **WHEN** the agent defers a type-promotion consideration linked to `entity_type_unregistered`
- **THEN** the optional workflow notification can be deferred but the canonical audit and attention finding remains visible until the registry or authored pages change

### Requirement: Vocabulary families share protocol but not unrestricted semantics

The workflow SHALL initially support entity instances, entity types and relation types as distinct families. Every additional supported family SHALL declare a versioned identifier, evidence and resolution contract, validator, persistence owner, allowed decisions, authority-action mapping and compatibility behaviour before it can mutate state. Family registration SHALL be product-controlled, not an executable plugin or permission declaration loaded from vault content.

User-created labels within a supported family SHALL remain open subject to that family's validation; an unknown family SHALL remain unsupported and non-mutating. Existing open observation categories and tags SHALL NOT acquire mandatory registration merely to join the common consideration protocol. A vocabulary definition SHALL NOT introduce arbitrary schema fields, new governed semantic kinds or code execution.

#### Scenario: A future family does not inherit a standing grant

- **WHEN** a runtime adds a new supported vocabulary family
- **THEN** existing grants do not cover it unless their explicit action set already names that supported action under the matching contract version
- **AND** a grant never expands through a wildcard such as all future families

#### Scenario: A vault file cannot install a new capability

- **WHEN** a registry entry names a validator module, permission class or unknown semantic kind
- **THEN** the entry cannot activate that code, authority or kind through vocabulary registration

### Requirement: Shared tools lead to canonical application and graph visibility

Read, decision and application operations SHALL be available through the existing review, triage, connection and schema tool families and equivalent supported REST/CLI routes over the same command leaves. Capability discovery SHALL identify supported operations rather than advertise unavailable adapter routes. No generic review decision SHALL bypass a family validator or execute an arbitrary mutation payload.

Type registration SHALL persist through its existing canonical registry writer before the corresponding typed application is considered active. Graph publication SHALL preserve existing registry-epoch, warming, provenance, raw-label and parent-family contracts. Multi-step recovery SHALL use the governed curation protocol when available, not invent cross-file atomicity.

#### Scenario: Registration is durable before graph projection is current

- **WHEN** a permitted type save commits but graph publication is pending
- **THEN** the response distinguishes registered from graph-current and gives the normal recovery/read route
- **AND** it does not repeat the registration under a new mutation identity

### Requirement: Adoption is verified through unprompted domain tasks

Acceptance SHALL exercise ordinary domain tasks whose prompts do not name the vocabulary mechanism or demand particular labels. Evidence SHALL distinguish consideration delivered, agent decision, canonical write and downstream retrieval/traversal. It SHALL include useful custom-type adoption, truthful reuse, enrichment, justified abstention and deferred work. Each claimed success SHALL be traceable to source material and actual tool results, not an aggregate increase in edges or definitions.

#### Scenario: Tools exist but the agent never considers vocabulary

- **WHEN** registry unit tests pass but an ordinary-task run never receives or acts on its relevant consideration
- **THEN** the adoption acceptance fails even though the registry capability works

#### Scenario: Incidental names remain context

- **WHEN** a task contains repeated boilerplate or an incidental name without a useful recurring identity
- **THEN** acceptance requires abstention from entity proliferation, not a higher entity count
