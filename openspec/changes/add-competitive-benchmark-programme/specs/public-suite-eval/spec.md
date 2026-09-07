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

#### Scenario: Paired own-harness and wrapped-provider rows remain distinct
- **WHEN** the MemoryBench subset is evaluated comparatively
- **THEN** Basic Memory's `bm-bench` own-harness row and its unmodified
  `BasicMemoryLocalProvider` MemoryBench row are both present as distinct
  variants, Supermemory's MemoryBench provider row is paired with the 4.7
  direct-SDK spot-check, and no paired observations are collapsed into one
  product number

## ADDED Requirements

### Requirement: Small Scored Diagnostics Reuse Verified Retrieval
The programme MAY prepare a seven-question diagnostic from an existing valid
guest export, selecting one answerable case per question type and one abstention
case before scoring, or replay the complete existing 25-case source. Preparation
SHALL validate source projections, requested per-case readiness, pre-registration
identity and guest cleanup evidence without network calls. Execution SHALL bind
the prepared plan digest, consume validated byte snapshots throughout execution,
revalidate source identity, use the common reader for retrieved, gold-evidence
and empty contexts, and execute the unchanged pinned official LongMemEval judge
through the same metered transport. Every billable call SHALL reserve a
conservative upper bound before transmission. Uncertain billing SHALL retain
the reservation and halt further calls. The diagnostic SHALL preserve original
artifacts, report unmeasured isolation and equivalence explicitly, remain
non-publishable, and SHALL NOT supply full-run approval evidence.

#### Scenario: A small diagnostic gives a measured spending baseline
- **WHEN** seven representative cases are prepared and approved for execution
- **THEN** the reader and judge share one immutable capped ledger
- **AND** the result preserves actual usage and separate retrieval, evidence-only
  and empty-context scores

#### Scenario: Source changes or uncertain spend halt execution
- **WHEN** a prepared artifact or source identity changes before validation,
  or a response does not establish its billed usage
- **THEN** changed inputs refuse scoring before transmission, and uncertain
  billing retains its reservation and stops subsequent calls
- **AND** path replacement after validation cannot change the bytes consumed
  by the reader or judge

#### Scenario: Diagnostic results cannot become a full-run approval
- **WHEN** a diagnostic finishes successfully
- **THEN** its result remains non-publishable and does not replace equivalence,
  scored-pilot, full-run approval or comparative publication gates
