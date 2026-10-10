## ADDED Requirements

### Requirement: Writes accept agent-declared referents

The `remember` and `observe_memory` commands SHALL accept one optional `referents` argument with one shared schema on MCP, CLI and REST.
A declaration list SHALL hold at most 8 entries.
Each entry SHALL carry `name` (at most 120 characters) and `evidence` (at most 200 characters).
An entry MAY carry `type`, `ref`, `summary` (at most 300 characters), `aliases` (at most 8), `relations` (at most 3) and `identity_decision`.
Each relation SHALL carry `predicate` and exactly one of `subject` or `object`, naming another entry of the same list or a stable ref; the declared referent is the other endpoint.
Only a malformed argument shape SHALL refuse the call, before any write.
The `evidence` value SHALL be matched against the committed text after the writer's Unicode normalization, including wikilink display text; for an edit, only against the text that the edit adds.
A span that does not match SHALL give that declaration the outcome `evidence_not_found` and SHALL NOT refuse the primary write.

#### Scenario: A wrong span costs one declaration, not the write

- **WHEN** a `remember` call declares two referents and one evidence span does not occur in its content
- **THEN** the note is written, that declaration's outcome is `evidence_not_found`, and the other declaration runs

#### Scenario: Display text counts as evidence

- **WHEN** the content links `[[Pip|my dog Pip]]` and a declaration's evidence is `my dog Pip`
- **THEN** the span matches

### Requirement: Declared referents resolve before anything is created

After the primary write commits, the server SHALL resolve each declaration through the existing entity resolution: by `ref` when given, otherwise by exact title and alias within the declared type's entity family when a `type` is given.
It SHALL report one outcome per declaration: `reused`, `created`, `needs_decision`, `needs_type`, `type_unknown`, `needs_create`, `unavailable`, `unresolved` or `evidence_not_found`.
It SHALL create an entity only through the existing entity writer, for a declaration with no visible match, a registered `type` and a `summary`.
A restricted caller's declaration SHALL never reach `created`; it SHALL get `needs_create` with the `create-entity` route.
A limited owner's declaration whose type resolution needs withheld private registry definitions SHALL get `unavailable`.
A referent outcome SHALL NOT refuse or roll back the primary write.

#### Scenario: A new referent is promoted

- **WHEN** no visible entity matches and the declaration carries a registered `type` and a `summary`
- **THEN** the entity writer creates the entity and the outcome is `created` with its ref

#### Scenario: A shared name returns the decision to the agent

- **WHEN** the declared name already denotes an active entity of another type
- **THEN** the outcome is `needs_decision` with the bounded candidates and a `candidate_fingerprint`, and nothing is created

#### Scenario: A restricted caller gets the route

- **WHEN** a restricted caller declares a name that no visible entity matches
- **THEN** the outcome is `needs_create` with the `create-entity` route, and nothing is created

### Requirement: A pending declaration completes with one call

A declaration with the outcome `needs_type` or `type_unknown` SHALL stay pending on the write's operation.
The outcome SHALL name one completion call, `connect_memory(operation="complete-referents", ref=<operation ref>)`, which runs every pending declaration of that write once.
Pending declarations SHALL appear in `recent_promotions` until they complete or the agent dismisses them.

#### Scenario: A type is saved, then the declaration completes

- **WHEN** a declaration names the unregistered type `animal`, the agent saves that type, and then runs the completion call
- **THEN** the entity is created with the originating write's provenance and the pending entry settles

### Requirement: A declared relation writes one typed edge

Each declared relation SHALL resolve its predicate by key, alias or label, and SHALL write one typed edge after both endpoints exist, through a typed connection or `add-relation`.
An unregistered or deprecated predicate SHALL write no edge and SHALL return the registry findings and the propose route.

#### Scenario: An ownership edge reaches the owner's page

- **WHEN** a declaration creates an animal entity and carries `{predicate: owns, subject: <person ref>}`
- **THEN** the person's page gains one `owns` bullet to the new entity through `add-relation`

### Requirement: A retried write replays its referent outcomes

The write's operation identity SHALL bind every referent leaf.
An identical retry SHALL replay the recorded outcomes and SHALL create no second entity and append no second edge.

#### Scenario: A crash between the primary write and its referents

- **WHEN** the process stops after the primary write commits and before its declarations run, and the client retries the identical call
- **THEN** each declaration runs exactly once and the primary write is not repeated

### Requirement: The served contract teaches the declaration channel

At `balanced` and `maximal` only, the compact core SHALL carry one registered capture line of at most 160 bytes that tells the agent to declare the durable referents a write names and to leave incidental names undeclared.
The `vocabulary` bootstrap section, the scaffold vocabulary reference and the two tool descriptions SHALL carry the full contract.

#### Scenario: A light-prominence client is not told to declare

- **WHEN** a client reads the compact bootstrap at `light`
- **THEN** the core carries no declaration line
