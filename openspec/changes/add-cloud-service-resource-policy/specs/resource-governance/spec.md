## MODIFIED Requirements

### Requirement: Safe normal-mode residency
Outside an explicit Cloud service profile, Normal mode SHALL avoid startup model and O(vault) cache preloads and SHALL run idle resource reclamation by default. Performance mode MAY preload when explicitly selected. Environment overrides SHALL remain available for deliberate operator choices. An explicit Cloud service profile SHALL independently resolve selective core residency and validated service budgets, without implicitly selecting workstation Performance or retaining all corpus caches. Conflicting legacy compute overrides SHALL be rejected under the service profile rather than silently defeating it.

#### Scenario: Normal-mode startup
- **WHEN** the service starts in normal mode without an explicit Cloud service profile
- **THEN** models and O(vault) CPU caches remain lazy
- **AND** the idle reaper is active

#### Scenario: Explicit performance mode
- **WHEN** an operator selects performance mode outside an explicit Cloud service profile
- **THEN** the service may preload latency-oriented resources
- **AND** the choice is visible through resource status

#### Scenario: Cloud policy has independent residency
- **WHEN** the Cloud service profile selects core residency
- **THEN** resource status reports that effective service policy separately from workstation mode
- **AND** stored workstation mode and engagement preference are not rewritten

### Requirement: Persistent-core resource acceptance envelope
Release verification SHALL measure the persistent service separately from transient workers. Outside an explicit Cloud service profile, after worker idle exit on the maintained fixture, the acceptance targets SHALL be no active media worker, less than 200 MiB GPU delta/no CUDA compute process, no more than 512 MiB persistent-core RSS before user-triggered cache growth, and less than 1% idle CPU averaged over 60 seconds. Cloud service-profile verification SHALL use its separate per-cell cgroup and aggregate warmed-capacity gates instead of treating a deliberately resident encoder as satisfying the local lazy-core fixture target.

#### Scenario: Resource verification after media work
- **WHEN** a representative media job completes outside an explicit Cloud service profile and the worker idle interval elapses
- **THEN** verification records zero media workers and the persistent-core RSS/CPU/GPU metrics
- **AND** an exceeded target is treated as a release failure or explicitly documented blocker

#### Scenario: Cloud verification uses actual cell allocation
- **WHEN** an explicit Cloud service profile is evaluated for promotion
- **THEN** verification records real cgroup peaks and simultaneous warmed node capacity
- **AND** desktop RSS or cold-cell memory is not substituted for those measurements

## ADDED Requirements

### Requirement: Service-policy status preserves no-allocation diagnostics

Resource status SHALL report effective Cloud profile/source, current core loaded/readiness state, pending semantic debt count and oldest original debt age, and background budget/activity without loading models, allocating corpus representations, creating sidecars or initializing CUDA. Unknown measurements SHALL remain unknown. Retry timestamps MUST NOT reset the age of unresolved work. Status MUST NOT equate canonical commit, Kubernetes readiness and completed semantic publication.

#### Scenario: Debt retries do not disguise its age
- **WHEN** a semantic receipt remains unresolved across retries
- **THEN** resource status reports age from its original unresolved creation time
- **AND** requesting status neither warms missing resources nor represents the work as completed
