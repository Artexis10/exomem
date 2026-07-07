## ADDED Requirements

### Requirement: Cross-OS Runtime Recommendation

Setup and install-readiness documentation SHALL recommend a runtime shape based on
host capabilities and tradeoffs rather than forcing one universal path. Windows
live-vault installs SHALL default to native service guidance. Linux hosts with
NVIDIA container runtime SHALL be able to choose CUDA Docker as the low-friction
hybrid/GPU-capable route. Windows+WSL2 CUDA Docker SHALL be offered with an
explicit file-watcher bind-mount tradeoff. macOS Apple Silicon SHALL default to
native setup for MPS/MLX support.

#### Scenario: Windows native remains recommended

- **WHEN** setup or documentation addresses a Windows user with a live local vault
- **THEN** it recommends the native Windows service path by default
- **AND** it explains that Docker Desktop bind mounts can miss live file-watch events

#### Scenario: Linux NVIDIA can choose CUDA Docker

- **WHEN** setup or documentation addresses a Linux user with Docker and NVIDIA
  runtime available
- **THEN** it offers the CUDA Docker path as a supported one-command route
- **AND** it states that the service still boots resource-safe unless performance
  mode or an explicit CUDA device is selected

### Requirement: Deterministic Native Dependency Gates

Native service setup SHALL gate the selected dependency profile before declaring the
service installed or restarted successfully. A hybrid native service SHALL fail the
gate when `sentence-transformers`, `torch`, `Pillow`, or `sqlite-vec` are missing.
A media native service SHALL additionally fail the gate when media dependencies are
missing. Remediation SHALL name the locked `uv sync` command for the selected profile.

#### Scenario: Hybrid service missing embeddings fails before success

- **WHEN** native service setup or restart is run for a hybrid profile and
  `sentence-transformers` is not importable
- **THEN** the operation reports failure instead of declaring the service healthy
- **AND** the remediation names `uv sync --frozen --extra embeddings`

#### Scenario: Media service missing media dependencies fails before success

- **WHEN** native service setup or restart is run for a media profile and media
  dependencies are not importable
- **THEN** the operation reports failure instead of declaring the service healthy
- **AND** the remediation names the media extra and any required system tools

### Requirement: Runtime And Compute Diagnostics

The doctor command SHALL report the effective runtime and compute profile when that
information is available. The report SHALL distinguish native versus container
runtime, dependency profile, compute mode, selected torch device, CUDA availability,
and CUDA residency or initialization status when detectable. Diagnostics MUST
distinguish "CUDA-capable but not resident" from "running on CUDA".

#### Scenario: Docker CUDA image in normal mode reports capability separately

- **WHEN** doctor runs inside a CUDA-capable container in normal mode
- **THEN** it reports the CUDA image/runtime capability
- **AND** it reports normal compute mode and CPU-default selected device
- **AND** it does not describe CUDA as resident unless CUDA has actually been
  initialized

#### Scenario: Native hybrid install reports missing extras clearly

- **WHEN** doctor runs for a native hybrid profile without the embeddings extra
- **THEN** it reports the missing dependency checks as failures
- **AND** it does not require inspecting server logs to understand that search would
  degrade to lexical ranking

### Requirement: Terminal-Safe Doctor Output

Human-readable doctor output SHALL be safe on Windows consoles using legacy code
pages as well as UTF-8 terminals. It MUST NOT crash while rendering advisory text
because of a non-ASCII symbol. Any optional decorative symbol SHALL have an ASCII
fallback or be omitted.

#### Scenario: Windows cp1252 console renders doctor output

- **WHEN** doctor emits human-readable output on a Windows cp1252 console
- **THEN** it completes without `UnicodeEncodeError`
- **AND** all warnings, failures, and remediation text remain readable
