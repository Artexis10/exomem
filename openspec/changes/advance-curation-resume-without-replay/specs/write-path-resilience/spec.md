## MODIFIED Requirements

### Requirement: Implicit Acknowledgement Recovery Retains Results For 600 Seconds

An identical mutation from the same stable authenticated principal, operation, vault identity, and canonical payload SHALL resolve the original per-replica completed receipt for 600 seconds without rerunning the leaf. Explicit idempotency keys retain their separate 24-hour contract. Pending and committed-uncertain receipts remain fail-closed. Two calls whose reply reports live progress are exempt. A curation `resume` SHALL replay only a retry that carries its own request id, so each new `resume` call runs the run's next step; its run store commits each step once. A maintenance job start SHALL run every time, because a later poll reads its handle.

#### Scenario: Human retries after investigating an abandoned request
- **WHEN** the same principal repeats a byte-identical mutation within 600 seconds of its completed receipt
- **THEN** the server returns the original terminal result without executing the leaf again
- **AND** the original path and request identity are preserved

#### Scenario: Implicit window or replica boundary is crossed
- **WHEN** the retry occurs after 600 seconds or reaches a replica without the original local receipt
- **THEN** the request is outside the implicit exactly-once guarantee
- **AND** the system does not infer idempotency from a similar title or silently claim cross-replica replay safety

#### Scenario: An agent advances a curation run step by step
- **WHEN** the same principal sends an identical curation `resume` within 600 seconds of the previous one
- **THEN** the server runs the run's next step
- **AND** the reply lists every step the run has committed
