## ADDED Requirements

### Requirement: Topic-Driven Thinking-Evolution Timelines

The system SHALL provide an `evolution` operation that accepts a topic query, finds the
matching notes, resolves each into its supersession chain, and returns one ordered timeline
per distinct chain (oldest version → newest). It SHALL de-duplicate hits that resolve to the
same chain into a single timeline and SHALL exclude any chain with fewer than two versions (a
note never superseded has no evolution to show).

#### Scenario: A topic with a superseded conclusion returns its timeline

- **WHEN** `evolution` is called with a query matching a note that has been superseded one or
  more times
- **THEN** it returns a single timeline for that chain whose `versions` run oldest to newest
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Hits on the same chain collapse, and unchanged topics are excluded

- **WHEN** the query matches two members of the same supersession chain, and also a note that
  was never superseded
- **THEN** the two members yield exactly one timeline (de-duplicated), and the
  never-superseded note yields no timeline

### Requirement: Chain Resolution And Ordering From Supersession Pointers

The system SHALL resolve a chain by walking the frontmatter supersession pointers — backward
via `supersedes` and forward via `superseded_by` — and SHALL order the resulting versions by
the pointer spine (the origin has no in-chain `supersedes`; the head has no `superseded_by`),
not by date alone. A `supersedes` reader SHALL be available on the parsed page alongside the
existing `superseded_by`.

#### Scenario: Versions are ordered by the pointer spine

- **WHEN** a chain A → B → C exists (A `superseded_by` B, B `superseded_by` C; C `supersedes`
  B, B `supersedes` A)
- **THEN** the timeline's `versions` are ordered exactly [A, B, C] regardless of their
  `updated` dates
- **AND** C (no `superseded_by`) is the chain head and A (no `supersedes`) is the origin

### Requirement: Per-Version Structural Claims And Recorded Transition Reasons

The system SHALL attach to each timeline version its structurally-extracted claims (lede,
recognized headline-section lines, and `##` outline) and, for every non-head version, the
**recorded** transition reason (from the superseded page's banner and/or the `why:` logged
at the supersession edit). It MUST NOT generate, summarize, or judge how the view changed —
the claims and reasons are the notes' own text, surfaced verbatim, and the active head
carries no transition.

#### Scenario: Each version carries its own claims; transitions carry recorded reasons

- **WHEN** a timeline is built for a multi-version chain
- **THEN** each version's `claims` are extracted structurally from that version's own body,
  and each non-head version's `transition.reason` is the recorded text, with the head
  version's `transition` null
- **AND** no generative or reasoning model is invoked

### Requirement: Bounded Output With Explicit Truncation

The system SHALL bound the result by a configurable cap on the number of timelines (by find
relevance) and a configurable cap on versions per timeline, and MUST NOT silently truncate:
whenever a cap drops content the result SHALL carry an explicit `truncation` entry naming
what was dropped.

#### Scenario: More chains than the limit are reported, not silently dropped

- **WHEN** more matching chains exist than the timelines cap
- **THEN** exactly the cap is returned and `truncation` carries an entry stating how many
  chains were not shown

### Requirement: Measurement-Only Operation On All Surfaces

The `evolution` operation SHALL be measurement-only — reading note content, frontmatter,
supersession pointers, and recorded edit reasons, applying only deterministic ordering — and
MUST NOT mutate the vault or change `find` ordering. It SHALL be defined by a single
command-registry entry and reachable as an MCP tool, a REST route (`/api/evolution`), and a
CLI subcommand (`kb evolution`) with no per-surface code.

#### Scenario: One registry entry exposes evolution everywhere, read-only

- **WHEN** the registry is built
- **THEN** an `evolution` MCP tool, an `/api/evolution` REST route, and a `kb evolution` CLI
  subcommand all exist from the one entry
- **AND** running it over a vault creates, modifies, moves, or deletes no file and leaves
  `find` ordering unchanged
