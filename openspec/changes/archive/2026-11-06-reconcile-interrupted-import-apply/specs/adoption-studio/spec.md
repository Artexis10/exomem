## MODIFIED Requirements

### Requirement: Durable resumable run object

An adoption run SHALL be a canonical-file-backed object stored under `Knowledge Base/_Adoption/runs/<run_id>/` as `run.json` (lifecycle, selection, plan, outcomes) and `proposals.json` (agent proposals), each written only through `vault.batch_atomic_write` so it inherits the access-tier backstop and writer-lease fence. The run SHALL be fully reconstructable after a process restart with no dependence on any rebuildable sidecar or in-memory state. `status` without a `run_id` SHALL list runs newest-first; The internal browser `status` with a `run_id` SHALL return the full run document plus computed `staleness`, an `interrupted` flag, a `proposals_summary`, and product-shaped `next_actions`.

#### Scenario: A run survives a process restart

- **WHEN** a run is created, a selection is set, and the process restarts
- **THEN** `status(run_id=...)` returns the run in its persisted phase with its selection intact, reconstructed from the on-disk `run.json`

#### Scenario: Listing runs is read-only and newest-first

- **WHEN** `status` is called with no `run_id`
- **THEN** it returns compact rows (id, phase, created, counts) ordered newest-first
- **AND** no run object or vault file is modified


### Requirement: Interrupted apply is visible and recoverable

An interrupted apply SHALL retain its plan, original mutation identity and resolved invocation scope. Status SHALL distinguish interrupted work from a budget stop and retain any continuation. An exact retry SHALL reconcile committed items and complete only the original invocation's pending work. Partial custody SHALL NOT establish that the whole call completed. Unprovable legacy scope SHALL remain explicitly unresolved.

#### Scenario: A crashed apply is reported and can resume

- **WHEN** the run is left in phase `applying` with incomplete outcomes
- **THEN** status reports `interrupted: true`
- **AND** an exact apply retry reconciles the original invocation without duplicating applied items

#### Scenario: A committed chunk has no mutation terminal

- **WHEN** an apply commits a chunk, stops before its terminal persists, and receives an identical retry
- **THEN** the service reconciles that invocation under the original mutation identity and completes its pending validated work
- **AND** it creates no duplicate Source, artifact or draft note and preserves original hashes

#### Scenario: A failed-only request stops before note settlement

- **WHEN** a failed-only invocation saves its Source and stops before saving its owed note
- **THEN** its exact retry retains the original failed subset and completes that note once
- **AND** changed outcome status does not remove the item from the invocation

#### Scenario: Abrupt interruption exposes part of a chunk

- **WHEN** interruption exposes a planned Source before its artifact or canonical bookkeeping exists
- **THEN** recovery validates the owned post-image, completes only missing matching components and records truthful outcomes
- **AND** a conflicting target is preserved and reported rather than overwritten

#### Scenario: An old subset has no sufficient scope evidence

- **WHEN** a legacy unknown invocation cannot prove its original subset
- **THEN** recovery reports that specific unresolved boundary and preserves the run, Sources and staging
- **AND** it does not widen the invocation or recommend a new mutation identity

#### Scenario: A budget stop is remaining work, not a crash

- **WHEN** apply returns a budget stop with pending items
- **THEN** status reports remaining work and the bound continuation without calling it interrupted

## ADDED Requirements

### Requirement: Bounded public run status

Public run status SHALL default to a summary with run revision, phase, plan identity, aggregate counts and continuation. Inventory, selection, plan and outcome details SHALL use bounded pages with offset, limit, total and next offset. Pages SHALL bind to one run revision, expose truncation or omission, and never embed original content. Status SHALL remain read-only. The internal browser view SHALL retain its full document.

An explicit `view="full"` SHALL retain the previous full document for browser compatibility and SHALL be exempt from the summary and page bounds.

#### Scenario: An agent inspects a large interrupted run

- **WHEN** public status reads a run with thousands of inventory, plan and outcome rows
- **THEN** the default reply fits the summary byte limit and reports the persisted aggregate counts and available continuation
- **AND** it does not embed the inventory, selected-path list, plan items or outcomes

#### Scenario: An agent pages through a stable run

- **WHEN** an agent requests each next page with the returned revision
- **THEN** every row in that view appears once in stable order within the page limits
- **AND** the final page has no next offset

#### Scenario: The run changes between pages

- **WHEN** an agent requests a page with an earlier run revision
- **THEN** status returns a stale-page diagnostic without modifying the run

#### Scenario: One metadata row exceeds the page byte limit

- **WHEN** a requested row cannot fit within one page
- **THEN** the page explicitly identifies its omission, advances the offset and retains truthful totals
- **AND** the reply stays bounded instead of trapping the caller on that row
