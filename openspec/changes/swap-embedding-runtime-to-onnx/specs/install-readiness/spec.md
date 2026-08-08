## ADDED Requirements

### Requirement: Readiness reports the embedding backend actually in use

The doctor's embeddings check SHALL determine health from the backend that is configured
to serve, not from a fixed dependency list. An installation whose embedding backend loads
and encodes SHALL be reported ready even when a framework used by a different backend is
absent, and its remediation SHALL name the extra that installs the configured backend.

Accelerator reporting SHALL be scoped to backends that can use one. A backend-specific
accelerator probe SHALL NOT report a failure or a missing device for an installation that
does not use that backend.

#### Scenario: Hosted image is checked without torch present

- **WHEN** `doctor --profile hybrid` runs on an image whose embedding backend is ONNX Runtime and which carries no torch
- **THEN** the embeddings check passes on the strength of the configured backend loading and encoding
- **AND** no failure is reported for the absent framework

#### Scenario: Configured backend is missing

- **WHEN** the configured embedding backend cannot be imported or cannot load the model
- **THEN** the embeddings check fails
- **AND** the remediation names the extra that installs that specific backend
