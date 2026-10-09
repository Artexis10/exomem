## ADDED Requirements

### Requirement: Planning values are a governed vocabulary registry
Planning SHALL read kind, status, priority, commitment, horizon, and health values from the `planning-values` registry: a shipped pack plus the vault overlay `_Schema/planning-values.yaml`, saved and restored through `schema_memory(subject="planning-values")`. Each entry key SHALL be `<field>.<value>`, and a Planning item SHALL store the bare value. Shipped entries SHALL NOT be overridden or deprecated. A status SHALL declare its planning `class` (`open`, `done`, or `dropped`); a kind SHALL declare `parents`, a list of registered kinds. Both SHALL be fixed once saved. Code SHALL branch on `class` and `parents`, never on a vault value.

A value equal to the stored item's value SHALL stay readable after its definition is deprecated or removed. A new or changed value SHALL be an active registered value. A caller that cannot admit the overlay SHALL see only shipped values, and a write that depends on a vault value SHALL refuse with `PLANNING_VALUES_UNAVAILABLE`. A status with no readable class SHALL count as not settled for the open-item rule. A write SHALL judge a kind's `parents` only for the item it changes, and only when that item's kind, parent, commitment, or lifecycle changes; every other item SHALL keep the shipped kind rules, so a read never depends on the registry.

#### Scenario: A vault status is used and classified
- **WHEN** the owner saves `status.waiting` with class `open` and triages a committed item to `waiting`
- **THEN** `plan_memory` accepts it, the item counts as open for unreflected outcomes, and archiving it refuses

#### Scenario: A dropped vault status admits the archive
- **WHEN** an item moves to a vault status with class `dropped`
- **THEN** it leaves the open state and an update to lifecycle `archived` succeeds

#### Scenario: A restore keeps stored items readable
- **WHEN** the owner restores the registry version before the vault statuses were saved
- **THEN** query still returns the items that use them, an edit that leaves the status alone succeeds, and a new use of a removed status refuses

#### Scenario: A kind registered again with other parents keeps stored items readable
- **WHEN** the owner restores the version before `kind.epic` and saves `kind.epic` again with different `parents`
- **THEN** query and inspect still read the epics stored under the earlier parents

#### Scenario: A caller without the vault definitions cannot re-parent a vault kind
- **WHEN** a caller who cannot admit the overlay changes the parent of an item whose kind is a vault kind
- **THEN** the write refuses with `PLANNING_VALUES_UNAVAILABLE` and changes nothing

## MODIFIED Requirements

### Requirement: Minimal typed Planning core
Every Planning item SHALL declare `type: plan`, `collection_id`, `plan_id`, positive `schema_version`, non-empty `title`, one `kind`, and one `lifecycle`. Kind, status, priority, commitment, horizon, and health values SHALL come from the `planning-values` registry. Its shipped kinds SHALL be `area`, `outcome`, `initiative`, and `work-item`; lifecycle SHALL be `active` or `archived`. Outcomes, initiatives, and work items SHALL also declare `status`, `priority`, `commitment`, and `horizon`. Shipped statuses SHALL be `candidate`, `planned`, `active`, `blocked`, `completed`, and `cancelled`; shipped priorities `critical`, `high`, `medium`, `low`, and `none`; shipped commitments `uncommitted`, `considering`, and `committed`; shipped horizons `inbox`, `week`, `month`, `quarter`, `year`, and `multi-year`. A vault MAY add values; shipped values SHALL NOT change. Areas SHALL omit status, priority, commitment, horizon, area, and parent.

The profile SHALL accept optional `health`, `window_start`, `window_end`, `area`, `parent`, `progress_evidence`, `execution`, `tags`, and manifest-declared domain fields. Shipped health values SHALL be `unknown`, `on-track`, `at-risk`, and `off-track`. `collection_id` and `plan_id` SHALL be UUIDs; title SHALL be at most 512 UTF-8 bytes; body and each declared domain string SHALL reuse the existing 32 KiB value bound; tags SHALL contain at most 32 distinct non-empty strings of at most 128 UTF-8 bytes. Arrays and nested mappings SHALL be strict JSON-compatible values with no unknown reserved system fields. Add `item` and update `changes` SHALL accept authored fields only and SHALL reject `type`, `collection_id`, `plan_id`, `schema_version`, `item_version`, and audit-marker mutation. Area/non-area kind conversion SHALL refuse after creation; deliverable kind changes MAY occur only among non-area kinds and SHALL validate the complete final state/hierarchy. The schema SHALL remain independent from templates and the Markdown body.

#### Scenario: Minimal capture receives safe explicit defaults
- **WHEN** `add` receives only a title and no explicit structural fields
- **THEN** it creates an active-lifecycle work item with `candidate`, `none`, `uncommitted`, `inbox`, and `unknown` defaults and returns every authored/defaulted value

#### Scenario: Area does not require a delivery horizon
- **WHEN** a user creates an ongoing area without priority, commitment, or horizon
- **THEN** the item validates without pretending the area is a time-bounded goal

#### Scenario: Domain pack extends rather than forks core
- **WHEN** a Planning manifest adds valid typed domain fields, views, tags, or templates
- **THEN** the shared Planning semantics remain unchanged and the extra fields validate through the declared schema

#### Scenario: Unknown field remains visible as a direct-edit finding
- **WHEN** a human adds an undeclared property to an otherwise readable Planning item
- **THEN** inspection reports the schema violation without dropping, rewriting, or silently adopting the property

### Requirement: Explicit lifecycle and horizon semantics
Horizon SHALL be an authored planning bucket, not an automatically moving time calculation. Named horizon views SHALL select exact authored values and SHALL label that provenance; `week` SHALL NOT claim calendar freshness. Optional `window_start` and `window_end` SHALL be ISO dates and SHALL be independently filterable; when both exist, start SHALL NOT be after end.

An area SHALL use lifecycle alone and MAY be archived directly. A candidate deliverable SHALL be active-lifecycle, uncommitted, and inbox. A planned deliverable SHALL be active-lifecycle, considering or committed, and outside inbox. An active or blocked deliverable SHALL be active-lifecycle, committed, and outside inbox. Only deliverables whose status has planning class `done` or `dropped` MAY be archived; shipped `completed` is `done` and shipped `cancelled` is `dropped`. A completed deliverable SHALL be committed and outside inbox. A cancelled deliverable MAY use any declared priority, commitment, and horizon combination, including uncommitted inbox cancellation, because those fields are explicit final state rather than inferred prior state. Reopening or changing any state SHALL be explicit and SHALL validate the complete final combination. The candidate, planned, active, blocked, and completed rules name shipped statuses; a vault status takes only its planning-class rule. No automatic transition exists.

#### Scenario: Time passage does not rebucket intent
- **WHEN** an item remains in the `week` horizon after the authored week passes
- **THEN** Exomem leaves the item unchanged, labels it as authored bucket state rather than calendar fact, and waits for explicit triage

#### Scenario: Invalid date window refuses mutation
- **WHEN** an add, update, or triage request makes `window_start` later than `window_end`
- **THEN** validation refuses before canonical publication

#### Scenario: Archive preserves completed status
- **WHEN** a completed item is explicitly archived
- **THEN** lifecycle becomes archived, status remains completed, and audit history preserves the transition

#### Scenario: Archived candidate refuses
- **WHEN** an update attempts to archive a candidate, planned, active, or blocked deliverable
- **THEN** validation refuses until the same guarded update supplies a coherent completed or cancelled terminal state

#### Scenario: Record changes do not change plan status
- **WHEN** linked Records receive new rows that appear relevant to a plan
- **THEN** Planning status, health, commitment, and horizon remain exactly as authored

### Requirement: Outcomes above initiatives and work items
Planning SHALL keep ongoing area membership separate from the desired-outcome hierarchy. A kind's registered `parents` SHALL name the kinds its parent may have; an empty list SHALL mean the kind takes no parent. Shipped outcomes SHALL take no parent, an initiative MAY name exactly one outcome parent, and a work item MAY name exactly one initiative parent. A committed item of a kind with parents must have a parent while active-lifecycle; archived deliverables MAY retain their last valid hierarchy. An area SHALL NOT have a parent and MAY be referenced by an outcome, initiative, or work item through the separate `area` property. Candidate and considering items MAY omit parent and area. Parent and area values SHALL be canonical same-collection `exomem://plan/<collection-uuid>/<plan-uuid>` references to authorized items of the required kind. An active-lifecycle source item SHALL reference only active-lifecycle targets; an archived source item MAY retain correctly typed links to active or archived targets. Missing, withheld, or structurally invalid targets SHALL refuse with the same bounded relation error. If child and parent both declare area, both references SHALL match; absent area SHALL remain absent rather than being copied or inferred.

#### Scenario: Valid planning chain is queryable
- **WHEN** an outcome contains an initiative that contains a work item and all three reference one area
- **THEN** bounded hierarchy output preserves outcome above initiative above work item while reporting area as a container rather than another goal level

#### Scenario: Inbox capture requires no premature hierarchy
- **WHEN** a candidate work item is captured before its outcome or initiative is known
- **THEN** add succeeds without a parent and triage can assign the relationship later with current stale-write guards

#### Scenario: Invalid parent kind refuses
- **WHEN** a work item names an outcome or area as its parent
- **THEN** mutation refuses without rewriting either item

#### Scenario: Hierarchy cycle refuses
- **WHEN** an update or triage operation would introduce a direct or transitive parent cycle
- **THEN** it refuses before publication and returns no hidden target content

#### Scenario: Archiving a live parent with active children refuses
- **WHEN** update would archive an outcome or initiative that still has active-lifecycle children
- **THEN** it refuses and requires the children to be moved or archived explicitly

#### Scenario: Archiving an area with active members refuses
- **WHEN** update would archive an area still referenced by an active-lifecycle outcome, initiative, or work item
- **THEN** it refuses and requires those memberships to be moved, cleared, or archived explicitly

### Requirement: Planning saved views are validated against Planning vocabulary

Planning manifest validation SHALL type-check saved-view predicate literals against the canonical profile vocabulary and any manifest-declared enum constraints. Predicates on `horizon` SHALL use only horizons registered in `planning-values`, deprecated ones included; predicates that can never match a valid item SHALL refuse even when the generic field type is string.

#### Scenario: Informal horizon aliases refuse

- **WHEN** a saved view filters `horizon` by `now`, `next`, or `later`
- **THEN** manifest validation identifies the invalid literal and writes nothing

#### Scenario: Canonical horizon view is accepted

- **WHEN** a saved view filters by one or more canonical Planning horizons with otherwise valid query grammar
- **THEN** validation accepts the view and query evaluates it against authored items
