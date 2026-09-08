## ADDED Requirements

### Requirement: Controlled And Native Modes Are Separated With Declared Asymmetries
Native-lifecycle evaluation SHALL run controlled-substrate and
native-best-practice modes as separate, never-merged rows. Controlled mode
equalizes every knob the products allow and REPORTS every knob they do not
(embedding families, fusion formulas, unavoidable extraction). Native mode
drives each product with its own shipped skills, plugins, or documented
recipes verbatim; the only harness-authored text is a product-neutral task
prompt whose token count is capped equally across products. Structural
asymmetries (a product that cannot ingest without extraction; a product
whose agent interface differs locally) SHALL render as row-level
declarations in both directions, never footnotes.

#### Scenario: Unreported asymmetry is a defect
- **WHEN** a controlled-mode row is produced and a known non-equalizable
  knob is absent from the fairness matrix entry
- **THEN** the row is not publishable until the asymmetry is recorded

### Requirement: Write Agents Are Future-Blind And Budgeted
The write agent SHALL be one model configuration for all products, receive a
scrubbed environment containing only phase-visible sources with no path to
probe material, and operate under an explicit envelope (model calls, tokens,
provider operations, wall-clock readiness, stored bytes, currency). A static
test SHALL prove evaluator-only fields and harness-injected probe material do
not cross the write-agent boundary. N-gram overlap in legitimate phase-visible
source material SHALL be preserved and reported for inspection rather than
treated as proof of leakage. Envelope exhaustion is a declared outcome, never
a crash. Parent-enforced run-wide limits SHALL survive fresh worker sessions.

#### Scenario: Probe text reaches the write agent
- **WHEN** an evaluator-only question, gold label, answer or harness-injected
  probe material crosses the write-agent boundary
- **THEN** the run is INVALID with the leak recorded

#### Scenario: Historical dialogue legitimately overlaps a later question
- **WHEN** phase-visible original source text overlaps a held-out probe
- **THEN** the original source bytes are retained and the overlap is reported
- **AND** no source turn is dropped or rewritten to improve leakage checks

### Requirement: Fresh Answer Agents Declare Their Own Basis
Answer probes SHALL run in a fresh process with no prior conversation
context, interacting only through the product's documented agent interface;
citations and abstentions are taken solely from the agent's own declared
basis, never harvested from output prose by the harness.

#### Scenario: Harness-authored citation is rejected
- **WHEN** a scoring path attempts to derive citations from retrieved-hit
  overlap rather than the agent's declared basis
- **THEN** the result is marked unsupported rather than pass or fail

### Requirement: Native Agent Profiles Are Economical And Reproducible
Native diagnostics SHALL accept explicitly verified economical agent models,
including GLM-5.3-Flash through OpenRouter, without changing the official judge.
New GLM-5.3-Flash preparations SHALL use high reasoning for writing and answering;
historical low-effort artifacts SHALL retain their original frozen profile.
Each preparation SHALL bind model settings, provider routing, regular-price
reservation rates and tokenizer identity. A non-OpenAI tokenizer SHALL be
supplied locally, verified against an immutable official digest and frozen with
the prepared inputs. The broker and transport SHALL use that model's tokenizer
for context bounds. No tokenizer or price fallback may silently substitute an
OpenAI profile. Compared products SHALL use the same chosen agent configuration;
model selection SHALL use independent workflow acceptance rather than held-out
evaluation answers. Existing historical results retain their original profiles.
The request SHALL pin the permanent release where available. A verified gateway
response alias SHALL be explicitly frozen; other model identities are refused.

#### Scenario: Tokenizer substitution is refused before spending
- **WHEN** an economical-model tokenizer is absent or differs from its frozen digest
- **THEN** preparation or execution refuses before a model request

#### Scenario: Agent and judge use distinct pinned providers
- **WHEN** a GLM native agent is followed by the fixed OpenAI judge
- **THEN** each call uses its own frozen provider and rates in one shared ledger
- **AND** a mismatched response provider stops subsequent calls after accounting
  for any known charge

### Requirement: Native Canonical Cohort Selection Is Explicit And Frozen
The native LongMemEval diagnostic SHALL default to a fresh seeded selection
that excludes the prior-inspected canonical 25-case cohort. An explicit
`canonical25` mode SHALL require size 25 and the exact pinned official
LongMemEval-S source, regenerate the canonical selection from the complete
source census with the shared selector, and preserve the frozen artifact's
exact membership, order, and full source histories. Its plan SHALL label the
cohort as prior-inspected rather than a fresh holdout and freeze the selection
artifact bytes and digest, algorithm and version, source identity, and census.
Execution SHALL validate those values against the evaluator and ordered case
plan before constructing a metered backend.

#### Scenario: Canonical plan drift refuses before spend
- **WHEN** a canonical native plan names the wrong source, changes cohort
  membership or order, or carries altered selection bytes or metadata
- **THEN** execution refuses before backend construction or provider spend

### Requirement: Competitor Extraction Cost Is Metered Symmetrically
Where a product performs server-side model work during ingestion or
maintenance, its model endpoint SHALL be routed through a metering proxy so
its tokens land in the same budget envelope as harness-side write-agent
tokens; a native-mode cost comparison without symmetric metering is not
publishable.

#### Scenario: Unmetered extraction blocks cost claims
- **WHEN** a native-mode cost row is rendered for a product whose extraction
  tokens were not metered
- **THEN** the cost cell renders as unmetered and no cross-product cost
  claim includes it
