## ADDED Requirements

### Requirement: Planning manifests support guarded validation, revision, and rebaseline

Planning SHALL expose read-only create-mode and revision-mode manifest validation and guarded mutating revision and rebaseline. Revision SHALL require a complete manifest, exact current manifest hash, exact current container hash, and a concise reason. Rebaseline SHALL require both hashes, the exact currently reported positive gap codes, and a concise reason. Both mutations SHALL preserve collection and Planning identity, append the profile-appropriate audit event, establish the required reader floor, and publish all affected canonical files atomically.

#### Scenario: Revision preflight returns exact guards

- **WHEN** revision-mode validation accepts a complete replacement Planning manifest
- **THEN** it returns exactly the current expected manifest and container hashes needed by guarded revision and performs no write

#### Scenario: Stale Planning revision refuses

- **WHEN** the manifest or collection container changes after validation
- **THEN** revision refuses with the stable stale lifecycle error and changes no canonical or audit file

#### Scenario: Rebaseline acknowledges only measured gaps

- **WHEN** a caller supplies missing, extra, or stale audit gap codes to Planning rebaseline
- **THEN** it refuses rather than blessing unmeasured history or silently discarding a gap

### Requirement: Planning saved views are validated against Planning vocabulary

Planning manifest validation SHALL type-check saved-view predicate literals against the canonical profile vocabulary and any manifest-declared enum constraints. Predicates on `horizon` SHALL use only `inbox`, `week`, `month`, `quarter`, `year`, or `multi-year`; predicates that can never match a valid item SHALL refuse even when the generic field type is string.

#### Scenario: Informal horizon aliases refuse

- **WHEN** a saved view filters `horizon` by `now`, `next`, or `later`
- **THEN** manifest validation identifies the invalid literal and writes nothing

#### Scenario: Canonical horizon view is accepted

- **WHEN** a saved view filters by one or more canonical Planning horizons with otherwise valid query grammar
- **THEN** validation accepts the view and query evaluates it against authored items

### Requirement: Newly scaffolded Planning is readable in ordinary Markdown tools

Newly scaffolded Planning Markdown-item manifests SHALL declare an `item_filename` recipe over the title-bearing natural key and a bounded `item_presentation` that exposes title, core planning state, useful context, and configured typed relationships. The presentation SHALL remain authored intent and SHALL NOT calculate progress, urgency, priority, confidence, or execution state.

#### Scenario: New planning item has a meaningful file and body

- **WHEN** a new scaffolded Planning collection receives its first item
- **THEN** the item is discoverable by its human title in the file tree and its Markdown body exposes the selected authored planning fields

#### Scenario: Presentation does not adjudicate the plan

- **WHEN** an item has Records evidence or external execution pointers
- **THEN** the body may link or label the authored descriptors but does not infer completion, health, urgency, or next action

