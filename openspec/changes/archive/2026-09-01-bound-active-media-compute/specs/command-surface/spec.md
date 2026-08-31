## ADDED Requirements

### Requirement: Resource diagnostics expose the active compute envelope
No-allocation resource status and doctor output SHALL report the effective native CPU budget, synchronous request-worker budget, bounded model-admission posture, whether each came from a default or override, any unsafe native-thread escape hatch, the background scheduling posture, and the configured ASR device/computation policy. Collecting these fields MUST NOT import model stacks, initialize an accelerator, or start a media worker.

#### Scenario: Idle status on a CPU-only host
- **WHEN** resource status is requested before a media job has run
- **THEN** it reports the bounded CPU posture and unresolved or CPU ASR policy
- **AND** no model or accelerator state is created

#### Scenario: Accelerator job is blocked
- **WHEN** a durable media job is blocked by a compute-runtime failure
- **THEN** status reports the failure class and bounded remediation
- **AND** it does not present source-file repair as the next action
