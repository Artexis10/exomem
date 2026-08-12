## MODIFIED Requirements

### Requirement: Installed-wheel stdio product loop
The system SHALL provide a black-box test that builds and installs the wheel, initializes a temporary vault through the installed CLI, connects through a real stdio MCP client, and completes source capture, source-backed memory, recall, read, graph context, evidence preservation, supersession, evolution review, reconcile, Records discovery/query/guarded mutation/direct-edit observation, restart, and persistence checks.

#### Scenario: Governed lifecycle survives restart
- **WHEN** the lean product E2E completes the governed lifecycle and restarts the stdio server
- **THEN** the active conclusion, preserved source/evidence links, supersession history, and stable references remain resolvable

#### Scenario: Human-owned Records lifecycle survives restart
- **WHEN** the lean product E2E manually inserts a template block, performs guarded Records append/update calls, edits the canonical log while stopped, and restarts the stdio server
- **THEN** the installed `record_memory` surface returns the manual and guarded state, observes the later edit, reports its audit gap, preserves the Planning descriptor, and does not expose raw rows through ordinary recall

### Requirement: HTTP lifecycle and timeout safety
The system SHALL exercise the actual HTTP application lifecycle, REST authentication, MCP initialization, a read, a write, auth-required remote Records refusal, and clean shutdown. Every transport test SHALL have a bounded timeout and MUST fail rather than hang. The existing no-auth local HTTP harness SHALL remain explicit owner mode and SHALL NOT be reclassified by fabricated authorization headers.

#### Scenario: HTTP server starts and stops cleanly
- **WHEN** the HTTP E2E starts the server, performs authenticated operations, and requests shutdown
- **THEN** every request completes within its timeout and the server exits without a leaked lifespan task

#### Scenario: Unauthenticated Records call is rejected at remote ingress
- **WHEN** installed `python -m exomem --transport http` starts with isolated temporary OAuth anchors and receives a protocol-valid unauthenticated raw `POST /mcp` JSON-RPC `tools/call` payload naming `record_memory`
- **THEN** it returns exactly 401 with a Bearer challenge naming the local protected-resource metadata URL, its raw response discloses no collection path, row, Planning reference, or aggregate value, and the E2E does not accept another HTTP error or claim command-level governance executed
