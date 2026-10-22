# Spec Delta

## ADDED Requirements

### Requirement: Packet modes
Activation SHALL serve one of two packet modes, selected by a setting: `anchors` and `maps`. The default SHALL be `anchors`. The requirements that predate this change SHALL apply to `anchors` mode. The requirements added by this change SHALL apply to `maps` mode.

#### Scenario: The default mode is unchanged
- **WHEN** the setting is absent
- **THEN** activation serves the `anchors` packet exactly as before

### Requirement: A catalogue with bounded projections
The compiler SHALL maintain a catalogue of topics, anchors, threads and trackers. Each entry SHALL carry a one-line description, its topic memberships, its source refs and the generation it was built from. In `maps` mode, activation SHALL return a map that is a bounded projection of the catalogue and does not depend on the turn. Each topic SHALL have a domain map, a projection of its entries, that a reader can open by ref.

#### Scenario: The map does not depend on the turn
- **WHEN** two turns activate in `maps` mode with no catalogue change between them
- **THEN** both packets carry the same map

#### Scenario: Cold material stays reachable
- **WHEN** an entry does not fit the map budget
- **THEN** the domain map of each of its topics lists it

### Requirement: Topic membership is declared
A topic SHALL be a domain that the vocabulary registry declares. An entry SHALL belong to a topic only when its page declares the domain, when its project key maps to the domain, or when an authored typed relation of a registry-declared membership kind links it to a member. A mention of an anchor SHALL NOT give the mentioning page that anchor's topics. Notes that are not anchors SHALL be catalogue entries in the topics they declare.

#### Scenario: A vault-added domain gets a domain map
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the catalogue holds a topic and a domain map for it without a code change

#### Scenario: A mention does not spread membership
- **WHEN** a note mentions an anchor of another topic and declares no relation of a membership kind
- **THEN** the note does not join that topic

### Requirement: Profiles read by ref
A reader SHALL be able to read an anchor profile and a topic profile by ref. A profile SHALL be compiled from the role lanes over the anchor or the topic's members. Each line SHALL be a semantic unit with its category, role, currency and ref, a current-state line from the current-state authority, or a ref to a page or unit. A unit that does not fit SHALL become a ref and SHALL NOT be cut. A unit's as-of date SHALL be its own authored date; a page's modification date SHALL NOT stand in for it.

#### Scenario: A superseded unit is marked
- **WHEN** a profile unit has a current successor visible to the reader
- **THEN** the profile shows the successor, or marks the unit superseded and names its successor

#### Scenario: A state without a date stays undated
- **WHEN** an entry's state has no known observation date
- **THEN** its map line and profile carry the state without an as-of date

### Requirement: Derived views resolve through one read contract
`read_memory` SHALL resolve map, domain-map and profile refs. Output SHALL be bounded; overflow SHALL be paginated refs. A missing ref SHALL report missing, and a view whose inputs changed and is not rebuilt SHALL be served marked stale.

#### Scenario: A stale profile is marked
- **WHEN** a member of a profile changed and the profile is not rebuilt
- **THEN** reading the profile returns it marked stale

### Requirement: Descriptions and projections stay current on write
A write that creates or changes a catalogue entry SHALL accept a one-line description from the writing agent. A description that the agent did not supply SHALL be stored as derived, marked as derived, and SHALL NOT be stored under Sources or Evidence. A write SHALL invalidate every projection and profile whose inputs it changed, including old and new memberships and incoming relations. A projection built from an obsolete generation SHALL NOT be published.

#### Scenario: A capture reaches the profile it touches
- **WHEN** an agent records a new decision about an anchor
- **THEN** after the refresh, the anchor's profile carries the decision with its own date

#### Scenario: A rename moves the entry
- **WHEN** a page is renamed
- **THEN** the catalogue, the map and every domain map that listed it carry the new ref and no line for the old one

### Requirement: The reader's view applies before compilation
The catalogue, map, domain maps and profiles served to a reader SHALL be computed over that reader's view. A hidden entry SHALL NOT affect any served description, selection, ranking, ordering, cap, count, staleness mark or generation token.

#### Scenario: A withheld entry changes nothing a non-owner sees
- **WHEN** a withheld entry is added to a topic that a non-owner can see
- **THEN** that reader's map, domain map and topic profile are unchanged, including their order and counts

### Requirement: The mode switch follows a frozen paired evaluation
The default SHALL change to `maps` only after an evaluation whose cases, arms, agent, pull budget, scoring rule and decision rule were fixed before any held-out generation. The primary measure SHALL be whether the first answer uses the required current facts and constraints. The switch SHALL require the lower bound of the paired difference on the primary measure to be above zero, and non-inferiority within the fixed tolerance on answer contamination for cases without relevant memory and on the activation-audit twins.

#### Scenario: The default stays until the evaluation passes
- **WHEN** the frozen evaluation has not passed
- **THEN** activation serves the `anchors` packet by default
