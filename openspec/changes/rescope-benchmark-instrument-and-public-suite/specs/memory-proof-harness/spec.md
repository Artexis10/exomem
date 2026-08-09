## ADDED Requirements

### Requirement: Cross-Contender Comparison Retirement
The harness SHALL NOT produce cross-contender comparative results for
publication. Contender adapters other than exomem and the reference bounds
(oracle-retrieval ceiling, null-abstain floor) are retained solely as
internal diagnostic fixtures, and any run or report containing a
non-reference competitor SHALL be labelled "internal diagnostic — not
publishable" in its manifest and rendered report.

#### Scenario: Competitor run is labelled internal
- **WHEN** a run specification names a contender that is neither exomem nor
  a reference bound
- **THEN** the run manifest and report carry the internal-diagnostic label
  and the report generator excludes the run from any publishable output set

#### Scenario: Comparative claim requires the public-suite lane
- **WHEN** a document in this repository asserts exomem's standing relative
  to another product
- **THEN** the assertion rests on the public-suite-eval capability's
  citation rules, not on Track B output

### Requirement: Structurally Incomparable Columns Are Withheld
Latency and harness-answer-mode abstention SHALL be withheld from every
cross-contender surface permanently: latency because adapter transport
differences (in-process versus per-query subprocess) dominate the
measurement, and harness-mode abstention because the shared extractive
answerer authors the column identically for all contenders. Withheld
columns SHALL be rendered as withheld with the structural reason, never as
zeros or blanks.

#### Scenario: Latency column withheld
- **WHEN** a report renders any surface covering more than one contender
- **THEN** no latency figures appear, and the withholding reason names the
  transport asymmetry

#### Scenario: Harness-mode abstention withheld
- **WHEN** a report renders abstention results from harness answer mode
- **THEN** the column is withheld with the shared-answerer reason, and only
  native-mode abstention (a contender's own decision seam) may be reported

### Requirement: Internal Instrument Contract
Track B's purpose SHALL be product regression and compiled-path correctness
against real product surfaces, bounded by the oracle-retrieval ceiling and
null-abstain floor. Internally reported dimensions SHALL carry those bounds.
New scenario families SHALL NOT be added without a stated internal product
question that existing families cannot answer, and packaging obligations
aimed at external replication (release-byte pinning, provider onboarding,
judge–human agreement for comparative tables) remain stood down under this
contract.

#### Scenario: Dimension reported with bounds
- **WHEN** a Track B dimension is reported internally
- **THEN** the report includes the ceiling and floor results for that
  dimension from the same corpus generation

#### Scenario: New family requires an internal question
- **WHEN** a change proposes a new scenario family
- **THEN** the proposal states the internal product question the family
  answers, or it is rejected under this contract
