## ADDED Requirements

### Requirement: Cheap Version And Install Provenance Surface

The `exomem` and `kb` console scripts SHALL accept `--version` and `--version --json` before loading command leaves or optional ML/media dependencies. The JSON result SHALL report distribution version, Python executable, install source, selected local profile, managed-service release/profile when configured, and effective CLI route. It MUST NOT include credentials, vault paths, user-authored content, or model initialization.

#### Scenario: Lean environment reports version without ML imports

- **WHEN** `exomem --version --json` runs in a lean environment without torch or sentence-transformers
- **THEN** it exits successfully with provenance JSON
- **AND** it does not import, initialize, or warn about optional ML capabilities

#### Scenario: Managed service differs from CLI

- **WHEN** the managed-install manifest names a service release different from the current CLI distribution
- **THEN** version JSON reports the mismatch explicitly and identifies the effective execution route
