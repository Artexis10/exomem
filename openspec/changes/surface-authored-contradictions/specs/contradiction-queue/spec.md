## ADDED Requirements

### Requirement: Asserted Contradiction Entries From Authored Edges

The `corpus_contradictions` queue SHALL surface every authored `contradicts` graph
edge as an asserted entry, sourced from typed graph edges whose `relation_type` is
`contradicts` rather than from embedding proximity. Both endpoints of an asserted
entry SHALL satisfy the same eligibility the proximity sweep applies — an active,
read-write, compiled conclusion that is not an index or log hub — so that only pairs
a reader can actually reconcile are surfaced. A symmetric authored edge SHALL yield
exactly one deduped, unordered pair anchored on the smaller path, carrying the other
endpoint in the finding's `paths`, exactly like a proximity pair.

Asserted entries SHALL NOT depend on the embedding sidecar: they SHALL be emitted
even when `EXOMEM_DISABLE_EMBEDDINGS` is set, when no sidecar exists, or when the
contradiction band is inverted. Asserted entries SHALL NOT be subject to the
`EXOMEM_CONTRADICTION_TOP_N` cap, and the cap's summary finding and omitted counts
SHALL continue to describe only the proximity lane.

Asserted entries SHALL carry the same fingerprint-bound triage contract as every
other entry: a stable review identity and a `meta.signal_version` derived from both
endpoints' current content, so a dismissal resurfaces when either note changes. Their
`meta.signal_version` SHALL be distinct from the proximity entry's for the same pair.

When the typed graph index is disabled, missing, or warming, asserted entries SHALL be
absent rather than fabricated, and the proximity lane SHALL be unaffected.

#### Scenario: An authored contradicts edge surfaces without embeddings

- **WHEN** an active compiled note authors `contradicts` against another active
  compiled note and `EXOMEM_DISABLE_EMBEDDINGS` is set
- **THEN** the `corpus_contradictions` category emits exactly one finding for that
  pair, anchored on the smaller path with both paths in `paths`
- **AND** no embedding model is loaded and no sidecar is read

#### Scenario: Asserted entries are emitted before every proximity pair

- **WHEN** the queue contains both authored `contradicts` pairs and in-band proximity
  pairs
- **THEN** every asserted finding is emitted before the first proximity finding
- **AND** the proximity findings keep their existing priority, same-family, and cap
  ordering among themselves

#### Scenario: Ineligible endpoint is not surfaced

- **WHEN** an authored `contradicts` edge points at a superseded, archived, draft,
  raw-source, or read-only page
- **THEN** no asserted finding is emitted for that pair

#### Scenario: Unavailable graph yields no asserted entries

- **WHEN** the typed graph index is disabled or has not been built
- **THEN** the category emits no asserted findings and reports no fabricated pair
- **AND** the proximity sweep behaves exactly as it does today

### Requirement: Entry Provenance Labelling

Every `corpus_contradictions` pair finding SHALL carry an explicit
`meta.provenance` of `asserted` or `proximity`, so a reader can distinguish a
conflict the author asserted from a conflict the embedding band merely measured. A
pair surfaced as asserted MUST NOT also surface as a proximity pair, MUST NOT be
counted toward the `EXOMEM_CONTRADICTION_TOP_N` cap, and MUST NOT appear in the
summary finding's omitted count.

Adding the provenance label MUST NOT change any existing entry's
`meta.signal_version`, so no already-recorded triage decision resurfaces because of
this labelling alone.

#### Scenario: Both lanes are labelled

- **WHEN** the queue emits an asserted pair and an in-band proximity pair
- **THEN** the asserted finding carries `meta.provenance: "asserted"` and the
  proximity finding carries `meta.provenance: "proximity"`

#### Scenario: An asserted pair suppresses its proximity duplicate

- **WHEN** a pair is both authored as `contradicts` and in the proximity band
- **THEN** exactly one finding is emitted for that pair, with
  `meta.provenance: "asserted"`
- **AND** the pair is not counted in the cap's total or omitted count

### Requirement: Competing-Alternatives Pair Stance

The system SHALL provide a `competing` triage disposition recording that two notes
are competing alternatives the reader intends to keep ("rivals; keep both"). The
stance SHALL be recorded in the existing review-state store with the same record
shape, record key, and atomic write path as `dismiss` and `snooze`, keyed on a
review identity derived from the pair rather than from a single queue item, so that
the same stance is addressable both from the review queue and from a write-time
check that knows only the two paths.

The stance SHALL be fingerprint-bound to both endpoints' current content. Editing
either note SHALL change the pair fingerprint so that the stored stance no longer
matches and the pair resurfaces as open, exactly as a fingerprint-bound dismissal
does. `reopen` on a stanced pair SHALL clear the pair stance.

The stance SHALL be refused for a review item that carries no counterpart, because
"rivals; keep both" is meaningless for a single-note signal. Recording a stance MUST
NOT mutate any note, MUST NOT supersede, merge, or rank either rival against the
other, and MUST NOT change `find` ordering.

#### Scenario: A stance removes the pair from the open queue

- **WHEN** a reader records the `competing` stance on a surfaced contradiction pair
- **THEN** the pair's effective review state becomes `competing` and it no longer
  appears in the default open view
- **AND** no file under the vault other than the review-state store is created,
  modified, moved, or deleted

#### Scenario: Editing a rival resurfaces the stance

- **WHEN** either note of a `competing` pair is edited
- **THEN** the pair fingerprint changes, the stored stance no longer applies, and the
  pair returns to the open view

#### Scenario: A stance is refused for a pairless item

- **WHEN** `competing` is requested for a review item with no counterpart reference
- **THEN** the request is refused with an explicit error and nothing is recorded

#### Scenario: Reopen clears the stance

- **WHEN** `reopen` is applied to a pair that carries a `competing` stance
- **THEN** the pair stance is cleared and the pair returns to the open view

### Requirement: Structural-Pair Exemption For Write-Time Proximity Warnings

The write-time near-duplicate and overlap draft warnings — the only duplicate
mechanism in the product — SHALL suppress a candidate when the draft's own page and
that candidate are a declared pair. A pair SHALL count as declared when the reader has
recorded a matching `competing` stance, or structurally when the two pages already
carry an authored `contradicts` edge between them, or when both carry an authored
`answers` edge into the same target. Suppression SHALL apply to both the
near-duplicate band and the overlap band, because genuine rivals frequently sit above
the duplicate threshold rather than below it.

Suppression SHALL apply only when the draft has an existing page identity; a draft
with no page of its own SHALL warn exactly as it does today. Suppression MUST NOT
apply to the contradiction queue or to deep packs: an authored or stanced pair still
surfaces there for review.

#### Scenario: A stanced pair stops warning at write time

- **WHEN** a page carrying a `competing` stance with a candidate is edited
- **THEN** no near-duplicate or overlap warning is emitted for that candidate
- **AND** the pair still appears in the contradiction queue

#### Scenario: An authored contradicts edge exempts the pair

- **WHEN** a page that already authors `contradicts` against a candidate is edited
- **THEN** no near-duplicate or overlap warning is emitted for that candidate, with
  no stance recorded

#### Scenario: Two answers to one question exempt the pair

- **WHEN** the edited page and the candidate both author an `answers` edge into the
  same question page
- **THEN** no near-duplicate or overlap warning is emitted for that candidate

#### Scenario: An undeclared pair still warns

- **WHEN** the edited page and the candidate carry no stance and no declaring edge
- **THEN** the near-duplicate and overlap warnings are emitted exactly as today

## MODIFIED Requirements

### Requirement: Review-Priority Ordering by Cosine and Dormancy

The system SHALL order the surfaced proximity `corpus_contradictions` pairs by a
per-pair review priority computed from the pair's embedding cosine (closer pairs
ranked higher) and the ACT-R base-level dormancy of the pair's two notes (a more
dormant note raises the pair's priority), so that the most-worth-reviewing pairs
surface first. The priority SHALL reuse the existing `stale_review` activation
machinery and SHALL be sort-only — it MUST NOT change which pairs are eligible, the
band edges, or `find` ranking.

Asserted entries SHALL be ordered ahead of every proximity pair regardless of that
priority, because an authored conflict is a stated stance rather than a measured
adjacency. The priority, dormancy, and same-family computations SHALL NOT be applied
to asserted entries, and asserted entries SHALL be ordered among themselves
deterministically by anchor path and then counterpart path.

#### Scenario: Closer pair ranks above a more distant pair at equal dormancy

- **WHEN** two cross-family in-band pairs have equal note dormancy but different
  cosines
- **THEN** the pair with the higher cosine appears earlier in the findings list
- **AND** each finding carries `meta.cosine` and `meta.priority`

#### Scenario: Dormant note lifts an equally-close pair

- **WHEN** two cross-family pairs have the same cosine but one pair contains a
  note that is dormant (never surfaced/read/cited) while the other pair's notes
  are recently and frequently accessed
- **THEN** the pair containing the dormant note appears earlier in the findings
  list
- **AND** the access signal being gated or absent is treated as maximally dormant,
  never as a fabricated "active" note

#### Scenario: An asserted pair outranks the closest proximity pair

- **WHEN** an authored `contradicts` pair and a maximum-priority proximity pair are
  both eligible
- **THEN** the asserted finding appears earlier in the findings list
- **AND** the asserted finding carries no `meta.priority`, `meta.dormancy`, or
  `meta.same_family`

### Requirement: Measurement-Only Ordering

The ordering, demotion, and capping SHALL be measurement-only. The system MUST
NOT mutate any note, MUST NOT auto-supersede, and MUST NOT affect `find` ranking.
When embeddings are disabled (`EXOMEM_DISABLE_EMBEDDINGS`), the proximity sweep
SHALL continue to short-circuit to an empty result without loading any model, while
asserted entries — which are read from authored graph edges and need no vectors —
SHALL still be surfaced.

Surfacing an asserted conflict SHALL remain proposal-only. The system MUST NOT rank
the two rivals against each other, MUST NOT infer which is correct, and MUST NOT
auto-merge, auto-supersede, or auto-dismiss either of them.

#### Scenario: Audit run leaves notes untouched

- **WHEN** an audit with the `corpus_contradictions` category runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** `find` ranking is unchanged

#### Scenario: Embeddings disabled keeps the proximity lane a no-op

- **WHEN** `EXOMEM_DISABLE_EMBEDDINGS` is set and no page authors a `contradicts`
  edge
- **THEN** the category returns no findings and loads no embedding model

#### Scenario: Embeddings disabled still surfaces authored conflicts

- **WHEN** `EXOMEM_DISABLE_EMBEDDINGS` is set and a page authors a `contradicts`
  edge against another eligible page
- **THEN** the category returns that asserted finding and still loads no embedding
  model
