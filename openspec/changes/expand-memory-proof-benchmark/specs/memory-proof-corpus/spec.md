## ADDED Requirements

### Requirement: Scenario-Family Registry With Oracle-Ability Classification
The corpus SHALL maintain an explicit registry of scenario families in which
every family is classified as deterministic-oracle (expected records fully
computable), rubric-track (writable knowledge whose quality is only
human/blind-judge assessable, routed to predeclared rubrics), or
out-of-scope (not digitally writable), with a stated rationale per family.
A template MUST NOT register outside a classified family, and the registry
SHALL be published with the corpus documentation so coverage claims are
auditable.

#### Scenario: Template registers under a classified family
- **WHEN** a new template is registered
- **THEN** it names a registry family whose classification is
  deterministic-oracle or rubric-track, and generation fails for a template
  naming an unregistered family

#### Scenario: Coverage claim is auditable
- **WHEN** the corpus documentation states its coverage
- **THEN** it enumerates the registry with each family's classification and
  rationale, including out-of-scope entries

### Requirement: Procedural And Quantitative Reasoning Families
The corpus SHALL include a procedural-knowledge family (ordered how-to
chains where step order, preconditions, and revisions over time are the
ground truth) and a quantitative-reasoning family (questions whose answers
require arithmetic over two or more stored values, with the oracle
computing the expected result and unit), each with oracle-derived
expectations and at least the template/variant discipline of existing
families.

#### Scenario: Step-order question scored deterministically
- **WHEN** a query asks for the step that must precede a named step after a
  procedure revision
- **THEN** the expected record names the post-revision predecessor step and
  the citation of the revising source, and an answer giving the
  pre-revision predecessor fails the current-state gate

#### Scenario: Derived quantity with units
- **WHEN** a query requires combining two stored measurements with a unit
  conversion
- **THEN** the expected record carries the oracle-computed value, unit, and
  tolerance, and both contributing sources as required citations

### Requirement: Negation And Counterfactual Family
The corpus SHALL include scenarios whose ground truth is negative or
counterfactual: facts recorded as NOT holding, plans that were considered
and rejected, and questions whose correct answer distinguishes "recorded as
false" from "not recorded" (absence-of-evidence), scored against the
existing abstention and current-state gates.

#### Scenario: Recorded-false is not unknown
- **WHEN** a query asks about a proposal the corpus records as rejected
- **THEN** the expected answer states the rejection with its citation, and
  an abstention fails while a claim that the proposal is active also fails

### Requirement: Cross-Lingual Fact Family
The corpus SHALL include facts whose source artifact and query language
differ, generated from privacy-safe synthetic vocabulary in at least one
non-Latin script, with expectations that score retrieval and answer
identity across the language boundary; a provider profile that declares no
cross-lingual support SHALL have the family reported unsupported rather
than zero.

#### Scenario: Cross-script recall
- **WHEN** a fact is recorded in a non-Latin-script source and queried in
  English
- **THEN** the expected record requires the same sentinel citation and
  value, and the family reports unsupported for profiles declaring no
  cross-lingual capability

### Requirement: Preference Attribution And Source-Reliability Families
The corpus SHALL distinguish preferences and opinions from facts (whose
holder and as-of time are the ground truth, and whose restatement as
objective fact is a gate failure), and SHALL include a source-reliability
family in which a recurring source's accuracy track record is derivable
from the corpus and questions probe whether conclusions weight sources
accordingly (behaviourally, via required citations and hedging
expectations, never via numeric confidence fields).

#### Scenario: Opinion restated as fact fails
- **WHEN** a query asks what is known about a topic on which the corpus
  holds only an attributed opinion
- **THEN** the expected answer attributes the holder, and an unattributed
  factual restatement fails the calibration gate

#### Scenario: Discredited source is not silently trusted
- **WHEN** a query's only support comes from a source the corpus shows was
  repeatedly corrected
- **THEN** the expected uncertainty requires hedged phrasing and citation of
  the correction history

### Requirement: Long-Horizon Entropy Release
The corpus SHALL provide a long-horizon release variant extending the
ingestion schedule to at least 52 simulated weeks with recurring capture,
duplication, correction, and deletion pressure, and its health metrics
SHALL be reported at quarterly snapshots so convergence-vs-chaos over time
is measurable.

#### Scenario: Quarterly health snapshots
- **WHEN** the long-horizon corpus is evaluated
- **THEN** duplicate, contradiction, stale, and orphan measurements are
  reported at each declared snapshot week from the same run

### Requirement: Multimodal Depth Under The Media Profile
The corpus SHALL include scenarios whose evidence exists only inside real
binary artifacts — a generated PDF, an image requiring OCR, and an audio
transcript pairing — active only under a declared media profile with the
required extras installed; without them the family SHALL degrade with
recorded reasons exactly as pdf-unavailable artifacts do today, and parity
reports SHALL state binary-only facts as degraded for text-only profiles.

#### Scenario: Image-only fact requires the media profile
- **WHEN** a fact exists only inside a generated image and the run lacks the
  media profile
- **THEN** the family is reported degraded/unsupported with the recorded
  reason, never scored zero

### Requirement: Replication Kit And Versioned Releases
Every corpus release SHALL ship a replication kit: pinned seeds, a
one-command regeneration path, and hash verification instructions proven on
a clean checkout; releases SHALL be versioned with changelogs, and the
held-out seed reserved for published numbers SHALL be documented as such
without publishing its artifacts.

#### Scenario: Clean-machine replication
- **WHEN** a third party follows the replication kit on a clean checkout
- **THEN** regeneration reproduces the release manifest hashes exactly or
  the discrepancy is diagnosable from recorded renderer versions
