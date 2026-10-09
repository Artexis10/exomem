## MODIFIED Requirements

### Requirement: Multimodal capability defaults on without startup model residency
The standard product profile SHALL install hybrid retrieval, document/PDF extraction, OCR
bindings, ASR, and CLIP capability. Starting the service MUST NOT load ASR, CLIP, embedding,
MPS, MLX, or CUDA model state solely because those capabilities are installed. Model-backed
extraction is deterministic transduction and SHALL soft-fail without preventing lexical
retrieval or service startup.

A job blocked because the engine its stage needs was unavailable SHALL return to pending,
without a caller's retry, when a supervisor starts with that engine available. A job blocked
for any other reason SHALL keep its existing behaviour.

#### Scenario: Standard service starts idle
- **WHEN** a standard-profile service starts with no queued media work
- **THEN** no media child process is running
- **AND** no ASR or CLIP model is loaded by startup

#### Scenario: Optional engine is unavailable
- **WHEN** queued evidence requires an optional engine that is missing
- **THEN** the job remains visible as blocked with remediation context
- **AND** the MCP service and lexical retrieval remain available

#### Scenario: A blocked job resumes when its engine appears
- **WHEN** a job is blocked because its engine was unavailable, and the service later starts with that engine available
- **THEN** the job returns to pending without a caller's retry
- **AND** it is processed without creating a duplicate job

#### Scenario: Other blocked jobs are not requeued
- **WHEN** a job is blocked for a reason other than an unavailable engine, such as a compute-runtime failure or an ambiguous sidecar boundary
- **THEN** the job stays blocked until its own existing recovery or an explicit retry

### Requirement: Durable idempotent multimodal jobs
Every extraction, CLIP, and post-processing operation SHALL be represented in a rebuildable
SQLite ledger before execution. Enqueue MUST deduplicate equivalent pending work, claiming MUST
be atomic, and interrupted running work MUST become eligible after recovery.

Work that a memory stop interrupts SHALL return to pending. A memory stop is a stop for memory
pressure, or an allocation failure under the worker's hard memory limit. The job SHALL NOT be
recorded as an artifact failure, and SHALL NOT consume an artifact attempt.

#### Scenario: Service crashes after claim
- **WHEN** a service or child process exits while a media job is running
- **THEN** the next supervisor recovers the job to pending
- **AND** processing may repeat without corrupting the sidecar or indexes

#### Scenario: Duplicate clients enqueue the same evidence
- **WHEN** two server processes enqueue the same pending stages for one evidence file
- **THEN** the ledger contains one merged job
- **AND** each requested stage runs at most once concurrently

#### Scenario: A memory stop is not an artifact failure
- **WHEN** the supervisor stops a media child because of memory pressure while a job runs
- **THEN** the job returns to pending with its artifact attempt count unchanged
- **AND** neither the job nor its sidecar reports the artifact as failed or corrupt

#### Scenario: An allocation failure under the hard limit is a memory stop
- **WHEN** an engine's allocation fails under the worker's hard memory limit
- **THEN** the job returns to pending with a typed memory reason and its artifact attempt count unchanged
