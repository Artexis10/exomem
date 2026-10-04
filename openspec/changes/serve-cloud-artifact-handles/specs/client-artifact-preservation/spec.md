## ADDED Requirements

### Requirement: Cloud file handles retain canonical preservation semantics

Cloud file-handle commands SHALL stage through the confined Cloud artifact transport before entering their existing mutation boundary. They SHALL retain destination validation, Sources/Evidence writers, hashing, ordered outcomes, idempotency and canonical receipts. Desktop direct retrieval and local held uploads MUST remain unchanged.

#### Scenario: Cloud preserves a client file
- **WHEN** a valid supplied handle is retrieved through the broker
- **THEN** the existing governed writer stores the bytes and returns the canonical path, reference, size and SHA-256
- **AND** a fresh read independently verifies the preserved artifact

#### Scenario: A mixed Cloud batch contains an invalid file
- **WHEN** one handle fails retrieval while another succeeds
- **THEN** each ordered outcome truthfully reports its own result
- **AND** the successful file retains the existing canonical receipt and the failed file has no final artifact
