## ADDED Requirements

### Requirement: Scenarios Are Pre-Registered State Trajectories
Epistemic scenarios SHALL be expressed as phase-keyed state trajectories —
initial sources, allowed operations (including out-of-band operations:
external artifact edit, engine stop and start, fresh-agent continuation,
export, snapshot), expected post-phase state, answer probes held out from
the write path, and continuation and portability probes. The scenario family
registry, assertion registry, and acceptance predicates SHALL be committed
and content-hashed before any competitor run; the hash SHALL appear in every
run manifest. Founder ratification SHALL be an immutable versioned receipt
binding the approved artifact path, original digest, decision, ratifier, date,
and repository revision. Every later change SHALL have a separate immutable,
ordered amendment receipt binding parent and amended whole-document digests,
its repository revision, affected sections, rationale, and effective policy.
Manifests SHALL pin `contract_revision` and record the complete identity chain
through that revision, never a caller-selected subset. Later amendments MAY
change current publishability and SHALL be disclosed, but SHALL NOT
retroactively invalidate a run whose chain was complete at its pin. Each family
SHALL record its public-suite coverage so overlapping families report state
metrics only.

#### Scenario: Post-registration change is an amendment
- **WHEN** a scenario family, assertion, or acceptance predicate changes
  after the pre-registration hash exists
- **THEN** the change lands as a dated amendment with rationale and the
  manifest records both the original hash and the amendment

#### Scenario: Amendment chain is selected or incomplete
- **WHEN** a run omits, reorders, or substitutes any amendment between the
  ratified original and its pinned contract revision
- **THEN** validation refuses the run identity before provider work or report
  generation

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
provider; no retrieval or answer excellence may offset it. The bound-run layer
SHALL persist a typed assertion-evidence payload containing scenario, family,
phase and expectation identity, snapshot references, probe inputs, and result.
A separate `AssertionEvidenceRef` SHALL carry its canonical run-relative path
and sha256, and every failed assertion SHALL require one. Before rendering,
validation SHALL open every path component beneath the run root without
following symlinks, verify regular-file type and digest, reconstruct the
assertion context, rerun the registered assertion, and require exact result
equality. A path or status field alone is not evidence.

#### Scenario: Integrity failure cannot be averaged away
- **WHEN** a provider fails any catastrophic assertion in a run
- **THEN** every aggregate for that provider is suppressed and the failing
  assertion with its artifact path renders in the headline table

#### Scenario: Failure path does not reproduce the assertion
- **WHEN** a failure artifact is missing, unsafe, changed, schema-invalid, or
  produces a different deterministic result when replayed
- **THEN** the row is invalid as a harness fault and no headline renders

### Requirement: Every Scenario Carries A Fairness Packet
Each scenario SHALL ship a fairness packet: why the invariant is
product-neutral in user-harm terms, its public-suite coverage subtraction,
the mechanisms by which each covered competitor could satisfy it with
verdict and evidence, a privileged-endpoint check listing every tool the
exomem driver calls and each competitor's equivalent, and the acceptance
predicate. A scenario whose exomem driver uses a surface with no competitor
equivalent is disqualified from scoring unless the missing equivalent is
itself reported as a capability gap rather than a score. Endpoint checks SHALL
form a closed matrix keyed by exact `driver_surface_id × provider × variant`.
Both `equivalent` and `capability_gap` dispositions SHALL carry audit scope,
competitor-authored evidence, and a reason; only `equivalent` carries the
competitor surface. The actual invoked inventory SHALL be derived from a
persisted, digest-bound instrumented driver/broker receipt. An undeclared call
refuses before assertions execute. Every provider-visible credential, socket,
SDK, CLI, and filesystem surface SHALL be exclusive to that broker, with
source/import and runtime capability conformance proving no direct driver
access. A proved capability gap becomes a named non-comparability exclusion and
never an `unsupported` or scored cell.

#### Scenario: Missing packet blocks the scenario
- **WHEN** a scenario lacks a complete fairness packet
- **THEN** the suite refuses to run that scenario and tests fail naming it

#### Scenario: Privileged call is absent from the receipt-bound matrix
- **WHEN** an instrumented invocation receipt contains a driver surface with
  no exact provider-variant matrix entry
- **THEN** the scenario is disqualified before deterministic scoring and the
  missing surface, provider, and variant are named

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
non-trivially before any comparative table renders. Product and control rows
SHALL share the exact ordered cohort identity: scenario id and digest, phase,
expectation ordinal, assertion, subject/counterpart, tolerance, and freshness
bound. Each control×scenario row SHALL contain at least one deterministic
`pass` or `fail`; empty and blocked/unsupported/not_applicable-only rows refuse.
If either control passes an assertion instance, matching product results SHALL
retain their five-valued outcome and receive the orthogonal
`signal_disposition=no_product_signal`, be disclosed, and be excluded from
every strategy-gate count including G2. All public epistemic tables SHALL
consume the same validated cohort artifact.

#### Scenario: Controls missing blocks the table
- **WHEN** an epistemic comparative table is rendered without current
  control rows for the same scenario set
- **THEN** rendering refuses and names the missing controls

#### Scenario: Control cohort differs or reproduces a product pass
- **WHEN** either control has a different ordered assertion cohort, has no
  deterministic result for a scenario, or passes an assertion instance
- **THEN** cohort validation refuses the mismatch or sets the corresponding
  product result's signal disposition to `no_product_signal` before rendering
  and G2 evaluation
