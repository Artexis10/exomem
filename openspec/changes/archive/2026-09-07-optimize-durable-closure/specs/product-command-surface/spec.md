## ADDED Requirements

### Requirement: Selected media batches acknowledge durable work without draining projections
Media processing SHALL accept either one existing path, a bounded list of 1–32 unique canonical paths, or the existing bounded all-media selector. Conflicting selectors, duplicate canonical identities and invalid batch shapes SHALL be rejected before work. Selected paths SHALL apply to process/retry and SHALL be rejected for aggregate status. The batch SHALL preserve input order and report each artifact's outcome in the default compact terminal through a bounded validated projection of path, outcome/state, media type, sidecar path/job identity, retry count and actionable error code/remediation. Post-preflight artifact failures SHALL preserve item outcomes. Canonical commits SHALL remain ordered under existing per-item guards; durable extraction and index jobs SHALL converge under their background owners. Processing and retry SHALL NOT synchronously drain derived graph/index queues. Index-refresh remaining counts SHALL retain their documented aggregate scope.

#### Scenario: Three independent artifacts
- **WHEN** the caller submits one PDF and two image paths
- **THEN** one public call returns three bounded results after durable reconciliation/enqueue, without waiting for extraction or graph convergence

#### Scenario: Partial artifact failure
- **WHEN** one artifact fails while another has durably committed
- **THEN** the response names both outcomes without rolling back or duplicating the successful artifact

#### Scenario: Replay and failed-subset retry
- **WHEN** a caller repeats an identical batch with its original replay identity
- **THEN** it receives the same terminal without new work, while a retry of only failed paths uses a new request identity and does not reconcile successful paths

#### Scenario: Selected retry changes no canonical bytes
- **WHEN** a selected media request only requeues existing durable jobs or observes their existing state
- **THEN** it still returns the validated media result projection with request identity and exact replay support, using a terminal `settled` state and `mutated=false` rather than claiming a canonical commit

#### Scenario: Existing single-path client
- **WHEN** the caller supplies the original single path selector
- **THEN** existing single-artifact fields remain available and accurately report deferred work

### Requirement: Committed mutation identities support dependent work without redundant reads
The compact mutation terminal SHALL retain validated bounded exact byte hashes and semantic-unit references supplied by its committed producer. Existing-page semantic edits SHALL expose their committed byte hash without a post-commit file read. Absent proof, including portable receipt recovery, SHALL omit the field rather than invent a current identity. Client guidance SHALL reuse known context, combine supported edits and independent artifact work, and normally perform one bounded final verification rather than rereading every successful write.

#### Scenario: Consecutive guarded edits
- **WHEN** an edit returns its exact after-hash and another edit is based on that state
- **THEN** the caller can pass that hash to the next expected-hash guard without an intervening read, while a foreign edit still causes a stale-write refusal
