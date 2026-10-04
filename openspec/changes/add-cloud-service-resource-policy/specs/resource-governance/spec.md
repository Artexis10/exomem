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

### Requirement: Node memory policy is an explicit deployment dependency

The operator SHALL declare MemoryQoS adoption separately from cell image selection. The pinned K3s configuration SHALL default to disabled, preserve requests and limits during node-policy activation, and expose its effective throttling and protection settings in deployment evidence. A separately declared scheduler-request change SHALL retain the tenant limits and require acceptance of the resulting effective controls; the original request configuration is not proof of that new envelope. Promotion SHALL use exact-source real-model acceptance under the intended effective controls and sufficient physical headroom; an outcome materially assisted by unrelated host reclaim SHALL NOT establish production headroom. Adoption and rollback SHALL account for all affected sibling workloads and actual container controls, not only the owner image. A cell SHALL NOT obtain authority to change cgroups or node configuration.

#### Scenario: Successful treatment does not enable the shared node
- **WHEN** an isolated MemoryQoS treatment meets numerical gates
- **THEN** node adoption still requires production-equivalent outcome and sibling-capacity evidence
- **AND** default-disabled source delivery and a per-cell image change do not enable MemoryQoS on other workloads

#### Scenario: Node rollback verifies effective controls
- **WHEN** the operator disables an adopted node memory configuration
- **THEN** the ordinary node and container lifecycle verifies effective memory controls and readiness
- **AND** canonical writes and unfinished durable work are preserved

### Requirement: Service-policy allocator release is bounded and shared

Existing completed-work allocator release callers SHALL share one process-wide minimum interval of five seconds under the validated service-v1 profile. Local and legacy profiles SHALL retain the sixty-second interval. Calls suppressed by that allowance SHALL retain the existing pending-release retry behavior. The policy SHALL NOT add a per-request trim, periodic release worker or core unload. Eligibility for an allocator release SHALL NOT be represented as proof of passing memory or latency acceptance.

#### Scenario: Adjacent completed service turns can return freed heap
- **WHEN** completed service-v1 work asks to return freed allocator pages five seconds after the last eligible call
- **THEN** that call is eligible under the shared allowance
- **AND** earlier calls are suppressed and remain pending for an existing retry caller
- **AND** actual cgroup peaks and latency still govern promotion

#### Scenario: Local and legacy release timing is preserved
- **WHEN** a local or legacy completed-work caller requests a trim inside sixty seconds of the last eligible call
- **THEN** the call remains suppressed by the existing allowance

### Requirement: Service-policy status preserves no-allocation diagnostics

Resource status SHALL report effective Cloud profile/source, current core loaded/readiness state, pending semantic debt count and oldest original debt age, and background budget/activity without loading models, allocating corpus representations, creating sidecars or initializing CUDA. Unknown measurements SHALL remain unknown. Retry timestamps MUST NOT reset the age of unresolved work. Status MUST NOT equate canonical commit, Kubernetes readiness and completed semantic publication.

#### Scenario: Debt retries do not disguise its age
- **WHEN** a semantic receipt remains unresolved across retries
- **THEN** resource status reports age from its original unresolved creation time
- **AND** requesting status neither warms missing resources nor represents the work as completed

### Requirement: Service-policy governed chunk scoring has bounded residency

Under service-v1, ordinary chunk recall and write-advisory scoring SHALL preserve the existing eligibility and exact score contracts while reading bounded vector blocks from a consistent sidecar snapshot. They SHALL NOT retain or replace a corpus-sized chunk matrix. Winner text hydration and stored space identity SHALL refer to that same snapshot. Context-pack pairwise scoring SHALL read only the selected parents' exact current stored rows, reduce pairwise maxima over bounded vector/score blocks, and SHALL NOT encode missing or drifted rows. Explicit all-vector consumers SHALL NOT populate the shared persistent matrix cache under service-v1; this does not claim their existing transient algorithm is bounded. Doctor SHALL count rows without materializing vectors. Local and legacy scoring behavior SHALL remain unchanged.

#### Scenario: Governed search and write do not warm the full matrix
- **WHEN** an eligible service-v1 query and ordinary governed write score stored chunk vectors
- **THEN** only bounded vector blocks and top-k winners are retained
- **AND** disallowed rows cannot enter results and the current model-space identity is preserved
- **AND** resource acceptance still fails if actual cgroup peaks or latency exceed the declared gates

#### Scenario: Concurrent publication does not mix a scoring snapshot
- **WHEN** a parent or stored vector space is replaced during chunk scoring
- **THEN** the result's vectors, width, row metadata and hydrated winner texts come from one consistent read snapshot
- **AND** no result combines old scores with newly replaced text

### Requirement: Service-policy parsed-page retention is byte bounded

Under service-v1, the parsed-page cache SHALL enforce a byte budget alongside its existing entry count. Its charge SHALL include parsed body/frontmatter and retained stripped/lowercase body variants. Oversized pages SHALL remain readable without retention or collateral eviction of unrelated cached pages. Replacement, path/scope invalidation and release SHALL retire charges. With no explicit byte override, local/legacy cache defaults SHALL remain unchanged. The cache budget SHALL NOT be represented as whole-process acceptance or disk-semantic serving authority.

#### Scenario: Large pages cannot consume unbounded retained cache bytes
- **WHEN** service-v1 reads differently sized pages under a byte budget
- **THEN** least-recently-used entries are evicted by charged bytes as well as count
- **AND** a page larger than the budget is returned correctly but is not retained
- **AND** native memory and latency gates still govern promotion
