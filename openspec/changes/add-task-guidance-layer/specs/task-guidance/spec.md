## ADDED Requirements

### Requirement: Activities live in a vocabulary registry

Exomem SHALL keep the kinds of task that a vault names in one vocabulary registry, subject `activities`. The shipped pack `src/exomem/vocabulary/packs/core/activities.yaml` SHALL declare no entries. A vault SHALL add entries in its overlay `_Schema/activities.yaml` through the shared registry contract: `schema_memory(subject="activities")` with inspect, propose, save, history and restore, the hash guard and a reason. An owner save SHALL take effect at once and a nonowner save SHALL wait as pending, as for every registry.

Each entry SHALL have a key and a one-line `description` attribute of at most 160 characters. A key SHALL use the activity-selector grammar that workflow contracts already define: 1–64 ASCII bytes matching `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`. Aliases and deprecation SHALL follow the shared registry contract. A declared alias SHALL resolve to its canonical key. A deprecated key SHALL resolve to its replacement, and a deprecated key without a replacement SHALL count as unknown.

The registry SHALL be default-deny for a restricted caller. The product route SHALL filter the overlay through the caller's release authority before it reads an entry. For a caller to whom the overlay is not released, the registry SHALL be absent: the caller gets no activity index, and every key it declares counts as unknown. Code SHALL NOT hold a list, set or keyed mapping of activity keys.

#### Scenario: A new vault has no activities

- **WHEN** a vault with no `_Schema/activities.yaml` is inspected through `schema_memory(subject="activities")`
- **THEN** the registry reports zero entries and no error

#### Scenario: The owner registers an activity

- **WHEN** the owner saves the entry `review-contract` with the description "Review a draft contract before signature" and a reason
- **THEN** the entry takes effect at once, its history records the reason, and the next maps packet lists it in the activity index

#### Scenario: A restricted caller sees no registry

- **WHEN** a caller whose release authority excludes the overlay declares `review-contract`
- **THEN** its packet carries no activity index and counts the key as unknown, exactly as for a vault where the key does not exist

### Requirement: Guidance roles live in a vocabulary registry

Exomem SHALL keep the roles that a guidance item can declare in one vocabulary registry, subject `guidance-roles`, with a shipped pack and a vault overlay `_Schema/guidance-roles.yaml` under the shared registry contract. The shipped pack SHALL declare three entries: `method` with family `method`, and `precedent` and `failure` with family `case`. An overlay MAY add entries, and each entry SHALL declare a `family`. The `family` attribute SHALL be the closed set `method` (how to do the kind of task) and `case` (a past instance to learn from), which this specification fixes. Code SHALL branch only on `family`, never on a role key.

#### Scenario: An overlay role groups by its family

- **WHEN** a vault adds the role `checklist` with family `method` and a page declares `guidance: checklist`
- **THEN** the guidance block lists that page in the method group, and no code path names `checklist`

#### Scenario: An undeclared role is a finding

- **WHEN** a page declares `guidance: recipe` and no registry entry has that key
- **THEN** the page's validation reports an unregistered guidance role that names `schema_memory(subject="guidance-roles")`, and the page is served with no role

### Requirement: Pages and units declare guidance scope

A guidance item SHALL be a page or a rich semantic unit that declares a scope. A page SHALL declare scope in frontmatter and a rich unit SHALL declare it in governed metadata rows (semantic-unit-language). The declarations are:

- `activities`: a list of activity keys, at most 16, each in the activity registry;
- an entity scope: a relation of family `entity` from the item to an entity page, as a note-level relation for a page or a block relation for a rich unit; the shipped relation `about_entity` has that family, and a relation that a vault adds to the family scopes in the same way;
- `guidance`: an optional role key from the guidance-roles registry;
- `stance: true`: a page-level mark that makes the page a stance page;
- `guidance_origin`: the recorded origin of the declarations above, `inferred` or `reviewed`.

Code SHALL read an entity scope only through the closed relation family `entity` and the closed anchor kind `entity`, never through a relation key, an entity-type key or the entity-type `cue_nouns` attribute. A page declaration SHALL NOT pass to the page's units, and a unit declaration SHALL NOT pass to its page. Scope SHALL change no authority: supersession and `contradicts` relations apply as before.

A governed write SHALL refuse an `activities` value that holds a key absent from the caller's view of the activity registry, and its error SHALL name `schema_memory(subject="activities")`. A hand edit that holds such a key SHALL produce a validation finding, and the key SHALL match no declaration.

#### Scenario: A page declares a method for one activity

- **WHEN** a page carries `activities: [review-contract]` and `guidance: method` in frontmatter
- **THEN** the page is a guidance item for `review-contract` in the method group, and none of its units becomes a guidance item by inheritance

#### Scenario: A rich unit declares a precedent for one entity

- **WHEN** a rich unit carries `- activities: reply-message`, `- guidance: precedent` and the block relation `about_entity` to the page of an invented person A
- **THEN** the unit is a guidance item scoped to `reply-message` and to person A

#### Scenario: A governed write refuses an unregistered activity

- **WHEN** an agent writes a page with `activities: [plan-trip]` and the registry has no `plan-trip`
- **THEN** the write is refused, and the error names `schema_memory(subject="activities")` as the route to register the key

### Requirement: Only owner-written or owner-reviewed declarations are served

A declaration's origin SHALL decide whether it is served. A page or unit with no `guidance_origin` SHALL read as owner-written (`authored`), because only a hand edit writes a declaration without an origin. `guidance_origin: reviewed` SHALL read as owner-reviewed. `guidance_origin: inferred` SHALL never be served as guidance or as a stance line.

A governed write that adds or changes `activities`, `guidance`, `stance` or an entity-family relation on an item SHALL record `guidance_origin: inferred`, unless an owner principal passes the owner's confirmation, which records `reviewed`. A nonowner principal SHALL only record `inferred`. Compact lines of a stance page SHALL take the page's origin. A governed write that adds a line to a stance page SHALL record the page's origin again under the same rule.

Each served guidance item SHALL report its origin in `why.origin` as `authored` or `reviewed`.

#### Scenario: An agent's proposal waits for review

- **WHEN** a delegated agent declares `activities: [review-contract]` on a page through a governed write
- **THEN** the page records `guidance_origin: inferred` and is not served as guidance until the owner confirms it

#### Scenario: The owner confirms a proposal

- **WHEN** the owner's agent writes the same declaration with the owner's confirmation under the owner principal
- **THEN** the page records `guidance_origin: reviewed`, and the next packet that declares `review-contract` serves it with `why.origin = "reviewed"`

#### Scenario: A hand-written declaration is served as authored

- **WHEN** the owner adds `activities: [review-contract]` to a page in an editor and adds no origin
- **THEN** the page is served for `review-contract` with `why.origin = "authored"`

### Requirement: Every maps packet carries a stance block

Every maps-mode packet SHALL carry a `stance` block, after the continuity fields and before the map. The block SHALL hold two parts, each under its own hard character cap:

- `lines`: the stance lines, each an object `{ref, text}` with `review_due` when it applies, under `STANCE_LINES_MAX_CHARS`;
- `activities`: the activity index, each an object `{key, description}` from the caller's view of the activity registry, under `STANCE_INDEX_MAX_CHARS`.

A stance line SHALL be a semantic unit of a stance page whose origin is served. A line SHALL enter the block only when it is unscoped: neither the line nor its page declares `activities` or an entity-family relation. A scoped stance page or a scoped rich unit on a stance page SHALL be a guidance item instead. Lines SHALL follow the stance pages in path order, then the units in page order. Each part SHALL keep whole items only and report `omitted` with the count of items that did not fit. The block SHALL report `state`: `ready` when it holds at least one line or index entry, `empty` when it holds neither, and `unavailable` when its compile failed. A failure SHALL leave the map and the slice served.

The block SHALL be computed over the caller's view. The packet budget SHALL charge the characters that the block uses to `max_chars` before the map is filled, so the map gives up only what the block uses. An empty block SHALL cost only its keys.

#### Scenario: A vault with no stance pages serves an empty block

- **WHEN** a maps packet is compiled for a vault with no stance page and no registered activity
- **THEN** the packet carries `stance.state = "empty"` and the map receives the same budget as before this change, less the block's keys

#### Scenario: Overflow is counted, not cut

- **WHEN** the stance pages hold forty lines and only twelve fit under `STANCE_LINES_MAX_CHARS`
- **THEN** the block serves twelve whole lines and reports `omitted: 28` in `lines`

#### Scenario: A withheld stance page is absent

- **WHEN** a restricted caller cannot see one of two stance pages
- **THEN** its block holds only the visible page's lines, and its counts equal those of a vault without the withheld page

### Requirement: The stance block is cached by generation and by its next date boundary

The stance block SHALL be cached apart from the packet. Its cache key SHALL cover the caller's view, the hash of the caller's view of the activity registry, the generation of the stance inputs, and the next date boundary that any of its lines crosses. The next date boundary SHALL be the earliest day after a line's `until` or `check_by` that is later than today, where today is the calendar day that the due-state projection uses. The block SHALL carry its own `generation`, which changes when any input of the key changes. A packet whose `conversation` bypasses the packet cache SHALL still reuse a cached stance block.

#### Scenario: An expiring line leaves the block on time

- **WHEN** a stance line carries `until: 2026-12-01` and the block was cached on 2026-11-30
- **THEN** the first packet compiled on 2026-12-02 misses the stance cache, withholds the line and counts it as expired

#### Scenario: A registry save refreshes the index

- **WHEN** the owner registers a new activity
- **THEN** the next packet carries a new stance `generation` and the index lists the new key

### Requirement: The client declares the activity

A maps-mode activation SHALL read the declared activity keys from `conversation.activities` (conversation-aware-activation). It SHALL resolve each key against the caller's view of the activity registry, through aliases and deprecation. It SHALL count as unknown, in `guidance.unknown_activities`, every key that is malformed, beyond the fourth, absent from the caller's view, or deprecated without a replacement. Duplicate keys SHALL be removed before counting. The packet SHALL list the resolved keys in `guidance.activities` and SHALL NOT echo an unknown key. A declaration SHALL never refuse the call.

The declaration SHALL be request-scoped: the continuity token SHALL NOT carry it, and a later call declares again.

#### Scenario: An unknown key is counted, not refused

- **WHEN** a client declares `["review-contract", "Review Contract!", "plan-trip"]` and only `review-contract` is registered
- **THEN** the packet is served with `guidance.activities = ["review-contract"]` and `guidance.unknown_activities = 2`, and no packet field contains `plan-trip`

#### Scenario: An alias resolves to its key

- **WHEN** the registry declares `contract-review` as an alias of `review-contract` and a client declares the alias
- **THEN** the packet lists `review-contract` in `guidance.activities`

### Requirement: The guidance block serves refs with their reason inside the slice reserve

Every maps-mode packet SHALL carry a `guidance` block. For each guidance item that the scope rule selects, the block SHALL list `{ref, title, role, why}`, where `role` is the item's role key or null and `why` is `{activity, entity, origin}`:

- `activity` is the declared key that the item matched, or null;
- `entity` is the declared entity ref that the item matched, or null;
- `origin` is `authored` or `reviewed`.

The block SHALL serve refs, never item text; a client reads an item through `read_memory`. Items SHALL be ordered by scope (activity and entity, then entity, then activity), then by role family (`method`, then `case`, then no role), then by ref. The block SHALL carry a fixed `description` that names it as guidance that the vault declares for the declared activity, and says to read a ref before relying on it.

The block SHALL be charged to the turn slice's reserve: guidance items SHALL be placed first and slice items SHALL fill the rest of the reserve. Both SHALL keep whole items only, and the block SHALL report `omitted` with the count of selected items that did not fit. The map budget SHALL be unchanged by the guidance block.

#### Scenario: Guidance displaces slice items, not the map

- **WHEN** a client declares `review-contract`, two guidance items are selected and the slice reserve holds five slice items without them
- **THEN** the packet lists the two guidance items and as many slice items as still fit in the reserve, and the map items equal those of the same request without the declaration

#### Scenario: Guidance is served by ref

- **WHEN** a method page is selected
- **THEN** the guidance item carries its ref, title, role and `why`, and no text from the page body

### Requirement: Guidance states are a closed set

The guidance block SHALL report exactly one `state` from the closed set that this specification fixes:

- `selected`: at least one guidance item is served;
- `none_reviewed`: at least one declared key resolved and no item is served for it;
- `unknown_activity`: keys were declared and none resolved;
- `none_declared`: no key was declared;
- `unavailable`: the guidance lookup failed, and the map and the slice are still served.

The first matching state in this order SHALL win. An item withheld from the caller, an item whose origin is `inferred`, and an item that currency makes history SHALL each count as absent and SHALL NOT produce a state of its own.

#### Scenario: A known activity without reviewed guidance

- **WHEN** a client declares `review-contract` and the only item declared for it records `guidance_origin: inferred`
- **THEN** the guidance state is `none_reviewed` and no item is listed

#### Scenario: A restricted caller cannot tell a withheld item from a missing one

- **WHEN** a restricted caller declares `review-contract` and the only item for it is outside the caller's view
- **THEN** the guidance block equals the block that the caller gets from a vault without that item

### Requirement: A guidance item is served only when each declared dimension matches

Scope SHALL take four forms: global, activity, entity, and activity and entity together. A global stance line is served in the stance block, never as guidance. A guidance item SHALL be selected only when every scope dimension it declares matches the client's declaration:

- if the item declares activities, the client SHALL declare at least one of them;
- if the item declares an entity scope, the client SHALL declare at least one of its target entities.

An item with no activity, no entity scope and no stance mark SHALL NOT be guidance. An entity-scoped item SHALL never be served on a task that declares another entity or no entity. Selection SHALL run over the caller's view, and a withheld entity SHALL count as undeclared.

#### Scenario: An entity-scoped precedent stays with its entity

- **WHEN** a precedent is scoped to `reply-message` and to an invented person A, and the client declares `reply-message` and person B
- **THEN** the precedent is not selected

#### Scenario: An activity-only declaration does not unlock entity scope

- **WHEN** the same precedent exists and the client declares only `reply-message`
- **THEN** the precedent is not selected, and activity-only items for `reply-message` still are

### Requirement: Guidance and stance follow typed currency

Every guidance item and stance line SHALL pass through the packet builder's currency step, and the typed date rules of context-activation SHALL apply in their order. A superseded or archived item, and an item past its `until`, SHALL never be served as guidance or as a stance line. An item past its `check_by` SHALL stay served and SHALL carry `review_due` with that date. The stance block SHALL withhold a line past its `until` and SHALL report the count in `lines.expired`. Every comparison SHALL be between typed dates, and no date SHALL be read from prose for these rules.

#### Scenario: A superseded method unit is never guidance

- **WHEN** a page holds a method unit for `review-contract`, and a later unit on the same page, also declared for `review-contract`, supersedes it
- **THEN** a packet that declares `review-contract` serves the later unit and never the superseded one

#### Scenario: A line past its review date is served with a marker

- **WHEN** a stance line carries `check_by: 2026-10-01` and today is 2026-11-02
- **THEN** the line is served with `review_due: "2026-10-01"` and is not withheld

### Requirement: A client may declare the entities that a task involves

A maps-mode activation SHALL read declared entity refs from `conversation.entities` (conversation-aware-activation). Each ref SHALL resolve in the caller's view to an anchor of kind `entity`. The server SHALL drop a ref that is unknown, withheld or not of kind `entity`, count it in `guidance.unknown_entities`, and never echo it. A declaration SHALL never refuse the call, and the server SHALL never match names in it.

A resolved entity SHALL be listed first in the turn slice with the match value `declared`, which joins the slice's closed match set. The guidance block SHALL list the entity's `profile` ref before its entity-scoped and activity-and-entity items. Profile content SHALL stay a pull.

#### Scenario: A declared entity leads the slice

- **WHEN** a client declares the ref of an invented person A, and the turn's words reach other anchors
- **THEN** the slice lists person A first with match `declared`, and the guidance block lists person A's `profile` ref first

#### Scenario: A non-entity ref is counted

- **WHEN** a client declares the ref of a method page as an entity
- **THEN** the packet serves nothing for it, reports `guidance.unknown_entities = 1` and does not echo the ref

### Requirement: One setting rolls the task layer back

`EXOMEM_ACTIVATION_GUIDANCE` SHALL default to `on`. The value `off` SHALL remove the `stance` and `guidance` blocks from maps packets, ignore `conversation.activities` and `conversation.entities`, and give the map and the slice reserve the budget that they have without the two blocks. The layer runs no model and adds indexed lookups only. With an empty activity registry and no stance page it serves an empty stance block and the state `none_declared`.

#### Scenario: Rollback removes both blocks

- **WHEN** `EXOMEM_ACTIVATION_GUIDANCE=off` and a client declares `review-contract`
- **THEN** the packet carries neither a `stance` nor a `guidance` block, and its map and slice equal those of the same request with no stance page and no declaration
