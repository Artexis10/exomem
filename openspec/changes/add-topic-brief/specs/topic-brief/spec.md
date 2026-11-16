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
The brief SHALL gather members deterministically from the seed alone over the caller's view, so that a read of the brief ref serves the same members as the read that minted it:
- compiled notes reached from the seed within two hops through the relation families of the shipped traversal profile `brief`, walking only through compiled notes, ordered by distance and then most recent first;
- Source pages that a member cites through its `sources` field, a citation or provenance relation, or an episode's `ingested_into` field.
The brief SHALL cap compiled-note members at 40 and Source members at 40. A candidate cut by a cap SHALL be counted in `missing` with reason `cap`. Search hits SHALL never add members; on a topic-text read they appear only in `related`. The member walk SHALL take its relation families from the traversal-profile registry, never from a list in code.

#### Scenario: A hub and its sister notes
- **WHEN** a caller reads the brief of a hub note that links to four sister notes, each citing one Source
- **THEN** the brief has five compiled-note members and four Source members

#### Scenario: An unlinked related note
- **WHEN** a compiled-note search hit for the topic text is not within two hops of the seed
- **THEN** it appears in `related` and not among the members

### Requirement: A brief has conclusions, sources and open questions
The brief SHALL serve these sections. Every item SHALL carry its `ref`, `path` and `updated`:
- `conclusions`: per compiled-note member, its title and summary or question, then its whole units whose kind carries the `assertion` trait, and its categorised observations. Members are ordered seed first, then by link distance, then most recent first. A superseded or closed unit SHALL be left out and counted per member.
- `sources`: Source members with title, source kind and capture date, and external links found in member bodies by the Markdown link parser, deduplicated by target.
- `open_questions`: units whose kind carries the `question` trait and that no `answers`, `resolves` or `bears_on` relation closes.
The brief SHALL select units by kind traits from the kinds registry, never by matching words in headings or text.

#### Scenario: An open thread is served as an open question
- **WHEN** a member note has a `## Open threads` section with two items and one is answered by an `answers` relation
- **THEN** `open_questions` carries the unanswered item only

### Requirement: A brief reports what changed since the principal's last read
The server SHALL keep a read watermark per principal and brief seed, holding each member's content hash and unit hashes and the read time. The watermark SHALL advance only when the last page of a brief read is served.
- On a principal's first read of a brief, `changed_since` SHALL be `{state: "first_read"}`.
- On later reads it SHALL list new, changed and removed members, with each changed member's `updated`, and the refs of units added since the watermark.
- One principal's reads SHALL NOT move another principal's watermark.
- The sidecar SHALL be bounded at 10,000 rows with least-recently-read eviction and 400-day retention. An evicted watermark SHALL read as `first_read`.

#### Scenario: A note changes between two reads
- **WHEN** a principal reads a brief, a member note gains an observation, and the principal reads the brief again
- **THEN** `changed_since` lists that member as changed and carries the new unit's ref

#### Scenario: Two principals
- **WHEN** principal A reads a brief and principal B then reads it for the first time
- **THEN** B's `changed_since` is `first_read`

### Requirement: A compiled brief is cached until its inputs change
The server SHALL cache a compiled brief under a key of the store root, the caller binding, a digest of each member's ref, content hash, lifecycle and path, a digest of the closing relations that target member units, the registry digest and the limits. The member walk SHALL take content hashes from the index, so a read whose key is cached SHALL read no page. An edit to a member page, a new closing relation on a member unit or a registry change SHALL produce a new key.

#### Scenario: A warm read
- **WHEN** a caller reads the same brief twice with no change to the store
- **THEN** the second read performs no page reads and returns the same generation

### Requirement: A brief filters by the caller's view first
Every candidate SHALL be filtered by what the caller may see before it is ordered, counted, capped, hashed, cached under a key or compared with a watermark. A page the caller cannot see SHALL move no byte of the response, including `missing` counts, `related` and `changed_since`. The compile SHALL record its served items, and the terminal egress guard SHALL re-decide them for the `brief` view kind.

#### Scenario: A withheld member
- **WHEN** one of five linked notes is withheld from the caller
- **THEN** the response is byte-identical to the brief of a store without that note

### Requirement: Brief reads are paged and measured on every surface
A brief SHALL page at the derived-view page size, with the existing signed continuation bound to view, caller and generation, and with `CONTINUATION_STALE` after a generation change. The same trusted principal SHALL receive the same items, order and generation on MCP, REST, Hosted, CLI and in-process dispatch. The server SHALL record the latency and the error rate of brief reads under their own route label.

#### Scenario: Paging a large brief
- **WHEN** a brief has more items than one page holds
- **THEN** the continuation opens the next page on every surface, and the watermark advances after the last page
