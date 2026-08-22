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

#### Scenario: Doctor defaults to fully offline

- **WHEN** `doctor` is run for any profile without `--probe`
- **THEN** it performs zero network requests
- **AND** its behavior and output are unchanged from before `--probe` existed

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

#### Scenario: Models are already cached locally

- **WHEN** `doctor --profile hybrid` is run and the embedding model and reranker are already
  present in the local Hugging Face hub cache
- **THEN** the `models.cache` check reports a passing status
- **AND** no network request is made and no model is downloaded as part of the check

#### Scenario: Models are not yet cached locally

- **WHEN** `doctor --profile hybrid` or `doctor --profile media` is run and one or more of the
  embedding model, reranker, or (when CLIP is enabled) CLIP model are not present in the local
  Hugging Face hub cache
- **THEN** the `models.cache` check reports a warn-level status naming the missing model(s)
- **AND** the remediation is to run `exomem warm`
- **AND** no vault file, cache directory, or model file is created, modified, or downloaded by the
  check itself

#### Scenario: Remote probe confirms the live endpoint triple

- **WHEN** `doctor --profile remote --probe` is run against a working tunnel
- **THEN** the report includes a passing check for `http://127.0.0.1:8765/mcp`
  returning `401`
- **AND** a passing check for `{EXOMEM_BASE_URL}/.well-known/oauth-authorization-server`
  returning `200` JSON
- **AND** a passing check for the bare `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource`
  returning `200` JSON with `resource == {EXOMEM_BASE_URL}/mcp`

#### Scenario: Remote probe catches the bare well-known 404 that breaks connector registration

- **WHEN** `doctor --profile remote --probe` is run and the bare
  `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource` path returns `404`
- **THEN** the corresponding check fails
- **AND** its remediation names the `mcp_registration_failed` failure mode
  claude.ai's gateway hits and points at the server's workaround route being
  live through the tunnel

#### Scenario: Remote probe reports an unreachable endpoint actionably

- **WHEN** `doctor --profile remote --probe` cannot connect to one of the three
  endpoints (connection refused, timeout, or DNS failure)
- **THEN** the corresponding check fails
- **AND** its remediation is actionable, e.g. naming that the tunnel is not
  running and how to start it
