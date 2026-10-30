## MODIFIED Requirements

### Requirement: Disposable media compute uses a healthy accelerator when available
Normal and performance modes SHALL permit a disposable media worker to use a supported accelerator after an ASR-engine-specific capability check because the worker exits after its bounded idle interval. ASR admission MUST NOT depend on torch capability. Quiet mode SHALL remain CPU-only unless explicitly overridden. CPU-only and accelerator-declined execution SHALL use the active compute budget.

#### Scenario: Normal mode with a healthy GPU
- **WHEN** a normal-mode disposable worker starts on a host whose ASR accelerator path is usable
- **THEN** ASR runs on that accelerator without making the long-lived service retain its model context
- **AND** the accelerator context is released when the disposable worker exits

#### Scenario: No usable accelerator
- **WHEN** the accelerator is absent, hidden, lacks headroom, or fails its capability check before work begins
- **THEN** a host without an explicit CUDA request uses the bounded CPU path
- **AND** an explicitly requested accelerator that is absent, hidden, or fails its capability check is refused rather than silently reported as healthy
- **AND** an explicitly requested accelerator that only lacks headroom defers the job instead

#### Scenario: Torch and CTranslate2 disagree
- **WHEN** torch reports CUDA healthy but CTranslate2 cannot resolve or admit its CUDA runtime
- **THEN** ASR refuses or blocks its accelerator path based on CTranslate2's result
- **AND** torch's result does not admit the job

### Requirement: ASR computation type follows runtime capability
Accelerated ASR SHALL use CUDA `float16` by default and CPU `int8` by default. A concrete operator computation-type override SHALL remain available and SHALL be disclosed, but `auto`, unknown values, and values not reported supported on the selected device SHALL be refused. On compute capability 12.x, every INT8-family override SHALL be refused despite CTranslate2 4.8 reporting it as supported. Runtime capability reporting is preflight only and SHALL NOT replace real model-execution proof.

#### Scenario: Runtime misreports Blackwell INT8 support
- **WHEN** the accelerator runtime reports both `float16` and an INT8 type on an `sm_120` device
- **THEN** Exomem selects the conservative CUDA `float16` default rather than CTranslate2 4.8's `auto` INT8 path
- **AND** a real ASR model operation succeeds before readiness claims the path works

#### Scenario: Automatic device decline respects a computation override
- **WHEN** automatic CUDA admission declines and the operator supplied a concrete computation type
- **THEN** Exomem uses bounded CPU only when that exact type is reported supported on CPU
- **AND** otherwise refuses instead of discarding the override or starting an unsafe fallback

#### Scenario: Explicit CUDA never falls back silently
- **WHEN** the operator explicitly requests CUDA and the runtime cannot serve it (the CUDA device is missing, the probe cannot run, or the CUDA libraries fail to load) or the selected computation type fails
- **THEN** Exomem returns a typed compute-runtime refusal
- **AND** it does not retry or continue on CPU

#### Scenario: Automatic computation policy cannot be re-enabled
- **WHEN** the operator sets `EXOMEM_ASR_COMPUTE_TYPE=auto`
- **THEN** Exomem refuses the value and names the concrete supported choices
- **AND** CTranslate2 4.8's incident-causing automatic INT8 selection is never entered

## ADDED Requirements

### Requirement: A busy GPU defers explicit CUDA speech work
When the operator explicitly requests CUDA and the GPU works but has too little free VRAM for ASR, Exomem SHALL treat the GPU as busy and not as broken. A busy GPU SHALL defer the speech job: the job stays pending with no error, no attempt counted and an unchanged sidecar, and it runs when a later attempt finds enough free VRAM. A busy GPU MUST NOT block the job, MUST NOT mark any other job blocked, MUST NOT hold back media that the speech engine does not process, and MUST NOT move the job to CPU. Exomem SHALL ask the GPU again at a bounded interval without operator action.

#### Scenario: A busy GPU defers the job and later runs it
- **WHEN** another process holds the GPU so that free VRAM is below the ASR minimum, and a recording is queued
- **THEN** the job stays pending with no error and no attempt counted
- **AND** the transcript is produced by a later attempt that finds enough free VRAM, without a manual retry

#### Scenario: A busy GPU does not hold back other media
- **WHEN** a speech job waits for a busy GPU and an image job is queued behind it
- **THEN** the image job is processed in the same worker run
- **AND** the speech job stays pending

#### Scenario: A broken CUDA runtime still blocks
- **WHEN** the operator explicitly requests CUDA and the CUDA device is missing, the probe cannot run, or the CUDA libraries fail to load
- **THEN** the media job becomes blocked with runtime remediation
- **AND** Exomem does not defer it as it defers a busy GPU

#### Scenario: A busy GPU never moves the job to CPU
- **WHEN** a speech job waits for a busy GPU under an explicit CUDA request
- **THEN** no transcript is produced on CPU
- **AND** the job is not blocked
