## ADDED Requirements

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
