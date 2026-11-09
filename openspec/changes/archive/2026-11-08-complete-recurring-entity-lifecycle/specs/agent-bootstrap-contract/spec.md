## ADDED Requirements

### Requirement: Bootstrap teaches resolve-before-create and hydrate-before-duplicate
The bootstrap entity-capture contract SHALL teach a type-neutral recurring identity decision loop: inspect the candidate evidence; resolve exact and alias matches first; stop on ambiguity; hydrate one resolved Entity before considering duplication; create only when no active Entity resolves and the active agent judges the identity stable, reusable, and useful; and register an unknown durable kind only through the guarded registry save with rationale. The contract SHALL identify candidate detection as read-only measurement, the active agent as the sole semantic decider, and governed curation as the mutation route.

Each active type entry SHALL expose its canonical leaf ID, folder, and derived family. The guidance MUST NOT maintain a community-, person-, organisation-, or other kind-specific decision branch.

While `entity_recurrence` remains outside the unfiltered attention union, balanced and maximal bootstrap SHALL direct the active agent to request that explicit category at most once ordinarily per bootstrapped conversation or session: during the first user turn handled after successful bootstrap, after primary task work and before the final response, whether or not the user named an entity topic. It MAY request at most one additional general state recheck after an Entity, accepted relation, or entity-type registry mutation that is not already a hydration batch. Each ordinary or general check SHALL request one audit pass and at most three candidates.

For hydration only, a terminal receipt from one separately confirmed batch SHALL authorize exactly one immediate recheck bound to the same identity. Rechecks after processed batches 1–7 MAY expose at most the next eight contexts; the recheck after processed batch 8 SHALL be closure-only and SHALL expose only closed or `deferred_remaining_count`, never a ninth batch. A returned batch authorizes no further call until it is separately confirmed and reaches a terminal mutation receipt. The chain SHALL obey the defined stop conditions, mutate at most eight batches, and perform at most eight identity rechecks per session; any remainder stays open for the next session's ordinary read. Off and light SHALL remain explicit-request-only.

If the exported surface cannot request an explicit category or attention read, bootstrap SHALL state that the check is unavailable and skip it. It MUST NOT simulate the check through a local scan, a model, an embedding call, due-state, or an invented command. The check itself authorizes no mutation.

#### Scenario: Recurring identity resolves to one Entity
- **WHEN** bootstrap guidance is applied to a hydration candidate with one active exact or alias match
- **THEN** it directs the agent to inspect and connect or update that Entity
- **AND** it forbids duplicate `create-entity` routing

#### Scenario: Recurring identity is ambiguous
- **WHEN** candidate resolution returns several active Entities
- **THEN** bootstrap directs the agent to reconcile or request clarification
- **AND** it does not select a target, type, merge, or curation plan

#### Scenario: Recurring identity has no registered kind
- **WHEN** a stable reusable identity warrants promotion but no active type fits
- **THEN** bootstrap directs the agent to propose a generic registry extension with `why`
- **AND** to refresh resolution after the guarded save before authoring an Entity plan

#### Scenario: Any stable kind follows the same decision loop
- **WHEN** the candidate is a community, account, place, venue, product, project, person, organisation, or another registered kind
- **THEN** bootstrap applies the same resolve, ambiguity, hydration, promotion, and governance rules

#### Scenario: Candidate reaches the agent without a user reminder
- **WHEN** a balanced or maximal agent completes primary work on the first user turn after bootstrap and has not yet sent the final response
- **THEN** bootstrap directs the active agent to request the explicit `entity_recurrence` category and inspect any candidate
- **AND** the user need not ask whether the recurring identity should become an Entity

#### Scenario: Recurrence read is bounded per session
- **WHEN** a balanced or maximal session crosses several later interaction boundaries without an Entity, relation, or registry mutation
- **THEN** it performs no second recurrence read
- **AND** one qualifying non-hydration mutation permits exactly one immediate general state recheck

#### Scenario: Confirmed hydration batches have a separate bounded continuation
- **WHEN** a confirmed hydration batch reaches a terminal mutation receipt and disconnected contexts remain
- **THEN** before the eighth processed batch, the agent may recheck only that identity once for at most eight next contexts
- **AND** after the eighth processed batch its eighth recheck is closure-only and leaves any remainder for the next session

#### Scenario: Incapable surface stays honest
- **WHEN** an active client exports no command capable of an explicit category or attention read
- **THEN** bootstrap marks the recurrence check unavailable and the agent skips it
- **AND** no local corpus scan, model inference, due-state claim, or nonexistent command substitutes for it
