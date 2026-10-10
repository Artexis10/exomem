## ADDED Requirements

### Requirement: Writes accept agent-declared referents

The `remember`, `observe_memory`, `edit_memory` and `capture_source` commands SHALL accept one optional `referents` argument with one shared schema on MCP, CLI and REST.
A declaration list SHALL hold at most 8 entries.
Each entry SHALL carry `name` (at most 120 characters) and `evidence` (at most 200 characters).
An entry MAY carry `type`, `ref`, `summary` (at most 300 characters), `aliases` (at most 8), `relations` (at most 3) and `identity_decision`.
Each relation SHALL carry `predicate` and exactly one of `subject` or `object`, naming another entry of the same list or a stable ref; the declared referent is the other endpoint.
The `evidence` value SHALL occur verbatim in the text that the write commits, after the writer's Unicode normalization.
A malformed argument SHALL refuse the whole call before any write.

#### Scenario: A malformed declaration writes nothing

- **WHEN** a `remember` call declares a referent whose `evidence` does not occur in its content
- **THEN** the call is refused with an argument error that names the field
- **AND** no page, entity, edge or registry entry is written

#### Scenario: Every door serves one schema

- **WHEN** the same declaration is sent to `observe_memory` through MCP, CLI and REST
- **THEN** the three doors accept the same fields and return the same referent outcomes

### Requirement: Declared referents resolve before anything is created

After the primary write commits, the server SHALL resolve each declaration through the existing entity resolution.
It SHALL use `ref` when given; otherwise it SHALL use exact title and alias resolution, within the declared type's entity family when a `type` is given.
It SHALL report one outcome per declaration: `reused`, `created`, `needs_decision`, `needs_type`, `type_unknown` or `unresolved`.
It SHALL create an entity only through the existing entity writer, and only for a declaration with no visible match, a registered `type` and a `summary`.
A referent outcome SHALL NOT refuse or roll back the primary write.

#### Scenario: An existing entity is reused

- **WHEN** a declaration names the title or an alias of exactly one visible active entity
- **THEN** the outcome is `reused` with that entity's ref
- **AND** no new entity page is created

#### Scenario: A new referent is promoted

- **WHEN** no visible entity matches and the declaration carries a registered `type` and a `summary`
- **THEN** the entity writer creates the entity and the outcome is `created` with its ref
- **AND** the primary write's committed content is unchanged

#### Scenario: A shared name returns the decision to the agent

- **WHEN** the declared name already denotes an active entity of another type
- **THEN** the outcome is `needs_decision` with the bounded candidates and a `candidate_fingerprint`, and nothing is created
- **AND** a later `create-entity` call with `identity_decision {outcome: distinct}` bound to that fingerprint commits the new identity

#### Scenario: An unknown type writes nothing

- **WHEN** the declared `type` is not in the live entity-type registry
- **THEN** the outcome is `type_unknown` with the `schema_memory` inspect and propose routes
- **AND** no entity is created and the registry is unchanged

### Requirement: A declared relation writes one typed edge

Each declared relation SHALL resolve its predicate through the relation registry by key, alias or label.
It SHALL write one typed edge after both endpoints exist.
An edge whose subject is created by the same declaration SHALL render in that creation.
Every other edge SHALL run the `add-relation` leaf.
An unregistered or deprecated predicate SHALL write no edge and SHALL return the registry findings and the propose route.

#### Scenario: An ownership edge reaches the owner's page

- **WHEN** a declaration creates an animal entity and carries the relation `{predicate: owns, subject: <person ref>}`
- **THEN** the person's page gains one `owns` bullet to the new entity through `add-relation`, guarded by the person page's hash
- **AND** the outcome lists the edge with its display label

#### Scenario: An unregistered predicate writes no edge

- **WHEN** a declared relation names a predicate that no registry entry, alias or label resolves
- **THEN** no edge is written and the outcome carries the registry findings and the propose route
- **AND** the declared entity is still reused or created

### Requirement: Typed edges have one additive leaf

`connect_memory` SHALL offer the operation `add-relation` with `path`, `requested_relation`, `target`, `expected_hash` and `why`.
It SHALL append one bullet under the subject page's `## Relations` through the edit writer and write a log entry.
It SHALL return `exists` for an edge that is already present.
It SHALL refuse a stale `expected_hash` and an unregistered or deprecated predicate.
It SHALL refuse a withheld target exactly as a missing target.
It SHALL also be a curation step kind whose compensation removes the bullet.
`create-entity` `connections` SHALL accept `{target, relation}` items beside strings, and a string SHALL keep the registry's generic relation.

#### Scenario: Repeating an edge writes nothing

- **WHEN** `add-relation` is called twice with the same subject, predicate and target
- **THEN** the second call returns `exists` and the page bytes are unchanged

#### Scenario: A withheld target cannot be detected

- **WHEN** a restricted caller calls `add-relation` with a target that is withheld from it, and separately with a target that does not exist
- **THEN** both refusals are byte-identical

#### Scenario: A typed connection renders its predicate

- **WHEN** `create-entity` receives `connections: [{target: <person>, relation: member_of}]`
- **THEN** the new page's `## Relations` holds `- member_of [[<person>]]`
- **AND** a plain string connection still renders the generic relation

### Requirement: Promotions carry provenance

An entity creation or typed edge made by a declaration or by `add-relation` SHALL write a log entry on its page.
The entry SHALL name the originating write's path and operation id.
It SHALL also carry the evidence span when one exists, and the episode key when an episode leaf ran the effect.
A registry save SHALL keep its history header and its reason.

#### Scenario: A created entity names its origin

- **WHEN** a `remember` call creates an entity through a declaration
- **THEN** the entity's log entry names the note's path, the operation id and the evidence span

### Requirement: A retried write replays its referent outcomes

The write's operation identity SHALL bind every referent leaf.
An identical retry SHALL replay the recorded outcomes and SHALL create no second entity and append no second edge.
A failure after the primary commit SHALL leave the unfinished declarations `pending` in the receipt, and the identical retry SHALL complete them once.

#### Scenario: A crash between the primary write and its referents

- **WHEN** the process stops after the primary write commits and before its declarations run, and the client retries the identical call
- **THEN** each declaration runs exactly once and the primary write is not repeated

### Requirement: Promotions surface once in the next session

The due-state carrier SHALL serve a `recent_promotions` category.
It SHALL hold one entry per registry addition, entity creation and typed edge between two entity pages, whichever leaf wrote it.
An edge from a page that is not an entity SHALL NOT be an entry.
The session that created an entry SHALL NOT count it.
Every other session SHALL count it until its first delivery stamps the first-surfaced ledger; then the entry SHALL settle.
A revert or a dismissal SHALL also settle it.
Family dispositions SHALL apply, and a withheld promotion SHALL contribute nothing to a restricted audience's count.

#### Scenario: A promotion is seen once, in the next session

- **WHEN** session A creates an entity and sessions B and then C call bootstrap
- **THEN** session A's counters exclude it, session B's counters include it once, and session C's counters exclude it

#### Scenario: A quieted family stays quiet

- **WHEN** the owner sets the `recent_promotions` family to quiet
- **THEN** no later bootstrap counts its entries, and an explicit review still lists them

### Requirement: Each promotion carries a one-call revert route

Each `recent_promotions` entry's item context SHALL carry one call that reverts it:
`schema_memory restore` to the version before a registry addition, with `also_removes` listing any later keys that the restore would remove;
`manage_memory_file` delete with `confirm` for an entity creation, which moves the page to `_trash`;
and `edit_memory` `replace_string` guarded by the page hash for a typed edge.
A revert SHALL run only on the user's request, and history and logs SHALL keep both the promotion and its revert.

#### Scenario: An edge reverts in one call

- **WHEN** the user asks to undo a promoted `owns` edge and the agent runs the entry's revert call
- **THEN** the bullet is removed, the page log records the removal, and the entry settles

#### Scenario: A stale revert refuses

- **WHEN** the subject page changed after the edge was written
- **THEN** the revert call refuses on its hash guard and nothing is removed

### Requirement: Promotion never discloses a withheld page

For a restricted caller, referent resolution, referent outcomes, the relation advisory, promotion notices and a later activation SHALL be byte-identical between two vaults.
One vault holds a withheld entity that answers to the declared name at another path; the other vault lacks it.
A withheld entity SHALL never be reused, linked, named or counted for that caller.

#### Scenario: Twin vaults give one answer

- **WHEN** the same restricted caller sends the same declaration to both twin vaults
- **THEN** every response, notice count and later activation packet is byte-identical
- **AND** no write reaches the withheld page

### Requirement: The server creates only what the agent declares

The server SHALL NOT detect a referent in prose, choose a type or a predicate, or create an entity, type or edge that no declaration or explicit leaf call names.
A body name without a declaration SHALL stay a wikilink, and the existing advisory candidate rules SHALL apply unchanged.

#### Scenario: An incidental name stays unpromoted

- **WHEN** a note mentions a passer-by's name in its body and declares no referent for it
- **THEN** no entity, edge or promotion notice exists for that name after the write and after publication

### Requirement: The served contract teaches the declaration channel

The compact core SHALL carry one capture line of at most 160 bytes, registered as a core rule.
The line SHALL tell the agent to declare the durable referents that a write names in `referents` and to leave incidental names undeclared.
The `vocabulary` bootstrap section, the scaffold vocabulary reference and the four tool descriptions SHALL carry the full declaration contract.
The compact core SHALL stay within its byte ceiling and its default and maximal headroom bands.

#### Scenario: A hookless client learns the channel from bootstrap

- **WHEN** a client with no installed skill reads the compact bootstrap and the `remember` schema
- **THEN** it learns that durable referents are declared in `referents` with a name, an optional type and an evidence span
