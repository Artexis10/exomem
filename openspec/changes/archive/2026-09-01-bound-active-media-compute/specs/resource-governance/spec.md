## ADDED Requirements

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
