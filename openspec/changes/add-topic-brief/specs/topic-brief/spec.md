# Spec Delta

## ADDED Requirements

### Requirement: A brief read resolves a subject to a seed page
`read_memory` SHALL accept an optional boolean `brief`, exposed on MCP as `read_page(part="brief")`, on every generated surface. With `brief` set, `path` SHALL name a subject: a page ref, path or title, or free topic text.
- When `path` resolves to a page the caller may read, that page SHALL be the seed.
- Otherwise the read SHALL run the existing hybrid search once over the caller's view. The top compiled-note hit SHALL be the seed. Up to ten other compiled-note hits that are not members SHALL be returned as `related`, each with title and ref, so that the caller can read the brief of any of them.
- When no compiled note in the caller's view matches, the read SHALL return the existing not-found refusal, with no hint of withheld pages.
- A brief read SHALL refuse `unit_ref`, `frontmatter_only`, `links`, `include_raw`, `include_history` and `units` with `INVALID_VIEW_READ_OPTIONS`.
- The response SHALL carry `ref: exomem://brief/<seed exomem id>`. A later read of that ref SHALL serve the same brief.

#### Scenario: Topic text resolves to a seed
- **WHEN** a caller reads `brief` with topic text that matches a hub note and two other notes
- **THEN** the hub note is the seed, the response ref is `exomem://brief/<hub id>`, and the other notes appear as members or in `related`

#### Scenario: Nothing visible matches
- **WHEN** the only notes that match the topic text are withheld from the caller
- **THEN** the read returns the not-found refusal that an unknown page returns

### Requirement: Brief members are bounded and gaps stay visible
The brief SHALL gather members deterministically from the seed over the caller's view: the same links and the same stored recall vectors give the same members. A read of the brief ref therefore serves the members of the read that minted it unless a link or a vector that ranks the second hop changed in between, and `changed_since` then reports the difference:
- compiled notes reached from the seed within two hops through the relation families of the shipped traversal profile `brief`, walking only through compiled notes: every note one hop from the seed, and at most five notes at the second hop, those nearest the seed first. A note's nearness is the best similarity of its stored recall vectors to the mean of the seed's, both read from one resident recall matrix, and a page is scored only by its own vectors. A candidate with no current vectors in that matrix follows the scored ones, most recent first. When no resident matrix serves the read or it holds no current vectors for the seed, the candidates are ordered most recent first and the response carries `ranked_by: "recency"`; otherwise it carries `ranked_by: "similarity"`. Which of the two applies SHALL be decided from the seed alone, so a page the caller may not see never moves it;
- Source pages that a member cites through its `sources` field, a citation or provenance relation, or an episode's `ingested_into` field.
The brief SHALL cap compiled-note members at 40 and Source members at 40. A candidate cut by a cap SHALL be counted in `missing` with reason `cap`. A member that has no stable ref to serve its items under SHALL be counted in `missing` with reason `unreferenced`, never left out silently. Search hits SHALL never add members; on a topic-text read they appear only in `related`. The member walk SHALL take its relation families from the traversal-profile registry, never from a list in code.

The edge rows that one walk step reads SHALL be bounded for each role that a far page plays, compiled note or Source, and shared evenly among the pages that the step expands. A page whose rows of one role exceed its share, such as a hub that links thousands of pages, SHALL cost the step that share and not its degree. The `missing` entry of the role that the cut affects SHALL carry `exact: false` and a `count` that is a lower bound, or null when the cut left no candidate to count, never 0. A row counts toward a share only when the page at its other end plays that role and is visible to the caller, so that cited Sources cannot spend the share of the linked notes, and a withheld page SHALL NOT move the bound, the order or the count.

#### Scenario: A page that links more pages than a step reads
- **WHEN** a caller reads the brief of a compiled note that links more pages than one walk step reads
- **THEN** the brief serves the members that the step read, and `missing` reports the cut with `exact: false`

#### Scenario: A hub that cites more Sources than a step reads
- **WHEN** a caller reads the brief of a hub note that links eight sister notes and cites more Sources than one walk step reads
- **THEN** all eight sister notes are members, and only the `missing` entry for Sources reports a cut, with `exact: false` and a null `count`

#### Scenario: A withheld spoke at the bound
- **WHEN** a hub links five visible and five withheld pages and the step reads six rows
- **THEN** the response is byte-identical to the brief of a store that has no withheld page

#### Scenario: A hub and its sister notes
- **WHEN** a caller reads the brief of a hub note that links to four sister notes, each citing one Source
- **THEN** the brief has five compiled-note members and four Source members

#### Scenario: A broad note one hop from the seed
- **WHEN** the seed links one note that links eight other notes
- **THEN** the five of those eight nearest the seed are members, and `missing` counts the other three with reason `cap`

#### Scenario: An unlinked related note
- **WHEN** a compiled-note search hit for the topic text is not within two hops of the seed
- **THEN** it appears in `related` and not among the members

### Requirement: A brief has conclusions, sources and open questions
The brief SHALL serve these sections. Every item SHALL carry its `ref`, `path` and `updated`:
- `conclusions`: per compiled-note member, its title and summary or question, then its whole units whose kind carries the `assertion` trait, and its categorised observations. Members are ordered seed first, then by link distance, then nearest the seed first, or most recent first when `ranked_by` is `recency`. A superseded or closed unit SHALL be left out and counted per member.
- `sources`: Source members with title, source kind and capture date, and external links found in member bodies by the Markdown link parser, deduplicated by target.
- `open_questions`: units whose kind carries the `question` trait and that nothing closes. Only a kind that carries the `closable` trait can be closed. A unit is closed by its own `verdict`, by a relation that the unit authors and that bears on it, or by an inbound relation of a family that settles or bears on it from a page the caller may see. The registry names those families and the closing relation reaches the unit by its fragment.
The brief SHALL select units by kind traits from the kinds registry, never by matching words in headings or text.

#### Scenario: An open thread is served as an open question
- **WHEN** a member note has a `## Open threads` section with two items and one is answered by an `answers` relation
- **THEN** `open_questions` carries the unanswered item only

### Requirement: A brief reports what changed since the view's last read
The server SHALL keep a read watermark per view and brief seed. A view is the audience, the declared purpose and the connector ceiling, which is the connector's denied scopes and not its client or its login. Two readers with the same view share a watermark, and a reader whose view differs has its own. A watermark holds a short hash of each member's content, a short digest of each served unit ref, a flag that tells whether the unit digests were cut, and the read time. A watermark holds at most 500 unit digests.
- On the first read of a brief by a view, `changed_since` SHALL be `{state: "first_read"}`.
- On later reads it SHALL list new, changed and removed members, with each changed member's `updated`, and the refs of units added since the watermark. When the watermark could not be read, `changed_since` SHALL be `{state: "unavailable"}`, never `first_read`.
- Everything compared SHALL be what the caller sees now. A stored member that the caller may no longer see SHALL NOT be listed in `removed`; a page that left the store SHALL be.
- When the unit digests were cut, `new_units` SHALL be empty and `changed_since` SHALL carry `new_units_exact: false`, so that a unit missing from the watermark is never listed as new.
- The watermark SHALL advance after the terminal egress guard has served the last page of a brief read, and not before. A caller whose read does not reach the guard SHALL NOT move the watermark.
- One view's reads SHALL NOT move another view's watermark.
- The sidecar SHALL be bounded at 10,000 rows with least-recently-read eviction and 400-day retention. An evicted watermark SHALL read as `first_read`.

#### Scenario: A note changes between two reads
- **WHEN** a principal reads a brief, a member note gains an observation, and the principal reads the brief again
- **THEN** `changed_since` lists that member as changed and carries the new unit's ref

#### Scenario: Two views
- **WHEN** a view reads a brief and a view with a different connector ceiling then reads it for the first time
- **THEN** the second view's `changed_since` is `first_read`

#### Scenario: A member that a view may no longer see
- **WHEN** a member page is withheld from the caller between two reads
- **THEN** the second read does not list that member as removed or changed

#### Scenario: A read that the guard refuses
- **WHEN** the terminal egress guard refuses a brief read
- **THEN** the watermark keeps its earlier state

### Requirement: A compiled brief is cached until its inputs change
The server SHALL cache a compiled brief under a key of the store root, the caller binding, a digest of each member's ref, content hash, lifecycle and path, a digest of the closing relations that target member units, the registry digest and the limits. The registry digest SHALL cover every registry instance that can change a unit's meaning, including a private instance's definitions, because such a change needs no edit to the page. The member walk SHALL take content hashes from the index, so a read whose key is cached SHALL read no page. An edit to a member page, a new closing relation on a member unit or a registry change SHALL produce a new key. A cached brief SHALL also stop being served on the first day after an authored `until` or `check_by` date of one of its units, because that day changes the unit's currency with no edit to any page. A date that has no following day SHALL never end a cached brief.

#### Scenario: A warm read
- **WHEN** a caller reads the same brief twice with no change to the store
- **THEN** the second read performs no page reads and returns the same generation

### Requirement: An owner's member walk is kept under the graph snapshot's token
The server SHALL keep the member walk and closing edges of an owner's read under a key of the store root, the caller binding, the seed path, the graph snapshot's token read in the walk's own read transaction, the recall policy identity and the registry digest. The token SHALL change on every write of graph rows, including an incremental refresh of one page. A restricted caller's walk SHALL NOT be kept.

#### Scenario: A warm owner read reads no edge
- **WHEN** an owner reads the same brief twice with no change to the graph
- **THEN** the second read performs no graph edge read

#### Scenario: An edit ends the kept walk
- **WHEN** a member page is edited through a governed write and the graph catches up incrementally
- **THEN** the graph token differs and the next read serves the edited text

### Requirement: A brief filters by the caller's view first
Every candidate SHALL be filtered by what the caller may see before it is ordered, counted, capped, hashed, cached under a key or compared with a watermark. A page the caller cannot see SHALL move no byte of the response, including `missing` counts, `related` and `changed_since`. The compile SHALL record its served items, and the terminal egress guard SHALL re-decide them for the `brief` view kind.

#### Scenario: A withheld member
- **WHEN** one of five linked notes is withheld from the caller
- **THEN** the response is byte-identical to the brief of a store without that note

### Requirement: Brief reads are paged and measured on every surface
A brief SHALL page at the derived-view page size, with the existing signed continuation bound to view, caller and generation, and with `CONTINUATION_STALE` after a generation change. The same trusted principal SHALL receive the same items, order and generation on MCP, REST, Hosted, CLI and in-process dispatch. The server SHALL record the latency and the error rate of brief reads in metric series of their own, apart from the series that count every tool call, so that a sum over tools counts a brief read once.

#### Scenario: Paging a large brief
- **WHEN** a brief has more items than one page holds
- **THEN** the continuation opens the next page on every surface, and the watermark advances after the last page
