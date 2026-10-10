## ADDED Requirements

### Requirement: Typed edges have additive and removal leaves

`connect_memory` SHALL offer `add-relation` with `path`, `requested_relation`, `target`, `expected_hash` and `why`.
It SHALL append one bullet under the subject page's `## Relations` through the edit writer, write a log entry, return `exists` for an edge that is already present, refuse a stale `expected_hash`, and refuse an unregistered or deprecated predicate with the propose route.
`edit_memory` SHALL offer a `remove_relation` operation kind with `relation` and `target` on the subject page's `path`, keyed by that triple and taking no hash.
It SHALL remove exactly the bullets of that triple and return `absent` when there are none.
Removal SHALL NOT be offered on `connect_memory`, which stays additive, so that every tool's destructive hint stays truthful.
Both leaves SHALL refuse a withheld subject or target exactly as a missing one, and both SHALL be curation step kinds that compensate each other.
`create-entity` `connections` SHALL accept `{target, relation}` items beside strings, and a string SHALL keep the registry's generic relation.

#### Scenario: Repeating an edge writes nothing

- **WHEN** `add-relation` is called twice with the same subject, predicate and target
- **THEN** the second call returns `exists` and the page bytes are unchanged

#### Scenario: Removal touches only its triple

- **WHEN** a `remove_relation` operation removes an `owns` edge while another session has edited a different section of the same page
- **THEN** only the `owns` bullet for that target is gone, the other edit survives, and a repeat returns `absent`

#### Scenario: A withheld target cannot be detected

- **WHEN** a restricted caller calls `add-relation` with a target that is withheld from it, and separately with a target that does not exist
- **THEN** both refusals are byte-identical

#### Scenario: A typed connection renders its predicate

- **WHEN** `create-entity` receives `connections: [{target: <person>, relation: member_of}]`
- **THEN** the new page's `## Relations` holds `- member_of [[<person>]]`
- **AND** a plain string connection still renders the generic relation

### Requirement: Promotions carry provenance

An entity creation or typed edge made by `create-entity`, `add-relation` or a declaration SHALL write a log entry on its page.
`create-entity` SHALL accept the existing `ref` argument as the originating write's ref, and the `undeclared_referents` route SHALL fill it.
The entry SHALL name the originating write's path and operation id when known, and the episode key when an episode leaf ran the effect.
An origin ref that the caller cannot read SHALL record the origin as unknown and SHALL NOT refuse the creation; a withheld ref and a missing ref SHALL give the same result.
A registry save SHALL keep its history header and its reason.

#### Scenario: A created entity names its origin

- **WHEN** an agent runs the `create-entity` route from a note's `undeclared_referents` block
- **THEN** the new entity's log entry names that note's path and operation id

#### Scenario: An unreadable origin does not block the creation

- **WHEN** a restricted caller runs `create-entity` with an origin ref that is withheld from it, and separately with an origin ref that does not exist
- **THEN** both calls create the entity, both log entries record the origin as unknown, and both results are byte-identical

### Requirement: Promotions surface once in the next session

The due-state carrier SHALL serve a `recent_promotions` category with one entry per registry addition, entity creation and typed edge between two entity pages, whichever leaf wrote it.
An edge from a page that is not an entity SHALL NOT be an entry.
A bulk writer SHALL add one entry per originating write.
A parentless entity type's entry SHALL carry `new_family` and SHALL be served first.
The session that created an entry SHALL NOT count it.
A promotion's entry SHALL settle at its first delivery to an interactive conversation session other than the creator's; a hook process or a CLI or REST call SHALL NOT settle it.
The server MAY also exclude a session that a wire field identifies as a delegated agent lane.
A dismissal SHALL settle any entry.
An entry SHALL also settle by state when its promotion no longer holds: the edge is absent, the entity page is in the trash, or the registry key is removed or deprecated.
The entry's review fingerprint SHALL stay the promotion's stable identity for dismissal and settlement, and SHALL NOT fold in content hashes or dependants.
Family dispositions SHALL apply, and a withheld promotion SHALL contribute nothing to a restricted audience's count.

#### Scenario: A promotion is seen once, in the next session

- **WHEN** session A creates an entity and sessions B and then C call bootstrap
- **THEN** session A's counters exclude it, session B's counters include it once, and session C's counters exclude it

#### Scenario: A hook does not consume the notice

- **WHEN** a client hook process calls bootstrap before the owner's next conversation does
- **THEN** the entry stays unsettled and the owner's next conversation counts it

#### Scenario: An import counts once

- **WHEN** one adoption run creates twelve entities
- **THEN** `recent_promotions` holds one entry for that run, naming the twelve

### Requirement: Each promotion reverts in one call

Every `recent_promotions` entry SHALL revert through one call keyed by its ref on a tool that is already destructive, and its item context SHALL name that call.
The item context SHALL serve a `revert_fingerprint`, computed at read time and separate from the entry's review fingerprint.
Every revert SHALL require that `revert_fingerprint`; a stale value SHALL refuse the call and change nothing.
A typed edge or an entity creation SHALL revert through `maintain_memory(mode="curation", curation_action="revert", review_ref=<entry ref>, expected_plan_fingerprint=<revert_fingerprint>, why)`.
For these entries the `revert_fingerprint` SHALL be `curation.plan_fingerprint` of the deterministic revert plan, whose binding manifest carries the dependant list and the content hash of each page that the plan changes.
The call SHALL recompute that plan from current state, seal it and apply it in the same call, so that curation apply's plan-fingerprint approval runs unchanged.
For a typed edge, the plan SHALL hold one `remove_relation` step; for an entity creation, its steps SHALL remove the promotion's own edges and then trash the entity.
The entry's item context SHALL list every other inbound link to the entity as a dependant.
A link from the promotion's originating write SHALL NOT be a dependant, because after the revert it is an unresolved wikilink again, as it was before the promotion.
A registry addition SHALL revert through one `schema_memory` save of that key with a `remove` delta, `expected_hash=<revert_fingerprint>` and `why`, where the `revert_fingerprint` is the registry's current `expected_hash`.
The save SHALL take no `vocabulary_ref` or `vocabulary_fingerprint`.
The server SHALL decide at save time from current usage: it SHALL remove the key when the key is vault-added and nothing uses it, and otherwise SHALL deprecate the key, to its parent for a relation, and list the dependants.
The `remove` verb SHALL be allowed only in a registry whose adapter declares a usage check, which today means entity types, relations and semantic categories, and every other registry SHALL refuse it.
No revert SHALL land on `triage_memory` or `connect_memory`.
A revert SHALL NOT restore an older registry version or drop a later save.
A revert SHALL run only on the user's request, which is its confirmation, and history and logs SHALL keep both the promotion and its revert.

#### Scenario: An edge reverts in one call

- **WHEN** the user asks to undo a promoted `owns` edge and the agent runs the entry's revert call
- **THEN** the one call removes the bullet, the page log records the removal, and the entry settles

#### Scenario: An entity reverts in one call

- **WHEN** the user asks to undo a promoted entity that only the promotion's own edges and its originating note link to
- **THEN** one call removes those edges and moves the entity page to the trash, with no separate preview or apply call

#### Scenario: A revert needs the current revert fingerprint

- **WHEN** the agent runs an entity revert without a `revert_fingerprint`, or with one read before a later note linked the entity
- **THEN** the call refuses, changes nothing, and the entry's item context lists the later note as a dependant
- **AND** after the user has seen and named that dependant, the same call with the newly read `revert_fingerprint` reverts the entity

#### Scenario: A busy page does not re-raise a dismissed promotion

- **WHEN** the agent dismisses a promotion entry, and later writes edit the promoted entity's page and add inbound links to it
- **THEN** the entry's review fingerprint is unchanged and the entry stays dismissed, while its `revert_fingerprint` changes

#### Scenario: A removal by another route settles the entry

- **WHEN** the promoted edge is removed through an `edit_memory` `remove_relation` operation rather than the revert call
- **THEN** the entry settles because the edge is absent

#### Scenario: A used type is deprecated, not removed

- **WHEN** the user asks to undo a promoted entity type that two entities use
- **THEN** the one `schema_memory` save deprecates the type, lists the two entities as dependants, and leaves every later registry save in place

#### Scenario: An unused type is removed

- **WHEN** the user asks to undo a promoted entity type that nothing uses
- **THEN** the one call removes the key and the registry history records the removal

#### Scenario: A registry without a usage check refuses removal

- **WHEN** a save sends the `remove` verb to a registry whose adapter declares no usage check
- **THEN** the save is refused and the registry is unchanged

### Requirement: Promotion never discloses a withheld page

For a restricted caller, the `undeclared_referents` block, the typed-edge leaves, the relation advisory, promotion notices and a later activation SHALL be byte-identical between a vault holding a withheld entity that answers to a linked or declared name and the same vault without it.
This SHALL hold for a withheld entity at another path; at the same path, the entity writer's occupancy refusal names no path and stays reported debt until restricted writers get suffixed filenames.
A withheld entity SHALL never be reused, linked, named or counted for that caller.

#### Scenario: Twin vaults give one answer

- **WHEN** the same restricted caller sends the same write to both twin vaults
- **THEN** every receipt block, advisory, notice count and later activation packet is byte-identical
- **AND** no write reaches the withheld page

### Requirement: The server creates only what the agent asks for

The server SHALL NOT detect a referent in prose, choose a type or a predicate, or create an entity, type or edge that no leaf call or declaration names.
A body name SHALL stay a wikilink until the agent creates its entity.

#### Scenario: An incidental name stays unpromoted

- **WHEN** a note mentions a passer-by's name, with or without a wikilink, and the agent creates nothing for it
- **THEN** no entity, edge or promotion notice exists for that name after the write and after publication
