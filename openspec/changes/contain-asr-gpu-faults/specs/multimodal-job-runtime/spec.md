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
- **AND** an explicitly requested accelerator that is absent, hidden, too small for ASR, or fails its capability check is refused rather than silently reported as healthy
- **AND** an explicitly requested accelerator that only lacks free memory now defers the job instead

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
When the operator explicitly requests CUDA and the GPU works but another process holds so much of its memory that ASR cannot load, Exomem SHALL treat the GPU as busy and not as broken. A busy GPU SHALL defer the speech job: the job stays pending with no error, no attempt counted and an unchanged sidecar, and it runs when a later attempt finds enough free memory. A busy GPU MUST NOT block the job, MUST NOT mark any other job blocked, MUST NOT hold back media that the speech engine does not process, and MUST NOT move the job to CPU. Exomem SHALL ask the GPU again at a bounded interval without operator action. Status SHALL report the number of waiting speech jobs, the reason with the free and needed memory, and since when.

#### Scenario: A busy GPU defers the job and later runs it
- **WHEN** another process holds the GPU so that free VRAM is below the ASR minimum, and a recording is queued
- **THEN** the job stays pending with no error and no attempt counted
- **AND** the transcript is produced by a later attempt that finds enough free VRAM, without a manual retry

#### Scenario: The GPU fills up between the check and the model load
- **WHEN** the free-memory check admits the GPU and the model load then fails with a CUDA out-of-memory error
- **THEN** the job is deferred as for a busy GPU
- **AND** it is not failed or blocked

#### Scenario: A busy GPU does not hold back other media
- **WHEN** a speech job waits for a busy GPU and an image job is queued, before or during the wait
- **THEN** the image job is processed without waiting for the interval to end
- **AND** the speech job stays pending

#### Scenario: A broken CUDA runtime still blocks
- **WHEN** the operator explicitly requests CUDA and the CUDA device is missing, the card is too small for ASR, the probe cannot run, or the CUDA libraries fail to load
- **THEN** the media job becomes blocked with runtime remediation
- **AND** Exomem does not defer it as it defers a busy GPU

#### Scenario: A busy GPU never moves the job to CPU
- **WHEN** a speech job waits for a busy GPU under an explicit CUDA request
- **THEN** no transcript is produced on CPU
- **AND** the job is not blocked

#### Scenario: A waiting job explains itself
- **WHEN** speech jobs wait for a busy GPU
- **THEN** status reports their number, the free and needed memory, and since when they wait
- **AND** doctor reports the same as a warning

### Requirement: A CUDA fault never takes down the serving worker
Exomem SHALL NOT let a CUDA library fault end the process that serves requests. The service manager SHALL start every worker it owns with no CUDA device visible and SHALL keep the deployment's device list for the disposable media process alone. Before the first media process of a worker lifetime, the media supervisor SHALL initialize CUDA once in a throwaway process that has the media process's environment, with a finite timeout. When that process dies by a signal, times out or fails, the media process SHALL start with no CUDA device. An explicit CUDA request SHALL then block the job with the failure named, an automatic request SHALL use the CPU, and status and doctor SHALL show the reason. A media process that dies by a signal the supervisor did not send SHALL be restarted after a delay that doubles to a ceiling.

#### Scenario: CUDA initialization crashes
- **WHEN** the preflight process dies with a segmentation fault while it initializes CUDA
- **THEN** the serving worker keeps running and the media process starts with no CUDA device
- **AND** an explicit CUDA speech job is blocked with the signal and the failed stage in its error
- **AND** status and doctor show the same reason

#### Scenario: The GPU is healthy
- **WHEN** the preflight process initializes CUDA and exits cleanly
- **THEN** the media process receives the deployment's CUDA device list
- **AND** the serving worker still sees no CUDA device

#### Scenario: A model asks for CUDA inside the serving worker
- **WHEN** the deployment asks a torch model in the serving worker to use CUDA
- **THEN** the model runs on the CPU
- **AND** the worker does not initialize CUDA

#### Scenario: A media process keeps dying by a signal
- **WHEN** successive media processes die by a signal that the supervisor did not send
- **THEN** each restart waits twice as long as the one before, up to a ceiling
- **AND** the supervisor does not start a process every few seconds
