# Spec Delta

## ADDED Requirements

### Requirement: An always-on map of the vault
The compiler SHALL maintain a map of the vault's live areas, threads, key anchors and trackers. Each map line SHALL name the item, say what it is, give its current state with an as-of date, and carry a ref. The map SHALL NOT depend on the current turn. The map SHALL stay within its configured character budget and SHALL link to a domain map for each topic it lists.

#### Scenario: The map is the same for every turn of a session
- **WHEN** two turns of one session activate without a write between them
- **THEN** both packets carry the same map

#### Scenario: A large vault tiers its map
- **WHEN** the vault holds more live items than the map budget fits
- **THEN** the map lists topics and the most active items, and each topic's domain map holds the rest

### Requirement: Topic and anchor profiles compiled from semantic units
The compiler SHALL compile a topic profile for each topic and an anchor profile for each indexed anchor. A topic SHALL be a domain that the vocabulary registry declares. Each profile line SHALL be a semantic unit or a reference to one, with its category, its role from the roles registry, its currency and as-of date, and its ref. A unit that does not fit the profile budget SHALL become a pointer and SHALL NOT be cut. Profiles SHALL be compiled from units and index rows, not by parsing page text.

#### Scenario: A vault-added domain gets a topic profile
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the compiler builds a topic profile and a domain map for it without a code change

#### Scenario: A superseded unit is marked
- **WHEN** a unit in a profile has a current successor
- **THEN** the profile shows the successor, or marks the unit superseded and names its successor

### Requirement: A turn slice selected by meaning
The compiler SHALL select a turn slice from the turn, the recent conversation and any agent `focus` or `queries`. It SHALL match them by meaning against map lines and profile summaries. Resolver evidence such as an exact name MAY raise a candidate and SHALL NOT exclude one. The slice SHALL carry at most the configured number of profiles. A candidate below the confidence threshold SHALL appear only as a named ref. The slice MAY be empty, and an empty slice SHALL NOT remove the map.

#### Scenario: A misspelled turn still reaches its topic
- **WHEN** a turn names a topic's anchor with a typo or a split compound
- **THEN** the slice carries that topic's profile or names its ref

#### Scenario: A turn with no relevant memory gets no profile
- **WHEN** no topic or anchor matches the turn above the threshold
- **THEN** the packet carries the map and no profile

### Requirement: Agent queries reach hybrid recall
`activate_context` SHALL accept up to three agent-written `queries`. Each query SHALL run through hybrid recall over the caller's view. Their hits SHALL join the slice as pointers or units labelled `origin: agent_query`. An agent query SHALL NOT resolve an ambiguous human reference to an identity.

#### Scenario: An agent query finds what the turn's words missed
- **WHEN** the turn alone matches nothing and the agent passes a query that recall answers
- **THEN** the slice carries the recalled page as a pointer or units labelled `origin: agent_query`

### Requirement: Maps and profiles stay current on write
Each write that creates or changes a page SHALL update the page's one-line summary and current-state fields with an as-of date. A summary that the writing agent did not supply SHALL be stored and marked as derived. The write SHALL queue a refresh of the profiles and map lines whose anchors or topics it touched. A map or profile line whose source changed and has not been refreshed SHALL be marked stale.

#### Scenario: A capture updates the profile it touches
- **WHEN** an agent records a new decision about an anchor
- **THEN** after the refresh, that anchor's profile carries the decision and its as-of date

#### Scenario: An unrefreshed line is marked
- **WHEN** a page changed and its profile refresh has not run
- **THEN** the profile line from that page is served marked stale

### Requirement: Maps and profiles respect the reader's view
A map, domain map or profile served to a reader SHALL leave out every anchor, unit and count that the reader may not see.

#### Scenario: A withheld anchor is absent for a non-owner
- **WHEN** a non-owner activates and the vault holds a withheld anchor in a listed topic
- **THEN** the map, domain map and topic profile carry no line, unit or count from it

### Requirement: The packet switch is decided by a held-out evaluation
The map packet SHALL be selectable by a setting and SHALL NOT become the default until one blind run on a held-out split of field-report cases shows a gain over the current packet whose interval excludes the accepted loss, with no added harm on cases without relevant memory and on the activation-audit twins.

#### Scenario: The default stays until the evaluation passes
- **WHEN** the held-out run has not passed
- **THEN** activation serves the current packet by default
