# Spec Delta

## ADDED Requirements

### Requirement: Packet modes
Activation SHALL serve one of two packet modes, `anchors` and `maps`, selected by the process setting `EXOMEM_ACTIVATION_PACKET`. When the setting is absent, activation SHALL serve `anchors`.
- These requirements SHALL bind both modes:
  - "Read-only activation operation", including the kill switch, which disables both modes;
  - "Derived activation index";
  - "Governance and egress";
  - "Freshness and caching";
  - "Instrumentation and latency bound";
  - "Surface parity and pins";
  - "Activation resolves a turn over the caller's view".
- These requirements SHALL bind `anchors` mode only:
  - "Categorical anchor evidence and resolution";
  - "A band resolution on an ordinary word yields to a different carried page";
  - the packet shape of "Bounded role lanes and the working-memory packet".
- The role lanes' bounds and lifecycle rules SHALL also bind profiles that are compiled through those lanes.
- The `continuity` and `conversation` contracts of the conversation-aware activation and continuity capabilities, and the retrieve-inject-hook rendering contract, SHALL bind `anchors` mode. `maps` mode SHALL follow "The maps packet".

#### Scenario: The default mode is unchanged
- **WHEN** `EXOMEM_ACTIVATION_PACKET` is absent
- **THEN** activation returns the same packet as `anchors` mode for the same inputs

#### Scenario: The kill switch disables both modes
- **WHEN** the working-set kill switch is set and `EXOMEM_ACTIVATION_PACKET` is `maps`
- **THEN** activation returns the disabled abstention and no map

### Requirement: The maps packet
In `maps` mode, activation SHALL return the map, its generation, the budget report, and the same governance and generation metadata as an `anchors` packet. It SHALL NOT resolve anchors, call a model other than retrieval scorers, or write to the vault. The map and any attached profile SHALL share the request's `max_chars` cap. A vault with no context-catalogue entries visible to the caller SHALL return an empty map with the reason `empty`. The `continuity` token SHALL carry the map generation, so that a caller can tell when its map is outdated, and SHALL NOT encode anchors. Unless the turn-slice development probe is enabled, `conversation`, `focus` and `trigger` SHALL NOT change the map.

#### Scenario: An empty vault returns an empty map
- **WHEN** no context-catalogue entry is visible to the caller
- **THEN** the packet carries an empty map with the reason `empty`

#### Scenario: The map fits the budget
- **WHEN** the caller sets `max_chars`
- **THEN** the map and any attached profile together fit within it

#### Scenario: Conversation does not change the map
- **WHEN** two `maps`-mode calls differ only in `conversation`
- **THEN** both return the same map

### Requirement: A context catalogue with bounded projections
The compiler SHALL maintain a context catalogue, distinct from the anchor catalogue. Its entry kinds SHALL be the closed set that its schema declares: topic, anchor, note, thread and collection.
- A note SHALL be a page that is not an anchor, a thread or a collection, and is outside the trees that the product's layout declaration names as raw material (Sources and Evidence), schema, governance or archive. Activation and the catalogue SHALL read that one declaration. A page whose lifecycle class is `retired` stays a note.
- A thread SHALL be the newest episode revision for one episode identity: the `ep-` key of the episode model together with the audience it was recorded under.
- A collection SHALL be a Records or Planning collection.
- Each entry SHALL carry:
  - a one-line description, or none, and the origin of that description;
  - its topic memberships;
  - its source refs;
  - the generation it was built from.

The context catalogue SHALL be a derived sidecar of its own: disposable, machine-local, rebuildable from the vault alone, and never read by recall. An entry's description SHALL be the first of these that exists, and its origin SHALL be the name in front of it:
1. `authored`: the page's `summary` field, or for a collection the `summary` field of its `_collection.md` page;
2. `registry`: for a topic, the `description` field of its domain's registry entry;
3. `model`: a model-made description of the page's current content, when a derived-description store exists;
4. `lede`: the page's structural lede, cut to one line;
5. `none`: the entry is shown as undescribed.

The server SHALL NOT call a model to make a description. When a derived-description store exists, it SHALL be a reserved internal-state path, so that no surface that lists, searches or reads vault files serves it directly. A derived description SHALL be visible only to callers who can see the page it describes.

The map SHALL be a projection of the catalogue that does not depend on the turn. It SHALL fit within the request's `max_chars` cap. Which entries the map lists SHALL be decided by registry-declared lifecycle classes and by entry activity, not by lists in code:
- An entry's lifecycle class SHALL be the status registry's `class` attribute of the entry's status.
- An entry's activity SHALL be its page's `updated` field or, for a thread, the date of its newest revision that the caller can see.
- The map SHALL order entries totally: by these rules, then by activity, newest first, then by ref. `exomem://map` SHALL page through the whole ordered projection, and activation SHALL return its first part within the budget.
- Entries whose lifecycle class is `superseded`, `retired` or `abandoned` SHALL appear only in domain maps. Each topic SHALL have a domain map: a projection of its entries, paginated at the read contract's page size. Entries with no topic SHALL be listed in an unfiled domain map. The map SHALL describe itself as an index of what exists, not as material relevant to the turn.

#### Scenario: The map does not depend on the turn
- **WHEN** two turns activate in `maps` mode with no catalogue change between them
- **THEN** both packets carry the same map

#### Scenario: Cold material stays reachable
- **WHEN** an entry does not fit the map
- **THEN** the domain map of each of its topics, or the unfiled domain map, lists it

### Requirement: Topic membership is declared
A topic SHALL be a domain that the vocabulary registry declares. An entry SHALL belong to a topic only through one of these:
- its page's `topics` field, a value or a list, names the domain;
- one of its page's project keys (its `project` and `projects` fields) maps to the domain through that key's `domain` field in the project-key registry; a project anchor belongs to its own key's domains;
- an authored relation from the entry to a page is of a relation kind whose registry family is `membership`, and that page declares the domain through its own `topics` field or project key.

A declared value SHALL resolve when the domains resolver returns it as a built-in, registered or alias key; a deprecated key SHALL resolve to its replacement. A value that does not resolve SHALL give no membership, and the catalogue SHALL count it as an unresolved declaration. A page's `domain` field SHALL NOT declare membership, because it names a source's subject or an experiment's folder. Membership SHALL run from the entry to the topic and SHALL NOT be transitive: an entry SHALL NOT join a topic that its relation's target reaches only through its own relations. A mention of an anchor SHALL NOT give the mentioning page that anchor's topics. Notes that are not anchors SHALL be catalogue entries in the topics they declare.

#### Scenario: A vault-added domain gets a domain map
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the context catalogue holds a topic and a domain map for it without a code change

#### Scenario: A project key gives its pages a topic
- **WHEN** the project-key registry maps a key to a domain and a note names that key in its `project` field
- **THEN** the note is an entry of that domain's topic

#### Scenario: An unresolved topic is counted, not guessed
- **WHEN** a page edited outside Exomem names a value in its `topics` field that the domains registry does not hold
- **THEN** the page joins no topic through that value and the catalogue counts one unresolved declaration

#### Scenario: An experiment's folder domain is not a topic
- **WHEN** an experiment page carries `domain: ops` and no `topics` field
- **THEN** it joins no topic through its `domain` field

#### Scenario: A mention does not spread membership
- **WHEN** a note mentions an anchor of another topic and has no relation of the membership family to it
- **THEN** the note does not join that topic

### Requirement: Profiles read by ref
A caller SHALL be able to read an anchor profile and a topic profile by ref. A profile SHALL be compiled through the role lanes over the anchor or over the topic's members, with the categories and roles that the registries declare. Each profile line SHALL be one of these:
- a semantic unit with its category, role, currency and ref;
- a current-state line from the current-state authority, with its as-of date and the source of that date;
- a ref to a page or a unit.

A unit that does not fit SHALL become a ref and SHALL NOT be cut. A unit's as-of date SHALL be its own authored date. A page's modification date SHALL NOT stand in for a unit's date.

#### Scenario: A superseded unit is marked
- **WHEN** a profile unit has a current successor visible to the caller
- **THEN** the profile shows the successor, or marks the unit superseded and names its successor

#### Scenario: A unit without a date stays undated
- **WHEN** a unit has no authored date
- **THEN** its profile line carries no as-of date

### Requirement: Derived views resolve through one read contract
`read_memory` SHALL resolve these refs on every surface that serves `read_memory`:
- `exomem://map`, the map;
- `exomem://map/topic/<domain key>` and `exomem://map/unfiled`, the domain maps;
- `exomem://profile/topic/<domain key>` and `exomem://profile/anchor/<percent-encoded anchor ref>`, the profiles.

Each response SHALL carry the ref, the view's generation, a stale flag and at most 40 items. Further items SHALL be reachable by an opaque continuation token that is bound to the view, the caller's view and the generation. A malformed, unknown, expired or differently bound token SHALL return the existing `INVALID_CONTINUATION` refusal. A token bound to the same view and caller but an older generation SHALL return `CONTINUATION_STALE`, and the caller restarts from the first page. A ref that names no view visible to the caller SHALL return `NOT_FOUND`. A view SHALL be built from the indexes as they are published. When an input that the caller can see changed and the indexes have not yet published that change, the response SHALL serve the view built from the published indexes, marked stale. A cached build SHALL be served only while its inputs are unchanged, or marked stale; a caller whose builds are not cached SHALL get a build per request. A view is stale only when an input that the caller can see changed. When the catalogue cannot yet tell whether a pending change touches an input that a restricted caller can see, it SHALL answer that caller with the warming state, not with a stale mark. A view's generation SHALL be a digest of what the caller can see, so two generations compare only as equal or different.

#### Scenario: A stale profile is marked
- **WHEN** a member of a profile changed and the indexes the profile reads have not yet published that change
- **THEN** reading the profile returns the build from the published indexes, marked stale, for the owner and for a restricted caller alike

### Requirement: Descriptions and projections stay current on write
A write tool that creates or changes a context-catalogue entry SHALL accept a one-line description and a `topics` value from the writing agent, and store them in the page's `summary` and `topics` fields. A description that the agent did not supply SHALL be derived and marked as derived. A stored derived description SHALL NOT be stored under Sources or Evidence. A write that sets a page's `topics` field SHALL resolve each value through the domains resolver that source capture uses, and SHALL register a new value as capture does, with a warning on a near-miss. When registration refuses a value, as capture does on an invalid key or a path collision, the write SHALL refuse with the same error. A write SHALL invalidate every projection and profile whose inputs it changed, including old and new memberships and incoming relations. Publishing a projection SHALL mean replacing the current one. A build from a catalogue state older than the current one SHALL NOT be published.

#### Scenario: A capture reaches the profile it touches
- **WHEN** an agent records a new decision about an anchor and derived sync reports the profile refresh complete
- **THEN** the anchor's profile carries the decision with its own date

#### Scenario: A rename moves the entry
- **WHEN** a page is renamed and derived sync reports the refresh complete
- **THEN** the context catalogue, the map and every domain map that listed it carry the new ref and no line for the old one

### Requirement: Projections are computed over the caller's view
The context catalogue, map, domain maps and profiles served to a caller SHALL be computed over that caller's view. A hidden entry SHALL NOT affect anything served from them:
- any description;
- selection, ranking or ordering;
- caps or counts;
- staleness marks;
- generation tokens.

The egress guard SHALL cover every new packet block and field.

#### Scenario: A withheld entry changes nothing a non-owner sees
- **WHEN** a withheld entry is added to a topic that a non-owner can see
- **THEN** that caller's map, domain map and topic profile are unchanged, including their order and counts

### Requirement: The mode switch follows a frozen paired evaluation
The default SHALL change to `maps` only after an evaluation passes. These parts of the evaluation SHALL be fixed before any held-out generation:
- the cases, their groups and the arms, with each arm's server mode, tools and instructions;
- the agent and its pull budget;
- the scoring rule and the judge's verdict schema;
- the retry and failure policy;
- the tolerances and the decision rule.

The arms SHALL include a pull-instructions arm with no map. The cases SHALL include ordinary turns sampled without regard to any arm's outcome, beside the cases of known misses. The primary measure SHALL be whether the answer to each case's scored turn uses the required current facts and constraints and asserts no forbidden claim as current. The switch SHALL require all of these:
- on held-out cases that expect memory, miss cases and ordinary turns pooled, `maps` is superior to `anchors` on the primary measure, with each stratum also reported;
- on held-out ordinary turns, `maps` is non-inferior to `anchors` within the fixed tolerance;
- `maps` is superior to the pull-instructions arm, or non-inferior to it within the fixed tolerance and superior on memory calls per case;
- contamination on cases without relevant memory is non-inferior within the fixed tolerance;
- `maps` loses no activation-audit positive case that `anchors` passes.

#### Scenario: The default stays until the evaluation passes
- **WHEN** the frozen evaluation has not passed
- **THEN** activation serves the `anchors` packet by default
