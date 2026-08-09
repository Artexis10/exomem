## ADDED Requirements

### Requirement: Canonical Events Quarantine Gold
Provider adapters SHALL receive dataset content only as canonical protocol
events carrying neutralized public identity (ordinal session identity, a
content hash over provider-visible bytes, declared timestamp semantics, and
hashed upstream identifiers). Gold answers, evidence labels, category labels
that leak answers, and future question text SHALL live in a separate record
type that adapter interfaces structurally cannot receive.

#### Scenario: Adapter cannot receive gold
- **WHEN** benchmark code attempts to pass a gold-bearing case record where
  an adapter expects protocol events
- **THEN** the call fails at the interface, and a test asserts this failure

#### Scenario: Evidence-labelled upstream identifiers are neutralized
- **WHEN** a dataset's raw session identifiers carry answer-evidence markers
- **THEN** normalized events expose only ordinal identity and a hash, and a
  dataset-level precondition refuses ingestion under an outdated neutralizer

### Requirement: Outbound Payloads Are Scanned For Leakage
The substrate SHALL scan the payloads actually transmitted to providers —
captured at the transport layer, not the intended payloads — under
scope-specific policies: ingestion payloads strictly (any gold text, answer
shingle, label token, or evidence-marked identifier invalidates the case),
search payloads advisorily (question text is legitimate), and run artifacts
permissively (gold is required for judging).

#### Scenario: Ingest leak invalidates
- **WHEN** an ingestion payload contains gold answer text, an answer
  shingle, a category label, or an evidence-marked identifier
- **THEN** the case is INVALID and unscored, and the manifest records the
  detector that fired

### Requirement: Case Namespaces Are Isolated And Canary-Probed
Every case SHALL run in a fresh provider namespace recorded in its
artifacts, verified by deterministic canaries: a presence canary planted in
non-evidence content must be retrievable within the case, a foreign case's
canary must not be retrievable, and a never-ingested canary must not be
retrievable. Contamination invalidates the case; unverifiable isolation is
recorded and blocks cross-provider tables.

#### Scenario: Cross-case bleed detected
- **WHEN** a canary planted for one case is retrieved inside another case's
  namespace
- **THEN** the affected case is INVALID and the run records contamination

### Requirement: Readiness Fails Closed With Positive Verification
A run SHALL verify every requested retrieval lane (lexical, semantic,
reranker) by positive evidence — index counts, configuration state, or a
passing zero-lexical-overlap semantic probe — before scoring. Command exit
codes SHALL NOT count as readiness evidence. A requested-but-unverified lane
or a detected silent fallback renders the run INVALID. Where a provider's
default mode offers no completion signal for derived state, the run SHALL be
labelled readiness-unverifiable on every affected row rather than scored as
verified or discarded as invalid.

#### Scenario: Silent semantic fallback invalidates
- **WHEN** semantic retrieval was requested and the provider served
  keyword-only results without error
- **THEN** the run is INVALID with the fallback recorded

### Requirement: Manifests And Traces Make Reports Regenerable Offline
A machine-readable manifest SHALL be written before the first provider call
and finalized with a terminal validity status; per-case traces SHALL persist
normalized inputs, transmitted payload digests, queries, raw responses,
normalized results, packed context, prompts, model identities, judge
outputs, timings, token and cost accounting, and cleanup results. Report
generation SHALL read only stored artifacts, refuse non-terminal manifests
and unknown schema versions, and prove offline operation via a network
guard.

#### Scenario: Report from artifacts only
- **WHEN** a report is regenerated from a completed run directory with the
  network guard active
- **THEN** rendering succeeds with zero provider or network calls

### Requirement: Spend Is Reserved Before It Happens
Billable operations SHALL reserve an upper-bound estimate against a shared
ledger before the call; a reservation that would exceed the approved cap is
refused before any spend occurs, a stop sentinel halts all processes, an
unpriced model or operation refuses rather than estimating zero, founder
approvals are recorded in the ledger, and no run can raise its own cap.

#### Scenario: Cap refusal precedes the call
- **WHEN** a billable call's estimate would exceed the remaining approved
  budget
- **THEN** the call is refused before transmission and the ledger records
  the refusal
