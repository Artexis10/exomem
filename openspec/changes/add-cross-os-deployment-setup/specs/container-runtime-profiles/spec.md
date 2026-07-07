## ADDED Requirements

### Requirement: Published Container Runtime Families

The project SHALL publish distinct container runtime families for lean lexical
search, CPU hybrid search, and NVIDIA CUDA-capable hybrid search. The lean family
SHALL remain the default `latest` and `X.Y.Z` tag. The CPU hybrid family SHALL use
`ml` and `X.Y.Z-ml` tags. The CUDA-capable family SHALL use `cuda` and `X.Y.Z-cuda`
tags. The CUDA family SHALL package deterministic measurement dependencies only
and MUST NOT add any server-side reasoning model.

#### Scenario: Release publishes all runtime tags

- **WHEN** a release publishes container images
- **THEN** lean tags include `latest` and the immutable version tag
- **AND** CPU hybrid tags include `ml` and the immutable `X.Y.Z-ml` tag
- **AND** CUDA tags include `cuda` and the immutable `X.Y.Z-cuda` tag

#### Scenario: Default pull remains resource-light

- **WHEN** a user pulls `ghcr.io/artexis10/exomem:latest`
- **THEN** the image is the lean runtime family
- **AND** it does not include torch, CUDA runtime libraries, or model downloads

### Requirement: CUDA Capability Does Not Imply Idle CUDA Residency

The CUDA-capable container SHALL boot in the same resource-safe normal mode as
native installs unless overridden. It SHALL make CUDA available for explicit
performance mode, explicit device selection, or bulk indexing, but MUST NOT
initialize CUDA merely because the CUDA image was selected.

#### Scenario: CUDA image starts in normal mode

- **WHEN** the CUDA-capable image starts without `EXOMEM_MODE=performance` or an
  explicit CUDA device override
- **THEN** the service reports normal mode
- **AND** steady-state torch models select CPU by default
- **AND** CUDA is not initialized just because the image includes CUDA libraries

#### Scenario: Performance mode can use CUDA

- **WHEN** the CUDA-capable image runs with `EXOMEM_MODE=performance` on a host with
  a working NVIDIA container runtime
- **THEN** the service may select CUDA for torch-backed measurement models
- **AND** failures to access CUDA degrade to CPU with an actionable diagnostic rather
  than crashing the MCP server

### Requirement: Compose Overrides Select Runtime Shape Explicitly

The root Compose configuration SHALL expose explicit runtime choices for lean,
CPU-ML, and CUDA-capable deployments without starting multiple Exomem services.
CPU-ML and CUDA SHALL be selected through Compose override files or an equivalent
single-service mechanism. The CUDA runtime selection SHALL declare the NVIDIA
device/runtime requirements separately from CPU paths. All container runtime
choices SHALL persist `/data` so logs, query telemetry, and model caches survive
restarts without being stored in the mounted vault.

#### Scenario: CPU runtime avoids NVIDIA requirements

- **WHEN** a user starts the lean Compose file or CPU-ML override
- **THEN** the Compose service does not require NVIDIA devices or runtime settings
- **AND** it mounts the vault at `/vault` and persists service state under `/data`

#### Scenario: CUDA runtime declares NVIDIA access

- **WHEN** a user starts the CUDA Compose override
- **THEN** the Compose service uses the CUDA image tag
- **AND** it declares NVIDIA device access in Compose
- **AND** it still persists `/data` separately from `/vault`

### Requirement: Container Documentation Names OS Tradeoffs

Docker documentation SHALL explain which runtime family to choose by host shape.
It SHALL recommend native Windows for live-vault file-watcher reliability, native
macOS for MPS/MLX, CUDA Docker for Linux+NVIDIA, and CUDA Docker on Windows+WSL2
only when the user accepts bind-mount watcher tradeoffs. The documentation SHALL
state that GPU-capable containers are still CPU-default at idle.

#### Scenario: User can choose a runtime from docs

- **WHEN** a user reads the Docker setup documentation
- **THEN** it distinguishes lean, CPU-ML, and CUDA-capable images
- **AND** it names the Windows and macOS native recommendations
- **AND** it states that CUDA capability is opt-in for residency through mode or
  device settings
