# context-packs Specification

## Purpose
TBD - created by archiving change add-context-packs. Update Purpose after archive.
## Requirements
### Requirement: Optional Context Pack Assembly From `find`

The system SHALL provide an optional `pack` parameter on `find` (default `false`) that,
when `false`, returns the existing hit list **unchanged** and, when `true`, returns an
object `{"hits": [...], "pack": {...}}` where `pack` is an assembled context pack over
the top hits carrying `packed_paths`, `claims`, `neighborhood`, `contradictions`,
`embeddings_available`, and `truncation`. The assembly SHALL NOT alter the hits, their
order, or any existing `find` behaviour, and the core `find` ranker signature and return
type SHALL be unchanged (the parameter and the object return are confined to the command
leaf).

#### Scenario: Pack off is byte-identical to today

- **WHEN** `find` is called with `pack` omitted or `false`
- **THEN** it returns the same hit list it returns today, with no `pack` object and no
  change to ordering or fields

#### Scenario: Pack on returns hits plus an assembled pack

- **WHEN** `find` is called with `pack=true` over a vault with matching notes
- **THEN** it returns `{"hits", "pack"}` where `hits` is the usual list and `pack`
  carries `packed_paths` (the top notes covered), `claims`, `neighborhood`,
  `contradictions`, `embeddings_available`, and `truncation`
- **AND** no file under the vault is created, modified, moved, or deleted

### Requirement: Structural Key-Claim Extraction Without Generation

The system SHALL extract each packed note's `claims` **structurally** from the note's own
text — a `lede` (its first content paragraph), `sections` (the first line or leading
bullets under recognized headline sections), and an `outline` (its `##` headings in
order) — and MUST NOT invoke any generative or summarizing model to produce them. Heading
and lede detection SHALL ignore content inside fenced code blocks.

#### Scenario: Claims are the note's own structure

- **WHEN** a packed note has a lede paragraph, a `## Summary` section, and several `##`
  headings
- **THEN** its `claims` carry the lede text, a `Summary:` entry drawn from that section,
  and the `##` headings as `outline`
- **AND** no generative/reasoning model is invoked to produce them

### Requirement: One-Hop Wikilink Neighbourhood Ranked By Co-Citation

The system SHALL assemble the `neighborhood` as the 1-hop inbound and outbound wikilink neighbours
of the packed notes — reusing the existing outbound-link resolution and a process-cached
inbound-link index built from a single vault content scan per index revision — excluding any note
already in `packed_paths`, recording each neighbour's link `direction` (`in`/`out`/`both`) and the
packed notes it is linked with (`referenced_by`), and ranking neighbours by co-citation (the count
of distinct packed notes that link them) before capping. Each neighbour SHALL carry at most a
one-sentence lede. Assembling a neighborhood for more than one packed page MUST NOT perform more
than one full vault content scan per index revision, and the resulting `neighborhood` set, ordering,
and per-neighbour fields MUST be identical to what a brute-force per-page inbound-link scan would
produce.

#### Scenario: A co-cited neighbour outranks a singly-cited one

- **WHEN** neighbour `X` is linked by two packed notes and neighbour `Y` by one
- **THEN** `X` is ranked above `Y` in `neighborhood`, each with its `direction` and `referenced_by`
- **AND** no note already present in `packed_paths` appears in `neighborhood`

#### Scenario: Packing several notes reuses one vault scan

- **WHEN** `find(pack=true)` packs more than one note whose inbound links must be resolved
- **THEN** the vault content is scanned once to answer every packed note's inbound-link lookup, not
  once per packed note
- **AND** the resulting `neighborhood` is identical to resolving each packed note's inbound links
  with an independent brute-force scan

#### Scenario: A rename after a cached scan is not missed

- **WHEN** a markdown file is renamed after the inbound-link index has already been cached
- **THEN** the next `find(pack=true)` neighborhood assembly reflects the rename rather than the
  stale cached index

### Requirement: Contradictions And Supersession Among The Packed Set

The system SHALL surface, within the pack's `contradictions`, two measured relations
among the notes in the set: recorded `superseded` edges read from `status` /
`superseded_by` frontmatter, and proximity `tension` pairs whose pairwise cosine sits in
the existing contradiction band `[CONTRADICTION_FLOOR, DUP_THRESHOLD)`. Tension SHALL be
labelled as proximity, not polarity, deferring the judgment to the reader, and SHALL be
computed only among the packed notes. When the embedding sidecar is unavailable
(embeddings disabled or unimportable), `tension` SHALL be empty and `embeddings_available`
SHALL be `false`, while `superseded` edges (which need no embeddings) SHALL still be
surfaced.

#### Scenario: A recorded supersession edge is surfaced

- **WHEN** a packed note's `superseded_by` points at another note in the set
- **THEN** `contradictions.superseded` carries that `{from, to}` edge, read from
  frontmatter without embeddings

#### Scenario: Embeddings-off still yields a useful pack

- **WHEN** `find(pack=true)` runs with embeddings disabled
- **THEN** `embeddings_available` is `false`, `tension` is empty, and `claims`,
  `neighborhood`, and `contradictions.superseded` are still populated

### Requirement: Bounded Assembly With Explicit Truncation

The system SHALL bound the pack by configurable, env-overridable caps on the number of
packed hits, neighbours, and tension pairs, and MUST NOT silently truncate: whenever a
cap drops content the pack's `truncation` list SHALL carry an explicit entry naming what
was capped and by how much.

#### Scenario: A capped neighbourhood is reported

- **WHEN** more 1-hop neighbours exist than the neighbour cap
- **THEN** exactly the cap is surfaced and `truncation` carries an entry stating how many
  neighbours were not shown

### Requirement: Measurement-Only Assembly On All Surfaces

The pack assembly SHALL be measurement-only — reading note content, frontmatter,
wikilinks, and precomputed sidecar embeddings, applying only deterministic extraction and
rank/band arithmetic — and MUST NOT invoke a generative or reasoning model, MUST NOT
mutate the vault, and MUST NOT change `find` ordering. The `pack` parameter SHALL be
exposed from the single `find` registry entry across the MCP, REST, and CLI surfaces with
no per-surface code.

#### Scenario: Pack assembly leaves the vault and find untouched

- **WHEN** `find(pack=true)` runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted, and `find`
  ordering is unchanged
- **AND** the `pack` parameter is reachable on the MCP tool, the `/api/find` REST route,
  and the `kb find` CLI from the one registry entry

