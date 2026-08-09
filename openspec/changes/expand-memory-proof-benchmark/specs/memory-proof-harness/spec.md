## ADDED Requirements

### Requirement: Governed Views Are Wired, Not Simulated
The exomem adapter SHALL translate the corpus policy set into the vault's
opt-in governance policy through public product surfaces during setup, the
runner SHALL thread each query's persona identity to the adapter, and the
adapter SHALL declare the governed-views capability only when this wiring is
active. Governance dimensions SHALL distinguish three states in reporting:
measured against wired governance, measured against an explicitly
ungoverned vault (labelled as the default-open surface), or unsupported.

#### Scenario: Restricted claim withheld for the restricted persona
- **WHEN** governance wiring is active and a persona without the required
  audience queries a restricted claim
- **THEN** the retrieved context and answer contain no forbidden value, the
  no-leak gate passes with a withhold observed, and the same query under the
  owner persona passes with the value present

#### Scenario: Ungoverned measurement is labelled
- **WHEN** a run executes without governance wiring
- **THEN** governance dimensions carry the default-open label and are
  excluded from any comparative governance table

### Requirement: Provider Onboarding Contract
The harness SHALL publish a provider onboarding document sufficient for a
third party to add their system without reading harness internals: the
adapter protocol, capability declaration semantics, native-renderer and
parity-report obligations, default and recommended profile declaration, and
the fairness rules that bind every provider. A new provider submission
SHALL be runnable with conformance tests that do not require the provider's
live system.

#### Scenario: Third-party adapter conformance
- **WHEN** a new provider adapter is submitted following the onboarding
  document
- **THEN** the conformance suite validates protocol shape, capability
  declaration honesty, skip semantics, and parity-report completeness with
  a faked runtime seam

### Requirement: Publication Gate For Comparative Results
Before any public comparative table is published from this benchmark, a
judge–human agreement measurement SHALL exist for every judged dimension in
the table (deterministic-only tables are exempt), published numbers SHALL
come from the held-out release seed, and every published figure SHALL carry
its profile label, provider versions, and a pointer to the replication kit.

#### Scenario: Judged dimension without agreement data
- **WHEN** a comparative table would include a judge-scored dimension with
  no recorded judge-human agreement measurement
- **THEN** publication of that dimension is blocked and the table either
  omits it or ships deterministic dimensions only
