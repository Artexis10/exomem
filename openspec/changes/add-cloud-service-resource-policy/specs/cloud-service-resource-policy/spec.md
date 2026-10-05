## Purpose

Defines the managed Cloud service's compute policy and measurable responsiveness within per-tenant and aggregate resource budgets, independently of workstation footprint modes.

## ADDED Requirements

### Requirement: Supported vault configuration independence

The service policy SHALL apply through existing vault discovery, schema and semantic indexing contracts to all supported vault configurations. Work classification MUST NOT depend on maintainer-specific path names, personal metadata or a particular optional schema feature. Deployment resource policy SHALL remain operator-controlled code. Inputs exceeding the declared resource budget SHALL remain visibly deferred/refused without changing canonical data or pretending configuration independence guarantees unlimited capacity.

#### Scenario: Alternative supported vault layout
- **WHEN** a Cloud cell uses an alternative supported schema or vault-root configuration
- **THEN** ordinary save, deferred semantic recovery and fresh recall use the same service policy and publication fences
- **AND** no private folder convention or tenant performance-mode change is required

### Requirement: Operator-owned Cloud service profile

Cloud SHALL support explicit `legacy` and `service-v1` deployment profiles. A missing selection SHALL preserve legacy behavior. Only operator-controlled Cloud deployment configuration SHALL select `service-v1`; tenant requests, engagement settings and generic model configuration MUST NOT change it. Invalid selections or selecting the service profile outside a valid Cloud deployment SHALL fail configuration with a value-free error. The system SHALL report profile and selection source separately from stored workstation mode and engagement preference; profile selection MUST NOT rewrite either stored preference.

#### Scenario: Operator canaries a service profile
- **WHEN** an operator upgrades one Cloud cell to a deployment explicitly selecting `service-v1`
- **THEN** that cell resolves the service policy while cells on legacy deployments retain their previous policy
- **AND** tenant configuration cannot switch or override the service policy

#### Scenario: Reviewer canary precedes owner adoption
- **WHEN** the operator separately authorizes one reviewer-only service-profile canary on existing infrastructure before dedicated owner adoption
- **THEN** it preserves its PVC, placement and tenant resources, shared-node memory controls and unselected cell images
- **AND** it requires its own verified identity, backup, source/preferences, measured warmed capacity, lifetime peak and workflow outcomes under the existing numeric gates
- **AND** its evidence governs that reviewer deployment only, leaving owner acceptance, fleet promotion and friend-admission gates open

#### Scenario: Invalid deployment selection
- **WHEN** the selection is unknown or `service-v1` is selected outside valid Cloud deployment
- **THEN** startup reports a configuration error without echoing credentials or arbitrary supplied values

#### Scenario: Stored workstation mode changes in Cloud
- **WHEN** the stored workstation mode changes while the service profile is running
- **THEN** live mode application preserves the operator-selected core residency and service compute policy
- **AND** the stored selection is not silently rewritten to match that policy

### Requirement: Selective core residency and truthful degradation

A healthy running `service-v1` cell SHALL keep its actual serving core recall encoder resident and ready on CPU, without automatically enabling or preloading optional model groups or corpus caches. Core failures SHALL preserve lexical service and canonical writes with durable semantic debt while reporting semantic unavailability accurately. Idle reclamation SHALL remain available for safely reclaimable optional models and caches without unloading an in-flight resource. The profile MUST NOT change vector identity, engagement settings or mutation authority.

#### Scenario: Healthy cell remains warm after idle
- **WHEN** a healthy service-profile cell passes the idle reclamation window
- **THEN** its core encoder remains usable without a new load
- **AND** inactive optional models and corpus caches remain eligible for safe reclamation

#### Scenario: Core preload fails
- **WHEN** the serving encoder cannot become usable
- **THEN** semantic readiness is degraded or unavailable and lexical retrieval remains available
- **AND** successful canonical writes are not represented as semantically indexed unless their exact derived version has published

#### Scenario: Disk-backed recall has no matrix to warm
- **WHEN** service-v1 background activation completes its startup drain
- **THEN** it skips the redundant matrix-warm vector scan while preserving core-encoder and request-path warm operations
- **AND** local and legacy matrix-warm behavior remains unchanged

#### Scenario: Resolver warming yields to active requests
- **WHEN** a projected-resolver background warm overlaps an active foreground request
- **THEN** shared fallback walks yield through the existing bounded bulk scheduling scope
- **AND** the warm resumes with the same resolver contents when the request finishes, while direct callers retain their existing behavior

### Requirement: Cloud outcome and capacity gates

Promotion of `service-v1` SHALL require measured ordinary-write and query outcomes on the maintained large-vault fixture both with and without sustained bulk work. Twenty ordinary-note samples in each workload, each no more than 1 KiB UTF-8 and three chunks plus three semantic units, SHALL publish the exact committed version into both projections within 5 seconds at p95 and 10 seconds for every sample after commit; request-to-publication SHALL be within 10 seconds at p95 and 20 seconds for every sample. Eligible warm hybrid queries over the maintained 25-query fixture SHALL retain core vector participation with p95 latency at most 3 seconds and no sample over 10 seconds or newly degraded/refused. Outcome checks SHALL distinguish canonical commit from derived publication and measure post-idle and durable restart recovery separately.

The unchanged tenant CPU/memory limits SHALL bound acceptance. Cgroup peak memory SHALL remain at most 80% of the cell memory limit, with no workload-induced OOM or restart. New admissions SHALL use fresh simultaneous warm-cell and platform usage/reservation evidence, charge at least the greater of declared memory request and measured warm peak for each proposed cell, and preserve at least 20% node allocatable memory headroom. A missed gate SHALL block promotion rather than silently relax its threshold. Arbitrary bulk inputs and explicitly unavailable/migrating vector spaces SHALL remain visibly pending/degraded rather than be counted as passing ordinary samples.

#### Scenario: Ordinary save during an import
- **WHEN** a service-profile cell accepts the bounded ordinary-note workload while bulk import runs
- **THEN** exact committed chunks and semantic units publish within the stated freshness gates
- **AND** hybrid query participation and latency satisfy the stated gates without caller retry

#### Scenario: Warm capacity is insufficient
- **WHEN** cold-cell attachment capacity appears sufficient but fresh warmed usage/reservations would violate the node headroom gate
- **THEN** further friend admission and fleet promotion remain blocked
- **AND** the outcome reports insufficient resource capacity without altering tenant limits

### Requirement: Shared-worker qualification matches scheduler placement

Shared-worker acceptance SHALL exercise competing real cells at the intended scheduler requests and effective node policy. Results SHALL claim only measured occupancy and workload. Serving and maintenance replacement SHALL retain any node capability on which acceptance depends. New-admission resource capacity SHALL account for eligible worker state, pending commitments, platform/maintenance reservations, CPU competition, memory and volume topology. Attachment slots alone SHALL NOT establish resource capacity. A transient capacity observation failure SHALL defer new provisioning with an observable reason and automatic reconciliation, without stopping existing cells or revoking entitlements.

Browser and OAuth admission SHALL count only capacity observations from the preceding five minutes, measured at database statement time after the existing serializing lock, with the existing non-deleted commitment count. Missing or future-dated observations SHALL be ineligible. An expired observation SHALL NOT consume an invite or OAuth transaction. A current observation SHALL restore eligibility automatically without changing existing tenant access.

#### Scenario: Capacity observation expires and recovers
- **WHEN** published worker capacity is older than five minutes
- **THEN** browser and OAuth admission defer without consuming the invitation or creating a cell
- **AND** a controller refresh permits the same invitation to be admitted under the ordinary capacity check

#### Scenario: Cells compete and recover
- **WHEN** a shared-worker envelope is qualified
- **THEN** separate cells alternate import and ordinary save/query workloads, including restart under peer load
- **AND** measured cells meet the existing freshness, query, memory and recovery gates at the intended scheduler requests
- **AND** the result does not claim larger unmeasured occupancy

#### Scenario: Scheduling inputs change
- **WHEN** an operator changes a qualified memory request or required node capability
- **THEN** acceptance records actual resulting controls and requalifies that configuration
- **AND** a selective canary preserves unselected resource/template inputs

### Requirement: Shared admission uses one interchangeable qualified envelope

Shared-worker selection SHALL default off and support selective qualification followed by explicit promotion for ordinary future admissions. Selective qualification SHALL preserve unselected resources, templates and render digests; dedicated placement SHALL remain independent. Serving, backup and restore SHALL use the same compatible worker profile and CSI attachability domain. Conflicting operator selections SHALL fail configuration before changing workloads.

Selected qualification SHALL publish a complete zero-slot general-admission snapshot and retire incompatible positive rows before changing ordinary placement authority. This cutover SHALL serialize with the existing admission transaction lock, and the migration inventory SHALL be established after publication so an earlier redemption cannot commit outside it. Existing cells SHALL migrate through the established stop/change/start protocol, completing active holds and releasing volume users before changing resource or placement inputs. Selected migration SHALL finish before all-shared promotion. Promotion SHALL replace the advertised legacy domain atomically and publish qualified capacity only once a fresh ordinary cell ID can use the shared profile/domain. Rollback SHALL retire shared capacity and use the same stopped-cell protocol before restoring legacy placement/capacity. Capacity closure SHALL defer prospective admissions automatically, preserve unconsumed invitations and leave existing access unaffected; complete all-shared reconciliation SHALL reopen qualified admission without a manual unlock.

Within the initial scalar admission contract, shared capacity SHALL represent one interchangeable qualified resource/topology envelope. Absolute capacity SHALL be the per-node minimum of qualified occupancy, CPU, memory and cell attachment capacity, summed only after per-node floors and minima. Scheduler requests SHALL cover the qualified footprint; platform reserves, Pending commitments and supported lifecycle overlap SHALL be charged without double-counting ordinary cells already charged by admission. Deletion-in-progress SHALL retain its commitment until physical lifecycle cleanup releases it. The complete capacity pass, including absent-node zeroing, SHALL publish atomically. Missing or incomplete observations MUST NOT acquire a fresh positive capacity timestamp.

The initial cohort SHALL be bounded to one shared worker until the runtime, resources and intended occupancy pass the fixed outcome gates. Multiple workers SHALL require qualification of the occupancy Kubernetes can actually concentrate under their real requests and allocatable resources; a published slot ceiling or preferred spread SHALL NOT substitute for that proof. A second independent resource class or storage domain SHALL require a compatible admission extension before its slots are added.

#### Scenario: Two node capacities change in one pass
- **WHEN** capacity moves between workers during a controller pass
- **THEN** admission observes either the previous complete pass or the new complete pass
- **AND** it cannot combine increased new capacity with unreduced old capacity

#### Scenario: Free attachments exceed resource capacity
- **WHEN** a compatible worker has spare volume attachments but qualified CPU or memory supports fewer cells
- **THEN** its absolute published slots use the lower resource capacity
- **AND** running, stopped and Pending cell commitments still consume the admission count

#### Scenario: An unrelated worker joins the cluster
- **WHEN** a worker has a different resource profile or incompatible CSI topology
- **THEN** it adds no slots to the qualified shared envelope
- **AND** a later compatible observation recovers eligibility without a manual unlock or changes to existing tenant access

#### Scenario: Pending work cannot use the shared worker
- **WHEN** an unscheduled Pod's fixed placement constraints or taint tolerance prove it cannot run on the shared worker
- **THEN** its requests and projected volume attachments do not reduce that worker's capacity
- **AND** eligible or uncertain Pending work and all actually scheduled demand remain charged

#### Scenario: A fresh ordinary ID arrives during promotion
- **WHEN** shared placement is being qualified for selected existing cells
- **THEN** ordinary admission sees zero slots rather than selected-only worker capacity or old positive legacy rows
- **AND** after completed migration and all-shared promotion, a newly minted unlisted cell ID uses the same qualified resources and serving/maintenance placement
- **AND** promotion does not change an active hold Job template or unexpectedly relocate an unmigrated existing cell

### Requirement: Safe service-policy rollback

An operator SHALL be able to return a canary to a verified compatible legacy deployment while preserving post-upgrade canonical writes and outstanding durable index work. Rollback MUST NOT restore an older vault snapshot over newer committed data or relax tenant identity, authorization or publication fences.

#### Scenario: Canary misses its outcome budget
- **WHEN** a canary fails service acceptance after accepting new canonical writes
- **THEN** promotion stops and the operator can return to a compatible legacy deployment
- **AND** the new writes and their outstanding derived work remain recoverable


### Requirement: Cancellable owned graph recovery

Service shutdown SHALL cancel synchronous startup, registered and query-triggered graph recovery between complete private work units, source proofs and retries, retain durable pending work, and release its rebuild owner and temporary artifacts. Cancellation alone MUST NOT publish partial work or invent source movement. An already-started atomic replacement SHALL complete its publication bookkeeping and remain successful for covered waiters. Shutdown SHALL seal new graph admissions and coalesced successors, stop the owned graph-drain producer, skip later startup draining and warming, and join activation and graph workers rather than detach them. A failed bounded service join MUST NOT enter the one-shot CLI completion wait; ordinary CLI completion behavior SHALL remain unchanged.

#### Scenario: Shutdown during a write-warmed startup rebuild
- **WHEN** shutdown arrives while startup recovery builds or proves a private graph sidecar
- **THEN** recovery unwinds without publishing the unfinished sidecar or retiring its outstanding debt
- **AND** the activation thread ends within the existing native shutdown grace window

#### Scenario: Shutdown after activation while registered graph work remains
- **WHEN** activation has ended but a registered or query-triggered rebuild remains active
- **THEN** the service lifetime cancels unfinished private work and rejects a subsequent rebuild
- **AND** any admitted replacement completes before its worker ends
- **AND** server exit does not add a 300-second CLI drain after service cleanup


### Requirement: Dedicated placement is operator selected and source managed

Dedicated-cell selection SHALL default to empty and belong only to operator deployment configuration. A selected cell's serving StatefulSet and backup/restore Jobs SHALL receive only a fixed own-cell selector and matching NoSchedule toleration. Admission SHALL allow absent placement during transition or that exact pair for an operator-selected own-cell namespace; arbitrary scheduling fields SHALL remain forbidden. Unselected render templates and digests SHALL remain unchanged. The agent role SHALL register and converge the matching node reservation independently of its default-disabled memory policy.

Reserved nodes SHALL publish zero general admission slots, preserve actual attachment-use reporting, and be excluded from node-removal capacity. Attachment slots SHALL NOT be reported as warmed memory capacity. Removing a reserved node SHALL require clearing its selected workloads and verifying volume relocation first.

Relocation and rollback SHALL stop the selected cell through its ordinary desired-state lifecycle, wait for completed holds and released volume users, change placement and resume. They SHALL preserve the existing PVC, canonical writes and durable debt, without restoring older data.

#### Scenario: Owner reservation does not move ordinary siblings
- **WHEN** the operator selects one cell for a reserved node
- **THEN** only its serving and maintenance workloads receive that reservation
- **AND** unselected cells retain their templates and digests and cannot use the reserved node

#### Scenario: Reserved attachment capacity cannot admit unrelated cells
- **WHEN** a reserved agent has spare CSI attachment slots
- **THEN** those slots do not increase general admission or removal capacity
- **AND** its actual attached volume count remains observable

#### Scenario: Placement changes preserve maintenance and newer data
- **WHEN** the operator relocates a selected cell or rolls placement back
- **THEN** stop/change/start sequencing completes current holds before changing Job placement
- **AND** the same volume and post-upgrade writes are preserved
