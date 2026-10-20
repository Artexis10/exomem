## MODIFIED Requirements

### Requirement: Multimodal capability defaults on without startup model residency
The standard product profile SHALL install hybrid retrieval, document/PDF extraction, OCR
bindings, ASR, and CLIP capability. Starting the service MUST NOT load ASR, CLIP, embedding,
MPS, MLX, or CUDA model state solely because those capabilities are installed. Model-backed
extraction is deterministic transduction and SHALL soft-fail without preventing lexical
retrieval or service startup.

A job blocked because the engine its stage needs was unavailable SHALL return to pending,
without a caller's retry, when a supervisor starts with that engine available. Two other
blocked reasons recover by the rules of the `cloud-multimodal-processing` capability: a
memory-blocked job, and a job that exceeds the deployment's processing budget. A job blocked
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
- **WHEN** a job is blocked for a reason other than an unavailable engine, memory or the processing budget, such as a compute-runtime failure or an ambiguous sidecar boundary
- **THEN** the job stays blocked until its own existing recovery or an explicit retry

#### Scenario: Memory-blocked and over-budget jobs follow their own recovery
- **WHEN** a job is memory-blocked, or blocked as exceeding the deployment's processing budget
- **THEN** it returns to pending under the recovery rules of the `cloud-multimodal-processing` capability, not under this requirement's engine rule

### Requirement: Durable idempotent multimodal jobs
Every extraction, CLIP, and post-processing operation SHALL be represented in a rebuildable
SQLite ledger before execution. Enqueue MUST deduplicate equivalent pending work, claiming MUST
be atomic, and interrupted running work MUST become eligible after recovery.

Work that a memory stop interrupts SHALL return to pending, except where the
`cloud-multimodal-processing` capability blocks it after repeated stops. A memory stop is a stop
for memory pressure, or an allocation failure under the worker's hard memory limit. The job SHALL
NOT be recorded as an artifact failure, and SHALL NOT consume an artifact attempt.

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

## ADDED Requirements

### Requirement: Retained media results bind material evidence and publish against current bytes

A retained result SHALL bind the original binary identity and the sidecar's existing evidence version.
The evidence version binds the body and typed frontmatter, excluding only `ingested_into` backlinks.
Backlink changes and equivalent YAML formatting SHALL NOT discard otherwise valid computation.
Body changes, meaningful metadata changes and binary replacement SHALL retain their existing invalidation behavior.

Publication SHALL validate and render from the same guarded current sidecar snapshot.
The final write SHALL retain full-byte compare-and-swap protection and preserve current backlinks.
A concurrent publication conflict SHALL revalidate retained computation without repeating extraction solely because backlinks changed.
Admission, completed-transcript handling, preserved-note boundaries and claim fences SHALL remain in force.

The durable result SHALL retain separate input and prepared-target evidence versions for recovery across publication.
Recovery SHALL recognize committed output after backlinks change and converge current content through the existing receipt path.
Recovery SHALL verify the binary before accepting material equivalence or a foreign completed extraction.
Legacy results without evidence versions SHALL retain exact-byte protection; unavailable versions SHALL never establish equivalence.

#### Scenario: A backlink changes during extraction
- **WHEN** a governed writer adds a backlink while extraction runs
- **THEN** the retained extraction publishes without repeating computation
- **AND** the published sidecar preserves the new backlink

#### Scenario: A meaningful edit accompanies a backlink
- **WHEN** the body or meaningful metadata changes alongside a backlink
- **THEN** publication preserves the edit and rejects the obsolete result

#### Scenario: The binary changes alongside bookkeeping
- **WHEN** the source binary changes while only backlinks change in the sidecar
- **THEN** the old result cannot publish or finalize against the replacement binary

#### Scenario: An edit races with publication
- **WHEN** another writer changes the sidecar after validation
- **THEN** the full-byte publication guard refuses the stale write
- **AND** retained computation is revalidated before another publication attempt

#### Scenario: Recovery spans a canonical write
- **WHEN** a process exits before or after canonical publication and a backlink changes before recovery
- **THEN** recovery distinguishes unchanged input from committed output through their separate evidence versions
- **AND** it preserves current backlinks and clears only the recorded receipt revision after convergence

#### Scenario: A legacy result cannot prove material equivalence
- **WHEN** a result lacks evidence versions or current metadata cannot supply a valid version
- **THEN** recovery grants no material-equivalence exception to its exact-byte protection
