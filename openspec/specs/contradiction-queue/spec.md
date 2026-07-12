# contradiction-queue Specification

## Purpose
Surface the contradiction pairs most worth reviewing first instead of in
arbitrary order: the `corpus_contradictions` queue is sorted by a review
priority derived from embedding cosine and note dormancy, with same-family
pairs (noise from adjacent notes in the same research subfolder) demoted below
cross-family pairs rather than dropped. Ordering is sort-only and
measurement-only — it never changes which pairs are eligible, never mutates
notes, and never affects `find` ranking — and the cap on surfaced pairs always
reports an explicit omitted count.
## Requirements
### Requirement: Review-Priority Ordering by Cosine and Dormancy

The system SHALL order the surfaced `corpus_contradictions` pairs by a per-pair
review priority computed from the pair's embedding cosine (closer pairs ranked
higher) and the ACT-R base-level dormancy of the pair's two notes (a more dormant
note raises the pair's priority), so that the most-worth-reviewing pairs surface
first. The priority SHALL reuse the existing `stale_review` activation machinery
and SHALL be sort-only — it MUST NOT change which pairs are eligible, the band
edges, or `find` ranking.

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

### Requirement: Same-Family Adjacency Demotion

The system SHALL mark a pair whose two notes live in the same
`Notes/Research/<X>/` subfolder as same-family via a `meta.same_family` flag and
SHALL sort every same-family pair after all cross-family pairs, regardless of
priority, so that architecture-cluster adjacency noise does not crowd out
cross-family review candidates. Same-family pairs SHALL be demoted, never
dropped.

#### Scenario: Same-family pair is demoted below a lower-priority cross-family pair

- **WHEN** a same-family pair has a higher raw priority than a cross-family pair
- **THEN** the cross-family pair appears earlier in the findings list
- **AND** the same-family finding carries `meta.same_family: true`

#### Scenario: Cross-family pair is not flagged same-family

- **WHEN** a pair's two notes are not in the same `Notes/Research/<X>/` subfolder
- **THEN** its finding does not mark `meta.same_family` true

### Requirement: Capped Surfacing with an Explicit Omitted Count

The system SHALL cap the surfaced pairs at the top `EXOMEM_CONTRADICTION_TOP_N`
(default 40) by review priority and SHALL, when more pairs are in band, append
exactly one summary finding reporting the number of lower-priority/same-family
pairs not shown. The system MUST NOT silently truncate. Setting
`EXOMEM_CONTRADICTION_TOP_N` to `0` SHALL disable the cap and surface all pairs
with no summary finding.

#### Scenario: Excess pairs are capped and counted

- **WHEN** the number of in-band pairs exceeds `EXOMEM_CONTRADICTION_TOP_N`
- **THEN** only the top-N pairs by priority are surfaced as pair findings
- **AND** one additional summary finding reports the omitted count via
  `meta.truncated` and a detail line stating "<N> more ... pairs not shown"

#### Scenario: Cap disabled surfaces everything

- **WHEN** `EXOMEM_CONTRADICTION_TOP_N` is `0`
- **THEN** every in-band pair is surfaced
- **AND** no summary finding is appended

### Requirement: Measurement-Only Ordering

The ordering, demotion, and capping SHALL be measurement-only. The system MUST
NOT mutate any note, MUST NOT auto-supersede, and MUST NOT affect `find` ranking.
When embeddings are disabled (`EXOMEM_DISABLE_EMBEDDINGS`), the category SHALL
continue to short-circuit to an empty result without loading any model.

#### Scenario: Audit run leaves notes untouched

- **WHEN** an audit with the `corpus_contradictions` category runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** `find` ranking is unchanged

#### Scenario: Embeddings disabled stays a no-op

- **WHEN** `EXOMEM_DISABLE_EMBEDDINGS` is set
- **THEN** the category returns no findings and loads no embedding model

