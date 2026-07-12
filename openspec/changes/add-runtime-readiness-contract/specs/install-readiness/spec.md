## MODIFIED Requirements

### Requirement: Read-Only Doctor Command
The system SHALL provide a CLI-only `doctor` admin command that checks installation readiness without mutating the repo, vault, environment, service state, model caches, or remote replicas. It SHALL support `--profile lean|hybrid|standard|media|remote|ha`, `--vault PATH`, `--json`, explicit `--probe`, and repeatable `--replica-url URL`. Documentation SHALL point users to the matching profile before wiring a client, optional capability, or HA failover route.

#### Scenario: Lean doctor over a valid vault
- **WHEN** `python -m exomem doctor --vault <valid-vault> --json` is run
- **THEN** it returns JSON containing `success`, `profile`, and a `checks` list
- **AND** each check contains `id`, `status`, `message`, and `remediation`
- **AND** no vault file is created, modified, moved, or deleted

#### Scenario: Missing required lean setup
- **WHEN** `doctor` cannot resolve a vault containing `Knowledge Base/_Schema/SKILL.md`
- **THEN** it exits non-zero
- **AND** it reports a remediation that tells the user to set `EXOMEM_VAULT_PATH` or pass `--vault` and run `init` if needed

#### Scenario: HA doctor stays offline by default
- **WHEN** `doctor --profile ha` runs without `--probe`
- **THEN** it validates local HA configuration without making network calls

### Requirement: Profile-Specific Readiness
The doctor command SHALL validate the requested capability profile. `lean` SHALL check Python/package/vault/registry basics. `hybrid` SHALL additionally check embeddings dependencies and embedding sidecar state. `standard` SHALL additionally check the normal optional media stack with soft degradation. `media` SHALL require the media extraction dependencies and Tesseract discovery. `remote` SHALL additionally check public URL and OAuth-related environment variables. `ha` SHALL additionally check local writer-coordination configuration and, only with `--probe`, compare explicit replica runtime-readiness endpoints.

#### Scenario: Optional capability profile is requested
- **WHEN** `doctor --profile media` is run without media extraction dependencies
- **THEN** the report marks the missing media components as failures
- **AND** the remediation names `uv sync --extra media` and any required system tool such as Tesseract

#### Scenario: Compatible HA releases differ
- **WHEN** HA replica probes report different releases with the same supported runtime contract, stateless transport, unique replica identities, healthy coordination, and takeover eligibility
- **THEN** doctor reports compatibility as passing
- **AND** reports release drift as a warning rather than a failure

#### Scenario: HA runtime is incompatible
- **WHEN** a replica probe reports an unsupported runtime contract, stateful transport, duplicate identity, unhealthy coordination, or takeover ineligibility
- **THEN** doctor fails with remediation to upgrade or repair that replica before enabling failover
