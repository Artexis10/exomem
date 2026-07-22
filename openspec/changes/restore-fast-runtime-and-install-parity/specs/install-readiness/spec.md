## ADDED Requirements

### Requirement: Managed CLI And Service Provenance Stay Coherent

Managed service upgrade SHALL record a non-secret manifest after verifying the live release and reconcile an existing uv-managed `exomem`/`kb` command with that exact release. Version reporting SHALL compare CLI distribution/interpreter provenance, service release, selected profiles, and the declared execution route without importing optional model stacks.

#### Scenario: Upgrade finds a stale uv-tool command

- **WHEN** the managed service is upgraded and an `exomem` command on PATH resolves to an older uv-tool distribution
- **THEN** upgrade reconciles that command to the requested release or fails before reporting success with an exact remediation command
- **AND** post-upgrade verification reports matching release provenance

#### Scenario: Intentional lean CLI accompanies a full service

- **WHEN** the uv-tool CLI is lean and the managed service uses a standard or media profile
- **THEN** release reconciliation upgrades only the Exomem distribution in the CLI
- **AND** it does not install Torch, sentence-transformers, or media extras into that CLI environment

#### Scenario: Historical find command survives alignment

- **WHEN** a release-aligned user runs `exomem find <query>`
- **THEN** it uses the current compact `ask` behavior
- **AND** it does not fall through to the server argument parser or the obsolete 0.4.1 fallback path

#### Scenario: Lean find skips capabilities it does not own

- **WHEN** a release-aligned lean CLI runs `exomem find <query>` without Torch or sentence-transformers
- **THEN** it uses the maintained lexical lanes directly
- **AND** it does not emit raw missing-module or missing-model warnings

### Requirement: Upgrade Verifies The User-Facing Command

The managed upgrade SHALL run the resolved post-upgrade `exomem --version --json` and a model-free command-surface smoke from the same shell resolution a user receives. Service health alone MUST NOT be sufficient to declare the installation upgraded.

#### Scenario: Service updated but PATH command remained stale

- **WHEN** the service reports the target release but the shell resolves an older executable
- **THEN** upgrade fails its verification phase and identifies both executable paths and releases without exposing secrets
