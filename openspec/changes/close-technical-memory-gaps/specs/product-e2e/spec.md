## ADDED Requirements

### Requirement: Installed-wheel stdio product loop
The system SHALL provide a black-box test that builds and installs the wheel, initializes a temporary vault through the installed CLI, connects through a real stdio MCP client, and completes source capture, source-backed memory, recall, read, graph context, evidence preservation, supersession, evolution review, reconcile, restart, and persistence checks.

#### Scenario: Governed lifecycle survives restart
- **WHEN** the lean product E2E completes the governed lifecycle and restarts the stdio server
- **THEN** the active conclusion, preserved source/evidence links, supersession history, and stable references remain resolvable

### Requirement: HTTP lifecycle and timeout safety
The system SHALL exercise the actual HTTP application lifecycle, REST authentication, MCP initialization, a read, a write, and clean shutdown. Every transport test SHALL have a bounded timeout and MUST fail rather than hang.

#### Scenario: HTTP server starts and stops cleanly
- **WHEN** the HTTP E2E starts the server, performs authenticated operations, and requests shutdown
- **THEN** every request completes within its timeout and the server exits without a leaked lifespan task

### Requirement: Tiered model and media gates
Lean product E2E SHALL run without optional models on every pull request. Real embeddings/reranking SHALL run in the model job, and real OCR, PDF, ASR, CLIP, and video fixtures SHALL run scheduled or opt-in with explicit soft-fail reporting when their configured dependencies are unavailable.

#### Scenario: Lean CI remains deterministic
- **WHEN** optional model and media extras are absent in the pull-request job
- **THEN** the lean E2E still proves the complete text/governance lifecycle and reports optional lanes as unavailable rather than failing implicitly
