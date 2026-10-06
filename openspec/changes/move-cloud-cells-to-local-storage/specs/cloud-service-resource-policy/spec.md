## MODIFIED Requirements

### Requirement: Shared admission uses one interchangeable qualified envelope

Shared-worker selection SHALL default off and support selective qualification followed by explicit promotion for ordinary future admissions. Selective qualification SHALL preserve unselected resources, templates and render digests; dedicated placement SHALL remain independent. Serving, backup and restore of a cell SHALL use the same compatible worker profile and the storage domain of the cell's bound volume. Conflicting operator selections SHALL fail configuration before changing workloads.

Selected qualification SHALL publish a complete zero-slot general-admission snapshot and retire incompatible positive rows before changing ordinary placement authority. This cutover SHALL serialize with the existing admission transaction lock, and the migration inventory SHALL be established after publication so an earlier redemption cannot commit outside it. Existing cells SHALL migrate through the established stop/change/start protocol, completing active holds and releasing volume users before changing resource or placement inputs. Selected migration SHALL finish before all-shared promotion. Promotion SHALL replace the advertised legacy domain atomically and publish qualified capacity only once a fresh ordinary cell ID can use the shared profile/domain. Rollback SHALL retire shared capacity and use the same stopped-cell protocol before restoring legacy placement/capacity. Capacity closure SHALL defer prospective admissions automatically, preserve unconsumed invitations and leave existing access unaffected; complete all-shared reconciliation SHALL reopen qualified admission without a manual unlock.

Within the initial scalar admission contract, shared capacity SHALL represent one interchangeable qualified resource/topology envelope. Absolute capacity SHALL be the per-node minimum of qualified occupancy, CPU, memory and cell storage capacity, summed only after per-node floors and minima. Scheduler requests SHALL cover the qualified footprint, including one backup Job alongside a serving cell; platform reserves, Pending commitments and supported lifecycle overlap SHALL be charged without double-counting ordinary cells already charged by admission. Deletion-in-progress SHALL retain its commitment until physical lifecycle cleanup releases it. The complete capacity pass, including absent-node zeroing, SHALL publish atomically. Missing or incomplete observations MUST NOT acquire a fresh positive capacity timestamp.

The initial cohort SHALL be bounded to one shared worker until the runtime, resources and intended occupancy pass the fixed outcome gates. Multiple workers SHALL require qualification of the occupancy Kubernetes can actually concentrate under their real requests and allocatable resources; a published slot ceiling or preferred spread SHALL NOT substitute for that proof. A second independent resource class or storage domain SHALL require a compatible admission extension before its slots are added. Moving cells from one storage domain to another SHALL NOT count as adding one: from a cutover under a capacity closure, only the target domain SHALL publish slots, and cells still on the source domain SHALL count against them until they move.

#### Scenario: Two node capacities change in one pass
- **WHEN** capacity moves between workers during a controller pass
- **THEN** admission observes either the previous complete pass or the new complete pass
- **AND** it cannot combine increased new capacity with unreduced old capacity

#### Scenario: Free storage exceeds resource capacity
- **WHEN** a compatible worker has spare cell storage but qualified CPU or memory supports fewer cells
- **THEN** its absolute published slots use the lower resource capacity
- **AND** running, stopped and Pending cell commitments still consume the admission count

#### Scenario: Cells still on the source storage domain

- **WHEN** the configured storage domain has switched while some cells still run on volumes of the previous domain
- **THEN** only the new domain's nodes publish slots
- **AND** every non-deleted cell, migrated or not, counts against those slots

#### Scenario: An unrelated worker joins the cluster
- **WHEN** a worker has a different resource profile or an incompatible storage domain
- **THEN** it adds no slots to the qualified shared envelope
- **AND** a later compatible observation recovers eligibility without a manual unlock or changes to existing tenant access

#### Scenario: Pending work cannot use the shared worker
- **WHEN** an unscheduled Pod's fixed placement constraints or taint tolerance prove it cannot run on the shared worker
- **THEN** its requests and projected storage do not reduce that worker's capacity
- **AND** eligible or uncertain Pending work and all actually scheduled demand remain charged

#### Scenario: A fresh ordinary ID arrives during promotion
- **WHEN** shared placement is being qualified for selected existing cells
- **THEN** ordinary admission sees zero slots rather than selected-only worker capacity or old positive legacy rows
- **AND** after completed migration and all-shared promotion, a newly minted unlisted cell ID uses the same qualified resources and serving/maintenance placement
- **AND** promotion does not change an active hold Job template or unexpectedly relocate an unmigrated existing cell

### Requirement: Dedicated placement is operator selected and source managed

Dedicated-cell selection SHALL default to empty and belong only to operator deployment configuration. A selected cell's serving StatefulSet and backup/restore Jobs SHALL receive only a fixed own-cell selector and matching NoSchedule toleration. Admission SHALL allow absent placement during transition or that exact pair for an operator-selected own-cell namespace; arbitrary scheduling fields SHALL remain forbidden. Unselected render templates and digests SHALL remain unchanged. The agent role SHALL register and converge the matching node reservation independently of its default-disabled memory policy.

Reserved nodes SHALL publish zero general admission slots, preserve actual storage-use reporting, and be excluded from node-removal capacity. Storage slots SHALL NOT be reported as warmed memory capacity. Removing a reserved node SHALL require clearing its selected workloads and relocating their cells first.

Relocation and rollback SHALL stop the selected cell through its ordinary desired-state lifecycle and wait for completed holds and released volume users. When the target node can reach the cell's existing volume, they SHALL change placement and resume on that volume. When it cannot, they SHALL take a fresh stopped backup, restore it on the target and keep the original volume until the relocated cell is accepted. Either way they SHALL preserve canonical writes and durable debt, without restoring data older than the stop.

#### Scenario: Owner reservation does not move ordinary siblings
- **WHEN** the operator selects one cell for a reserved node
- **THEN** only its serving and maintenance workloads receive that reservation
- **AND** unselected cells retain their templates and digests and cannot use the reserved node

#### Scenario: Reserved storage capacity cannot admit unrelated cells
- **WHEN** a reserved agent has spare cell storage
- **THEN** that storage does not increase general admission or removal capacity
- **AND** its actual storage use remains observable

#### Scenario: Placement changes preserve maintenance and newer data
- **WHEN** the operator relocates a selected cell or rolls placement back
- **THEN** stop/change/start sequencing completes current holds before changing Job placement
- **AND** every write made before the stop is present after the move
