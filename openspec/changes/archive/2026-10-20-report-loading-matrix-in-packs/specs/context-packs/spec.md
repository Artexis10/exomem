## MODIFIED Requirements

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
still be surfaced. In an activated managed runtime, pack assembly SHALL read proximity
pairs only from a resident corpus matrix that is current, or that the bounded catch-up
makes current, and SHALL NOT load it on the request thread.
While the background load runs, `embeddings_available` SHALL be `false`, no proximity
pair SHALL be surfaced, and `truncation` SHALL say that the embedding matrix is still
loading.

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

#### Scenario: A pack names a still-loading embedding matrix

- **WHEN** an activated managed runtime assembles a pack while its corpus matrix loads in
  the background
- **THEN** the pack does not wait for the load and surfaces no proximity pair
- **AND** `embeddings_available` is `false` and `truncation` says the embedding matrix is
  still loading
- **AND** asserted pairs and `contradictions.superseded` are still surfaced
