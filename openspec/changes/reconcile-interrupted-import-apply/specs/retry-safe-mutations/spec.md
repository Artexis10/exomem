## ADDED Requirements

### Requirement: Interrupted import applies reconcile under their original identity

The service SHALL admit reconciliation only for an exact interrupted import apply identity. It SHALL preserve vault, verified-client and principal isolation, reject changed command arguments and serialize recovery under the normal writer guard. A complete terminal SHALL replay normally. Partial import custody SHALL NOT become a whole-call commit receipt. Unknown outcomes for other mutations SHALL remain blocked.

#### Scenario: An exact import retry meets a retained unknown outcome

- **WHEN** a proven-abandoned import apply has a retained unknown outcome and the same caller repeats the exact mutation
- **THEN** one owned attempt reconciles its durable import checkpoint and returns truthful progress or an explicit unresolved boundary
- **AND** concurrent retries cannot import the same item twice

#### Scenario: A caller changes the command under an explicit key

- **WHEN** an import caller reuses an explicit key with changed canonical apply arguments
- **THEN** the service rejects the mismatch without importing another item

#### Scenario: An unrelated mutation has an unknown outcome

- **WHEN** a non-import mutation with an unknown outcome receives an identical retry
- **THEN** the existing fail-closed outcome remains unchanged
