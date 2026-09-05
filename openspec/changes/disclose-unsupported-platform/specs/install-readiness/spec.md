## ADDED Requirements

### Requirement: A host with no held-filesystem backend refuses once and explains itself

Every governed write acquires a reserved-path root through the held-filesystem
substrate, which ships a backend per platform rather than a portable one. On a
host with no backend the system SHALL refuse a vault-touching command once, with
a message naming the platform and the platforms that are served, instead of
letting each acquisition fail separately and describe a permanent platform fact
as an unavailable filesystem route. Diagnostic and identification commands SHALL
remain reachable on such a host, and the read-only doctor SHALL report the
absent backend as a failure with remediation. The system SHALL NOT substitute a
weaker filesystem route where the backend is absent, and the distributed package
SHALL NOT advertise operating-system independence.

#### Scenario: A vault command on an unserved platform

- **WHEN** a user runs a vault-touching command on a host for which no held-filesystem backend exists
- **THEN** the command refuses with one message naming that platform and the served platforms
- **AND** the message directs the user to the read-only doctor

#### Scenario: Diagnosis stays reachable

- **WHEN** the same user runs the doctor or asks the package to identify itself
- **THEN** the command runs
- **AND** the doctor reports the absent backend as a failure whose remediation states that no choice of vault can repair it

#### Scenario: No weaker fallback is substituted

- **WHEN** the substrate is asked to acquire a root on a host with no backend
- **THEN** it returns a capability refusal
- **AND** no path-based or non-anchored route is used in its place

## MODIFIED Requirements

### Requirement: Cross-OS Runtime Recommendation

Setup and install-readiness documentation SHALL recommend a runtime shape based on
host capabilities and tradeoffs rather than forcing one universal path. Windows
live-vault installs SHALL default to native service guidance. Linux hosts with
NVIDIA container runtime SHALL be able to choose CUDA Docker as the low-friction
hybrid/GPU-capable route. Windows+WSL2 CUDA Docker SHALL be offered with an
explicit file-watcher bind-mount tradeoff. macOS SHALL be named as a platform
exomem cannot serve today, because the held-filesystem substrate has no darwin
backend, rather than being offered a runtime shape; any MPS/MLX guidance for it
SHALL be reinstated only alongside that backend.

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

#### Scenario: macOS is named as unserved rather than recommended

- **WHEN** setup or documentation addresses a macOS user
- **THEN** it states that exomem has no held-filesystem backend for that platform and cannot serve a vault there
- **AND** it does not recommend a native macOS runtime shape as though the vault would work
