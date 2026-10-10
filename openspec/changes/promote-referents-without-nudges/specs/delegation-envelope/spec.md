## MODIFIED Requirements

### Requirement: Hard authority ceilings bound every action class

The product SHALL define a closed v1 set of envelope action classes —
`hygiene_writes`, `proactive_capture`, `link_acceptance`,
`structural_suggestions`, `restructure_execution`, `disclosure` — each with a
hard ceiling: `hygiene_writes` silent; `proactive_capture` silent-capable;
`link_acceptance` confirm; `structural_suggestions` advisory (surface only);
`restructure_execution` — covering restructure application, supersession
commit, entity merge, deletion, and edge removal, which is an `edit_memory`
`remove_relation` operation or the removal of an edge from a page that the
current write does not author — confirm-required; `disclosure` governed
exclusively by the governance plane. On a personal vault, additive entity
creation (a new identity through the entity writer's resolve-before-create
path, which refuses or prepares a decision on an existing exact name) belongs
to `proactive_capture` and follows the vault's capture prominence; merging,
superseding or deleting an entity stays under `restructure_execution`.
By the founder ratification of 2026-10-10 (owner decision O2 of
`promote-referents-without-nudges`), when the caller resolves to the vault owner
and the vault has not activated v2 additive authority, an entity-type or
semantic-category registry save, a relation-extension save and a typed edge that
the agent authors also belong to `proactive_capture`. `link_acceptance` covers
acceptance of a relation that the server's relation queue suggested. Replacing a
generic edge in place with a specific predicate is a typed edge the agent
authors, not an edge removal, and any other `edit_memory` operation that drops
a `## Relations` bullet from the page it writes is part of that write, not an
edge removal. A promotion revert is `restructure_execution` whose confirmation
is the user's request; it seals and applies its own curation plan in one call,
the promotion's item context is its preview, and it requires the promotion's
current fingerprint, which covers the dependants and the content it changes and
serves as the plan-fingerprint approval that curation apply requires. Edge
removal and promotion revert SHALL run only on tools that are already marked
destructive. A resolved
non-owner's registry save remains a pending item for the owner. An
additive-authority grant that governs the caller, including the Hosted v2
grants, applies unchanged and is neither widened nor replaced by this class. No
upgrade, migration or default SHALL activate v2 or route a v1 owner's promotion
to an approval step. No prominence level, envelope setting,
disposition, configuration value, or adaptation SHALL authorize behaviour above
a class's ceiling.

Confirm-required SHALL bind at three tiers: the served envelope marks the class
confirm-required; the agent contract requires explicit in-conversation user
confirmation before any surface of the class is invoked; and every server-side
confirmation or preview-first gate that exists today — deletion's explicit
confirm parameter, the adoption apply surface's preview-first default — SHALL
remain required. v1 SHALL add no new server-side confirmation parameter and no
tool-schema change; a server-side confirm for supersession is named future
work behind the documented tool-surface rollout, and its absence today SHALL be
stated in the served contract rather than implied away. The served contract
SHALL also state that additive entity creation, the owner's vocabulary saves
and the agent's own typed edges follow `proactive_capture`, SHALL name the
scoped edge removal as confirm-required, and SHALL NOT tell the owner's agent
that those promotions need confirmation. The served envelope SHALL carry these
classes as a structured map from each promotion action to its class for the
caller, so that no promotion action is left without a class.

A request to set a disposition for an unknown class id SHALL be refused with a
class-specific error and no state change. A request to configure `disclosure`
through the envelope SHALL be refused naming the governance plane as the owner.
An unknown class id or disposition found in the STORED configuration (for
example, written by a newer runtime) SHALL be reported and ignored at read
time, never refused — reading the envelope never breaks bootstrap.

#### Scenario: Maximal prominence cannot lift a ceiling

- **WHEN** prominence is `maximal` and every envelope override is set as
  permissive as its range allows
- **THEN** the served envelope still marks `restructure_execution`
  confirm-required, deletion still requires its explicit confirm parameter, and
  the adoption apply surface still defaults to preview-first

#### Scenario: An unknown class is refused at write and tolerated at read

- **WHEN** an envelope disposition is requested for a class id outside the
  closed v1 set, and separately a stored configuration carries an unknown id
- **THEN** the write is refused with a class-specific error and no state
  change, while the read serves the known classes and reports the unknown id

#### Scenario: Disclosure is not envelope-configurable

- **WHEN** an envelope disposition is requested for `disclosure`
- **THEN** the request is refused naming the governance plane as the owner

#### Scenario: A new identity is created without a confirmation question

- **WHEN** prominence is `balanced` on a personal vault and the agent creates an
  entity whose exact name resolves to no active entity
- **THEN** the creation follows `proactive_capture` and needs no
  in-conversation confirmation
- **AND** merging, superseding or deleting that entity still requires
  confirmed `restructure_execution`
- **AND** a caller governed by an additive-authority grant is held to that
  grant exactly as before

#### Scenario: A type and an edge are saved without a question

- **WHEN** prominence is `balanced`, the owner's agent finds no fitting entity
  type, saves one, and authors an `owns` edge to an entity of that type
- **THEN** both writes follow `proactive_capture` and the agent asks no
  confirmation question

#### Scenario: Queue acceptance keeps its boundary

- **WHEN** the agent accepts a relation that the relation queue suggested
- **THEN** the action remains `link_acceptance` with its confirmation

#### Scenario: A restricted non-owner's save waits for the owner

- **WHEN** a resolved non-owner saves a new relation extension
- **THEN** the registry is unchanged and a pending item carries the delta for
  the owner

#### Scenario: An edge removal is confirmed by the user's request

- **WHEN** the agent runs an `edit_memory` `remove_relation` operation, or a
  write removes an edge from a page that the write does not author
- **THEN** the action is `restructure_execution`, and a user's explicit revert
  request is its confirmation

#### Scenario: An edit to the page it writes is not an edge removal

- **WHEN** the agent's `edit_memory` call with an operation other than
  `remove_relation` drops a `## Relations` bullet from the page that the call
  writes
- **THEN** the edit follows the class of that write and asks no edge-removal
  confirmation

#### Scenario: Every promotion action has a served class

- **WHEN** the owner's agent on a v1 vault reads the served envelope
- **THEN** the structured promotion map gives `proactive_capture` for an entity
  creation, a typed edge and a registry save, `link_acceptance` for queue
  acceptance, and `restructure_execution` for an edge removal and a promotion
  revert

#### Scenario: An upgrade grants and gates nothing

- **WHEN** a runtime that ships owner approvals is installed on a v1 vault
- **THEN** the vault stays v1, and an owner's promotion still takes effect
  without an approval step
