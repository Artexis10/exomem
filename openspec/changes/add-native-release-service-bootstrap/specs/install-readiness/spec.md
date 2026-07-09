## ADDED Requirements

### Requirement: Native release service bootstrap
The project SHALL expose one blessed native service-install command per platform.
Release mode MUST create or update a PyPI-backed service venv outside the checkout,
install the selected profile extras, and install and start the platform service.
Repository-venv mode SHALL remain available for development.

#### Scenario: Linux or macOS release install
- **WHEN** a user runs `bash scripts/install-service.sh --release --profile hybrid`
- **THEN** the installer creates or updates an external venv with
  `exomem[embeddings]` from PyPI
- **AND** it installs and starts a systemd user service on Linux or a launchd user
  agent on macOS

#### Scenario: Repository development install
- **WHEN** a contributor selects `--repo-dev` or uses the historical no-mode form
- **THEN** the installer uses the checkout `.venv` without installing a PyPI
  release package

### Requirement: Profile-complete release environment
The release installer SHALL map lean, hybrid, and media profiles to their published
extras, load the selected dotenv file into preflight and service environments, and
MUST NOT depend on the checkout working directory for runtime dotenv discovery.

#### Scenario: Media profile on Apple Silicon
- **WHEN** release mode selects the media profile on macOS arm64
- **THEN** the PyPI requirement includes embeddings, media, vision, diarization,
  and the macOS MLX media extra

#### Scenario: Service receives dotenv values
- **WHEN** the selected `.env` contains Exomem vault and OAuth settings
- **THEN** those values are available to doctor and the installed service
- **AND** generated files containing secrets are readable only by the current user

### Requirement: Transactional readiness and endpoint verification
Native installers SHALL run the selected capability doctor and remote-environment
doctor before changing the service manager. They SHALL start the service only after
both gates pass and SHALL verify `/mcp` before reporting success.

#### Scenario: Doctor failure preserves service-manager state
- **WHEN** either doctor gate fails
- **THEN** the installer exits nonzero before invoking launchd, systemd, or NSSM
  installation commands

#### Scenario: Authenticated MCP endpoint is healthy
- **WHEN** the started service returns HTTP `401` from its local `/mcp` endpoint
- **THEN** the installer reports the service as installed and healthy

#### Scenario: Unauthenticated MCP endpoint fails closed
- **WHEN** the started service returns HTTP `200` from its local `/mcp` endpoint
- **THEN** the installer stops the service, exits nonzero, and reports that OAuth
  enforcement is missing
