## ADDED Requirements

### Requirement: Hosted gateway preserves adoption truth and request identity
Generic Hosted forwarding of `capture_source` and `preserve_artifacts` SHALL preserve normal request idempotency scope, semantic lane, and adoption-envelope fields. A gateway handoff response SHALL be explicitly non-committing and MUST NOT synthesize Source capture, Evidence preservation, or adoption success before an authenticated cell-side exact-byte receipt returns. Delivery-event forwarding SHALL remain an ordinary `record_memory` operation and MUST NOT act as an upload-success proxy.

#### Scenario: Generic forwarding preserves identities
- **WHEN** an authenticated Hosted request forwards selected-artifact adoption through either direct-handle lane
- **THEN** the cell receives the command-selected lane, scoped request idempotency identity, and validated adoption envelope without semantic rewriting

#### Scenario: Prepared handoff is non-committing
- **WHEN** the gateway prepares a file-transfer handoff but no cell-side byte commit has completed
- **THEN** it reports a handoff state with `committed=false` and no saved or adopted projection

#### Scenario: Cell receipt authorizes success
- **WHEN** an authenticated cell returns a complete committed exact-byte receipt
- **THEN** the gateway may project adoption success using only the receipt's stored identity fields

#### Scenario: Delivery forwarding cannot stand in for upload
- **WHEN** a delivery Record is forwarded
- **THEN** the gateway does not treat that Record as proof that artifact bytes were transferred or preserved
