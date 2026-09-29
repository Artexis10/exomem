## MODIFIED Requirements

### Requirement: Hard authority ceilings bound every action class

The product SHALL define a closed v1 set of envelope action classes —
`hygiene_writes`, `proactive_capture`, `link_acceptance`,
`structural_suggestions`, `restructure_execution`, `disclosure` — each with a
hard ceiling: `hygiene_writes` silent; `proactive_capture` silent-capable;
`link_acceptance` confirm; `structural_suggestions` advisory (surface only);
`restructure_execution` — covering restructure application, supersession
commit, entity merge, and deletion — confirm-required; `disclosure` governed
exclusively by the governance plane. On a personal vault, additive entity
creation (a new identity through the entity writer's resolve-before-create
path, which refuses or prepares a decision on an existing exact name) belongs
to `proactive_capture` and follows the vault's capture prominence; merging,
superseding or deleting an entity stays under `restructure_execution`. An
additive-authority grant that governs the caller, including the Hosted v2
grants, applies unchanged and is neither widened nor replaced by this class. No prominence level, envelope setting,
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
SHALL also state that additive entity creation follows `proactive_capture`.

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
