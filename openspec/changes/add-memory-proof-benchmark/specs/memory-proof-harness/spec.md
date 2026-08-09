## ADDED Requirements

### Requirement: Capability-Declaring Adapter Contract
Provider adapters SHALL implement a shared contract (setup, schedule-op
application, search, bounded context assembly, optional state export, cleanup,
version info) and SHALL declare their supported capabilities explicitly. A
capability a provider does not support SHALL be reported as unsupported and
MUST NOT be emulated, faked, or scored as zero. The Exomem adapter SHALL also
conform to the external `basic_memory_benchmarks` provider contract through a
bridge so one adapter core serves both harnesses.

#### Scenario: Unsupported capability reported
- **WHEN** a scorer needs a capability the provider did not declare
- **THEN** the affected metrics are recorded as unsupported and excluded from
  comparative tables rather than scored as zero

#### Scenario: Bridge conformance
- **WHEN** the Track-A bridge wraps the Exomem adapter
- **THEN** it satisfies the external harness's provider interface, including
  skip semantics when the exomem executable is absent

### Requirement: Isolated Deterministic Provider Execution
Every provider run SHALL execute against benchmark-owned disposable state
(temp vaults, homes, config, databases) and MUST NOT read or mutate a real
user vault or live product configuration. The Exomem adapter SHALL pin
determinism knobs (ranking config, warmup, caches, usage boost, backend
selection) and SHALL fail the run if a scored response reports warming or
degraded retrieval lanes. Diagnostic logs SHALL be written outside the
disposable state so evidence survives cleanup.

#### Scenario: Real vault cannot be touched
- **WHEN** an adapter starts without an explicitly provided benchmark vault
  path
- **THEN** it refuses to run rather than resolving any default vault

#### Scenario: Warming response invalidates scoring
- **WHEN** a scored retrieval response carries a warming or degraded marker
- **THEN** the run records a harness-environment failure for that phase
  instead of scoring the response

### Requirement: Immutable Run Artifacts With Visible Failures
Every run SHALL write a self-contained run directory (manifest, environment,
corpus manifest, ingest/retrieval/answers streams, deterministic and judge
scores, traces, failures, report) whose creation fails rather than
overwriting an existing directory. Failures, retries, timeouts, and
exclusions SHALL be recorded and SHALL remain in every denominator; a
harness, setup, adapter, or environment failure SHALL mark the run invalid
rather than counting as a contender loss. The manifest SHALL pin product
commits and dirty flags, adapter and model identifiers, prompts, profiles,
seeds, budgets, and timing.

#### Scenario: Run directory collision
- **WHEN** a run attempts to create a run directory that already exists
- **THEN** the run aborts without modifying the existing directory

#### Scenario: Failure stays in the denominator
- **WHEN** a query fails during answering
- **THEN** it is recorded in the failures stream and the reported metrics
  count it against the total rather than dropping it

### Requirement: Deterministic Gates Are Final
Deterministic scorers SHALL evaluate exactness, temporal/as-of correctness,
current-versus-superseded state, required and forbidden claims, citation
identity with transitive provenance, governance leaks and redaction,
abstention/clarification, graph facts, retrieval ranking, corpus health, and
behavioural checks. A judge or answer-normalization step MUST NOT override a
deterministic policy-leak, wrong-date, missing-mandatory-citation,
wrong-current-state, missed-required-abstention, or forbidden-disclosure
verdict; answer normalization MAY only add structure to an answer record,
never change its gate-relevant fields.

#### Scenario: Judge disagreement cannot flip a gate
- **WHEN** a judge rates an answer favourably but a deterministic gate failed
  for that answer
- **THEN** the reported result keeps the failed gate and marks the judge
  disagreement as a conflict annotation

### Requirement: Blind Optional Judge With Recorded Variance
Model-backed answering and judging SHALL be optional desk-side backends,
default off, with a deterministic extractive answerer as the model-free
default; the runner SHALL never require model credentials (subagent backends
exchange request/response files). Judge inputs SHALL be blinded to provider
identity, including normalization of provider-identifying reference styles;
presentation order SHALL be deterministically randomized; repeated samples
SHALL be preserved individually and reported with variance.

#### Scenario: Blinding holds in serialized requests
- **WHEN** judge request files are written
- **THEN** they contain no provider names, provider-specific URI schemes, or
  vault path shapes

#### Scenario: Default run needs no model
- **WHEN** a run executes with default settings
- **THEN** no model backend is invoked and answers come from the
  deterministic extractive answerer

### Requirement: Per-Dimension Reporting Without Aggregate
Reports SHALL present each scoring dimension separately with its gate
outcomes, profile label, and denominators, and SHALL NOT compute a weighted
aggregate score. Latency, token, and cost figures SHALL be reported separately
from correctness. Comparative tables SHALL include only metrics measurable on
every listed provider; provider-introspective metrics SHALL be labeled as
non-comparative.

#### Scenario: No aggregate emitted
- **WHEN** a report is generated
- **THEN** it contains per-dimension results and no combined weighted score

### Requirement: Harness Activation Measurement With Dual Witnesses
Track C SHALL measure memory activation against predeclared expectations
using a frozen control-prompt suite (including substantive prompts that must
fire, control prompts that must not, non-English substantive prompts, and
hard negatives), with per-case isolated hook homes. Activation ground truth
SHALL join two independent witnesses — server-side call traces and
client-side transcripts or hook output — and a witness disagreement SHALL
fail the case as a harness fault. Simulated activation (subagent-driven)
SHALL be labeled simulation and MUST NOT be merged with organic-session
results.

#### Scenario: Control prompt stays quiet
- **WHEN** a frozen control prompt that must not trigger memory runs under
  the nudge hook in an isolated home
- **THEN** the hook emits no retrieval context and the case scores a correct
  non-activation

#### Scenario: Witness mismatch
- **WHEN** the client transcript claims a memory tool call that the server
  trace does not contain
- **THEN** the case is recorded as a harness fault, not as product behaviour

### Requirement: Continuity Round-Trip Verification
The harness SHALL verify pre-compaction, session-restart, and client-switch
continuity by planting structural markers, driving the shipped checkpoint
hooks with synthetic lifecycle events in isolated homes, and scoring restored
context by planted-marker recall within the checkpoint size bounds.

#### Scenario: Cross-client restore
- **WHEN** a checkpoint is created under one client's isolated home and a
  session-start event fires under another client sharing the same hook home
- **THEN** the restored context contains the planted markers

### Requirement: Workflow Journey Evaluation
Track D SHALL express knowledge-work journeys as product-neutral event
streams with predeclared expectations, executed through per-product adapter
mappings that are published for review; deterministic checks SHALL cover
current-state correctness, correction propagation, duplicate and stale
growth, planted-queue recall/precision, connection precision/recall against
predeclared hidden links with decoys, provenance retention, and step counts,
and any judged aspect SHALL use a predeclared rubric under the blind judge
contract.

#### Scenario: Hidden connection scoring
- **WHEN** a journey seeds hidden valid links and decoy near-links
- **THEN** the report scores suggestion precision and recall against the
  predeclared sets and counts a suggested decoy against precision

### Requirement: External Benchmark Result Sanity
Track A execution SHALL include a result sanity gate: a provider returning
zero hits for more than half of the queries without declaring a skip SHALL
fail artifact validation and demand diagnosis, and every run SHALL include a
canary query whose answer text appears verbatim in exactly one corpus
document and which every non-skipped provider must hit.

#### Scenario: Silent zero-hit run rejected
- **WHEN** a provider completes a run with empty hits for most queries and no
  skip reason
- **THEN** artifact validation fails the run instead of publishing zero
  scores
