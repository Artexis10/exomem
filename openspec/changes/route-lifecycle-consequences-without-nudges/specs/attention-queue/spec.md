## ADDED Requirements

### Requirement: Unreflected outcomes is a default family derived from authored bindings

The audit SHALL register an `unreflected_outcomes` category. For each Records manifest whose Planning link carries a `join` and whose reference resolves to a Planning manifest, each open Planning item — lifecycle `active` and status not `completed` or `cancelled` — that at least one record joins to on the declared fields SHALL be one finding carrying the item reference, the Records collection, the joined record references (the first eight plus the total) and the binding that produced it, with the severity of a review candidate and a `signal_version` derived from authored state only. The finding's fingerprint SHALL be the item reference plus the sorted joined record keys, so a new joined record resurfaces a dismissed item under the existing material-change rule while the dismissal record stands. The finding SHALL resolve only when the item leaves the open state, the binding is removed, or the joined records are gone — never by time and never by the runtime mutating either side. The category SHALL be in the default attention union, in the due-state projection categories and in the delta categories, and therefore a registered family for dispositions. An unresolvable reference SHALL produce no finding and SHALL be reported as unevaluated, never silently skipped. Disclosure SHALL follow every other category: a withheld record or item contributes nothing to the finding, the count or the reference list.

#### Scenario: A recorded event on an open item is a candidate

- **WHEN** a Records collection joined on `title` holds a `produced` event for a deliverable whose Planning work item is still queued
- **THEN** the audit reports one `unreflected_outcomes` finding for that item naming the event, and the item appears in the default attention listing

#### Scenario: Twins stay quiet

- **WHEN** the same event exists in a Records collection without a `join`, or the joined work item is already `completed`
- **THEN** no finding is produced

#### Scenario: The transition clears it by state change

- **WHEN** the work item is triaged to `completed` or archived
- **THEN** the finding is gone on the next read without any dismissal being recorded

#### Scenario: A new event resurfaces a dismissed item

- **WHEN** a user dismissed the finding and a second joined record lands on the same open item
- **THEN** the item resurfaces with the new fingerprint and the earlier dismissal record is retained

#### Scenario: Family disposition applies

- **WHEN** the family's disposition is `quiet`
- **THEN** the audit still measures it, and it leaves the default union, the carriers and the write advisories exactly as any other quiet family

#### Scenario: The runtime never performs the transition

- **WHEN** a finding is open
- **THEN** no command, sweep or carrier edits the Planning item or the record; the decider acts through `plan_memory`

### Requirement: A structured write settles its own unreflected outcomes

A record append or update SHALL apply a bounded delta that re-evaluates the open Planning items whose join values equal the written record's, reading one snapshot of the bound Planning collection; a Planning add, update or triage SHALL re-evaluate that one item against the Records collections bound to its collection, reading their snapshots. The read SHALL be bounded by the declared bindings and the bound collections, never by the vault, and its cost SHALL be measured. `reconcile` SHALL remain the healer and the only full recomputation.

#### Scenario: Delta equals recompute for the touched pair

- **WHEN** a record append opens a gap on one item
- **THEN** the projection after the delta equals the projection after a full reconcile for that item, and no other item changed

#### Scenario: Out-of-band edits heal on reconcile

- **WHEN** a person edits the Planning item's status by hand
- **THEN** the projection is stale until `reconcile`, which removes the finding
