## ADDED Requirements

### Requirement: Supported Media Events Dispatch Without Entering Text Freshness
The live watcher SHALL separately debounce supported media create/modify events under the governed Knowledge Base and dispatch them to canonical media reconciliation. Binary paths MUST NOT be passed to Markdown embedding upsert/delete or included in Markdown freshness and inbound-link registries.

#### Scenario: Audio event dispatches media only
- **WHEN** the watcher observes a new `.m4a` under the governed Knowledge Base
- **THEN** it dispatches targeted media reconciliation after the debounce window
- **AND** it does not pass the binary path to text embedding or Markdown freshness handlers

#### Scenario: Unsupported attachment remains ignored
- **WHEN** the watcher observes a non-Markdown attachment outside the supported media registry
- **THEN** neither media processing nor text-index dispatch occurs

### Requirement: Periodic Reconciliation Heals Media Event Drift
The existing periodic reconciliation loop SHALL run a bounded supported-media discovery pass independent of change events. The pass SHALL be idempotent and MUST NOT perform a full text-index rebuild solely because supported media exists.

#### Scenario: Periodic pass finds an unobserved recording
- **WHEN** a supported recording exists without canonical completed or pending state and no watcher event was observed
- **THEN** the next periodic pass creates or repairs the sidecar and durably enqueues processing
- **AND** unrelated Markdown files are not re-embedded
