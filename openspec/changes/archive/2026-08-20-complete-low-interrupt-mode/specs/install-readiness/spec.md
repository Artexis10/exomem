## ADDED Requirements

### Requirement: Doctor Reports Resource Posture Without Heavy Allocation

The doctor command SHALL report the current resource posture for local installs
without mutating the repo, vault, environment, service state, model caches, or
CUDA state. The report SHALL identify the effective mode, CPU/GPU fallback
posture, whether CUDA is required for the requested profile, and whether the host
appears to be CPU-only or marginal for GPU use. The check MUST NOT load models,
download model files, create sidecars, or initialize CUDA solely for diagnostics.

#### Scenario: CPU-only host passes lean readiness

- **WHEN** `doctor --profile lean` runs on a host with no usable CUDA device
- **THEN** the doctor report does not fail because CUDA is absent
- **AND** it reports that CPU is the supported baseline for the current profile

#### Scenario: Marginal GPU produces remediation not failure for lean profile

- **WHEN** `doctor --profile lean` or `doctor --profile hybrid` detects that the
  GPU is absent, unavailable, or below the configured free-VRAM threshold
- **THEN** the report explains that Exomem will use CPU unless the user explicitly
  enables and satisfies GPU policy
- **AND** the check does not allocate CUDA to prove that result

#### Scenario: Resource posture appears in JSON output

- **WHEN** `doctor --json` is run
- **THEN** the JSON includes a resource-posture check with mode, policy, and
  best-effort GPU availability fields
- **AND** unknown probe results are represented as unknown or unavailable rather
  than as failures

### Requirement: Setup Recommends Safe Default Resource Mode

The setup flow SHALL keep the safe resource default unless the user explicitly
opts into performance mode. If setup detects a capable idle GPU, it MAY recommend
performance mode for faster indexing, but it MUST explain that normal mode avoids
steady-state CUDA residency and that quiet mode is available for gaming or other
foreground workloads.

#### Scenario: Capable GPU is discoverable but not silently enabled

- **WHEN** setup detects a capable idle GPU
- **THEN** setup may offer performance mode as an explicit option
- **AND** setup does not silently switch the user into performance mode without
  consent

#### Scenario: Setup documents quiet mode

- **WHEN** setup completes
- **THEN** the user-facing next steps mention the CLI command for entering quiet
  mode or inspecting resource status
