# hosted-secret-custody Specification

## Purpose
Bind hosted secret delivery to recoverable, explicitly identified source entries while preserving existing governed destination and rotation controls.

## Requirements

### Requirement: Matrix-owned BWS source

The hosted handoff SHALL support an explicit BWS source with a repository-owned binding file and one named binding. It SHALL retrieve through the shared identity/shape validator without exposing plaintext in arguments or diagnostics. Binding paths SHALL remain inside the repository's infrastructure contracts. A failed source SHALL not fall back or mutate a destination.

#### Scenario: BWS is unavailable or rejects the entry
- **WHEN** the selected BWS source fails, times out or returns a rejected value
- **THEN** the handoff emits a content-free failure and creates no receipt or destination write

#### Scenario: Dry-run checks a BWS-backed route
- **WHEN** the operator requests a dry-run
- **THEN** matrix and destination checks run without contacting BWS

### Requirement: Explicit first-adopter coverage

The production control-plane wrapping key SHALL have a recorded exact BWS project, entry ID, expected key and base64url-32 format. Its normal runbook handoff SHALL use that binding. This source check SHALL not be represented as proof of authenticated decryption or migration of unrelated credentials.

#### Scenario: Existing key is delivered again
- **WHEN** the operator chooses the documented BWS-backed control-key route
- **THEN** the existing bound value is used without generating or rotating a key
