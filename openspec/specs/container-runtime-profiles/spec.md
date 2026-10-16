# container-runtime-profiles Specification

## Purpose
Publish distinct container runtime families for lean lexical search, CPU hybrid search and NVIDIA CUDA hybrid search, with explicit Compose overrides to choose among them. CUDA capability does not imply idle CUDA residency, and Docker documentation states the OS tradeoffs for each host shape.

## Requirements

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

### Requirement: Exomem Cloud Images Publish One Multi-Platform Index

A release that publishes container images SHALL publish the Exomem Cloud cell image and the cellctl image for `linux/amd64` and `linux/arm64` as one image index per tag. Each platform SHALL be built on a runner of its own architecture. The release SHALL attest the index digest and retain its signed receipt as a release asset; a node pulls the platform manifest that matches its architecture. Every binary that the Cloud cell or cellctl image build downloads SHALL be selected by the target architecture and checked against a sha256 pinned for that architecture, and a target architecture without a pin SHALL fail the build. The CUDA image MAY remain `linux/amd64` only. Retired image roles SHALL remain retired.

#### Scenario: A release publishes both platforms under one digest

- **WHEN** a release publishes the Cloud images
- **THEN** `X.Y.Z-cloud` and `exomem-cellctl:X.Y.Z` each resolve to one index that lists `linux/amd64` and `linux/arm64`
- **AND** the image attestation and retained release receipt name that index digest

#### Scenario: A binary for the wrong architecture fails the build

- **WHEN** an image build targets an architecture that has no pinned download, or the downloaded restic binary cannot run on the build platform
- **THEN** the build fails before the image is published

#### Scenario: Each architecture is smoke-tested before merge

- **WHEN** a pull request changes a Cloud image's Dockerfile, its dependency manifest or lock, or the source it packages
- **THEN** CI builds the cell and cellctl images natively on `linux/amd64` and `linux/arm64`
- **AND** on each, restic runs, the CLI reports its version, and a cell saves a note and finds it

### Requirement: Native publication preserves release recovery

The release plan SHALL remain authoritative for lean, ml, cuda, cloud and cellctl. Native jobs SHALL build only missing roles, push only by digest, and pass successful child digests through Actions artifacts. One serialized publisher SHALL recover authentic existing receipts before joining new native digests. New Cloud and cellctl indexes SHALL contain exactly `linux/amd64` and `linux/arm64` runtime descriptors, preserve BuildKit provenance, and be inspected by digest before attestation. Historical authentic receipts SHALL NOT require both platforms or trigger a rebuild. Only the existing image finalizer SHALL write release aliases after evidence and conflict checks.

#### Scenario: A retry finds an authentic historical receipt

- **WHEN** original evidence exists for a role, including a historical single-platform image
- **THEN** the release recovers that digest without rebuilding or replacing its receipt
- **AND** it completes missing aliases only after the existing conflict checks

#### Scenario: One native role fails

- **WHEN** a native role lacks a complete platform pair
- **THEN** the serialized publisher completes unaffected roles with valid evidence
- **AND** its finalizer reports the missing role as incomplete
- **AND** it creates no attestation or alias for the incomplete role

#### Scenario: A dry run builds the current release plan

- **WHEN** a release runs in dry-run mode
- **THEN** both native Cloud and cellctl platforms and the other three roles build
- **AND** the run performs no login, push, attestation, receipt upload or alias write
