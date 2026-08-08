## ADDED Requirements

### Requirement: External Suite Evaluation Is Exomem-Only
The public-suite evaluation lane SHALL evaluate exomem alone. The repository
SHALL NOT author, configure, invoke, or maintain an integration of any
competitor product with any external suite. A published table MAY place
exomem's result beside competitor figures only when each competitor row is
cited to a result published by that product's owner or an independent third
party, with the differing reader/judge configuration stated on the row.

#### Scenario: Competitor integration is refused
- **WHEN** a task or change proposes adding a competitor adapter, renderer,
  or profile for an external suite
- **THEN** the proposal is rejected under this requirement, and the
  rejection cites the authored-competitor defect class from the 2026-08-08
  audit

#### Scenario: Published table cites owners
- **WHEN** a results document places exomem's suite score beside another
  product's score
- **THEN** the competitor row carries a citation to its owner's or a third
  party's publication and a configuration caveat, and no competitor number
  in the table was produced by this repository

### Requirement: Official Protocol Fidelity
A public-suite run SHALL use the suite's official dataset variant and
grading protocol unmodified: the pinned dataset with recorded checksum, the
suite's shipped judge prompts and designated judge model, and per-ability
reporting with no aggregate score. The reader model and all component
versions SHALL be recorded in the run environment. Hypothesis output and
judge verdicts SHALL be preserved as run artifacts sufficient for
third-party re-judging.

#### Scenario: Modified judge voids publishability
- **WHEN** a run is produced with an altered judge prompt, a substituted
  judge model, or a filtered question set not defined by the suite
- **THEN** the run is marked not publishable and reported only as an
  internal diagnostic

#### Scenario: Re-judgeable artifacts
- **WHEN** a run completes
- **THEN** its artifacts include the hypothesis file, the judge verdict
  file, dataset checksum, and reader/judge model identifiers, such that an
  outside party can re-run judging without access to this machine

### Requirement: Bounds Accompany Every Published Figure
Every published per-ability figure SHALL be accompanied by a
gold-evidence-retrieval ceiling and a null-abstain floor measured under the
same reader configuration, so that harness or reader limitations are
distinguishable from product limitations.

#### Scenario: Figure without bounds is blocked
- **WHEN** a results document is generated and a reported ability lacks a
  ceiling or floor measurement from the same reader configuration
- **THEN** generation refuses to render that ability's row as publishable

#### Scenario: Ceiling failure reclassifies the row
- **WHEN** the ceiling configuration itself fails a question class
- **THEN** the affected rows are reported as harness-bounded rather than as
  product findings

### Requirement: Product Defaults Are Not De-Tuned
The exomem integration SHALL run with product-default retrieval behaviour —
graph ranking, compiled preference, and active-state ranking enabled —
unless a documented capability tier states otherwise. Determinism pins that
change no capability MAY be applied; capability amputations MUST NOT be,
and a run whose requested semantic capability fails to load SHALL be an
environment fault, never a product score.

#### Scenario: De-tuned run is not publishable
- **WHEN** a run overrides product-default ranking or disables a capability
  lane without a documented tier declaration
- **THEN** the run is labelled de-tuned and excluded from publishable
  results

#### Scenario: Failed semantic load invalidates
- **WHEN** the embedding model fails to load under a profile that requires
  it
- **THEN** the run is INVALID with an environment fault, and no score is
  recorded

### Requirement: Pilot And Spend Gates Precede The Full Run
The lane SHALL support a stratified pilot subset covering every suite
ability, and SHALL record measured ingest wall-time and per-question API
cost from the pilot. Metered API usage requires explicit founder approval
before the first billable call, and the full-question run requires explicit
founder approval given the pilot evidence.

#### Scenario: Billable call before approval is refused
- **WHEN** the reader or judge backend would issue a metered API call and
  no recorded founder approval exists
- **THEN** the lane refuses the call and reports the missing gate

#### Scenario: Full run gated on pilot evidence
- **WHEN** a full-question run is requested
- **THEN** it proceeds only with recorded pilot artifacts (scores, ingest
  wall-time extrapolation, cost extrapolation) and founder approval given
  after them
