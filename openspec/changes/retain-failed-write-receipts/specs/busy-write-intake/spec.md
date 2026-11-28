## MODIFIED Requirements

### Requirement: A resend of a queued save runs nothing

While a save is queued or being applied, a resend with the same identity and payload SHALL return its receipt without running. For a retained failure within the original retry window, this also applies when the store or loaded index can identify it. The same identity with a different payload SHALL be a different save.

#### Scenario: A resend while queued runs nothing

- **WHEN** a save is queued
- **AND** the caller resends the same call with the same idempotency identity
- **THEN** the reply is the same queued receipt
- **AND** only one commit results

#### Scenario: A reused identity with a different payload is a different save

- **WHEN** a save is queued
- **AND** the caller sends a different payload with the same idempotency key
- **THEN** both saves are applied as separate commits

#### Scenario: A resend after an unresolved effect keeps the same failure receipt

- **WHEN** a queued save fails with `MUTATION_OUTCOME_UNKNOWN`
- **AND** the service restarts before the caller resends the same call with the same identity within its retry window
- **THEN** the reply names the original failed receipt and reports that its commit effect is unknown
- **AND** the service does not execute a new write

#### Scenario: An older release created two receipts

- **WHEN** more than one retained terminal receipt matches the same resend
- **THEN** the service returns the latest matching receipt still within the retry window
- **AND** when the owner dismisses that receipt, a resend can find the surviving receipt before and after restart

### Requirement: The next save names failed queued saves

When queued saves have failed and no reply has named them, the next successful reply to an owner's eligible save SHALL keep its own outcome first. It SHALL give the failure count, including the `queued_writes_failed` field, and the surface's receipt lookup. It SHALL direct the caller to check the recorded outcomes before resending. Each failure SHALL be named once across restarts. Replays, reads, and other callers' replies SHALL neither carry nor take a notice.

#### Scenario: A failure is named on the next save, once

- **WHEN** a queued edit fails because its page changed
- **AND** the owner then saves a note
- **THEN** the reply starts with the save's own outcome and says that one earlier queued save failed
- **AND** the owner's following save carries no notice
- **AND** a save queued behind the failed edit, a read, or a restart does not take or repeat the notice
