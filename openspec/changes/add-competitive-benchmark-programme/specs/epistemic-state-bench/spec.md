## ADDED Requirements

### Requirement: Scenarios Are Pre-Registered State Trajectories
Epistemic scenarios SHALL be expressed as phase-keyed state trajectories —
initial sources, allowed operations (including out-of-band operations:
external artifact edit, engine stop and start, fresh-agent continuation,
export, snapshot), expected post-phase state, answer probes held out from
the write path, and continuation and portability probes. The scenario family
registry, assertion registry, and acceptance predicates SHALL be committed
and content-hashed before any competitor run; the hash SHALL appear in every
run manifest; later changes SHALL be dated amendments with rationale. Each
family SHALL record its public-suite coverage so overlapping families report
state metrics only.

#### Scenario: Post-registration change is an amendment
- **WHEN** a scenario family, assertion, or acceptance predicate changes
  after the pre-registration hash exists
- **THEN** the change lands as a dated amendment with rationale and the
  manifest records both the original hash and the amendment

### Requirement: Assertions Are Registered And Deterministic
Every scenario expectation SHALL name an assertion resolved against a
registered assertion set at fixture-load time; an unknown assertion name is
a load error. Assertions SHALL run against observed state snapshots, not
provider internals or answer text, and deterministic assertion results are
final — no judge verdict may overturn one.

#### Scenario: Unknown assertion fails loading
- **WHEN** a scenario declares an assertion name absent from the registry
- **THEN** fixture loading fails before any provider runs

### Requirement: State Is Observed Through Neutral Snapshots
Provider state SHALL be projected into a neutral snapshot schema (items with
kind, currency, revision lineage, evidence edges, contradiction edges,
review state, authorship, and locator) by read-only projectors that use only
documented provider surfaces. Every field mapping SHALL cite
competitor-authored evidence; a declaration without evidence fails tests.
Projector size and endpoint counts SHALL be published, and gross asymmetry
between projectors is itself a reportable finding.

#### Scenario: Unsourced field declaration fails
- **WHEN** a projector declares a field mapping without competitor-authored
  evidence
- **THEN** the projector test suite fails naming the field

### Requirement: Scoring Is Five-Valued And Capability-Honest
Assertion outcomes SHALL be one of pass, fail, not_applicable
(capability-declared absence), unsupported (projector cannot observe), or
blocked (environment fault). A not_applicable outcome in a family SHALL
exclude that family from every comparative claim for all providers; a
property the provider's own materials claim SHALL score fail rather than
not_applicable, with the claim cited; every invariant SHALL carry an
acceptance predicate enumerating at least two structurally different
satisfying representations, and any of them passes.

#### Scenario: N/A poisons the family
- **WHEN** any provider scores not_applicable on a family
- **THEN** report rendering excludes that family from comparative claims and
  prints the exclusion with its reason

### Requirement: Catastrophic Integrity Failures Suppress Aggregates
A registered set of catastrophic assertions (retired state served as
current, destroyed history, unresolvable evidence path for a promoted
conclusion, silently flattened contradiction, cross-case residue, ignored
authoritative external edit) SHALL, on failure, render the provider's row as
an integrity failure that suppresses every aggregate and headline for that
provider; no retrieval or answer excellence may offset it.

#### Scenario: Integrity failure cannot be averaged away
- **WHEN** a provider fails any catastrophic assertion in a run
- **THEN** every aggregate for that provider is suppressed and the failing
  assertion with its artifact path renders in the headline table

### Requirement: Every Scenario Carries A Fairness Packet
Each scenario SHALL ship a fairness packet: why the invariant is
product-neutral in user-harm terms, its public-suite coverage subtraction,
the mechanisms by which each covered competitor could satisfy it with
verdict and evidence, a privileged-endpoint check listing every tool the
exomem driver calls and each competitor's equivalent, and the acceptance
predicate. A scenario whose exomem driver uses a surface with no competitor
equivalent is disqualified from scoring unless the missing equivalent is
itself reported as a capability gap rather than a score.

#### Scenario: Missing packet blocks the scenario
- **WHEN** a scenario lacks a complete fairness packet
- **THEN** the suite refuses to run that scenario and tests fail naming it

### Requirement: Judges Are Confined And Structurally Blinded
Model judges SHALL be limited to semantic task success and continuation
narrative quality, run in a final phase such that deterministic scores are
byte-identical without them, and remain disabled until blinding withstands a
structure-swap test in which identical content presented in each provider's
native structural shape cannot be attributed to its vendor. Judge–human
agreement on a blind sample SHALL precede any published judged number.

#### Scenario: Structure identifies the vendor
- **WHEN** the blinding scanner is given identical content in each
  provider's native structural shape and any classifier distinguishes them
- **THEN** judge use remains blocked and the failure is recorded

### Requirement: Negative Controls Accompany Every Table
Every epistemic results table SHALL include the registered negative controls
(plain text search over the raw corpus, and no-memory) so that invariant
totals are interpretable against a floor, and the controls SHALL score
non-trivially before any comparative table renders.

#### Scenario: Controls missing blocks the table
- **WHEN** an epistemic comparative table is rendered without current
  control rows for the same scenario set
- **THEN** rendering refuses and names the missing controls
