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
test SHALL assert the assembled write prompt shares no significant n-gram
with any probe. Envelope exhaustion is a declared outcome, never a crash.

#### Scenario: Probe text reaches the write agent
- **WHEN** the assembled write-agent input shares a significant n-gram with
  any held-out probe
- **THEN** the run is INVALID with the leak recorded

### Requirement: Fresh Answer Agents Declare Their Own Basis
Answer probes SHALL run in a fresh process with no prior conversation
context, interacting only through the product's documented agent interface;
citations and abstentions are taken solely from the agent's own declared
basis, never harvested from output prose by the harness.

#### Scenario: Harness-authored citation is rejected
- **WHEN** a scoring path attempts to derive citations from retrieved-hit
  overlap rather than the agent's declared basis
- **THEN** the result is marked unsupported rather than pass or fail

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
