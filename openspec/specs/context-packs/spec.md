# context-packs Specification

## Purpose
Let a caller get more context from one `find` call instead of chaining several
`get` calls: an optional `pack` parameter that assembles a bounded context
pack — structurally extracted claims, a co-citation-ranked wikilink
neighborhood, and recorded contradictions/supersessions — over the top hits.
Assembly is purely structural and deterministic (no generative or reasoning
model), never mutates the vault, and never changes `find`'s hits or ordering
when `pack` is off.
## Requirements
### Requirement: Optional Context Pack Assembly From `find`

The system SHALL provide an optional `pack` parameter on `find` (default
`false`) that, when `false`, returns the existing hit list **unchanged** and,
when `true`, returns an object `{"hits": [...], "pack": {...}}` where `pack` is
an assembled context pack over the top hits carrying `packed_paths`, `claims`,
`semantic_blocks`, `neighborhood`, `contradictions`, `embeddings_available`,
and `truncation`. The assembly SHALL NOT alter the hits, their order, or any
existing `find` behaviour, and the core `find` ranker signature and return type
SHALL be unchanged (the parameter and the object return are confined to the
command leaf).

#### Scenario: Pack off is byte-identical to today

- **WHEN** `find` is called with `pack` omitted or `false`
- **THEN** it returns the same hit list it returns today, with no `pack` object
  and no change to ordering or fields

#### Scenario: Pack on returns hits plus an assembled pack

- **WHEN** `find` is called with `pack=true` over a vault with matching notes
- **THEN** it returns `{"hits", "pack"}` where `hits` is the usual list and
  `pack` carries `packed_paths` (the top notes covered), `claims`,
  `semantic_blocks`, `neighborhood`, `contradictions`, `embeddings_available`,
  and `truncation`
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Semantic blocks are additive

- **WHEN** a packed page contains supported semantic block headings
- **THEN** `pack.semantic_blocks` includes parsed block dictionaries keyed by
  packed page path
- **AND** pages without semantic blocks do not require any placeholder block
  entries

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
`superseded_by` frontmatter, and `tension` pairs among the packed notes. Tension pairs
SHALL come from two distinct sources, each labelled with an explicit `provenance`
field: `asserted` pairs, read from authored `contradicts` graph edges between two
packed notes, and `proximity` pairs, whose pairwise cosine sits in the existing
contradiction band `[CONTRADICTION_FLOOR, DUP_THRESHOLD)`. A pair that is both
authored and in band SHALL be surfaced once, as `asserted`.

Asserted pairs SHALL be ordered before proximity pairs and SHALL NOT depend on the
embedding sidecar. A proximity pair SHALL be labelled as proximity, not polarity,
deferring the judgment to the reader, and SHALL be computed only among the packed
notes. When the embedding sidecar is unavailable (embeddings disabled or
unimportable), `embeddings_available` SHALL be `false` and no proximity pair SHALL be
surfaced, while asserted pairs and `superseded` edges (which need no embeddings) SHALL
still be surfaced.

Pack assembly SHALL NOT consult review state: an authored or stanced pair is
reasoning context here, not a work item, and labelling it MUST NOT suppress it.

#### Scenario: A recorded supersession edge is surfaced

- **WHEN** a packed note's `superseded_by` points at another note in the set
- **THEN** `contradictions.superseded` carries that `{from, to}` edge, read from
  frontmatter without embeddings

#### Scenario: Embeddings-off still yields a useful pack

- **WHEN** `find(pack=true)` runs with embeddings disabled
- **THEN** `embeddings_available` is `false`, no proximity tension pair is surfaced,
  and `claims`, `neighborhood`, and `contradictions.superseded` are still populated

#### Scenario: An authored contradicts pair is surfaced without embeddings

- **WHEN** two packed notes carry an authored `contradicts` edge and embeddings are
  disabled
- **THEN** `contradictions.tension` carries that pair with `provenance: "asserted"`
- **AND** `embeddings_available` remains `false`

#### Scenario: Proximity pairs are labelled and ordered after asserted pairs

- **WHEN** a pack contains both an authored `contradicts` pair and an in-band
  proximity pair
- **THEN** the asserted pair appears first with `provenance: "asserted"` and the
  proximity pair follows with `provenance: "proximity"`

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

### Requirement: Deep packs honor release decisions

Deep context packs SHALL be assembled only from items that have passed the release
gate, and each pack element SHALL carry its decision. A pack SHALL NOT contain the
content, claims, neighborhood, or contradictions of any item released below its
excerpt level, and the pack header SHALL carry the governance context (policy
fingerprint and any withheld notices) rather than sub-notice content.

#### Scenario: Withheld item is absent from the pack

- **WHEN** a pack is assembled for a query whose candidates include a withheld item
- **THEN** that item's content, claims, and neighborhood do not appear in the pack,
  and its withholding is represented only by a notice in the pack header

#### Scenario: Permitted pack elements are unchanged

- **WHEN** a pack is assembled with no governed items
- **THEN** the pack is identical to baseline

### Requirement: Unified bounded memory context
`connect_memory(operation="context")` SHALL accept a query, path, or stable reference and return seed nodes, semantic blocks, typed edges, source/evidence provenance, supersession history, warnings, and explicit truncation. `graph-context` SHALL remain a compatibility alias.

#### Scenario: Context follows evidence and history
- **WHEN** context is requested for a source-backed conclusion that supersedes an earlier version and cites evidence
- **THEN** the response includes the active and prior conclusion, their supersession edge, the source/evidence nodes, and provenance paths within configured bounds

### Requirement: Context assembly remains measurement-only
Unified context SHALL use deterministic parsing, stored relations, retrieval, and precomputed model measurements only. It MUST NOT generate summaries, accept suggested relations, change retrieval ranking, or mutate the vault.

#### Scenario: Context request is read-only
- **WHEN** unified context is assembled with graph enrichment
- **THEN** no vault file changes and returned excerpts are sourced from stored content with provenance

### Requirement: Unresolved observed relations remain visible
The graph SHALL represent an observed edge to a missing target with a typed placeholder node rather than silently dropping the edge during traversal.

#### Scenario: Forward reference appears in context
- **WHEN** a semantic relation points to a not-yet-created page
- **THEN** context includes the observed edge and an unresolved placeholder carrying the original target

### Requirement: Opt-In Typed Graph Neighborhoods In Packs
The system SHALL allow `find(pack=true)` to include a typed graph neighborhood when graph enrichment is explicitly requested and the epistemic graph sidecar is available. This enrichment SHALL be additive: `find(pack=false)` remains unchanged, default pack behavior remains compatible with the existing pack contract, and `find` hit ordering remains unchanged.

#### Scenario: Pack without graph request stays compatible
- **WHEN** `find` is called with `pack=true` and graph enrichment is not requested
- **THEN** the pack response preserves the existing pack fields and behavior
- **AND** no graph-specific fields are required for the call to succeed

#### Scenario: Graph-enriched pack includes typed relations
- **WHEN** `find` is called with `pack=true` and graph enrichment requested over a vault with an available graph sidecar
- **THEN** the returned pack includes graph neighborhood data with typed nodes, typed edges, relation types, and provenance for packed paths
- **AND** the hits list, hit ordering, and existing pack claims remain unchanged

### Requirement: Graph Pack Enrichment Soft-Fails
Graph enrichment in packs SHALL soft-fail when the graph sidecar is missing, stale, disabled, or schema-incompatible. The pack response SHALL remain useful through existing structural claims, wikilink neighborhoods, and contradiction/supersession fields, and SHALL report graph availability instead of raising an unhandled error.

#### Scenario: Missing sidecar falls back to existing pack
- **WHEN** `find(pack=true)` requests graph enrichment but no graph sidecar exists
- **THEN** the pack response is produced using the existing non-graph pack assembly
- **AND** the response indicates graph enrichment was unavailable

#### Scenario: Graph enrichment does not mutate files
- **WHEN** graph-enriched pack assembly runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** no graph relation suggestion is persisted as an accepted fact

### Requirement: Unified context exposes registry-aware traversal
Unified context SHALL accept a traversal profile and return the resolved profile,
core registry version, extension-registry hash, included relation families,
canonical/raw relation metadata, unknown/out-of-scope counts, warnings, and
explicit truncation. Extension edges selected through a core parent SHALL retain
their more precise canonical key in the response.

#### Scenario: Cross-domain support remains precise and portable
- **WHEN** epistemic context encounters a registered domain extension whose core
  parent is `supports`
- **THEN** the edge is included as its namespaced canonical type, reports parent
  `supports`, and carries its raw label and source provenance

### Requirement: Context never hides unknown relation observations
Normal context SHALL exclude unregistered edges from traversal but SHALL report
their bounded count and source examples as advisory warnings when they occur in
the selected neighborhood. An explicit diagnostic view MAY include the
semantically inert observed edges while clearly marking them unregistered. These
warnings MUST NOT add items to default attention or alter ordinary retrieval.

#### Scenario: Unknown edge is warned without semantic promotion
- **WHEN** a seed page contains a valid but unregistered typed relation
- **THEN** context reports the observation in warnings and does not treat it as a
  core or extension family edge

### Requirement: Adoption work items are a governed pack consumer

Semantic unit context packs SHALL be available to `adoption_studio(action="work-item")` as a read-only consumer: pack assembly for a governed Source under adoption SHALL reuse the same pack construction and bounding rules as the primary pack surface, with the work item's caps applied on top. Pack assembly SHALL never mutate units, indexes, or pages.

#### Scenario: Pack construction is shared, bounds are the consumer's

- **WHEN** a work item assembles the pack for a bound source
- **THEN** the pack's content matches what the primary pack surface would return for that source
- **AND** the stricter of the two bounds (pack surface vs work item caps) applies

#### Scenario: Assembly is read-only

- **WHEN** packs are assembled for a work item
- **THEN** no vault file, index, or unit record is written
