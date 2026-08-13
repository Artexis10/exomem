## ADDED Requirements

### Requirement: Objective Measurements Are Automated And Pinned
Operational measurements (installation to healthy state, readiness
visibility, time to first useful answer, second-client connection, resource
footprint, disk growth by source versus derived state, backup and restore
round-trip verified by state-snapshot equality, recovery after interrupted
indexing, external-edit freshness, cost per unit of work, media retention
policy by checksum) SHALL be produced by scripts against pinned product
versions in isolated throwaway environments, each measurement carrying its
method and evidence path in a manifest.

#### Scenario: Measurement without evidence
- **WHEN** an operational measurement is reported without a method and
  evidence path
- **THEN** the report renders it as unverified and excludes it from
  comparisons

### Requirement: Heuristic Judgments Are Labeled
Assessments that cannot be automated (documentation friction, conceptual
load) SHALL be recorded as labeled heuristic judgments with rationale,
evidence paths, the judge's identity class, and whether the assessment was
blinded — never presented as quantitative fact.

#### Scenario: Heuristic presented as measurement
- **WHEN** a heuristic judgment appears in a table of automated
  measurements without its heuristic label
- **THEN** report tests fail naming the row

### Requirement: No Aggregate And No Comparative Latency From Unvalidated Hosts
Operational reporting SHALL NOT combine measurement families into any
aggregate score, and SHALL refuse to render cross-provider latency
comparisons from hosts without validated latency characteristics; latency on
such hosts renders per-provider as indicative-only.

#### Scenario: Latency column refused
- **WHEN** a report is rendered from runs on a host flagged
  latency-unvalidated
- **THEN** no cross-provider latency column renders and each latency cell
  carries the indicative-only label

### Requirement: Failure Transparency Is Fault-Injected
Each product SHALL be measured under injected faults (malformed source,
unreachable model endpoint, read-only index storage) and scored on whether
the user-facing surface reports the fault, degrades silently, or corrupts
state — including this repository's own product.

#### Scenario: Silent degradation is recorded against any product
- **WHEN** an injected fault produces reduced capability with no user-facing
  fault signal in any product, including exomem
- **THEN** the run records silent degradation for that product with the
  evidence transcript
