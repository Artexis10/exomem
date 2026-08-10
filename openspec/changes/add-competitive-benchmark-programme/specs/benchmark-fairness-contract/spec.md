## ADDED Requirements

### Requirement: Competitor Configuration Carries Provenance
Every configuration value applied to a competitor product in any lane SHALL
trace to competitor-authored code or documentation recorded as file and line
or URL in a provenance table; a value without provenance refuses to run.
Defects found in one competitor's configuration of another SHALL be filed
upstream as issues with evidence, never fixed in this repository.

#### Scenario: Cross-competitor fix is refused
- **WHEN** a change proposes correcting one competitor's provider for
  another competitor inside this programme
- **THEN** the change is rejected and the finding is filed upstream instead

### Requirement: Exomem-Authored Glue Is Disclosed And Accounted
Every lane's fairness-matrix entry SHALL record what this repository
authored for each row (files, line counts, endpoints used), who authored
each configuration value, all known asymmetries with their direction, pins,
and blocked measurements with reasons. A row with an unreported known
asymmetry is a defect; a row with a reported asymmetry is a result.

#### Scenario: Missing fairness entry blocks publication
- **WHEN** a comparative row lacks its fairness-matrix entry
- **THEN** rendering refuses to mark the row publishable

### Requirement: Harness Faults Are Never Contender Losses
Environment faults — unreachable services, failed model loads, broken
indexes, near-zero retrieval under a retrieval-floor guard, version-drift
regressions — SHALL invalidate the affected rows rather than score against
the product, for every product equally.

#### Scenario: Near-zero retrieval flags the harness
- **WHEN** a provider retrieves near-zero results across a run's cases
- **THEN** the run is flagged as a suspected harness or environment fault
  and is not scored as a product result without a recorded investigation

### Requirement: Historical-Untrusted Artifacts Are Refused
Run artifacts predating this programme's substrate, and any artifact whose
manifest cannot prove environment and readiness, SHALL be labelled
historical-untrusted; the report renderer SHALL refuse to consume them.

#### Scenario: Old run directory rejected
- **WHEN** report generation is pointed at a run directory without a valid
  terminal protocol manifest
- **THEN** generation refuses with the reason rather than rendering partial
  results

### Requirement: Comparative Publication Requires Independent Adversarial Review
No comparative claim from this programme SHALL be published before an
independent adversarial review — performed by a reviewer with no stake in
the outcome, given the auto-generated adversarial packet (assumptions,
confounds, suspicious-win flags, challenge-artifact paths, pre-registration
hash and amendments) — and every material objection is either fixed or
documented with the publication.

#### Scenario: Unreviewed comparison is blocked
- **WHEN** a comparative results document is finalized without a recorded
  adversarial review disposition
- **THEN** the document renders as internal-diagnostic, not publishable

### Requirement: Provider Variants Never Collapse
Distinct provider variants (hosted versus local, controlled versus native,
document-search versus memory-search, git-backed versus plain) SHALL carry
distinct registered variant identities in every artifact and render as
separate rows with per-row disclosure text; presenting one variant's result
under another's identity is a validity failure.

#### Scenario: Variant conflation is a validity failure
- **WHEN** an artifact or table presents a result under a variant identity
  that differs from the manifest's registered variant
- **THEN** the run or table is INVALID until corrected

### Requirement: Transport Glue Is Not Competitor Configuration
Exomem-authored transport glue MAY isolate state, authenticate loopback
traffic, enforce deadlines, preserve evidence, and adapt process boundaries,
but SHALL forward every competitor-authored renderer, search, retrieval, and
configuration value unchanged under the competitor's pinned project
environment. Protocol-mandated canonical-event projection, including neutral
identity derivation before product-visible ingestion, occurs before and
outside the observation seam and SHALL be applied identically by provenance-
locked protocol adapters. Every private observation seam SHALL preserve the
post-projection arguments, exceptions, object identity, and return values; its files, line count,
endpoints, and observed calls SHALL be disclosed. Isolation-only values SHALL
be labelled Exomem-authored transport values and SHALL NOT be represented as
competitor configuration.

#### Scenario: Observation wrapper changes behavior
- **WHEN** a wrapper changes a competitor call's post-projection arguments,
  exception, return object, fallback decision, result normalization, or
  warm-process lifecycle
- **THEN** the row is INVALID as a modified competitor rather than reported as
  an own-provider result

#### Scenario: Isolation default is disclosed as transport glue
- **WHEN** the Basic Memory sidecar seeds an inert benchmark-owned default
  project to prevent access to the operator's home project
- **THEN** the value is recorded as Exomem-authored isolation glue, every
  configured path is proven inside the work root, and no retrieval default is
  changed

#### Scenario: Growing-corpus reindex asymmetry is recorded
- **WHEN** MemoryBench's one-session ingest lifecycle makes the unmodified
  Basic provider perform one full reindex per unique session
- **THEN** the paired row records that directed asymmetry against Basic's
  grouped own-harness lifecycle and does not batch, skip, or optimize the calls

#### Scenario: Fallback is not a competitor loss
- **WHEN** the pinned provider enters its documented embedding fallback or
  converts a non-JSON MCP response into an empty result
- **THEN** the observation seam leaves provider behavior untouched but marks
  the benchmark row invalid instead of scoring the fallback or ambiguity

#### Scenario: Neutral identity projection precedes observation
- **WHEN** canonical-event normalization replaces an evidence-labelled raw
  session ID with a neutral digest before the Basic renderer is called
- **THEN** the projection is recorded as shared protocol hygiene and the
  observation seam proves exact forwarding from that normalized boundary
