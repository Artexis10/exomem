## ADDED Requirements

### Requirement: Hosted RAW upload refusal extends the transfer error catalog

A hosted cell without an owner binding SHALL refuse RAW-marked preservation before any canonical write or duplicate receipt. Both hosted upload routes SHALL bind the principal verified by their transfer authority before preservation.

The public transfer-v2 error catalog SHALL retain every entry and envelope field in the archived normative contract. RAW refusal adds exactly one error: `RAW_PROTECTION_UNAVAILABLE`, HTTP 400, with the canonical RAW unavailability message and `retryable=false`. After consuming the transfer grant, this error SHALL set `requires_new_grant=true`. The error SHALL contain no target, user metadata, content or credential. Ordinary uploads, imported marked pages and adoption staging SHALL retain their existing behavior.

#### Scenario: A public upload requests RAW protection through its filename
- **WHEN** a resolved hosted caller submits a valid transfer-v2 upload with a RAW-marked preservation filename
- **THEN** the cell returns `RAW_PROTECTION_UNAVAILABLE` in the existing error envelope, without writing an original or companion
- **AND** the response is not retryable and requires a new grant after consuming the upload grant

#### Scenario: A private upload requests RAW protection through its filename
- **WHEN** a resolved hosted caller submits a valid private-v1 upload with a RAW-marked preservation filename
- **THEN** the cell returns its existing governed error envelope with `RAW_PROTECTION_UNAVAILABLE`, without writing an original or companion

#### Scenario: Legacy transfer errors retain their contract
- **WHEN** a transfer encounters a condition modeled by the archived normative error catalog
- **THEN** its status, code, message, retryability, grant requirement and envelope remain unchanged

#### Scenario: A protected duplicate does not claim hosted protection
- **WHEN** a resolved hosted caller requests a protected capture whose bytes already exist
- **THEN** the cell refuses with `RAW_PROTECTION_UNAVAILABLE` before returning a preservation or adoption receipt
