# resource-governance Specification

## Purpose
TBD - created by archiving change add-resource-bounded-multimodal-workers. Update Purpose after archive.

## Requirements

### Requirement: Safe normal-mode residency
Normal mode SHALL avoid startup model and O(vault) cache preloads and SHALL run idle resource
reclamation by default. Performance mode MAY preload when explicitly selected. Environment
overrides SHALL remain available for deliberate operator choices.

#### Scenario: Normal-mode startup
- **WHEN** the service starts in normal mode
- **THEN** models and O(vault) CPU caches remain lazy
- **AND** the idle reaper is active

#### Scenario: Explicit performance mode
- **WHEN** an operator selects performance mode
- **THEN** the service may preload latency-oriented resources
- **AND** the choice is visible through resource status

### Requirement: Persistent-core resource acceptance envelope
Release verification SHALL measure the persistent service separately from transient workers.
After worker idle exit on the maintained fixture, the acceptance targets SHALL be no active
media worker, less than 200 MiB GPU delta/no CUDA compute process, no more than 512 MiB
persistent-core RSS before user-triggered cache growth, and less than 1% idle CPU averaged over
60 seconds.

#### Scenario: Resource verification after media work
- **WHEN** a representative media job completes and the worker idle interval elapses
- **THEN** verification records zero media workers and the persistent-core RSS/CPU/GPU metrics
- **AND** an exceeded target is treated as a release failure or explicitly documented blocker

### Requirement: Resource Modes Define Host Footprint Policy

The system SHALL expose resource modes that define how much of the host machine
Exomem may occupy while running. The canonical modes SHALL be `quiet`, `normal`,
and `performance`. `quiet` SHALL be the low-resource mode: CPU-only for steady
state model work, no heavy startup preloads, idle unload enabled, large CPU-side
caches evictable, and background maintenance throttled or deferred. `normal`
SHALL be the safe default: CPU steady-state with no automatic CUDA residency, but
latency-oriented CPU caches may remain warm. `performance` SHALL be an explicit
high-throughput opt-in that may use a capable GPU and retain warm caches for
speed.

#### Scenario: Quiet mode resolves low-resource policy

- **WHEN** the effective mode is `quiet`
- **THEN** the resolved policy reports CPU steady-state device selection, disabled
  heavy preloads, enabled idle unload, evictable CPU caches, and deferred expensive
  background indexing

#### Scenario: Normal mode does not automatically allocate CUDA

- **WHEN** the effective mode is `normal`
- **THEN** steady-state torch model selection does not choose CUDA unless an
  explicit device override asks for it
- **AND** normal mode may retain CPU-side caches for latency

#### Scenario: Performance mode is explicit opt-in

- **WHEN** the effective mode is `performance`
- **THEN** CUDA may be selected only if the GPU capability and free-VRAM checks pass
- **AND** the policy may preload and retain warm caches for speed

### Requirement: Quiet Mode Reclaims Idle Model And Cache Residency

The system SHALL reclaim resident model and large cache memory in quiet mode.
Entering quiet mode SHALL unload loaded model singletons and SHALL evict large
CPU-side caches that are safe to rebuild lazily, including vector matrices, CLIP
matrices, BM25 corpora/tokens, parsed-page cache entries, resolver cache entries,
and hot find-result cache entries. Idle unload SHALL repeat this reclamation after
the configured idle window. Eviction MUST preserve correctness: later requests
MUST rebuild or reload the needed data from the vault or sidecars.

#### Scenario: Switching into quiet unloads resident resources

- **WHEN** the process has loaded models and large find/index caches
- **AND** the effective mode changes to `quiet`
- **THEN** the process unloads model singletons and evicts large CPU-side caches
- **AND** lightweight freshness and inbound-link metadata needed to avoid
  unnecessary full-vault work remains available when safe

#### Scenario: Idle quiet process reclaims resources

- **WHEN** the effective mode is `quiet`
- **AND** a loaded model or large cache has been idle longer than the configured
  idle threshold
- **THEN** the idle reaper unloads or evicts that resource without crashing the
  process

#### Scenario: Cache eviction preserves correctness

- **WHEN** a large cache has been evicted in quiet mode
- **AND** a later request needs that cache's data
- **THEN** the request rebuilds or reloads the data from the authoritative vault
  or sidecar source
- **AND** it returns the same result it would return with a warm cache over the
  same underlying state

### Requirement: Resource Status Is No-Allocation

The system SHALL provide a scriptable resource status surface that reports the
effective mode, policy fields, model residency, large-cache residency, deferred
work counters, and GPU memory accounting when already available. Gathering status
MUST NOT load models, create sidecars, read entire matrices, or create a CUDA
context. If a metric cannot be read without allocation or a platform-specific
probe is unavailable, the status SHALL report it as unknown or unavailable.

#### Scenario: Status does not create CUDA context

- **WHEN** the process has not initialized CUDA
- **AND** the user requests resource status
- **THEN** the status command completes without initializing CUDA
- **AND** CUDA memory accounting is reported as unavailable or not initialized

#### Scenario: Status reports loaded resources

- **WHEN** models or large CPU-side caches are resident
- **THEN** resource status reports their loaded state and best-effort size or row
  counts
- **AND** the status command does not load any missing resource to compute those
  fields

### Requirement: GPU Use Degrades To CPU On Unsupported Or Marginal Hosts

The system SHALL treat CPU as the universal baseline. A host with no CUDA, a torch
build without usable CUDA, a CUDA probe failure, or a GPU below the configured
free-VRAM threshold MUST degrade to CPU without failing startup, mode status,
doctor checks, `find`, or writer paths. Warm-up and optional model loads MUST
soft-fail and continue where CPU fallback is possible.

#### Scenario: CPU-only host starts cleanly

- **WHEN** the host has no usable CUDA device
- **THEN** the server starts and reports CPU-backed policy without raising a GPU
  error

#### Scenario: Marginal GPU is declined

- **WHEN** a GPU exists but the free-VRAM probe reports less than the configured
  threshold
- **THEN** Exomem declines CUDA for policy-gated model work
- **AND** it falls back to CPU instead of attempting a GPU allocation

### Requirement: Auto-Quiet Is Optional And Soft-Fail

The system SHALL keep automatic quiet-mode switching disabled by default. When
enabled, auto-quiet SHALL observe local machine pressure signals such as GPU
pressure or memory pressure without importing torch or allocating CUDA solely for
the probe. If probes are unavailable, fail, or are unsupported on the platform,
auto-quiet SHALL log or report the unavailable signal and continue with manual
mode behavior. Auto-quiet MUST NOT override an explicit `EXOMEM_MODE` environment
pin.

#### Scenario: Auto-quiet is disabled by default

- **WHEN** no auto-quiet configuration is enabled
- **THEN** Exomem changes modes only through the existing manual mode controls

#### Scenario: Pressure enters and leaves quiet

- **WHEN** auto-quiet is enabled
- **AND** the configured pressure signal remains active past the hysteresis window
- **THEN** Exomem switches the config-file mode to `quiet` and records the prior
  config-file mode
- **AND** when pressure clears past the restore window, Exomem restores the prior
  mode unless the user changed modes manually while pressure was active

#### Scenario: Probe failure does not change mode

- **WHEN** auto-quiet is enabled but every configured pressure probe is unavailable
  or fails
- **THEN** Exomem does not crash
- **AND** Exomem does not change the current mode based on the failed probe

### Requirement: Active compute remains host-cooperative
Every Exomem service and disposable worker SHALL apply a conservative native CPU concurrency budget before model or numeric runtimes initialize. The common budget SHALL replace inherited library-specific thread counts unless the operator explicitly enables an unsafe compatibility escape hatch. The long-lived server SHALL also bound synchronous request-worker concurrency and derive model-compute admission from it so at least half of the effective workers, and never fewer than one, remain outside the model queue. The default budgets SHALL keep foreground work schedulable on a CPU-only host, SHALL be operator-overridable without violating that reserve, and SHALL report their source plus any unsafe escape. Background media work SHALL run with lower scheduling priority than interactive work on platforms that expose such a facility.

#### Scenario: CPU-only media processing
- **WHEN** a standard installation processes media without a usable accelerator
- **THEN** each Exomem process limits native compute to the effective CPU budget
- **AND** the service remains responsive to health and status requests while the job runs

#### Scenario: Multiple local cells are active
- **WHEN** two or more native services perform legitimate background work concurrently
- **THEN** each service and its children retain their own bounded compute envelope
- **AND** one cell cannot obtain unbounded host parallelism merely because its queue is non-empty

#### Scenario: Many clients submit synchronous tools
- **WHEN** concurrent MCP or REST clients exceed the configured synchronous request budget
- **THEN** excess work waits behind a bounded worker limiter
- **AND** model admission reserves capacity for quick health, status, and filesystem work instead of blocking every worker behind the model execution gate

#### Scenario: Operator lowers the synchronous worker budget
- **WHEN** an operator selects any supported synchronous worker value
- **THEN** model admission is at most four and at most half of that effective value
- **AND** values that cannot reserve at least one general worker are rejected

#### Scenario: Host exports a larger BLAS pool
- **WHEN** the service inherits `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, or an equivalent library setting larger than the Exomem CPU budget
- **THEN** bootstrap replaces it with the Exomem budget before importing that runtime
- **AND** weakening the common bound requires an explicit reported compatibility escape hatch

#### Scenario: Operator selects a larger budget
- **WHEN** an operator explicitly configures a valid larger CPU budget
- **THEN** the configured value replaces the conservative default
- **AND** diagnostics disclose the effective value and the source of the override

### Requirement: Native service managers provide a contention backstop
Native service definitions SHALL request background CPU scheduling on platforms that support it. Linux user-service installations SHALL additionally carry a finite CPU backstop that includes spawned media children. The rendered quota SHALL admit at most half of the detected online logical CPUs, capped at four cores, and SHALL reserve capacity even on a one-CPU host. Portable in-process limits remain the primary cross-platform control.

#### Scenario: Native library ignores its runtime limit
- **WHEN** a native dependency creates more runnable threads than the Exomem budget permits
- **THEN** the service-manager envelope prevents that service cgroup from monopolising the Linux host
- **AND** health and operator control processes retain scheduling access under contention

#### Scenario: One-CPU Linux host
- **WHEN** the installer renders the service for a host with one online logical CPU
- **THEN** the service quota is no greater than half a CPU
- **AND** the same formula never grants more than four CPUs on a larger host
