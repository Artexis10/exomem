## ADDED Requirements

### Requirement: Managed installation uses copied qualification

A managed installation SHALL use a rehearsal on an isolated copy of the serving vault and state. The target SHALL become ready in less than 120000 milliseconds after the previous worker stops. Readiness SHALL remain ready for 45 minutes after the handoff completes and the target is ready. During each full lexical repair in that window, one MCP read and one MCP write SHALL succeed without caller `MUTATION_BUSY` or `VAULT_LOCK_TIMEOUT`.

#### Scenario: Handoff or availability check fails

- **WHEN** the handoff fails its limit or a readiness observation fails inside the fixed window
- **THEN** installation remains held and the report retains the failure

#### Scenario: A caller cannot read or write during a full lexical repair

- **WHEN** a full lexical repair has no successful read and write during its duration, or a caller receives either refusal
- **THEN** installation remains held even when later readiness is healthy

### Requirement: Copied qualification proves recovery

A declined full repair SHALL be eligible for recovery through existing bounded retries and catch-up. Before the fixed window ends, the target SHALL publish a replacement and restore authoritative current-catalogue admission. Publication and admission need not occur in one step. The final in-window health observation SHALL show retrieval ready and admitted, repair idle, and no observed target recovery left unresolved. Failed or unproved recovery SHALL hold installation.

#### Scenario: A declined pass recovers

- **WHEN** a full repair declines, a later retry publishes, and catch-up restores authoritative admission inside the fixed window
- **THEN** the recovery check passes when final in-window readiness proves that recovered state
- **AND** the original declined attempt remains in the report

#### Scenario: Publication has no admission proof

- **WHEN** a replacement publishes but authoritative current-catalogue admission is not proved before the window ends
- **THEN** installation remains held even when the repair reports idle

### Requirement: Qualification preserves evidence and scope

Qualification SHALL preserve every decline, error, unmatched event, unknown attribution and earlier verdict. A revised criterion SHALL receive a separate assessment. Retained evidence SHALL be reused only while its relevant artifact, runtime, workload and conditions match. Final-state proof SHALL NOT be reported as complete event pairing or an empty internal queue.

#### Scenario: An event has unknown ownership

- **WHEN** a repair event cannot be assigned to a worker or paired with a finish
- **THEN** its ownership or completion remains unknown
- **AND** independently proved target recovery is assessed separately

#### Scenario: A criterion changes after a recorded run

- **WHEN** an authorized criterion changes and the retained run still matches its relevant conditions
- **THEN** a separate assessment names the revised criterion and its supporting evidence
- **AND** the earlier report and its verdict remain unchanged

#### Scenario: Relevant conditions changed

- **WHEN** the artifact, runtime, workload or a condition that supports the proof changes
- **THEN** the affected acceptance proof is refreshed before installation
