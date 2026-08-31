## ADDED Requirements

### Requirement: Disposable media compute uses a healthy accelerator when available
Normal and performance modes SHALL permit a disposable media worker to use a supported accelerator after an ASR-engine-specific capability check because the worker exits after its bounded idle interval. ASR admission MUST NOT depend on torch capability. Quiet mode SHALL remain CPU-only unless explicitly overridden. CPU-only and accelerator-declined execution SHALL use the active compute budget.

#### Scenario: Normal mode with a healthy GPU
- **WHEN** a normal-mode disposable worker starts on a host whose ASR accelerator path is usable
- **THEN** ASR runs on that accelerator without making the long-lived service retain its model context
- **AND** the accelerator context is released when the disposable worker exits

#### Scenario: No usable accelerator
- **WHEN** the accelerator is absent, hidden, lacks headroom, or fails its capability check before work begins
- **THEN** a CPU-only host uses the bounded CPU path
- **AND** an explicitly requested accelerator is refused rather than silently reported as healthy

#### Scenario: Torch and CTranslate2 disagree
- **WHEN** torch reports CUDA healthy but CTranslate2 cannot resolve or admit its CUDA runtime
- **THEN** ASR refuses or blocks its accelerator path based on CTranslate2's result
- **AND** torch's result does not admit the job

### Requirement: ASR computation type follows runtime capability
Accelerated ASR SHALL select a computation type supported by the installed runtime and device rather than force a quantization mode known to be unsupported on a device generation. An explicit operator computation-type override SHALL remain available and SHALL be disclosed.

#### Scenario: Accelerator does not support INT8 execution
- **WHEN** the accelerator runtime excludes INT8 from its supported computation types
- **THEN** automatic ASR selects a supported floating-point computation type
- **AND** transcription does not attempt the unsupported INT8 operation

### Requirement: Compute infrastructure failure is not an artifact failure
An accelerator, native-runtime, or compute-initialization failure SHALL durably block the media job before publishing its canonical failure sidecar, then converge either partial state after a crash without re-entering compute. The source artifact SHALL remain unchanged, and status SHALL NOT advise repairing or replacing it. Exomem SHALL NOT silently retry the same failed accelerator job on an unbounded CPU path. Retained legacy failed rows and sidecars with known CUDA/cuBLAS/cuDNN signatures SHALL receive corrected status immediately and SHALL converge to the blocked contract during bounded worker recovery.

#### Scenario: cuBLAS execution fails
- **WHEN** ASR raises a CUDA or cuBLAS execution failure for a valid media artifact
- **THEN** the durable job and canonical sidecar both record a blocked compute-runtime failure
- **AND** the remediation names the runtime/device choice rather than the media file

#### Scenario: Process exits between ledger and sidecar publication
- **WHEN** the process dies after the durable job is marked blocked but before its failure sidecar is published
- **THEN** startup repairs the sidecar from the blocked job without running ASR again
- **AND** the job never becomes pending merely because the sidecar write was interrupted

#### Scenario: Upgrade sees a legacy CUDA failure
- **WHEN** an older installation retained a failed job or failed sidecar whose error matches a known accelerator-runtime signature
- **THEN** status immediately presents compute-runtime remediation instead of artifact repair
- **AND** bounded recovery converges both stores to blocked without replacing or retranscribing the artifact

#### Scenario: Runtime is repaired
- **WHEN** the operator repairs the accelerator runtime or explicitly selects bounded CPU and retries
- **THEN** the existing durable job becomes eligible without replacing the source artifact
- **AND** a successful extraction moves both job and sidecar to their existing completed contract
