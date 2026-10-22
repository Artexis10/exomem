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
In `maps` mode, activation SHALL return the map, its generation, the budget report, and the same governance and generation metadata as an `anchors` packet. It SHALL NOT resolve anchors, call a model other than retrieval scorers, or write to the vault. The map and any attached profile SHALL share the request's `max_chars` cap. A vault with no context-catalogue entries visible to the caller SHALL return an empty map with the reason `empty`. The `continuity` token SHALL carry the map generation, so that a caller can tell when its map is outdated, and SHALL NOT encode anchors. Unless the turn-slice evaluation arm is enabled, `conversation`, `focus` and `trigger` SHALL NOT change the map.

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
- A note SHALL be a page that is not an anchor, a thread or a collection.
- A thread SHALL be the newest episode revision for one episode identity: the `ep-` key of the episode model.
- A collection SHALL be a Records or Planning collection.
- Each entry SHALL carry:
  - a one-line description;
  - its topic memberships;
  - its source refs;
  - the generation it was built from.

The context catalogue SHALL be part of the derived activation index: recall SHALL NOT read it, and it SHALL be rebuildable from the vault alone. Agent-supplied descriptions SHALL persist in the page field that the catalogue contract names. Derived descriptions SHALL persist in a vault-held derived-description store.

The map SHALL be a projection of the catalogue that does not depend on the turn. It SHALL fit within the request's `max_chars` cap. Which entries the map lists SHALL be decided by registry-declared lifecycle classes and by entry activity as the catalogue contract defines it, not by lists in code. Each topic SHALL have a domain map: a projection of its entries, paginated at the read contract's page size. Entries with no topic SHALL be listed in an unfiled domain map. The map SHALL describe itself as an index of what exists, not as material relevant to the turn.

#### Scenario: The map does not depend on the turn
- **WHEN** two turns activate in `maps` mode with no catalogue change between them
- **THEN** both packets carry the same map

#### Scenario: Cold material stays reachable
- **WHEN** an entry does not fit the map
- **THEN** the domain map of each of its topics, or the unfiled domain map, lists it

### Requirement: Topic membership is declared
A topic SHALL be a domain that the vocabulary registry declares. An entry SHALL belong to a topic only through one of these:
- its page names the domain in the page field that the membership contract declares;
- its project key carries a registry attribute that maps it to the domain;
- an authored relation from the entry to a topic member is of a relation kind whose registry family is `membership`.

Membership SHALL run from the entry to the topic and SHALL NOT be transitive. A mention of an anchor SHALL NOT give the mentioning page that anchor's topics. Notes that are not anchors SHALL be catalogue entries in the topics they declare.

#### Scenario: A vault-added domain gets a domain map
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the context catalogue holds a topic and a domain map for it without a code change

#### Scenario: A mention does not spread membership
- **WHEN** a note mentions an anchor of another topic and has no relation of the membership family to it
- **THEN** the note does not join that topic

### Requirement: Profiles read by ref
A caller SHALL be able to read an anchor profile and a topic profile by ref. A profile SHALL be compiled through the role lanes over the anchor or over the topic's members, with the categories and roles that the registries declare. Each profile line SHALL be one of these:
- a semantic unit with its category, role, currency and ref;
- a current-state line from the current-state authority;
- a ref to a page or a unit.

A unit that does not fit SHALL become a ref and SHALL NOT be cut. A unit's as-of date SHALL be its own authored date. A page's modification date SHALL NOT stand in for a unit's date.

#### Scenario: A superseded unit is marked
- **WHEN** a profile unit has a current successor visible to the caller
- **THEN** the profile shows the successor, or marks the unit superseded and names its successor

#### Scenario: A unit without a date stays undated
- **WHEN** a unit has no authored date
- **THEN** its profile line carries no as-of date

### Requirement: Derived views resolve through one read contract
`read_memory` SHALL resolve map, domain-map and profile refs, on every surface that serves `read_memory`. Each response SHALL return at most one page of the page size that the read contract defines. Further pages SHALL be reachable by a continuation token. A missing ref SHALL report missing. When a view's inputs changed and the rebuilt view has not been published yet, the response SHALL serve the last published view marked stale.

#### Scenario: A stale profile is marked
- **WHEN** a member of a profile changed and no newer build of the profile is published
- **THEN** reading the profile returns the last published build marked stale

### Requirement: Descriptions and projections stay current on write
A write that creates or changes a context-catalogue entry SHALL accept a one-line description from the writing agent. A description that the agent did not supply SHALL be stored as derived and marked as derived. It SHALL NOT be stored under Sources or Evidence. A write SHALL invalidate every projection and profile whose inputs it changed, including old and new memberships and incoming relations. Publishing a projection SHALL mean replacing the current one. A build from a generation older than the current published one SHALL NOT be published.

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
- the cases and arms;
- the agent and its pull budget;
- the scoring rule;
- the contamination tolerance;
- the decision rule.

The arms SHALL include a pull-instructions arm with no map. The primary measure SHALL be whether the first answer uses the required current facts and constraints. The switch SHALL require all of these:
- the lower bound of the paired difference against `anchors` on the primary measure is above zero;
- the same lower bound against the pull-instructions arm is above zero;
- contamination on cases without relevant memory is non-inferior within the fixed tolerance;
- the activation-audit twins are non-inferior within the fixed tolerance.

#### Scenario: The default stays until the evaluation passes
- **WHEN** the frozen evaluation has not passed
- **THEN** activation serves the `anchors` packet by default
