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
