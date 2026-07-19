## ADDED Requirements

### Requirement: Capture Guidance Covers Durable Entities As Well As Conclusions
The shipped capture hook, scaffold, workflow skills, and bootstrap contract SHALL prompt the reasoning agent to consider both durable conclusions and durable recurring entities. Guidance MUST refer to the active entity registry or knowledge packs rather than maintain a frozen enumeration of entity kinds.

#### Scenario: Session accumulates durable knowledge about a person or organization
- **WHEN** a session establishes reusable facts, decisions, history, or relations about a recurring named person or organization
- **THEN** capture guidance asks the agent to resolve an existing registered entity before creating another page
- **AND** Notes remain available for conclusions while the entity page becomes the durable node for that actor

#### Scenario: Registry gains a supported entity kind
- **WHEN** a later release adds a kind to the central registry
- **THEN** bootstrap exposes the new kind and its capture guidance without editing the hook's prose or an independent Python entity switch

### Requirement: Proactive Entity Capture Is Conservative
The agent guidance SHALL require an entity to have a stable identity and likely reuse beyond the current source or moment before creation. A single incidental mention, unresolved identity, transient participant, or speculative extraction MUST NOT trigger entity creation.

#### Scenario: Incidental proper noun appears once
- **WHEN** a source mentions a name without durable reusable context
- **THEN** the guidance keeps the name in the source/note context and does not create an entity page

#### Scenario: Durable entity is absent
- **WHEN** exact and alias-aware lookup finds no existing entity and the identity is durable, recurring, and useful
- **THEN** the agent may create it through the canonical registered entity route and report the created path

### Requirement: Existing Entities Are Updated Or Linked Before Duplication
When a durable entity already exists, guidance SHALL prefer a guarded surgical update for new stable facts and the canonical relation route for new connections. It MUST NOT use create-only entity routing to duplicate the page, and substantial rewrites SHALL remain governed through replacement/supersession.

#### Scenario: Existing person gains a durable affiliation
- **WHEN** lookup resolves one existing person and the session establishes a stable new affiliation
- **THEN** the agent uses a guarded `edit_memory` update against the current hash and verification-reads the result
- **AND** it does not create a second person page

#### Scenario: Existing entity only gains a relationship
- **WHEN** the new durable information is a relationship to another governed page
- **THEN** the agent uses the canonical relation workflow and preserves both entity identities

### Requirement: Capture Routing Stays Agent-Side
Exomem SHALL expose deterministic registry, lookup, and graph measurements but MUST NOT run a server-side reasoning model or autonomously create entity pages from prose. The reasoning client SHALL decide whether the durability and usefulness criteria are met.

#### Scenario: Capture nudge fires
- **WHEN** the capture hook emits its bounded advisory
- **THEN** it contains no extracted identity claim and performs no write
- **AND** all entity creation or update occurs through an explicit governed tool invocation
