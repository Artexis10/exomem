# multimodal-job-runtime Specification

## Purpose
TBD - created by archiving change add-resource-bounded-multimodal-workers. Update Purpose after archive.

## Requirements

### Requirement: Multimodal capability defaults on without startup model residency
The standard product profile SHALL install hybrid retrieval, document/PDF extraction, OCR
bindings, ASR, and CLIP capability. Starting the service MUST NOT load ASR, CLIP, embedding,
MPS, MLX, or CUDA model state solely because those capabilities are installed. Model-backed
extraction is deterministic transduction and SHALL soft-fail without preventing lexical
retrieval or service startup.

#### Scenario: Standard service starts idle
- **WHEN** a standard-profile service starts with no queued media work
- **THEN** no media child process is running
- **AND** no ASR or CLIP model is loaded by startup

#### Scenario: Optional engine is unavailable
- **WHEN** queued evidence requires an optional engine that is missing
- **THEN** the job remains visible as blocked with remediation context
- **AND** the MCP service and lexical retrieval remain available

### Requirement: Durable idempotent multimodal jobs
Every extraction, CLIP, and post-processing operation SHALL be represented in a rebuildable
SQLite ledger before execution. Enqueue MUST deduplicate equivalent pending work, claiming MUST
be atomic, and interrupted running work MUST become eligible after recovery.

#### Scenario: Service crashes after claim
- **WHEN** a service or child process exits while a media job is running
- **THEN** the next supervisor recovers the job to pending
- **AND** processing may repeat without corrupting the sidecar or indexes

#### Scenario: Duplicate clients enqueue the same evidence
- **WHEN** two server processes enqueue the same pending stages for one evidence file
- **THEN** the ledger contains one merged job
- **AND** each requested stage runs at most once concurrently

### Requirement: One serialized disposable worker per vault
Heavy media work SHALL run in a child process, serialized to one active worker per vault across
all server processes. The child SHALL reuse loaded models across a bounded burst and SHALL exit
after the configured idle interval when no eligible jobs remain.

#### Scenario: Worker returns resources after a burst
- **WHEN** the final queued media job completes and the idle interval elapses
- **THEN** the child process exits
- **AND** its Python heap, native model state, and accelerator context are no longer resident

#### Scenario: Duplicate supervisors race
- **WHEN** multiple Exomem processes attempt to launch a worker for the same vault
- **THEN** an OS-level vault lock permits only one child to process jobs
- **AND** losing children exit before loading model stacks

### Requirement: Observable job lifecycle
No-allocation status SHALL report pending, running, blocked, and failed job counts plus active
worker state without importing heavy model modules. Human-readable logs SHALL distinguish
queued, loading/processing, blocked, failed, and idle-exit states.

#### Scenario: Resource status with queued work
- **WHEN** media work is queued while no worker is active
- **THEN** resource status reports the durable queue depth and worker inactive state
- **AND** collecting status does not import torch, MLX, CTranslate2, or model modules

### Requirement: Multimodal advanced enrichments remain opt-in
Generated image captioning and speaker diarization SHALL remain default-off optional
enrichments. Their absence MUST NOT make the standard profile non-multimodal or prevent OCR,
ASR, CLIP, PDF, and Office processing.

#### Scenario: Standard profile without advanced enrichments
- **WHEN** a standard installation processes image, audio, and video evidence
- **THEN** baseline OCR/ASR/CLIP processing is available
- **AND** no captioning or diarization model is loaded unless explicitly enabled

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
- **WHEN** the operator explicitly requests CUDA and CUDA admission or the selected computation type fails
- **THEN** Exomem returns a typed compute-runtime refusal
- **AND** it does not retry or continue on CPU

#### Scenario: Automatic computation policy cannot be re-enabled
- **WHEN** the operator sets `EXOMEM_ASR_COMPUTE_TYPE=auto`
- **THEN** Exomem refuses the value and names the concrete supported choices
- **AND** CTranslate2 4.8's incident-causing automatic INT8 selection is never entered

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

### Requirement: Machine-generated extraction duplication is repairable without deleting authored notes
Media extraction SHALL use explicit machine-owned boundary markers and treat headings inside extracted Markdown as payload. An unmarked nonempty legacy preserved-notes boundary SHALL block extraction commit before sidecar metadata or deferred-index state changes. The governed legacy repair SHALL collapse preserved-note residue only when a surviving extraction supplies the byte-exact candidate. When no extraction survives, repair SHALL retain the residual until a fresh source extraction supplies the candidate. Source-derived cleanup SHALL require at least three exact candidate copies separated only by whitespace. Two copies and any differing non-whitespace residue SHALL remain preserved. Repair SHALL retain frontmatter verbatim, remain idempotent, and refuse an output that shortens the selected extraction.

#### Scenario: Extracted document contains Markdown headings
- **WHEN** a document extraction containing H1 or H2 headings is written again above an explicitly marked machine-owned notes section
- **THEN** the full extraction is replaced once through that explicit boundary
- **AND** neither the document headings nor the preserved notes are duplicated

#### Scenario: Legacy sidecar has a surviving extraction candidate
- **WHEN** preserved-note residue contains hundreds of exact copies of a surviving selected extraction with only whitespace between them
- **THEN** repair leaves one selected extraction and removes the duplicate residue
- **AND** a second repair produces no change

#### Scenario: Blank-top residual waits for source authority
- **WHEN** a legacy sidecar has an empty extraction and repeated preserved-note bytes whose authored unit cannot be inferred
- **THEN** audit reports that source re-extraction is required and makes no repair write
- **AND** a fresh extraction collapses the residual only when it is at least three exact copies of that fresh result separated solely by whitespace

#### Scenario: Preserved residue includes authored prose
- **WHEN** only two candidate copies exist or even one non-whitespace fragment differs from the selected extraction
- **THEN** repair retains that residual content
- **AND** exact-copy detection does not heuristically delete it

#### Scenario: Legacy notes boundary is ambiguous
- **WHEN** a nonempty unmarked legacy `## Preserved notes` heading could be document payload or sidecar structure
- **THEN** extraction commit leaves the sidecar and deferred-index queue unchanged
- **AND** the durable media job is blocked with an action to review the sidecar boundary before retry
