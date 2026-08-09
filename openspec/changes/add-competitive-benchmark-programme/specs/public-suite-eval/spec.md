## MODIFIED Requirements

### Requirement: External Suite Evaluation Is Exomem-Only
The OFFICIAL published external-suite row SHALL evaluate exomem alone, under
the suite's official dataset variant and grading protocol. The repository MAY
additionally produce competitor rows for an external suite only when all of
the following hold: every competitor-side configuration value traces to
competitor-authored code or documentation, recorded in a per-provider
provenance table (file and line, or documentation URL); the row is produced
either by the competitor's own harness or by wrapping the competitor's own
provider implementation unmodified under its own project environment; the
Exomem-authored glue for the row is disclosed with size accounting in the
fairness matrix; and publication of any comparative claim is gated on the
independent adversarial review required by `benchmark-fairness-contract`. A
published table MAY still place exomem's official row beside figures
published by competitors' owners, cited as such with configuration caveats. A
competitor number produced by this repository's direct lane SHALL be
published only alongside that competitor's own-harness row for the same
subset.

#### Scenario: Configuration without provenance refuses to run
- **WHEN** a competitor provider is invoked and any configuration value has
  no recorded competitor-authored provenance
- **THEN** the provider refuses to run and the run records the missing
  provenance as the refusal reason

#### Scenario: Controlled row without its paired harness row
- **WHEN** a results document would publish a competitor number produced by
  the direct lane and no corresponding competitor-harness row exists for the
  same subset
- **THEN** rendering refuses to mark that row publishable

#### Scenario: Self-authored competitor configuration is rejected
- **WHEN** a task or change proposes competitor-side configuration values
  authored in this repository without competitor-authored provenance
- **THEN** the proposal is rejected under this requirement, citing the
  authored-competitor defect class from the 2026-08-08 audit
